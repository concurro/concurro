//! Exchange effect claims, retries, observations, and retirement.

use concurro_db::{Database, UnitOfWork};
use concurro_events_postgres::Outbox;
use concurro_exchange_v1::{TurnObservation, TurnSubmission};
use concurro_tasks_core::{Stage, TaskState};
use concurro_tasks_diagnostics::jsonb_representable;
use serde_json::{Value, json};
use sqlx::types::Json;
use uuid::Uuid;

use crate::{
    EffectJob, EffectKind, ExecutionOwner, archive, enqueue_design_batch,
    enqueue_design_correction, enqueue_effect, enqueue_implementation, enqueue_intake,
    enqueue_plan, enqueue_poll, events, intake, lock_attempt, lock_execution_owner,
    lock_issue_for_turn, record_runner_session, retire_runner_sessions,
    sponsorship::{record_credential_failure, record_credential_success},
};

/// Recognize only a definite failed provider result from an admitted retained session.
pub(crate) fn definite_session_failure(
    status: &str,
    reason: Option<&str>,
    result: Option<&Value>,
    submit_body: &[u8],
) -> bool {
    status == "finished"
        && reason.is_none()
        && result.is_some_and(|result| result["outcome"] == "failed")
        && serde_json::from_slice::<TurnSubmission>(submit_body)
            .ok()
            .is_some_and(|submission| submission.payload["request"]["session"].is_object())
}

/// Apply one terminal attempt through its recorded operation's policy.
pub(crate) async fn settle_attempt(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    state: &TaskState,
    turn: Uuid,
) -> anyhow::Result<()> {
    let operation: Option<String> =
        sqlx::query_scalar("SELECT operation FROM tasks.attempts WHERE id=$1 AND task_id=$2")
            .bind(turn)
            .bind(state.id.0)
            .fetch_optional(uow.connection())
            .await?;
    match operation.as_deref() {
        Some("intake") => intake::settle_intake(uow, outbox, state, turn).await,
        Some("plan") => crate::plan::settle_plan(uow, outbox, state, turn).await,
        Some("plan_qc") => crate::design::settle_design_attempt(uow, outbox, state, turn).await,
        Some("review") => {
            crate::pr_review::settle_pr_review_attempt(uow, outbox, state, turn).await
        }
        Some("develop") => {
            crate::implementation::settle_development(uow, outbox, state, turn).await
        }
        Some(operation) => anyhow::bail!("unsupported terminal attempt operation: {operation}"),
        None => Ok(()),
    }
}

/// Settle a terminal attempt through the lifecycle that owns it.
async fn settle_owned_attempt(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    owner: &ExecutionOwner,
    turn: Uuid,
) -> anyhow::Result<()> {
    match owner {
        ExecutionOwner::Contribution { .. } => {
            if let Some(state) = lock_issue_for_turn(uow, turn).await? {
                settle_attempt(uow, outbox, &state, turn).await?;
            }
        }
        ExecutionOwner::Standalone { task_id, .. } => {
            crate::standalone::settle_standalone_review_attempt(uow, *task_id, turn).await?;
        }
    }
    Ok(())
}

/// Finish a source-validation job whose task or review binding became obsolete.
pub async fn finish_unapplied_result(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    turn: Uuid,
) -> anyhow::Result<bool> {
    let pending: Option<(String, String, bool, Option<Json<Value>>)> = sqlx::query_as(
        "SELECT operation,status,finished_event_emitted,result \
         FROM tasks.attempts WHERE id=$1 FOR UPDATE",
    )
    .bind(turn)
    .fetch_optional(uow.connection())
    .await?;
    let Some((operation, status, emitted, result)) = pending else {
        return Ok(false);
    };
    if !matches!(operation.as_str(), "plan_qc" | "develop" | "review")
        || status != "finished"
        || emitted
        || result
            .as_ref()
            .is_none_or(|result| result.0["outcome"] != "ok")
    {
        return Ok(false);
    }
    events::append_deferred_turn_finished(uow, outbox, turn, Some("superseded_before_validation"))
        .await?;
    Ok(true)
}

/// Claim one Graphile Worker effect by its stable ID, fencing an older worker.
pub async fn claim_effect(
    uow: &mut UnitOfWork<'_>,
    effect_id: i64,
) -> Result<Option<EffectJob>, sqlx::Error> {
    let turn: Option<Uuid> = sqlx::query_scalar("SELECT turn_id FROM tasks.effects WHERE id=$1")
        .bind(effect_id)
        .fetch_optional(uow.connection())
        .await?;
    let Some(turn) = turn else {
        return Ok(None);
    };
    lock_attempt(uow, turn).await?;
    let row: Option<(i64, Uuid, String, Vec<u8>, String, i32, i32)> = sqlx::query_as(
        "SELECT e.id,e.turn_id,e.kind,a.submit_body,a.status,e.submit_attempts, \
         GREATEST(0,CEIL(EXTRACT(EPOCH FROM \
         (e.updated_at + interval '15 seconds' - now()))))::int \
         FROM tasks.effects e \
         JOIN tasks.attempts a ON a.id=e.turn_id \
         WHERE e.id=$1 AND e.status<>'settled' \
         FOR UPDATE OF e",
    )
    .bind(effect_id)
    .fetch_optional(uow.connection())
    .await?;
    let Some((id, turn, kind, body, attempt_status, prior_sends, delay)) = row else {
        return Ok(None);
    };
    if kind == "submit" && !matches!(attempt_status.as_str(), "ready" | "sending") {
        return Ok(None);
    }
    if kind == "submit" && prior_sends > 0 && delay > 0 {
        enqueue_effect(uow, id, prior_sends, delay).await?;
        return Ok(None);
    }
    let token = Uuid::now_v7();
    let submit_attempts: i32 = sqlx::query_scalar(
        "UPDATE tasks.effects SET status='sending', claim_token=$2, \
         submit_attempts=submit_attempts+CASE WHEN kind='submit' AND submit_attempts<4 THEN 1 ELSE 0 END, \
         updated_at=now() WHERE id=$1 RETURNING submit_attempts",
    )
    .bind(id)
    .bind(token)
    .fetch_one(uow.connection())
    .await?;
    let kind = match kind.as_str() {
        "submit" => {
            sqlx::query(
                "UPDATE tasks.attempts SET status='sending' WHERE id=$1 AND status='ready'",
            )
            .bind(turn)
            .execute(uow.connection())
            .await?;
            EffectKind::Submit
        }
        "retire" => EffectKind::Retire,
        _ => unreachable!("effect kind is constrained by schema"),
    };
    Ok(Some(EffectJob {
        id,
        turn,
        kind,
        submit_body: (kind == EffectKind::Submit).then_some(body),
        claim_token: token,
        submit_attempts,
        recovered_exhaustion: kind == EffectKind::Submit && prior_sends >= 4,
    }))
}

/// Persist a submit reply only while this worker still owns its claim.
pub async fn settle_submit(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    job: &EffectJob,
    mut response: serde_json::Value,
    raw_body: Option<&[u8]>,
) -> anyhow::Result<bool> {
    if job.kind != EffectKind::Submit {
        return Ok(false);
    }
    lock_execution_owner(uow, job.turn).await?;
    lock_attempt(uow, job.turn).await?;
    let raw_response =
        raw_body.map_or_else(|| serde_json::to_vec(&response), |body| Ok(body.to_vec()))?;
    operational_json(&mut response);
    let updated = sqlx::query(
        "UPDATE tasks.effects SET status='settled', response=$3, response_raw=$4, updated_at=now() \
         WHERE id=$1 AND claim_token=$2 AND status='sending'",
    )
    .bind(job.id)
    .bind(job.claim_token)
    .bind(Json(response))
    .bind(raw_response)
    .execute(uow.connection())
    .await?;
    if updated.rows_affected() == 0 {
        return Ok(false);
    }
    let changed = sqlx::query(
        "UPDATE tasks.attempts SET status='queued' WHERE id=$1 AND status IN ('ready','sending')",
    )
    .bind(job.turn)
    .execute(uow.connection())
    .await?;
    if changed.rows_affected() > 0 {
        events::append_turn_status(uow, outbox, job.turn).await?;
    }
    Ok(true)
}

/// Settle a permanent turn refusal without losing any previously recorded result.
pub async fn settle_submit_refusal(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    job: &EffectJob,
    status: u16,
    reason: &str,
) -> anyhow::Result<bool> {
    if job.kind != EffectKind::Submit {
        return Ok(false);
    }
    let owner = lock_execution_owner(uow, job.turn).await?;
    lock_attempt(uow, job.turn).await?;
    let updated = sqlx::query(
        "UPDATE tasks.effects SET status='settled',response=$3,updated_at=now() \
         WHERE id=$1 AND claim_token=$2 AND status='sending'",
    )
    .bind(job.id)
    .bind(job.claim_token)
    .bind(Json(json!({"http_status": status, "error": reason})))
    .execute(uow.connection())
    .await?;
    if updated.rows_affected() == 0 {
        return Ok(false);
    }
    let changed = sqlx::query(
        "UPDATE tasks.attempts SET status='unknown',outcome_reason=$2 \
         WHERE id=$1 AND status NOT IN ('cancelled','finished')",
    )
    .bind(job.turn)
    .bind(format!("exchange_http_{status}:{reason}"))
    .execute(uow.connection())
    .await?;
    if changed.rows_affected() > 0 {
        events::append_turn_disposition(uow, outbox, job.turn, "admission_unknown").await?;
    }
    retire_runner_sessions(uow, job.turn).await?;
    if let Some(owner) = owner.as_ref() {
        settle_owned_attempt(uow, outbox, owner, job.turn).await?;
    }
    let effect_id: Option<i64> = sqlx::query_scalar(
        "INSERT INTO tasks.effects (turn_id,kind,status) VALUES ($1,'retire','recorded') \
         ON CONFLICT (turn_id,kind) DO NOTHING RETURNING id",
    )
    .bind(job.turn)
    .fetch_optional(uow.connection())
    .await?;
    if let Some(id) = effect_id {
        enqueue_effect(uow, id, 0, 0).await?;
    }
    Ok(true)
}

/// Reschedule a transient submit at fifteen seconds or abandon its fourth send.
pub async fn record_submit_failure(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    job: &EffectJob,
) -> anyhow::Result<bool> {
    if job.kind != EffectKind::Submit {
        return Ok(false);
    }
    let owner = lock_execution_owner(uow, job.turn).await?;
    lock_attempt(uow, job.turn).await?;
    let attempts: Option<i32> = sqlx::query_scalar(
        "UPDATE tasks.effects SET status='recorded',claim_token=NULL,updated_at=now() \
         WHERE id=$1 AND claim_token=$2 AND status='sending' RETURNING submit_attempts",
    )
    .bind(job.id)
    .bind(job.claim_token)
    .fetch_optional(uow.connection())
    .await?;
    let Some(attempts) = attempts else {
        return Ok(false);
    };
    if attempts < 4 {
        enqueue_effect(uow, job.id, attempts, 15).await?;
        return Ok(true);
    }
    sqlx::query(
        "UPDATE tasks.effects SET status='settled',response=$2,updated_at=now() WHERE id=$1",
    )
    .bind(job.id)
    .bind(Json(json!({"error": "submit_attempts_exhausted"})))
    .execute(uow.connection())
    .await?;
    let changed = sqlx::query(
        "UPDATE tasks.attempts SET status='unknown',outcome_reason='submit_attempts_exhausted' \
         WHERE id=$1 AND status IN ('ready','sending')",
    )
    .bind(job.turn)
    .execute(uow.connection())
    .await?;
    if changed.rows_affected() > 0 {
        events::append_turn_disposition(uow, outbox, job.turn, "admission_unknown").await?;
    }
    retire_runner_sessions(uow, job.turn).await?;
    if let Some(owner) = owner.as_ref() {
        settle_owned_attempt(uow, outbox, owner, job.turn).await?;
    }
    let retirement: Option<i64> = sqlx::query_scalar(
        "INSERT INTO tasks.effects (turn_id,kind,status) VALUES ($1,'retire','recorded') \
         ON CONFLICT (turn_id,kind) DO NOTHING RETURNING id",
    )
    .bind(job.turn)
    .fetch_optional(uow.connection())
    .await?;
    if let Some(id) = retirement {
        enqueue_effect(uow, id, 0, 0).await?;
    }
    Ok(true)
}

/// Check whether a queued poll generation still addresses a live turn.
pub async fn pollable_generation(
    database: &Database,
    turn: Uuid,
    generation: i64,
) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM tasks.attempts WHERE id=$1 AND poll_generation=$2 \
         AND status IN ('queued','running'))",
    )
    .bind(turn)
    .bind(generation)
    .fetch_one(database.pool())
    .await
}

/// Find live attempts past their queue or execution deadline, including lost jobs.
pub async fn due_attempt_deadlines(database: &Database) -> Result<Vec<(Uuid, i64)>, sqlx::Error> {
    sqlx::query_as(
        "SELECT id,poll_generation FROM tasks.attempts WHERE deadline <= now() \
         AND status IN ('ready','sending','queued','running') ORDER BY deadline,id",
    )
    .fetch_all(database.pool())
    .await
}

/// Requeue unsettled turn retirements after Graphile jobs exhaust or disappear.
pub async fn recover_retirements(database: &Database) -> anyhow::Result<usize> {
    let mut work = database.begin().await?;
    let ids: Vec<i64> = sqlx::query_scalar(
        "UPDATE tasks.effects SET updated_at=now() WHERE kind='retire' \
         AND status<>'settled' AND updated_at<=now()-interval '5 minutes' RETURNING id",
    )
    .fetch_all(work.connection())
    .await?;
    for id in &ids {
        enqueue_effect(&mut work, *id, 0, 0).await?;
    }
    work.commit().await?;
    Ok(ids.len())
}

/// Expire a current turn at its recorded queue or execution deadline and queue cancellation.
pub async fn expire_attempt(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    turn: Uuid,
    generation: i64,
) -> anyhow::Result<bool> {
    let Some(owner) = lock_execution_owner(uow, turn).await? else {
        return Ok(false);
    };
    let due: Option<(bool, String)> = sqlx::query_as(
        "SELECT COALESCE(deadline <= now(),false),status FROM tasks.attempts WHERE id=$1 AND poll_generation=$2 \
         AND status IN ('ready','sending','queued','running') FOR UPDATE",
    )
    .bind(turn)
    .bind(generation)
    .fetch_optional(uow.connection())
    .await?;
    let Some((true, status)) = due else {
        return Ok(false);
    };
    let reason = if status == "running" {
        "timed_out"
    } else {
        "queue_expired"
    };
    sqlx::query("UPDATE tasks.attempts SET status='unknown',outcome_reason=$2 WHERE id=$1")
        .bind(turn)
        .bind(reason)
        .execute(uow.connection())
        .await?;
    events::append_turn_disposition(uow, outbox, turn, reason).await?;
    retire_runner_sessions(uow, turn).await?;
    settle_owned_attempt(uow, outbox, &owner, turn).await?;
    let effect_id: Option<i64> = sqlx::query_scalar(
        "INSERT INTO tasks.effects (turn_id,kind,status) VALUES ($1,'retire','recorded') \
         ON CONFLICT (turn_id,kind) DO NOTHING RETURNING id",
    )
    .bind(turn)
    .fetch_optional(uow.connection())
    .await?;
    if let Some(id) = effect_id {
        enqueue_effect(uow, id, 0, 0).await?;
    }
    Ok(true)
}

/// Count a failed long poll; after twenty failures the attempt is unknown.
pub async fn record_poll_failure(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    turn: Uuid,
    generation: i64,
    definitive_status: Option<u16>,
) -> anyhow::Result<Option<bool>> {
    let Some(owner) = lock_execution_owner(uow, turn).await? else {
        return Ok(None);
    };
    let failure: Option<(i32, i64)> = sqlx::query_as(
        "UPDATE tasks.attempts SET poll_failures=poll_failures+1, \
         status=CASE WHEN poll_failures+1>20 OR $3::int IS NOT NULL THEN 'unknown' ELSE status END, \
         outcome_reason=CASE WHEN $3::int IS NOT NULL THEN 'exchange_poll_http_' || $3::text \
         WHEN poll_failures+1>20 THEN 'poll_failures_exhausted' ELSE outcome_reason END, \
         poll_generation=poll_generation+1 \
         WHERE id=$1 AND poll_generation=$2 AND status IN ('queued','running') \
         RETURNING poll_failures,poll_generation",
    )
    .bind(turn)
    .bind(generation)
    .bind(definitive_status.map(i32::from))
    .fetch_optional(uow.connection())
    .await?;
    if failure.is_some_and(|(count, _)| count > 20 || definitive_status.is_some()) {
        if let Some(status) = definitive_status {
            retire_runner_sessions(uow, turn).await?;
            events::append_turn_disposition(
                uow,
                outbox,
                turn,
                &format!("exchange_poll_http_{status}"),
            )
            .await?;
            let retirement: Option<i64> = sqlx::query_scalar(
                "INSERT INTO tasks.effects (turn_id,kind,status) VALUES ($1,'retire','recorded') \
                 ON CONFLICT (turn_id,kind) DO NOTHING RETURNING id",
            )
            .bind(turn)
            .fetch_optional(uow.connection())
            .await?;
            if let Some(id) = retirement {
                enqueue_effect(uow, id, 0, 0).await?;
            }
        }
        settle_owned_attempt(uow, outbox, &owner, turn).await?;
    } else if let Some((_, next_generation)) = failure {
        enqueue_poll(uow, turn, next_generation, 15).await?;
    }
    Ok(failure.map(|(count, _)| count > 20 || definitive_status.is_some()))
}

/// Replace PostgreSQL-unrepresentable NULs in a queryable JSON projection.
pub(crate) fn operational_json(value: &mut Value) {
    match value {
        Value::String(text) => *text = text.replace('\0', "\\u0000"),
        Value::Array(items) => items.iter_mut().for_each(operational_json),
        Value::Object(fields) => {
            let old = std::mem::take(fields);
            for (key, mut item) in old {
                operational_json(&mut item);
                fields.insert(key.replace('\0', "\\u0000"), item);
            }
        }
        Value::Number(number) if !jsonb_representable(&Value::Number(number.clone())) => {
            *value = Value::String(number.to_string());
        }
        _ => {}
    }
}

/// Record a poll and its sessions; terminal turns are retired even if their task stopped.
pub async fn record_observation(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    observation: &TurnObservation,
    expected_generation: Option<i64>,
) -> anyhow::Result<bool> {
    let Some(owner) = lock_execution_owner(uow, observation.turn).await? else {
        return Ok(false);
    };
    let row: Option<(String, i64, bool, Option<Json<Value>>, Option<String>)> =
        sqlx::query_as("SELECT status,poll_generation,result IS NOT NULL,observation,outcome_reason FROM tasks.attempts WHERE id=$1 FOR UPDATE")
            .bind(observation.turn)
            .fetch_optional(uow.connection())
            .await?;
    let Some((status, generation, has_result, previous_observation, prior_reason)) = row else {
        return Ok(false);
    };
    if expected_generation.is_some_and(|expected| expected != generation) {
        return Ok(false);
    }
    if !matches!(observation.status.as_str(), "lost" | "cancelled") {
        record_credential_failure(uow, observation).await?;
    }
    if let Some(session) = observation.dispatch {
        sqlx::query(
            "INSERT INTO tasks.attempt_sessions (turn_id,session_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
        )
        .bind(observation.turn)
        .bind(session)
        .execute(uow.connection())
        .await?;
        record_runner_session(uow, observation.turn, session).await?;
    }
    if let Some(runner) = &observation.runner {
        sqlx::query(
            "INSERT INTO tasks.attempt_sessions (turn_id,session_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
        )
        .bind(observation.turn)
        .bind(runner.session)
        .execute(uow.connection())
        .await?;
    }
    let failure_reason = if let Some(failure) = &observation.provider_failure {
        Some(format!(
            "provider_failure:{}",
            failure["category"].as_str().unwrap_or("unknown")
        ))
    } else {
        observation
            .refusal
            .as_ref()
            .map(|_| "runner_refusal".to_owned())
    };
    let terminal = match observation.status.as_str() {
        "queued" | "running" => false,
        "done" | "lost" | "cancelled" => true,
        _ => anyhow::bail!("invalid exchange turn status"),
    } || failure_reason.is_some();
    let next = if status == "cancelled" || status == "finished" || status == "unknown" {
        status.as_str()
    } else {
        match observation.status.as_str() {
            "lost" => "unknown",
            _ if failure_reason.is_some() => "finished",
            "queued" => "queued",
            "running" => "running",
            "done" => "finished",
            "cancelled" => "cancelled",
            _ => unreachable!("exchange status checked above"),
        }
    };
    let schedule_next = !terminal
        && matches!(next, "queued" | "running")
        && !matches!(status.as_str(), "cancelled" | "unknown" | "finished");
    let next_generation = generation + i64::from(schedule_next);
    if terminal
        && !has_result
        && let Some(result) = &observation.result
    {
        archive::record_archive(uow, observation.turn, result).await?;
    }
    let mut stored_observation = serde_json::to_value(observation)?;
    if let Some(previous) = &previous_observation {
        if observation.runner.is_none() && observation.dispatch.is_none() {
            stored_observation["runner"] = previous.0["runner"].clone();
            stored_observation["dispatch"] = previous.0["dispatch"].clone();
        }
        for field in ["started", "finished"] {
            if stored_observation[field].is_null() {
                stored_observation[field] = previous.0[field].clone();
            }
        }
    }
    if let Some(result) = stored_observation
        .get_mut("result")
        .and_then(Value::as_object_mut)
    {
        result.remove("retained");
        result.remove("transcript");
    }
    operational_json(&mut stored_observation);
    let mut stored_result = observation.result.clone();
    if let Some(result) = &mut stored_result {
        operational_json(result);
        if let Some(fields) = result.as_object_mut() {
            fields.remove("retained");
            fields.remove("transcript");
        }
    }
    let raw_observation = observation
        .raw_body
        .clone()
        .unwrap_or(serde_json::to_vec(observation)?);
    let raw_result = observation
        .result
        .as_ref()
        .map(serde_json::to_vec)
        .transpose()?;
    sqlx::query(
        "UPDATE tasks.attempts SET status=$2, observation=$3, observation_raw=$8, \
         result=COALESCE(result,$4), result_raw=COALESCE(result_raw,$9), \
         poll_generation=$5, poll_failures=0, outcome_reason=COALESCE(outcome_reason,$7), \
         deadline=CASE WHEN $6='running' AND unbounded_running THEN NULL ELSE deadline END \
         WHERE id=$1",
    )
    .bind(observation.turn)
    .bind(next)
    .bind(Json(&stored_observation))
    .bind(if failure_reason.is_some() {
        None
    } else {
        stored_result.as_ref().map(Json)
    })
    .bind(next_generation)
    .bind(&observation.status)
    .bind(&failure_reason)
    .bind(raw_observation)
    .bind(raw_result)
    .execute(uow.connection())
    .await?;
    if next == "finished"
        && observation.status == "done"
        && observation.provider_failure.is_none()
        && observation.refusal.is_none()
        && observation
            .result
            .as_ref()
            .is_some_and(|result| result["outcome"] == "ok")
    {
        record_credential_success(uow, observation.turn).await?;
    }
    let identity_changed = previous_observation.as_ref().is_none_or(|previous| {
        ["runner", "dispatch", "started", "finished"]
            .iter()
            .any(|field| previous.0[*field] != stored_observation[*field])
    });
    let live_poll_outage = status == "unknown"
        && prior_reason.as_deref() == Some("poll_failures_exhausted")
        && !terminal;
    if !live_poll_outage && (status != next || identity_changed) {
        events::append_turn_status(uow, outbox, observation.turn).await?;
    }
    if terminal
        || matches!(next, "cancelled" | "finished")
        || (next == "unknown" && status != "unknown")
    {
        retire_runner_sessions(uow, observation.turn).await?;
    }
    if terminal {
        settle_owned_attempt(uow, outbox, &owner, observation.turn).await?;
        let operation: String =
            sqlx::query_scalar("SELECT operation FROM tasks.attempts WHERE id=$1")
                .bind(observation.turn)
                .fetch_one(uow.connection())
                .await?;
        let awaiting_validation: bool = match operation.as_str() {
            "plan_qc" => {
                sqlx::query_scalar(
                    "SELECT EXISTS(SELECT 1 FROM tasks.plan_review_seats \
                 WHERE attempt_id=$1 AND status='completed')",
                )
                .bind(observation.turn)
                .fetch_one(uow.connection())
                .await?
            }
            "develop" => {
                sqlx::query_scalar(
                    "SELECT EXISTS(SELECT 1 FROM tasks.implementation_reports \
                 WHERE attempt_id=$1 AND status='pending')",
                )
                .bind(observation.turn)
                .fetch_one(uow.connection())
                .await?
            }
            "review" => {
                sqlx::query_scalar(
                    "SELECT EXISTS(SELECT 1 FROM tasks.pr_review_seats \
                     WHERE attempt_id=$1 AND status='completed')",
                )
                .bind(observation.turn)
                .fetch_one(uow.connection())
                .await?
            }
            _ => false,
        };
        let deferred_validation = matches!(operation.as_str(), "plan_qc" | "develop" | "review")
            && awaiting_validation
            && failure_reason.is_none()
            && observation.status == "done"
            && observation
                .result
                .as_ref()
                .is_some_and(|result| result["outcome"] == "ok");
        if !deferred_validation {
            let obsolete_before_validation =
                matches!(operation.as_str(), "plan_qc" | "develop" | "review")
                    && !awaiting_validation
                    && failure_reason.is_none()
                    && observation.status == "done"
                    && observation
                        .result
                        .as_ref()
                        .is_some_and(|result| result["outcome"] == "ok");
            events::append_turn_finished(
                uow,
                outbox,
                observation.turn,
                observation.result.as_ref(),
                failure_reason
                    .as_deref()
                    .or(obsolete_before_validation.then_some("superseded_before_validation"))
                    .or(observation.refusal.as_deref())
                    .or(match observation.status.as_str() {
                        "lost" => Some("lost"),
                        "cancelled" => Some("cancelled"),
                        _ => None,
                    }),
                observation.created,
                observation.started,
                observation.finished,
                observation.provider_failure.as_ref(),
            )
            .await?;
        }
    }
    if schedule_next {
        enqueue_poll(uow, observation.turn, next_generation, 15).await?;
    }
    if terminal {
        let effect_id: Option<i64> = sqlx::query_scalar(
            "INSERT INTO tasks.effects (turn_id,kind,status) VALUES ($1,'retire','recorded') \
             ON CONFLICT (turn_id,kind) DO NOTHING RETURNING id",
        )
        .bind(observation.turn)
        .fetch_optional(uow.connection())
        .await?;
        if let Some(id) = effect_id {
            enqueue_effect(uow, id, 0, 0).await?;
        }
    }
    Ok(true)
}

/// Record the exchange's session-complete retirement acknowledgement and release its group.
pub async fn acknowledge_retirement(
    uow: &mut UnitOfWork<'_>,
    job: &EffectJob,
    sessions: &[Uuid],
) -> anyhow::Result<bool> {
    if job.kind != EffectKind::Retire {
        return Ok(false);
    }
    lock_attempt(uow, job.turn).await?;
    let updated = sqlx::query(
        "UPDATE tasks.effects SET status='settled', response=$3, updated_at=now() \
         WHERE id=$1 AND claim_token=$2 AND status='sending'",
    )
    .bind(job.id)
    .bind(job.claim_token)
    .bind(Json(sessions))
    .execute(uow.connection())
    .await?;
    if updated.rows_affected() == 0 {
        return Ok(false);
    }
    for session in sessions {
        sqlx::query(
            "INSERT INTO tasks.attempt_sessions (turn_id,session_id,retired_at) VALUES ($1,$2,now()) \
             ON CONFLICT (turn_id,session_id) DO UPDATE SET retired_at=EXCLUDED.retired_at",
        )
        .bind(job.turn)
        .bind(session)
        .execute(uow.connection())
        .await?;
    }
    retire_runner_sessions(uow, job.turn).await?;
    sqlx::query("UPDATE tasks.effects SET status='settled' WHERE turn_id=$1 AND kind='submit'")
        .bind(job.turn)
        .execute(uow.connection())
        .await?;
    sqlx::query("DELETE FROM tasks.staffing_groups WHERE turn_id=$1")
        .bind(job.turn)
        .execute(uow.connection())
        .await?;
    let waiting: Option<(Json<TaskState>,)> = sqlx::query_as(
        "SELECT t.state FROM tasks.task_state t JOIN tasks.attempts a ON a.task_id=t.task_id \
         WHERE a.id=$1",
    )
    .bind(job.turn)
    .fetch_optional(uow.connection())
    .await?;
    if let Some((Json(state),)) = waiting {
        if state.stage == Stage::Intake && state.current_attempt.is_none() {
            enqueue_intake(uow, &state, Some(job.turn)).await?;
        } else if state.stage == Stage::Plan && state.current_attempt.is_none() {
            enqueue_plan(uow, &state).await?;
        } else if state.stage == Stage::Implement && state.current_attempt.is_none() {
            enqueue_implementation(uow, &state).await?;
        } else if state.stage == Stage::PlanQc {
            let correcting: Vec<(Uuid, String, i32)> = sqlx::query_as(
                "SELECT s.batch_id,s.reviewer_id, \
                 CASE WHEN s.status='retrying' THEN s.failure_count ELSE s.correction_count END \
                 FROM tasks.plan_review_seats s \
                 JOIN tasks.plan_review_batches b ON b.id=s.batch_id \
                 WHERE b.task_id=$1 AND b.status='admitted' \
                 AND s.status IN ('correcting','retrying')",
            )
            .bind(state.id.0)
            .fetch_all(uow.connection())
            .await?;
            for (batch, reviewer, count) in correcting {
                enqueue_design_correction(uow, batch, &reviewer, count).await?;
            }
            let batch: Option<Uuid> = sqlx::query_scalar(
                "SELECT id FROM tasks.plan_review_batches WHERE task_id=$1 AND status='recorded' \
                 ORDER BY generation DESC,id DESC LIMIT 1",
            )
            .bind(state.id.0)
            .fetch_optional(uow.connection())
            .await?;
            if let Some(batch) = batch {
                enqueue_design_batch(uow, &state, batch).await?;
            }
        }
    }
    Ok(true)
}

#[cfg(test)]
mod tests {
    use serde_json::Value;

    use super::operational_json;

    /// Keep an oversized model number queryable without losing its raw representation.
    #[test]
    fn operational_projection_spells_outside_jsonb_numbers() {
        let mut value: Value = serde_json::from_str(r#"{"large":1e999999}"#).expect("JSON");
        operational_json(&mut value);
        assert!(
            value["large"]
                .as_str()
                .is_some_and(|text| text.contains("999999"))
        );
    }
}
