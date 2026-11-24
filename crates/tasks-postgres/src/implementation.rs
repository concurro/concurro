//! Approved implementation inputs and candidate result persistence.

use anyhow::{Context, ensure};
use concurro_db::{Database, UnitOfWork};
use concurro_events_postgres::Outbox;
use concurro_tasks_core::{
    DesignIssue, DesignReviewer, InitializationSnapshot, IssueKey, Stage, TaskState,
    legacy_requirements, python_digest,
};
use serde_json::{Value, json};
use sqlx::types::Json;
use uuid::Uuid;

use crate::{
    append_task_snapshot,
    effects::{definite_session_failure, operational_json},
    enqueue_implementation, enqueue_implementation_result, events, lock_issue_for_turn,
    sponsorship::has_credential_exclusion,
};

/// Columns read by `implementation_inputs`.
type ImplementationInputsPublishedRow = (String, String, Option<Json<Value>>, Option<Json<Value>>);

/// Columns read by `settle_development`.
type SettleDevelopmentAttemptRow = (
    String,
    Option<Json<Value>>,
    Option<Vec<u8>>,
    Option<String>,
    Vec<u8>,
);

/// Columns read by `apply_implementation_result`.
type ApplyImplementationResultFindingsRow = (String, String, Json<Value>, Option<Json<Value>>);

/// Read the durable implementer journal generation after a provider session failure.
pub async fn implementation_session_epoch(
    database: &Database,
    task: Uuid,
) -> Result<i32, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT implementation_session_epoch FROM tasks.initialization_inputs WHERE task_id=$1",
    )
    .bind(task)
    .fetch_one(database.pool())
    .await
}

/// Exact plan and controller authorization supplied to one implementer turn.
pub struct ImplementationInputs {
    /// Writer-approved plan identity.
    pub plan_id: String,
    /// Accepted planner response.
    pub plan: Value,
    /// Bound design and writer approval attestation.
    pub authorization: Value,
    /// Source commit for this implementation round.
    pub parent: String,
    /// Existing publication branch when a review requested fixes.
    pub branch: Option<String>,
    /// Last settled independent PR assessments.
    pub prior_reviews: Value,
    /// Durable finding ledger including owner dispositions.
    pub review_findings: Value,
    /// Description from the last published candidate.
    pub current_description: Value,
    /// Validation summary from the last published candidate.
    pub current_validation: Value,
}

/// A completed implementer response awaiting pinned source validation.
pub struct ImplementationResult {
    /// Exact exchange turn that supplied the response.
    pub turn: Uuid,
    /// Owning task identity.
    pub task_id: Uuid,
    /// Issue whose pinned source must be read.
    pub issue: IssueKey,
    /// Full result, including the native workspace extraction.
    pub result: Value,
}

/// Last rejected implementer output supplied to its next bounded correction.
pub struct ImplementationCorrection {
    /// Original result retained before source validation.
    pub result: Value,
    /// Controller feedback explaining the rejected candidate.
    pub reason: String,
    /// One-based number of rejected results in this implementation round.
    pub attempt: i64,
}

/// Source-checked implementer result or a bounded design reassessment request.
pub enum ImplementationAssessment {
    /// A candidate with validated native edits or a description-only repair.
    Candidate(Value),
    /// Structured evidence that the approved design needs another review.
    Reopen(DesignIssue),
    /// Invalid output to return as bounded correction feedback.
    Invalid(String),
}

/// Read only the current approval's latest invalid implementer result.
pub async fn latest_implementation_correction(
    database: &Database,
    task: Uuid,
) -> anyhow::Result<Option<ImplementationCorrection>> {
    let row: Option<(Vec<u8>, String, i64)> = sqlx::query_as(
        "SELECT r.raw_result,r.validation_error,count(*) OVER () \
         FROM tasks.implementation_reports r WHERE r.task_id=$1 AND r.status='rejected' \
         AND r.recorded_at >= (SELECT recorded_at FROM tasks.plan_approvals \
             WHERE task_id=$1 ORDER BY recorded_at DESC,request_id DESC LIMIT 1) \
         AND r.recorded_at > COALESCE((SELECT max(recorded_at) \
             FROM tasks.implementation_reports WHERE task_id=$1 AND status='accepted'), \
             '-infinity'::timestamptz) \
         ORDER BY r.recorded_at DESC,r.attempt_id DESC LIMIT 1",
    )
    .bind(task)
    .fetch_optional(database.pool())
    .await?;
    row.map(|(raw, reason, attempt)| {
        Ok(ImplementationCorrection {
            result: serde_json::from_slice(&raw)?,
            reason,
            attempt,
        })
    })
    .transpose()
}

/// Read a pending implementer result without applying it or writing Git objects.
pub async fn implementation_result(
    database: &Database,
    turn: Uuid,
) -> anyhow::Result<Option<ImplementationResult>> {
    let row: Option<(Uuid, i64, i64, Vec<u8>)> = sqlx::query_as(
        "SELECT r.task_id,t.repository_id,t.issue_number,r.raw_result \
         FROM tasks.implementation_reports r JOIN tasks.task_state t ON t.task_id=r.task_id \
         WHERE r.attempt_id=$1 AND r.status='pending'",
    )
    .bind(turn)
    .fetch_optional(database.pool())
    .await?;
    row.map(|(task_id, repository_id, number, raw)| {
        Ok(ImplementationResult {
            turn,
            task_id,
            issue: IssueKey {
                repository_id,
                number,
            },
            result: serde_json::from_slice(&raw)?,
        })
    })
    .transpose()
}

/// Read the approved plan and unanimous identities without changing task state.
pub async fn implementation_inputs(
    database: &Database,
    task: Uuid,
) -> anyhow::Result<Option<ImplementationInputs>> {
    let approval: Option<(String, String, Vec<u8>, Json<InitializationSnapshot>)> = sqlx::query_as(
        "SELECT a.plan_id,a.binding,p.raw_report_bytes,i.snapshot FROM tasks.plan_approvals a \
         JOIN tasks.plan_reports p ON p.task_id=a.task_id AND p.plan_id=a.plan_id AND p.accepted AND p.current \
         JOIN tasks.initialization_inputs i ON i.task_id=a.task_id \
         WHERE a.task_id=$1 ORDER BY a.recorded_at DESC,a.request_id DESC LIMIT 1",
    )
    .bind(task)
    .fetch_optional(database.pool())
    .await?;
    let Some((plan_id, binding, raw, Json(snapshot))) = approval else {
        return Ok(None);
    };
    let batch: Option<(i64, Json<Vec<DesignReviewer>>)> = sqlx::query_as(
        "SELECT generation,roster FROM tasks.plan_review_batches \
         WHERE task_id=$1 AND plan_id=$2 AND binding=$3 AND status='settled' \
         ORDER BY generation DESC,id DESC LIMIT 1",
    )
    .bind(task)
    .bind(&plan_id)
    .bind(&binding)
    .fetch_optional(database.pool())
    .await?;
    let Some((generation, Json(roster))) = batch else {
        return Ok(None);
    };
    let plan: Value = serde_json::from_slice(&raw)?;
    let charter: Value = serde_json::from_str(&snapshot.charter)?;
    let authorization = json!({
        "verified_by":"controller",
        "approval_kind":"human",
        "binding":binding,
        "plan_id":plan_id,
        "plan_digest":python_digest(&plan)?,
        "base":snapshot.pins.base_sha,
        "bundle_id":snapshot.bundle_id,
        "charter_id":python_digest(&charter)?,
        "requirements_digest":python_digest(&legacy_requirements(&snapshot.requirements))?,
        "generation":generation,
        "required_identities":roster,
        "successful_votes":roster.iter().map(|identity| json!({
            "identity":identity,"binding":binding,"decision":"approve"
        })).collect::<Vec<_>>(),
    });
    let published: Option<ImplementationInputsPublishedRow> = sqlx::query_as(
        "SELECT b.head,b.branch,r.candidate->'description',r.candidate->'validation' \
         FROM tasks.pr_review_batches b JOIN tasks.implementation_reports r \
             ON r.attempt_id=b.publication_attempt \
         WHERE b.task_id=$1 AND b.status='settled' \
         ORDER BY b.round DESC,b.id DESC LIMIT 1",
    )
    .bind(task)
    .fetch_optional(database.pool())
    .await?;
    let (parent, branch, current_description, current_validation) =
        if let Some((head, branch, description, validation)) = published {
            (
                head,
                Some(branch),
                description.map_or(Value::Null, |Json(value)| value),
                validation.map_or(Value::Null, |Json(value)| value),
            )
        } else {
            (
                snapshot.pins.base_sha.clone(),
                None,
                Value::Null,
                Value::Null,
            )
        };
    let reviews: Vec<(String, Json<Value>)> = sqlx::query_as(
        "SELECT s.reviewer_id,s.report FROM tasks.pr_review_seats s \
         JOIN tasks.pr_review_batches b ON b.id=s.batch_id \
         WHERE b.task_id=$1 AND b.head=$2 AND b.status='settled' AND s.status='accepted' \
         ORDER BY s.reviewer_id",
    )
    .bind(task)
    .bind(&parent)
    .fetch_all(database.pool())
    .await?;
    let findings: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT jsonb_build_object('id',id,'provider',reviewer_id, \
         'path',finding->'path','start_line',finding->'start_line', \
         'line',finding->'line','detail',finding->'detail') \
         FROM tasks.pr_review_findings WHERE task_id=$1 AND status='open' ORDER BY sequence",
    )
    .bind(task)
    .fetch_all(database.pool())
    .await?;
    Ok(Some(ImplementationInputs {
        plan_id,
        plan,
        authorization,
        parent,
        branch,
        prior_reviews: Value::Object(
            reviews
                .into_iter()
                .map(|(id, Json(report))| (id, report))
                .collect(),
        ),
        review_findings: json!(
            findings
                .into_iter()
                .map(|(Json(value),)| value)
                .collect::<Vec<_>>()
        ),
        current_description,
        current_validation,
    }))
}

/// Retain a completed implementer result before source-aware candidate validation.
pub(crate) async fn settle_development(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    state: &TaskState,
    turn: Uuid,
) -> anyhow::Result<()> {
    if state.stage != Stage::Implement || state.current_attempt != Some(turn) {
        return Ok(());
    }
    let attempt: Option<SettleDevelopmentAttemptRow> = sqlx::query_as(
        "SELECT status,result,result_raw,outcome_reason,submit_body FROM tasks.attempts \
             WHERE id=$1 AND status IN ('finished','unknown','cancelled')",
    )
    .bind(turn)
    .fetch_optional(uow.connection())
    .await?;
    let Some((status, result, raw_result, reason, submit_body)) = attempt else {
        return Ok(());
    };
    if status != "finished"
        || reason.is_some()
        || result
            .as_ref()
            .is_none_or(|result| result.0["outcome"] != "ok")
    {
        let credential_failure =
            status == "finished" && has_credential_exclusion(uow, turn).await?;
        let session_failure = definite_session_failure(
            &status,
            reason.as_deref(),
            result.as_ref().map(|result| &result.0),
            &submit_body,
        );
        if credential_failure || session_failure {
            let failures: i32 = sqlx::query_scalar(
                "UPDATE tasks.initialization_inputs SET \
                 implementation_session_epoch=implementation_session_epoch+1, \
                 implementation_failure_count=implementation_failure_count+$2 \
                 WHERE task_id=$1 RETURNING implementation_failure_count",
            )
            .bind(state.id.0)
            .bind(i32::from(session_failure))
            .fetch_one(uow.connection())
            .await
            .context("record implementer session failure")?;
            if credential_failure || failures == 1 {
                let next = TaskState {
                    revision: state.revision + 1,
                    stage_reason: Some(if credential_failure {
                        "Provider credential failed; selecting the next eligible entry".to_owned()
                    } else {
                        "Implementer provider session failed; retrying once".to_owned()
                    }),
                    current_attempt: None,
                    ..state.clone()
                };
                sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
                    .bind(state.id.0)
                    .bind(i64::try_from(next.revision)?)
                    .bind(Json(&next))
                    .execute(uow.connection())
                    .await?;
                enqueue_implementation(uow, &next).await?;
                append_task_snapshot(uow, outbox, &next).await?;
                return Ok(());
            }
        }
        let mut blocked = state.clone();
        blocked.retry_stage = Some(blocked.stage);
        blocked.stage = Stage::Blocked;
        blocked.stage_reason = Some(if session_failure {
            "Implementer provider session failed twice; a fresh session is prepared for retry"
                .to_owned()
        } else {
            reason.unwrap_or_else(|| {
                result
                    .as_ref()
                    .and_then(|result| result.0["outcome"].as_str())
                    .map_or_else(
                        || format!("Implementer turn ended with status: {status}"),
                        |outcome| format!("Implementer turn ended with outcome: {outcome}"),
                    )
            })
        });
        blocked.current_attempt = None;
        blocked.revision += 1;
        sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
            .bind(state.id.0)
            .bind(i64::try_from(blocked.revision)?)
            .bind(Json(&blocked))
            .execute(uow.connection())
            .await?;
        append_task_snapshot(uow, outbox, &blocked).await?;
        return Ok(());
    }
    sqlx::query(
        "UPDATE tasks.initialization_inputs SET implementation_failure_count=0 WHERE task_id=$1",
    )
    .bind(state.id.0)
    .execute(uow.connection())
    .await?;
    let raw = raw_result.unwrap_or(serde_json::to_vec(
        &result.expect("successful result checked").0,
    )?);
    sqlx::query(
        "INSERT INTO tasks.implementation_reports (attempt_id,task_id,raw_result) \
         VALUES ($1,$2,$3) ON CONFLICT (attempt_id) DO NOTHING",
    )
    .bind(turn)
    .bind(state.id.0)
    .bind(raw)
    .execute(uow.connection())
    .await?;
    enqueue_implementation_result(uow, turn).await?;
    Ok(())
}

/// Accept only a source-validated candidate under the current writer approval.
pub async fn apply_implementation_result(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    turn: Uuid,
    candidate: ImplementationAssessment,
) -> anyhow::Result<bool> {
    let Some(state) = lock_issue_for_turn(uow, turn)
        .await
        .context("lock implementer result issue")?
    else {
        return Ok(false);
    };
    if state.stage != Stage::Implement || state.current_attempt != Some(turn) {
        return Ok(false);
    }
    let pending: Option<(Vec<u8>,)> = sqlx::query_as(
        "SELECT raw_result FROM tasks.implementation_reports \
         WHERE attempt_id=$1 AND task_id=$2 AND status='pending' FOR UPDATE",
    )
    .bind(turn)
    .bind(state.id.0)
    .fetch_optional(uow.connection())
    .await
    .context("read pending implementer report")?;
    let Some((raw,)) = pending else {
        return Ok(false);
    };
    let approval: Option<(String, String)> = sqlx::query_as(
        "SELECT plan_id,binding FROM tasks.plan_approvals WHERE task_id=$1 \
         ORDER BY recorded_at DESC,request_id DESC LIMIT 1",
    )
    .bind(state.id.0)
    .fetch_optional(uow.connection())
    .await
    .context("read implementer approval")?;
    if approval.is_none()
        || approval
            != crate::design::approval_proof(uow, state.id.0)
                .await
                .context("prove implementer approval")?
    {
        return Ok(false);
    }
    let (plan_id, binding) = approval.expect("approval checked");
    let mut next = state.clone();
    next.revision += 1;
    next.current_attempt = None;
    match candidate {
        ImplementationAssessment::Candidate(mut candidate) => {
            ensure!(
                candidate["plan_id"] == plan_id
                    && candidate["binding"] == binding
                    && candidate["parent"] == sqlx::query_scalar::<_, String>(
                        "SELECT COALESCE((SELECT head FROM tasks.pr_review_batches \
                         WHERE task_id=$1 AND status='settled' ORDER BY round DESC,id DESC LIMIT 1),$2)")
                        .bind(state.id.0)
                        .bind(&state.pins.as_ref().expect("approved pins").base_sha)
                        .fetch_one(uow.connection()).await?,
                "candidate changed its approved plan or parent"
            );
            operational_json(&mut candidate);
            sqlx::query(
                "UPDATE tasks.implementation_reports SET status='accepted',candidate=$2 \
                 WHERE attempt_id=$1 AND status='pending'",
            )
            .bind(turn)
            .bind(Json(&candidate))
            .execute(uow.connection())
            .await?;
            next.stage = Stage::Publish;
            next.stage_reason = Some("Validated candidate awaits Git publication".to_owned());
            crate::enqueue_git_publication(uow, &next).await?;
            events::append_task_report(
                uow,
                outbox,
                &state,
                turn,
                &raw,
                "develop",
                "implementation",
            )
            .await?;
        }
        ImplementationAssessment::Reopen(issue) => {
            let Json(snapshot): Json<InitializationSnapshot> = sqlx::query_scalar(
                "SELECT snapshot FROM tasks.initialization_inputs WHERE task_id=$1 FOR UPDATE",
            )
            .bind(state.id.0)
            .fetch_one(uow.connection())
            .await
            .context("read reopening snapshot")?;
            let prior_head: Option<String> = sqlx::query_scalar(
                "SELECT head FROM tasks.pr_review_batches WHERE task_id=$1 AND status='settled' \
                 ORDER BY round DESC,id DESC LIMIT 1",
            )
            .bind(state.id.0)
            .fetch_optional(uow.connection())
            .await
            .context("read reopening PR head")?;
            let findings: Vec<ApplyImplementationResultFindingsRow> = sqlx::query_as(
                "SELECT id,status,finding,disposition FROM tasks.pr_review_findings \
                 WHERE task_id=$1 ORDER BY sequence",
            )
            .bind(state.id.0)
            .fetch_all(uow.connection())
            .await?;
            sqlx::query(
                "INSERT INTO tasks.implementation_reopens \
                 (turn_id,task_id,plan_id,binding,prior_snapshot,prior_head,design_issue,findings_snapshot) \
                 VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
            )
            .bind(turn)
            .bind(state.id.0)
            .bind(&plan_id)
            .bind(&binding)
            .bind(Json(&snapshot))
            .bind(prior_head)
            .bind(Json(&issue))
            .bind(Json(&findings))
            .execute(uow.connection())
            .await?;
            sqlx::query(
                "UPDATE tasks.implementation_reports SET status='reopened',candidate=$2 \
                 WHERE attempt_id=$1 AND status='pending'",
            )
            .bind(turn)
            .bind(Json(json!({"design_issue":issue})))
            .execute(uow.connection())
            .await?;
            sqlx::query("UPDATE tasks.plan_reports SET current=false WHERE task_id=$1 AND current")
                .bind(state.id.0)
                .execute(uow.connection())
                .await?;
            sqlx::query(
                "UPDATE tasks.pr_review_findings SET status='withdrawn' \
                 WHERE task_id=$1 AND status='open'",
            )
            .bind(state.id.0)
            .execute(uow.connection())
            .await?;
            let (spent, extra, reopens): (i64, i64, i64) = sqlx::query_as(
                "SELECT spent_revisions,extra_plan_revisions,spent_design_reopens \
                 FROM tasks.initialization_inputs WHERE task_id=$1",
            )
            .bind(state.id.0)
            .fetch_one(uow.connection())
            .await
            .context("read reopening budgets")?;
            let max_reopens = snapshot.configuration["max_design_reopens"]
                .as_u64()
                .unwrap_or(0);
            if u64::try_from(reopens)? >= max_reopens {
                next.stage = Stage::Escalated;
                next.stage_reason =
                    Some("Implementation design reopen allowance exhausted".to_owned());
            } else if u64::try_from(spent)?
                >= state
                    .pins
                    .as_ref()
                    .expect("approved pins")
                    .max_plan_revisions
                    + u64::try_from(extra)?
            {
                next.stage = Stage::Escalated;
                next.stage_reason =
                    Some("Implementation design reopen needs a plan revision".to_owned());
            } else {
                sqlx::query(
                    "UPDATE tasks.initialization_inputs SET \
                     spent_revisions=spent_revisions+1,spent_design_reopens=spent_design_reopens+1 \
                     WHERE task_id=$1",
                )
                .bind(state.id.0)
                .execute(uow.connection())
                .await?;
                next.stage = Stage::Plan;
                next.stage_reason = Some("Implementer reopened the approved design".to_owned());
                crate::enqueue_plan(uow, &next)
                    .await
                    .context("queue reopened plan")?;
            }
            events::append_task_report(
                uow,
                outbox,
                &state,
                turn,
                &raw,
                "develop",
                "implementation",
            )
            .await
            .context("publish implementer design issue")?;
        }
        ImplementationAssessment::Invalid(error) => {
            sqlx::query(
                "UPDATE tasks.implementation_reports SET status='rejected',validation_error=$2 \
                 WHERE attempt_id=$1 AND status='pending'",
            )
            .bind(turn)
            .bind(&error)
            .execute(uow.connection())
            .await?;
            let rejected: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM tasks.implementation_reports WHERE task_id=$1 \
                 AND recorded_at >= (SELECT recorded_at FROM tasks.plan_approvals \
                     WHERE task_id=$1 ORDER BY recorded_at DESC,request_id DESC LIMIT 1) \
                 AND status='rejected' AND recorded_at > COALESCE((SELECT max(recorded_at) \
                 FROM tasks.implementation_reports WHERE task_id=$1 AND status='accepted'), \
                 '-infinity'::timestamptz)",
            )
            .bind(state.id.0)
            .fetch_one(uow.connection())
            .await?;
            let allowance = state.pins.as_ref().expect("approved pins").max_fix_rounds;
            if u64::try_from(rejected)? <= allowance {
                next.stage_reason =
                    Some(format!("Implementation result needs correction: {error}"));
                crate::enqueue_implementation(uow, &next).await?;
            } else {
                next.retry_stage = Some(next.stage);
                next.stage = Stage::Blocked;
                next.stage_reason = Some(format!(
                    "Implementation repair allowance exhausted: {error}"
                ));
            }
        }
    }
    sqlx::query(
        "UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1 AND revision=$4",
    )
    .bind(state.id.0)
    .bind(i64::try_from(next.revision)?)
    .bind(Json(&next))
    .bind(i64::try_from(state.revision)?)
    .execute(uow.connection())
    .await?;
    append_task_snapshot(uow, outbox, &next)
        .await
        .context("append implementation decision snapshot")?;
    events::append_deferred_turn_finished(uow, outbox, turn, None)
        .await
        .context("append deferred implementer completion")?;
    Ok(true)
}
