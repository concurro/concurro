//! Durable GitHub list rows with event-safe full scans.

use anyhow::{Result, ensure};
use chrono::{DateTime, Utc};
use concurro_db::{Database, UnitOfWork};
use serde::Serialize;
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

/// One issue, task PR or executor run read from GitHub.
pub struct IndexRow {
    /// Issue repository or upstream repository identity.
    pub repository_id: i64,
    /// `issue`, `pull` or `run`.
    pub kind: &'static str,
    /// GitHub number, or run and attempt joined by a slash.
    pub item_id: String,
    /// GitHub's update time for stale-page comparison.
    pub source_at: DateTime<Utc>,
    /// Complete list projection without credentials.
    pub body: Value,
}

/// One durable index row returned to an internal read client.
#[derive(Serialize)]
pub struct IndexEntry {
    /// Stable GitHub item key.
    pub item_id: String,
    /// Latest GitHub update time.
    pub source_at: DateTime<Utc>,
    /// Source list document.
    pub document: Value,
}

/// Find issue numbers with a retained task or sponsorship history.
pub async fn held_issue_numbers(
    database: &Database,
    repository_id: i64,
) -> Result<Vec<i64>, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT issue_number FROM tasks.task_state WHERE repository_id=$1 \
         UNION SELECT issue_number FROM tasks.sponsorship_facts WHERE repository_id=$1 \
         UNION SELECT issue_number FROM tasks.sponsorship_removals WHERE repository_id=$1",
    )
    .bind(repository_id)
    .fetch_all(database.pool())
    .await
}

/// Find PRs already claimed by a Rust or imported Python task.
pub async fn owned_pr_numbers(
    database: &Database,
    upstream: &str,
) -> Result<Vec<i64>, sqlx::Error> {
    sqlx::query_scalar("SELECT pr_number FROM tasks.pr_ownership WHERE upstream=lower($1)")
        .bind(upstream)
        .fetch_all(database.pool())
        .await
}

/// Check one PR ownership inside a webhook application transaction.
pub async fn pr_is_owned(
    work: &mut UnitOfWork<'_>,
    upstream: &str,
    number: i64,
) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM tasks.pr_ownership WHERE upstream=lower($1) AND pr_number=$2)",
    )
    .bind(upstream)
    .bind(number)
    .fetch_one(work.connection())
    .await
}

/// Page one repository list without exposing storage credentials to the caller.
pub async fn index_page(
    database: &Database,
    repository: &str,
    kind: &str,
    after: &str,
) -> Result<Vec<IndexEntry>, sqlx::Error> {
    let rows: Vec<(String, DateTime<Utc>, Json<Value>)> = sqlx::query_as(
        "SELECT item_id,source_at,body FROM tasks.target_index \
         WHERE body->>'repository'=$1 AND kind=$2 AND item_id>$3 \
         ORDER BY item_id LIMIT 101",
    )
    .bind(repository)
    .bind(kind)
    .bind(after)
    .fetch_all(database.pool())
    .await?;
    Ok(rows
        .into_iter()
        .map(|(item_id, source_at, Json(document))| IndexEntry {
            item_id,
            source_at,
            document,
        })
        .collect())
}

/// Read one current source document from the durable target index.
pub async fn index_item(
    database: &Database,
    repository: &str,
    kind: &str,
    item_id: &str,
) -> Result<Option<IndexEntry>, sqlx::Error> {
    let row: Option<(String, DateTime<Utc>, Json<Value>)> = sqlx::query_as(
        "SELECT item_id,source_at,body FROM tasks.target_index \
         WHERE body->>'repository'=$1 AND kind=$2 AND item_id=$3",
    )
    .bind(repository)
    .bind(kind)
    .bind(item_id)
    .fetch_optional(database.pool())
    .await?;
    Ok(row.map(|(item_id, source_at, Json(document))| IndexEntry {
        item_id,
        source_at,
        document,
    }))
}

/// Capture the database clock before paginating one repository's GitHub lists.
pub async fn index_scan_started(database: &Database) -> Result<DateTime<Utc>, sqlx::Error> {
    sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(database.pool())
        .await
}

/// Merge one delivery without allowing an older GitHub snapshot to replace it.
pub async fn upsert_index_event(work: &mut UnitOfWork<'_>, row: &IndexRow) -> Result<()> {
    ensure!(
        matches!(row.kind, "issue" | "pull" | "run"),
        "invalid index kind"
    );
    sqlx::query(
        "INSERT INTO tasks.target_index \
         (repository_id,kind,item_id,source_at,scan_id,body) \
         VALUES ($1,$2,$3,$4,NULL,$5) \
         ON CONFLICT (repository_id,kind,item_id) DO UPDATE SET \
         source_at=EXCLUDED.source_at,observed_at=now(),scan_id=NULL,body=EXCLUDED.body \
         WHERE tasks.target_index.source_at<=EXCLUDED.source_at",
    )
    .bind(row.repository_id)
    .bind(row.kind)
    .bind(&row.item_id)
    .bind(row.source_at)
    .bind(Json(&row.body))
    .execute(work.connection())
    .await?;
    Ok(())
}

/// Publish one complete list scan while preserving events received after it began.
pub async fn replace_target_index(
    database: &Database,
    repository_id: i64,
    kind: &'static str,
    started_at: DateTime<Utc>,
    rows: &[IndexRow],
) -> Result<()> {
    ensure!(
        matches!(kind, "issue" | "pull" | "run"),
        "invalid index kind"
    );
    let scan_id = Uuid::now_v7();
    let mut work = database.begin().await?;
    for row in rows {
        ensure!(
            row.repository_id == repository_id && row.kind == kind,
            "index scan identity changed"
        );
        sqlx::query(
            "INSERT INTO tasks.target_index \
             (repository_id,kind,item_id,source_at,observed_at,scan_id,body) \
             VALUES ($1,$2,$3,$4,now(),$5,$6) \
             ON CONFLICT (repository_id,kind,item_id) DO UPDATE SET \
             source_at=EXCLUDED.source_at,observed_at=now(),scan_id=EXCLUDED.scan_id, \
             body=EXCLUDED.body WHERE tasks.target_index.source_at<EXCLUDED.source_at \
             OR (tasks.target_index.source_at=EXCLUDED.source_at \
                 AND tasks.target_index.observed_at<=$7)",
        )
        .bind(row.repository_id)
        .bind(row.kind)
        .bind(&row.item_id)
        .bind(row.source_at)
        .bind(scan_id)
        .bind(Json(&row.body))
        .bind(started_at)
        .execute(work.connection())
        .await?;
    }
    sqlx::query(
        "DELETE FROM tasks.target_index WHERE repository_id=$1 AND kind=$4 \
         AND scan_id IS DISTINCT FROM $2 AND observed_at<=$3",
    )
    .bind(repository_id)
    .bind(scan_id)
    .bind(started_at)
    .bind(kind)
    .execute(work.connection())
    .await?;
    work.commit().await?;
    Ok(())
}

/// Include Python-owned PRs that GitHub's recent list no longer returns.
pub async fn historical_pulls(
    database: &Database,
    upstream: &str,
) -> Result<Vec<(i64, Value)>, sqlx::Error> {
    let rows: Vec<(i64, Json<Value>)> = sqlx::query_as(
        "SELECT pr_number,document FROM tasks.cutover_pr_index WHERE upstream=lower($1) ORDER BY pr_number",
    )
    .bind(upstream)
    .fetch_all(database.pool())
    .await?;
    Ok(rows
        .into_iter()
        .map(|(number, Json(body))| (number, body))
        .collect())
}

/// Match fork runs to exact live and imported serving identities without borrowing evidence across attempts.
pub async fn index_run_links(database: &Database, fork: &str) -> Result<Vec<Value>, sqlx::Error> {
    let rows: Vec<(Json<Value>,)> = sqlx::query_as(
        "WITH numbered AS (SELECT a.id,a.task_id,a.standalone_task_id,a.operation,row_number() OVER (PARTITION BY COALESCE(a.task_id,a.standalone_task_id) ORDER BY a.admitted_at,a.id) ordinal FROM tasks.attempts a) \
         SELECT jsonb_build_object('run_id',r.run_id,'attempt',1,'turn',n.ordinal,'turn_id',n.id,'operation',n.operation, \
         'repository',CASE WHEN s.id IS NOT NULL THEN s.upstream ELSE c.target->>'issue_repository' END,'issue',COALESCE(t.issue_number,s.pr_number),'kind',CASE WHEN s.id IS NULL THEN 'contribution' ELSE 'review' END,'association','dispatched') \
         FROM tasks.runner_sessions r JOIN numbered n ON n.id=r.turn_id LEFT JOIN tasks.task_state t ON t.task_id=n.task_id LEFT JOIN tasks.standalone_review_tasks s ON s.id=n.standalone_task_id \
         CROSS JOIN tasks.config_targets config CROSS JOIN LATERAL jsonb_array_elements(config.body) c(target) \
         WHERE r.fork=$1 AND r.run_id IS NOT NULL AND (c.target->>'repository_id')::bigint=COALESCE(t.repository_id,s.runner_repository_id) ORDER BY n.ordinal")
        .bind(fork).fetch_all(database.pool()).await?;
    let mut links = rows
        .into_iter()
        .map(|(Json(value),)| value)
        .collect::<Vec<_>>();
    let imported: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('deployment',i.deployment,'source_id',i.source_id,'repository',i.repository,'issue',i.issue_number,'serving_run',i.serving_run,'source_record',i.source_record,'kind',h.kind, \
         'turn',row_number() OVER (PARTITION BY i.repository,i.issue_number ORDER BY i.deployment,i.source_id)) \
         FROM tasks.python_import_turns i CROSS JOIN tasks.config_targets config CROSS JOIN LATERAL jsonb_array_elements(config.body) c(target) \
         LEFT JOIN LATERAL (SELECT kind FROM tasks.historical_display h WHERE h.repository=lower(i.repository) AND h.number=i.issue_number ORDER BY observed_at DESC,deployment DESC LIMIT 1) h ON true \
         WHERE c.target->>'fork'=$1 AND i.repository IN (c.target->>'issue_repository',c.target->>'upstream') AND i.serving_run IS NOT NULL")
        .bind(fork).fetch_all(database.pool()).await?;
    for (Json(mut row),) in imported {
        let parts = row["serving_run"]
            .as_str()
            .unwrap_or("")
            .split('/')
            .collect::<Vec<_>>();
        let Some(run) = parts
            .first()
            .and_then(|run| run.parse::<i64>().ok())
            .filter(|run| *run > 0)
        else {
            continue;
        };
        let attempt = parts
            .get(1)
            .and_then(|attempt| attempt.parse::<i64>().ok())
            .unwrap_or(1);
        let request: Value = row["source_record"]["request"]
            .as_str()
            .and_then(|value| serde_json::from_str(value).ok())
            .unwrap_or(Value::Null);
        let kind = row["kind"].as_str().map(str::to_owned).unwrap_or_else(|| {
            if request["task_kind"] == "standalone_pr_review" {
                "review"
            } else {
                "contribution"
            }
            .to_owned()
        });
        row["kind"] = serde_json::json!(kind);
        row["run_id"] = serde_json::json!(run);
        row["attempt"] = serde_json::json!(attempt);
        row["association"] = serde_json::json!("assigned");
        row["historical"] = serde_json::json!(true);
        row.as_object_mut()
            .expect("import link object")
            .remove("source_record");
        links.push(row);
    }
    Ok(links)
}

/// Record a target scan outcome without replacing its last successful verification time on failure.
pub async fn record_index_scan(
    database: &Database,
    lists: &[(&str, &str)],
    succeeded: bool,
) -> Result<(), sqlx::Error> {
    let mut work = database.begin().await?;
    for (repository, kind) in lists {
        sqlx::query("INSERT INTO tasks.target_index_status(repository,kind,verified_at,failed) VALUES ($1,$2,CASE WHEN $3 THEN now() END,NOT $3) ON CONFLICT (repository,kind) DO UPDATE SET attempted_at=now(),failed=NOT $3,verified_at=CASE WHEN $3 THEN now() ELSE tasks.target_index_status.verified_at END")
            .bind(repository).bind(kind).bind(succeeded).execute(work.connection()).await?;
    }
    work.commit().await
}

/// Return safe freshness metadata for the saved list, including its first-build state.
pub async fn index_scan_status(
    database: &Database,
    repository: &str,
    kind: &str,
) -> Result<Value, sqlx::Error> {
    let value: Option<Json<Value>> = sqlx::query_scalar("SELECT jsonb_build_object('observedAt',verified_at,'verified',verified_at,'stale',failed,'refreshing',false,'state',CASE WHEN failed THEN 'failed' ELSE 'current' END,'error',CASE WHEN failed THEN 'GitHub list refresh failed. Saved rows may be out of date.' END) FROM tasks.target_index_status WHERE repository=$1 AND kind=$2")
        .bind(repository).bind(kind).fetch_optional(database.pool()).await?;
    Ok(value.map(|Json(value)| value).unwrap_or_else(|| serde_json::json!({"observedAt":"","verified":null,"stale":true,"refreshing":false,"state":"rebuilding","error":null})))
}
