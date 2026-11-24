//! Durable PR review batches bound to a published head and individual reviewer identities.

use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Result, ensure};
use concurro_db::{Database, UnitOfWork};
use concurro_events_postgres::Outbox;
use concurro_exchange_v1::TurnSubmission;
use concurro_tasks_core::{
    Admission, AttemptSpec, DesignReviewer, InitializationSnapshot, IssueKey, PrReviewReport,
    Stage, TaskState, pr_review_binding, pr_review_body, pr_review_roster, python_digest,
};
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

use crate::{
    append_task_snapshot,
    archive::{RetainedCheck, check_retained_attempt},
    effects::{definite_session_failure, operational_json},
    enqueue_effect, enqueue_pr_review_correction, events,
    git_publication::GitPublicationIntent,
    sponsorship::{
        billable_model_calls, has_credential_exclusion, record_funding_grant,
        validate_funding_admission,
    },
};

/// Columns read by `pr_review_publication`.
#[derive(sqlx::FromRow)]
struct PrReviewPublicationRow {
    /// Persisted repository id.
    repository_id: i64,
    /// Persisted issue number.
    issue_number: i64,
    /// Persisted batch id.
    batch_id: Uuid,
    /// Persisted pr number.
    pr_number: i64,
    /// Persisted head.
    head: String,
    /// Persisted branch.
    branch: String,
    /// Persisted marker.
    marker: String,
    /// Persisted body.
    body: String,
    /// Persisted status.
    status: String,
    /// Persisted bot user id.
    bot_user_id: Option<i64>,
    /// Persisted review node id.
    review_node_id: Option<String>,
    /// Persisted threaded.
    threaded: bool,
}

/// Columns read by `pr_review_batch`.
#[derive(sqlx::FromRow)]
struct PrReviewBatchRow {
    /// Persisted task id.
    task_id: Uuid,
    /// Persisted repository id.
    repository_id: i64,
    /// Persisted issue number.
    issue_number: i64,
    /// Persisted pr number.
    pr_number: i64,
    /// Persisted head.
    head: String,
    /// Persisted branch.
    branch: String,
    /// Persisted plan id.
    plan_id: String,
    /// Persisted round.
    round: i64,
    /// Persisted fix round.
    fix_round: i64,
    /// Persisted changed paths.
    changed_paths: Json<Vec<String>>,
    /// Persisted roster.
    roster: Json<Vec<DesignReviewer>>,
    /// Persisted snapshot.
    snapshot: Json<InitializationSnapshot>,
    /// Persisted plan report.
    plan_report: Vec<u8>,
}

/// Columns read by `pr_review_result`.
#[derive(sqlx::FromRow)]
struct PrReviewResultRow {
    /// Persisted batch id.
    batch_id: Uuid,
    /// Persisted task id.
    task_id: Uuid,
    /// Persisted repository id.
    repository_id: i64,
    /// Persisted issue number.
    issue_number: i64,
    /// Persisted pr number.
    pr_number: i64,
    /// Persisted head.
    head: String,
    /// Persisted changed paths.
    changed_paths: Json<Vec<String>>,
    /// Persisted reviewer.
    reviewer: Json<DesignReviewer>,
    /// Persisted report raw.
    report_raw: Vec<u8>,
}

/// Columns read by `settle_pr_review_attempt`.
type SettlePrReviewAttemptRow = (
    String,
    Option<Json<Value>>,
    Option<Vec<u8>>,
    Option<String>,
    Vec<u8>,
);

/// Columns read by `apply_pr_review_result`.
#[derive(sqlx::FromRow)]
struct PrReviewSettlementRow {
    /// Persisted batch id.
    batch_id: Uuid,
    /// Persisted head.
    head: String,
    /// Persisted plan id.
    plan_id: String,
    /// Persisted reviewer.
    reviewer: Json<DesignReviewer>,
    /// Persisted report raw.
    report_raw: Vec<u8>,
    /// Persisted snapshot.
    snapshot: Json<InitializationSnapshot>,
    /// Persisted plan report.
    plan_report: Vec<u8>,
    /// Persisted binding.
    binding: String,
    /// Persisted correction count.
    correction_count: i32,
    /// Persisted repair signature.
    repair_signature: Option<String>,
}

/// One immutable pull request head and pinned independent reviewer roster.
pub struct PrReviewBatch {
    /// Durable batch identity.
    pub id: Uuid,
    /// Owning task identity.
    pub task_id: Uuid,
    /// Source issue for the batch.
    pub issue: IssueKey,
    /// App-authored upstream pull request.
    pub pr_number: i64,
    /// Exact reviewed commit.
    pub head: String,
    /// Task-owned fork branch at this head.
    pub branch: String,
    /// Current writer-approved plan identity.
    pub plan_id: String,
    /// Zero-based implementation review round.
    pub round: i64,
    /// Number of requested fix cycles before this batch.
    pub fix_round: i64,
    /// Paths verified against GitHub's PR file inventory.
    pub changed_paths: Vec<String>,
    /// Pinned reviewer identities in roster order.
    pub roster: Vec<DesignReviewer>,
    /// Accepted plan supplied to every reviewer.
    pub plan: Value,
    /// Pinned task configuration and requirements.
    pub snapshot: InitializationSnapshot,
    /// Earlier settled assessments supplied as untrusted context in later rounds.
    pub prior_outcomes: Vec<Value>,
    /// Still-open owner findings by reviewer identity.
    pub own_findings: BTreeMap<String, Vec<Value>>,
    /// Durable private session generation by reviewer identity.
    pub session_epochs: BTreeMap<String, i32>,
}

/// One completed reviewer response awaiting source-aware validation.
pub struct PrReviewResult {
    /// Batch that owns this immutable review head.
    pub batch_id: Uuid,
    /// Task and issue owning the response.
    pub task_id: Uuid,
    /// Source issue for the PR.
    pub issue: IssueKey,
    /// App-authored pull request being reviewed.
    pub pr_number: i64,
    /// Exact reviewed commit.
    pub head: String,
    /// Paths frozen for the review batch.
    pub changed_paths: Vec<String>,
    /// Reviewer identity bound at admission.
    pub reviewer: DesignReviewer,
    /// Original response JSON, before PostgreSQL-safe projection.
    pub report: Value,
    /// Previously open finding IDs owned by this reviewer.
    pub open_ids: Vec<String>,
    /// Withdrawn finding IDs owned by this reviewer.
    pub withdrawn_ids: Vec<String>,
}

/// One owner's invalid report or failed session awaiting a private replacement turn.
pub struct PrReviewCorrection {
    /// Immutable published source and reviewer roster.
    pub batch: PrReviewBatch,
    /// Pinned reviewer whose seat is being replaced.
    pub reviewer: DesignReviewer,
    /// Original invalid report, absent after a provider failure.
    pub report: Value,
    /// Controller validation or provider feedback.
    pub reason: String,
    /// One-based correction or failure count.
    pub count: i32,
    /// Whether this is a provider-session restart.
    pub retry: bool,
}

/// Frozen native GitHub review write and its send/readback fence.
pub struct PrReviewPublication {
    /// Reviewer turn that produced the immutable body.
    pub turn: Uuid,
    /// Task issue that owns the PR.
    pub issue: IssueKey,
    /// Review batch to block on an external conflict.
    pub batch_id: Uuid,
    /// Upstream PR number.
    pub pr_number: i64,
    /// Exact reviewed commit.
    pub head: String,
    /// Task-owned fork branch for this head.
    pub branch: String,
    /// Unique marker at the start of the frozen body.
    pub marker: String,
    /// Entire frozen review body.
    pub body: String,
    /// `recorded` may send once; `sent` permits only readback.
    pub status: String,
    /// Public App account ID pinned before sending.
    pub bot_user_id: Option<i64>,
    /// GraphQL node ID of an App-owned pending review.
    pub review_node_id: Option<String>,
    /// Whether this publication has native thread or reply actions.
    pub threaded: bool,
}

/// Read a pending native review publication without advancing its write fence.
pub async fn pr_review_publication(
    database: &Database,
    turn: Uuid,
) -> Result<Option<PrReviewPublication>> {
    let row: Option<PrReviewPublicationRow> = sqlx::query_as(
        "SELECT t.repository_id, t.issue_number, p.batch_id, p.pr_number, p.head, b.branch, p.marker, p.body, p.status, p.bot_user_id, p.review_node_id, EXISTS (SELECT 1 FROM tasks.pr_review_actions a WHERE a.turn_id=p.turn_id) AS threaded \
         FROM tasks.pr_review_publications p \
         JOIN tasks.pr_review_batches b ON b.id=p.batch_id \
         JOIN tasks.task_state t ON t.task_id=p.task_id WHERE p.turn_id=$1 \
         AND p.status IN ('recorded','sent')",
    )
    .bind(turn)
    .fetch_optional(database.pool())
    .await?;
    Ok(row.map(
        |PrReviewPublicationRow {
             repository_id,
             issue_number: number,
             batch_id,
             pr_number,
             head,
             branch,
             marker,
             body,
             status,
             bot_user_id,
             review_node_id,
             threaded,
         }| {
            PrReviewPublication {
                turn,
                issue: IssueKey {
                    repository_id,
                    number,
                },
                batch_id,
                pr_number,
                head,
                branch,
                marker,
                body,
                status,
                bot_user_id,
                review_node_id,
                threaded,
            }
        },
    ))
}

/// Fence all later retries to readback before the first native review mutation.
pub async fn mark_pr_review_sent(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    bot_user_id: i64,
) -> Result<bool> {
    let state = crate::lock_issue_for_turn(uow, turn).await?;
    if state
        .as_ref()
        .is_none_or(|state| state.stage != Stage::Review)
    {
        return Ok(false);
    }
    let changed = sqlx::query(
        "UPDATE tasks.pr_review_publications SET status='sent',bot_user_id=$2 \
         WHERE turn_id=$1 AND status='recorded'",
    )
    .bind(turn)
    .bind(bot_user_id)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Release a send fence after GitHub proves a retryable review POST did not apply.
pub async fn retry_pr_review_publication(uow: &mut UnitOfWork<'_>, turn: Uuid) -> Result<bool> {
    let changed = sqlx::query(
        "UPDATE tasks.pr_review_publications SET status='recorded' \
         WHERE turn_id=$1 AND status='sent'",
    )
    .bind(turn)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Confirm one App-authored review only after its exact body and head are visible.
pub async fn confirm_pr_review_publication(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    turn: Uuid,
    review_id: i64,
) -> Result<bool> {
    ensure!(review_id > 0, "GitHub review ID is invalid");
    let action_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM tasks.pr_review_actions WHERE turn_id=$1")
            .bind(turn)
            .fetch_one(uow.connection())
            .await?;
    if action_count > 0 && !crate::pr_review_actions::pr_review_actions_complete(uow, turn).await? {
        return Ok(false);
    }
    let Some(state) = crate::lock_issue_for_turn(uow, turn).await? else {
        return Ok(false);
    };
    let batch: Option<Uuid> =
        sqlx::query_scalar("SELECT batch_id FROM tasks.pr_review_publications WHERE turn_id=$1")
            .bind(turn)
            .fetch_optional(uow.connection())
            .await?;
    let Some(batch) = batch else { return Ok(false) };
    let changed = sqlx::query(
        "UPDATE tasks.pr_review_publications SET status='confirmed',review_id=$2 \
         WHERE turn_id=$1 AND status='sent'",
    )
    .bind(turn)
    .bind(review_id)
    .execute(uow.connection())
    .await?;
    if changed.rows_affected() == 0 {
        return Ok(false);
    }
    if state.stage != Stage::Review {
        return Ok(true);
    }
    let roster: Option<(Json<Vec<DesignReviewer>>, i64)> = sqlx::query_as(
        "SELECT roster,fix_round FROM tasks.pr_review_batches WHERE id=$1 AND task_id=$2 \
         AND status='admitted'",
    )
    .bind(batch)
    .bind(state.id.0)
    .fetch_optional(uow.connection())
    .await?;
    let Some((Json(roster), fix_round)) = roster else {
        return Ok(true);
    };
    let seats: Vec<(String, Option<Json<PrReviewReport>>, Option<String>)> = sqlx::query_as(
        "SELECT s.status,s.report,p.status FROM tasks.pr_review_seats s \
         LEFT JOIN tasks.pr_review_publications p ON p.turn_id=s.attempt_id \
         WHERE s.batch_id=$1 ORDER BY s.reviewer_id",
    )
    .bind(batch)
    .fetch_all(uow.connection())
    .await?;
    if seats.len() != roster.len()
        || seats.iter().any(|(status, _, publication)| {
            status != "accepted" || publication.as_deref() != Some("confirmed")
        })
    {
        return Ok(true);
    }
    let approved = seats.iter().all(|(_, report, _)| {
        report
            .as_ref()
            .is_some_and(|report| report.0.verdict == "approve")
    });
    let reopened = seats.iter().any(|(_, report, _)| {
        report
            .as_ref()
            .is_some_and(|report| report.0.verdict == "reopen")
    });
    let mut next = state.clone();
    next.revision += 1;
    if approved {
        next.stage = Stage::Gates;
        next.stage_reason = Some("Independent PR reviews approved the published head".to_owned());
        crate::enqueue_gates(uow, &next).await?;
    } else if reopened {
        sqlx::query(
            "UPDATE tasks.pr_review_findings SET status='withdrawn' \
             WHERE task_id=$1 AND status='open'",
        )
        .bind(state.id.0)
        .execute(uow.connection())
        .await?;
        let (spent, extra_revisions, design_reopens, Json(snapshot)): (
            i64,
            i64,
            i64,
            Json<InitializationSnapshot>,
        ) = sqlx::query_as(
            "SELECT spent_revisions,extra_plan_revisions,spent_design_reopens,snapshot \
             FROM tasks.initialization_inputs WHERE task_id=$1 FOR UPDATE",
        )
        .bind(state.id.0)
        .fetch_one(uow.connection())
        .await?;
        let max_design_reopens = snapshot.configuration["max_design_reopens"]
            .as_u64()
            .unwrap_or(0);
        if u64::try_from(design_reopens)? >= max_design_reopens {
            next.stage = Stage::Escalated;
            next.stage_reason = Some("PR review design reopen allowance exhausted".to_owned());
        } else {
            sqlx::query("UPDATE tasks.initialization_inputs SET spent_design_reopens=spent_design_reopens+1 WHERE task_id=$1")
                .bind(state.id.0).execute(uow.connection()).await?;
            if u64::try_from(spent)?
                >= state.pins.as_ref().expect("review pins").max_plan_revisions
                    + u64::try_from(extra_revisions)?
            {
                next.stage = Stage::Escalated;
                next.stage_reason = Some(
                    "PR review reopened design after plan revision budget was spent".to_owned(),
                );
            } else {
                sqlx::query("UPDATE tasks.initialization_inputs SET spent_revisions=spent_revisions+1 WHERE task_id=$1")
                    .bind(state.id.0).execute(uow.connection()).await?;
                next.stage = Stage::Plan;
                next.stage_reason =
                    Some("PR review supplied evidence to reopen the design".to_owned());
                sqlx::query(
                    "UPDATE tasks.plan_reports SET current=false WHERE task_id=$1 AND current",
                )
                .bind(state.id.0)
                .execute(uow.connection())
                .await?;
                crate::enqueue_plan(uow, &next).await?;
            }
        }
    } else if u64::try_from(fix_round)? < state.pins.as_ref().expect("review pins").max_fix_rounds {
        next.stage = Stage::Implement;
        next.stage_reason = Some("Independent PR reviews requested a fix round".to_owned());
        crate::enqueue_implementation(uow, &next).await?;
    } else {
        next.retry_stage = Some(Stage::Implement);
        next.stage = Stage::Blocked;
        next.stage_reason = Some("PR review fix-round allowance exhausted".to_owned());
    }
    sqlx::query("UPDATE tasks.pr_review_batches SET status='settled' WHERE id=$1")
        .bind(batch)
        .execute(uow.connection())
        .await?;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(state.id.0)
        .bind(i64::try_from(next.revision)?)
        .bind(Json(&next))
        .execute(uow.connection())
        .await?;
    append_task_snapshot(uow, outbox, &next).await?;
    Ok(true)
}

/// Record a definite GitHub refusal without resending the frozen review.
pub async fn fail_pr_review_publication(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    publication: &PrReviewPublication,
    reason: &str,
) -> Result<bool> {
    block_pr_review_batch(
        uow,
        outbox,
        publication.issue,
        publication.batch_id,
        &format!("PR review publication refused: {reason}"),
    )
    .await?;
    let changed = sqlx::query(
        "UPDATE tasks.pr_review_publications SET status='failed',error=$2 \
         WHERE turn_id=$1 AND status='sent'",
    )
    .bind(publication.turn)
    .bind(reason)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Keep readback active after the worker's retry budget is exhausted.
pub async fn pending_pr_review_readbacks(database: &Database) -> Result<Vec<Uuid>> {
    Ok(sqlx::query_scalar(
        "SELECT turn_id FROM tasks.pr_review_publications p WHERE p.status='sent' \
         OR (p.status='recorded' AND EXISTS (SELECT 1 FROM tasks.pr_review_actions a \
             WHERE a.turn_id=p.turn_id AND a.status='sent')) \
         ORDER BY recorded_at,turn_id",
    )
    .fetch_all(database.pool())
    .await?)
}

/// Freeze a review batch when the approved PR head and file inventory are confirmed.
pub(crate) async fn record_pr_review_batch(
    uow: &mut UnitOfWork<'_>,
    state: &TaskState,
    turn: Uuid,
    pr_number: i64,
    intent: &GitPublicationIntent,
    changed_paths: &[String],
) -> Result<Uuid> {
    ensure!(
        state.stage == Stage::Review,
        "PR review stage is not current"
    );
    let head = intent
        .commit_sha
        .as_deref()
        .ok_or_else(|| anyhow::anyhow!("published PR head is missing"))?;
    let row: (Json<InitializationSnapshot>, Vec<u8>, Json<Value>) = sqlx::query_as(
        "SELECT i.snapshot,p.raw_report_bytes,r.candidate FROM tasks.implementation_reports r \
         JOIN tasks.initialization_inputs i ON i.task_id=r.task_id \
         JOIN tasks.plan_reports p ON p.task_id=r.task_id AND p.accepted AND p.current \
             AND p.plan_id=r.candidate->>'plan_id' \
         WHERE r.attempt_id=$1 AND r.task_id=$2 AND r.status='accepted'",
    )
    .bind(turn)
    .bind(state.id.0)
    .fetch_one(uow.connection())
    .await?;
    let (Json(snapshot), plan_bytes, Json(candidate)) = row;
    let plan: Value = serde_json::from_slice(&plan_bytes)?;
    let plan_id = candidate["plan_id"]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("review plan identity is missing"))?;
    let mut changed_paths = changed_paths.to_vec();
    changed_paths.sort();
    changed_paths.dedup();
    let roster = pr_review_roster(&snapshot.configuration).map_err(anyhow::Error::msg)?;
    let round: i64 = sqlx::query_scalar(
        "SELECT COALESCE(max(round)+1,0) FROM tasks.pr_review_batches WHERE task_id=$1",
    )
    .bind(state.id.0)
    .fetch_one(uow.connection())
    .await?;
    let fix_round: Option<i64> = sqlx::query_scalar(
        "SELECT b.fix_round + CASE WHEN EXISTS (SELECT 1 FROM tasks.pr_review_seats s \
             WHERE s.batch_id=b.id AND s.report->>'verdict'='changes_requested') \
             AND NOT EXISTS (SELECT 1 FROM tasks.pr_review_seats s \
             WHERE s.batch_id=b.id AND s.report->>'verdict'='reopen') \
             THEN 1 ELSE 0 END \
         FROM tasks.pr_review_batches b WHERE b.task_id=$1 AND b.status='settled' \
         ORDER BY b.round DESC,b.id DESC LIMIT 1",
    )
    .bind(state.id.0)
    .fetch_optional(uow.connection())
    .await?;
    let batch = Uuid::now_v7();
    sqlx::query(
        "INSERT INTO tasks.pr_review_batches \
         (id,task_id,publication_attempt,pr_number,head,branch,plan_id,round,fix_round,changed_paths,roster) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
    )
    .bind(batch)
    .bind(state.id.0)
    .bind(turn)
    .bind(pr_number)
    .bind(head)
    .bind(&intent.branch)
    .bind(plan_id)
    .bind(round)
    .bind(fix_round.unwrap_or(0))
    .bind(Json(&changed_paths))
    .bind(Json(&roster))
    .execute(uow.connection())
    .await?;
    for reviewer in &roster {
        sqlx::query("INSERT INTO tasks.pr_review_sessions (task_id,reviewer_id) VALUES ($1,$2) ON CONFLICT DO NOTHING")
            .bind(state.id.0).bind(&reviewer.id).execute(uow.connection()).await?;
        let binding = pr_review_binding(&snapshot, &plan, plan_id, head, reviewer)
            .map_err(anyhow::Error::msg)?;
        sqlx::query(
            "INSERT INTO tasks.pr_review_seats \
             (batch_id,reviewer_id,identity,binding,session_epoch) VALUES ($1,$2,$3,$4, \
             COALESCE((SELECT session_epoch FROM tasks.pr_review_sessions \
               WHERE task_id=$5 AND reviewer_id=$2),0))",
        )
        .bind(batch)
        .bind(&reviewer.id)
        .bind(Json(reviewer))
        .bind(binding)
        .bind(state.id.0)
        .execute(uow.connection())
        .await?;
    }
    enqueue_pr_review_batch(uow, state, batch).await?;
    Ok(batch)
}

/// Requeue one recorded PR review batch at the current task revision.
async fn enqueue_pr_review_batch(
    uow: &mut UnitOfWork<'_>,
    state: &TaskState,
    batch: Uuid,
) -> Result<()> {
    let queue = format!("task-pr-review:{}", state.id.0);
    let key = format!("task-pr-review:{batch}");
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'task_pr_review_batch', \
         payload => json_build_object('repository_id',$1,'issue_number',$2,'revision',$3,'batch',$4::text), \
         queue_name => $5,max_attempts => 25,job_key => $6)).id",
    )
    .bind(state.issue.repository_id)
    .bind(state.issue.number)
    .bind(i64::try_from(state.revision)?)
    .bind(batch)
    .bind(queue)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Requeue source validation of one completed PR reviewer response.
async fn enqueue_pr_review_result(uow: &mut UnitOfWork<'_>, task: Uuid, turn: Uuid) -> Result<()> {
    let key = format!("task-pr-review-result:{turn}");
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job(identifier => 'task_pr_review_result', \
         payload => json_build_object('turn',$1::text), \
         queue_name => $2,max_attempts => 25,job_key => $3)).id",
    )
    .bind(turn)
    .bind(format!("task-pr-review:{task}"))
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Requeue exact readback or a safely reopened native review write.
pub(crate) async fn enqueue_pr_review_publish(
    uow: &mut UnitOfWork<'_>,
    task: Uuid,
    turn: Uuid,
) -> Result<()> {
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job(identifier => 'task_pr_review_publish', \
         payload => json_build_object('turn',$1::text), \
         queue_name => $2,max_attempts => 25,job_key => $3)).id",
    )
    .bind(turn)
    .bind(format!("task-pr-review:{task}"))
    .bind(format!("task-pr-review-publish:{turn}"))
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Renew a paused PR review while keeping accepted sibling reports and send fences.
pub(crate) async fn retry_pr_review_batch(
    uow: &mut UnitOfWork<'_>,
    task: &TaskState,
) -> Result<bool> {
    let batch: Option<(Uuid, String)> = sqlx::query_as(
        "SELECT id,status FROM tasks.pr_review_batches WHERE task_id=$1 \
         AND status IN ('recorded','admitted','superseded') \
         ORDER BY round DESC,id DESC LIMIT 1 FOR UPDATE",
    )
    .bind(task.id.0)
    .fetch_optional(uow.connection())
    .await?;
    let Some((batch, status)) = batch else {
        return Ok(false);
    };
    if status == "recorded" {
        enqueue_pr_review_batch(uow, task, batch).await?;
        return Ok(true);
    }
    let seats: Vec<(String, String, Option<Uuid>, bool)> = sqlx::query_as(
        "SELECT reviewer_id,status,attempt_id,report_raw IS NOT NULL \
         FROM tasks.pr_review_seats WHERE batch_id=$1 ORDER BY reviewer_id FOR UPDATE",
    )
    .bind(batch)
    .fetch_all(uow.connection())
    .await?;
    let mut renewed = false;
    for (reviewer, status, attempt, has_report) in seats {
        match status.as_str() {
            "failed" | "correcting" | "retrying" => {
                let next = if has_report { "correcting" } else { "retrying" };
                sqlx::query(
                    "UPDATE tasks.pr_review_seats SET status=$3,correction_count=0, \
                     failure_count=0,session_failure_count=0,repair_signature=NULL \
                     WHERE batch_id=$1 AND reviewer_id=$2",
                )
                .bind(batch)
                .bind(&reviewer)
                .bind(next)
                .execute(uow.connection())
                .await?;
                crate::enqueue_pr_review_correction(uow, batch, &reviewer, 0).await?;
                renewed = true;
            }
            "completed" => {
                if let Some(turn) = attempt {
                    enqueue_pr_review_result(uow, task.id.0, turn).await?;
                    renewed = true;
                }
            }
            _ => {}
        }
    }
    let writes: Vec<(Uuid, String)> = sqlx::query_as(
        "SELECT turn_id,status FROM tasks.pr_review_publications WHERE batch_id=$1 \
         AND status IN ('recorded','sent','failed') ORDER BY turn_id FOR UPDATE",
    )
    .bind(batch)
    .fetch_all(uow.connection())
    .await?;
    for (turn, status) in writes {
        if status == "failed" {
            sqlx::query(
                "UPDATE tasks.pr_review_actions SET status='recorded' \
                 WHERE turn_id=$1 AND status='failed'",
            )
            .bind(turn)
            .execute(uow.connection())
            .await?;
            sqlx::query(
                "UPDATE tasks.pr_review_publications SET status='recorded',error=NULL \
                 WHERE turn_id=$1 AND status='failed'",
            )
            .bind(turn)
            .execute(uow.connection())
            .await?;
        }
        enqueue_pr_review_publish(uow, task.id.0, turn).await?;
        renewed = true;
    }
    if renewed && status == "superseded" {
        sqlx::query("UPDATE tasks.pr_review_batches SET status='admitted' WHERE id=$1")
            .bind(batch)
            .execute(uow.connection())
            .await?;
    }
    Ok(renewed)
}

/// Read one recorded batch and its exact approved source without consuming calls.
pub async fn pr_review_batch(database: &Database, batch: Uuid) -> Result<Option<PrReviewBatch>> {
    let row: Option<PrReviewBatchRow> = sqlx::query_as(
        "SELECT b.task_id, t.repository_id, t.issue_number, b.pr_number, b.head, b.branch, b.plan_id, b.round, b.fix_round, b.changed_paths, b.roster, i.snapshot, p.raw_report_bytes AS plan_report \
         FROM tasks.pr_review_batches b JOIN tasks.task_state t ON t.task_id=b.task_id \
         JOIN tasks.initialization_inputs i ON i.task_id=b.task_id \
         JOIN tasks.plan_reports p ON p.task_id=b.task_id AND p.plan_id=b.plan_id \
             AND p.accepted AND p.current \
         WHERE b.id=$1 AND b.status IN ('recorded','admitted') \
         ORDER BY p.recorded_at DESC,p.attempt_id DESC LIMIT 1",
    )
    .bind(batch)
    .fetch_optional(database.pool())
    .await?;
    let batch = row
        .map(
            |PrReviewBatchRow {
                 task_id,
                 repository_id,
                 issue_number: number,
                 pr_number,
                 head,
                 branch,
                 plan_id,
                 round,
                 fix_round,
                 changed_paths: Json(changed_paths),
                 roster: Json(roster),
                 snapshot: Json(snapshot),
                 plan_report: raw,
             }|
             -> Result<PrReviewBatch> {
                Ok(PrReviewBatch {
                    id: batch,
                    task_id,
                    issue: IssueKey {
                        repository_id,
                        number,
                    },
                    pr_number,
                    head,
                    branch,
                    plan_id,
                    round,
                    fix_round,
                    changed_paths,
                    roster,
                    plan: serde_json::from_slice(&raw)?,
                    snapshot,
                    prior_outcomes: Vec::new(),
                    own_findings: BTreeMap::new(),
                    session_epochs: BTreeMap::new(),
                })
            },
        )
        .transpose()?;
    let Some(mut batch) = batch else {
        return Ok(None);
    };
    let prior: Vec<(String, Json<Value>)> = sqlx::query_as(
        "SELECT s.reviewer_id,s.report FROM tasks.pr_review_seats s \
         JOIN tasks.pr_review_batches b ON b.id=s.batch_id \
         WHERE b.task_id=$1 AND b.round<$2 AND b.status='settled' AND s.status='accepted' \
         ORDER BY b.round,s.reviewer_id",
    )
    .bind(batch.task_id)
    .bind(batch.round)
    .fetch_all(database.pool())
    .await?;
    batch.prior_outcomes = prior.into_iter().map(|(reviewer_id, Json(report))| {
        serde_json::json!({"reviewer_id":reviewer_id,"report":report})
    }).collect();
    let findings: Vec<(String, Json<Value>)> = sqlx::query_as(
        "SELECT reviewer_id,jsonb_build_object('id',id,'status',status, \
         'path',finding->'path','start_line',finding->'start_line', \
         'line',finding->'line','detail',finding->'detail') \
         FROM tasks.pr_review_findings WHERE task_id=$1 \
         AND status IN ('open','withdrawn') ORDER BY sequence",
    )
    .bind(batch.task_id)
    .fetch_all(database.pool())
    .await?;
    for (reviewer_id, Json(finding)) in findings {
        batch
            .own_findings
            .entry(reviewer_id)
            .or_default()
            .push(finding);
    }
    batch.session_epochs = sqlx::query_as::<_, (String, i32)>(
        "SELECT reviewer_id,session_epoch FROM tasks.pr_review_sessions WHERE task_id=$1",
    )
    .bind(batch.task_id)
    .fetch_all(database.pool())
    .await?
    .into_iter()
    .collect();
    Ok(Some(batch))
}

/// Read one pending private correction from its durable owner seat.
pub async fn pr_review_correction(
    database: &Database,
    batch_id: Uuid,
    reviewer_id: &str,
    count: i32,
) -> Result<Option<PrReviewCorrection>> {
    let Some(batch) = pr_review_batch(database, batch_id).await? else {
        return Ok(None);
    };
    let row: Option<(String, Option<Vec<u8>>, Option<String>)> = sqlx::query_as(
        "SELECT s.status,s.report_raw,s.validation_error FROM tasks.pr_review_seats s \
         JOIN tasks.pr_review_batches b ON b.id=s.batch_id \
         WHERE s.batch_id=$1 AND s.reviewer_id=$2 AND b.status='admitted' \
         AND ((s.status='correcting' AND s.correction_count=$3) \
           OR (s.status='retrying' AND s.failure_count=$3))",
    )
    .bind(batch_id)
    .bind(reviewer_id)
    .bind(count)
    .fetch_optional(database.pool())
    .await?;
    let Some((status, report, reason)) = row else {
        return Ok(None);
    };
    let reviewer = batch
        .roster
        .iter()
        .find(|reviewer| reviewer.id == reviewer_id)
        .cloned()
        .ok_or_else(|| anyhow::anyhow!("PR correction reviewer is outside the roster"))?;
    Ok(Some(PrReviewCorrection {
        batch,
        reviewer,
        report: report.map_or(Ok(Value::Null), |raw| serde_json::from_slice(&raw))?,
        reason: reason.unwrap_or_default(),
        count,
        retry: status == "retrying",
    }))
}

/// Read a completed review and the finding IDs only its owner may disposition.
pub async fn pr_review_result(database: &Database, turn: Uuid) -> Result<Option<PrReviewResult>> {
    let row: Option<PrReviewResultRow> = sqlx::query_as(
        "SELECT b.id AS batch_id, b.task_id, t.repository_id, t.issue_number, b.pr_number, b.head, b.changed_paths, s.identity AS reviewer, s.report_raw \
         FROM tasks.pr_review_seats s JOIN tasks.pr_review_batches b ON b.id=s.batch_id \
         JOIN tasks.task_state t ON t.task_id=b.task_id \
         WHERE s.attempt_id=$1 AND s.status='completed' AND b.status='admitted'",
    )
    .bind(turn)
    .fetch_optional(database.pool())
    .await?;
    let Some(PrReviewResultRow {
        batch_id,
        task_id,
        repository_id,
        issue_number: number,
        pr_number,
        head,
        changed_paths: Json(changed_paths),
        reviewer: Json(reviewer),
        report_raw: raw,
    }) = row
    else {
        return Ok(None);
    };
    let findings: Vec<(String, String)> = sqlx::query_as(
        "SELECT id,status FROM tasks.pr_review_findings WHERE task_id=$1 \
         AND reviewer_id=$2 AND status IN ('open','withdrawn') ORDER BY sequence",
    )
    .bind(task_id)
    .bind(&reviewer.id)
    .fetch_all(database.pool())
    .await?;
    let (mut open_ids, mut withdrawn_ids) = (Vec::new(), Vec::new());
    for (id, status) in findings {
        if status == "open" {
            open_ids.push(id)
        } else {
            withdrawn_ids.push(id)
        }
    }
    Ok(Some(PrReviewResult {
        batch_id,
        task_id,
        issue: IssueKey {
            repository_id,
            number,
        },
        pr_number,
        head,
        changed_paths,
        reviewer,
        report: serde_json::from_slice(&raw)?,
        open_ids,
        withdrawn_ids,
    }))
}

/// Charge one complete PR reviewer roster under the task row lock.
pub async fn admit_pr_review_batch(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    issue: IssueKey,
    batch: Uuid,
    expected_revision: u64,
    specs: &[(String, AttemptSpec)],
) -> Result<Admission> {
    sqlx::query(
        "SELECT 1 FROM tasks.issue_controls WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    let current: Option<Json<TaskState>> = sqlx::query_scalar(
        "SELECT state FROM tasks.task_state WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    let Some(Json(mut current)) = current else {
        return Ok(Admission::Stale);
    };
    let recorded: Option<(Uuid, String, Json<Vec<DesignReviewer>>)> = sqlx::query_as(
        "SELECT task_id,head,roster FROM tasks.pr_review_batches \
         WHERE id=$1 AND status='recorded' FOR UPDATE",
    )
    .bind(batch)
    .fetch_optional(uow.connection())
    .await?;
    let Some((task_id, head, Json(roster))) = recorded else {
        return Ok(Admission::Stale);
    };
    if current.id.0 != task_id
        || current.stage != Stage::Review
        || current.revision != expected_revision
        || current.current_attempt.is_some()
    {
        return Ok(Admission::Stale);
    }
    let expected = roster
        .iter()
        .map(|reviewer| reviewer.id.as_str())
        .collect::<BTreeSet<_>>();
    let actual = specs
        .iter()
        .map(|(id, _)| id.as_str())
        .collect::<BTreeSet<_>>();
    ensure!(
        !specs.is_empty() && actual == expected && specs.len() == expected.len(),
        "PR reviewer roster changed before admission"
    );
    for (reviewer_id, spec) in specs {
        ensure!(
            spec.task == current.id && spec.expected_revision == expected_revision,
            "PR review attempt task identity changed"
        );
        let submission: TurnSubmission = serde_json::from_slice(&spec.submit_body)?;
        let binding: String = sqlx::query_scalar(
            "SELECT binding FROM tasks.pr_review_seats WHERE batch_id=$1 AND reviewer_id=$2",
        )
        .bind(batch)
        .bind(reviewer_id)
        .fetch_one(uow.connection())
        .await?;
        ensure!(
            submission.turn == spec.id
                && submission.payload["request"]["context"]["pr_review_binding"] == binding
                && submission.payload["request"]["context"]["reviewer_identity"]["id"]
                    == *reviewer_id
                && submission.payload["request"]["context"]["head"] == head,
            "PR review attempt binding changed"
        );
    }
    let mut retained = Vec::with_capacity(specs.len());
    for (_, spec) in specs {
        let check = check_retained_attempt(uow, spec).await?;
        if let RetainedCheck::Refused(reason) = check {
            current.retry_stage = Some(current.stage);
            current.stage = Stage::Blocked;
            current.stage_reason = Some(format!("PR review retained session refused: {reason}"));
            current.revision += 1;
            sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
                .bind(current.id.0)
                .bind(i64::try_from(current.revision)?)
                .bind(Json(&current))
                .execute(uow.connection())
                .await?;
            sqlx::query("UPDATE tasks.pr_review_batches SET status='superseded' WHERE id=$1")
                .bind(batch)
                .execute(uow.connection())
                .await?;
            append_task_snapshot(uow, outbox, &current).await?;
            return Ok(Admission::BudgetExhausted);
        }
        if matches!(check, RetainedCheck::Replay) {
            return Ok(Admission::Stale);
        }
        retained.push(check);
    }
    let pins = current
        .pins
        .as_ref()
        .ok_or_else(|| anyhow::anyhow!("task pins missing"))?;
    if billable_model_calls(uow, &current).await? + specs.len() as u64 > pins.max_model_calls {
        current.retry_stage = Some(current.stage);
        current.stage = Stage::Blocked;
        current.stage_reason =
            Some("model-call budget cannot admit the PR reviewer roster".to_owned());
        current.revision += 1;
        sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
            .bind(current.id.0)
            .bind(i64::try_from(current.revision)?)
            .bind(Json(&current))
            .execute(uow.connection())
            .await?;
        sqlx::query("UPDATE tasks.pr_review_batches SET status='superseded' WHERE id=$1")
            .bind(batch)
            .execute(uow.connection())
            .await?;
        append_task_snapshot(uow, outbox, &current).await?;
        return Ok(Admission::BudgetExhausted);
    }
    let mut funded = current.clone();
    for (_, spec) in specs {
        validate_funding_admission(uow, &funded, spec).await?;
        funded.used_model_calls += 1;
    }
    for (_, spec) in specs {
        sqlx::query("INSERT INTO tasks.staffing_groups (staffing_group,turn_id) VALUES ($1,$2) ON CONFLICT DO NOTHING")
            .bind(&spec.staffing_group).bind(spec.id).execute(uow.connection()).await?;
    }
    for (_, spec) in specs {
        let owner: Uuid =
            sqlx::query_scalar("SELECT turn_id FROM tasks.staffing_groups WHERE staffing_group=$1")
                .bind(&spec.staffing_group)
                .fetch_one(uow.connection())
                .await?;
        if owner != spec.id {
            for (_, proposed) in specs {
                sqlx::query(
                    "DELETE FROM tasks.staffing_groups WHERE staffing_group=$1 AND turn_id=$2",
                )
                .bind(&proposed.staffing_group)
                .bind(proposed.id)
                .execute(uow.connection())
                .await?;
            }
            return Ok(Admission::WaitingForGroup);
        }
    }
    for ((reviewer_id, spec), check) in specs.iter().zip(&retained) {
        let (scope, ordinal, digest) = match check {
            RetainedCheck::Eligible {
                scope,
                turn,
                digest,
            } => (Some(scope.as_str()), Some(*turn), Some(digest.as_str())),
            _ => (None, None, None),
        };
        sqlx::query("INSERT INTO tasks.attempts \
             (id,task_id,operation,expected_revision,staffing_group,fork,config_sha,submit_body, \
              prompt_body,initial_deadline,deadline,unbounded_running,status,retained_scope,retained_turn,retained_request) \
             VALUES ($1,$2,'review',$3,$4,$5,$6,$7,$8,$9,$9,$10,'ready',$11,$12,$13)")
            .bind(spec.id).bind(spec.task.0).bind(i64::try_from(expected_revision)?)
            .bind(&spec.staffing_group).bind(&spec.fork).bind(&spec.config_sha)
            .bind(&spec.submit_body).bind(&spec.prompt_body).bind(spec.deadline)
            .bind(spec.unbounded_running).bind(scope).bind(ordinal).bind(digest)
            .execute(uow.connection()).await?;
        sqlx::query(
            "UPDATE tasks.pr_review_seats SET attempt_id=$3,status='admitted' \
             WHERE batch_id=$1 AND reviewer_id=$2 AND attempt_id IS NULL",
        )
        .bind(batch)
        .bind(reviewer_id)
        .bind(spec.id)
        .execute(uow.connection())
        .await?;
        record_funding_grant(uow, spec).await?;
        events::append_turn_submitted(uow, outbox, current.id, spec, "review").await?;
        let effect_id: i64 = sqlx::query_scalar(
            "INSERT INTO tasks.effects (turn_id,kind,status) \
             VALUES ($1,'submit','recorded') RETURNING id",
        )
        .bind(spec.id)
        .fetch_one(uow.connection())
        .await?;
        enqueue_effect(uow, effect_id, 0, 0).await?;
    }
    current.used_model_calls += specs.len() as u64;
    current.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(current.id.0)
        .bind(i64::try_from(current.revision)?)
        .bind(Json(&current))
        .execute(uow.connection())
        .await?;
    sqlx::query("UPDATE tasks.pr_review_batches SET status='admitted' WHERE id=$1")
        .bind(batch)
        .execute(uow.connection())
        .await?;
    append_task_snapshot(uow, outbox, &current).await?;
    Ok(Admission::Admitted)
}

/// Charge one replacement reviewer turn while retaining all accepted sibling reports.
pub async fn admit_pr_review_correction(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    issue: IssueKey,
    batch: Uuid,
    reviewer: &str,
    count: i32,
    spec: &AttemptSpec,
) -> Result<Admission> {
    sqlx::query(
        "SELECT 1 FROM tasks.issue_controls WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    let state: Option<Json<TaskState>> = sqlx::query_scalar(
        "SELECT state FROM tasks.task_state WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    let Some(Json(mut state)) = state else {
        return Ok(Admission::Stale);
    };
    let seat: Option<(Uuid, String, String, String)> = sqlx::query_as(
        "SELECT b.task_id,b.head,s.binding,s.status FROM tasks.pr_review_batches b \
         JOIN tasks.pr_review_seats s ON s.batch_id=b.id \
         WHERE b.id=$1 AND b.status='admitted' AND s.reviewer_id=$2 \
         AND ((s.status='correcting' AND s.correction_count=$3) \
           OR (s.status='retrying' AND s.failure_count=$3)) FOR UPDATE OF b,s",
    )
    .bind(batch)
    .bind(reviewer)
    .bind(count)
    .fetch_optional(uow.connection())
    .await?;
    let Some((task_id, head, binding, seat_status)) = seat else {
        return Ok(Admission::Stale);
    };
    if state.id.0 != task_id || state.stage != Stage::Review || spec.task != state.id {
        return Ok(Admission::Stale);
    }
    let submission: TurnSubmission = serde_json::from_slice(&spec.submit_body)?;
    ensure!(
        submission.turn == spec.id
            && submission.payload["request"]["context"]["pr_review_binding"] == binding
            && submission.payload["request"]["context"]["reviewer_identity"]["id"] == reviewer
            && submission.payload["request"]["context"]["head"] == head
            && (seat_status == "retrying"
                || submission.payload["request"]["context"]["review_validation_feedback"]
                    .is_object()),
        "PR review correction binding changed"
    );
    let retained = check_retained_attempt(uow, spec).await?;
    if let RetainedCheck::Refused(reason) = retained {
        state.retry_stage = Some(state.stage);
        state.stage = Stage::Blocked;
        state.stage_reason = Some(format!("PR review retained session refused: {reason}"));
        state.revision += 1;
        sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
            .bind(state.id.0)
            .bind(i64::try_from(state.revision)?)
            .bind(Json(&state))
            .execute(uow.connection())
            .await?;
        sqlx::query("UPDATE tasks.pr_review_batches SET status='superseded' WHERE id=$1")
            .bind(batch)
            .execute(uow.connection())
            .await?;
        append_task_snapshot(uow, outbox, &state).await?;
        return Ok(Admission::BudgetExhausted);
    }
    if matches!(retained, RetainedCheck::Replay) {
        return Ok(Admission::Existing);
    }
    if billable_model_calls(uow, &state).await?
        >= state.pins.as_ref().expect("review pins").max_model_calls
    {
        state.retry_stage = Some(state.stage);
        state.stage = Stage::Blocked;
        state.stage_reason =
            Some("model-call budget exhausted during PR review correction".to_owned());
        state.revision += 1;
        sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
            .bind(state.id.0)
            .bind(i64::try_from(state.revision)?)
            .bind(Json(&state))
            .execute(uow.connection())
            .await?;
        sqlx::query("UPDATE tasks.pr_review_batches SET status='superseded' WHERE id=$1")
            .bind(batch)
            .execute(uow.connection())
            .await?;
        append_task_snapshot(uow, outbox, &state).await?;
        return Ok(Admission::BudgetExhausted);
    }
    validate_funding_admission(uow, &state, spec).await?;
    sqlx::query("INSERT INTO tasks.staffing_groups (staffing_group,turn_id) VALUES ($1,$2) ON CONFLICT DO NOTHING")
        .bind(&spec.staffing_group).bind(spec.id).execute(uow.connection()).await?;
    let owner: Uuid =
        sqlx::query_scalar("SELECT turn_id FROM tasks.staffing_groups WHERE staffing_group=$1")
            .bind(&spec.staffing_group)
            .fetch_one(uow.connection())
            .await?;
    if owner != spec.id {
        return Ok(Admission::WaitingForGroup);
    }
    let (scope, ordinal, digest) = match &retained {
        RetainedCheck::Eligible {
            scope,
            turn,
            digest,
        } => (Some(scope.as_str()), Some(*turn), Some(digest.as_str())),
        _ => (None, None, None),
    };
    sqlx::query("INSERT INTO tasks.attempts \
         (id,task_id,operation,expected_revision,staffing_group,fork,config_sha,submit_body, \
          prompt_body,initial_deadline,deadline,unbounded_running,status,retained_scope,retained_turn,retained_request) \
         VALUES ($1,$2,'review',$3,$4,$5,$6,$7,$8,$9,$9,$10,'ready',$11,$12,$13)")
        .bind(spec.id).bind(state.id.0).bind(i64::try_from(state.revision)?)
        .bind(&spec.staffing_group).bind(&spec.fork).bind(&spec.config_sha)
        .bind(&spec.submit_body).bind(&spec.prompt_body).bind(spec.deadline)
        .bind(spec.unbounded_running).bind(scope).bind(ordinal).bind(digest)
        .execute(uow.connection()).await?;
    sqlx::query(
        "UPDATE tasks.pr_review_seats SET attempt_id=$4,status='admitted' \
         WHERE batch_id=$1 AND reviewer_id=$2 \
         AND ((status='correcting' AND correction_count=$3) \
           OR (status='retrying' AND failure_count=$3))",
    )
    .bind(batch)
    .bind(reviewer)
    .bind(count)
    .bind(spec.id)
    .execute(uow.connection())
    .await?;
    record_funding_grant(uow, spec).await?;
    events::append_turn_submitted(uow, outbox, state.id, spec, "review").await?;
    let effect_id: i64 = sqlx::query_scalar(
        "INSERT INTO tasks.effects (turn_id,kind,status) VALUES ($1,'submit','recorded') RETURNING id",
    )
    .bind(spec.id).fetch_one(uow.connection()).await?;
    enqueue_effect(uow, effect_id, 0, 0).await?;
    state.used_model_calls += 1;
    state.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(state.id.0)
        .bind(i64::try_from(state.revision)?)
        .bind(Json(&state))
        .execute(uow.connection())
        .await?;
    append_task_snapshot(uow, outbox, &state).await?;
    Ok(Admission::Admitted)
}

/// Retain a terminal reviewer result before validating its findings against the batch.
pub(crate) async fn settle_pr_review_attempt(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    state: &TaskState,
    turn: Uuid,
) -> Result<()> {
    let seat: Option<(Uuid, String, i32, i32, i32)> = sqlx::query_as(
        "SELECT s.batch_id,s.reviewer_id,s.failure_count,s.session_failure_count,s.session_epoch FROM tasks.pr_review_seats s \
         JOIN tasks.pr_review_batches b ON b.id=s.batch_id \
         WHERE s.attempt_id=$1 AND b.task_id=$2 AND b.status='admitted' \
         AND s.status='admitted' FOR UPDATE OF s",
    )
    .bind(turn)
    .bind(state.id.0)
    .fetch_optional(uow.connection())
    .await?;
    let Some((batch, reviewer, failure_count, session_failures, seat_epoch)) = seat else {
        return Ok(());
    };
    let (status, result, raw_result, reason, submit_body): SettlePrReviewAttemptRow = sqlx::query_as(
        "SELECT status,result,result_raw,outcome_reason,submit_body FROM tasks.attempts WHERE id=$1 FOR UPDATE",
    )
    .bind(turn)
    .fetch_one(uow.connection())
    .await?;
    if !matches!(status.as_str(), "finished" | "unknown" | "cancelled") {
        return Ok(());
    }
    if status == "finished"
        && reason.is_none()
        && result
            .as_ref()
            .is_some_and(|value| value.0["outcome"] == "ok")
    {
        let result: Value = raw_result.map_or_else(
            || Ok(result.expect("successful result checked").0),
            |raw| serde_json::from_slice(&raw),
        )?;
        let report = result.get("response").cloned().unwrap_or(Value::Null);
        let raw = serde_json::to_vec(&report)?;
        let mut projected = report;
        operational_json(&mut projected);
        sqlx::query(
            "UPDATE tasks.pr_review_seats SET status='completed',report=$4,report_raw=$5, \
             failure_count=0,session_failure_count=0 \
             WHERE batch_id=$1 AND reviewer_id=$2 AND attempt_id=$3 AND status='admitted'",
        )
        .bind(batch)
        .bind(&reviewer)
        .bind(turn)
        .bind(Json(projected))
        .bind(raw)
        .execute(uow.connection())
        .await?;
        enqueue_pr_review_result(uow, state.id.0, turn).await?;
    } else {
        let credential_failure =
            status == "finished" && has_credential_exclusion(uow, turn).await?;
        let session_failure = definite_session_failure(
            &status,
            reason.as_deref(),
            result.as_ref().map(|value| &value.0),
            &submit_body,
        );
        let reason = reason.unwrap_or_else(|| {
            result
                .as_ref()
                .and_then(|value| value.0["outcome"].as_str())
                .map_or_else(
                    || format!("Reviewer turn ended with status: {status}"),
                    |outcome| format!("Reviewer turn ended with outcome: {outcome}"),
                )
        });
        let retry = state.stage == Stage::Review
            && (credential_failure || (session_failure && session_failures < 1));
        let next_failures = failure_count + 1;
        let next_session_failures = session_failures + i32::from(session_failure);
        let session_epoch: i32 = if session_failure || credential_failure {
            sqlx::query_scalar(
                "INSERT INTO tasks.pr_review_sessions (task_id,reviewer_id,session_epoch) \
                 VALUES ($1,$2,1) ON CONFLICT (task_id,reviewer_id) DO UPDATE \
                 SET session_epoch=pr_review_sessions.session_epoch+1 RETURNING session_epoch",
            )
            .bind(state.id.0)
            .bind(&reviewer)
            .fetch_one(uow.connection())
            .await?
        } else {
            seat_epoch
        };
        sqlx::query(
            "UPDATE tasks.pr_review_seats SET status=$4,validation_error=$5, \
             failure_count=$6,session_epoch=$7,session_failure_count=$8 \
             WHERE batch_id=$1 AND reviewer_id=$2 AND attempt_id=$3 AND status='admitted'",
        )
        .bind(batch)
        .bind(&reviewer)
        .bind(turn)
        .bind(if retry { "retrying" } else { "failed" })
        .bind(&reason)
        .bind(next_failures)
        .bind(session_epoch)
        .bind(next_session_failures)
        .execute(uow.connection())
        .await?;
        if retry {
            enqueue_pr_review_correction(uow, batch, &reviewer, next_failures).await?;
        } else if state.stage == Stage::Review {
            let mut blocked = state.clone();
            blocked.retry_stage = Some(blocked.stage);
            blocked.stage = Stage::Blocked;
            blocked.stage_reason = Some(format!("PR reviewer {reviewer} failed: {reason}"));
            blocked.revision += 1;
            sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
                .bind(state.id.0)
                .bind(i64::try_from(blocked.revision)?)
                .bind(Json(&blocked))
                .execute(uow.connection())
                .await?;
            sqlx::query("UPDATE tasks.pr_review_batches SET status='superseded' WHERE id=$1")
                .bind(batch)
                .execute(uow.connection())
                .await?;
            append_task_snapshot(uow, outbox, &blocked).await?;
        }
    }
    Ok(())
}

/// Apply one independently validated review without allowing another owner to answer its findings.
pub async fn apply_pr_review_result(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    turn: Uuid,
    validated: Result<PrReviewReport, String>,
) -> Result<bool> {
    let Some(state) = crate::lock_issue_for_turn(uow, turn).await? else {
        return Ok(false);
    };
    let row: Option<PrReviewSettlementRow> = sqlx::query_as(
        "SELECT b.id AS batch_id, b.head, b.plan_id, s.identity AS reviewer, s.report_raw, i.snapshot, p.raw_report_bytes AS plan_report, s.binding, s.correction_count, s.repair_signature \
         FROM tasks.pr_review_seats s JOIN tasks.pr_review_batches b ON b.id=s.batch_id \
         JOIN tasks.initialization_inputs i ON i.task_id=b.task_id \
         JOIN tasks.plan_reports p ON p.task_id=b.task_id AND p.plan_id=b.plan_id \
             AND p.accepted AND p.current \
         WHERE s.attempt_id=$1 AND s.status='completed' AND b.status='admitted' \
         FOR UPDATE OF b,s",
    )
    .bind(turn)
    .fetch_optional(uow.connection())
    .await?;
    let Some(PrReviewSettlementRow {
        batch_id: batch,
        head,
        plan_id,
        reviewer: Json(reviewer),
        report_raw: raw,
        snapshot: Json(snapshot),
        plan_report: plan_raw,
        binding,
        correction_count,
        repair_signature,
    }) = row
    else {
        return Ok(false);
    };
    if state.stage != Stage::Review {
        return Ok(false);
    }
    let plan: Value = serde_json::from_slice(&plan_raw)?;
    let current_binding = pr_review_binding(&snapshot, &plan, &plan_id, &head, &reviewer)
        .map_err(anyhow::Error::msg)?;
    let published: Option<Json<GitPublicationIntent>> = sqlx::query_scalar(
        "SELECT p.intent FROM tasks.git_publications p \
            JOIN tasks.pr_review_batches b ON b.publication_attempt=p.attempt_id \
            WHERE b.id=$1 AND p.task_id=$2",
    )
    .bind(batch)
    .bind(state.id.0)
    .fetch_optional(uow.connection())
    .await?;
    if current_binding != binding
        || published
            .as_ref()
            .and_then(|intent| intent.0.commit_sha.as_deref())
            != Some(&head)
    {
        block_pr_review_batch(
            uow,
            outbox,
            state.issue,
            batch,
            "PR review binding or published head changed before result admission",
        )
        .await?;
        return Ok(false);
    }
    let report = match validated {
        Ok(report) => report,
        Err(error) => {
            let signature = python_digest(&serde_json::json!([
                error,
                serde_json::from_slice::<Value>(&raw)?
            ]))?;
            let max_repairs = snapshot.configuration["max_review_repairs"]
                .as_i64()
                .unwrap_or(0);
            let stored_error = error.replace('\0', "\\u0000");
            if repair_signature.as_deref() == Some(&signature)
                || i64::from(correction_count) >= max_repairs
            {
                sqlx::query(
                    "UPDATE tasks.pr_review_seats SET status='failed',validation_error=$2 \
                     WHERE attempt_id=$1 AND status='completed'",
                )
                .bind(turn)
                .bind(&stored_error)
                .execute(uow.connection())
                .await?;
                let mut escalated = state.clone();
                escalated.retry_stage = Some(escalated.stage);
                escalated.stage = Stage::Escalated;
                escalated.stage_reason = Some(
                    "PR reviewer output repair made no progress or exhausted its budget".to_owned(),
                );
                escalated.revision += 1;
                sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
                    .bind(state.id.0)
                    .bind(i64::try_from(escalated.revision)?)
                    .bind(Json(&escalated))
                    .execute(uow.connection())
                    .await?;
                sqlx::query("UPDATE tasks.pr_review_batches SET status='superseded' WHERE id=$1")
                    .bind(batch)
                    .execute(uow.connection())
                    .await?;
                append_task_snapshot(uow, outbox, &escalated).await?;
            } else {
                let count = correction_count + 1;
                sqlx::query(
                    "UPDATE tasks.pr_review_seats SET status='correcting',validation_error=$2, \
                     correction_count=$3,repair_signature=$4 \
                     WHERE attempt_id=$1 AND status='completed'",
                )
                .bind(turn)
                .bind(&stored_error)
                .bind(count)
                .bind(&signature)
                .execute(uow.connection())
                .await?;
                enqueue_pr_review_correction(uow, batch, &reviewer.id, count).await?;
            }
            events::append_deferred_turn_finished(uow, outbox, turn, None).await?;
            return Ok(true);
        }
    };
    let mut sequence: i64 = sqlx::query_scalar(
        "SELECT COALESCE(max(sequence),0) FROM tasks.pr_review_findings WHERE task_id=$1",
    )
    .bind(state.id.0)
    .fetch_one(uow.connection())
    .await?;
    for disposition in &report.dispositions {
        let changed = sqlx::query(
            "UPDATE tasks.pr_review_findings SET status=$4,disposition=$5 \
             WHERE task_id=$1 AND id=$2 AND reviewer_id=$3 AND status IN ('open','withdrawn')",
        )
        .bind(state.id.0)
        .bind(&disposition.id)
        .bind(&reviewer.id)
        .bind(&disposition.status)
        .bind(Json(disposition))
        .execute(uow.connection())
        .await?;
        ensure!(
            changed.rows_affected() == 1,
            "PR finding owner or status changed"
        );
    }
    let mut finding_ids = Vec::with_capacity(report.findings.len());
    for finding in &report.findings {
        sequence += 1;
        let id = format!("F{sequence}");
        sqlx::query(
            "INSERT INTO tasks.pr_review_findings \
             (task_id,sequence,id,reviewer_id,status,head,finding) \
             VALUES ($1,$2,$3,$4,'open',$5,$6)",
        )
        .bind(state.id.0)
        .bind(sequence)
        .bind(&id)
        .bind(&reviewer.id)
        .bind(&head)
        .bind(Json(finding))
        .execute(uow.connection())
        .await?;
        finding_ids.push(id);
    }
    sqlx::query("UPDATE tasks.pr_review_seats SET status='accepted',report=$2 WHERE attempt_id=$1")
        .bind(turn)
        .bind(Json(&report))
        .execute(uow.connection())
        .await?;
    let marker = format!("<!-- concurro:pr-review:{}:{turn} -->", state.id.0);
    let body = pr_review_body(&marker, &reviewer, &report, &finding_ids);
    let pr_number: i64 =
        sqlx::query_scalar("SELECT pr_number FROM tasks.pr_review_batches WHERE id=$1")
            .bind(batch)
            .fetch_one(uow.connection())
            .await?;
    sqlx::query(
        "INSERT INTO tasks.pr_review_publications \
         (turn_id,task_id,batch_id,pr_number,head,marker,body) \
         VALUES ($1,$2,$3,$4,$5,$6,$7)",
    )
    .bind(turn)
    .bind(state.id.0)
    .bind(batch)
    .bind(pr_number)
    .bind(&head)
    .bind(&marker)
    .bind(&body)
    .execute(uow.connection())
    .await?;
    crate::pr_review_actions::record_pr_review_actions(
        uow,
        &state,
        turn,
        &marker,
        &body,
        &report,
        &finding_ids,
    )
    .await?;
    enqueue_pr_review_publish(uow, state.id.0, turn).await?;
    events::append_task_report(uow, outbox, &state, turn, &raw, "review", "assessment").await?;
    events::append_deferred_turn_finished(uow, outbox, turn, None).await?;
    Ok(true)
}

/// Preserve a changed external PR as a visible review conflict.
pub async fn block_pr_review_batch(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    issue: IssueKey,
    batch: Uuid,
    reason: &str,
) -> Result<bool> {
    sqlx::query(
        "SELECT 1 FROM tasks.issue_controls WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    let state: Option<Json<TaskState>> = sqlx::query_scalar(
        "SELECT state FROM tasks.task_state WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(uow.connection())
    .await?;
    let Some(Json(mut state)) = state else {
        return Ok(false);
    };
    if state.stage != Stage::Review {
        return Ok(false);
    }
    let changed = sqlx::query(
        "UPDATE tasks.pr_review_batches SET status='superseded' WHERE id=$1 AND task_id=$2 \
         AND status IN ('recorded','admitted')",
    )
    .bind(batch)
    .bind(state.id.0)
    .execute(uow.connection())
    .await?;
    if changed.rows_affected() == 0 {
        return Ok(false);
    }
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
    append_task_snapshot(uow, outbox, &state).await?;
    Ok(true)
}
