//! Durable independent reviewer batches and per-seat attempt admission.

use std::collections::BTreeSet;

use anyhow::ensure;
use concurro_db::{Database, UnitOfWork};
use concurro_events_postgres::Outbox;
use concurro_exchange_v1::TurnSubmission;
use concurro_tasks_core::{
    Admission, AttemptSpec, DesignReviewReport, DesignReviewer, InitializationSnapshot, IssueKey,
    Stage, TaskState, design_binding, design_roster,
};
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

use crate::{
    append_task_snapshot,
    archive::{RetainedCheck, check_retained_attempt},
    block_preflight,
    effects::{definite_session_failure, operational_json},
    enqueue_design_batch, enqueue_design_correction, enqueue_design_result, enqueue_effect,
    enqueue_plan, events, lock_issue_for_turn,
    sponsorship::{
        billable_model_calls, has_credential_exclusion, record_funding_grant,
        validate_funding_admission,
    },
};

/// Frozen plan and roster to prepare before one atomic batch admission.
pub struct DesignBatch {
    /// Stable batch identity used by the worker job and seats.
    pub id: Uuid,
    /// Owning task identity.
    pub task_id: Uuid,
    /// Exact plan identity.
    pub plan_id: String,
    /// Governance and source binding shared by all seats.
    pub binding: String,
    /// Cooperative or fresh assessment phase.
    pub phase: String,
    /// Design revision number.
    pub generation: i64,
    /// Independent reviewers in pinned roster order.
    pub roster: Vec<DesignReviewer>,
    /// Exact accepted plan for all reviewer prompts.
    pub plan: Value,
}

/// One retained reviewer response awaiting source-aware vote validation.
pub struct DesignResult {
    /// Exchange attempt supplying the response.
    pub turn: Uuid,
    /// Owning task identity.
    pub task_id: Uuid,
    /// Issue source needed for pinned GitHub reads.
    pub issue: IssueKey,
    /// Review batch identity.
    pub batch_id: Uuid,
    /// Exact governance binding.
    pub binding: String,
    /// Cooperative or fresh phase of the bound assessment.
    pub phase: String,
    /// Pinned reviewer identity.
    pub reviewer: DesignReviewer,
    /// Exact response body.
    pub report: Value,
}

/// One seat's rejected report and feedback for its next private repair turn.
pub struct DesignCorrection {
    /// Source issue for the task's current state and pinned target.
    pub issue: IssueKey,
    /// Review batch and shared frozen design inputs.
    pub batch: DesignBatch,
    /// Reviewer whose output failed validation.
    pub reviewer: DesignReviewer,
    /// Exact rejected report.
    pub report: Value,
    /// Validation feedback for the same reviewer only.
    pub reason: String,
    /// One-based correction count.
    pub count: i32,
    /// Whether this seat is restarting after a provider failure.
    pub retry: bool,
    /// Rotated private journal generation after provider failures.
    pub session_epoch: i32,
}

/// Read a queued correction without admitting or charging a replacement turn.
pub async fn design_correction(
    database: &Database,
    batch_id: Uuid,
    reviewer_id: &str,
    count: i32,
) -> anyhow::Result<Option<DesignCorrection>> {
    let batch: Option<(Uuid, i64, i64, String, String, String, i64, Json<Vec<DesignReviewer>>, Vec<u8>, Option<Vec<u8>>, String, String, i32)> = sqlx::query_as(
        "SELECT b.task_id,t.repository_id,t.issue_number,b.plan_id,b.binding,b.phase,b.generation,b.roster,p.raw_report_bytes, \
         s.report_raw,s.validation_error,s.status,s.session_epoch FROM tasks.plan_review_batches b \
         JOIN tasks.plan_review_seats s ON s.batch_id=b.id \
         JOIN tasks.task_state t ON t.task_id=b.task_id \
         JOIN tasks.plan_reports p ON p.task_id=b.task_id AND p.plan_id=b.plan_id AND p.accepted AND p.current \
         WHERE b.id=$1 AND b.status='admitted' AND s.reviewer_id=$2 \
         AND ((s.status='correcting' AND s.correction_count=$3) \
           OR (s.status='retrying' AND s.failure_count=$3)) \
         ORDER BY p.recorded_at DESC,p.attempt_id DESC LIMIT 1",
    )
    .bind(batch_id)
    .bind(reviewer_id)
    .bind(count)
    .fetch_optional(database.pool())
    .await?;
    let Some((
        task_id,
        repository_id,
        number,
        plan_id,
        binding,
        phase,
        generation,
        Json(roster),
        plan,
        report,
        reason,
        status,
        session_epoch,
    )) = batch
    else {
        return Ok(None);
    };
    let Some(reviewer) = roster
        .iter()
        .find(|reviewer| reviewer.id == reviewer_id)
        .cloned()
    else {
        anyhow::bail!("correction reviewer is outside the bound roster");
    };
    Ok(Some(DesignCorrection {
        issue: IssueKey {
            repository_id,
            number,
        },
        batch: DesignBatch {
            id: batch_id,
            task_id,
            plan_id,
            binding,
            phase,
            generation,
            roster,
            plan: serde_json::from_slice(&plan)?,
        },
        reviewer,
        report: report.map_or(Ok(Value::Null), |raw| serde_json::from_slice(&raw))?,
        reason,
        count,
        retry: status == "retrying",
        session_epoch,
    }))
}

/// Renew a paused design seat without discarding accepted sibling reports.
pub(crate) async fn retry_design_review(
    uow: &mut UnitOfWork<'_>,
    task: &TaskState,
) -> anyhow::Result<bool> {
    let batch: Option<(Uuid, String)> = sqlx::query_as(
        "SELECT b.id,b.status FROM tasks.plan_review_batches b \
         WHERE b.task_id=$1 AND b.plan_id=(SELECT plan_id FROM tasks.plan_reports \
             WHERE task_id=$1 AND accepted AND current \
             ORDER BY recorded_at DESC,attempt_id DESC LIMIT 1) \
         ORDER BY b.generation DESC,b.id DESC LIMIT 1 FOR UPDATE OF b",
    )
    .bind(task.id.0)
    .fetch_optional(uow.connection())
    .await?;
    let Some((batch, status)) = batch else {
        return Ok(false);
    };
    if status == "recorded" {
        crate::enqueue_design_batch(uow, task, batch).await?;
        return Ok(true);
    }
    if !matches!(status.as_str(), "admitted" | "superseded") {
        return Ok(false);
    }
    let seats: Vec<(String, String, Option<Uuid>, bool)> = sqlx::query_as(
        "SELECT reviewer_id,status,attempt_id,report_raw IS NOT NULL \
         FROM tasks.plan_review_seats WHERE batch_id=$1 ORDER BY reviewer_id FOR UPDATE",
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
                    "UPDATE tasks.plan_review_seats SET status=$3,correction_count=0, \
                     failure_count=0,session_failure_count=0,repair_signature=NULL \
                     WHERE batch_id=$1 AND reviewer_id=$2",
                )
                .bind(batch)
                .bind(&reviewer)
                .bind(next)
                .execute(uow.connection())
                .await?;
                crate::enqueue_design_correction(uow, batch, &reviewer, 0).await?;
                renewed = true;
            }
            "completed" => {
                if let Some(turn) = attempt {
                    crate::enqueue_design_result(uow, turn).await?;
                    renewed = true;
                }
            }
            _ => {}
        }
    }
    if renewed && status == "superseded" {
        sqlx::query("UPDATE tasks.plan_review_batches SET status='admitted' WHERE id=$1")
            .bind(batch)
            .execute(uow.connection())
            .await?;
    }
    Ok(renewed)
}

/// Freeze one design generation and its entire reviewer roster before admission.
pub(crate) async fn record_design_batch(
    uow: &mut UnitOfWork<'_>,
    task: &TaskState,
    snapshot: &InitializationSnapshot,
    plan: &Value,
    plan_id: &str,
    generation: i64,
    phase: &str,
) -> anyhow::Result<Uuid> {
    let roster = design_roster(&snapshot.configuration).map_err(anyhow::Error::msg)?;
    let binding = design_binding(
        plan,
        plan_id,
        snapshot,
        &roster,
        u64::try_from(generation)?,
        phase,
        None,
        &[],
    )
    .map_err(anyhow::Error::msg)?;
    let batch = Uuid::now_v7();
    sqlx::query(
        "INSERT INTO tasks.plan_review_batches \
         (id,task_id,plan_id,generation,phase,binding,roster,snapshot) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
    )
    .bind(batch)
    .bind(task.id.0)
    .bind(plan_id)
    .bind(generation)
    .bind(phase)
    .bind(&binding)
    .bind(Json(&roster))
    .bind(Json(snapshot))
    .execute(uow.connection())
    .await?;
    for reviewer in &roster {
        sqlx::query(
            "INSERT INTO tasks.plan_review_seats \
             (batch_id,reviewer_id,identity,session_epoch) VALUES ($1,$2,$3, \
             COALESCE((SELECT session_epoch FROM tasks.plan_review_sessions \
               WHERE task_id=$4 AND reviewer_id=$2),0))",
        )
        .bind(batch)
        .bind(&reviewer.id)
        .bind(Json(reviewer))
        .bind(task.id.0)
        .execute(uow.connection())
        .await?;
    }
    enqueue_design_batch(uow, task, batch).await?;
    Ok(batch)
}

/// Read a saved independent result without applying its vote.
pub async fn design_result(
    database: &Database,
    turn: Uuid,
) -> anyhow::Result<Option<DesignResult>> {
    let row: Option<(Uuid, i64, i64, Uuid, String, String, Json<DesignReviewer>, Vec<u8>)> = sqlx::query_as(
        "SELECT b.task_id,t.repository_id,t.issue_number,b.id,b.binding,b.phase,s.identity,s.report_raw \
         FROM tasks.plan_review_seats s JOIN tasks.plan_review_batches b ON b.id=s.batch_id \
         JOIN tasks.task_state t ON t.task_id=b.task_id \
         WHERE s.attempt_id=$1 AND s.status='completed' AND b.status='admitted'",
    )
    .bind(turn)
    .fetch_optional(database.pool())
    .await?;
    row.map(
        |(task_id, repository_id, number, batch_id, binding, phase, Json(reviewer), raw)| {
            Ok(DesignResult {
                turn,
                task_id,
                issue: IssueKey {
                    repository_id,
                    number,
                },
                batch_id,
                binding,
                phase,
                reviewer,
                report: serde_json::from_slice(&raw)?,
            })
        },
    )
    .transpose()
}

/// Read one reviewer's own objection history without exposing sibling votes.
pub async fn reviewer_objections(
    database: &Database,
    task: Uuid,
    reviewer: &str,
) -> Result<Vec<Value>, sqlx::Error> {
    let rows: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('id',id,'reviewer',reviewer_id,'text',detail, \
         'counterexample',counterexample,'closure',closure,'evidence',evidence, \
         'resolved',resolved,'plan',plan_id,'phase',phase) \
         FROM tasks.plan_objections WHERE task_id=$1 AND reviewer_id=$2 ORDER BY sequence",
    )
    .bind(task)
    .bind(reviewer)
    .fetch_all(database.pool())
    .await?;
    Ok(rows.into_iter().map(|(Json(value),)| value).collect())
}

/// Read the reviewer's retained-session generation across design batches.
pub async fn reviewer_session_epoch(
    database: &Database,
    task: Uuid,
    reviewer: &str,
) -> Result<i32, sqlx::Error> {
    let epoch: Option<i32> = sqlx::query_scalar(
        "SELECT session_epoch FROM tasks.plan_review_sessions \
         WHERE task_id=$1 AND reviewer_id=$2",
    )
    .bind(task)
    .bind(reviewer)
    .fetch_optional(database.pool())
    .await?;
    Ok(epoch.unwrap_or_default())
}

/// Read only unresolved objections that the next planner must answer exactly once.
pub async fn open_plan_objections(
    database: &Database,
    task: Uuid,
) -> Result<Vec<Value>, sqlx::Error> {
    let rows: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('id',id,'reviewer',reviewer_id,'text',detail, \
         'counterexample',counterexample,'closure',closure,'evidence',evidence, \
         'resolved',resolved,'plan',plan_id,'phase',phase) \
         FROM tasks.plan_objections WHERE task_id=$1 AND NOT resolved ORDER BY sequence",
    )
    .bind(task)
    .fetch_all(database.pool())
    .await?;
    Ok(rows.into_iter().map(|(Json(value),)| value).collect())
}

/// Prove the latest plan has a complete current unanimous binding.
pub(crate) async fn approval_proof(
    uow: &mut UnitOfWork<'_>,
    task: Uuid,
) -> anyhow::Result<Option<(String, String)>> {
    let row: Option<(String, Vec<u8>, Json<InitializationSnapshot>, bool)> = sqlx::query_as(
        "SELECT p.plan_id,p.raw_report_bytes,i.snapshot, \
         COALESCE((SELECT r.adopted_snapshot=i.snapshot FROM tasks.task_resumes r \
           WHERE r.task_id=p.task_id ORDER BY r.recorded_at DESC,r.request_id DESC \
           LIMIT 1),false) FROM tasks.plan_reports p \
         JOIN tasks.initialization_inputs i ON i.task_id=p.task_id \
         WHERE p.task_id=$1 AND p.accepted AND p.current \
         ORDER BY p.recorded_at DESC,p.attempt_id DESC LIMIT 1",
    )
    .bind(task)
    .fetch_optional(uow.connection())
    .await?;
    let Some((plan_id, raw, Json(snapshot), resumed)) = row else {
        return Ok(None);
    };
    let batch: Option<(
        String,
        String,
        i64,
        Json<Vec<DesignReviewer>>,
        Option<Json<InitializationSnapshot>>,
        i64,
        bool,
    )> = sqlx::query_as(
        "SELECT b.binding,b.phase,b.generation,b.roster,b.snapshot,count(s.reviewer_id), \
         COALESCE(bool_and(s.status='accepted' AND s.report->>'decision'='approve' \
         AND s.report->>'risk'<>'high' AND (b.phase='fresh' OR s.first_pass_eligible)),false) \
         FROM tasks.plan_review_batches b LEFT JOIN tasks.plan_review_seats s ON s.batch_id=b.id \
         WHERE b.task_id=$1 AND b.plan_id=$2 AND b.status='settled' \
         GROUP BY b.id ORDER BY b.generation DESC,b.phase DESC,b.id DESC LIMIT 1",
    )
    .bind(task)
    .bind(&plan_id)
    .fetch_optional(uow.connection())
    .await?;
    let Some((binding, phase, generation, Json(roster), bound, count, unanimous)) = batch else {
        return Ok(None);
    };
    if !unanimous || usize::try_from(count)? != roster.len() {
        return Ok(None);
    }
    let plan: Value = serde_json::from_slice(&raw)?;
    let expected = design_binding(
        &plan,
        &plan_id,
        bound
            .as_ref()
            .filter(|_| resumed)
            .map_or(&snapshot, |bound| &bound.0),
        &roster,
        u64::try_from(generation)?,
        &phase,
        None,
        &[],
    )
    .map_err(anyhow::Error::msg)?;
    Ok((expected == binding).then_some((plan_id, binding)))
}

/// Read the recorded batch and accepted plan without consuming a turn.
pub async fn design_batch(database: &Database, batch: Uuid) -> anyhow::Result<Option<DesignBatch>> {
    let row: Option<(
        Uuid,
        String,
        String,
        String,
        i64,
        Json<Vec<DesignReviewer>>,
        Vec<u8>,
    )> = sqlx::query_as(
        "SELECT b.task_id,b.plan_id,b.binding,b.phase,b.generation,b.roster,p.raw_report_bytes \
             FROM tasks.plan_review_batches b JOIN tasks.plan_reports p \
             ON p.task_id=b.task_id AND p.plan_id=b.plan_id AND p.accepted AND p.current \
             WHERE b.id=$1 AND b.status='recorded' \
             ORDER BY p.recorded_at DESC,p.attempt_id DESC LIMIT 1",
    )
    .bind(batch)
    .fetch_optional(database.pool())
    .await?;
    row.map(
        |(task_id, plan_id, binding, phase, generation, Json(roster), raw)| {
            Ok(DesignBatch {
                id: batch,
                task_id,
                plan_id,
                binding,
                phase,
                generation,
                roster,
                plan: serde_json::from_slice(&raw)?,
            })
        },
    )
    .transpose()
}

/// Charge and record every initial review seat in one task transaction.
pub async fn admit_design_batch(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    issue: IssueKey,
    batch_id: Uuid,
    expected_revision: u64,
    specs: &[(String, AttemptSpec)],
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
    let batch: Option<(Uuid, String, Json<Vec<DesignReviewer>>)> = sqlx::query_as(
        "SELECT task_id,binding,roster FROM tasks.plan_review_batches \
         WHERE id=$1 AND status='recorded' FOR UPDATE",
    )
    .bind(batch_id)
    .fetch_optional(uow.connection())
    .await?;
    let Some((task_id, binding, Json(roster))) = batch else {
        return Ok(Admission::Stale);
    };
    if current.id.0 != task_id
        || current.stage != Stage::PlanQc
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
        .map(|(reviewer, _)| reviewer.as_str())
        .collect::<BTreeSet<_>>();
    ensure!(
        !specs.is_empty() && actual == expected && specs.len() == expected.len(),
        "review roster changed before admission"
    );
    for (reviewer_id, spec) in specs {
        ensure!(
            spec.task == current.id && spec.expected_revision == expected_revision,
            "review attempt task identity changed"
        );
        let submission: TurnSubmission = serde_json::from_slice(&spec.submit_body)?;
        ensure!(
            submission.turn == spec.id
                && submission.payload["request"]["context"]["design_binding"] == binding
                && submission.payload["request"]["context"]["reviewer_identity"]["id"]
                    == *reviewer_id,
            "review attempt binding changed"
        );
    }
    let mut retained_checks = Vec::with_capacity(specs.len());
    for (_, spec) in specs {
        let check = check_retained_attempt(uow, spec).await?;
        if let RetainedCheck::Refused(reason) = check {
            return block_preflight(
                uow,
                outbox,
                &mut current,
                &format!("Retained continuation unavailable: {reason}"),
                Some(batch_id),
            )
            .await;
        }
        if matches!(check, RetainedCheck::Replay) {
            return Ok(Admission::Stale);
        }
        retained_checks.push(check);
    }
    let pins = current
        .pins
        .as_ref()
        .ok_or_else(|| anyhow::anyhow!("task pins missing"))?;
    if billable_model_calls(uow, &current).await? + specs.len() as u64 > pins.max_model_calls {
        current.retry_stage = Some(current.stage);
        current.stage = Stage::Blocked;
        current.stage_reason = Some("model-call budget cannot admit the review roster".to_owned());
        current.revision += 1;
        sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
            .bind(current.id.0)
            .bind(i64::try_from(current.revision)?)
            .bind(Json(&current))
            .execute(uow.connection())
            .await?;
        sqlx::query("UPDATE tasks.plan_review_batches SET status='superseded' WHERE id=$1")
            .bind(batch_id)
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
        sqlx::query(
            "INSERT INTO tasks.staffing_groups (staffing_group,turn_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
        )
        .bind(&spec.staffing_group)
        .bind(spec.id)
        .execute(uow.connection())
        .await?;
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
    for ((reviewer_id, spec), retained) in specs.iter().zip(retained_checks.iter()) {
        let (retained_scope, retained_turn, retained_request) = match retained {
            RetainedCheck::Eligible {
                scope,
                turn,
                digest,
            } => (Some(scope.as_str()), Some(*turn), Some(digest.as_str())),
            _ => (None, None, None),
        };
        sqlx::query(
            "INSERT INTO tasks.attempts \
             (id,task_id,operation,expected_revision,staffing_group,fork,config_sha,submit_body, \
              prompt_body,initial_deadline,deadline,unbounded_running,status,retained_scope,retained_turn,retained_request) \
             VALUES ($1,$2,'plan_qc',$3,$4,$5,$6,$7,$8,$9,$9,$10,'ready',$11,$12,$13)",
        )
        .bind(spec.id)
        .bind(spec.task.0)
        .bind(i64::try_from(expected_revision)?)
        .bind(&spec.staffing_group)
        .bind(&spec.fork)
        .bind(&spec.config_sha)
        .bind(&spec.submit_body)
        .bind(&spec.prompt_body)
        .bind(spec.deadline)
        .bind(spec.unbounded_running)
        .bind(retained_scope)
        .bind(retained_turn)
        .bind(retained_request)
        .execute(uow.connection())
        .await?;
        sqlx::query(
            "UPDATE tasks.plan_review_seats SET attempt_id=$3,status='admitted' \
             WHERE batch_id=$1 AND reviewer_id=$2 AND attempt_id IS NULL",
        )
        .bind(batch_id)
        .bind(reviewer_id)
        .bind(spec.id)
        .execute(uow.connection())
        .await?;
        record_funding_grant(uow, spec).await?;
        events::append_turn_submitted(uow, outbox, current.id, spec, "plan_qc").await?;
        let effect_id: i64 = sqlx::query_scalar(
            "INSERT INTO tasks.effects (turn_id,kind,status) VALUES ($1,'submit','recorded') RETURNING id",
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
    sqlx::query("UPDATE tasks.plan_review_batches SET status='admitted' WHERE id=$1")
        .bind(batch_id)
        .execute(uow.connection())
        .await?;
    append_task_snapshot(uow, outbox, &current).await?;
    Ok(Admission::Admitted)
}

/// Charge one private correction while preserving every sibling review seat.
pub async fn admit_design_correction(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    issue: IssueKey,
    batch_id: Uuid,
    reviewer_id: &str,
    count: i32,
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
    let seat: Option<(Uuid, String, String)> = sqlx::query_as(
        "SELECT b.task_id,b.binding,s.status FROM tasks.plan_review_batches b \
         JOIN tasks.plan_review_seats s ON s.batch_id=b.id \
         WHERE b.id=$1 AND b.status='admitted' AND s.reviewer_id=$2 \
         AND b.plan_id=(SELECT plan_id FROM tasks.plan_reports \
             WHERE task_id=b.task_id AND accepted AND current \
             ORDER BY recorded_at DESC,attempt_id DESC LIMIT 1) \
         AND ((s.status='correcting' AND s.correction_count=$3) \
           OR (s.status='retrying' AND s.failure_count=$3)) FOR UPDATE OF b,s",
    )
    .bind(batch_id)
    .bind(reviewer_id)
    .bind(count)
    .fetch_optional(uow.connection())
    .await?;
    let Some((task_id, binding, seat_status)) = seat else {
        return Ok(Admission::Stale);
    };
    if !matches!(seat_status.as_str(), "correcting" | "retrying")
        || current.id.0 != task_id
        || current.stage != Stage::PlanQc
        || spec.task != current.id
    {
        return Ok(Admission::Stale);
    }
    let retained = check_retained_attempt(uow, spec).await?;
    if let RetainedCheck::Refused(reason) = retained {
        return block_preflight(
            uow,
            outbox,
            &mut current,
            &format!("Retained continuation unavailable: {reason}"),
            Some(batch_id),
        )
        .await;
    }
    if matches!(retained, RetainedCheck::Replay) {
        return Ok(Admission::Existing);
    }
    let submission: TurnSubmission = serde_json::from_slice(&spec.submit_body)?;
    ensure!(
        submission.turn == spec.id
            && submission.payload["request"]["context"]["design_binding"] == binding
            && submission.payload["request"]["context"]["reviewer_identity"]["id"] == reviewer_id
            && (seat_status == "retrying"
                || submission.payload["request"]["context"]["review_validation_feedback"]
                    .is_object()),
        "review correction binding changed"
    );
    let pins = current
        .pins
        .as_ref()
        .ok_or_else(|| anyhow::anyhow!("task pins missing"))?;
    if billable_model_calls(uow, &current).await? >= pins.max_model_calls {
        current.retry_stage = Some(current.stage);
        current.stage = Stage::Blocked;
        current.stage_reason =
            Some("model-call budget exhausted during review correction".to_owned());
        current.revision += 1;
        sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
            .bind(current.id.0)
            .bind(i64::try_from(current.revision)?)
            .bind(Json(&current))
            .execute(uow.connection())
            .await?;
        sqlx::query("UPDATE tasks.plan_review_batches SET status='superseded' WHERE id=$1")
            .bind(batch_id)
            .execute(uow.connection())
            .await?;
        append_task_snapshot(uow, outbox, &current).await?;
        return Ok(Admission::BudgetExhausted);
    }
    validate_funding_admission(uow, &current, spec).await?;
    sqlx::query(
        "INSERT INTO tasks.staffing_groups (staffing_group,turn_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
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
        "INSERT INTO tasks.attempts \
         (id,task_id,operation,expected_revision,staffing_group,fork,config_sha,submit_body, \
          prompt_body,initial_deadline,deadline,unbounded_running,status,retained_scope,retained_turn,retained_request) \
         VALUES ($1,$2,'plan_qc',$3,$4,$5,$6,$7,$8,$9,$9,$10,'ready',$11,$12,$13)",
    )
    .bind(spec.id)
    .bind(spec.task.0)
    .bind(i64::try_from(current.revision)?)
    .bind(&spec.staffing_group)
    .bind(&spec.fork)
    .bind(&spec.config_sha)
    .bind(&spec.submit_body)
    .bind(&spec.prompt_body)
    .bind(spec.deadline)
    .bind(spec.unbounded_running)
    .bind(retained_scope)
    .bind(retained_turn)
    .bind(retained_request)
    .execute(uow.connection())
    .await?;
    sqlx::query(
        "UPDATE tasks.plan_review_seats SET attempt_id=$4,status='admitted' \
         WHERE batch_id=$1 AND reviewer_id=$2 \
         AND ((status='correcting' AND correction_count=$3) \
           OR (status='retrying' AND failure_count=$3))",
    )
    .bind(batch_id)
    .bind(reviewer_id)
    .bind(count)
    .bind(spec.id)
    .execute(uow.connection())
    .await?;
    record_funding_grant(uow, spec).await?;
    events::append_turn_submitted(uow, outbox, current.id, spec, "plan_qc").await?;
    let effect_id: i64 = sqlx::query_scalar(
        "INSERT INTO tasks.effects (turn_id,kind,status) VALUES ($1,'submit','recorded') RETURNING id",
    )
    .bind(spec.id)
    .fetch_one(uow.connection())
    .await?;
    enqueue_effect(uow, effect_id, 0, 0).await?;
    current.used_model_calls += 1;
    current.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(current.id.0)
        .bind(i64::try_from(current.revision)?)
        .bind(Json(&current))
        .execute(uow.connection())
        .await?;
    append_task_snapshot(uow, outbox, &current).await?;
    Ok(Admission::Admitted)
}

/// Retain one independent terminal result before any source-aware vote validation.
pub(crate) async fn settle_design_attempt(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    state: &TaskState,
    turn: Uuid,
) -> anyhow::Result<()> {
    let seat: Option<(Uuid, String, String, i32, i32, i32)> = sqlx::query_as(
        "SELECT s.batch_id,s.reviewer_id,s.status,s.failure_count,s.session_failure_count,s.session_epoch FROM tasks.plan_review_seats s \
         JOIN tasks.plan_review_batches b ON b.id=s.batch_id \
         WHERE s.attempt_id=$1 AND b.task_id=$2 AND b.status='admitted' FOR UPDATE OF s",
    )
    .bind(turn)
    .bind(state.id.0)
    .fetch_optional(uow.connection())
    .await?;
    let Some((batch, reviewer, seat_status, failure_count, session_failures, seat_epoch)) = seat
    else {
        return Ok(());
    };
    if seat_status != "admitted" {
        return Ok(());
    }
    let attempt: (String, Option<Json<Value>>, Option<Vec<u8>>, Option<String>, Vec<u8>) = sqlx::query_as(
        "SELECT status,result,result_raw,outcome_reason,submit_body FROM tasks.attempts WHERE id=$1 FOR UPDATE",
    )
    .bind(turn)
    .fetch_one(uow.connection())
    .await?;
    let (status, result, raw_result, reason, submit_body) = attempt;
    if !matches!(status.as_str(), "finished" | "unknown" | "cancelled") {
        return Ok(());
    }
    if status == "finished"
        && reason.is_none()
        && result
            .as_ref()
            .is_some_and(|result| result.0["outcome"] == "ok")
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
            "UPDATE tasks.plan_review_seats SET status='completed',report=$4,report_raw=$5, \
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
        enqueue_design_result(uow, turn).await?;
    } else {
        let credential_failure =
            status == "finished" && has_credential_exclusion(uow, turn).await?;
        let session_failure = definite_session_failure(
            &status,
            reason.as_deref(),
            result.as_ref().map(|result| &result.0),
            &submit_body,
        );
        let reason = reason.unwrap_or_else(|| {
            result
                .as_ref()
                .and_then(|result| result.0["outcome"].as_str())
                .map_or_else(
                    || format!("Reviewer turn ended with status: {status}"),
                    |outcome| format!("Reviewer turn ended with outcome: {outcome}"),
                )
        });
        let retry = state.stage == Stage::PlanQc
            && (credential_failure || (session_failure && session_failures < 1));
        let next_failures = failure_count + 1;
        let next_session_failures = session_failures + i32::from(session_failure);
        let session_epoch: i32 = if session_failure || credential_failure {
            sqlx::query_scalar(
                "INSERT INTO tasks.plan_review_sessions (task_id,reviewer_id,session_epoch) \
             VALUES ($1,$2,1) ON CONFLICT (task_id,reviewer_id) DO UPDATE \
             SET session_epoch=plan_review_sessions.session_epoch+1 \
             RETURNING session_epoch",
            )
            .bind(state.id.0)
            .bind(&reviewer)
            .fetch_one(uow.connection())
            .await?
        } else {
            seat_epoch
        };
        sqlx::query(
            "UPDATE tasks.plan_review_seats SET status=$4,validation_error=$5, \
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
            enqueue_design_correction(uow, batch, &reviewer, next_failures).await?;
        } else if state.stage == Stage::PlanQc {
            let mut blocked = state.clone();
            blocked.retry_stage = Some(blocked.stage);
            blocked.stage = Stage::Blocked;
            blocked.stage_reason = Some(format!(
                "Design reviewer {} failed: {reason}",
                &reviewer[..reviewer.len().min(8)]
            ));
            blocked.revision += 1;
            sqlx::query(
                "UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1 AND revision=$4",
            )
            .bind(state.id.0)
            .bind(i64::try_from(blocked.revision)?)
            .bind(Json(&blocked))
            .bind(i64::try_from(state.revision)?)
            .execute(uow.connection())
            .await?;
            append_task_snapshot(uow, outbox, &blocked).await?;
        }
    }
    Ok(())
}

/// Apply one source-checked vote while fencing changed plans and sibling results.
pub async fn apply_design_result(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    turn: Uuid,
    validated: Result<DesignReviewReport, String>,
) -> anyhow::Result<bool> {
    let Some(state) = lock_issue_for_turn(uow, turn).await? else {
        return Ok(false);
    };
    let seat: Option<(Uuid, String, String, String, i64, Json<Vec<DesignReviewer>>, String, Vec<u8>, i32, i32, Option<String>)> =
        sqlx::query_as(
            "SELECT b.id,b.plan_id,b.binding,b.phase,b.generation,b.roster,s.reviewer_id,s.report_raw,s.session_epoch, \
             s.correction_count,s.repair_signature \
             FROM tasks.plan_review_seats s JOIN tasks.plan_review_batches b ON b.id=s.batch_id \
             WHERE s.attempt_id=$1 AND s.status='completed' AND b.status='admitted' \
             FOR UPDATE OF b,s",
        )
        .bind(turn)
        .fetch_optional(uow.connection())
        .await?;
    let Some((
        batch,
        plan_id,
        binding,
        phase,
        generation,
        Json(roster),
        reviewer,
        raw,
        session_epoch,
        correction_count,
        repair_signature,
    )) = seat
    else {
        return Ok(false);
    };
    ensure!(
        roster.iter().any(|member| member.id == reviewer),
        "reviewer seat is outside the bound roster"
    );
    if state.stage != Stage::PlanQc {
        return Ok(false);
    }
    let current_plan: Option<(Json<InitializationSnapshot>, Vec<u8>)> = sqlx::query_as(
        "SELECT i.snapshot,p.raw_report_bytes FROM tasks.initialization_inputs i \
         JOIN tasks.plan_reports p ON p.task_id=i.task_id AND p.plan_id=$2 AND p.accepted AND p.current \
         WHERE i.task_id=$1 ORDER BY p.recorded_at DESC,p.attempt_id DESC LIMIT 1",
    )
    .bind(state.id.0)
    .bind(&plan_id)
    .fetch_optional(uow.connection())
    .await?;
    let Some((Json(snapshot), plan_bytes)) = current_plan else {
        return Ok(false);
    };
    let plan: Value = serde_json::from_slice(&plan_bytes)?;
    let latest_plan: Option<String> = sqlx::query_scalar(
        "SELECT plan_id FROM tasks.plan_reports WHERE task_id=$1 AND accepted AND current \
         ORDER BY recorded_at DESC,attempt_id DESC LIMIT 1",
    )
    .bind(state.id.0)
    .fetch_optional(uow.connection())
    .await?;
    let current_binding = design_binding(
        &plan,
        &plan_id,
        &snapshot,
        &roster,
        u64::try_from(generation)?,
        &phase,
        None,
        &[],
    )
    .map_err(anyhow::Error::msg)?;
    if latest_plan.as_deref() != Some(&plan_id) || current_binding != binding {
        sqlx::query("UPDATE tasks.plan_review_batches SET status='superseded' WHERE id=$1")
            .bind(batch)
            .execute(uow.connection())
            .await?;
        return Ok(false);
    }
    let report = match validated {
        Ok(report) => report,
        Err(error) => {
            let signature = concurro_tasks_core::python_digest(&serde_json::json!([
                error,
                serde_json::from_slice::<Value>(&raw)?
            ]))
            .map_err(anyhow::Error::msg)?;
            let max_repairs = snapshot.configuration["max_review_repairs"]
                .as_i64()
                .unwrap_or(0);
            let stored_error = error.replace('\0', "\\u0000");
            if repair_signature.as_deref() == Some(&signature)
                || i64::from(correction_count) >= max_repairs
            {
                sqlx::query(
                    "UPDATE tasks.plan_review_seats SET status='failed',validation_error=$2 \
                     WHERE attempt_id=$1 AND status='completed'",
                )
                .bind(turn)
                .bind(&stored_error)
                .execute(uow.connection())
                .await?;
                sqlx::query("UPDATE tasks.plan_review_batches SET status='superseded' WHERE id=$1")
                    .bind(batch)
                    .execute(uow.connection())
                    .await?;
                let mut escalated = state.clone();
                escalated.retry_stage = Some(escalated.stage);
                escalated.stage = Stage::Escalated;
                escalated.stage_reason = Some(
                    "Review output repair made no progress or exhausted its budget".to_owned(),
                );
                escalated.revision += 1;
                sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
                    .bind(state.id.0)
                    .bind(i64::try_from(escalated.revision)?)
                    .bind(Json(&escalated))
                    .execute(uow.connection())
                    .await?;
                append_task_snapshot(uow, outbox, &escalated).await?;
            } else {
                let next_count = correction_count + 1;
                sqlx::query(
                    "UPDATE tasks.plan_review_seats SET status='correcting',validation_error=$2, \
                     correction_count=$3,repair_signature=$4 \
                     WHERE attempt_id=$1 AND status='completed'",
                )
                .bind(turn)
                .bind(&stored_error)
                .bind(next_count)
                .bind(&signature)
                .execute(uow.connection())
                .await?;
                enqueue_design_correction(uow, batch, &reviewer, next_count).await?;
            }
            events::append_deferred_turn_finished(uow, outbox, turn, None).await?;
            return Ok(true);
        }
    };
    let mut operational_report = serde_json::to_value(&report)?;
    operational_json(&mut operational_report);
    let report: DesignReviewReport = serde_json::from_value(operational_report)?;
    let mut next_objection: i64 = sqlx::query_scalar(
        "SELECT COALESCE(MAX(sequence),0) FROM tasks.plan_objections WHERE task_id=$1",
    )
    .bind(state.id.0)
    .fetch_one(uow.connection())
    .await?;
    for update in &report.objection_updates {
        if update.id.is_empty() {
            next_objection += 1;
            let id = format!("O{}-{}", next_objection, &reviewer[..reviewer.len().min(8)]);
            sqlx::query(
                "INSERT INTO tasks.plan_objections \
                 (task_id,sequence,id,reviewer_id,detail,counterexample,closure,evidence,resolved,plan_id,phase) \
                 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,false,$9,$10)",
            )
            .bind(state.id.0)
            .bind(next_objection)
            .bind(id)
            .bind(&reviewer)
            .bind(&update.detail)
            .bind(&update.counterexample)
            .bind(&update.closure)
            .bind(&update.evidence)
            .bind(&plan_id)
            .bind(&phase)
            .execute(uow.connection())
            .await?;
        } else {
            let changed = sqlx::query(
                "UPDATE tasks.plan_objections SET detail=$4,counterexample=$5,closure=$6, \
                 evidence=$7,resolved=$8,plan_id=$9,phase=$10 \
                 WHERE task_id=$1 AND id=$2 AND reviewer_id=$3",
            )
            .bind(state.id.0)
            .bind(&update.id)
            .bind(&reviewer)
            .bind(&update.detail)
            .bind(&update.counterexample)
            .bind(&update.closure)
            .bind(&update.evidence)
            .bind(update.status == "resolved")
            .bind(&plan_id)
            .bind(&phase)
            .execute(uow.connection())
            .await?;
            ensure!(
                changed.rows_affected() == 1,
                "reviewer objection owner changed"
            );
        }
    }
    let first_pass = phase == "cooperative"
        && session_epoch == 0
        && plan["responses"].as_array().is_some_and(Vec::is_empty)
        && report.objection_updates.is_empty();
    sqlx::query(
        "UPDATE tasks.plan_review_seats SET status='accepted',report=$2,first_pass_eligible=$3 \
         WHERE attempt_id=$1 AND status='completed'",
    )
    .bind(turn)
    .bind(Json(&report))
    .bind(first_pass)
    .execute(uow.connection())
    .await?;
    events::append_task_report(uow, outbox, &state, turn, &raw, "plan_qc", "assessment").await?;
    let accepted: Vec<(Json<DesignReviewReport>, bool)> = sqlx::query_as(
        "SELECT report,first_pass_eligible FROM tasks.plan_review_seats \
         WHERE batch_id=$1 AND status='accepted' ORDER BY reviewer_id",
    )
    .bind(batch)
    .fetch_all(uow.connection())
    .await?;
    if accepted.len() != roster.len() {
        events::append_deferred_turn_finished(uow, outbox, turn, None).await?;
        return Ok(true);
    }
    let unanimous = accepted
        .iter()
        .all(|(Json(report), _)| report.decision == "approve" && report.risk != "high");
    let all_first_pass = accepted.iter().all(|(_, eligible)| *eligible);
    let mut next = state.clone();
    next.revision += 1;
    if unanimous && (phase == "fresh" || all_first_pass) {
        next.stage = Stage::Planned;
        next.stage_reason = Some("Unanimous independent design approval".to_owned());
    } else if unanimous {
        next.stage_reason = Some("Fresh independent review required".to_owned());
        record_design_batch(uow, &next, &snapshot, &plan, &plan_id, generation, "fresh").await?;
    } else if accepted
        .iter()
        .any(|(Json(report), _)| report.risk == "high" || report.decision == "escalate")
    {
        next.stage = Stage::Escalated;
        next.stage_reason = Some("Design review escalated consequential risk".to_owned());
    } else if accepted
        .iter()
        .any(|(Json(report), _)| report.decision == "reject")
    {
        next.stage = Stage::Rejected;
        next.stage_reason = Some("Design review rejected the plan".to_owned());
    } else if accepted
        .iter()
        .any(|(Json(report), _)| report.decision == "clarify")
    {
        next.stage = Stage::Clarify;
        next.stage_reason = Some("Design review needs a human answer".to_owned());
    } else {
        let base_allowance = state
            .pins
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("task pins missing"))?
            .max_plan_revisions;
        let (spent_revisions, extra_revisions): (i64, i64) = sqlx::query_as(
            "SELECT spent_revisions,extra_plan_revisions FROM tasks.initialization_inputs \
             WHERE task_id=$1 FOR UPDATE",
        )
        .bind(state.id.0)
        .fetch_one(uow.connection())
        .await?;
        if u64::try_from(spent_revisions)? >= base_allowance + u64::try_from(extra_revisions)? {
            next.stage = Stage::Escalated;
            next.stage_reason = Some(
                "Plan revision budget exhausted: design reviewers require a revision".to_owned(),
            );
        } else {
            sqlx::query(
                "UPDATE tasks.initialization_inputs SET spent_revisions=spent_revisions+1 \
                 WHERE task_id=$1",
            )
            .bind(state.id.0)
            .execute(uow.connection())
            .await?;
            next.stage = Stage::Plan;
            next.stage_reason = Some("Design reviewers require a plan revision".to_owned());
            enqueue_plan(uow, &next).await?;
        }
    }
    sqlx::query("UPDATE tasks.plan_review_batches SET status='settled' WHERE id=$1")
        .bind(batch)
        .execute(uow.connection())
        .await?;
    sqlx::query(
        "UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1 AND revision=$4",
    )
    .bind(state.id.0)
    .bind(i64::try_from(next.revision)?)
    .bind(Json(&next))
    .bind(i64::try_from(state.revision)?)
    .execute(uow.connection())
    .await?;
    append_task_snapshot(uow, outbox, &next).await?;
    events::append_deferred_turn_finished(uow, outbox, turn, None).await?;
    Ok(true)
}
