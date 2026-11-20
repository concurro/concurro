//! Read-only task, attempt and pending-effect views for the host operator.

use anyhow::ensure;
use concurro_db::{Database, UnitOfWork};
use concurro_events_postgres::Outbox;
use concurro_tasks_core::IssueKey;
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

use crate::{cancel_attempts, intake, lock_issue_for_turn};

/// List current tasks in a stable repository and issue order.
pub async fn list_tasks(database: &Database) -> Result<Vec<Value>, sqlx::Error> {
    let rows: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object( \
         'repository_id',repository_id,'issue_number',issue_number,'task_id',task_id, \
         'revision',revision,'stage',state->>'stage','stage_reason',state->>'stage_reason', \
         'used_model_calls',state->'used_model_calls') \
         FROM tasks.task_state ORDER BY repository_id,issue_number",
    )
    .fetch_all(database.pool())
    .await?;
    Ok(rows.into_iter().map(|row| row.0.0).collect())
}

/// Read one task with its ordered requests, turns, reports and publication writes.
pub async fn task_detail(
    database: &Database,
    issue: IssueKey,
) -> Result<Option<Value>, sqlx::Error> {
    let state: Option<(Json<Value>,)> = sqlx::query_as(
        "SELECT state FROM tasks.task_state WHERE repository_id=$1 AND issue_number=$2",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(database.pool())
    .await?;
    let dashboard: Option<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('revision',s.revision,'comment_id',c.comment_id, \
         'watch',s.watch,'last_error',s.last_error,'retry_at',s.retry_at, \
         'writes',COALESCE((SELECT jsonb_agg(jsonb_build_object( \
             'id',w.id,'revision',w.revision,'status',w.status, \
             'failure',w.failure,'updated_at',w.updated_at) ORDER BY w.id) \
             FROM tasks.dashboard_writes w WHERE w.task_id=s.task_id),'[]'::jsonb)) \
         FROM tasks.dashboard_surfaces s \
         JOIN tasks.publication_comments c ON c.id=s.create_id \
         WHERE s.repository_id=$1 AND s.issue_number=$2",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(database.pool())
    .await?;
    let Some((Json(state),)) = state else {
        return Ok(None);
    };
    let requests: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('id',id,'channel',channel,'source',source, \
         'source_at',source_at,'actor',request->'actor','kind',request->'kind', \
         'status',status,'reason',reason) \
         FROM tasks.requests WHERE repository_id=$1 AND issue_number=$2 \
         ORDER BY source_at,source_id,source COLLATE \"C\",source_index",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let attempts: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('id',a.id,'operation',a.operation, \
         'expected_revision',a.expected_revision,'status',a.status, \
         'outcome_reason',a.outcome_reason,'admitted_at',a.admitted_at, \
         'deadline',a.deadline,'poll_failures',a.poll_failures, \
         'status_revision',a.status_revision,'finished_event_emitted',a.finished_event_emitted, \
         'may_be_live',EXISTS(SELECT 1 FROM tasks.effects s \
             WHERE s.turn_id=a.id AND s.kind='submit' AND s.submit_attempts>0) \
             AND NOT EXISTS(SELECT 1 FROM tasks.effects r \
             WHERE r.turn_id=a.id AND r.kind='retire' AND r.status='settled'), \
         'observation',a.observation,'result',a.result, \
         'artifacts',COALESCE((SELECT jsonb_agg(jsonb_build_object( \
             'sha256',b.sha256,'bytes',octet_length(b.body),'media_type',b.media_type) \
             ORDER BY b.sha256) FROM tasks.turn_artifact_links l \
             JOIN tasks.turn_artifacts b ON b.sha256=l.sha256 \
             WHERE l.turn_id=a.id),'[]'::jsonb), \
         'sessions',COALESCE((SELECT jsonb_agg(jsonb_build_object( \
             'session_id',s.session_id,'retired_at',s.retired_at) \
             ORDER BY s.session_id) FROM tasks.attempt_sessions s WHERE s.turn_id=a.id),'[]'::jsonb), \
         'report',(SELECT jsonb_build_object('accepted',r.accepted, \
             'raw_report',r.raw_report,'validation_error',r.validation_error) \
             FROM tasks.intake_reports r WHERE r.attempt_id=a.id), \
         'effects',COALESCE((SELECT jsonb_agg(jsonb_build_object( \
             'id',e.id,'kind',e.kind,'status',e.status,'updated_at',e.updated_at) \
             ORDER BY e.id) FROM tasks.effects e WHERE e.turn_id=a.id),'[]'::jsonb)) \
         FROM tasks.attempts a JOIN tasks.task_state t ON t.task_id=a.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2 ORDER BY a.admitted_at,a.id",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let publication: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('id',id,'request_id',request_id,'kind',kind, \
         'status',status,'comment_id',comment_id,'failure',failure,'updated_at',updated_at) \
         FROM tasks.publication_comments WHERE repository_id=$1 AND issue_number=$2 \
         ORDER BY updated_at,id",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let archives: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('turn',a.turn_id,'gzip_sha256',a.gzip_sha256, \
         'tar_sha256',a.tar_sha256,'archive_error',a.archive_error, \
         'upload_status',u.status,'byte_count',u.byte_count,'last_error',u.last_error) \
         FROM tasks.turn_archives a JOIN tasks.attempts turn ON turn.id=a.turn_id \
         JOIN tasks.task_state t ON t.task_id=turn.task_id \
         LEFT JOIN tasks.archive_uploads u ON u.gzip_sha256=a.gzip_sha256 \
         WHERE t.repository_id=$1 AND t.issue_number=$2 ORDER BY a.recorded_at,a.turn_id",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    let stage_label: Option<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('revision',s.revision,'desired',s.desired, \
         'watch',s.watch,'last_error',s.last_error,'retry_at',s.retry_at, \
         'writes',COALESCE((SELECT jsonb_agg(jsonb_build_object( \
             'id',w.id,'revision',w.revision,'action',w.action,'label',w.label, \
             'status',w.status,'failure',w.failure,'updated_at',w.updated_at) ORDER BY w.id) \
             FROM tasks.stage_label_writes w WHERE w.task_id=s.task_id),'[]'::jsonb)) \
         FROM tasks.stage_label_surfaces s JOIN tasks.task_state t ON t.task_id=s.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(database.pool())
    .await?;
    let runners: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('session_id',r.session_id,'turn_id',r.turn_id, \
         'fork',r.fork,'config_sha',r.config_sha,'desired_present',r.desired_present, \
         'ref_revision',r.ref_revision,'ref_watch',r.ref_watch,'ref_error',r.ref_error, \
         'dispatch_status',r.dispatch_status,'dispatch_error',r.dispatch_error, \
         'run_id',r.run_id,'writes',COALESCE((SELECT jsonb_agg(jsonb_build_object( \
             'id',w.id,'revision',w.revision,'action',w.action,'status',w.status, \
             'failure',w.failure,'updated_at',w.updated_at) ORDER BY w.id) \
             FROM tasks.runner_ref_writes w WHERE w.session_id=r.session_id),'[]'::jsonb)) \
         FROM tasks.runner_sessions r JOIN tasks.attempts a ON a.id=r.turn_id \
         JOIN tasks.task_state t ON t.task_id=a.task_id \
         WHERE t.repository_id=$1 AND t.issue_number=$2 ORDER BY r.updated_at,r.session_id",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_all(database.pool())
    .await?;
    Ok(Some(serde_json::json!({
        "state": state,
        "requests": requests.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "attempts": attempts.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "publication": publication.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "archives": archives.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
        "stage_label": stage_label.map(|row| row.0.0),
        "dashboard": dashboard.map(|row| row.0.0),
        "runners": runners.into_iter().map(|row| row.0.0).collect::<Vec<_>>(),
    })))
}

/// List unsent and uncertain exchange and publication effects without credentials.
pub async fn pending_effects(database: &Database) -> Result<Vec<Value>, sqlx::Error> {
    let rows: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('id',e.id,'turn',e.turn_id,'kind',e.kind, \
         'status',e.status,'updated_at',e.updated_at, \
         'age_seconds',EXTRACT(EPOCH FROM now()-e.updated_at), \
         'attempt_status',a.status,'outcome_reason',a.outcome_reason, \
         'repository_id',t.repository_id,'issue_number',t.issue_number) \
         FROM tasks.effects e JOIN tasks.attempts a ON a.id=e.turn_id \
         JOIN tasks.task_state t ON t.task_id=a.task_id \
         WHERE e.status<>'settled' ORDER BY e.updated_at,e.id",
    )
    .fetch_all(database.pool())
    .await?;
    let mut effects = rows.into_iter().map(|row| row.0.0).collect::<Vec<_>>();
    let comments: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('id',id,'request_id',request_id,'kind',kind, \
         'status',status,'repository_id',repository_id,'issue_number',issue_number, \
         'updated_at',updated_at,'age_seconds',EXTRACT(EPOCH FROM now()-updated_at), \
         'resolution',CASE WHEN status='sending' THEN 'readback_or_operator' \
             ELSE 'send' END) \
         FROM tasks.publication_comments WHERE status IN ('recorded','sending') \
         ORDER BY updated_at,id",
    )
    .fetch_all(database.pool())
    .await?;
    effects.extend(comments.into_iter().map(|row| row.0.0));
    let archives: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('kind','archive','gzip_sha256',gzip_sha256, \
         'status',status,'byte_count',byte_count,'last_error',last_error, \
         'upload_attempts',upload_attempts,'updated_at',updated_at, \
         'age_seconds',EXTRACT(EPOCH FROM now()-updated_at)) \
         FROM tasks.archive_uploads WHERE status<>'verified' ORDER BY updated_at,gzip_sha256",
    )
    .fetch_all(database.pool())
    .await?;
    effects.extend(archives.into_iter().map(|row| row.0.0));
    let labels: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('id',w.id,'kind','stage_label', \
         'task_id',w.task_id,'revision',w.revision,'action',w.action,'label',w.label, \
         'status',w.status,'updated_at',w.updated_at, \
         'age_seconds',EXTRACT(EPOCH FROM now()-w.updated_at), \
         'desired',s.desired,'desired_revision',s.revision,'watch',s.watch) \
         FROM tasks.stage_label_writes w JOIN tasks.stage_label_surfaces s ON s.task_id=w.task_id \
         WHERE w.status IN ('sending','obsolete_uncertain') ORDER BY w.updated_at,w.id",
    )
    .fetch_all(database.pool())
    .await?;
    effects.extend(labels.into_iter().map(|row| row.0.0));
    let dashboards: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('kind','dashboard_edit','task_id',s.task_id, \
         'repository_id',s.repository_id,'issue_number',s.issue_number, \
         'revision',s.revision,'comment_id',c.comment_id,'watch',s.watch, \
         'last_error',s.last_error,'retry_at',s.retry_at,'updated_at',s.updated_at, \
         'age_seconds',EXTRACT(EPOCH FROM now()-s.updated_at)) \
         FROM tasks.dashboard_surfaces s \
         JOIN tasks.publication_comments c ON c.id=s.create_id \
         WHERE s.watch AND c.comment_id IS NOT NULL ORDER BY s.updated_at,s.task_id",
    )
    .fetch_all(database.pool())
    .await?;
    effects.extend(dashboards.into_iter().map(|row| row.0.0));
    let runners: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('kind','runner','session_id',session_id,'turn_id',turn_id, \
         'fork',fork,'config_sha',config_sha,'desired_present',desired_present, \
         'ref_revision',ref_revision,'ref_watch',ref_watch,'ref_error',ref_error, \
         'dispatch_status',dispatch_status,'dispatch_error',dispatch_error, \
         'run_id',run_id,'updated_at',updated_at, \
         'age_seconds',EXTRACT(EPOCH FROM now()-updated_at)) \
         FROM tasks.runner_sessions WHERE ref_watch OR ref_error IS NOT NULL \
         OR dispatch_status IN ('recorded','sending','obsolete_uncertain','sent','failed') \
         ORDER BY updated_at,session_id",
    )
    .fetch_all(database.pool())
    .await?;
    effects.extend(runners.into_iter().map(|row| row.0.0));
    Ok(effects)
}

/// Record a host operator's attempt cancellation and queue exchange retirement.
pub async fn cancel_attempt(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    turn: Uuid,
    actor: &str,
) -> anyhow::Result<bool> {
    ensure!(!actor.trim().is_empty(), "operator actor is empty");
    let changed = if let Some(state) = lock_issue_for_turn(uow, turn).await? {
        let changed = cancel_attempts(uow, outbox, state.id.0, Some(turn)).await? > 0;
        if changed {
            intake::settle_intake(uow, outbox, &state, turn).await?;
        }
        changed
    } else {
        false
    };
    sqlx::query(
        "INSERT INTO tasks.operator_actions (actor,action,target,outcome) \
         VALUES ($1,'attempts.cancel',$2,$3)",
    )
    .bind(actor)
    .bind(turn.to_string())
    .bind(if changed { "cancelled" } else { "noop" })
    .execute(uow.connection())
    .await?;
    Ok(changed)
}
