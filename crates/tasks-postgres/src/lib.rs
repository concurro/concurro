//! PostgreSQL state and serialized request decisions for the task application.

use crate::recovery::RecoveryDecision;
pub use archive::RetainedInput;
pub use sponsorship::InitialSponsorship;
pub use standalone::StandaloneReviewAuthority;
pub use standalone_commands::StandaloneActionSource;
mod archive;
mod baseline;
mod browser_actions;
mod config;
mod coverage_gap;
mod credential_revocation;
mod cutover;
mod funding_reads;
mod historical;
use archive::{RetainedCheck, check_retained_attempt};
pub use funding_reads::{own_charged_calls, unavailable_historical_usage};
pub use historical::{historical_display, import_historical_display};
mod artifacts;
mod dashboard;
mod design;
mod diagnostic_reads;
mod effects;
mod execution_owner;
pub use design::{
    DesignBatch, DesignCorrection, DesignResult, admit_design_batch, admit_design_correction,
    apply_design_result, design_batch, design_correction, design_result, open_plan_objections,
    reviewer_objections, reviewer_session_epoch,
};
use execution_owner::{ExecutionOwner, lock_execution_owner};
mod events;
mod gates;
pub use archive::{
    RetainedEligibility, RetainedPreparation, fail_archive_upload, pending_archive,
    retained_eligibility, retained_preparation, retry_archive_upload, store_archive_snapshot,
    verify_archive,
};
pub use baseline::{BaselineRow, baseline_page, create_baseline, finish_baseline};
pub use browser_actions::{
    BrowserAction, RequestRecord, browser_action, browser_retry_feedback, record_browser_action,
    request_record,
};
pub use config::{current_config_targets, sync_config_targets};
pub use coverage_gap::{
    PendingCoverageGap, comment_revision_recorded, complete_coverage_gap, fail_coverage_gap,
    known_live_comments, pending_coverage_gaps, record_coverage_gap,
    record_coverage_source_failure, resolve_coverage_source_failure,
    unresolved_coverage_source_count,
};
pub use credential_revocation::{
    apply_credential_change, lock_credential_versions, withdrawn_grants,
};
pub use cutover::{CutoverFloor, cutover_floor, import_cutover_manifest, source_after_activation};
pub use dashboard::{
    DashboardSurface, begin_dashboard_edit, dashboard_surface, enqueue_dashboard,
    finish_dashboard_edit, observe_dashboard, record_dashboard_desire, watched_dashboards,
};
pub use diagnostic_reads::{
    TranscriptLine, imported_transcript_page, transcript_page, turn_artifact,
};
pub use effects::{
    acknowledge_retirement, claim_effect, due_attempt_deadlines, expire_attempt,
    finish_unapplied_result, pollable_generation, record_observation, record_poll_failure,
    record_rust_finished, record_rust_observation, record_submit_failure, recover_retirements,
    settle_submit, settle_submit_refusal,
};
pub use python_import::{import_python_manifest, pending_python_import_archives};
mod git_publication;
mod implementation;
mod intake;
mod plan;
mod pr_review;
mod pr_review_actions;
mod python_import;
mod read_model;
mod recovery;
mod rust_exchange;
mod withdrawal;
pub use recovery::CommandEvidence;
pub use rust_exchange::{
    OrgAttempt, admit_rust_revision, due_rust_observations, freeze_submit, org_attempt,
    record_usage,
};
pub use withdrawal::{complete_withdrawal_step, due_withdrawal_steps};
mod report_publication;
pub use gates::{
    GateInputs, TerminalPrInputs, confirm_ready, fail_ready_intent, gate_inputs, mark_ready_sent,
    observe_ready_seen, record_gate_reason, record_terminal_pr, reserve_ready_intent,
    retry_ready_intent, terminal_pr_inputs, watched_gate_issues,
};
pub use git_publication::{
    GitPublicationInput, GitPublicationIntent, block_git_publication, complete_git_publication,
    git_publication_input, queue_implementation_started, reserve_git_publication,
    save_git_publication,
};
pub use implementation::{
    ImplementationAssessment, ImplementationCorrection, ImplementationInputs, ImplementationResult,
    apply_implementation_result, implementation_inputs, implementation_result,
    implementation_session_epoch, latest_implementation_correction,
};
pub use plan::{
    PlanCorrection, PlanRevision, continue_planner_answer, latest_plan_correction,
    latest_plan_revision, planner_session_epoch,
};
pub use pr_review::{
    PrReviewBatch, PrReviewCorrection, PrReviewPublication, PrReviewResult, admit_pr_review_batch,
    admit_pr_review_correction, apply_pr_review_result, block_pr_review_batch,
    confirm_pr_review_publication, fail_pr_review_publication, mark_pr_review_sent,
    pending_pr_review_readbacks, pr_review_batch, pr_review_correction, pr_review_publication,
    pr_review_result, retry_pr_review_publication,
};
pub use pr_review_actions::{
    PrReviewAction, confirm_pr_review_action, fail_pr_review_action, fallback_pr_review_thread,
    mark_pr_review_action_sent, next_pr_review_action, pr_review_actions_complete,
    retry_pr_review_action,
};
pub use report_publication::{
    ReportArtifactWrite, confirm_report_artifact, fail_report_artifact, mark_report_artifact_sent,
    pending_report_artifact_readbacks, report_artifact_write, retry_report_artifact,
};
mod operator;
mod publication;
mod runner;
mod sponsorship;
mod stage_label;
mod standalone;
mod standalone_checks;
mod standalone_commands;
mod standalone_publication;
mod target_index;
pub use intake::{IntakeCorrection, applicable_intake_remarks, latest_intake_correction};
pub use operator::{
    cancel_attempt, list_tasks, pending_effects, resolve_pending_write, task_detail,
};
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
pub use sponsorship::{
    ConsentSources, FundingSlot, SponsorshipWrite, acknowledge_sponsorship_transition,
    activate_funding_policy, claim_sponsorship_transition, confirm_current_sponsorship_sources,
    consent_projection, consent_sources, excluded_credential_versions, fail_sponsorship_transition,
    funding_policy_at, funding_slot, record_initial_sponsorship, record_sponsorship_transition,
    record_standalone_funding_fact_transition, record_standalone_initial_sponsorship,
    record_withdraw_transition, standalone_consent_sources, standalone_sponsorship_initialized,
};
use sponsorship::{billable_model_calls, record_funding_grant, validate_funding_admission};
pub use stage_label::{
    StageLabelSurface, begin_stage_label_write, enqueue_stage_label, finish_stage_label_write,
    observe_stage_label, record_stage_label_desire, stage_label_surface, watched_stage_labels,
};
pub use standalone::{
    StandaloneReviewRequest, StandaloneReviewResult, StandaloneReviewRound, StandaloneReviewWake,
    admit_standalone_review_attempt, apply_standalone_review_request,
    apply_standalone_review_result, await_standalone_review_credentials,
    defer_standalone_review_wake, enqueue_standalone_review_scan, next_standalone_review_request,
    observe_standalone_review_pr_state, observe_standalone_review_wake,
    pending_standalone_review_issues, pending_standalone_review_results,
    pending_standalone_review_rounds, pending_standalone_review_wakes,
    record_standalone_review_request, resume_standalone_review_credentials,
    standalone_review_attempts, standalone_review_boundary, standalone_review_correction,
    standalone_review_outstanding_findings, standalone_review_result, standalone_review_round,
    standalone_review_source_is_current, standalone_review_task, standalone_task_repository_id,
    watched_standalone_review_prs,
};
pub use standalone_checks::{
    StandaloneReviewCheck, adopt_standalone_check_run, fail_standalone_check,
    mark_standalone_check_sent, miss_standalone_check_create, prior_standalone_checks,
    retire_standalone_check, settle_standalone_check, standalone_check_progress,
    standalone_review_check,
};
pub use standalone_commands::{
    StandaloneReviewCommand, apply_standalone_review_control, browser_standalone_action,
    next_standalone_review_command, pending_standalone_review_commands,
    record_browser_standalone_action, record_standalone_funding_revision,
    record_standalone_review_command, record_standalone_withdrawal_steps,
    settle_standalone_review_command, standalone_action_record,
};
pub use standalone_publication::{
    StandaloneDisposition, StandaloneReviewPublication, confirm_standalone_disposition_reply,
    confirm_standalone_review_publication, fail_standalone_disposition_reply,
    fail_standalone_review_publication, fallback_standalone_review_comments,
    finish_standalone_thread_resolution, intend_standalone_thread_resolution,
    mark_standalone_disposition_reply_sent, mark_standalone_review_sent,
    next_standalone_disposition, pending_standalone_dispositions,
    pending_standalone_review_readbacks, retry_standalone_disposition_reply,
    retry_standalone_review_publication, standalone_review_publication,
};
pub use target_index::{
    IndexEntry, IndexRow, held_issue_numbers, historical_pulls, index_item, index_page,
    index_run_links, index_scan_started, index_scan_status, owned_pr_numbers, pr_is_owned,
    record_index_scan, replace_target_index, upsert_index_event,
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
    LaterCommand, Request, RequestKind, RequestStatus, SourceOrder, Stage, TaskState,
    consent_facts, decide, initialize, welcome_body,
};
use graphile_worker::WorkerUtils;
use sqlx::{migrate::Migrator, types::Json};
use uuid::Uuid;

/// Columns read by `apply_request_with_evidence_mode`.
type ApplyRequestWithEvidenceModeControlRow = (
    Option<Json<SourceOrder>>,
    Option<Json<SourceOrder>>,
    Option<Json<SourceOrder>>,
);

/// Columns read by `admit_stage`.
#[derive(sqlx::FromRow)]
struct AttemptIdentityRow {
    /// Persisted task id.
    task_id: Uuid,
    /// Persisted operation.
    operation: String,
    /// Persisted expected revision.
    expected_revision: i64,
    /// Persisted staffing group.
    staffing_group: String,
    /// Persisted fork.
    fork: String,
    /// Persisted config sha.
    config_sha: String,
    /// Persisted submit body.
    submit_body: Vec<u8>,
    /// Persisted prompt body.
    prompt_body: Option<Vec<u8>>,
    /// Persisted initial deadline.
    initial_deadline: chrono::DateTime<Utc>,
    /// Persisted unbounded running.
    unbounded_running: bool,
}

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
    concurro_events_postgres::migrate(database).await?;
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

/// Wake a timeline scan for an opt-in label delivery.
pub async fn enqueue_label_scan(
    uow: &mut UnitOfWork<'_>,
    issue: IssueKey,
    delivery_id: &str,
    action: &str,
    actor_id: i64,
    updated_at: chrono::DateTime<Utc>,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO tasks.label_wakes \
         (delivery_id,repository_id,issue_number,action,actor_id,updated_at) \
         VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (delivery_id) DO NOTHING",
    )
    .bind(delivery_id)
    .bind(issue.repository_id)
    .bind(issue.number)
    .bind(action)
    .bind(actor_id)
    .bind(updated_at)
    .execute(uow.connection())
    .await?;
    let queue = format!("task-label:{}:{}", issue.repository_id, issue.number);
    let key = format!("task-label:{delivery_id}");
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'task_label_scan', \
         payload => json_build_object('repository_id',$1,'issue_number',$2, \
             'action',$3,'actor_id',$4,'updated_at',$5,'delivery_id',$6), \
         queue_name => $7, max_attempts => 25, job_key => $8)).id",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .bind(action)
    .bind(actor_id)
    .bind(updated_at)
    .bind(delivery_id)
    .bind(queue)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// A label delivery whose exact source has not yet appeared in the issue timeline.
#[derive(sqlx::FromRow)]
pub struct PendingLabelWake {
    /// Delivery GUID used to settle the wake.
    pub delivery_id: String,
    /// Numeric target repository ID.
    pub repository_id: i64,
    /// Repository-local issue number.
    pub issue_number: i64,
    /// Label action from the authenticated delivery.
    pub action: String,
    /// Numeric authenticated sender ID.
    pub actor_id: i64,
    /// GitHub issue update time captured in the delivery.
    pub updated_at: chrono::DateTime<Utc>,
}

/// Find label wakes that still need timeline readback after worker retries.
pub async fn pending_label_wakes(
    database: &Database,
) -> Result<Vec<PendingLabelWake>, sqlx::Error> {
    sqlx::query_as(
        "SELECT delivery_id,repository_id,issue_number,action,actor_id,updated_at \
         FROM tasks.label_wakes WHERE observed_at IS NULL AND exhausted_at IS NULL \
         AND retry_at<=now() \
         ORDER BY retry_at,delivery_id",
    )
    .fetch_all(database.pool())
    .await
}

/// Settle a label wake only after its action is found in the issue timeline.
pub async fn observe_label_wake(database: &Database, delivery_id: &str) -> Result<(), sqlx::Error> {
    sqlx::query("UPDATE tasks.label_wakes SET observed_at=now() WHERE delivery_id=$1")
        .bind(delivery_id)
        .execute(database.pool())
        .await?;
    Ok(())
}

/// Back off an unresolved label wake while retaining it for later readback.
pub async fn defer_label_wake(
    database: &Database,
    delivery_id: &str,
    error: &str,
) -> Result<bool, sqlx::Error> {
    let exhausted: Option<bool> = sqlx::query_scalar(
        "UPDATE tasks.label_wakes SET scan_attempts=scan_attempts+1, \
         retry_at=now()+(LEAST(3600,5*power(2,LEAST(scan_attempts,10)))::integer * interval '1 second'), \
         exhausted_at=CASE WHEN scan_attempts+1>=48 THEN now() ELSE NULL END, \
         last_error=left($2,500) \
         WHERE delivery_id=$1 AND observed_at IS NULL AND exhausted_at IS NULL \
         RETURNING exhausted_at IS NOT NULL",
    )
    .bind(delivery_id)
    .bind(error)
    .fetch_optional(database.pool())
    .await?;
    Ok(exhausted.unwrap_or(false))
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

/// Queue a planner turn at the exact task revision accepted by intake.
async fn enqueue_plan(uow: &mut UnitOfWork<'_>, task: &TaskState) -> Result<(), sqlx::Error> {
    let queue = format!("task-plan:{}", task.id.0);
    let key = format!("task-plan:{}:{}", task.id.0, task.revision);
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'task_plan', \
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

/// Queue a source read for an authorized answer to planner questions.
async fn enqueue_clarification(
    uow: &mut UnitOfWork<'_>,
    task: &TaskState,
    request: Uuid,
) -> Result<(), sqlx::Error> {
    let queue = format!("task-clarification:{}", task.id.0);
    let key = format!("task-clarification:{}:{}", task.id.0, task.revision);
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'task_clarification', \
         payload => json_build_object('repository_id',$1,'issue_number',$2,'revision',$3,'request_id',$4::text), \
         queue_name => $5,max_attempts => 25,job_key => $6)).id",
    )
    .bind(task.issue.repository_id)
    .bind(task.issue.number)
    .bind(i64::try_from(task.revision).expect("task revision fits bigint"))
    .bind(request)
    .bind(queue)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Queue the recorded independent design roster at one task revision.
async fn enqueue_design_batch(
    uow: &mut UnitOfWork<'_>,
    task: &TaskState,
    batch: Uuid,
) -> Result<(), sqlx::Error> {
    let queue = format!("task-design:{}", task.id.0);
    let key = format!("task-design:{batch}");
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'task_design_batch', \
         payload => json_build_object('repository_id',$1,'issue_number',$2,'revision',$3,'batch',$4::text), \
         queue_name => $5, max_attempts => 25, job_key => $6)).id",
    )
    .bind(task.issue.repository_id)
    .bind(task.issue.number)
    .bind(i64::try_from(task.revision).expect("task revision fits bigint"))
    .bind(batch)
    .bind(queue)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Queue source-aware validation of one retained design reviewer result.
async fn enqueue_design_result(uow: &mut UnitOfWork<'_>, turn: Uuid) -> Result<(), sqlx::Error> {
    let key = format!("task-design-result:{turn}");
    let queue = format!("task-design-result:{turn}");
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'task_design_result', payload => json_build_object('turn',$1::text), \
         queue_name => $2, max_attempts => 25, job_key => $3)).id",
    )
    .bind(turn)
    .bind(queue)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Queue source-aware validation of one retained implementer result.
async fn enqueue_implementation_result(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
) -> Result<(), sqlx::Error> {
    let key = format!("task-implementation-result:{turn}");
    let queue = format!("task-implementation-result:{turn}");
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'task_implementation_result', payload => json_build_object('turn',$1::text), \
         queue_name => $2, max_attempts => 25, job_key => $3)).id",
    )
    .bind(turn)
    .bind(queue)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Queue one recoverable fork and draft PR publication for an accepted candidate.
async fn enqueue_git_publication(
    uow: &mut UnitOfWork<'_>,
    task: &TaskState,
) -> Result<(), sqlx::Error> {
    let key = format!("task-git-publication:{}", task.id.0);
    let queue = key.clone();
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'task_git_publication', \
         payload => json_build_object('repository_id',$1,'issue_number',$2), \
         queue_name => $3,max_attempts => 25,job_key => $4)).id",
    )
    .bind(task.issue.repository_id)
    .bind(task.issue.number)
    .bind(queue)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Queue one bounded correction for a single review seat.
async fn enqueue_design_correction(
    uow: &mut UnitOfWork<'_>,
    batch: Uuid,
    reviewer: &str,
    count: i32,
) -> Result<(), sqlx::Error> {
    let key = format!("task-design-correction:{batch}:{reviewer}:{count}");
    let queue = format!("task-design-correction:{batch}:{reviewer}");
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'task_design_correction', \
         payload => json_build_object('batch',$1::text,'reviewer',$2,'count',$3), \
         queue_name => $4, max_attempts => 25, job_key => $5)).id",
    )
    .bind(batch)
    .bind(reviewer)
    .bind(count)
    .bind(queue)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Queue one owner-bound PR review repair without disturbing sibling seats.
async fn enqueue_pr_review_correction(
    uow: &mut UnitOfWork<'_>,
    batch: Uuid,
    reviewer: &str,
    count: i32,
) -> Result<(), sqlx::Error> {
    let key = format!("task-pr-review-correction:{batch}:{reviewer}:{count}");
    let queue = format!("task-pr-review:{batch}:{reviewer}");
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'task_pr_review_correction', \
         payload => json_build_object('batch',$1::text,'reviewer',$2,'count',$3), \
         queue_name => $4,max_attempts => 25,job_key => $5)).id",
    )
    .bind(batch)
    .bind(reviewer)
    .bind(count)
    .bind(queue)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Queue the writer-approved implementer at its exact task revision.
async fn enqueue_implementation(
    uow: &mut UnitOfWork<'_>,
    task: &TaskState,
) -> Result<(), sqlx::Error> {
    let queue = format!("task-implement:{}", task.id.0);
    let key = format!("task-implement:{}:{}", task.id.0, task.revision);
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'task_implement', \
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

/// Queue one current-head PR and CI gate observation.
async fn enqueue_gates(uow: &mut UnitOfWork<'_>, task: &TaskState) -> Result<(), sqlx::Error> {
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job(identifier => 'task_gates', \
         payload => json_build_object('repository_id',$1,'issue_number',$2,'revision',$3), \
         queue_name => $4,max_attempts => 25,job_key => $5)).id",
    )
    .bind(task.issue.repository_id)
    .bind(task.issue.number)
    .bind(i64::try_from(task.revision).expect("task revision fits bigint"))
    .bind(format!("task-gates:{}", task.id.0))
    .bind(format!("task-gates:{}:{}", task.id.0, task.revision))
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
    sqlx::query(
        "UPDATE tasks.funding_grants SET status='revoked',revoked_at=COALESCE(revoked_at,now()) \
         WHERE attempt_id=ANY($1) AND status='issued'",
    )
    .bind(&changed)
    .execute(uow.connection())
    .await?;
    sqlx::query(
        "UPDATE tasks.funding_grants g SET reservation_status='released',released_at=now() \
         WHERE g.attempt_id=ANY($1) AND g.purpose='task_contributed' \
         AND g.reservation_status='charged' AND EXISTS ( \
             SELECT 1 FROM tasks.effects e WHERE e.turn_id=g.attempt_id \
             AND e.kind='submit' AND e.submit_attempts=0)",
    )
    .bind(&changed)
    .execute(uow.connection())
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
    let Some(ExecutionOwner::Contribution {
        repository_id,
        issue_number,
    }) = lock_execution_owner(uow, turn).await?
    else {
        return Ok(None);
    };
    let state: (Json<TaskState>,) = sqlx::query_as(
        "SELECT state FROM tasks.task_state WHERE repository_id=$1 AND issue_number=$2",
    )
    .bind(repository_id)
    .bind(issue_number)
    .fetch_one(uow.connection())
    .await?;
    Ok(Some(state.0.0))
}

/// Build the same public task record for live events and frozen baselines.
async fn build_task_snapshot(
    uow: &mut UnitOfWork<'_>,
    state: &TaskState,
) -> anyhow::Result<TaskSnapshot> {
    let pins = state.pins.as_ref();
    let accepted_plan: Option<(String, Json<serde_json::Value>)> = sqlx::query_as(
        "SELECT plan_id,raw_report FROM tasks.plan_reports \
         WHERE task_id=$1 AND accepted AND current ORDER BY recorded_at DESC,attempt_id DESC LIMIT 1",
    )
    .bind(state.id.0)
    .fetch_optional(uow.connection())
    .await?;
    let enrollment: Option<(String, chrono::DateTime<Utc>)> = sqlx::query_as(
        "SELECT r.channel,r.source_at FROM tasks.start_pins p \
         JOIN tasks.requests r ON r.id=p.request_id WHERE p.task_id=$1",
    )
    .bind(state.id.0)
    .fetch_optional(uow.connection())
    .await?;
    let issue_source: Option<(Option<String>, Option<chrono::DateTime<Utc>>)> = sqlx::query_as(
        "SELECT COALESCE(i.snapshot #>> '{requirements,title}',x.body->>'title'), \
         x.source_at FROM tasks.task_state t \
         LEFT JOIN tasks.initialization_inputs i ON i.task_id=t.task_id \
         LEFT JOIN tasks.target_index x ON x.repository_id=t.repository_id \
             AND x.kind='issue' AND x.item_id=t.issue_number::text \
         WHERE t.task_id=$1",
    )
    .bind(state.id.0)
    .fetch_optional(uow.connection())
    .await?;
    let pull_request: Option<(String, i64, Json<serde_json::Value>)> = sqlx::query_as(
        "SELECT o.upstream,o.pr_number,p.intent FROM tasks.pr_ownership o \
         JOIN tasks.git_publications p ON p.task_id=o.owner_id \
         WHERE o.owner_kind='contribution' AND o.owner_id=$1 \
         ORDER BY p.recorded_at DESC,p.attempt_id DESC LIMIT 1",
    )
    .bind(state.id.0)
    .fetch_optional(uow.connection())
    .await?;
    let rejected_implementations: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM tasks.implementation_reports WHERE task_id=$1 AND status='rejected'",
    )
    .bind(state.id.0)
    .fetch_one(uow.connection())
    .await?;
    let revision_budget: Option<(i64, i64)> = sqlx::query_as(
        "SELECT spent_revisions,extra_plan_revisions FROM tasks.initialization_inputs WHERE task_id=$1",
    )
    .bind(state.id.0)
    .fetch_optional(uow.connection())
    .await?;
    let data = TaskSnapshot {
        task_id: state.id,
        issue: state.issue,
        kind: "contribution".to_owned(),
        author: state.author.clone(),
        title: issue_source.as_ref().and_then(|(title, _)| title.clone()),
        updated_at: issue_source
            .as_ref()
            .and_then(|(_, updated)| updated.map(Timestamp)),
        initialized: state.pins.is_some(),
        mode: if enrollment
            .as_ref()
            .is_some_and(|(channel, _)| channel == "label")
        {
            "autonomous"
        } else {
            "manual"
        }
        .to_owned(),
        stage: match state.stage {
            Stage::Initializing => Some("initializing".to_owned()),
            Stage::Intake => Some("intake".to_owned()),
            Stage::Plan => Some("plan".to_owned()),
            Stage::PlanQc => Some("plan_qc".to_owned()),
            Stage::Planned => Some("planned".to_owned()),
            Stage::Implement => Some("implement".to_owned()),
            Stage::Publish => Some("publish".to_owned()),
            Stage::Review => Some("review".to_owned()),
            Stage::Gates => Some("gates".to_owned()),
            Stage::Ready => Some("ready".to_owned()),
            Stage::ObservedMerged => Some("observed_merged".to_owned()),
            Stage::Closed => Some("closed".to_owned()),
            Stage::Clarify => Some("clarify".to_owned()),
            Stage::Rejected => Some("rejected".to_owned()),
            Stage::Escalated => Some("escalated".to_owned()),
            Stage::Blocked => Some("blocked".to_owned()),
            Stage::Stopped => Some("stopped".to_owned()),
        },
        stage_reason: state.stage_reason.clone(),
        enrollment: enrollment.map(
            |(channel, source_at)| serde_json::json!({"channel":channel,"source_at":source_at}),
        ),
        plan: accepted_plan
            .as_ref()
            .map(|(id, body)| serde_json::json!({"id":id,"body":body.0})),
        budgets: TaskBudgets {
            model_calls: TaskBudget {
                used: billable_model_calls(uow, state).await?,
                max: pins.map_or(0, |pins| pins.max_model_calls),
            },
            fix_rounds: TaskBudget {
                used: u64::try_from(rejected_implementations)?,
                max: pins.map_or(0, |pins| pins.max_fix_rounds),
            },
            plan_revisions: TaskBudget {
                used: u64::try_from(revision_budget.map_or(0, |(spent, _)| spent))?,
                max: pins.map_or(0, |pins| pins.max_plan_revisions)
                    + u64::try_from(revision_budget.map_or(0, |(_, extra)| extra))?,
            },
        },
        attention: state.stage == Stage::Blocked,
        pull_request: pull_request.map(|(upstream, number, Json(intent))| {
            serde_json::json!({
                "number":number,
                "url":format!("https://github.com/{upstream}/pull/{number}"),
                "title":intent["pr_title"],
                "branch":intent["branch"],
                "head_sha":intent["commit_sha"]
            })
        }),
    };
    Ok(data)
}

/// Append the complete task state in the transaction that changed its revision.
async fn append_task_snapshot(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    state: &TaskState,
) -> anyhow::Result<()> {
    let data = build_task_snapshot(uow, state).await?;
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
    sqlx::query(
        "UPDATE tasks.requests SET status=$2, reason=$3, task_id=$4, applied_at=now() WHERE id=$1",
    )
    .bind(request.id)
    .bind(stored)
    .bind(reason)
    .bind(task_id.map(|id| id.0))
    .execute(uow.connection())
    .await?;
    let outcome_status = match status {
        RequestStatus::Applied => RequestOutcomeStatus::Applied,
        RequestStatus::Refused => RequestOutcomeStatus::Refused,
        RequestStatus::Superseded => RequestOutcomeStatus::Superseded,
    };
    append_request_fact(uow, outbox, request, task_id, outcome_status, reason).await?;
    publication::queue_request_reply(uow, request, status, reason).await?;
    Ok(())
}

/// Append one immutable request step under the request's existing event family.
async fn append_request_fact(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    request: &Request,
    task_id: Option<TaskId>,
    status: RequestOutcomeStatus,
    reason: Option<&str>,
) -> anyhow::Result<()> {
    let command = request.kind.outcome_command();
    let step = match status {
        RequestOutcomeStatus::Applied
        | RequestOutcomeStatus::Refused
        | RequestOutcomeStatus::Superseded => None,
        RequestOutcomeStatus::Blocked => Some("blocked"),
        RequestOutcomeStatus::Stopped => Some("stopped"),
    };
    let event_id = Uuid::new_v5(
        &Uuid::NAMESPACE_OID,
        match step {
            Some(step) => format!("request-outcome:{}:{step}", request.id),
            None => format!("request-outcome:{}", request.id),
        }
        .as_bytes(),
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
            status,
            reason: reason.map(str::to_owned),
            reply: request.reply.clone(),
        },
    );
    outbox.append(uow, &event).await?;
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
    let previous_revision: Option<(bool,)> = sqlx::query_as(
        "SELECT deleted FROM tasks.comment_revisions WHERE comment_id=$1 AND source=$2",
    )
    .bind(revision.comment_id)
    .bind(&revision.order.source)
    .fetch_optional(uow.connection())
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
    if previous_revision.is_none_or(|(deleted,)| revision.deleted && !deleted) {
        let sources: Vec<(Json<Request>,)> =
            sqlx::query_as("SELECT request FROM tasks.requests WHERE comment_id=$1")
                .bind(revision.comment_id)
                .fetch_all(uow.connection())
                .await?;
        if sources
            .iter()
            .any(|(Json(request),)| !consent_facts(request).is_empty())
        {
            sqlx::query(
                "UPDATE tasks.issue_controls SET consent_revision=consent_revision+1 \
                 WHERE repository_id=$1 AND issue_number=$2",
            )
            .bind(revision.issue.repository_id)
            .bind(revision.issue.number)
            .execute(uow.connection())
            .await?;
        }
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
    sponsorship::reconcile_comment_revision(uow, revision, latest.map(String::as_str)).await?;
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

/// Refuse one definitively invalid source without leaving later commands queued.
pub async fn refuse_request(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    candidate: Uuid,
    issue: IssueKey,
    reason: &'static str,
) -> anyhow::Result<bool> {
    sqlx::query(
        "SELECT 1 FROM tasks.issue_controls WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    let pending: Option<(Uuid, Json<Request>)> = sqlx::query_as(NEXT_PENDING)
        .bind(issue.repository_id)
        .bind(issue.number)
        .fetch_optional(uow.connection())
        .await?;
    let Some((id, Json(request))) = pending else {
        return Ok(false);
    };
    if id != candidate {
        return Ok(false);
    }
    let task_id: Option<Uuid> = sqlx::query_scalar(
        "SELECT task_id FROM tasks.task_state WHERE repository_id=$1 AND issue_number=$2",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    finish_request(
        uow,
        outbox,
        &request,
        task_id.map(TaskId),
        RequestStatus::Refused,
        Some(reason),
    )
    .await?;
    Ok(true)
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

/// Read the latest pinned label Start for an autonomous task, if any.
pub async fn label_enrollment_request(
    database: &Database,
    task_id: Uuid,
) -> Result<Option<Request>, sqlx::Error> {
    let row: Option<(Json<Request>,)> = sqlx::query_as(
        "SELECT r.request FROM tasks.start_pins p \
         JOIN tasks.requests r ON r.id=p.request_id \
         WHERE p.task_id=$1 AND r.channel='label' AND r.status='applied'",
    )
    .bind(task_id)
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
        matches!(task.stage, Stage::Initializing | Stage::Clarify)
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
    apply_request_with_evidence(uow, outbox, candidate, issue, authority, None).await
}

/// Apply a source-ordered request with fresh evidence for recovery commands.
pub async fn apply_request_with_evidence(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    candidate: Uuid,
    issue: IssueKey,
    authority: Authority,
    evidence: Option<&CommandEvidence>,
) -> anyhow::Result<Option<(Request, Decision)>> {
    apply_request_with_evidence_mode(uow, outbox, candidate, issue, authority, evidence, false)
        .await
}

/// Apply a request with the selected exchange mode's withdrawal reporting.
pub async fn apply_request_with_evidence_mode(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    candidate: Uuid,
    issue: IssueKey,
    authority: Authority,
    evidence: Option<&CommandEvidence>,
    rust_exchange: bool,
) -> anyhow::Result<Option<(Request, Decision)>> {
    let control: Option<ApplyRequestWithEvidenceModeControlRow> = sqlx::query_as(
        "SELECT applied_stop,applied_lifecycle,applied_label FROM tasks.issue_controls WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    let Some((stop, lifecycle, label_cutoff)) = control else {
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
    let latest_lifecycle = if request.channel == "label" {
        lifecycle
            .as_ref()
            .map(|row| &row.0)
            .into_iter()
            .chain(label_cutoff.as_ref().map(|row| &row.0))
            .max()
    } else {
        lifecycle.as_ref().map(|row| &row.0)
    };
    let mut decision = decide(
        &request,
        current.as_ref().map(|row| &row.0.0),
        stop.as_ref().map(|row| &row.0),
        latest_lifecycle,
        authority,
    );
    let consent_command = !consent_facts(&request).is_empty();
    let pre_enrollment_consent = current.is_none() && consent_command;
    if pre_enrollment_consent {
        decision = if matches!(
            request.kind,
            RequestKind::Later(LaterCommand::Withdraw { .. })
        ) {
            Decision {
                status: RequestStatus::Applied,
                reason: None,
                task: None,
                stop_order: None,
            }
        } else if !authority.writer {
            Decision {
                status: RequestStatus::Refused,
                reason: Some("not_writer"),
                task: None,
                stop_order: None,
            }
        } else if !authority.issue_open {
            Decision {
                status: RequestStatus::Refused,
                reason: Some("issue_closed"),
                task: None,
                stop_order: None,
            }
        } else {
            Decision {
                status: RequestStatus::Applied,
                reason: None,
                task: None,
                stop_order: None,
            }
        };
        if decision.status == RequestStatus::Applied {
            sponsorship::record_consent(uow, &request).await?;
        }
    }
    let mut withdrawn_transitions = Vec::new();
    if let Some((Json(state),)) = current.as_ref()
        && matches!(
            request.kind,
            RequestKind::Later(LaterCommand::Withdraw { .. })
        )
    {
        decision = Decision {
            status: RequestStatus::Applied,
            reason: None,
            task: None,
            stop_order: None,
        };
        sponsorship::record_consent(uow, &request).await?;
        for fact in consent_facts(&request) {
            sponsorship::record_withdraw_transition(uow, state.id.0, &fact).await?;
            withdrawn_transitions
                .extend(sponsorship::pending_withdraw_transitions(uow, state.id.0, &fact).await?);
        }
    }
    let mut approval_binding = None;
    let mut clarification_start = false;
    let mut reset_from_pause = false;
    let mut spend_reassessment_revision = false;
    let mut resume_adoption: Option<recovery::ResumeAdoption> = None;
    if authority.writer
        && authority.issue_open
        && decision.status != RequestStatus::Superseded
        && request.kind == RequestKind::Start
        && let Some(current) = current.as_ref().map(|row| &row.0.0)
        && current.stage == Stage::Clarify
    {
        let planner_questions: bool = sqlx::query_scalar(
            "SELECT COALESCE(jsonb_array_length(p.raw_report->'questions')>0,false) \
             FROM tasks.plan_reports p WHERE p.task_id=$1 AND p.accepted AND p.current \
             ORDER BY p.recorded_at DESC,p.attempt_id DESC LIMIT 1",
        )
        .bind(current.id.0)
        .fetch_optional(uow.connection())
        .await?
        .unwrap_or(false);
        if planner_questions {
            clarification_start = true;
            decision = Decision {
                status: RequestStatus::Applied,
                reason: None,
                task: Some(TaskState {
                    revision: current.revision + 1,
                    stage_reason: Some("Checking answer to planner questions".to_owned()),
                    ..current.clone()
                }),
                stop_order: None,
            };
        }
    }
    if authority.writer
        && authority.issue_open
        && decision.status != RequestStatus::Superseded
        && request.kind == RequestKind::Start
        && !clarification_start
        && let Some(current) = current.as_ref().map(|row| &row.0.0)
        && matches!(current.stage, Stage::Blocked | Stage::Clarify)
    {
        let (spent, extra): (i64, i64) = sqlx::query_as(
            "SELECT spent_revisions,extra_plan_revisions FROM tasks.initialization_inputs WHERE task_id=$1",
        )
        .bind(current.id.0)
        .fetch_optional(uow.connection())
        .await?
        .unwrap_or_default();
        let spent = u64::try_from(spent)?;
        if current.pins.as_ref().is_some_and(|pins| {
            spent >= pins.max_plan_revisions + u64::try_from(extra).unwrap_or(0)
        }) {
            decision = Decision {
                status: RequestStatus::Refused,
                reason: Some("plan_revision_exhausted"),
                task: None,
                stop_order: None,
            };
        } else {
            reset_from_pause = true;
            spend_reassessment_revision = current.pins.is_some();
            decision = Decision {
                status: RequestStatus::Applied,
                reason: None,
                task: Some(TaskState {
                    stage: Stage::Initializing,
                    revision: current.revision + 1,
                    stage_reason: Some("Writer requested reassessment".to_owned()),
                    pins: None,
                    current_attempt: None,
                    ..current.clone()
                }),
                stop_order: None,
            };
        }
    }
    if request.kind == RequestKind::Start
        && decision.status == RequestStatus::Applied
        && !clarification_start
        && let Some(current) = current.as_ref().map(|row| &row.0.0)
    {
        let prepared: bool = sqlx::query_scalar(
            "SELECT EXISTS (SELECT 1 FROM tasks.git_publications WHERE task_id=$1) \
             OR EXISTS (SELECT 1 FROM tasks.implementation_reports \
                 WHERE task_id=$1 AND status='accepted')",
        )
        .bind(current.id.0)
        .fetch_one(uow.connection())
        .await?;
        if prepared {
            reset_from_pause = false;
            spend_reassessment_revision = false;
            decision = Decision {
                status: RequestStatus::Refused,
                reason: Some("publication_prepared"),
                task: None,
                stop_order: None,
            };
        }
    }
    if spend_reassessment_revision && decision.status == RequestStatus::Applied {
        sqlx::query(
            "UPDATE tasks.initialization_inputs SET spent_revisions=spent_revisions+1 \
             WHERE task_id=$1",
        )
        .bind(current.as_ref().expect("reassessment has task").0.0.id.0)
        .execute(uow.connection())
        .await?;
    }
    if reset_from_pause && decision.status == RequestStatus::Applied {
        cancel_attempts(
            uow,
            outbox,
            current.as_ref().expect("reassessment has task").0.0.id.0,
            None,
        )
        .await?;
    }
    if authority.writer
        && decision.status != RequestStatus::Superseded
        && let RequestKind::Later(LaterCommand::Approve { plan_id }) = &request.kind
    {
        decision = if let Some(current) = current.as_ref().map(|row| &row.0.0) {
            if current.stage != Stage::Planned {
                Decision {
                    status: RequestStatus::Refused,
                    reason: Some("not_planned"),
                    task: None,
                    stop_order: None,
                }
            } else if let Some((bound_plan, binding)) =
                design::approval_proof(uow, current.id.0).await?
            {
                if bound_plan == *plan_id {
                    approval_binding = Some(binding);
                    Decision {
                        status: RequestStatus::Applied,
                        reason: None,
                        task: Some(TaskState {
                            stage: Stage::Implement,
                            revision: current.revision + 1,
                            stage_reason: Some("Writer approved the reviewed plan".to_owned()),
                            ..current.clone()
                        }),
                        stop_order: None,
                    }
                } else {
                    Decision {
                        status: RequestStatus::Refused,
                        reason: Some("plan_id_mismatch"),
                        task: None,
                        stop_order: None,
                    }
                }
            } else {
                Decision {
                    status: RequestStatus::Refused,
                    reason: Some("design_consensus_missing"),
                    task: None,
                    stop_order: None,
                }
            }
        } else {
            Decision {
                status: RequestStatus::Refused,
                reason: Some("not_planned"),
                task: None,
                stop_order: None,
            }
        };
    }
    recovery::decide_recovery(
        uow,
        outbox,
        &request,
        current.as_ref().map(|row| &row.0.0),
        authority,
        evidence,
        RecoveryDecision {
            decision: &mut decision,
            resume_adoption: &mut resume_adoption,
        },
    )
    .await?;
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
    if let (Some(adoption), Some(task)) = (resume_adoption.as_ref(), decision.task.as_ref()) {
        let previous = &current.as_ref().expect("resume has current task").0.0;
        recovery::apply_resume_adoption(uow, outbox, &request, previous, task, adoption).await?;
    }
    if matches!(request.kind, concurro_tasks_core::RequestKind::Stop)
        && decision.status == RequestStatus::Applied
        && let Some(task) = current.as_ref()
    {
        cancel_attempts(uow, outbox, task.0.0.id.0, None).await?;
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
        if clarification_start {
            enqueue_clarification(uow, task, request.id).await?;
        }
        if task.stage == Stage::Implement && approval_binding.is_some() {
            enqueue_implementation(uow, task).await?;
        }
        recovery::enqueue_command_stage(
            uow,
            &request,
            current.as_ref().map(|row| &row.0.0),
            task,
            resume_adoption.is_some(),
        )
        .await?;
        if let Some(binding) = &approval_binding {
            let RequestKind::Later(LaterCommand::Approve { plan_id }) = &request.kind else {
                unreachable!("approval binding requires an approve command")
            };
            sqlx::query(
                "INSERT INTO tasks.plan_approvals (request_id,task_id,plan_id,binding,actor) \
                 VALUES ($1,$2,$3,$4,$5)",
            )
            .bind(request.id)
            .bind(task.id.0)
            .bind(plan_id)
            .bind(binding)
            .bind(Json(&request.actor))
            .execute(uow.connection())
            .await?;
            sqlx::query(
                "UPDATE tasks.initialization_inputs SET implementation_failure_count=0 \
                 WHERE task_id=$1",
            )
            .bind(task.id.0)
            .execute(uow.connection())
            .await?;
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
        && request.kind == RequestKind::Later(LaterCommand::Unenroll)
    {
        sqlx::query("UPDATE tasks.issue_controls SET applied_label=$3 WHERE repository_id=$1 AND issue_number=$2")
            .bind(issue.repository_id)
            .bind(issue.number)
            .bind(Json(&request.order))
            .execute(uow.connection())
            .await?;
    } else if decision.status == RequestStatus::Applied && !consent_command {
        sqlx::query("UPDATE tasks.issue_controls SET applied_lifecycle=$3 WHERE repository_id=$1 AND issue_number=$2")
            .bind(issue.repository_id)
            .bind(issue.number)
            .bind(Json(&request.order))
            .execute(uow.connection())
            .await?;
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
    if rust_exchange
        && decision.status == RequestStatus::Applied
        && matches!(
            request.kind,
            RequestKind::Later(LaterCommand::Withdraw { .. })
        )
    {
        withdrawal::record_withdrawal_blocked(
            uow,
            outbox,
            &request,
            decision
                .task
                .as_ref()
                .map(|task| task.id)
                .or_else(|| current.as_ref().map(|row| row.0.0.id)),
            &withdrawn_transitions,
            false,
        )
        .await?;
    }
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
    sqlx::query("UPDATE tasks.plan_reports SET current=false WHERE task_id=$1 AND current")
        .bind(initialized.id.0)
        .execute(uow.connection())
        .await?;
    sqlx::query(
        "UPDATE tasks.plan_review_batches SET status='superseded' \
         WHERE task_id=$1 AND status IN ('recorded','admitted','settled')",
    )
    .bind(initialized.id.0)
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
    admit_stage(uow, outbox, issue, spec, Stage::Intake, "intake").await
}

/// Charge and record one planner turn against its current task revision.
pub async fn admit_plan(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    issue: IssueKey,
    spec: &AttemptSpec,
) -> anyhow::Result<Admission> {
    admit_stage(uow, outbox, issue, spec, Stage::Plan, "plan").await
}

/// Admit one implementer only while its writer and design bindings remain current.
pub async fn admit_implementation(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    issue: IssueKey,
    spec: &AttemptSpec,
) -> anyhow::Result<Admission> {
    let approved: Option<(String, String)> = sqlx::query_as(
        "SELECT plan_id,binding FROM tasks.plan_approvals WHERE task_id=$1 \
         ORDER BY recorded_at DESC,request_id DESC LIMIT 1",
    )
    .bind(spec.task.0)
    .fetch_optional(uow.connection())
    .await?;
    if approved.is_none() || approved != design::approval_proof(uow, spec.task.0).await? {
        return Ok(Admission::Stale);
    }
    admit_stage(uow, outbox, issue, spec, Stage::Implement, "develop").await
}

/// Stop funding a task whose exact role journal cannot be restored.
async fn block_preflight(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    state: &mut TaskState,
    reason: &str,
    batch: Option<Uuid>,
) -> anyhow::Result<Admission> {
    state.retry_stage = Some(state.stage);
    state.stage = Stage::Blocked;
    state.stage_reason = Some(reason.to_owned());
    state.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(state.id.0)
        .bind(i64::try_from(state.revision)?)
        .bind(Json(&state))
        .execute(uow.connection())
        .await?;
    if let Some(batch) = batch {
        sqlx::query("UPDATE tasks.plan_review_batches SET status='superseded' WHERE id=$1")
            .bind(batch)
            .execute(uow.connection())
            .await?;
    }
    append_task_snapshot(uow, outbox, state).await?;
    Ok(Admission::RetainedUnavailable)
}

/// Record a permanent preflight refusal without charging a model turn.
pub async fn refuse_stage_preflight(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    task: Uuid,
    revision: u64,
    reason: &str,
) -> anyhow::Result<bool> {
    let row: Option<(Json<TaskState>,)> =
        sqlx::query_as("SELECT state FROM tasks.task_state WHERE task_id=$1 FOR UPDATE")
            .bind(task)
            .fetch_optional(uow.connection())
            .await?;
    let Some((Json(mut state),)) = row else {
        return Ok(false);
    };
    if state.revision != revision
        || !matches!(
            state.stage,
            Stage::Intake | Stage::Plan | Stage::PlanQc | Stage::Implement | Stage::Review
        )
    {
        return Ok(false);
    }
    block_preflight(uow, outbox, &mut state, reason, None).await?;
    sqlx::query(
        "UPDATE tasks.plan_review_batches SET status='superseded' \
         WHERE task_id=$1 AND status IN ('recorded','admitted')",
    )
    .bind(task)
    .execute(uow.connection())
    .await?;
    sqlx::query(
        "UPDATE tasks.pr_review_batches SET status='superseded' \
         WHERE task_id=$1 AND status IN ('recorded','admitted')",
    )
    .bind(task)
    .execute(uow.connection())
    .await?;
    Ok(true)
}

/// Apply the shared call budget, staffing and exact-body admission boundary.
async fn admit_stage(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    issue: IssueKey,
    spec: &AttemptSpec,
    stage: Stage,
    operation: &str,
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
    let existing: Option<AttemptIdentityRow> = sqlx::query_as(
        "SELECT task_id, operation, expected_revision, staffing_group, fork, config_sha, submit_body, prompt_body, initial_deadline, unbounded_running \
         FROM tasks.attempts WHERE id=$1",
    )
    .bind(spec.id)
    .fetch_optional(uow.connection())
    .await?;
    if let Some(AttemptIdentityRow {
        task_id,
        operation: stored_operation,
        expected_revision: revision,
        staffing_group: group,
        fork,
        config_sha,
        submit_body: body,
        prompt_body,
        initial_deadline: deadline,
        unbounded_running,
    }) = existing
    {
        anyhow::ensure!(
            task_id == spec.task.0
                && stored_operation == operation
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
    if current.stage != stage || current.current_attempt.is_some() {
        return Ok(Admission::Stale);
    }
    validate_funding_admission(uow, &current, spec).await?;
    let retained = check_retained_attempt(uow, spec).await?;
    if let RetainedCheck::Refused(reason) = retained {
        return block_preflight(
            uow,
            outbox,
            &mut current,
            &format!("Retained continuation unavailable: {reason}"),
            None,
        )
        .await;
    }
    if matches!(retained, RetainedCheck::Replay) {
        return Ok(Admission::Existing);
    }
    let Some(pins) = &current.pins else {
        return Ok(Admission::Stale);
    };
    if billable_model_calls(uow, &current).await? >= pins.max_model_calls {
        current.retry_stage = Some(current.stage);
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
    let (retained_scope, retained_turn, retained_request) = match &retained {
        RetainedCheck::Eligible {
            scope,
            turn,
            digest,
        } => (Some(scope.as_str()), Some(*turn), Some(digest.as_str())),
        _ => (None, None, None),
    };
    sqlx::query(
        "INSERT INTO tasks.attempts (id, task_id, operation, expected_revision, staffing_group, fork, config_sha, submit_body, prompt_body, initial_deadline, deadline, unbounded_running, status, retained_scope, retained_turn, retained_request) \
         VALUES ($1, $2, $11, $3, $4, $5, $6, $7, $8, $9, $9, $10, 'ready', $12, $13, $14)",
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
    .bind(operation)
    .bind(retained_scope)
    .bind(retained_turn)
    .bind(retained_request)
    .execute(uow.connection())
    .await?;
    record_funding_grant(uow, spec).await?;
    events::append_turn_submitted(uow, outbox, current.id, spec, operation).await?;
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
