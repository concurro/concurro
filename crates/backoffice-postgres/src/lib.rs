//! Postgres storage for the lower-privilege backoffice projection.

use anyhow::Result;
use concurro_db::{Database, UnitOfWork};
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::types::Json;
use uuid::Uuid;

/// One configured repository identity for read entitlement and task display.
#[derive(Clone, Deserialize)]
pub struct Target {
    /// Numeric GitHub issue or PR repository ID.
    pub repository_id: i64,
    /// Numeric upstream PR repository ID when it differs from the issue source.
    pub upstream_repository_id: Option<i64>,
    /// Upstream repository used for writer authority.
    pub upstream: String,
    /// Fork repository whose visibility writer tier also requires.
    pub fork: String,
    /// Repository containing contribution issues.
    pub issue_repository: String,
}

/// One event from the task application's durable stream.
#[derive(Deserialize)]
pub struct TaskEvent {
    /// Stable event identity across redeliveries.
    pub id: Uuid,
    /// Concrete event type.
    #[serde(rename = "type")]
    pub event_type: String,
    /// Snapshot entity when present.
    pub entity: Option<String>,
    /// Source revision for a snapshot.
    pub revision: Option<i64>,
    /// Typed payload retained without a command credential.
    pub data: Value,
}

/// Run the isolated backoffice schema migrations.
pub async fn migrate(database: &Database) -> Result<()> {
    database
        .migrate_in_schema("backoffice", &sqlx::migrate!("./migrations"))
        .await
}

/// Read target identities from the task application's latest baseline snapshot.
pub async fn configured_targets(database: &Database) -> Result<Vec<Target>> {
    let targets: Json<Vec<Target>> = sqlx::query_scalar(
        "SELECT body FROM backoffice.model_items \
         WHERE entity='config_targets' AND entity_id='current'",
    )
    .fetch_one(database.pool())
    .await?;
    Ok(targets.0)
}

/// Apply one decoded source event inside the shared consumer's acknowledgement transaction.
pub async fn ingest_event_in_work(work: &mut UnitOfWork<'_>, event: &TaskEvent) -> Result<()> {
    let ready: bool =
        sqlx::query_scalar("SELECT ready FROM backoffice.model_state WHERE singleton FOR UPDATE")
            .fetch_one(work.connection())
            .await?;
    let inserted: Option<Uuid> = sqlx::query_scalar(
        "INSERT INTO backoffice.model_events \
         (id,event_type,entity,revision,data) VALUES ($1,$2,$3,$4,$5) \
         ON CONFLICT (id) DO NOTHING RETURNING id",
    )
    .bind(event.id)
    .bind(&event.event_type)
    .bind(&event.entity)
    .bind(event.revision)
    .bind(Json(&event.data))
    .fetch_optional(work.connection())
    .await?;
    if inserted.is_some() && ready {
        apply_event(work, event).await?;
        sqlx::query("UPDATE backoffice.model_events SET applied=true WHERE id=$1")
            .bind(event.id)
            .execute(work.connection())
            .await?;
    }
    Ok(())
}

/// Reserve a new baseline staging set and capture the event overlap floor.
pub async fn begin_baseline(database: &Database) -> Result<i64> {
    let mut work = database.begin().await?;
    let floor =
        sqlx::query_scalar("SELECT COALESCE(max(received_order),0) FROM backoffice.model_events")
            .fetch_one(work.connection())
            .await?;
    sqlx::query("DELETE FROM backoffice.baseline_stage")
        .execute(work.connection())
        .await?;
    work.commit().await?;
    Ok(floor)
}

/// Retain one ordered source page until the full baseline can be swapped.
pub async fn stage_baseline_row(
    work: &mut UnitOfWork<'_>,
    snapshot: Uuid,
    ordinal: i64,
    entity: &str,
    entity_id: &str,
    revision: i64,
    body: Value,
) -> Result<()> {
    sqlx::query(
        "INSERT INTO backoffice.baseline_stage \
         (snapshot_id,ordinal,entity,entity_id,revision,body) \
         VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (snapshot_id,ordinal) \
         DO UPDATE SET entity=EXCLUDED.entity,entity_id=EXCLUDED.entity_id, \
         revision=EXCLUDED.revision,body=EXCLUDED.body",
    )
    .bind(snapshot)
    .bind(ordinal)
    .bind(entity)
    .bind(entity_id)
    .bind(revision)
    .bind(Json(body))
    .execute(work.connection())
    .await?;
    Ok(())
}

/// Atomically replace the baseline and replay overlapping durable events.
pub async fn finish_baseline(
    database: &Database,
    snapshot: Uuid,
    overlap_floor: i64,
) -> Result<()> {
    let mut work = database.begin().await?;
    sqlx::query("SELECT ready FROM backoffice.model_state WHERE singleton FOR UPDATE")
        .fetch_one(work.connection())
        .await?;
    sqlx::query("DELETE FROM backoffice.model_items")
        .execute(work.connection())
        .await?;
    sqlx::query(
        "INSERT INTO backoffice.model_items (entity,entity_id,revision,body) \
         SELECT entity,entity_id,revision,body FROM backoffice.baseline_stage \
         WHERE snapshot_id=$1 ORDER BY ordinal",
    )
    .bind(snapshot)
    .execute(work.connection())
    .await?;
    let buffered: Vec<(Uuid, String, Option<String>, Option<i64>, Json<Value>)> = sqlx::query_as(
        "SELECT id,event_type,entity,revision,data FROM backoffice.model_events \
         WHERE NOT applied OR received_order>$1 ORDER BY received_order",
    )
    .bind(overlap_floor)
    .fetch_all(work.connection())
    .await?;
    for (id, event_type, entity, revision, Json(data)) in buffered {
        apply_event(
            &mut work,
            &TaskEvent {
                id,
                event_type,
                entity,
                revision,
                data,
            },
        )
        .await?;
        sqlx::query("UPDATE backoffice.model_events SET applied=true WHERE id=$1")
            .bind(id)
            .execute(work.connection())
            .await?;
    }
    sqlx::query(
        "UPDATE backoffice.model_state SET ready=true,baseline_id=$1,revision=revision+1 \
         WHERE singleton",
    )
    .bind(snapshot)
    .execute(work.connection())
    .await?;
    sqlx::query("DELETE FROM backoffice.baseline_stage WHERE snapshot_id=$1")
        .bind(snapshot)
        .execute(work.connection())
        .await?;
    work.commit().await?;
    Ok(())
}

/// Merge a later task snapshot or request outcome by its source identity.
async fn apply_event(work: &mut UnitOfWork<'_>, event: &TaskEvent) -> Result<()> {
    let source = &event.data;
    if event.event_type == "view.record"
        && let (Some(entity), Some(id)) = (source["entity"].as_str(), source["entity_id"].as_str())
    {
        sqlx::query(
            "INSERT INTO backoffice.model_items (entity,entity_id,revision,body) \
             VALUES ($1,$2,$3,$4) ON CONFLICT (entity,entity_id) DO UPDATE SET \
             revision=EXCLUDED.revision,body=EXCLUDED.body \
             WHERE backoffice.model_items.revision<=EXCLUDED.revision",
        )
        .bind(entity)
        .bind(id)
        .bind(event.revision.unwrap_or(0))
        .bind(Json(source["body"].clone()))
        .execute(work.connection())
        .await?;
        sqlx::query("UPDATE backoffice.model_state SET revision=revision+1 WHERE singleton")
            .execute(work.connection())
            .await?;
        return Ok(());
    }
    if event.event_type.starts_with("turn.")
        && let Some(id) = source["turn_id"].as_str()
    {
        let status = event.event_type.ends_with(".status");
        let field = if status {
            "status_snapshot"
        } else if event.event_type.ends_with(".submitted") {
            "submitted"
        } else {
            "finished"
        };
        let mut body = json!({"turn_id":id});
        if source["task_id"].is_string() {
            body["task_id"] = source["task_id"].clone();
        }
        body[field] = source.clone();
        if status {
            body["status"] = source["status"].clone();
            sqlx::query(
                "INSERT INTO backoffice.model_items (entity,entity_id,revision,body) \
                 VALUES ('turn',$1,$2,$3) ON CONFLICT (entity,entity_id) DO UPDATE SET \
                 revision=EXCLUDED.revision,body=backoffice.model_items.body||EXCLUDED.body \
                 WHERE backoffice.model_items.revision<=EXCLUDED.revision",
            )
            .bind(id)
            .bind(event.revision.unwrap_or(0))
            .bind(Json(body))
            .execute(work.connection())
            .await?;
        } else {
            sqlx::query(
                "INSERT INTO backoffice.model_items (entity,entity_id,revision,body) \
                 VALUES ('turn',$1,0,$2) ON CONFLICT (entity,entity_id) DO UPDATE SET \
                 body=backoffice.model_items.body||EXCLUDED.body",
            )
            .bind(id)
            .bind(Json(body))
            .execute(work.connection())
            .await?;
        }
        sqlx::query("UPDATE backoffice.model_state SET revision=revision+1 WHERE singleton")
            .execute(work.connection())
            .await?;
        return Ok(());
    }
    let (entity, id, revision, body) = if event.event_type.ends_with(".report")
        && let Some(id) = source["report_id"].as_str()
    {
        (
            "report",
            id.to_owned(),
            event.revision.unwrap_or(1),
            source.clone(),
        )
    } else if event.event_type.ends_with(".notice")
        && let Some(id) = source["notice_id"].as_str()
    {
        (
            "notice",
            id.to_owned(),
            event.revision.unwrap_or(1),
            source.clone(),
        )
    } else if event.event_type.ends_with(".state")
        && event.event_type.starts_with("task.")
        && let Some(id) = source["task_id"].as_str()
    {
        let issue = &source["issue"];
        (
            "contribution_task",
            id.to_owned(),
            event.revision.unwrap_or(0),
            json!({"repository_id":issue["repository_id"],"issue_number":issue["number"],
                "snapshot":source}),
        )
    } else if event.event_type == "config.targets" && source["targets"].is_array() {
        (
            "config_targets",
            "current".to_owned(),
            event.revision.unwrap_or(0),
            source["targets"].clone(),
        )
    } else if event.event_type.starts_with("request.")
        && let Some(id) = source["request_id"].as_str()
    {
        let issue = &source["issue"];
        let step_revision = match event.event_type.as_str() {
            "request.tasks.blocked" => 2,
            "request.tasks.stopped" => 3,
            _ => 1,
        };
        (
            "request",
            id.to_owned(),
            step_revision,
            json!({"repository_id":issue["repository_id"],"issue_number":issue["number"],
                "task_id":source["task_id"],"status":source["status"],
                "reason":source["reason"],"outcome":source}),
        )
    } else {
        sqlx::query("UPDATE backoffice.model_state SET revision=revision+1 WHERE singleton")
            .execute(work.connection())
            .await?;
        return Ok(());
    };
    sqlx::query(
        "INSERT INTO backoffice.model_items (entity,entity_id,revision,body) \
         VALUES ($1,$2,$3,$4) ON CONFLICT (entity,entity_id) DO UPDATE SET \
         revision=EXCLUDED.revision,body=CASE WHEN EXCLUDED.entity='config_targets' \
         THEN EXCLUDED.body ELSE backoffice.model_items.body||EXCLUDED.body END \
         WHERE backoffice.model_items.revision<=EXCLUDED.revision",
    )
    .bind(entity)
    .bind(id)
    .bind(revision)
    .bind(Json(body))
    .execute(work.connection())
    .await?;
    sqlx::query("UPDATE backoffice.model_state SET revision=revision+1 WHERE singleton")
        .execute(work.connection())
        .await?;
    Ok(())
}

mod queries;
pub use queries::*;
