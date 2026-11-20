//! Atomic task, notice, report and turn events from durable state transitions.

use anyhow::Result;
use chrono::{DateTime, Utc};
use concurro_contracts::{
    Envelope, EventId, TaskNotice, TaskReport, Timestamp, TurnFinished, TurnRunner, TurnStatus,
    TurnSubmitted, TurnTranscript,
};
use concurro_db::UnitOfWork;
use concurro_events_postgres::Outbox;
use concurro_exchange_v1::TurnSubmission;
use concurro_tasks_core::{AttemptSpec, TaskState};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::types::Json;
use uuid::Uuid;

use crate::artifacts::record_turn_artifact;

/// Convert an exchange epoch second into an aware event timestamp.
fn exchange_time(seconds: Option<f64>) -> Option<Timestamp> {
    seconds
        .filter(|seconds| seconds.is_finite())
        .and_then(|seconds| DateTime::<Utc>::from_timestamp_millis((seconds * 1000.0) as i64))
        .map(Timestamp)
}

/// Publish one admitted turn and content-address its redacted inputs.
pub(crate) async fn append_turn_submitted(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    task: &TaskState,
    spec: &AttemptSpec,
) -> Result<()> {
    let submission = serde_json::from_slice::<TurnSubmission>(&spec.submit_body).ok();
    let payload = submission.as_ref().map(|submission| &submission.payload);
    let request = payload.map(|payload| &payload["request"]);
    let spec_body = payload
        .map(|payload| payload["spec"].clone())
        .unwrap_or_else(|| json!({"fork":spec.fork,"config_sha":spec.config_sha}));
    let spec_ref = record_turn_artifact(
        uow,
        spec.id,
        &serde_json::to_vec(&spec_body)?,
        "application/json",
    )
    .await?;
    let prompt_ref = match &spec.prompt_body {
        Some(prompt) => Some(record_turn_artifact(uow, spec.id, prompt, "application/json").await?),
        None => None,
    };
    let bundle_id = request
        .and_then(|request| request["bundle_id"].as_str())
        .unwrap_or_default()
        .to_owned();
    let data = TurnSubmitted {
        turn_id: spec.id,
        attempt_id: spec.id,
        task_id: task.id,
        grant_id: submission
            .as_ref()
            .and_then(|submission| submission.binding["grant_id"].as_str())
            .unwrap_or("org")
            .to_owned(),
        stage: "intake".to_owned(),
        role: submission
            .as_ref()
            .and_then(|submission| submission.binding["role"].as_str())
            .unwrap_or("intake")
            .to_owned(),
        harness: request
            .and_then(|request| request["provider"].as_str())
            .unwrap_or("unknown")
            .to_owned(),
        model: request
            .and_then(|request| request["model"].as_str())
            .unwrap_or("unknown")
            .to_owned(),
        effort: request.and_then(|request| request["effort"].as_str()).map(str::to_owned),
        profile: request.and_then(|request| request["agent"]["profile"].as_str()).map(str::to_owned),
        session: request.and_then(|request| request.get("session")).filter(|session| !session.is_null()).cloned(),
        bundle_id,
        limits: submission.as_ref().map_or_else(
            || json!({"deadline":spec.deadline}),
            |submission| json!({"budget":submission.budget,"lease":submission.lease,"deadline":spec.deadline}),
        ),
        spec: spec_ref,
        prompt: prompt_ref,
        submitted_at: Timestamp(Utc::now()),
    };
    let identity = format!("turn-submitted:{}", spec.id);
    outbox
        .append(
            uow,
            &Envelope::fact(
                EventId(Uuid::new_v5(&Uuid::NAMESPACE_OID, identity.as_bytes())),
                "tasks",
                Timestamp(Utc::now()),
                data,
            ),
        )
        .await?;
    Ok(())
}

/// Publish one task notice with the same identity as its managed comment.
pub(crate) async fn append_notice(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    task: &TaskState,
    notice_id: Uuid,
    kind: &str,
    body: &str,
) -> Result<()> {
    let identity = format!("task-notice:{notice_id}:1");
    let data = TaskNotice {
        task_id: task.id,
        notice_id,
        kind: kind.to_owned(),
        state: "open".to_owned(),
        data: json!({"markdown":body}),
    };
    outbox
        .append(
            uow,
            &Envelope::snapshot(
                EventId(Uuid::new_v5(&Uuid::NAMESPACE_OID, identity.as_bytes())),
                "tasks",
                Timestamp(Utc::now()),
                format!("task:{}:notice:{notice_id}", task.id.0),
                1,
                data,
            ),
        )
        .await?;
    Ok(())
}

/// Publish the report body that intake validated in this transaction.
pub(crate) async fn append_intake_report(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    task: &TaskState,
    turn: Uuid,
    raw: &[u8],
) -> Result<()> {
    let report_id = Uuid::new_v5(
        &Uuid::NAMESPACE_OID,
        format!("task-report:{turn}").as_bytes(),
    );
    let identity = format!("task-report:{report_id}:1");
    let body = serde_json::from_slice(raw)?;
    let data = TaskReport {
        task_id: task.id,
        report_id,
        kind: "intake".to_owned(),
        step: "assessment".to_owned(),
        body,
        digest: hex::encode(Sha256::digest(raw)),
    };
    outbox
        .append(
            uow,
            &Envelope::snapshot(
                EventId(Uuid::new_v5(&Uuid::NAMESPACE_OID, identity.as_bytes())),
                "tasks",
                Timestamp(Utc::now()),
                format!("task:{}:report:{report_id}", task.id.0),
                1,
                data,
            ),
        )
        .await?;
    Ok(())
}

/// Publish a changed turn lifecycle state using its monotonic revision.
pub(crate) async fn append_turn_status(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    turn: Uuid,
) -> Result<()> {
    let revision: i64 = sqlx::query_scalar(
        "UPDATE tasks.attempts SET status_revision=status_revision+1 \
         WHERE id=$1 RETURNING status_revision",
    )
    .bind(turn)
    .fetch_one(uow.connection())
    .await?;
    let (status, observation): (String, Option<Json<Value>>) =
        sqlx::query_as("SELECT status,observation FROM tasks.attempts WHERE id=$1")
            .bind(turn)
            .fetch_one(uow.connection())
            .await?;
    let observed = observation.as_ref().map(|value| &value.0);
    let runner = observed
        .and_then(|observed| {
            observed["runner"]["session"]
                .as_str()
                .or_else(|| observed["dispatch"].as_str())
        })
        .and_then(|session| Uuid::parse_str(session).ok())
        .map(|session_id| {
            let run = observed.and_then(|observed| observed["runner"]["run_id"].as_str());
            let split = run.and_then(|run| run.rsplit_once('/'));
            let run_attempt = split.and_then(|(_, attempt)| attempt.parse::<u32>().ok());
            TurnRunner {
                session_id,
                run_id: run.map(|run| {
                    if run_attempt.is_some() {
                        split.expect("parsed attempt has a separator").0.to_owned()
                    } else {
                        run.to_owned()
                    }
                }),
                run_attempt,
            }
        });
    let data = TurnStatus {
        turn_id: turn,
        status: if status == "unknown" {
            "finished".to_owned()
        } else {
            status
        },
        created_at: observed.and_then(|observed| exchange_time(observed["created"].as_f64())),
        started_at: observed.and_then(|observed| exchange_time(observed["started"].as_f64())),
        finished_at: observed.and_then(|observed| exchange_time(observed["finished"].as_f64())),
        runner,
    };
    let identity = format!("turn-status:{turn}:{revision}");
    outbox
        .append(
            uow,
            &Envelope::snapshot(
                EventId(Uuid::new_v5(&Uuid::NAMESPACE_OID, identity.as_bytes())),
                "tasks",
                Timestamp(Utc::now()),
                format!("turn:{turn}"),
                u64::try_from(revision)?,
                data,
            ),
        )
        .await?;
    Ok(())
}

/// Publish a logical terminal transition and its single outcome fact.
pub(crate) async fn append_turn_disposition(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    turn: Uuid,
    reason: &str,
) -> Result<()> {
    append_turn_status(uow, outbox, turn).await?;
    append_turn_finished(uow, outbox, turn, None, Some(reason), 0.0, None, None, None).await
}

/// Publish at most one terminal outcome after the archive rows exist.
pub(crate) async fn append_turn_finished(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    turn: Uuid,
    result: Option<&Value>,
    reason: Option<&str>,
    created: f64,
    started: Option<f64>,
    finished: Option<f64>,
    provider_failure: Option<&Value>,
) -> Result<()> {
    let newly_emitted: Option<Uuid> = sqlx::query_scalar(
        "UPDATE tasks.attempts SET finished_event_emitted=true \
         WHERE id=$1 AND NOT finished_event_emitted RETURNING id",
    )
    .bind(turn)
    .fetch_optional(uow.connection())
    .await?;
    if newly_emitted.is_none() {
        return Ok(());
    }
    let archive: Option<(Option<String>, Option<String>, Option<i64>)> = sqlx::query_as(
        "SELECT a.gzip_sha256,a.archive_error,u.byte_count FROM tasks.turn_archives a \
         LEFT JOIN tasks.archive_uploads u ON u.gzip_sha256=a.gzip_sha256 WHERE a.turn_id=$1",
    )
    .bind(turn)
    .fetch_optional(uow.connection())
    .await?;
    let transcript = archive.as_ref().and_then(|(sha, error, size)| {
        (error.is_none())
            .then(|| {
                Some(TurnTranscript {
                    blob: sha.clone()?,
                    bytes: u64::try_from((*size)?).ok()?,
                })
            })
            .flatten()
    });
    let response = match result
        .and_then(|result| result.get("response"))
        .filter(|value| !value.is_null())
    {
        Some(response) => Some(
            record_turn_artifact(
                uow,
                turn,
                &serde_json::to_vec(response)?,
                "application/json",
            )
            .await?,
        ),
        None => None,
    };
    let workspace = match result
        .and_then(|result| result.get("workspace"))
        .filter(|value| !value.is_null())
    {
        Some(workspace) => Some(
            record_turn_artifact(
                uow,
                turn,
                &serde_json::to_vec(workspace)?,
                "application/json",
            )
            .await?,
        ),
        None => None,
    };
    let invalid_report: Option<String> =
        sqlx::query_scalar("SELECT validation_error FROM tasks.intake_reports WHERE attempt_id=$1")
            .bind(turn)
            .fetch_optional(uow.connection())
            .await?
            .flatten();
    let raw_outcome = result.and_then(|result| result["outcome"].as_str());
    let reason = reason
        .or_else(|| invalid_report.as_ref().map(|_| "invalid_output"))
        .or_else(|| raw_outcome.filter(|outcome| *outcome != "ok"))
        .or_else(|| result.and_then(|result| result["termination_reason"].as_str()));
    let outcome = if invalid_report.is_some()
        || provider_failure.is_some()
        || (reason == Some("runner_refusal") && raw_outcome == Some("ok"))
    {
        "failed"
    } else {
        match raw_outcome.or(reason) {
            Some("ok") => "succeeded",
            Some("failed" | "terminated" | "queue_expired") => "failed",
            Some("uncertain" | "lost" | "timed_out" | "admission_unknown") => "unknown",
            _ => "unknown",
        }
    };
    let data = TurnFinished {
        turn_id: turn,
        outcome: outcome.to_owned(),
        reason: reason.map(str::to_owned),
        provider_requests: "unknown".to_owned(),
        response,
        workspace,
        transcript,
        transcript_error: archive.and_then(|(_, error, _)| error),
        elapsed_seconds: result
            .and_then(|result| result["elapsed_seconds"].as_f64())
            .or_else(|| finished.map(|finished| (finished - started.unwrap_or(created)).max(0.0))),
        provider_failure: provider_failure.cloned(),
        funding_ended: false,
    };
    let identity = format!("turn-finished:{turn}");
    outbox
        .append(
            uow,
            &Envelope::fact(
                EventId(Uuid::new_v5(&Uuid::NAMESPACE_OID, identity.as_bytes())),
                "tasks",
                Timestamp(Utc::now()),
                data,
            ),
        )
        .await?;
    Ok(())
}
