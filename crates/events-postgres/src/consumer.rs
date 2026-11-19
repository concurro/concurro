//! Durable consumer decisions and event-state transitions.

use std::time::Duration;

use anyhow::Result;
use concurro_contracts::{Contract, ContractError, Envelope, decode, peek_event_id};
use concurro_db::{Database, UnitOfWork};
use concurro_events::{Handler, Outcome, ProcessResult};
use serde::de::DeserializeOwned;
use sha2::{Digest, Sha256};
use sqlx::{PgConnection, types::Json};
use uuid::Uuid;

/// Origin of a processing attempt.
#[derive(Clone, Copy)]
enum Source {
    /// A JetStream delivery.
    Delivery,
    /// A released row from the held table.
    Released(Uuid),
}

/// Process a JetStream delivery with a stable fallback key for malformed envelopes.
pub async fn process_delivery<T, H>(
    database: &Database,
    consumer: &str,
    max_attempts: i32,
    raw: &[u8],
    fallback_key: &str,
    handler: &H,
) -> Result<ProcessResult>
where
    T: Contract + DeserializeOwned + Clone + Send,
    H: Handler<T>,
{
    process(
        database,
        consumer,
        max_attempts,
        raw,
        fallback_key,
        handler,
        Source::Delivery,
    )
    .await
}

/// Re-run one released held event whose backoff has expired.
pub async fn process_released<T, H>(
    database: &Database,
    consumer: &str,
    max_attempts: i32,
    handler: &H,
) -> Result<Option<ProcessResult>>
where
    T: Contract + DeserializeOwned + Clone + Send,
    H: Handler<T>,
{
    let row: Option<(Uuid, Vec<u8>)> = sqlx::query_as(
        "SELECT event_id, wire_bytes FROM events.held \
         WHERE consumer = $1 AND released AND (retry_at IS NULL OR retry_at <= now()) \
         ORDER BY event_id LIMIT 1",
    )
    .bind(consumer)
    .fetch_optional(database.pool())
    .await?;
    let Some((event_id, raw)) = row else {
        return Ok(None);
    };
    let result = process(
        database,
        consumer,
        max_attempts,
        &raw,
        &event_id.to_string(),
        handler,
        Source::Released(event_id),
    )
    .await?;
    Ok(Some(result))
}

/// Serialize one event's attempt and handler transactions on one session.
async fn process<T, H>(
    database: &Database,
    consumer: &str,
    max_attempts: i32,
    raw: &[u8],
    fallback_key: &str,
    handler: &H,
    source: Source,
) -> Result<ProcessResult>
where
    T: Contract + DeserializeOwned + Clone + Send,
    H: Handler<T>,
{
    let (event, unsupported_version) = match decode::<T>(raw) {
        Ok(event) => (Some(event), None),
        Err(ContractError::UnsupportedVersion { found, .. }) => (None, Some(found)),
        Err(_) => (None, None),
    };
    let mut connection = database.pool().acquire().await?.detach();
    let (event_id, known_alias) = match source {
        Source::Released(id) => (id, true),
        Source::Delivery => {
            let alias = if fallback_key.len() > 512
                || fallback_key.contains('\0')
                || fallback_key.starts_with("sha256:")
            {
                format!(
                    "sha256:{}",
                    hex::encode(Sha256::digest(fallback_key.as_bytes()))
                )
            } else {
                fallback_key.to_owned()
            };
            let candidate = peek_event_id(raw).map_or_else(
                || {
                    Uuid::new_v5(
                        &Uuid::NAMESPACE_OID,
                        format!("{consumer}:raw:{alias}").as_bytes(),
                    )
                },
                |id| id.0,
            );
            if unsupported_version.is_some() {
                let existing: Option<Uuid> = sqlx::query_scalar(
                    "SELECT event_id FROM events.identity_aliases \
                     WHERE consumer = $1 AND fallback_key = $2",
                )
                .bind(consumer)
                .bind(alias)
                .fetch_optional(&mut connection)
                .await?;
                (existing.unwrap_or(candidate), existing.is_some())
            } else {
                let id = sqlx::query_scalar(
                    "INSERT INTO events.identity_aliases (consumer, fallback_key, event_id) \
                     VALUES ($1, $2, $3) ON CONFLICT (consumer, fallback_key) \
                     DO UPDATE SET event_id = events.identity_aliases.event_id RETURNING event_id",
                )
                .bind(consumer)
                .bind(alias)
                .bind(candidate)
                .fetch_one(&mut connection)
                .await?;
                (id, true)
            }
        }
    };
    lock(&mut connection, &format!("event:{consumer}:{event_id}")).await?;
    let mut attempt = UnitOfWork::begin_on(&mut connection).await?;
    let consumed: Option<String> = sqlx::query_scalar(
        "SELECT disposition FROM events.consumed WHERE consumer = $1 AND event_id = $2",
    )
    .bind(consumer)
    .bind(event_id)
    .fetch_optional(attempt.connection())
    .await?;
    if consumed.is_some() {
        if matches!(source, Source::Released(_)) {
            sqlx::query("DELETE FROM events.held WHERE consumer = $1 AND event_id = $2")
                .bind(consumer)
                .bind(event_id)
                .execute(attempt.connection())
                .await?;
            attempt.commit().await?;
        } else {
            attempt.rollback().await?;
        }
        return Ok(ProcessResult::Skipped);
    }
    let held: Option<(bool, bool, bool)> = sqlx::query_as(
        "SELECT released, retry_at IS NULL OR retry_at <= now(), wire_bytes = $3 \
         FROM events.held WHERE consumer = $1 AND event_id = $2",
    )
    .bind(consumer)
    .bind(event_id)
    .bind(raw)
    .fetch_optional(attempt.connection())
    .await?;
    if let Some(version) = unsupported_version {
        let owned = match source {
            Source::Released(_) => held == Some((true, true, true)),
            Source::Delivery if held.is_none() && known_alias => {
                sqlx::query_scalar(
                    "SELECT EXISTS (SELECT 1 FROM events.attempts \
                     WHERE consumer = $1 AND event_id = $2)",
                )
                .bind(consumer)
                .bind(event_id)
                .fetch_one(attempt.connection())
                .await?
            }
            Source::Delivery => false,
        };
        attempt.rollback().await?;
        if owned {
            park(
                &mut connection,
                consumer,
                event_id,
                raw,
                &format!("unsupported contract version {version}"),
            )
            .await?;
            return Ok(ProcessResult::Parked);
        }
        return Ok(ProcessResult::Skipped);
    }
    let should_skip = match source {
        Source::Delivery => held.is_some(),
        Source::Released(_) => !held.is_some_and(|(released, due, _)| released && due),
    };
    if should_skip {
        attempt.rollback().await?;
        return Ok(ProcessResult::Skipped);
    }
    let count: i32 = sqlx::query_scalar(
        "INSERT INTO events.attempts (consumer, event_id, count) VALUES ($1, $2, 1) \
         ON CONFLICT (consumer, event_id) DO UPDATE SET count = events.attempts.count + 1 \
         RETURNING count",
    )
    .bind(consumer)
    .bind(event_id)
    .fetch_one(attempt.connection())
    .await?;
    attempt.commit().await?;
    let Some(event) = event else {
        if count > max_attempts {
            park(
                &mut connection,
                consumer,
                event_id,
                raw,
                "invalid event envelope",
            )
            .await?;
            return Ok(ProcessResult::Parked);
        }
        return retry(
            &mut connection,
            consumer,
            event_id,
            count,
            "invalid event envelope",
            source,
        )
        .await;
    };
    if count > max_attempts {
        park(
            &mut connection,
            consumer,
            event_id,
            raw,
            "attempt limit reached",
        )
        .await?;
        return Ok(ProcessResult::Parked);
    }
    let mut applying = UnitOfWork::begin_on(&mut connection).await?;
    sqlx::query(
        "INSERT INTO events.consumed (consumer, event_id, disposition) VALUES ($1, $2, 'applied')",
    )
    .bind(consumer)
    .bind(event_id)
    .execute(applying.connection())
    .await?;
    let outcome = handler.handle(&mut applying, event.clone()).await;
    match outcome {
        Outcome::Done => {
            if let Err(error) = sqlx::query("SELECT 1").execute(applying.connection()).await {
                applying.rollback().await?;
                return retry(
                    &mut connection,
                    consumer,
                    event_id,
                    count,
                    &error.to_string(),
                    source,
                )
                .await;
            }
            if matches!(source, Source::Released(_)) {
                sqlx::query("DELETE FROM events.held WHERE consumer = $1 AND event_id = $2")
                    .bind(consumer)
                    .bind(event_id)
                    .execute(applying.connection())
                    .await?;
            }
            applying.commit().await?;
            Ok(ProcessResult::Done)
        }
        Outcome::Retry(error) => {
            applying.rollback().await?;
            retry(&mut connection, consumer, event_id, count, &error, source).await
        }
        Outcome::Reject(reason) => {
            applying.rollback().await?;
            park(&mut connection, consumer, event_id, raw, &reason).await?;
            Ok(ProcessResult::Parked)
        }
        Outcome::Hold(key) => {
            applying.rollback().await?;
            let context = HoldContext {
                consumer,
                event_id,
                raw,
                count,
                source,
            };
            confirm_hold(&mut connection, &context, event, key, handler).await
        }
    }
}

/// Mark held rows ready in the same transaction that satisfies their prerequisite.
pub async fn release_held(uow: &mut UnitOfWork<'_>, consumer: &str, key: &str) -> Result<u64> {
    let lock_key = format!("key:{consumer}:{key}");
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(lock_key)
        .execute(uow.connection())
        .await?;
    let changed = sqlx::query(
        "UPDATE events.held SET released = true, retry_at = NULL \
         WHERE consumer = $1 AND key = $2 AND NOT released",
    )
    .bind(consumer)
    .bind(key)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected())
}

/// State carried across a hold's confirmation transactions.
#[derive(Clone, Copy)]
struct HoldContext<'a> {
    /// Consumer owning the event.
    consumer: &'a str,
    /// Stable event identity.
    event_id: Uuid,
    /// Original event bytes.
    raw: &'a [u8],
    /// Durable attempt count.
    count: i32,
    /// Delivery or released-row origin.
    source: Source,
}

/// Hold the key lock while re-running the handler to close the release race.
async fn confirm_hold<T, H>(
    connection: &mut PgConnection,
    context: &HoldContext<'_>,
    event: Envelope<T>,
    mut key: String,
    handler: &H,
) -> Result<ProcessResult>
where
    T: Contract + Clone + Send,
    H: Handler<T>,
{
    let HoldContext {
        consumer,
        event_id,
        raw,
        count,
        source,
    } = *context;
    loop {
        let lock_key = format!("key:{consumer}:{key}");
        lock(connection, &lock_key).await?;
        let mut confirming = UnitOfWork::begin_on(&mut *connection).await?;
        sqlx::query(
            "INSERT INTO events.consumed (consumer, event_id, disposition) VALUES ($1, $2, 'applied')",
        )
        .bind(consumer)
        .bind(event_id)
        .execute(confirming.connection())
        .await?;
        let outcome = handler.handle(&mut confirming, event.clone()).await;
        match outcome {
            Outcome::Done => {
                if let Err(error) = sqlx::query("SELECT 1")
                    .execute(confirming.connection())
                    .await
                {
                    confirming.rollback().await?;
                    return retry(
                        connection,
                        consumer,
                        event_id,
                        count,
                        &error.to_string(),
                        source,
                    )
                    .await;
                }
                if matches!(source, Source::Released(_)) {
                    sqlx::query("DELETE FROM events.held WHERE consumer = $1 AND event_id = $2")
                        .bind(consumer)
                        .bind(event_id)
                        .execute(confirming.connection())
                        .await?;
                }
                confirming.commit().await?;
                return Ok(ProcessResult::Done);
            }
            Outcome::Retry(error) => {
                confirming.rollback().await?;
                return retry(connection, consumer, event_id, count, &error, source).await;
            }
            Outcome::Reject(reason) => {
                confirming.rollback().await?;
                park(connection, consumer, event_id, raw, &reason).await?;
                return Ok(ProcessResult::Parked);
            }
            Outcome::Hold(next_key) => {
                confirming.rollback().await?;
                if next_key == key {
                    let mut holding = UnitOfWork::begin_on(&mut *connection).await?;
                    sqlx::query(
                        "INSERT INTO events.held (consumer, event_id, key, message, wire_bytes) \
                         VALUES ($1, $2, $3, $4, $5) \
                         ON CONFLICT (consumer, event_id) DO UPDATE SET \
                           key = EXCLUDED.key, message = EXCLUDED.message, \
                           wire_bytes = EXCLUDED.wire_bytes, released = false, retry_at = NULL",
                    )
                    .bind(consumer)
                    .bind(event_id)
                    .bind(&key)
                    .bind(optional_jsonb(raw))
                    .bind(raw)
                    .execute(holding.connection())
                    .await?;
                    sqlx::query(
                        "DELETE FROM events.attempts WHERE consumer = $1 AND event_id = $2",
                    )
                    .bind(consumer)
                    .bind(event_id)
                    .execute(holding.connection())
                    .await?;
                    holding.commit().await?;
                    return Ok(ProcessResult::Held);
                }
                unlock(connection, &lock_key).await?;
                key = next_key;
            }
        }
    }
}

/// Park an event and its original bytes under the already held event lock.
async fn park(
    connection: &mut PgConnection,
    consumer: &str,
    event_id: Uuid,
    raw: &[u8],
    reason: &str,
) -> Result<()> {
    let mut uow = UnitOfWork::begin_on(connection).await?;
    sqlx::query(
        "INSERT INTO events.consumed (consumer, event_id, disposition) VALUES ($1, $2, 'parked')",
    )
    .bind(consumer)
    .bind(event_id)
    .execute(uow.connection())
    .await?;
    sqlx::query("DELETE FROM events.held WHERE consumer = $1 AND event_id = $2")
        .bind(consumer)
        .bind(event_id)
        .execute(uow.connection())
        .await?;
    sqlx::query(
        "INSERT INTO events.parked (consumer, event_id, message, wire_bytes, reason) \
         VALUES ($1, $2, $3, $4, $5)",
    )
    .bind(consumer)
    .bind(event_id)
    .bind(optional_jsonb(raw))
    .bind(raw)
    .bind(reason)
    .execute(uow.connection())
    .await?;
    uow.commit().await?;
    Ok(())
}

/// Keep parsed JSON optional when PostgreSQL jsonb cannot store its raw escapes.
fn optional_jsonb(raw: &[u8]) -> Option<Json<serde_json::Value>> {
    if raw.windows(6).any(|window| window == br"\u0000") {
        return None;
    }
    serde_json::from_slice(raw).ok().map(Json)
}

/// Persist the error after rolling back the handler's effects.
async fn retry(
    connection: &mut PgConnection,
    consumer: &str,
    event_id: Uuid,
    count: i32,
    error: &str,
    source: Source,
) -> Result<ProcessResult> {
    let delay = 1_i64 << count.clamp(0, 10);
    sqlx::query(
        "UPDATE events.attempts SET last_error = $3, retry_at = now() + $4 * interval '1 second' \
         WHERE consumer = $1 AND event_id = $2",
    )
    .bind(consumer)
    .bind(event_id)
    .bind(error)
    .bind(delay)
    .execute(&mut *connection)
    .await?;
    if matches!(source, Source::Released(_)) {
        sqlx::query(
            "UPDATE events.held SET retry_at = now() + $3 * interval '1 second' \
             WHERE consumer = $1 AND event_id = $2",
        )
        .bind(consumer)
        .bind(event_id)
        .bind(delay)
        .execute(connection)
        .await?;
    }
    Ok(ProcessResult::Retry(Duration::from_secs(delay as u64)))
}

/// Acquire a session advisory lock that survives the attempt transaction.
pub(crate) async fn lock(connection: &mut PgConnection, key: &str) -> Result<()> {
    sqlx::query("SELECT pg_advisory_lock(hashtextextended($1, 0))")
        .bind(key)
        .execute(connection)
        .await?;
    Ok(())
}

/// Release an obsolete prerequisite lock before confirming a different key.
async fn unlock(connection: &mut PgConnection, key: &str) -> Result<()> {
    let released: bool = sqlx::query_scalar("SELECT pg_advisory_unlock(hashtextextended($1, 0))")
        .bind(key)
        .fetch_one(connection)
        .await?;
    anyhow::ensure!(released, "prerequisite lock was not held");
    Ok(())
}
