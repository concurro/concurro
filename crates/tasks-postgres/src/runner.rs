//! Durable runner ref desires and one-send workflow dispatches.

use anyhow::{Result, ensure};
use concurro_db::{Database, UnitOfWork};
use uuid::Uuid;

/// A session's immutable fork and current publication state.
#[derive(Clone, Debug)]
pub struct RunnerSession {
    /// Exchange session allocated to the turn.
    pub session_id: Uuid,
    /// Turn whose runner this session serves.
    pub turn_id: Uuid,
    /// Configured issue repository identity for target lookup.
    pub repository_id: i64,
    /// Fork pinned when the turn was admitted.
    pub fork: String,
    /// Commit that the run ref must point to.
    pub config_sha: String,
    /// Whether the pinned run ref should exist now.
    pub desired_present: bool,
    /// Monotonic ref desire revision.
    pub ref_revision: i64,
    /// Whether a physical ref write still needs readback.
    pub ref_watch: bool,
    /// Whether a read confirmed the exact desired ref.
    pub ref_present_observed: bool,
    /// Dispatch lifecycle; sending and sent are readback only.
    pub dispatch_status: String,
}

/// Queue one serialized runner-session reconciliation.
pub async fn enqueue_runner_session(
    uow: &mut UnitOfWork<'_>,
    session_id: Uuid,
    suffix: &str,
) -> Result<()> {
    let queue = format!("runner-session:{session_id}");
    let key = format!("runner-session:{session_id}:{suffix}");
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'runner_session',payload => json_build_object('session_id',$1), \
         queue_name => $2,max_attempts => 25,job_key => $3)).id",
    )
    .bind(session_id)
    .bind(queue)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Bind a new exchange dispatch to the turn's immutable fork and config commit.
pub async fn record_runner_session(
    uow: &mut UnitOfWork<'_>,
    turn_id: Uuid,
    session_id: Uuid,
) -> Result<()> {
    let inserted: Option<bool> = sqlx::query_scalar(
        "INSERT INTO tasks.runner_sessions \
         (session_id,turn_id,repository_id,fork,config_sha,desired_present,ref_watch,dispatch_status) \
         SELECT $2,$1,s.repository_id,a.fork,a.config_sha, \
         a.status IN ('ready','sending','queued','running') OR \
             (a.status='unknown' AND a.outcome_reason='poll_failures_exhausted'), \
         true,CASE WHEN a.status IN ('ready','sending','queued','running') OR \
             (a.status='unknown' AND a.outcome_reason='poll_failures_exhausted') \
             THEN 'recorded' ELSE 'skipped' END \
         FROM tasks.attempts a \
         JOIN tasks.task_state s ON s.task_id=a.task_id WHERE a.id=$1 \
         ON CONFLICT (session_id) DO NOTHING RETURNING desired_present",
    )
    .bind(turn_id)
    .bind(session_id)
    .fetch_optional(uow.connection())
    .await?;
    if inserted.is_some() {
        enqueue_runner_session(uow, session_id, "initial").await?;
    } else {
        let same: Option<bool> = sqlx::query_scalar(
            "SELECT r.turn_id=$1 AND r.repository_id=s.repository_id \
             AND r.fork=a.fork AND r.config_sha=a.config_sha \
             FROM tasks.runner_sessions r JOIN tasks.attempts a ON a.id=$1 \
             JOIN tasks.task_state s ON s.task_id=a.task_id WHERE r.session_id=$2",
        )
        .bind(turn_id)
        .bind(session_id)
        .fetch_optional(uow.connection())
        .await?;
        ensure!(same == Some(true), "runner session binding changed");
    }
    if inserted == Some(true) {
        retire_runner_sessions_except(uow, turn_id, Some(session_id)).await?;
    }
    Ok(())
}

/// Read the current session desire without holding a lock across GitHub calls.
pub async fn runner_session(
    database: &Database,
    session_id: Uuid,
) -> Result<Option<RunnerSession>> {
    let row: Option<(
        Uuid,
        Uuid,
        i64,
        String,
        String,
        bool,
        i64,
        bool,
        bool,
        String,
    )> = sqlx::query_as(
        "SELECT session_id,turn_id,repository_id,fork,config_sha,desired_present, \
             ref_revision,ref_watch,ref_present_observed,dispatch_status \
             FROM tasks.runner_sessions WHERE session_id=$1",
    )
    .bind(session_id)
    .fetch_optional(database.pool())
    .await?;
    Ok(row.map(
        |(
            session_id,
            turn_id,
            repository_id,
            fork,
            config_sha,
            desired_present,
            ref_revision,
            ref_watch,
            ref_present_observed,
            dispatch_status,
        )| RunnerSession {
            session_id,
            turn_id,
            repository_id,
            fork,
            config_sha,
            desired_present,
            ref_revision,
            ref_watch,
            ref_present_observed,
            dispatch_status,
        },
    ))
}

/// Read the immutable submit body for nonsecret runner display labels.
pub async fn runner_submission(database: &Database, turn_id: Uuid) -> Result<Vec<u8>> {
    Ok(
        sqlx::query_scalar("SELECT submit_body FROM tasks.attempts WHERE id=$1")
            .bind(turn_id)
            .fetch_one(database.pool())
            .await?,
    )
}

/// Record one ref mutation before sending its HTTP request.
pub async fn begin_ref_write(
    uow: &mut UnitOfWork<'_>,
    session: &RunnerSession,
    action: &str,
) -> Result<Option<i64>> {
    let current: Option<bool> = sqlx::query_scalar(
        "SELECT desired_present FROM tasks.runner_sessions \
         WHERE session_id=$1 AND ref_revision=$2 AND ref_watch \
         AND (ref_retry_at IS NULL OR ref_retry_at<=now()) FOR UPDATE",
    )
    .bind(session.session_id)
    .bind(session.ref_revision)
    .fetch_optional(uow.connection())
    .await?;
    if current != Some(action == "create") {
        return Ok(None);
    }
    let id: i64 = sqlx::query_scalar(
        "INSERT INTO tasks.runner_ref_writes (session_id,revision,action,status) \
         VALUES ($1,$2,$3,'sending') RETURNING id",
    )
    .bind(session.session_id)
    .bind(session.ref_revision)
    .bind(action)
    .fetch_one(uow.connection())
    .await?;
    Ok(Some(id))
}

/// Record a definite ref result and schedule observation of the current desire.
pub async fn finish_ref_write(
    uow: &mut UnitOfWork<'_>,
    session_id: Uuid,
    write_id: i64,
    status: &str,
    failure: Option<&str>,
    retry_seconds: Option<u64>,
) -> Result<()> {
    let revision: Option<i64> = sqlx::query_scalar(
        "UPDATE tasks.runner_ref_writes SET status=$3,failure=$4,updated_at=now() \
         WHERE id=$1 AND session_id=$2 AND status IN ('sending','obsolete_uncertain') \
         RETURNING revision",
    )
    .bind(write_id)
    .bind(session_id)
    .bind(status)
    .bind(failure)
    .fetch_optional(uow.connection())
    .await?;
    if revision.is_some() {
        if status == "failed" {
            sqlx::query(
                "UPDATE tasks.runner_sessions SET \
                 ref_watch=$3='retryable_refusal' OR EXISTS( \
                     SELECT 1 FROM tasks.runner_ref_writes w WHERE w.session_id=$1 \
                     AND w.status IN ('sending','obsolete_uncertain')), \
                 ref_error=$3,ref_retry_at=CASE WHEN $4::bigint IS NULL THEN NULL \
                     ELSE now()+$4::bigint * interval '1 second' END, \
                 updated_at=now() WHERE session_id=$1 AND ref_revision=$2",
            )
            .bind(session_id)
            .bind(revision)
            .bind(failure)
            .bind(retry_seconds.map(|seconds| seconds.min(i32::MAX as u64) as i64))
            .execute(uow.connection())
            .await?;
        }
        enqueue_runner_session(uow, session_id, &format!("ref-write:{write_id}")).await?;
    }
    Ok(())
}

/// Confirm one ref observation while retaining watches for uncertain old sends.
pub async fn observe_ref(
    uow: &mut UnitOfWork<'_>,
    session: &RunnerSession,
    present: bool,
) -> Result<()> {
    sqlx::query(
        "UPDATE tasks.runner_sessions SET ref_present_observed=$3, \
         ref_watch=EXISTS(SELECT 1 FROM tasks.runner_ref_writes w \
             WHERE w.session_id=$1 AND w.status IN ('sending','obsolete_uncertain')), \
         ref_error=NULL,ref_retry_at=NULL,updated_at=now() \
         WHERE session_id=$1 AND ref_revision=$2",
    )
    .bind(session.session_id)
    .bind(session.ref_revision)
    .bind(present)
    .execute(uow.connection())
    .await?;
    Ok(())
}

/// Surface a conflicting ref SHA and stop dispatching this session.
pub async fn refuse_ref_conflict(uow: &mut UnitOfWork<'_>, session: &RunnerSession) -> Result<()> {
    sqlx::query(
        "UPDATE tasks.runner_sessions SET ref_error='conflicting_sha',ref_watch=false, \
         dispatch_status=CASE WHEN dispatch_status='recorded' THEN 'failed' ELSE dispatch_status END, \
         dispatch_error=CASE WHEN dispatch_status='recorded' THEN 'ref_conflict' ELSE dispatch_error END, \
         updated_at=now() WHERE session_id=$1 AND ref_revision=$2",
    )
    .bind(session.session_id)
    .bind(session.ref_revision)
    .execute(uow.connection())
    .await?;
    Ok(())
}

/// Fence the only allowed dispatch send after a matching run ref was read.
pub async fn claim_dispatch(uow: &mut UnitOfWork<'_>, session_id: Uuid) -> Result<bool> {
    let changed = sqlx::query(
        "UPDATE tasks.runner_sessions SET dispatch_status='sending',updated_at=now() \
         WHERE session_id=$1 AND desired_present AND ref_present_observed \
         AND dispatch_status='recorded' AND EXISTS ( \
             SELECT 1 FROM tasks.attempts a WHERE a.id=turn_id \
             AND (a.status IN ('ready','sending','queued','running') OR \
                  (a.status='unknown' AND a.outcome_reason='poll_failures_exhausted'))) ",
    )
    .bind(session_id)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Mark an acknowledged dispatch without ever allowing a second send.
pub async fn settle_dispatch_sent(uow: &mut UnitOfWork<'_>, session_id: Uuid) -> Result<()> {
    sqlx::query(
        "UPDATE tasks.runner_sessions SET dispatch_status='sent',updated_at=now() \
         WHERE session_id=$1 AND dispatch_status='sending'",
    )
    .bind(session_id)
    .execute(uow.connection())
    .await?;
    Ok(())
}

/// Stop a definitively refused dispatch without replaying its POST.
pub async fn fail_dispatch(uow: &mut UnitOfWork<'_>, session_id: Uuid, reason: &str) -> Result<()> {
    sqlx::query(
        "UPDATE tasks.runner_sessions SET dispatch_status='failed',dispatch_error=$2,updated_at=now() \
         WHERE session_id=$1 AND dispatch_status IN ('sending','obsolete_uncertain')",
    )
    .bind(session_id)
    .bind(reason)
    .execute(uow.connection())
    .await?;
    Ok(())
}

/// Adopt the first matching workflow run for one already-sent dispatch.
pub async fn observe_dispatch_run(
    uow: &mut UnitOfWork<'_>,
    session_id: Uuid,
    run_id: i64,
) -> Result<()> {
    sqlx::query(
        "UPDATE tasks.runner_sessions SET dispatch_status='observed',run_id=$2,updated_at=now() \
         WHERE session_id=$1 AND dispatch_status IN ('sending','obsolete_uncertain','sent')",
    )
    .bind(session_id)
    .bind(run_id)
    .execute(uow.connection())
    .await?;
    Ok(())
}

/// Retire refs and skip only dispatches that never reached sending.
pub async fn retire_runner_sessions(uow: &mut UnitOfWork<'_>, turn_id: Uuid) -> Result<()> {
    retire_runner_sessions_except(uow, turn_id, None).await
}

/// Retire every session except a newly observed replacement for the same turn.
async fn retire_runner_sessions_except(
    uow: &mut UnitOfWork<'_>,
    turn_id: Uuid,
    keep: Option<Uuid>,
) -> Result<()> {
    let changed: Vec<(Uuid, i64)> = sqlx::query_as(
        "UPDATE tasks.runner_sessions SET desired_present=false, \
         ref_revision=ref_revision+1,ref_present_observed=false,ref_watch=true,ref_retry_at=NULL, \
         dispatch_status=CASE WHEN dispatch_status='recorded' THEN 'skipped' \
             WHEN dispatch_status='sending' THEN 'obsolete_uncertain' ELSE dispatch_status END, \
         updated_at=now() WHERE turn_id=$1 AND desired_present \
         AND ($2::uuid IS NULL OR session_id<>$2) \
         RETURNING session_id,ref_revision",
    )
    .bind(turn_id)
    .bind(keep)
    .fetch_all(uow.connection())
    .await?;
    for (session_id, revision) in changed {
        sqlx::query(
            "UPDATE tasks.runner_ref_writes SET status='obsolete_uncertain',updated_at=now() \
             WHERE session_id=$1 AND revision<$2 AND status='sending'",
        )
        .bind(session_id)
        .bind(revision)
        .execute(uow.connection())
        .await?;
        enqueue_runner_session(uow, session_id, &format!("retire:{revision}")).await?;
    }
    Ok(())
}

/// Find sessions with an unobserved ref desire or dispatch after job exhaustion.
pub async fn watched_runner_sessions(database: &Database) -> Result<Vec<Uuid>> {
    Ok(sqlx::query_scalar(
        "SELECT session_id FROM tasks.runner_sessions \
         WHERE (ref_watch AND (ref_retry_at IS NULL OR ref_retry_at<=now())) \
         OR dispatch_status IN ('recorded','sending','obsolete_uncertain','sent') \
         ORDER BY updated_at,session_id",
    )
    .fetch_all(database.pool())
    .await?)
}
