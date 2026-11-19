//! Read-only status and bounded remapping selections for ingress operators.

use chrono::{DateTime, Utc};
use concurro_db::Database;
use concurro_ingress_core::RecoveryDecision;
use serde::Serialize;

/// The last complete App delivery scan.
#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct RecoveryScanStatus {
    /// Scan start, used as the coverage boundary.
    pub started_at: DateTime<Utc>,
    /// Scan completion time.
    pub completed_at: DateTime<Utc>,
    /// GUIDs without a receipt or successful attempt.
    pub unresolved_guids: Vec<String>,
    /// GUIDs still being attempted by GitHub.
    pub pending_guids: Vec<String>,
    /// Attempt IDs requested for redelivery.
    pub redelivered_ids: Vec<i64>,
}

/// A recent retained-journal replay request.
#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct ReplayStatus {
    /// Consumer requesting recovery.
    pub consumer: String,
    /// Request time.
    pub requested_at: DateTime<Utc>,
    /// Stream incarnation supplied by the consumer.
    pub floor_created: String,
    /// Last acknowledged sequence supplied by the consumer.
    pub floor_sequence: i64,
    /// Whether any earlier row may be irrecoverable.
    pub unrecoverable: bool,
}

/// Ingress coverage, mapping and publication health.
#[derive(Debug, Serialize)]
pub struct OperatorStatus {
    /// Latest covered delivery time.
    pub covered_until: Option<DateTime<Utc>>,
    /// First ingress startup time, retained across restarts.
    pub first_started_at: DateTime<Utc>,
    /// Number of receipts blocking normal coverage.
    pub unmappable_count: i64,
    /// Earliest receipt blocking normal coverage.
    pub earliest_unmappable: Option<DateTime<Utc>>,
    /// Deliveries with no model since the requested time.
    pub unmodeled_count: i64,
    /// Age of the oldest unpublished outbox row in seconds.
    pub oldest_unpublished_age_seconds: Option<f64>,
    /// Last completed recovery scan, if any.
    pub last_scan: Option<RecoveryScanStatus>,
    /// Recent retained replay requests.
    pub recent_replays: Vec<ReplayStatus>,
}

/// Persist one complete recovery listing and its redelivery decisions.
pub async fn record_recovery_scan(
    database: &Database,
    started_at: DateTime<Utc>,
    decision: &RecoveryDecision,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO ingress.recovery_scan \
         (id, started_at, completed_at, unresolved_guids, pending_guids, redelivered_ids) \
         VALUES (true, $1, clock_timestamp(), $2, $3, $4) \
         ON CONFLICT (id) DO UPDATE SET started_at = EXCLUDED.started_at, \
         completed_at = EXCLUDED.completed_at, unresolved_guids = EXCLUDED.unresolved_guids, \
         pending_guids = EXCLUDED.pending_guids, redelivered_ids = EXCLUDED.redelivered_ids",
    )
    .bind(started_at)
    .bind(&decision.unresolved_guids)
    .bind(&decision.pending_guids)
    .bind(&decision.redeliver)
    .execute(database.pool())
    .await?;
    Ok(())
}

/// Read the ingress status without altering any durable state.
pub async fn status(
    database: &Database,
    unmodeled_since: DateTime<Utc>,
) -> Result<OperatorStatus, sqlx::Error> {
    let (covered_until, first_started_at): (Option<DateTime<Utc>>, DateTime<Utc>) =
        sqlx::query_as("SELECT covered_until, first_started_at FROM ingress.coverage WHERE id")
            .fetch_one(database.pool())
            .await?;
    let (unmappable_count, earliest_unmappable): (i64, Option<DateTime<Utc>>) = sqlx::query_as(
        "SELECT count(*), min(received_at) FROM ingress.deliveries WHERE outcome = 'unmappable'",
    )
    .fetch_one(database.pool())
    .await?;
    let unmodeled_count: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM ingress.deliveries WHERE outcome = 'unmodeled' AND received_at >= $1",
    )
    .bind(unmodeled_since)
    .fetch_one(database.pool())
    .await?;
    let oldest_unpublished_age_seconds: Option<f64> = sqlx::query_scalar(
        "SELECT extract(epoch FROM clock_timestamp() - min(created_at))::float8 \
         FROM events.outbox WHERE published_at IS NULL",
    )
    .fetch_one(database.pool())
    .await?;
    let last_scan = sqlx::query_as::<_, RecoveryScanStatus>(
        "SELECT started_at, completed_at, unresolved_guids, pending_guids, redelivered_ids \
         FROM ingress.recovery_scan WHERE id",
    )
    .fetch_optional(database.pool())
    .await?;
    let recent_replays = sqlx::query_as::<_, ReplayStatus>(
        "SELECT consumer, requested_at, floor_created, floor_sequence, unrecoverable \
         FROM events.replay_requests ORDER BY id DESC LIMIT 10",
    )
    .fetch_all(database.pool())
    .await?;
    Ok(OperatorStatus {
        covered_until,
        first_started_at,
        unmappable_count,
        earliest_unmappable,
        unmodeled_count,
        oldest_unpublished_age_seconds,
        last_scan,
        recent_replays,
    })
}

/// One stored receipt selected for a remapping pass.
#[derive(Debug, sqlx::FromRow)]
pub struct RemapCandidate {
    /// Original receipt time.
    pub received_at: DateTime<Utc>,
    /// Delivery GUID.
    pub guid: String,
}

/// Select a stable page of stored payloads that lack the current mapping revision.
pub async fn remap_candidates(
    database: &Database,
    from: DateTime<Utc>,
    to: DateTime<Utc>,
    event: Option<&str>,
    revision: i32,
    after: Option<&RemapCandidate>,
    limit: i64,
) -> Result<Vec<RemapCandidate>, sqlx::Error> {
    sqlx::query_as(
        "SELECT received_at, guid FROM ingress.deliveries WHERE received_at >= $1 \
         AND received_at < $2 AND ($3::text IS NULL OR event = $3) \
         AND payload IS NOT NULL AND mapping_revision IS DISTINCT FROM $4 \
         AND ($5::timestamptz IS NULL OR (received_at, guid) > ($5, $6)) \
         ORDER BY received_at, guid LIMIT $7",
    )
    .bind(from)
    .bind(to)
    .bind(event)
    .bind(revision)
    .bind(after.map(|candidate| candidate.received_at))
    .bind(after.map(|candidate| candidate.guid.as_str()))
    .bind(limit)
    .fetch_all(database.pool())
    .await
}
