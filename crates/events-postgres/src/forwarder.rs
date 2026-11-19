//! Typed storage operations for a leased outbox forwarder.

use concurro_db::{Database, UnitOfWork};
use sqlx::PgConnection;
use uuid::Uuid;

/// An unpublished event with its original admitted wire bytes.
#[derive(Debug, sqlx::FromRow)]
pub struct OutboxRecord {
    /// Producer-local insertion sequence.
    pub sequence: i64,
    /// Stable event identifier.
    pub id: Uuid,
    /// NATS publish subject.
    pub subject: String,
    /// Contract major version.
    pub version: i32,
    /// Bytes stored at append time.
    pub wire_bytes: Vec<u8>,
    /// Number of publish failures recorded for backoff.
    pub failure_count: i32,
}

/// Health of the oldest event still waiting to publish.
#[derive(Debug, sqlx::FromRow)]
pub struct UnpublishedStatus {
    /// Seconds since the event was committed.
    pub age_seconds: f64,
    /// Most recent publish error, if one occurred.
    pub last_error: Option<String>,
}

/// A dedicated Postgres session holding the one-forwarder advisory lock.
pub struct ForwarderLease {
    connection: PgConnection,
}

impl ForwarderLease {
    /// Acquire the producer's forwarder lock, or report another active owner.
    pub async fn try_acquire(database: &Database) -> Result<Option<Self>, sqlx::Error> {
        let mut connection = database.pool().acquire().await?.detach();
        let acquired: bool = sqlx::query_scalar(
            "SELECT pg_try_advisory_lock(hashtextextended('concurro:outbox', 0))",
        )
        .fetch_one(&mut connection)
        .await?;
        Ok(acquired.then_some(Self { connection }))
    }

    /// Read the next row eligible to publish.
    pub async fn next(&mut self) -> Result<Option<OutboxRecord>, sqlx::Error> {
        sqlx::query_as(
            "SELECT sequence, id, subject, version, wire_bytes, failure_count FROM events.outbox \
             WHERE published_at IS NULL AND (retry_at IS NULL OR retry_at <= now()) \
             ORDER BY sequence LIMIT 1",
        )
        .fetch_optional(&mut self.connection)
        .await
    }

    /// Mark a row only after JetStream acknowledges it.
    pub async fn mark_published(
        &mut self,
        sequence: i64,
        position: Option<(&str, i64)>,
    ) -> Result<(), sqlx::Error> {
        let mut uow = UnitOfWork::begin_on(&mut self.connection).await?;
        let generation = if let Some((created, _)) = position {
            Some(
                sqlx::query_scalar::<_, i64>(
                    "INSERT INTO events.stream_generations (stream_created) VALUES ($1) \
                     ON CONFLICT (stream_created) DO UPDATE \
                     SET stream_created = EXCLUDED.stream_created RETURNING id",
                )
                .bind(created)
                .fetch_one(uow.connection())
                .await?,
            )
        } else {
            None
        };
        sqlx::query(
            "UPDATE events.outbox SET published_at = now(), retry_at = NULL, last_error = NULL, failure_count = 0, \
             stream_generation = $2, stream_sequence = $3 \
             WHERE sequence = $1 AND published_at IS NULL",
        )
        .bind(sequence)
        .bind(generation)
        .bind(position.map(|(_, stream_sequence)| stream_sequence))
        .execute(uow.connection())
        .await?;
        uow.commit().await?;
        Ok(())
    }

    /// Keep a failed row durable and schedule its next attempt.
    pub async fn record_failure(
        &mut self,
        sequence: i64,
        error: &str,
        delay_seconds: i64,
    ) -> Result<(), sqlx::Error> {
        sqlx::query(
            "UPDATE events.outbox SET last_error = $2, failure_count = failure_count + 1, \
             retry_at = now() + $3 * interval '1 second' \
             WHERE sequence = $1 AND published_at IS NULL",
        )
        .bind(sequence)
        .bind(error)
        .bind(delay_seconds)
        .execute(&mut self.connection)
        .await?;
        Ok(())
    }

    /// Age and last error of the oldest unpublished row, if any.
    pub async fn oldest_unpublished(&mut self) -> Result<Option<UnpublishedStatus>, sqlx::Error> {
        sqlx::query_as(
            "SELECT extract(epoch FROM now() - created_at)::float8 AS age_seconds, last_error \
             FROM events.outbox WHERE published_at IS NULL \
             ORDER BY created_at, sequence LIMIT 1",
        )
        .fetch_optional(&mut self.connection)
        .await
    }

    /// Remove published rows older than the producer's retention period.
    pub async fn purge_published(&mut self, retention_seconds: i64) -> Result<u64, sqlx::Error> {
        let mut uow = UnitOfWork::begin_on(&mut self.connection).await?;
        let newest: Option<(i64, i64)> = sqlx::query_as(
            "SELECT stream_generation, stream_sequence FROM events.outbox \
             WHERE published_at < now() - $1 * interval '1 second' \
             AND stream_generation IS NOT NULL \
             ORDER BY stream_generation DESC, stream_sequence DESC LIMIT 1",
        )
        .bind(retention_seconds)
        .fetch_optional(uow.connection())
        .await?;
        if let Some((generation, stream_sequence)) = newest {
            sqlx::query(
                "UPDATE events.journal_state SET pruned_generation = $1, pruned_sequence = $2 \
                 WHERE id AND (pruned_generation IS NULL OR \
                 (pruned_generation, pruned_sequence) < ($1, $2))",
            )
            .bind(generation)
            .bind(stream_sequence)
            .execute(uow.connection())
            .await?;
        }
        let result = sqlx::query(
            "DELETE FROM events.outbox WHERE published_at < now() - $1 * interval '1 second'",
        )
        .bind(retention_seconds)
        .execute(uow.connection())
        .await?;
        uow.commit().await?;
        Ok(result.rows_affected())
    }
}
