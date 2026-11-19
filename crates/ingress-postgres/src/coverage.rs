//! Atomic coverage advancement and delayed publication.

use chrono::{DateTime, Duration, Utc};
use concurro_contracts::{CoverageAdvanced, CoverageGap, Envelope, EventId, Timestamp};
use concurro_db::{Database, UnitOfWork};
use concurro_events_postgres::{Outbox, OutboxError, capture_barrier, take_ready_barrier};

/// Failure of an atomic coverage operation.
#[derive(Debug, thiserror::Error)]
pub enum CoverageError {
    /// The requested interval does not advance in time.
    #[error("coverage gap must have from before to")]
    InvalidInterval,
    /// A manual gap cannot certify a future interval.
    #[error("coverage gap ends after the database clock")]
    FutureInterval,
    /// PostgreSQL rejected the state change.
    #[error(transparent)]
    Database(#[from] sqlx::Error),
    /// A coverage fact could not be admitted to the outbox.
    #[error(transparent)]
    Outbox(#[from] OutboxError),
}

/// Append a gap fact for each accepted installation in the caller's transaction.
async fn append_gaps(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    from: DateTime<Utc>,
    to: DateTime<Utc>,
    installations: &[i64],
) -> Result<(), CoverageError> {
    for &installation_id in installations {
        let event = Envelope::fact(
            EventId::mint(),
            "ingress",
            Timestamp(to),
            CoverageGap {
                from: Timestamp(from),
                to: Timestamp(to),
                installation_id,
            },
        );
        outbox.append(uow, &event).await?;
    }
    Ok(())
}

/// Advance only after a complete recovery scan, capturing an outbox barrier.
pub async fn advance_coverage(
    database: &Database,
    outbox: &Outbox,
    scan_started: DateTime<Utc>,
    scan_resolved: bool,
    installations: &[i64],
) -> Result<Option<DateTime<Utc>>, CoverageError> {
    let mut uow = database.begin().await?;
    sqlx::query("LOCK TABLE ingress.deliveries IN SHARE MODE")
        .execute(uow.connection())
        .await?;
    let (previous, first_started): (Option<DateTime<Utc>>, DateTime<Utc>) = sqlx::query_as(
        "SELECT covered_until, first_started_at FROM ingress.coverage WHERE id FOR UPDATE",
    )
    .fetch_one(uow.connection())
    .await?;
    let unmappable: Option<DateTime<Utc>> = sqlx::query_scalar(
        "SELECT received_at FROM ingress.deliveries \
         WHERE outcome = 'unmappable' AND ($1::timestamptz IS NULL OR received_at > $1) \
         ORDER BY received_at LIMIT 1",
    )
    .bind(previous)
    .fetch_optional(uow.connection())
    .await?;
    let expired = scan_started - previous.unwrap_or(first_started) >= Duration::days(3);
    if !scan_resolved && !expired {
        return Ok(None);
    }
    if previous.is_none() && !expired && unmappable.is_some_and(|blocked| blocked <= scan_started) {
        return Ok(None);
    }
    let gap_from = if previous.is_none() {
        Some(first_started - Duration::days(3))
    } else if expired {
        previous
    } else {
        None
    };
    let target = if gap_from.is_some() {
        scan_started
    } else if let Some(blocked_at) = unmappable {
        scan_started.min(blocked_at - Duration::microseconds(1))
    } else {
        scan_started
    };
    if previous.is_some_and(|covered| target <= covered) {
        return Ok(None);
    }
    if let Some(from) = gap_from {
        append_gaps(&mut uow, outbox, from, target, installations).await?;
    }
    sqlx::query("UPDATE ingress.coverage SET covered_until = $1 WHERE id")
        .bind(target)
        .execute(uow.connection())
        .await?;
    capture_barrier(&mut uow, target).await?;
    uow.commit().await?;
    Ok(Some(target))
}

/// Publish an operator-declared incomplete period and its publication barrier.
pub async fn declare_gap(
    database: &Database,
    outbox: &Outbox,
    from: DateTime<Utc>,
    to: DateTime<Utc>,
    installations: &[i64],
) -> Result<(), CoverageError> {
    if from >= to {
        return Err(CoverageError::InvalidInterval);
    }
    let mut uow = database.begin().await?;
    let now: DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(uow.connection())
        .await?;
    if to > now {
        return Err(CoverageError::FutureInterval);
    }
    sqlx::query("LOCK TABLE ingress.deliveries IN SHARE MODE")
        .execute(uow.connection())
        .await?;
    let (previous, first_started): (Option<DateTime<Utc>>, DateTime<Utc>) = sqlx::query_as(
        "SELECT covered_until, first_started_at FROM ingress.coverage WHERE id FOR UPDATE",
    )
    .fetch_one(uow.connection())
    .await?;
    let gap_from = previous.map_or(from.min(first_started - Duration::days(3)), |_| from);
    append_gaps(&mut uow, outbox, gap_from, to, installations).await?;
    let covered_until = previous.map_or(to, |previous| previous.max(to));
    sqlx::query("UPDATE ingress.coverage SET covered_until = $1 WHERE id")
        .bind(covered_until)
        .execute(uow.connection())
        .await?;
    capture_barrier(&mut uow, covered_until).await?;
    uow.commit().await?;
    Ok(())
}

/// Emit the coverage fact after every captured delivery and gap has published.
pub async fn finalize_coverage(
    database: &Database,
    outbox: &Outbox,
) -> Result<bool, CoverageError> {
    let mut uow = database.begin().await?;
    let Some(covered_until) = take_ready_barrier(&mut uow).await? else {
        return Ok(false);
    };
    let occurred_at: DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
        .fetch_one(uow.connection())
        .await?;
    let event = Envelope::fact(
        EventId::mint(),
        "ingress",
        Timestamp(occurred_at),
        CoverageAdvanced {
            covered_until: Timestamp(covered_until),
        },
    );
    outbox.append(&mut uow, &event).await?;
    uow.commit().await?;
    Ok(true)
}
