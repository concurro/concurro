//! Atomic GitHub delivery receipts and compressed payload storage.

mod coverage;
mod operator;

pub use coverage::{CoverageError, advance_coverage, declare_gap, finalize_coverage};
pub use operator::{
    OperatorStatus, RemapCandidate, record_recovery_scan, remap_candidates, status,
};

use std::collections::HashSet;

use chrono::{DateTime, Utc};
use concurro_db::{Database, UnitOfWork};
use concurro_events_postgres::{Outbox, OutboxError};
use concurro_ingress_core::{MAPPING_REVISION, MappedEvent, Mapping, map_delivery};
use sqlx::PgConnection;
use sqlx::migrate::Migrator;

static MIGRATIONS: Migrator = sqlx::migrate!("./migrations");

/// Dedicated session ensuring only one ingress instance scans App deliveries.
pub struct RecoveryLease {
    /// Session holding the recovery advisory lock.
    _connection: PgConnection,
}

impl RecoveryLease {
    /// Acquire the recovery lease without waiting for another instance.
    pub async fn try_acquire(database: &Database) -> Result<Option<Self>, sqlx::Error> {
        let mut connection = database.pool().acquire().await?.detach();
        let acquired: bool = sqlx::query_scalar(
            "SELECT pg_try_advisory_lock(hashtextextended('concurro:ingress:recovery', 0))",
        )
        .fetch_one(&mut connection)
        .await?;
        Ok(acquired.then_some(Self {
            _connection: connection,
        }))
    }
}

/// Find GUIDs for which ingress already committed a receipt.
pub async fn received_guids(
    database: &Database,
    guids: &[String],
) -> Result<HashSet<String>, sqlx::Error> {
    let rows: Vec<String> =
        sqlx::query_scalar("SELECT guid FROM ingress.deliveries WHERE guid = ANY($1)")
            .bind(guids)
            .fetch_all(database.pool())
            .await?;
    Ok(rows.into_iter().collect())
}

/// Read PostgreSQL's clock before an external delivery scan starts.
pub async fn database_time(database: &Database) -> Result<DateTime<Utc>, sqlx::Error> {
    sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(database.pool())
        .await
}

/// Drop retained raw bytes past the configured remapping window, keeping receipts.
pub async fn prune_payloads(
    database: &Database,
    retention_seconds: i64,
) -> Result<u64, sqlx::Error> {
    let result = sqlx::query(
        "UPDATE ingress.deliveries SET payload = NULL WHERE payload IS NOT NULL \
         AND received_at < now() - $1 * interval '1 second'",
    )
    .bind(retention_seconds)
    .execute(database.pool())
    .await?;
    Ok(result.rows_affected())
}

/// Input already authenticated by the public HTTP endpoint.
pub struct SignedDelivery<'a> {
    /// GitHub delivery GUID.
    pub guid: &'a str,
    /// GitHub event header.
    pub event: &'a str,
    /// Original signed request body.
    pub payload: &'a [u8],
    /// Installation IDs accepted by this ingress.
    pub installations: &'a [i64],
}

/// Persisted disposition of one signed delivery.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReceiptOutcome {
    /// A typed event was appended to the outbox.
    Published,
    /// The installation is outside the configured set.
    OtherInstallation,
    /// There is no mapper for this event type yet.
    Unmodeled,
    /// The mapper or outbox admission rejected the event.
    Unmappable,
}

impl ReceiptOutcome {
    /// Database spelling of the disposition.
    fn as_str(self) -> &'static str {
        match self {
            Self::Published => "published",
            Self::OtherInstallation => "other_installation",
            Self::Unmodeled => "unmodeled",
            Self::Unmappable => "unmappable",
        }
    }
}

/// Append whichever typed fact the pure mapper produced.
async fn append_mapped(
    outbox: &Outbox,
    uow: &mut UnitOfWork<'_>,
    event: &MappedEvent,
) -> Result<i64, OutboxError> {
    match event {
        MappedEvent::Issues(event) => outbox.append(uow, event).await,
        MappedEvent::IssueComment(event) => outbox.append(uow, event).await,
        MappedEvent::PullRequest(event) => outbox.append(uow, event).await,
        MappedEvent::PullRequestReview(event) => outbox.append(uow, event).await,
        MappedEvent::PullRequestReviewComment(event) => outbox.append(uow, event).await,
        MappedEvent::PullRequestReviewThread(event) => outbox.append(uow, event).await,
        MappedEvent::Push(event) => outbox.append(uow, event).await,
        MappedEvent::Status(event) => outbox.append(uow, event).await,
        MappedEvent::CheckRun(event) => outbox.append(uow, event).await,
        MappedEvent::CheckSuite(event) => outbox.append(uow, event).await,
        MappedEvent::WorkflowRun(event) => outbox.append(uow, event).await,
    }
}

/// Result of one durable receipt attempt.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RecordResult {
    /// The GUID was previously committed.
    Duplicate,
    /// The signed receipt committed with this disposition.
    Recorded(ReceiptOutcome),
}

/// Failure before a receipt transaction could commit.
#[derive(Debug, thiserror::Error)]
pub enum RecordError {
    /// PostgreSQL refused the receipt or event transaction.
    #[error(transparent)]
    Database(#[from] sqlx::Error),
    /// Compression of the signed bytes failed.
    #[error(transparent)]
    Compress(#[from] std::io::Error),
    /// Outbox admission failed for a reason other than message size.
    #[error(transparent)]
    Outbox(#[from] OutboxError),
}

/// Install the ingress schema beside the shared event schema.
pub async fn migrate(database: &Database) -> anyhow::Result<()> {
    database.migrate_in_schema("ingress", &MIGRATIONS).await
}

/// Commit one authenticated receipt and its mapped event as one operation.
pub async fn record_delivery(
    database: &Database,
    outbox: &Outbox,
    delivery: &SignedDelivery<'_>,
) -> Result<RecordResult, RecordError> {
    let mut uow = database.begin().await?;
    let result = record_delivery_in(&mut uow, outbox, delivery).await?;
    uow.commit().await?;
    Ok(result)
}

/// Write a receipt through the caller's transaction without committing it.
pub async fn record_delivery_in(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    delivery: &SignedDelivery<'_>,
) -> Result<RecordResult, RecordError> {
    sqlx::query("LOCK TABLE ingress.deliveries IN ROW EXCLUSIVE MODE")
        .execute(uow.connection())
        .await?;
    let received_at: DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(uow.connection())
        .await?;
    let mapping = map_delivery(
        delivery.guid,
        delivery.event,
        delivery.payload,
        received_at,
        delivery.installations,
    );
    let (metadata, outcome, error, revision) = match &mapping {
        Mapping::OtherInstallation(metadata) => {
            (metadata, ReceiptOutcome::OtherInstallation, None, None)
        }
        Mapping::Unmodeled(metadata) => (metadata, ReceiptOutcome::Unmodeled, None, None),
        Mapping::Unmappable(metadata, error) => (
            metadata,
            ReceiptOutcome::Unmappable,
            Some(error.as_str()),
            None,
        ),
        Mapping::Published(metadata, _) => (
            metadata,
            ReceiptOutcome::Published,
            None,
            Some(i32::try_from(MAPPING_REVISION).expect("mapping revision fits i32")),
        ),
    };
    let payload = zstd::bulk::compress(delivery.payload, 3)?;
    let inserted: Option<String> = sqlx::query_scalar(
        "INSERT INTO ingress.deliveries \
         (guid, event, action, installation_id, repository_id, received_at, payload, \
          outcome, mapping_revision, error) \
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) \
         ON CONFLICT (guid) DO NOTHING RETURNING guid",
    )
    .bind(delivery.guid)
    .bind(&metadata.event)
    .bind(&metadata.action)
    .bind(metadata.installation_id)
    .bind(metadata.repository_id)
    .bind(received_at)
    .bind(payload)
    .bind(outcome.as_str())
    .bind(revision)
    .bind(error)
    .fetch_optional(uow.connection())
    .await?;
    if inserted.is_none() {
        return Ok(RecordResult::Duplicate);
    }
    if let Mapping::Published(_, event) = mapping {
        match append_mapped(outbox, uow, &event).await {
            Ok(_) => {}
            Err(OutboxError::MessageTooLarge { .. }) => {
                sqlx::query(
                    "UPDATE ingress.deliveries SET outcome = 'unmappable', \
                     mapping_revision = NULL, error = 'event_too_large' WHERE guid = $1",
                )
                .bind(delivery.guid)
                .execute(uow.connection())
                .await?;
                return Ok(RecordResult::Recorded(ReceiptOutcome::Unmappable));
            }
            Err(error) => return Err(error.into()),
        }
    }
    Ok(RecordResult::Recorded(outcome))
}

/// Read one receipt and its authoritative raw body for remapping.
#[derive(Debug, sqlx::FromRow)]
pub struct StoredDelivery {
    /// GitHub delivery GUID.
    pub guid: String,
    /// GitHub event family.
    pub event: String,
    /// Original receipt time.
    pub received_at: DateTime<Utc>,
    /// Compressed signed request body, if retention has not pruned it.
    pub payload: Option<Vec<u8>>,
    /// Current disposition.
    pub outcome: String,
    /// Last successful mapping revision.
    pub mapping_revision: Option<i32>,
}

/// Load one stored delivery without altering its receipt.
pub async fn get_delivery(
    database: &Database,
    guid: &str,
) -> Result<Option<StoredDelivery>, sqlx::Error> {
    sqlx::query_as(
        "SELECT guid, event, received_at, payload, outcome, mapping_revision \
         FROM ingress.deliveries WHERE guid = $1",
    )
    .bind(guid)
    .fetch_optional(database.pool())
    .await
}

/// Read the earliest unmappable receipt time, if one remains.
pub async fn earliest_unmappable(
    uow: &mut UnitOfWork<'_>,
) -> Result<Option<DateTime<Utc>>, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT received_at FROM ingress.deliveries \
         WHERE outcome = 'unmappable' ORDER BY received_at LIMIT 1",
    )
    .fetch_optional(uow.connection())
    .await
}

/// Preserve receipt and event atomically when remapping a stored payload.
pub async fn remap_delivery(
    database: &Database,
    outbox: &Outbox,
    guid: &str,
    installations: &[i64],
) -> Result<RecordResult, RecordError> {
    let mut uow = database.begin().await?;
    sqlx::query("LOCK TABLE ingress.deliveries IN ROW EXCLUSIVE MODE")
        .execute(uow.connection())
        .await?;
    let stored: Option<StoredDelivery> = sqlx::query_as(
        "SELECT guid, event, received_at, payload, outcome, mapping_revision \
         FROM ingress.deliveries WHERE guid = $1 FOR UPDATE",
    )
    .bind(guid)
    .fetch_optional(uow.connection())
    .await?;
    let Some(stored) = stored else {
        return Ok(RecordResult::Duplicate);
    };
    let Some(payload) = stored.payload else {
        return Ok(RecordResult::Duplicate);
    };
    let raw = zstd::bulk::decompress(&payload, 25_000_000)?;
    let mapping = map_delivery(
        &stored.guid,
        &stored.event,
        &raw,
        stored.received_at,
        installations,
    );
    let result = match mapping {
        Mapping::Published(_, event) => {
            let revision = i32::try_from(MAPPING_REVISION).expect("revision fits i32");
            if stored.mapping_revision == Some(revision) {
                RecordResult::Duplicate
            } else {
                match append_mapped(outbox, &mut uow, &event).await {
                    Ok(_) => {
                        sqlx::query(
                            "UPDATE ingress.deliveries SET outcome = 'published', \
                             mapping_revision = $2, error = NULL WHERE guid = $1",
                        )
                        .bind(guid)
                        .bind(revision)
                        .execute(uow.connection())
                        .await?;
                        RecordResult::Recorded(ReceiptOutcome::Published)
                    }
                    Err(OutboxError::MessageTooLarge { .. }) => {
                        if stored.outcome == "published" {
                            return Ok(RecordResult::Duplicate);
                        }
                        sqlx::query(
                            "UPDATE ingress.deliveries SET outcome = 'unmappable', \
                             mapping_revision = NULL, error = 'event_too_large' WHERE guid = $1",
                        )
                        .bind(guid)
                        .execute(uow.connection())
                        .await?;
                        RecordResult::Recorded(ReceiptOutcome::Unmappable)
                    }
                    Err(error) => return Err(error.into()),
                }
            }
        }
        Mapping::Unmappable(_, error) => {
            if stored.outcome == "published" {
                return Ok(RecordResult::Duplicate);
            }
            sqlx::query(
                "UPDATE ingress.deliveries SET outcome = 'unmappable', \
                 mapping_revision = NULL, error = $2 WHERE guid = $1",
            )
            .bind(guid)
            .bind(error)
            .execute(uow.connection())
            .await?;
            RecordResult::Recorded(ReceiptOutcome::Unmappable)
        }
        Mapping::Unmodeled(_) | Mapping::OtherInstallation(_) if stored.outcome == "published" => {
            RecordResult::Duplicate
        }
        Mapping::Unmodeled(_) => {
            sqlx::query(
                "UPDATE ingress.deliveries SET outcome = 'unmodeled', mapping_revision = NULL, \
                 error = NULL WHERE guid = $1",
            )
            .bind(guid)
            .execute(uow.connection())
            .await?;
            RecordResult::Recorded(ReceiptOutcome::Unmodeled)
        }
        Mapping::OtherInstallation(_) => {
            sqlx::query(
                "UPDATE ingress.deliveries SET outcome = 'other_installation', mapping_revision = NULL, \
                 error = NULL WHERE guid = $1",
            )
            .bind(guid)
            .execute(uow.connection())
            .await?;
            RecordResult::Recorded(ReceiptOutcome::OtherInstallation)
        }
    };
    uow.commit().await?;
    Ok(result)
}
