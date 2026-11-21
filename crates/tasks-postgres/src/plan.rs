//! Atomic planner report acceptance and bounded output correction.

use std::collections::BTreeSet;

use concurro_db::{Database, UnitOfWork};
use concurro_events_postgres::Outbox;
use concurro_tasks_core::{
    InitializationSnapshot, Stage, TaskState, plan_identity, plan_notice_body, validate_plan,
};
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

use crate::{
    append_task_snapshot,
    effects::{definite_session_failure, operational_json},
    enqueue_intake, enqueue_plan, events, queue_task_comment,
    sponsorship::has_credential_exclusion,
};

/// Rejected planner output supplied to the next bounded correction turn.
pub struct PlanCorrection {
    /// Exact rejected planner response.
    pub report: Value,
    /// Validation reason given to the planner.
    pub reason: String,
    /// Invalid-output number within this initialization.
    pub attempt: i64,
}

/// Latest accepted plan and reviewer reports carried into a plan revision.
pub struct PlanRevision {
    /// Prior accepted plan body.
    pub plan: Value,
    /// Bound non-approval reports from its completed design batch.
    pub design_feedback: Vec<Value>,
}

/// Read the planner's durable session generation after a definite provider failure.
pub async fn planner_session_epoch(database: &Database, task: Uuid) -> Result<i32, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT planner_session_epoch FROM tasks.initialization_inputs WHERE task_id=$1",
    )
    .bind(task)
    .fetch_one(database.pool())
    .await
}

/// Read the previous plan and current design objections before a revision turn.
pub async fn latest_plan_revision(
    database: &Database,
    task: Uuid,
) -> anyhow::Result<Option<PlanRevision>> {
    let prior: Option<(String, Vec<u8>)> = sqlx::query_as(
        "SELECT plan_id,raw_report_bytes FROM tasks.plan_reports WHERE task_id=$1 AND accepted \
         AND (current OR initialization_revision=(SELECT task_revision FROM tasks.initialization_inputs WHERE task_id=$1)) \
         ORDER BY recorded_at DESC,attempt_id DESC LIMIT 1",
    )
    .bind(task)
    .fetch_optional(database.pool())
    .await?;
    let Some((plan_id, raw)) = prior else {
        return Ok(None);
    };
    let batch: Option<Uuid> = sqlx::query_scalar(
        "SELECT id FROM tasks.plan_review_batches WHERE task_id=$1 AND plan_id=$2 \
         AND status='settled' ORDER BY generation DESC,phase DESC,id DESC LIMIT 1",
    )
    .bind(task)
    .bind(&plan_id)
    .fetch_optional(database.pool())
    .await?;
    let mut feedback = if let Some(batch) = batch {
        let rows: Vec<(Json<Value>,)> = sqlx::query_as(
            "SELECT report FROM tasks.plan_review_seats WHERE batch_id=$1 AND status='accepted' \
             AND report->>'decision'<>'approve' ORDER BY reviewer_id",
        )
        .bind(batch)
        .fetch_all(database.pool())
        .await?;
        rows.into_iter().map(|(Json(report),)| report).collect()
    } else {
        Vec::new()
    };
    let reopened: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT s.report FROM tasks.pr_review_seats s \
         JOIN tasks.pr_review_batches b ON b.id=s.batch_id \
         WHERE b.task_id=$1 AND b.plan_id=$2 AND b.status='settled' \
         AND s.status='accepted' AND s.report->>'verdict'='reopen' \
         ORDER BY b.round,s.reviewer_id",
    )
    .bind(task)
    .bind(&plan_id)
    .fetch_all(database.pool())
    .await?;
    feedback.extend(reopened.into_iter().map(|(Json(report),)| report));
    Ok(Some(PlanRevision {
        plan: serde_json::from_slice(&raw)?,
        design_feedback: feedback,
    }))
}

/// Read the latest invalid report from the current initialization only.
pub async fn latest_plan_correction(
    database: &Database,
    task: Uuid,
) -> Result<Option<PlanCorrection>, sqlx::Error> {
    let row: Option<(Vec<u8>, String, i64)> = sqlx::query_as(
        "SELECT raw_report_bytes,validation_error,count(*) OVER () \
         FROM tasks.plan_reports WHERE task_id=$1 AND NOT accepted AND current \
         AND initialization_revision=( \
           SELECT task_revision FROM tasks.initialization_inputs WHERE task_id=$1) \
         AND recorded_at > COALESCE((SELECT max(recorded_at) FROM tasks.plan_reports \
           WHERE task_id=$1 AND accepted AND current),'-infinity'::timestamptz) \
         ORDER BY recorded_at DESC,attempt_id DESC LIMIT 1",
    )
    .bind(task)
    .fetch_optional(database.pool())
    .await?;
    row.map(|(raw, reason, attempt)| {
        Ok(PlanCorrection {
            report: serde_json::from_slice(&raw)
                .map_err(|error| sqlx::Error::Decode(Box::new(error)))?,
            reason,
            attempt,
        })
    })
    .transpose()
}

/// Adopt one eligible answer and continue the pending plan without a new intake.
pub async fn continue_planner_answer(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    issue: concurro_tasks_core::IssueKey,
    expected_revision: u64,
    fresh: &InitializationSnapshot,
    answer_comment_id: i64,
) -> anyhow::Result<bool> {
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
        return Ok(false);
    };
    if current.stage != Stage::Clarify || current.revision != expected_revision {
        return Ok(false);
    }
    let (initialization_revision, Json(original), spent_revisions, extra_revisions): (i64, Json<InitializationSnapshot>, i64, i64) =
        sqlx::query_as(
            "SELECT task_revision,snapshot,spent_revisions,extra_plan_revisions FROM tasks.initialization_inputs WHERE task_id=$1 FOR UPDATE",
        )
        .bind(current.id.0)
        .fetch_one(uow.connection())
        .await?;
    let questions: Option<(Uuid, Json<Value>)> = sqlx::query_as(
        "SELECT attempt_id,raw_report->'questions' FROM tasks.plan_reports \
         WHERE task_id=$1 AND accepted AND current ORDER BY recorded_at DESC,attempt_id DESC LIMIT 1",
    )
    .bind(current.id.0)
    .fetch_optional(uow.connection())
    .await?;
    let Some((plan_turn, Json(questions))) = questions else {
        return Ok(false);
    };
    if questions.as_array().is_none_or(Vec::is_empty) {
        return Ok(false);
    }
    let notice_id = Uuid::new_v5(
        &Uuid::NAMESPACE_OID,
        format!("task-comment:{}:plan:{plan_turn}", current.id.0).as_bytes(),
    );
    let notice: Option<(String, Option<i64>)> = sqlx::query_as(
        "SELECT status,comment_id FROM tasks.publication_comments \
         WHERE id=$1 AND task_id=$2 AND kind='notice'",
    )
    .bind(notice_id)
    .bind(current.id.0)
    .fetch_optional(uow.connection())
    .await?;
    let Some((status, Some(notice_comment_id))) = notice else {
        return Ok(false);
    };
    if status != "written" {
        return Ok(false);
    }
    let mut baseline = fresh.clone();
    baseline.requirements = original.requirements.clone();
    let old_discussion = &original.requirements.discussion;
    let new_discussion = &fresh.requirements.discussion;
    let one_answer = original.requirements.title == fresh.requirements.title
        && original.requirements.body == fresh.requirements.body
        && ((new_discussion.len() == old_discussion.len() + 1
            && new_discussion.starts_with(old_discussion))
            || (old_discussion.len() == 20
                && new_discussion.len() == 20
                && new_discussion[..19] == old_discussion[1..]));
    let valid = baseline == original && one_answer && answer_comment_id > notice_comment_id;
    let allowance = current
        .pins
        .as_ref()
        .map_or(0, |pins| pins.max_plan_revisions)
        + u64::try_from(extra_revisions)?;
    let reassess = !valid && u64::try_from(spent_revisions)? < allowance;
    let next = TaskState {
        stage: if valid {
            Stage::Plan
        } else if reassess {
            Stage::Intake
        } else {
            Stage::Escalated
        },
        revision: current.revision + 1,
        stage_reason: Some(if valid {
            "Planner question answered; continuing plan".to_owned()
        } else if reassess {
            "Changed planner inputs require a fresh intake assessment".to_owned()
        } else {
            "Plan revision budget exhausted before changed inputs can be reassessed".to_owned()
        }),
        pins: if reassess {
            Some(fresh.pins.clone())
        } else {
            current.pins.clone()
        },
        ..current.clone()
    };
    sqlx::query(
        "UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1 AND revision=$4",
    )
    .bind(current.id.0)
    .bind(i64::try_from(next.revision)?)
    .bind(Json(&next))
    .bind(i64::try_from(current.revision)?)
    .execute(uow.connection())
    .await?;
    if valid || reassess {
        sqlx::query(
            "UPDATE tasks.initialization_inputs SET task_revision=$2,snapshot=$3, \
             spent_revisions=spent_revisions+$5 \
             WHERE task_id=$1 AND task_revision=$4",
        )
        .bind(current.id.0)
        .bind(i64::try_from(next.revision)?)
        .bind(Json(fresh))
        .bind(initialization_revision)
        .bind(i64::from(reassess))
        .execute(uow.connection())
        .await?;
        if reassess {
            sqlx::query("UPDATE tasks.plan_reports SET current=false WHERE task_id=$1 AND current")
                .bind(current.id.0)
                .execute(uow.connection())
                .await?;
            sqlx::query(
                "UPDATE tasks.plan_review_batches SET status='superseded' \
                         WHERE task_id=$1 AND status IN ('recorded','admitted','settled')",
            )
            .bind(current.id.0)
            .execute(uow.connection())
            .await?;
            enqueue_intake(uow, &next, None).await?;
        } else {
            enqueue_plan(uow, &next).await?;
        }
    }
    append_task_snapshot(uow, outbox, &next).await?;
    Ok(true)
}

/// Apply only the finished planner attempt currently owned by the task.
pub(crate) async fn settle_plan(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    state: &TaskState,
    turn: Uuid,
) -> anyhow::Result<()> {
    if state.stage != Stage::Plan || state.current_attempt != Some(turn) {
        return Ok(());
    }
    let attempt: Option<(
        String,
        Option<Json<Value>>,
        Option<Vec<u8>>,
        Option<String>,
        Vec<u8>,
    )> = sqlx::query_as(
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
        if status == "finished" && has_credential_exclusion(uow, turn).await? {
            sqlx::query(
                "UPDATE tasks.initialization_inputs SET planner_session_epoch=planner_session_epoch+1 \
                 WHERE task_id=$1",
            )
            .bind(state.id.0)
            .execute(uow.connection())
            .await?;
            let next = TaskState {
                revision: state.revision + 1,
                stage_reason: Some(
                    "Provider credential failed; selecting the next eligible entry".to_owned(),
                ),
                current_attempt: None,
                ..state.clone()
            };
            save_plan_state(uow, outbox, state, &next).await?;
            enqueue_plan(uow, &next).await?;
            return Ok(());
        }
        let session_failure = definite_session_failure(
            &status,
            reason.as_deref(),
            result.as_ref().map(|result| &result.0),
            &submit_body,
        );
        if session_failure {
            let failures: i32 = sqlx::query_scalar(
                "UPDATE tasks.initialization_inputs SET \
                 planner_session_epoch=planner_session_epoch+1, \
                 planner_failure_count=planner_failure_count+1 \
                 WHERE task_id=$1 RETURNING planner_failure_count",
            )
            .bind(state.id.0)
            .fetch_one(uow.connection())
            .await?;
            if failures == 1 {
                let next = TaskState {
                    revision: state.revision + 1,
                    stage_reason: Some("Planner provider session failed; retrying once".to_owned()),
                    current_attempt: None,
                    ..state.clone()
                };
                save_plan_state(uow, outbox, state, &next).await?;
                enqueue_plan(uow, &next).await?;
                return Ok(());
            }
        }
        let next = TaskState {
            stage: Stage::Blocked,
            retry_stage: Some(Stage::Plan),
            revision: state.revision + 1,
            stage_reason: Some(if session_failure {
                "Planner provider session failed twice; a fresh session is prepared for retry"
                    .to_owned()
            } else {
                reason.unwrap_or_else(|| {
                    result
                        .as_ref()
                        .and_then(|result| result.0["outcome"].as_str())
                        .map_or_else(
                            || format!("Planner turn ended with status: {status}"),
                            |outcome| format!("Planner turn ended with outcome: {outcome}"),
                        )
                })
            }),
            current_attempt: None,
            ..state.clone()
        };
        save_plan_state(uow, outbox, state, &next).await?;
        return Ok(());
    }
    sqlx::query("UPDATE tasks.initialization_inputs SET planner_failure_count=0 WHERE task_id=$1")
        .bind(state.id.0)
        .execute(uow.connection())
        .await?;
    let result: Value = raw_result.map_or_else(
        || Ok(result.expect("successful result checked").0),
        |raw| serde_json::from_slice(&raw),
    )?;
    let report = result.get("response").cloned().unwrap_or(Value::Null);
    let raw = serde_json::to_vec(&report)?;
    let mut projected = report.clone();
    operational_json(&mut projected);
    let (initialization_revision, Json(snapshot)): (i64, Json<InitializationSnapshot>) =
        sqlx::query_as(
            "SELECT task_revision,snapshot FROM tasks.initialization_inputs WHERE task_id=$1",
        )
        .bind(state.id.0)
        .fetch_one(uow.connection())
        .await?;
    let mut error = validate_plan(&report, &snapshot.configuration).err();
    if error.is_none() {
        let open_ids: Vec<String> = sqlx::query_scalar(
            "SELECT id FROM tasks.plan_objections WHERE task_id=$1 AND NOT resolved ORDER BY sequence",
        )
        .bind(state.id.0)
        .fetch_all(uow.connection())
        .await?;
        let response_ids = report["responses"]
            .as_array()
            .expect("validated planner responses")
            .iter()
            .filter_map(|response| response["id"].as_str())
            .collect::<BTreeSet<_>>();
        let expected = open_ids.iter().map(String::as_str).collect::<BTreeSet<_>>();
        if response_ids != expected
            || report["responses"]
                .as_array()
                .expect("validated responses")
                .len()
                != expected.len()
        {
            error = Some("planner must respond to every open objection exactly once".to_owned());
        }
    }
    let plan_id = if error.is_none() {
        Some(plan_identity(&report, &snapshot).map_err(anyhow::Error::msg)?)
    } else {
        None
    };
    let inserted = sqlx::query(
        "INSERT INTO tasks.plan_reports \
         (attempt_id,task_id,initialization_revision,raw_report,raw_report_bytes,accepted,validation_error,plan_id) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (attempt_id) DO UPDATE \
         SET attempt_id=EXCLUDED.attempt_id WHERE plan_reports.task_id=EXCLUDED.task_id \
         AND plan_reports.initialization_revision=EXCLUDED.initialization_revision \
         AND plan_reports.raw_report=EXCLUDED.raw_report \
         AND plan_reports.raw_report_bytes=EXCLUDED.raw_report_bytes \
         AND plan_reports.accepted=EXCLUDED.accepted \
         AND plan_reports.validation_error IS NOT DISTINCT FROM EXCLUDED.validation_error \
         AND plan_reports.plan_id IS NOT DISTINCT FROM EXCLUDED.plan_id",
    )
    .bind(turn)
    .bind(state.id.0)
    .bind(initialization_revision)
    .bind(Json(&projected))
    .bind(&raw)
    .bind(error.is_none())
    .bind(error.as_deref())
    .bind(plan_id.as_deref())
    .execute(uow.connection())
    .await?;
    anyhow::ensure!(
        inserted.rows_affected() == 1,
        "planner report changed on replay"
    );
    if error.is_none() {
        events::append_task_report(uow, outbox, state, turn, &raw, "plan", "draft").await?;
    }
    let next = if let Some(error) = error {
        let invalid_count: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM tasks.plan_reports WHERE task_id=$1 \
             AND initialization_revision=$2 AND NOT accepted AND current \
             AND recorded_at > COALESCE((SELECT max(recorded_at) FROM tasks.plan_reports \
               WHERE task_id=$1 AND accepted AND current),'-infinity'::timestamptz)",
        )
        .bind(state.id.0)
        .bind(initialization_revision)
        .fetch_one(uow.connection())
        .await?;
        let exhausted = invalid_count > 2;
        TaskState {
            stage: if exhausted {
                Stage::Escalated
            } else {
                Stage::Plan
            },
            retry_stage: exhausted.then_some(Stage::Plan),
            revision: state.revision + 1,
            stage_reason: Some(if exhausted {
                format!("Planner output still needs correction after two retries: {error}")
            } else {
                format!("Planner output needs correction: {error}")
            }),
            current_attempt: None,
            ..state.clone()
        }
    } else {
        let questions = report["questions"].as_array().expect("validated questions");
        TaskState {
            stage: if questions.is_empty() {
                Stage::PlanQc
            } else {
                Stage::Clarify
            },
            revision: state.revision + 1,
            stage_reason: (!questions.is_empty())
                .then(|| "Planner questions need answers".to_owned()),
            current_attempt: None,
            ..state.clone()
        }
    };
    save_plan_state(uow, outbox, state, &next).await?;
    if next.stage == Stage::Plan {
        enqueue_plan(uow, &next).await?;
    } else if next.stage == Stage::PlanQc {
        let generation: i64 = sqlx::query_scalar(
            "SELECT GREATEST(COALESCE(MAX(generation)+1,0), \
             (SELECT spent_revisions FROM tasks.initialization_inputs WHERE task_id=$1)) \
             FROM tasks.plan_review_batches WHERE task_id=$1",
        )
        .bind(state.id.0)
        .fetch_one(uow.connection())
        .await?;
        crate::design::record_design_batch(
            uow,
            &next,
            &snapshot,
            &report,
            plan_id.as_deref().expect("validated plan has identity"),
            generation,
            "cooperative",
        )
        .await?;
    } else if next.stage == Stage::Clarify {
        if let Some(body) = plan_notice_body(&report) {
            let notice_id =
                queue_task_comment(uow, &next, "notice", &format!("plan:{turn}"), &body).await?;
            events::append_notice(uow, outbox, &next, notice_id, "plan", &body).await?;
        }
    }
    Ok(())
}

/// Save the task revision and its public projection in the same result transaction.
async fn save_plan_state(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    prior: &TaskState,
    next: &TaskState,
) -> anyhow::Result<()> {
    let updated = sqlx::query(
        "UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1 AND revision=$4",
    )
    .bind(next.id.0)
    .bind(i64::try_from(next.revision)?)
    .bind(Json(next))
    .bind(i64::try_from(prior.revision)?)
    .execute(uow.connection())
    .await?;
    anyhow::ensure!(
        updated.rows_affected() == 1,
        "planner task revision changed"
    );
    append_task_snapshot(uow, outbox, next).await?;
    Ok(())
}
