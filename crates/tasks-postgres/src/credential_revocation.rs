//! Credential-version fences and durable exchange retirement effects.

use anyhow::Result;
use concurro_contracts::CredentialChanged;
use concurro_db::Database;
use concurro_db::UnitOfWork;
use uuid::Uuid;

use crate::enqueue_effect;

/// Serialize version retirement with insertion of an exact-version funding grant.
pub async fn lock_credential_versions(work: &mut UnitOfWork<'_>) -> Result<()> {
    sqlx::query("SELECT pg_advisory_xact_lock(hashtext('concurro-credential-versions'))")
        .execute(work.connection())
        .await?;
    Ok(())
}

/// Persist source facts and retire every submitted grant using a revoked version.
pub async fn apply_credential_change(
    work: &mut UnitOfWork<'_>,
    change: &CredentialChanged,
) -> Result<()> {
    lock_credential_versions(work).await?;
    for version in &change.revoked {
        sqlx::query(
            "INSERT INTO tasks.revoked_credential_versions \
             (payer_id,record_id,version,source_sequence,revoked_at) VALUES($1,$2,$3,$4,$5) \
             ON CONFLICT DO NOTHING",
        )
        .bind(change.user_id)
        .bind(&version.record_id)
        .bind(version.version)
        .bind(change.sequence)
        .bind(change.at.0)
        .execute(work.connection())
        .await?;
        let contribution: Vec<Uuid> = sqlx::query_scalar(
            "UPDATE tasks.funding_grants SET status='revoked',revoked_at=COALESCE(revoked_at,now()) \
             WHERE purpose='task_contributed' AND payer_id=$1 AND credential_record_id=$2 \
             AND credential_version=$3 RETURNING attempt_id",
        )
        .bind(change.user_id)
        .bind(&version.record_id)
        .bind(version.version)
        .fetch_all(work.connection())
        .await?;
        let standalone: Vec<Uuid> = sqlx::query_scalar(
            "UPDATE tasks.standalone_review_funding_grants SET status='revoked' \
             WHERE purpose='contributed' AND payer_id=$1 AND credential_record_id=$2 \
             AND credential_version=$3 RETURNING attempt_id",
        )
        .bind(change.user_id)
        .bind(&version.record_id)
        .bind(version.version)
        .fetch_all(work.connection())
        .await?;
        for turn in contribution.into_iter().chain(standalone) {
            enqueue_retirement(work, turn).await?;
        }
    }
    Ok(())
}

/// Enqueue the existing idempotent turn retirement if a submit may have reached exchange.
pub async fn enqueue_retirement(work: &mut UnitOfWork<'_>, turn: Uuid) -> Result<()> {
    let id: Option<i64> = sqlx::query_scalar(
        "INSERT INTO tasks.effects(turn_id,kind,status) \
         SELECT $1,'retire','recorded' WHERE EXISTS \
         (SELECT 1 FROM tasks.effects WHERE turn_id=$1 AND kind='submit' AND submit_attempts>0) \
         ON CONFLICT (turn_id,kind) DO NOTHING RETURNING id",
    )
    .bind(turn)
    .fetch_optional(work.connection())
    .await?;
    if let Some(id) = id {
        enqueue_effect(work, id, 0, 0).await?;
    }
    Ok(())
}

/// Exact exchange grant IDs whose spending a withdrawn provider slot must stop.
pub async fn withdrawn_grants(
    database: &Database,
    task: Uuid,
    provider: &str,
    payer: i64,
    epoch: i64,
) -> Result<Vec<String>> {
    let ids: Vec<Uuid> = sqlx::query_scalar(
        "SELECT attempt_id FROM tasks.funding_grants WHERE task_id=$1 AND provider=$2 \
         AND payer_id=$3 AND slot_epoch<=$4 AND purpose='task_contributed' \
         UNION SELECT attempt_id FROM tasks.standalone_review_funding_grants \
         WHERE task_id=$1 AND provider=$2 AND payer_id=$3 AND slot_epoch<=$4 \
         AND purpose='contributed'",
    )
    .bind(task)
    .bind(provider)
    .bind(payer)
    .bind(epoch)
    .fetch_all(database.pool())
    .await?;
    Ok(ids.into_iter().map(|turn| turn.to_string()).collect())
}
