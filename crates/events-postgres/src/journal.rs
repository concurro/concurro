//! Publication barriers and retained outbox replay.

use chrono::{DateTime, Utc};
use concurro_db::{Database, UnitOfWork};
use concurro_events::{
    ContinuityDecision, JournalPosition, ReplayAnswer, ReplayRequest, Unrecoverable,
    check_continuity, coverage_is_applied,
};

/// Capture all outbox writes committed before a coverage advance.
pub async fn capture_barrier(
    uow: &mut UnitOfWork<'_>,
    covered_until: DateTime<Utc>,
) -> Result<i64, sqlx::Error> {
    sqlx::query("LOCK TABLE events.outbox IN SHARE ROW EXCLUSIVE MODE")
        .execute(uow.connection())
        .await?;
    let through: i64 = sqlx::query_scalar("SELECT COALESCE(max(sequence), 0) FROM events.outbox")
        .fetch_one(uow.connection())
        .await?;
    sqlx::query(
        "UPDATE events.publication_barrier \
         SET covered_until = $1, through_sequence = $2 WHERE id",
    )
    .bind(covered_until)
    .bind(through)
    .execute(uow.connection())
    .await?;
    Ok(through)
}

/// Take a pending coverage advance only when every captured row is published.
pub async fn take_ready_barrier(
    uow: &mut UnitOfWork<'_>,
) -> Result<Option<DateTime<Utc>>, sqlx::Error> {
    sqlx::query("LOCK TABLE events.outbox IN SHARE ROW EXCLUSIVE MODE")
        .execute(uow.connection())
        .await?;
    let pending: Option<(Option<DateTime<Utc>>, Option<i64>)> = sqlx::query_as(
        "SELECT covered_until, through_sequence FROM events.publication_barrier WHERE id FOR UPDATE",
    )
    .fetch_optional(uow.connection())
    .await?;
    let Some((Some(covered_until), Some(through))) = pending else {
        return Ok(None);
    };
    let unpublished: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM events.outbox \
         WHERE sequence <= $1 AND published_at IS NULL)",
    )
    .bind(through)
    .fetch_one(uow.connection())
    .await?;
    if unpublished {
        return Ok(None);
    }
    sqlx::query(
        "UPDATE events.publication_barrier \
         SET covered_until = NULL, through_sequence = NULL WHERE id",
    )
    .execute(uow.connection())
    .await?;
    Ok(Some(covered_until))
}

/// Re-queue one retained journal batch after a consumer's recorded floor.
pub async fn replay_retained(
    database: &Database,
    consumer: &str,
    request: &ReplayRequest,
    batch_size: i64,
) -> Result<ReplayAnswer, sqlx::Error> {
    let mut uow = database.begin().await?;
    sqlx::query("LOCK TABLE events.outbox IN SHARE ROW EXCLUSIVE MODE")
        .execute(uow.connection())
        .await?;
    let upper_sequence: i64 =
        sqlx::query_scalar("SELECT COALESCE(max(sequence), 0) FROM events.outbox")
            .fetch_one(uow.connection())
            .await?;
    let replay_id: i64 = sqlx::query_scalar(
        "INSERT INTO events.replay_requests \
         (consumer, floor_created, floor_sequence, target_created, unrecoverable, upper_sequence) \
         VALUES ($1, $2, $3, $4, false, $5) \
         ON CONFLICT (consumer, floor_created, floor_sequence, target_created) \
         WHERE completed_at IS NULL \
         DO UPDATE SET consumer = EXCLUDED.consumer RETURNING id",
    )
    .bind(consumer)
    .bind(&request.floor.stream_created)
    .bind(i64::try_from(request.floor.sequence).unwrap_or(i64::MAX))
    .bind(&request.target_created)
    .bind(upper_sequence)
    .fetch_one(uow.connection())
    .await?;
    let generation: Option<i64> =
        sqlx::query_scalar("SELECT id FROM events.stream_generations WHERE stream_created = $1")
            .bind(&request.floor.stream_created)
            .fetch_optional(uow.connection())
            .await?;
    uow.commit().await?;
    loop {
        let mut uow = database.begin().await?;
        sqlx::query("LOCK TABLE events.outbox IN SHARE ROW EXCLUSIVE MODE")
            .execute(uow.connection())
            .await?;
        let (cursor, upper, last_created, last_stream_sequence): (
            i64,
            i64,
            Option<String>,
            Option<i64>,
        ) = sqlx::query_as(
            "SELECT cursor_sequence, upper_sequence, replayed_created, replayed_stream_sequence \
             FROM events.replay_requests WHERE id = $1 FOR UPDATE",
        )
        .bind(replay_id)
        .fetch_one(uow.connection())
        .await?;
        let rows: Vec<(i64, String, i64, i64)> = sqlx::query_as(
            "SELECT o.sequence, g.stream_created, o.stream_generation, o.stream_sequence \
             FROM events.outbox o JOIN events.stream_generations g \
             ON g.id = o.stream_generation \
             WHERE ($1::bigint IS NULL OR o.stream_generation > $1 OR \
             (o.stream_generation = $1 AND o.stream_sequence > $2)) \
             AND o.sequence > $3 AND o.sequence <= $4 \
             ORDER BY o.sequence LIMIT $5",
        )
        .bind(generation)
        .bind(i64::try_from(request.floor.sequence).unwrap_or(i64::MAX))
        .bind(cursor)
        .bind(upper)
        .bind(batch_size.max(1))
        .fetch_all(uow.connection())
        .await?;
        if let Some(last) = rows.last() {
            let sequences: Vec<i64> = rows.iter().map(|row| row.0).collect();
            sqlx::query(
                "UPDATE events.outbox SET published_at = NULL, retry_at = NULL \
                 WHERE sequence = ANY($1) AND published_at IS NOT NULL",
            )
            .bind(&sequences)
            .execute(uow.connection())
            .await?;
            sqlx::query(
                "UPDATE events.replay_requests SET cursor_sequence = $2, replayed_through = $2, \
                 replayed_created = $3, replayed_stream_sequence = $4 WHERE id = $1",
            )
            .bind(replay_id)
            .bind(last.0)
            .bind(&last.1)
            .bind(last.3)
            .execute(uow.connection())
            .await?;
            uow.commit().await?;
            continue;
        }
        let pruned: Option<(Option<i64>, Option<i64>)> = sqlx::query_as(
            "SELECT pruned_generation, pruned_sequence FROM events.journal_state WHERE id",
        )
        .fetch_optional(uow.connection())
        .await?;
        let unrecoverable = match (generation, pruned) {
            (None, Some((Some(_), Some(_)))) => true,
            (Some(floor_generation), Some((Some(pruned_generation), Some(pruned_sequence)))) => {
                (
                    floor_generation,
                    i64::try_from(request.floor.sequence).unwrap_or(i64::MAX),
                ) <= (pruned_generation, pruned_sequence)
            }
            _ => false,
        };
        let before: Option<String> = if unrecoverable {
            let oldest: Option<DateTime<Utc>> = sqlx::query_scalar(
                "SELECT created_at FROM events.outbox WHERE stream_generation IS NOT NULL \
                 ORDER BY stream_generation, stream_sequence LIMIT 1",
            )
            .fetch_optional(uow.connection())
            .await?;
            oldest.map(|created| created.to_rfc3339())
        } else {
            None
        };
        let requested_at: DateTime<Utc> = sqlx::query_scalar("SELECT clock_timestamp()")
            .fetch_one(uow.connection())
            .await?;
        sqlx::query(
            "UPDATE events.replay_requests SET requested_at = $2, completed_at = $2, \
             unrecoverable = $3 WHERE id = $1",
        )
        .bind(replay_id)
        .bind(requested_at)
        .bind(unrecoverable)
        .execute(uow.connection())
        .await?;
        uow.commit().await?;
        return Ok(ReplayAnswer {
            requested_at: requested_at.to_rfc3339(),
            replayed_through: last_created.zip(last_stream_sequence).map(
                |(stream_created, sequence)| JournalPosition {
                    stream_created,
                    sequence: u64::try_from(sequence).expect("NATS sequence is nonnegative"),
                },
            ),
            unrecoverable: unrecoverable.then_some(Unrecoverable { before }),
        });
    }
}

/// Persist a consumer floor or one open stream loss.
pub async fn observe_consumer(
    database: &Database,
    consumer: &str,
    stream_created: &str,
    first_sequence: u64,
    ack_floor: u64,
) -> Result<ContinuityDecision, sqlx::Error> {
    let mut uow = database.begin().await?;
    let prior: Option<(String, i64)> = sqlx::query_as(
        "SELECT stream_created, floor_sequence FROM events.consumer_positions \
         WHERE consumer = $1 FOR UPDATE",
    )
    .bind(consumer)
    .fetch_optional(uow.connection())
    .await?;
    let recorded = prior.map(|(stream_created, sequence)| JournalPosition {
        stream_created,
        sequence: u64::try_from(sequence).expect("stored sequence is nonnegative"),
    });
    let open_loss: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM events.consumer_losses \
         WHERE consumer = $1 AND resolved_at IS NULL)",
    )
    .bind(consumer)
    .fetch_one(uow.connection())
    .await?;
    let decision = if open_loss {
        ContinuityDecision::Lost(recorded.clone().expect("loss has a recorded floor"))
    } else {
        check_continuity(recorded.as_ref(), stream_created, first_sequence, ack_floor)
    };
    match &decision {
        ContinuityDecision::Initialize(position) | ContinuityDecision::Advance(position) => {
            sqlx::query(
                "INSERT INTO events.consumer_positions (consumer, stream_created, floor_sequence) \
                 VALUES ($1, $2, $3) ON CONFLICT (consumer) DO UPDATE SET \
                 stream_created = EXCLUDED.stream_created, \
                 floor_sequence = EXCLUDED.floor_sequence, observed_at = now()",
            )
            .bind(consumer)
            .bind(&position.stream_created)
            .bind(i64::try_from(position.sequence).unwrap_or(i64::MAX))
            .execute(uow.connection())
            .await?;
        }
        ContinuityDecision::Lost(position) => {
            sqlx::query(
                "INSERT INTO events.consumer_losses \
                 (consumer, floor_created, floor_sequence) VALUES ($1, $2, $3) \
                 ON CONFLICT (consumer) WHERE resolved_at IS NULL DO NOTHING",
            )
            .bind(consumer)
            .bind(&position.stream_created)
            .bind(i64::try_from(position.sequence).unwrap_or(i64::MAX))
            .execute(uow.connection())
            .await?;
        }
    }
    uow.commit().await?;
    Ok(decision)
}

/// Resolve a loss after replay and any required reconciliation complete.
pub async fn resolve_consumer_loss(
    database: &Database,
    consumer: &str,
    current: &JournalPosition,
) -> Result<(), sqlx::Error> {
    let mut uow = database.begin().await?;
    sqlx::query(
        "UPDATE events.consumer_losses SET resolved_at = now() \
         WHERE consumer = $1 AND resolved_at IS NULL",
    )
    .bind(consumer)
    .execute(uow.connection())
    .await?;
    sqlx::query(
        "INSERT INTO events.consumer_positions (consumer, stream_created, floor_sequence) \
         VALUES ($1, $2, $3) ON CONFLICT (consumer) DO UPDATE SET \
         stream_created = EXCLUDED.stream_created, \
         floor_sequence = EXCLUDED.floor_sequence, observed_at = now()",
    )
    .bind(consumer)
    .bind(&current.stream_created)
    .bind(i64::try_from(current.sequence).unwrap_or(i64::MAX))
    .execute(uow.connection())
    .await?;
    uow.commit().await
}

/// Check whether the consumer may apply one coverage fact to its read model.
pub async fn consumer_coverage_applied(
    database: &Database,
    consumer: &str,
    coverage: &JournalPosition,
    current: &JournalPosition,
) -> Result<bool, sqlx::Error> {
    let pending: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM events.held WHERE consumer = $1) \
         OR EXISTS (SELECT 1 FROM events.parked WHERE consumer = $1 \
         AND replayed_at IS NULL AND discarded_at IS NULL)",
    )
    .bind(consumer)
    .fetch_one(database.pool())
    .await?;
    let loss: bool = sqlx::query_scalar(
        "SELECT EXISTS (SELECT 1 FROM events.consumer_losses \
         WHERE consumer = $1 AND resolved_at IS NULL)",
    )
    .bind(consumer)
    .fetch_one(database.pool())
    .await?;
    Ok(coverage_is_applied(coverage, current, pending, loss))
}
