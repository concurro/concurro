//! Stable, paged source snapshots for the read-only backoffice model.

use concurro_db::Database;
use concurro_tasks_core::TaskState;
use serde::Serialize;
use serde_json::{Value, json};
use sqlx::types::Json;
use uuid::Uuid;

/// One durable source fact at a snapshot's stable position.
#[derive(Serialize)]
pub struct BaselineRow {
    /// One-based position in the frozen snapshot.
    pub ordinal: i64,
    /// Source fact family.
    pub entity: String,
    /// Stable identity within that family.
    pub entity_id: String,
    /// Source revision or status revision.
    pub revision: i64,
    /// Complete read-model source record.
    pub body: Value,
}

/// Materialize all durable view facts from one PostgreSQL statement snapshot.
pub async fn create_baseline(database: &Database) -> anyhow::Result<Uuid> {
    let snapshot = Uuid::now_v7();
    let mut work = database.begin().await?;
    sqlx::query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        .execute(work.connection())
        .await?;
    sqlx::query(
        "DELETE FROM tasks.baseline_snapshots WHERE id NOT IN \
         (SELECT id FROM tasks.baseline_snapshots ORDER BY created_at DESC LIMIT 3)",
    )
    .execute(work.connection())
    .await?;
    sqlx::query("INSERT INTO tasks.baseline_snapshots (id) VALUES ($1)")
        .bind(snapshot)
        .execute(work.connection())
        .await?;
    sqlx::query(
        "INSERT INTO tasks.baseline_rows (snapshot_id,ordinal,entity,entity_id,revision,body) \
         SELECT $1,row_number() OVER (ORDER BY entity,entity_id,revision), \
             entity,entity_id,revision,body FROM ( \
         SELECT 'standalone_review'::text entity,t.id::text entity_id,t.view_revision revision, \
             jsonb_build_object('task_id',t.id,'repository_id',t.repository_id, \
                 'upstream',t.upstream,'pr_number',t.pr_number,'stage',t.stage, \
                 'round',t.round,'snapshot',r.snapshot,'round_status',r.status, \
                 'used_model_calls',t.used_model_calls,'enrolled_at',t.enrolled_at) body \
             FROM tasks.standalone_review_tasks t \
             LEFT JOIN tasks.standalone_review_rounds r \
                 ON r.task_id=t.id AND r.number=t.round \
         UNION ALL \
         SELECT 'request',q.id::text,CASE WHEN q.status='pending' THEN 0 ELSE 1 END::bigint, \
             jsonb_build_object('request_id',q.id,'repository_id',q.repository_id, \
                 'issue_number',q.issue_number,'task_id',q.task_id,'channel',q.channel, \
                 'source',q.source,'source_at',q.source_at,'request',q.request, \
                 'status',q.status,'reason',q.reason,'applied_at',q.applied_at) \
             FROM tasks.requests q \
         UNION ALL \
         SELECT 'turn',a.id::text,a.status_revision, \
             jsonb_build_object('turn_id',a.id,'task_id',a.task_id, \
                 'standalone_task_id',a.standalone_task_id,'operation',a.operation, \
                 'status',a.status,'outcome_reason',a.outcome_reason, \
                 'admitted_at',a.admitted_at,'submit_body_base64',encode(a.submit_body,'base64'), \
                 'prompt_body_base64',encode(a.prompt_body,'base64'), \
                 'result',a.result,'result_raw_base64',encode(a.result_raw,'base64'), \
                 'runner_sessions',(SELECT COALESCE(jsonb_agg(jsonb_build_object( \
                     'session_id',s.session_id,'run_id',s.run_id,'status',s.dispatch_status) \
                     ORDER BY s.session_id),'[]'::jsonb) \
                     FROM tasks.runner_sessions s WHERE s.turn_id=a.id), \
                 'archive',to_jsonb(archive)-'journal', \
                 'standalone',to_jsonb(sa)-'report_raw') \
             FROM tasks.attempts a \
             LEFT JOIN tasks.turn_archives archive ON archive.turn_id=a.id \
             LEFT JOIN tasks.standalone_review_attempts sa ON sa.attempt_id=a.id \
         UNION ALL \
         SELECT 'imported_turn',i.deployment||':'||i.source_id,1::bigint, \
             jsonb_build_object('deployment',i.deployment,'source_id',i.source_id, \
                 'repository',i.repository,'issue_number',i.issue_number, \
                 'generation',i.generation,'stage',i.stage,'status',i.status, \
                 'session_scope',i.session_scope,'requested_turn',i.requested_turn, \
                 'completed_turn',i.completed_turn,'serving_session',i.serving_session, \
                 'serving_run',i.serving_run,'staffing',i.staffing, \
                 'source_record',i.source_record,'gzip_sha256',i.gzip_sha256, \
                 'tar_sha256',i.tar_sha256) \
             FROM tasks.python_import_turns i \
             WHERE i.import_status IN ('verified','no_archive') \
         UNION ALL \
         SELECT 'report',p.report_id::text,1::bigint, \
             jsonb_build_object('report_id',p.report_id,'task_id',p.task_id, \
                 'standalone_task_id',p.standalone_task_id,'repository_id',p.repository_id, \
                 'issue_number',p.issue_number,'name',p.name,'body',p.body, \
                 'status',p.status,'recorded_at',p.recorded_at) \
             FROM tasks.report_artifact_writes p \
         UNION ALL \
         SELECT 'consent',f.request_id::text||':'||f.provider, \
             CASE WHEN f.effective THEN 1 ELSE 2 END::bigint, \
             jsonb_build_object('request_id',f.request_id,'repository_id',f.repository_id, \
                 'issue_number',f.issue_number,'provider',f.provider,'fact',f.fact, \
                 'effective',f.effective,'source_at',f.source_at,'source',f.source) \
             FROM tasks.sponsorship_facts f \
         UNION ALL \
         SELECT 'consent_removal',m.comment_id::text||':'||m.source||':'|| \
             m.prior_request_id::text||':'||m.provider,1::bigint, \
             jsonb_build_object('repository_id',m.repository_id,'issue_number',m.issue_number, \
                 'provider',m.provider,'fact',m.fact,'source_at',m.source_at,'source',m.source) \
             FROM tasks.sponsorship_removals m \
         UNION ALL \
         SELECT 'standalone_funding',f.command_id::text||':'||f.provider, \
             CASE WHEN f.effective THEN 1 ELSE 2 END::bigint, \
             jsonb_build_object('repository_id',f.repository_id,'pr_number',f.pr_number, \
                 'provider',f.provider,'fact',f.fact,'effective',f.effective) \
             FROM tasks.standalone_review_funding_facts f \
         UNION ALL \
         SELECT 'standalone_funding_removal',r.command_id::text||':'|| \
             r.prior_command_id::text||':'||r.provider,1::bigint, \
             jsonb_build_object('repository_id',c.repository_id,'pr_number',c.pr_number, \
                 'provider',r.provider,'fact',r.fact) \
             FROM tasks.standalone_review_funding_removals r \
             JOIN tasks.standalone_review_commands c ON c.id=r.command_id \
         UNION ALL \
         SELECT 'standalone_finding',f.task_id::text||':'||f.id,f.sequence, \
             jsonb_build_object('task_id',f.task_id,'finding_id',f.id, \
                 'reviewer_id',f.reviewer_id,'status',f.status,'head',f.head, \
                 'finding',f.finding,'disposition',f.disposition, \
                 'thread_node_id',f.thread_node_id) \
             FROM tasks.standalone_review_findings f \
         UNION ALL \
         SELECT 'target_index',i.repository_id::text||':'||i.kind||':'||i.item_id, \
             (extract(epoch FROM i.observed_at)*1000000)::bigint, \
             jsonb_build_object('repository_id',i.repository_id,'kind',i.kind, \
                 'item_id',i.item_id,'source_at',i.source_at,'document',i.body) \
             FROM tasks.target_index i \
         UNION ALL \
         SELECT 'config_targets','current',revision,body FROM tasks.config_targets \
         ) facts",
    )
    .bind(snapshot)
    .execute(work.connection())
    .await?;
    let mut ordinal: i64 = sqlx::query_scalar(
        "SELECT COALESCE(max(ordinal),0) FROM tasks.baseline_rows WHERE snapshot_id=$1",
    )
    .bind(snapshot)
    .fetch_one(work.connection())
    .await?;
    let tasks: Vec<(i64, Json<TaskState>)> =
        sqlx::query_as("SELECT revision,state FROM tasks.task_state ORDER BY task_id")
            .fetch_all(work.connection())
            .await?;
    for (revision, Json(state)) in tasks {
        ordinal += 1;
        let body = json!({"task_id":state.id.0,
            "repository_id":state.issue.repository_id,
            "issue_number":state.issue.number,
            "snapshot":super::build_task_snapshot(&mut work, &state).await?});
        sqlx::query(
            "INSERT INTO tasks.baseline_rows \
             (snapshot_id,ordinal,entity,entity_id,revision,body) \
             VALUES ($1,$2,'contribution_task',$3,$4,$5)",
        )
        .bind(snapshot)
        .bind(ordinal)
        .bind(state.id.0.to_string())
        .bind(revision)
        .bind(Json(body))
        .execute(work.connection())
        .await?;
    }
    let consent_tasks: Vec<Uuid> = sqlx::query_scalar(
        "SELECT DISTINCT COALESCE(task_id,standalone_task_id) \
         FROM tasks.sponsorship_transitions ORDER BY 1",
    )
    .fetch_all(work.connection())
    .await?;
    for task in consent_tasks {
        let Some((revision, body)) =
            super::read_model::build_consent_state(&mut work, task).await?
        else {
            continue;
        };
        ordinal += 1;
        sqlx::query(
            "INSERT INTO tasks.baseline_rows \
             (snapshot_id,ordinal,entity,entity_id,revision,body) \
             VALUES ($1,$2,'consent_state',$3,$4,$5)",
        )
        .bind(snapshot)
        .bind(ordinal)
        .bind(task.to_string())
        .bind(i64::try_from(revision)?)
        .bind(Json(body))
        .execute(work.connection())
        .await?;
    }
    work.commit().await?;
    Ok(snapshot)
}

/// Return one page from a materialized source snapshot, including one lookahead row.
pub async fn baseline_page(
    database: &Database,
    snapshot: Uuid,
    after: i64,
    limit: i64,
) -> Result<Option<Vec<BaselineRow>>, sqlx::Error> {
    let exists: bool =
        sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM tasks.baseline_snapshots WHERE id=$1)")
            .bind(snapshot)
            .fetch_one(database.pool())
            .await?;
    if !exists {
        return Ok(None);
    }
    let rows: Vec<(i64, String, String, i64, Json<Value>)> = sqlx::query_as(
        "SELECT ordinal,entity,entity_id,revision,body FROM tasks.baseline_rows \
         WHERE snapshot_id=$1 AND ordinal>$2 ORDER BY ordinal LIMIT $3",
    )
    .bind(snapshot)
    .bind(after)
    .bind(limit)
    .fetch_all(database.pool())
    .await?;
    Ok(Some(
        rows.into_iter()
            .map(
                |(ordinal, entity, entity_id, revision, Json(body))| BaselineRow {
                    ordinal,
                    entity,
                    entity_id,
                    revision,
                    body,
                },
            )
            .collect(),
    ))
}

/// Release a fully delivered frozen baseline and its materialized rows.
pub async fn finish_baseline(database: &Database, snapshot: Uuid) -> Result<(), sqlx::Error> {
    sqlx::query("DELETE FROM tasks.baseline_snapshots WHERE id=$1")
        .bind(snapshot)
        .execute(database.pool())
        .await?;
    Ok(())
}
