//! Transactional intake report acceptance and correction scheduling.

use concurro_db::{Database, UnitOfWork};
use concurro_events_postgres::Outbox;
use concurro_tasks_core::{IntakeReport, Stage, TaskState, finish_intake, intake_notice_body};
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

use crate::{
    append_task_snapshot, effects::operational_json, enqueue_intake, enqueue_plan, events,
    queue_task_comment,
};

/// Rejected output retained for the next paid intake correction.
pub struct IntakeCorrection {
    /// Exact rejected report from the runner.
    pub report: Value,
    /// Validation reason returned to the runner.
    pub reason: String,
    /// Number of invalid reports in the current initialization.
    pub attempt: i64,
}

/// Read the latest invalid intake report from the task's current initialization.
pub async fn latest_intake_correction(
    database: &Database,
    task: Uuid,
) -> Result<Option<IntakeCorrection>, sqlx::Error> {
    let row: Option<(Vec<u8>, String, i64)> = sqlx::query_as(
        "SELECT raw_report_bytes,validation_error,count(*) OVER () \
         FROM tasks.intake_reports \
         WHERE task_id=$1 AND initialization_revision=( \
           SELECT task_revision FROM tasks.initialization_inputs WHERE task_id=$1 \
         ) AND NOT accepted \
         ORDER BY recorded_at DESC,attempt_id DESC LIMIT 1",
    )
    .bind(task)
    .fetch_optional(database.pool())
    .await?;
    row.map(|(raw, reason, attempt)| {
        Ok(IntakeCorrection {
            report: serde_json::from_slice(&raw)
                .map_err(|error| sqlx::Error::Decode(Box::new(error)))?,
            reason,
            attempt,
        })
    })
    .transpose()
}

/// Apply a finished current attempt's first recorded report or queue a bounded repair.
pub(crate) async fn settle_intake(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    state: &TaskState,
    turn: Uuid,
) -> anyhow::Result<()> {
    if state.stage != Stage::Intake || state.current_attempt != Some(turn) {
        return Ok(());
    }
    let result: Option<(String, Option<Json<Value>>, Option<Vec<u8>>, Option<String>)> = sqlx::query_as(
        "SELECT status,result,result_raw,outcome_reason FROM tasks.attempts WHERE id=$1 AND status IN ('finished','unknown','cancelled')",
    )
    .bind(turn)
    .fetch_optional(uow.connection())
    .await?;
    let Some((status, result, raw_result, reason)) = result else {
        return Ok(());
    };
    let result_outcome = result
        .as_ref()
        .and_then(|value| value.0["outcome"].as_str());
    let uncertain_result = matches!(result_outcome, Some("uncertain" | "lost" | "timed_out"));
    if status != "finished"
        || reason.is_some()
        || result
            .as_ref()
            .is_none_or(|value| value.0["outcome"] != "ok")
    {
        let unknown_count: i64 = if status == "unknown" || uncertain_result {
            sqlx::query_scalar(
                "SELECT count(*) FROM tasks.attempts a \
                 JOIN tasks.initialization_inputs i ON i.task_id=a.task_id \
                 WHERE a.task_id=$1 AND a.operation='intake' \
                 AND a.expected_revision>=i.task_revision \
                 AND (a.status='unknown' OR a.result->>'outcome' IN ('uncertain','lost','timed_out'))",
            )
            .bind(state.id.0)
            .fetch_one(uow.connection())
            .await?
        } else {
            0
        };
        let retry_unknown = (status == "unknown" || uncertain_result)
            && reason.as_deref() != Some("poll_failures_exhausted")
            && unknown_count < 2;
        let next = TaskState {
            stage: if retry_unknown {
                Stage::Intake
            } else {
                Stage::Blocked
            },
            retry_stage: (!retry_unknown).then_some(Stage::Intake),
            revision: state.revision + 1,
            stage_reason: Some(reason.unwrap_or_else(|| {
                if status == "finished" {
                    result_outcome.map_or_else(
                        || "Intake turn finished without an accepted model result".to_owned(),
                        |outcome| format!("Intake turn ended with outcome: {outcome}"),
                    )
                } else {
                    format!("Intake turn ended with uncertain status: {status}")
                }
            })),
            current_attempt: None,
            ..state.clone()
        };
        sqlx::query(
            "UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1 AND revision=$4",
        )
        .bind(next.id.0)
        .bind(i64::try_from(next.revision)?)
        .bind(Json(&next))
        .bind(i64::try_from(state.revision)?)
        .execute(uow.connection())
        .await?;
        append_task_snapshot(uow, outbox, &next).await?;
        if let Some(body) = intake_notice_body(&next, None) {
            let notice_id =
                queue_task_comment(uow, &next, "notice", &format!("intake:{turn}"), &body).await?;
            events::append_notice(uow, outbox, &next, notice_id, "intake", &body).await?;
        }
        return Ok(());
    }
    let result: Value = match raw_result {
        Some(raw) => serde_json::from_slice(&raw)?,
        None => result.expect("accepted result checked above").0,
    };
    let raw_report = result.get("response").cloned().unwrap_or(Value::Null);
    let raw_report_bytes = serde_json::to_vec(&raw_report)?;
    let mut stored_report = raw_report.clone();
    operational_json(&mut stored_report);
    let snapshot: (i64, Json<concurro_tasks_core::InitializationSnapshot>) = sqlx::query_as(
        "SELECT task_revision,snapshot FROM tasks.initialization_inputs WHERE task_id=$1",
    )
    .bind(state.id.0)
    .fetch_one(uow.connection())
    .await?;
    let parsed = IntakeReport::parse(raw_report.clone(), &snapshot.1.0.charter);
    let mut parsed = parsed;
    if let Ok(report) = &mut parsed {
        let mut projected = serde_json::to_value(&*report)?;
        operational_json(&mut projected);
        *report = serde_json::from_value(projected)?;
    }
    let error = parsed.as_ref().err().cloned();
    let updated = sqlx::query(
        "INSERT INTO tasks.intake_reports (attempt_id,task_id,initialization_revision,raw_report,raw_report_bytes,accepted,validation_error) \
         VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (attempt_id) DO UPDATE \
         SET attempt_id=EXCLUDED.attempt_id \
         WHERE intake_reports.initialization_revision=EXCLUDED.initialization_revision \
         AND intake_reports.raw_report=EXCLUDED.raw_report \
         AND intake_reports.raw_report_bytes=EXCLUDED.raw_report_bytes \
         AND intake_reports.accepted=EXCLUDED.accepted \
         AND intake_reports.validation_error IS NOT DISTINCT FROM EXCLUDED.validation_error",
    )
    .bind(turn)
    .bind(state.id.0)
    .bind(snapshot.0)
    .bind(Json(&stored_report))
    .bind(&raw_report_bytes)
    .bind(parsed.is_ok())
    .bind(error.as_deref())
    .execute(uow.connection())
    .await?;
    anyhow::ensure!(
        updated.rows_affected() == 1,
        "intake report changed on replay"
    );
    if parsed.is_ok() {
        events::append_task_report(
            uow,
            outbox,
            state,
            turn,
            &raw_report_bytes,
            "intake",
            "assessment",
        )
        .await?;
    }
    let accepted_report = parsed.as_ref().ok().cloned();
    let next = match parsed {
        Ok(report) => finish_intake(state, turn, &report).expect("current intake attempt"),
        Err(error) => {
            let invalid_count: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM tasks.intake_reports WHERE task_id=$1 AND initialization_revision=$2 AND NOT accepted",
            )
            .bind(state.id.0)
            .bind(snapshot.0)
            .fetch_one(uow.connection())
            .await?;
            let exhausted = invalid_count > 2;
            TaskState {
                stage: if exhausted {
                    Stage::Escalated
                } else {
                    Stage::Intake
                },
                retry_stage: exhausted.then_some(Stage::Intake),
                revision: state.revision + 1,
                stage_reason: Some(if exhausted {
                    format!("Intake output still needs correction after two retries: {error}")
                } else {
                    format!("Intake output needs correction: {error}")
                }),
                current_attempt: None,
                ..state.clone()
            }
        }
    };
    sqlx::query(
        "UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1 AND revision=$4",
    )
    .bind(next.id.0)
    .bind(i64::try_from(next.revision)?)
    .bind(Json(&next))
    .bind(i64::try_from(state.revision)?)
    .execute(uow.connection())
    .await?;
    append_task_snapshot(uow, outbox, &next).await?;
    if let Some(body) = intake_notice_body(&next, accepted_report.as_ref()) {
        let notice_id =
            queue_task_comment(uow, &next, "notice", &format!("intake:{turn}"), &body).await?;
        events::append_notice(uow, outbox, &next, notice_id, "intake", &body).await?;
    }
    if next.stage == Stage::Intake {
        enqueue_intake(uow, &next, None).await?;
    } else if next.stage == Stage::Plan {
        enqueue_plan(uow, &next).await?;
    }
    Ok(())
}
