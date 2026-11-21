//! Durable coverage-gap receipts and source refresh publication.

use chrono::{DateTime, Utc};
use concurro_db::{Database, UnitOfWork};
use concurro_events_postgres::Outbox;
use concurro_tasks_core::TaskState;
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

use crate::append_task_snapshot;

/// One unresolved ingress interval that needs source reconciliation.
#[derive(Clone, sqlx::FromRow)]
pub struct PendingCoverageGap {
    /// Stable coverage event identity.
    pub event_id: Uuid,
    /// First potentially missing delivery time.
    pub from_at: DateTime<Utc>,
    /// Last potentially missing delivery time.
    pub to_at: DateTime<Utc>,
    /// GitHub installation that emitted the gap.
    pub installation_id: i64,
}

/// Record a gap within the event consumer's acknowledgement transaction.
pub async fn record_coverage_gap(
    work: &mut UnitOfWork<'_>,
    id: Uuid,
    from_at: DateTime<Utc>,
    to_at: DateTime<Utc>,
    installation_id: i64,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO tasks.coverage_gaps (event_id,from_at,to_at,installation_id) \
         VALUES ($1,$2,$3,$4) ON CONFLICT (event_id) DO NOTHING",
    )
    .bind(id)
    .bind(from_at)
    .bind(to_at)
    .bind(installation_id)
    .execute(work.connection())
    .await?;
    Ok(())
}

/// Return unresolved gaps after worker restart or a failed GitHub read.
pub async fn pending_coverage_gaps(
    database: &Database,
) -> Result<Vec<PendingCoverageGap>, sqlx::Error> {
    sqlx::query_as(
        "SELECT event_id,from_at,to_at,installation_id FROM tasks.coverage_gaps \
         WHERE recovered_at IS NULL AND retry_at<=now() ORDER BY from_at,event_id",
    )
    .fetch_all(database.pool())
    .await
}

/// List known live comment identities for a deletion readback after a gap.
pub async fn known_live_comments(
    database: &Database,
    repository_id: i64,
) -> Result<Vec<(i64, i64)>, sqlx::Error> {
    sqlx::query_as(
        "SELECT comment_id,issue_number FROM tasks.comment_revisions \
         WHERE repository_id=$1 GROUP BY comment_id,issue_number \
         HAVING NOT bool_or(deleted) ORDER BY comment_id",
    )
    .bind(repository_id)
    .fetch_all(database.pool())
    .await
}

/// Check whether a webhook already supplied an edited revision's authenticated actor.
pub async fn comment_revision_recorded(
    database: &Database,
    comment_id: i64,
    source_at: DateTime<Utc>,
) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM tasks.comment_revisions \
         WHERE comment_id=$1 AND source_at=$2 AND NOT deleted)",
    )
    .bind(comment_id)
    .bind(source_at)
    .fetch_one(database.pool())
    .await
}

/// Keep a failed scan visible to an operator while the worker retries it.
pub async fn fail_coverage_gap(
    database: &Database,
    id: Uuid,
    error: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE tasks.coverage_gaps SET attempts=attempts+1,last_error=left($2,500), \
         retry_at=now()+LEAST(interval '1 hour', \
             interval '30 seconds'*(1 << LEAST(attempts,7))) \
         WHERE event_id=$1 AND recovered_at IS NULL",
    )
    .bind(id)
    .bind(error)
    .execute(database.pool())
    .await?;
    Ok(())
}

/// Retain one source that cannot be reconstructed from available GitHub evidence.
pub async fn record_coverage_source_failure(
    database: &Database,
    gap_id: Uuid,
    repository_id: i64,
    source_kind: &str,
    source_id: &str,
    reason: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO tasks.coverage_source_failures \
         (gap_id,repository_id,source_kind,source_id,reason) \
         VALUES ($1,$2,$3,$4,left($5,500)) \
         ON CONFLICT (gap_id,repository_id,source_kind,source_id) DO UPDATE SET \
         reason=EXCLUDED.reason,last_seen_at=now(),resolved_at=NULL",
    )
    .bind(gap_id)
    .bind(repository_id)
    .bind(source_kind)
    .bind(source_id)
    .bind(reason)
    .execute(database.pool())
    .await?;
    Ok(())
}

/// Close a source failure when a later scan reconstructs its authenticated source.
pub async fn resolve_coverage_source_failure(
    database: &Database,
    gap_id: Uuid,
    repository_id: i64,
    source_kind: &str,
    source_id: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE tasks.coverage_source_failures SET resolved_at=now() \
         WHERE gap_id=$1 AND repository_id=$2 AND source_kind=$3 AND source_id=$4 \
         AND resolved_at IS NULL",
    )
    .bind(gap_id)
    .bind(repository_id)
    .bind(source_kind)
    .bind(source_id)
    .execute(database.pool())
    .await?;
    Ok(())
}

/// Count source failures still requiring an authenticated reconstruction.
pub async fn unresolved_coverage_source_count(
    database: &Database,
    gap_id: Uuid,
    repository_id: i64,
) -> Result<i64, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT count(*) FROM tasks.coverage_source_failures \
         WHERE gap_id=$1 AND repository_id=$2 AND resolved_at IS NULL",
    )
    .bind(gap_id)
    .bind(repository_id)
    .fetch_one(database.pool())
    .await
}

/// Republish all task snapshots after fresh external observations settle.
pub async fn complete_coverage_gap(
    database: &Database,
    outbox: &Outbox,
    id: Uuid,
    repository_ids: &[i64],
) -> anyhow::Result<()> {
    let mut work = database.begin().await?;
    let states: Vec<Json<Value>> = sqlx::query_scalar(
        "SELECT state FROM tasks.task_state WHERE repository_id=ANY($1) ORDER BY repository_id,issue_number",
    )
    .bind(repository_ids)
    .fetch_all(work.connection())
    .await?;
    for Json(value) in states {
        let state: TaskState = serde_json::from_value(value)?;
        append_task_snapshot(&mut work, outbox, &state).await?;
    }
    sqlx::query(
        "UPDATE tasks.coverage_gaps SET recovered_at=now(),last_error=NULL WHERE event_id=$1",
    )
    .bind(id)
    .execute(work.connection())
    .await?;
    work.commit().await?;
    Ok(())
}
