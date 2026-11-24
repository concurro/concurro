//! Storage boundary for a frozen Rust exchange org submission.

use anyhow::{Result, ensure};
use concurro_contracts::InferenceUsage;
use concurro_db::{Database, UnitOfWork};
use uuid::Uuid;

/// Columns read by `org_attempt`.
#[derive(sqlx::FromRow)]
struct IssuedGrantRow {
    /// Persisted task id.
    task_id: Uuid,
    /// Persisted repository id.
    repository_id: i64,
    /// Persisted route.
    route: Option<String>,
    /// Persisted purpose.
    purpose: String,
    /// Persisted payer id.
    payer_id: Option<i64>,
    /// Persisted credential record id.
    credential_record_id: Option<String>,
    /// Persisted credential version.
    credential_version: Option<i64>,
    /// Persisted status.
    status: String,
}

/// Immutable task and source identity needed to convert an org attempt.
pub struct OrgAttempt {
    /// Owning task.
    pub task_id: Uuid,
    /// Issue repository ID for target route selection.
    pub repository_id: i64,
    /// Persisted route, if this attempt was already converted.
    pub route: Option<String>,
    /// Task funding purpose, including standalone review's older spelling.
    pub purpose: String,
    /// Personal payer ID, when this is a contributed attempt.
    pub payer_id: Option<i64>,
    /// Frozen personal credential record and version.
    pub credential: Option<(String, i64)>,
    /// Local grant state, including version revocation before the first send.
    pub status: String,
}

/// Read an issued grant and its contribution or review runner target.
pub async fn org_attempt(database: &Database, turn: Uuid) -> Result<OrgAttempt> {
    let row: Option<IssuedGrantRow> = sqlx::query_as(
        "SELECT a.task_id, t.repository_id, g.route, g.purpose, g.payer_id, g.credential_record_id, g.credential_version, g.status \
         FROM tasks.attempts a \
         JOIN tasks.task_state t ON t.task_id=a.task_id \
         JOIN tasks.funding_grants g ON g.attempt_id=a.id \
         WHERE a.id=$1 \
         UNION ALL SELECT a.standalone_task_id,t.runner_repository_id,g.route,g.purpose,g.payer_id,g.credential_record_id,g.credential_version,g.status \
         FROM tasks.attempts a JOIN tasks.standalone_review_tasks t ON t.id=a.standalone_task_id \
         JOIN tasks.standalone_review_funding_grants g ON g.attempt_id=a.id \
         WHERE a.id=$1",
    )
    .bind(turn)
    .fetch_optional(database.pool())
    .await?;
    let IssuedGrantRow {
        task_id,
        repository_id,
        route,
        purpose,
        payer_id,
        credential_record_id: record,
        credential_version: version,
        status,
    } = row.ok_or_else(|| anyhow::anyhow!("issued funding grant missing"))?;
    Ok(OrgAttempt {
        task_id,
        repository_id,
        route,
        purpose,
        payer_id,
        credential: record.zip(version),
        status,
    })
}

/// Freeze converted bytes and the issued route before the first Rust operator send.
pub async fn freeze_submit(
    work: &mut UnitOfWork<'_>,
    turn: Uuid,
    original: &[u8],
    converted: &[u8],
    route: &str,
) -> Result<()> {
    let updated = sqlx::query(
        "UPDATE tasks.attempts SET submit_body=$3 \
         WHERE id=$1 AND submit_body=$2 AND status='sending'",
    )
    .bind(turn)
    .bind(original)
    .bind(converted)
    .execute(work.connection())
    .await?
    .rows_affected();
    ensure!(updated == 1, "org submission changed before freeze");
    let contribution = sqlx::query(
        "UPDATE tasks.funding_grants SET route=$2 WHERE attempt_id=$1 \
         AND status='issued' AND (route IS NULL OR route=$2)",
    )
    .bind(turn)
    .bind(route)
    .execute(work.connection())
    .await?
    .rows_affected();
    let review = sqlx::query(
        "UPDATE tasks.standalone_review_funding_grants SET route=$2 WHERE attempt_id=$1 \
         AND status='issued' AND (route IS NULL OR route=$2)",
    )
    .bind(turn)
    .bind(route)
    .execute(work.connection())
    .await?
    .rows_affected();
    ensure!(
        contribution + review == 1,
        "funding grant route changed before freeze"
    );
    Ok(())
}

/// Idempotently attribute a settled provider request to its admitted task grant.
pub async fn record_usage(
    work: &mut UnitOfWork<'_>,
    event_id: Uuid,
    usage: &InferenceUsage,
) -> Result<()> {
    let tokens = usage.usage.as_ref();
    sqlx::query(
        "INSERT INTO tasks.funding_usage \
         (request_id,event_id,turn_id,grant_id,model,category,input_tokens,output_tokens, \
          cache_read_input_tokens,cache_creation_input_tokens,usage_unavailable,occurred_at) \
         SELECT $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12 \
         WHERE $4=$3::text AND (EXISTS (SELECT 1 FROM tasks.funding_grants g WHERE g.attempt_id=$3 \
             AND g.purpose IN ('task_org','task_contributed') AND g.status IN ('issued','revoked')) OR \
             EXISTS (SELECT 1 FROM tasks.standalone_review_funding_grants g WHERE g.attempt_id=$3 \
             AND g.purpose IN ('org','contributed') AND g.status IN ('issued','revoked'))) \
         ON CONFLICT (request_id) DO NOTHING",
    )
    .bind(usage.request_id)
    .bind(event_id)
    .bind(usage.turn_id)
    .bind(&usage.grant_id)
    .bind(&usage.model)
    .bind(&usage.category)
    .bind(
        tokens
            .map(|value| i64::try_from(value.input_tokens))
            .transpose()?,
    )
    .bind(
        tokens
            .map(|value| i64::try_from(value.output_tokens))
            .transpose()?,
    )
    .bind(
        tokens
            .and_then(|value| value.cache_read_input_tokens)
            .map(i64::try_from)
            .transpose()?,
    )
    .bind(
        tokens
            .and_then(|value| value.cache_creation_input_tokens)
            .map(i64::try_from)
            .transpose()?,
    )
    .bind(&usage.usage_unavailable)
    .bind(usage.finished.0)
    .execute(work.connection())
    .await?;
    Ok(())
}

/// Admit only a newer exchange lifecycle revision for a task attempt.
pub async fn admit_rust_revision(
    work: &mut UnitOfWork<'_>,
    turn: Uuid,
    revision: u64,
) -> Result<bool> {
    let revision = i64::try_from(revision)?;
    let updated: Option<Uuid> = sqlx::query_scalar(
        "UPDATE tasks.attempts SET rust_exchange_revision=$2 WHERE id=$1 \
         AND rust_exchange_revision<$2 RETURNING id",
    )
    .bind(turn)
    .bind(revision)
    .fetch_optional(work.connection())
    .await?;
    Ok(updated.is_some())
}

/// Find Rust-mode attempts not observed for five minutes, including missing terminal facts.
pub async fn due_rust_observations(database: &Database) -> Result<Vec<Uuid>> {
    Ok(sqlx::query_scalar(
        "SELECT id FROM tasks.attempts WHERE rust_exchange_revision>0 AND \
         (status IN ('queued','running') OR \
          (status IN ('finished','unknown','cancelled') AND NOT finished_event_emitted)) \
         AND coalesce(rust_observed_at,admitted_at)<now()-interval '5 minutes' \
         ORDER BY coalesce(rust_observed_at,admitted_at),id LIMIT 100",
    )
    .fetch_all(database.pool())
    .await?)
}
