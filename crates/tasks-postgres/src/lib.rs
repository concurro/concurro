//! PostgreSQL state and serialized request decisions for the task application.

mod archive;
mod artifacts;
mod dashboard;
mod effects;
mod events;
pub use archive::{
    RetainedEligibility, fail_archive_upload, pending_archive, retained_eligibility,
    retry_archive_upload, verify_archive,
};
pub use dashboard::{
    DashboardSurface, begin_dashboard_edit, dashboard_surface, enqueue_dashboard,
    finish_dashboard_edit, observe_dashboard, record_dashboard_desire, watched_dashboards,
};
pub use effects::{
    acknowledge_retirement, claim_effect, due_attempt_deadlines, expire_attempt,
    pollable_generation, record_observation, record_poll_failure, record_submit_failure,
    recover_retirements, settle_submit, settle_submit_refusal,
};
mod intake;
mod operator;
mod publication;
mod runner;
mod stage_label;
pub use intake::{IntakeCorrection, latest_intake_correction};
pub use operator::{cancel_attempt, list_tasks, pending_effects, task_detail};
pub use publication::{
    CommentResolution, CommentWrite, claim_comment, comment_scope, defer_comment,
    due_comment_readbacks, fail_comment, queue_task_comment, resolve_comment, settle_comment,
};
pub use runner::{
    RunnerSession, begin_ref_write, claim_dispatch, enqueue_runner_session, fail_dispatch,
    finish_ref_write, observe_dispatch_run, observe_ref, record_runner_session,
    refuse_ref_conflict, retire_runner_sessions, runner_session, runner_submission,
    settle_dispatch_sent, watched_runner_sessions,
};
pub use stage_label::{
    StageLabelSurface, begin_stage_label_write, enqueue_stage_label, finish_stage_label_write,
    observe_stage_label, record_stage_label_desire, stage_label_surface, watched_stage_labels,
};

use chrono::Utc;
use concurro_contracts::{
    Envelope, EventId, RequestId, RequestOutcome, RequestOutcomeStatus, TaskBudget, TaskBudgets,
    TaskId, TaskSnapshot, Timestamp,
};
use concurro_db::{Database, UnitOfWork};
use concurro_events_postgres::Outbox;
use concurro_tasks_core::{
    Admission, AttemptSpec, Authority, CommentRevision, Decision, InitializationSnapshot, IssueKey,
    Request, RequestStatus, SourceOrder, Stage, TaskState, decide, initialize, welcome_body,
};
use graphile_worker::WorkerUtils;
use sqlx::{migrate::Migrator, types::Json};
use uuid::Uuid;

static MIGRATIONS: Migrator = sqlx::migrate!("./migrations");
const NEXT_PENDING: &str = "SELECT id, request FROM tasks.requests WHERE repository_id=$1 AND issue_number=$2 AND status='pending' \
     ORDER BY request_class,source_at,source_id,source COLLATE \"C\",source_index LIMIT 1";

/// Exchange action represented by one durable effect row.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EffectKind {
    /// Idempotent submit with the attempt's exact stored body.
    Submit,
    /// Idempotent retirement of every session associated with a turn.
    Retire,
}

/// One Graphile Worker effect attempt fenced from older workers.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EffectJob {
    /// Durable effect row ID.
    pub id: i64,
    /// Turn addressed by the effect.
    pub turn: Uuid,
    /// Exchange operation to perform.
    pub kind: EffectKind,
    /// Exact submit bytes, present for submit only.
    pub submit_body: Option<Vec<u8>>,
    /// Claim identity used to fence a late worker result.
    pub claim_token: Uuid,
    /// Number of possible submit sends, including this claimed send.
    pub submit_attempts: i32,
    /// The previous fourth send vanished before it could be settled.
    pub recovered_exhaustion: bool,
}

/// Install the task application's tables in their own schema.
pub async fn migrate(database: &Database) -> anyhow::Result<()> {
    database.migrate_in_schema("tasks", &MIGRATIONS).await?;
    WorkerUtils::new(database.pool().clone(), "graphile_worker")
        .migrate()
        .await?;
    Ok(())
}

/// Queue one durable effect in Graphile Worker with its stable ID and own queue.
async fn enqueue_effect(
    uow: &mut UnitOfWork<'_>,
    id: i64,
    generation: i32,
    delay_seconds: i32,
) -> Result<(), sqlx::Error> {
    let turn: Uuid = sqlx::query_scalar("SELECT turn_id FROM tasks.effects WHERE id=$1")
        .bind(id)
        .fetch_one(uow.connection())
        .await?;
    let queue = format!("exchange:turn:{turn}");
    let key = format!("exchange-effect:{id}:{generation}");
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'exchange_effect', payload => json_build_object('id',$1), \
         queue_name => $2, run_at => now()+$4::int * interval '1 second', \
         max_attempts => 25, job_key => $3)).id",
    )
    .bind(id)
    .bind(queue)
    .bind(key)
    .bind(delay_seconds)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Queue one request decision in its issue's serial Graphile queue.
async fn enqueue_decision(
    uow: &mut UnitOfWork<'_>,
    request: &Request,
    id: Uuid,
) -> Result<(), sqlx::Error> {
    let queue = format!(
        "task-decision:{}:{}",
        request.issue.repository_id, request.issue.number
    );
    let key = format!("task-decision:{id}");
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'task_decision', \
         payload => json_build_object('repository_id',$1,'issue_number',$2), \
         queue_name => $3, max_attempts => 25, job_key => $4)).id",
    )
    .bind(request.issue.repository_id)
    .bind(request.issue.number)
    .bind(queue)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Queue the pinned GitHub reads for one applied start revision.
async fn enqueue_initialization(
    uow: &mut UnitOfWork<'_>,
    request: &Request,
    revision: u64,
) -> Result<(), sqlx::Error> {
    let queue = format!(
        "task-initialize:{}:{}",
        request.issue.repository_id, request.issue.number
    );
    let key = format!("task-initialize:{}", request.id);
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'task_initialize', \
         payload => json_build_object('request_id',$1::text,'repository_id',$2,'issue_number',$3,'revision',$4), \
         queue_name => $5, max_attempts => 25, job_key => $6)).id",
    )
    .bind(request.id)
    .bind(request.issue.repository_id)
    .bind(request.issue.number)
    .bind(i64::try_from(revision).expect("task revision fits bigint"))
    .bind(queue)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Queue one intake attempt decision at its exact task revision.
async fn enqueue_intake(
    uow: &mut UnitOfWork<'_>,
    task: &TaskState,
    retired_turn: Option<Uuid>,
) -> Result<(), sqlx::Error> {
    let queue = format!("task-intake:{}", task.id.0);
    let key = match retired_turn {
        Some(turn) => format!("task-intake:{}:{}:retired:{turn}", task.id.0, task.revision),
        None => format!("task-intake:{}:{}", task.id.0, task.revision),
    };
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'task_intake', \
         payload => json_build_object('repository_id',$1,'issue_number',$2,'revision',$3), \
         queue_name => $4, max_attempts => 25, job_key => $5)).id",
    )
    .bind(task.issue.repository_id)
    .bind(task.issue.number)
    .bind(i64::try_from(task.revision).expect("task revision fits bigint"))
    .bind(queue)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Queue the next generation of a turn's long poll in its own serial queue.
async fn enqueue_poll(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    generation: i64,
    delay_seconds: i32,
) -> Result<(), sqlx::Error> {
    let key = format!("exchange-poll:{turn}:{generation}");
    let queue = format!("exchange:poll:{turn}");
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'exchange_poll', payload => json_build_object('turn',$1,'generation',$2), \
         queue_name => $3, run_at => now()+$5::int * interval '1 second', \
         max_attempts => 25, job_key => $4)).id",
    )
    .bind(turn)
    .bind(generation)
    .bind(queue)
    .bind(key)
    .bind(delay_seconds)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Lock one attempt before any effect row for that turn is changed.
async fn lock_attempt(uow: &mut UnitOfWork<'_>, turn: Uuid) -> Result<(), sqlx::Error> {
    sqlx::query("SELECT 1 FROM tasks.attempts WHERE id=$1 FOR UPDATE")
        .bind(turn)
        .fetch_one(uow.connection())
        .await?;
    Ok(())
}

/// Cancel every selected live attempt and retire only turns that may have reached exchange.
async fn cancel_attempts(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    task_id: Uuid,
    selected_turn: Option<Uuid>,
) -> anyhow::Result<u64> {
    let changed: Vec<Uuid> = sqlx::query_scalar(
        "UPDATE tasks.attempts SET status='cancelled' WHERE task_id=$1 \
         AND ($2::uuid IS NULL OR id=$2) \
         AND (status IN ('ready', 'sending', 'queued', 'running') \
              OR (status='unknown' AND outcome_reason='poll_failures_exhausted')) RETURNING id",
    )
    .bind(task_id)
    .bind(selected_turn)
    .fetch_all(uow.connection())
    .await?;
    for turn in &changed {
        retire_runner_sessions(uow, *turn).await?;
        events::append_turn_disposition(uow, outbox, *turn, "cancelled").await?;
    }
    let retirements: Vec<i64> = sqlx::query_scalar(
        "INSERT INTO tasks.effects (turn_id, kind, status) \
         SELECT a.id, 'retire', 'recorded' FROM tasks.attempts a \
         JOIN tasks.effects submit ON submit.turn_id=a.id AND submit.kind='submit' \
         WHERE a.task_id=$1 AND ($2::uuid IS NULL OR a.id=$2) \
         AND a.status='cancelled' AND submit.submit_attempts>0 \
         ON CONFLICT (turn_id, kind) DO NOTHING RETURNING id",
    )
    .bind(task_id)
    .bind(selected_turn)
    .fetch_all(uow.connection())
    .await?;
    for id in retirements {
        enqueue_effect(uow, id, 0, 0).await?;
    }
    sqlx::query(
        "UPDATE tasks.effects SET status='settled', updated_at=now() \
         WHERE kind='submit' AND status='recorded' AND turn_id IN \
         (SELECT id FROM tasks.attempts WHERE task_id=$1 \
          AND ($2::uuid IS NULL OR id=$2) AND status='cancelled')",
    )
    .bind(task_id)
    .bind(selected_turn)
    .execute(uow.connection())
    .await?;
    sqlx::query(
        "DELETE FROM tasks.staffing_groups WHERE turn_id IN \
         (SELECT a.id FROM tasks.attempts a JOIN tasks.effects e ON e.turn_id=a.id \
          WHERE a.task_id=$1 AND ($2::uuid IS NULL OR a.id=$2) \
          AND a.status='cancelled' AND e.kind='submit' AND e.status='settled' \
          AND NOT EXISTS (SELECT 1 FROM tasks.effects r WHERE r.turn_id=a.id AND r.kind='retire'))",
    )
    .bind(task_id)
    .bind(selected_turn)
    .execute(uow.connection())
    .await?;
    Ok(changed.len() as u64)
}

/// Lock issue control and task state before the attempt whose result may change the task.
async fn lock_issue_for_turn(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
) -> Result<Option<TaskState>, sqlx::Error> {
    let issue: Option<(i64, i64)> = sqlx::query_as(
        "SELECT s.repository_id,s.issue_number FROM tasks.attempts a \
         JOIN tasks.task_state s ON s.task_id=a.task_id WHERE a.id=$1",
    )
    .bind(turn)
    .fetch_optional(uow.connection())
    .await?;
    let Some((repository_id, issue_number)) = issue else {
        return Ok(None);
    };
    sqlx::query(
        "SELECT 1 FROM tasks.issue_controls WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(repository_id)
    .bind(issue_number)
    .fetch_one(uow.connection())
    .await?;
    let state: (Json<TaskState>,) = sqlx::query_as(
        "SELECT state FROM tasks.task_state WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(repository_id)
    .bind(issue_number)
    .fetch_one(uow.connection())
    .await?;
    Ok(Some(state.0.0))
}

/// Append the complete task state in the transaction that changed its revision.
async fn append_task_snapshot(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    state: &TaskState,
) -> anyhow::Result<()> {
    let pins = state.pins.as_ref();
    let data = TaskSnapshot {
        task_id: state.id,
        issue: state.issue,
        kind: "contribution".to_owned(),
        author: state.author.clone(),
        initialized: state.pins.is_some(),
        mode: "manual".to_owned(),
        stage: match state.stage {
            Stage::Initializing => None,
            Stage::Intake => Some("intake".to_owned()),
            Stage::Plan => Some("plan".to_owned()),
            Stage::Clarify => Some("clarify".to_owned()),
            Stage::Rejected => Some("rejected".to_owned()),
            Stage::Escalated => Some("escalated".to_owned()),
            Stage::Blocked => Some("blocked".to_owned()),
            Stage::Stopped => Some("stopped".to_owned()),
        },
        stage_reason: state.stage_reason.clone(),
        enrollment: None,
        plan: None,
        budgets: TaskBudgets {
            model_calls: TaskBudget {
                used: state.used_model_calls,
                max: pins.map_or(0, |pins| pins.max_model_calls),
            },
            fix_rounds: TaskBudget {
                used: 0,
                max: pins.map_or(0, |pins| pins.max_fix_rounds),
            },
            plan_revisions: TaskBudget {
                used: 0,
                max: pins.map_or(0, |pins| pins.max_plan_revisions),
            },
        },
        attention: state.stage == Stage::Blocked,
        pull_request: None,
    };
    record_stage_label_desire(uow, state, data.stage.as_deref()).await?;
    record_dashboard_desire(uow, state).await?;
    let identity = format!("task-state:{}:{}", state.id.0, state.revision);
    let event = Envelope::snapshot(
        EventId(Uuid::new_v5(&Uuid::NAMESPACE_OID, identity.as_bytes())),
        "tasks",
        Timestamp(Utc::now()),
        format!("task:{}", state.id.0),
        state.revision,
        data,
    );
    outbox.append(uow, &event).await?;
    Ok(())
}

/// Persist one final request disposition and its fact in the same transaction.
async fn finish_request(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    request: &Request,
    task_id: Option<TaskId>,
    status: RequestStatus,
    reason: Option<&str>,
) -> anyhow::Result<()> {
    let stored = match status {
        RequestStatus::Applied => "applied",
        RequestStatus::Refused => "refused",
        RequestStatus::Superseded => "superseded",
    };
    sqlx::query("UPDATE tasks.requests SET status=$2, reason=$3 WHERE id=$1")
        .bind(request.id)
        .bind(stored)
        .bind(reason)
        .execute(uow.connection())
        .await?;
    let command = request.kind.outcome_command();
    let outcome_status = match status {
        RequestStatus::Applied => RequestOutcomeStatus::Applied,
        RequestStatus::Refused => RequestOutcomeStatus::Refused,
        RequestStatus::Superseded => RequestOutcomeStatus::Superseded,
    };
    let event_id = Uuid::new_v5(
        &Uuid::NAMESPACE_OID,
        format!("request-outcome:{}", request.id).as_bytes(),
    );
    let event = Envelope::fact(
        EventId(event_id),
        "tasks",
        Timestamp(Utc::now()),
        RequestOutcome {
            request_id: RequestId(request.id),
            task_id,
            issue: request.issue,
            command,
            actor: request.actor.clone(),
            channel: request.channel.clone(),
            source: request.order.source.clone(),
            source_at: Timestamp(request.order.at),
            status: outcome_status,
            reason: reason.map(str::to_owned),
            reply: request.reply.clone(),
        },
    );
    outbox.append(uow, &event).await?;
    publication::queue_request_reply(uow, request, status, reason).await?;
    Ok(())
}

/// Record a channel request in the caller's delivery transaction; return its durable ID.
pub async fn record_request(
    uow: &mut UnitOfWork<'_>,
    request: &Request,
) -> Result<Uuid, sqlx::Error> {
    sqlx::query(
        "INSERT INTO tasks.issue_controls (repository_id, issue_number) VALUES ($1, $2) ON CONFLICT DO NOTHING",
    )
    .bind(request.issue.repository_id)
    .bind(request.issue.number)
    .execute(uow.connection())
    .await?;
    sqlx::query(
        "SELECT 1 FROM tasks.issue_controls WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(request.issue.repository_id)
    .bind(request.issue.number)
    .fetch_one(uow.connection())
    .await?;
    let id = sqlx::query_scalar(
        "INSERT INTO tasks.requests (id, repository_id, issue_number, channel, comment_id, source, source_at, source_id, source_index, request_class, request) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) \
         ON CONFLICT (channel, source, source_index) DO UPDATE SET id = requests.id RETURNING id",
    )
    .bind(request.id)
    .bind(request.issue.repository_id)
    .bind(request.issue.number)
    .bind(&request.channel)
    .bind(request.comment_id)
    .bind(&request.order.source)
    .bind(request.order.at)
    .bind(request.order.source_id)
    .bind(request.order.index)
    .bind(if matches!(request.kind, concurro_tasks_core::RequestKind::Stop) { 0_i16 } else { 1_i16 })
    .bind(Json(request))
    .fetch_one(uow.connection())
    .await?;
    enqueue_decision(uow, request, id).await?;
    Ok(id)
}

/// Finalize a comment revision and supersede pending commands from older revisions.
pub async fn record_comment_revision(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    revision: &CommentRevision,
) -> anyhow::Result<()> {
    sqlx::query(
        "INSERT INTO tasks.issue_controls (repository_id, issue_number) VALUES ($1,$2) ON CONFLICT DO NOTHING",
    )
    .bind(revision.issue.repository_id)
    .bind(revision.issue.number)
    .execute(uow.connection())
    .await?;
    sqlx::query(
        "SELECT 1 FROM tasks.issue_controls WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(revision.issue.repository_id)
    .bind(revision.issue.number)
    .fetch_one(uow.connection())
    .await?;
    sqlx::query(
        "INSERT INTO tasks.comment_revisions (repository_id,issue_number,comment_id,source,source_at,deleted) \
         VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (comment_id,source) DO UPDATE \
         SET deleted=tasks.comment_revisions.deleted OR EXCLUDED.deleted",
    )
    .bind(revision.issue.repository_id)
    .bind(revision.issue.number)
    .bind(revision.comment_id)
    .bind(&revision.order.source)
    .bind(revision.order.at)
    .bind(revision.deleted)
    .execute(uow.connection())
    .await?;
    if let Some(request) = &revision.request {
        record_request(uow, request).await?;
    }
    let deleted: bool = sqlx::query_scalar(
        "SELECT bool_or(deleted) FROM tasks.comment_revisions WHERE comment_id=$1",
    )
    .bind(revision.comment_id)
    .fetch_one(uow.connection())
    .await?;
    let at_latest: Vec<String> = sqlx::query_scalar(
        "SELECT source FROM tasks.comment_revisions WHERE comment_id=$1 AND source_at=( \
         SELECT max(source_at) FROM tasks.comment_revisions WHERE comment_id=$1) \
         ORDER BY source COLLATE \"C\"",
    )
    .bind(revision.comment_id)
    .fetch_all(uow.connection())
    .await?;
    let latest = if deleted || at_latest.len() > 1 {
        None
    } else {
        at_latest.first()
    };
    let obsolete: Vec<(Json<Request>,)> = sqlx::query_as(
        "SELECT request FROM tasks.requests WHERE comment_id=$1 \
         AND ($2::text IS NULL OR source<>$2) AND status='pending' FOR UPDATE",
    )
    .bind(revision.comment_id)
    .bind(latest)
    .fetch_all(uow.connection())
    .await?;
    let task_id: Option<Uuid> = sqlx::query_scalar(
        "SELECT task_id FROM tasks.task_state WHERE repository_id=$1 AND issue_number=$2",
    )
    .bind(revision.issue.repository_id)
    .bind(revision.issue.number)
    .fetch_optional(uow.connection())
    .await?;
    let reason = if deleted {
        "comment_deleted"
    } else if at_latest.len() > 1 {
        "ambiguous_comment_revision"
    } else {
        "superseded_by_edit"
    };
    for (Json(request),) in obsolete {
        finish_request(
            uow,
            outbox,
            &request,
            task_id.map(TaskId),
            RequestStatus::Superseded,
            Some(reason),
        )
        .await?;
    }
    Ok(())
}

/// Read the next request so its own actor can be authorized before application.
pub async fn next_pending(
    database: &Database,
    issue: IssueKey,
) -> Result<Option<Request>, sqlx::Error> {
    let pending: Option<(Uuid, Json<Request>)> = sqlx::query_as(NEXT_PENDING)
        .bind(issue.repository_id)
        .bind(issue.number)
        .fetch_optional(database.pool())
        .await?;
    Ok(pending.map(|(_, row)| row.0))
}

/// Find issues whose undecided requests need a fresh authority check.
pub async fn pending_issues(database: &Database) -> Result<Vec<IssueKey>, sqlx::Error> {
    let rows: Vec<(i64, i64)> = sqlx::query_as(
        "SELECT DISTINCT repository_id,issue_number FROM tasks.requests \
         WHERE status='pending' ORDER BY repository_id,issue_number",
    )
    .fetch_all(database.pool())
    .await?;
    Ok(rows
        .into_iter()
        .map(|(repository_id, number)| IssueKey {
            repository_id,
            number,
        })
        .collect())
}

/// Read the immutable source of an applied initialization job.
pub async fn applied_request(
    database: &Database,
    id: Uuid,
) -> Result<Option<Request>, sqlx::Error> {
    let row: Option<(Json<Request>,)> =
        sqlx::query_as("SELECT request FROM tasks.requests WHERE id=$1 AND status='applied'")
            .bind(id)
            .fetch_optional(database.pool())
            .await?;
    Ok(row.map(|row| row.0.0))
}

/// Pin the fork configuration commit in the same transaction as an applied start.
pub async fn pin_start_config(
    uow: &mut UnitOfWork<'_>,
    request_id: Uuid,
    task: &TaskState,
    config_sha: &str,
) -> anyhow::Result<()> {
    anyhow::ensure!(
        task.stage == Stage::Initializing
            && config_sha.len() == 40
            && config_sha.bytes().all(|byte| byte.is_ascii_hexdigit()),
        "applied start has no valid configuration commit"
    );
    sqlx::query(
        "INSERT INTO tasks.start_pins (task_id,request_id,task_revision,config_sha) \
         VALUES ($1,$2,$3,$4) ON CONFLICT (task_id) DO UPDATE \
         SET request_id=EXCLUDED.request_id,task_revision=EXCLUDED.task_revision,config_sha=EXCLUDED.config_sha",
    )
    .bind(task.id.0)
    .bind(request_id)
    .bind(i64::try_from(task.revision)?)
    .bind(config_sha.to_ascii_lowercase())
    .execute(uow.connection())
    .await?;
    Ok(())
}

/// Read only the configuration commit bound to this applied start revision.
pub async fn start_config_pin(
    database: &Database,
    issue: IssueKey,
    request_id: Uuid,
    revision: u64,
) -> Result<Option<String>, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT pin.config_sha FROM tasks.start_pins pin \
         JOIN tasks.task_state task ON task.task_id=pin.task_id \
         WHERE task.repository_id=$1 AND task.issue_number=$2 \
         AND pin.request_id=$3 AND pin.task_revision=$4",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .bind(request_id)
    .bind(i64::try_from(revision).expect("task revision fits bigint"))
    .fetch_optional(database.pool())
    .await
}

/// Apply only the authorized request if it remains first under the issue's row lock.
pub async fn apply_request(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    candidate: Uuid,
    issue: IssueKey,
    authority: Authority,
) -> anyhow::Result<Option<(Request, Decision)>> {
    let control: Option<(Option<Json<SourceOrder>>, Option<Json<SourceOrder>>)> = sqlx::query_as(
        "SELECT applied_stop,applied_lifecycle FROM tasks.issue_controls WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    let Some((stop, lifecycle)) = control else {
        return Ok(None);
    };
    let pending: Option<(Uuid, Json<Request>)> = sqlx::query_as(NEXT_PENDING)
        .bind(issue.repository_id)
        .bind(issue.number)
        .fetch_optional(uow.connection())
        .await?;
    let Some((id, Json(request))) = pending else {
        return Ok(None);
    };
    if id != candidate {
        return Ok(None);
    }
    let current: Option<(Json<TaskState>,)> = sqlx::query_as(
        "SELECT state FROM tasks.task_state WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    let mut decision = decide(
        &request,
        current.as_ref().map(|row| &row.0.0),
        stop.as_ref().map(|row| &row.0),
        lifecycle.as_ref().map(|row| &row.0),
        authority,
    );
    if request.channel == "issue_close" && authority.issue_open {
        decision = Decision {
            status: RequestStatus::Refused,
            reason: Some("issue_reopened"),
            task: None,
            stop_order: None,
        };
    }
    if authority.writer
        && matches!(
            request.kind,
            concurro_tasks_core::RequestKind::Start | concurro_tasks_core::RequestKind::Stop
        )
        && decision.status != RequestStatus::Superseded
    {
        let prior: bool = sqlx::query_scalar(
            "SELECT EXISTS (SELECT 1 FROM tasks.requests WHERE repository_id=$1 AND issue_number=$2 \
             AND id<>$3 AND channel='comment' AND status='applied' \
             AND request->>'reply'=$4)",
        )
        .bind(issue.repository_id)
        .bind(issue.number)
        .bind(id)
        .bind(&request.reply)
        .fetch_one(uow.connection())
        .await?;
        if prior {
            decision = Decision {
                status: RequestStatus::Refused,
                reason: Some("already_applied"),
                task: None,
                stop_order: None,
            };
        }
    }
    if let Some(task) = &decision.task {
        let updated = sqlx::query(
            "INSERT INTO tasks.task_state (repository_id, issue_number, task_id, revision, state) \
             VALUES ($1, $2, $3, $4, $5) ON CONFLICT (repository_id, issue_number) DO UPDATE \
             SET revision=EXCLUDED.revision, state=EXCLUDED.state \
             WHERE tasks.task_state.revision=EXCLUDED.revision-1",
        )
        .bind(issue.repository_id)
        .bind(issue.number)
        .bind(task.id.0)
        .bind(i64::try_from(task.revision).expect("task revision fits bigint"))
        .bind(Json(task))
        .execute(uow.connection())
        .await?;
        anyhow::ensure!(updated.rows_affected() == 1, "task revision changed");
        append_task_snapshot(uow, outbox, task).await?;
        if task.stage == Stage::Initializing {
            enqueue_initialization(uow, &request, task.revision).await?;
        }
    }
    if let Some(stop) = &decision.stop_order {
        sqlx::query("UPDATE tasks.issue_controls SET applied_stop=$3 WHERE repository_id=$1 AND issue_number=$2")
            .bind(issue.repository_id)
            .bind(issue.number)
            .bind(Json(stop))
            .execute(uow.connection())
            .await?;
    }
    if decision.status == RequestStatus::Applied
        && matches!(
            request.kind,
            concurro_tasks_core::RequestKind::Start | concurro_tasks_core::RequestKind::Stop
        )
    {
        sqlx::query("UPDATE tasks.issue_controls SET applied_lifecycle=$3 WHERE repository_id=$1 AND issue_number=$2")
            .bind(issue.repository_id)
            .bind(issue.number)
            .bind(Json(&request.order))
            .execute(uow.connection())
            .await?;
    }
    if matches!(request.kind, concurro_tasks_core::RequestKind::Stop)
        && decision.status == RequestStatus::Applied
    {
        if let Some(task) = current.as_ref() {
            cancel_attempts(uow, outbox, task.0.0.id.0, None).await?;
        }
    }
    finish_request(
        uow,
        outbox,
        &request,
        decision
            .task
            .as_ref()
            .map(|task| task.id)
            .or_else(|| current.as_ref().map(|row| row.0.0.id)),
        decision.status,
        decision.reason,
    )
    .await?;
    Ok(Some((request, decision)))
}

/// Commit pinned GitHub reads only for the initialization revision that requested them.
pub async fn initialize_task(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    issue: IssueKey,
    expected_revision: u64,
    snapshot: &InitializationSnapshot,
) -> anyhow::Result<Option<TaskState>> {
    sqlx::query(
        "SELECT 1 FROM tasks.issue_controls WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    let current: Option<(Json<TaskState>,)> = sqlx::query_as(
        "SELECT state FROM tasks.task_state WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    let Some((Json(current),)) = current else {
        return Ok(None);
    };
    if current.revision != expected_revision {
        return Ok(None);
    }
    let Some(initialized) = initialize(&current, snapshot.pins.clone()) else {
        return Ok(None);
    };
    let updated = sqlx::query(
        "UPDATE tasks.task_state SET revision=$3, state=$4 WHERE repository_id=$1 AND issue_number=$2 AND revision=$5",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .bind(i64::try_from(initialized.revision).expect("task revision fits bigint"))
    .bind(Json(&initialized))
    .bind(i64::try_from(expected_revision).expect("task revision fits bigint"))
    .execute(uow.connection())
    .await?;
    anyhow::ensure!(updated.rows_affected() == 1, "task revision changed");
    sqlx::query(
        "INSERT INTO tasks.initialization_inputs (task_id, task_revision, snapshot) \
         VALUES ($1,$2,$3) ON CONFLICT (task_id) DO UPDATE \
         SET task_revision=EXCLUDED.task_revision, snapshot=EXCLUDED.snapshot",
    )
    .bind(initialized.id.0)
    .bind(i64::try_from(initialized.revision)?)
    .bind(Json(snapshot))
    .execute(uow.connection())
    .await?;
    append_task_snapshot(uow, outbox, &initialized).await?;
    let welcome_actor: Option<String> = sqlx::query_scalar(
        "SELECT r.request->'actor'->>'login' FROM tasks.start_pins p \
         JOIN tasks.requests r ON r.id=p.request_id WHERE p.task_id=$1",
    )
    .bind(initialized.id.0)
    .fetch_optional(uow.connection())
    .await?;
    let welcome = welcome_body(
        &snapshot.configuration,
        welcome_actor
            .as_deref()
            .unwrap_or(&initialized.author.login),
    );
    let notice_id = queue_task_comment(
        uow,
        &initialized,
        "notice",
        &format!("welcome:{}", initialized.revision),
        &welcome,
    )
    .await?;
    events::append_notice(uow, outbox, &initialized, notice_id, "welcome", &welcome).await?;
    enqueue_intake(uow, &initialized, None).await?;
    Ok(Some(initialized))
}

/// Charge and record a fresh intake turn with its exact submit body and staffing reservation.
pub async fn admit_intake(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    issue: IssueKey,
    spec: &AttemptSpec,
) -> anyhow::Result<Admission> {
    sqlx::query(
        "SELECT 1 FROM tasks.issue_controls WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    let current: Option<(Json<TaskState>,)> = sqlx::query_as(
        "SELECT state FROM tasks.task_state WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    let Some((Json(mut current),)) = current else {
        return Ok(Admission::Stale);
    };
    let existing: Option<(Uuid, i64, String, String, String, Vec<u8>, Option<Vec<u8>>, chrono::DateTime<Utc>, bool)> = sqlx::query_as(
        "SELECT task_id,expected_revision,staffing_group,fork,config_sha,submit_body,prompt_body,initial_deadline,unbounded_running \
         FROM tasks.attempts WHERE id=$1",
    )
    .bind(spec.id)
    .fetch_optional(uow.connection())
    .await?;
    if let Some((
        task_id,
        revision,
        group,
        fork,
        config_sha,
        body,
        prompt_body,
        deadline,
        unbounded_running,
    )) = existing
    {
        anyhow::ensure!(
            task_id == spec.task.0
                && revision == i64::try_from(spec.expected_revision)?
                && group == spec.staffing_group
                && fork == spec.fork
                && config_sha == spec.config_sha
                && body == spec.submit_body
                && prompt_body == spec.prompt_body
                && deadline == spec.deadline
                && unbounded_running == spec.unbounded_running,
            "attempt identity changed on replay"
        );
        return Ok(Admission::Existing);
    }
    if current.id != spec.task || current.revision != spec.expected_revision {
        return Ok(Admission::Stale);
    }
    if current.stage != Stage::Intake || current.current_attempt.is_some() {
        return Ok(Admission::Stale);
    }
    let Some(pins) = &current.pins else {
        return Ok(Admission::Stale);
    };
    if current.used_model_calls >= pins.max_model_calls {
        current.stage = Stage::Blocked;
        current.stage_reason = Some("model-call budget exhausted".to_owned());
        current.revision += 1;
        let updated = sqlx::query("UPDATE tasks.task_state SET revision=$3, state=$4 WHERE repository_id=$1 AND issue_number=$2 AND revision=$5")
            .bind(issue.repository_id)
            .bind(issue.number)
            .bind(i64::try_from(current.revision).expect("task revision fits bigint"))
            .bind(Json(&current))
            .bind(i64::try_from(current.revision - 1).expect("task revision fits bigint"))
            .execute(uow.connection())
            .await?;
        anyhow::ensure!(updated.rows_affected() == 1, "task revision changed");
        append_task_snapshot(uow, outbox, &current).await?;
        return Ok(Admission::BudgetExhausted);
    }
    sqlx::query(
        "INSERT INTO tasks.staffing_groups (staffing_group, turn_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
    )
    .bind(&spec.staffing_group)
    .bind(spec.id)
    .execute(uow.connection())
    .await?;
    let owner: Uuid =
        sqlx::query_scalar("SELECT turn_id FROM tasks.staffing_groups WHERE staffing_group=$1")
            .bind(&spec.staffing_group)
            .fetch_one(uow.connection())
            .await?;
    if owner != spec.id {
        return Ok(Admission::WaitingForGroup);
    }
    sqlx::query(
        "INSERT INTO tasks.attempts (id, task_id, operation, expected_revision, staffing_group, fork, config_sha, submit_body, prompt_body, initial_deadline, deadline, unbounded_running, status) \
         VALUES ($1, $2, 'intake', $3, $4, $5, $6, $7, $8, $9, $9, $10, 'ready')",
    )
    .bind(spec.id)
    .bind(spec.task.0)
    .bind(i64::try_from(spec.expected_revision)?)
    .bind(&spec.staffing_group)
    .bind(&spec.fork)
    .bind(&spec.config_sha)
    .bind(&spec.submit_body)
    .bind(&spec.prompt_body)
    .bind(spec.deadline)
    .bind(spec.unbounded_running)
    .execute(uow.connection())
    .await?;
    events::append_turn_submitted(uow, outbox, &current, spec).await?;
    let effect_id: i64 = sqlx::query_scalar(
        "INSERT INTO tasks.effects (turn_id, kind, status) VALUES ($1, 'submit', 'recorded') RETURNING id",
    )
    .bind(spec.id)
    .fetch_one(uow.connection())
    .await?;
    enqueue_effect(uow, effect_id, 0, 0).await?;
    current.used_model_calls += 1;
    current.current_attempt = Some(spec.id);
    current.revision += 1;
    let updated = sqlx::query("UPDATE tasks.task_state SET revision=$3, state=$4 WHERE repository_id=$1 AND issue_number=$2 AND revision=$5")
        .bind(issue.repository_id)
        .bind(issue.number)
        .bind(i64::try_from(current.revision).expect("task revision fits bigint"))
        .bind(Json(&current))
        .bind(i64::try_from(current.revision - 1).expect("task revision fits bigint"))
        .execute(uow.connection())
        .await?;
    anyhow::ensure!(updated.rows_affected() == 1, "task revision changed");
    append_task_snapshot(uow, outbox, &current).await?;
    Ok(Admission::Admitted)
}

/// Read the current durable task for an issue.
pub async fn task(database: &Database, issue: IssueKey) -> Result<Option<TaskState>, sqlx::Error> {
    let state: Option<(Json<TaskState>,)> = sqlx::query_as(
        "SELECT state FROM tasks.task_state WHERE repository_id=$1 AND issue_number=$2",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(database.pool())
    .await?;
    Ok(state.map(|row| row.0.0))
}

/// Read the exact pinned source snapshot for a task's current initialization.
pub async fn initialization_snapshot(
    database: &Database,
    task_id: TaskId,
) -> Result<Option<InitializationSnapshot>, sqlx::Error> {
    let row: Option<(Json<InitializationSnapshot>,)> =
        sqlx::query_as("SELECT snapshot FROM tasks.initialization_inputs WHERE task_id=$1")
            .bind(task_id.0)
            .fetch_optional(database.pool())
            .await?;
    Ok(row.map(|row| row.0.0))
}
