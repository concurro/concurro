//! Typed operations behind parked and held event commands.

use anyhow::{Context, Result, bail};
use chrono::{DateTime, Utc};
use concurro_db::{Database, UnitOfWork};
use serde::Serialize;
use sqlx::types::Json;
use uuid::Uuid;

use crate::consumer::lock;

/// Execute a parked or held command for a service's event consumer.
pub async fn run_admin(database: &Database, consumer: &str, args: &[String]) -> Result<String> {
    let value = match args {
        [section, action] if section == "parked" && action == "list" => {
            serde_json::to_value(list_parked(database, consumer).await?)?
        }
        [section, action, id] if section == "parked" && action == "show" => {
            let id = id.parse::<i64>().context("parking ID must be an integer")?;
            let detail = show_parked(database, id)
                .await?
                .context("parking not found")?;
            if detail.consumer != consumer {
                bail!("parking belongs to a different consumer");
            }
            let mut value = serde_json::to_value(&detail)?;
            value["wire_message"] =
                serde_json::from_slice(&detail.wire_bytes).unwrap_or_else(|_| {
                    serde_json::Value::String(
                        String::from_utf8_lossy(&detail.wire_bytes).into_owned(),
                    )
                });
            value
        }
        [section, action, id, operator] if section == "parked" && action == "replay" => {
            let id = id.parse::<i64>().context("parking ID must be an integer")?;
            let detail = show_parked(database, id)
                .await?
                .context("parking not found")?;
            if detail.consumer != consumer {
                bail!("parking belongs to a different consumer");
            }
            serde_json::to_value(replay_parked(database, id, operator).await?)?
        }
        [section, action, id, operator, reason] if section == "parked" && action == "discard" => {
            let id = id.parse::<i64>().context("parking ID must be an integer")?;
            let detail = show_parked(database, id)
                .await?
                .context("parking not found")?;
            if detail.consumer != consumer {
                bail!("parking belongs to a different consumer");
            }
            serde_json::to_value(discard_parked(database, id, operator, reason).await?)?
        }
        [section, action] if section == "held" && action == "list" => {
            serde_json::to_value(list_held(database, consumer).await?)?
        }
        [section, action, id] if section == "held" && action == "show" => {
            let id = Uuid::parse_str(id).context("event ID must be a UUID")?;
            let detail = show_held(database, consumer, id)
                .await?
                .context("held event not found")?;
            let mut value = serde_json::to_value(&detail)?;
            value["wire_message"] =
                serde_json::from_slice(&detail.wire_bytes).unwrap_or_else(|_| {
                    serde_json::Value::String(
                        String::from_utf8_lossy(&detail.wire_bytes).into_owned(),
                    )
                });
            value
        }
        _ => bail!(
            "usage: parked list|show ID|replay ID OPERATOR|discard ID OPERATOR REASON; held list|show EVENT_ID"
        ),
    };
    Ok(serde_json::to_string_pretty(&value)?)
}

/// One open parking shown in an operator list.
#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct ParkedSummary {
    /// Monotonic parking history row ID.
    pub id: i64,
    /// Event identity.
    pub event_id: Uuid,
    /// Reason this delivery was parked.
    pub reason: String,
    /// Time the parking committed.
    pub parked_at: DateTime<Utc>,
}

/// One historical parking with its original message.
#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct ParkedDetail {
    /// Monotonic parking history row ID.
    pub id: i64,
    /// Consumer owning this parking.
    pub consumer: String,
    /// Event identity.
    pub event_id: Uuid,
    /// Original message bytes.
    #[serde(skip_serializing)]
    pub wire_bytes: Vec<u8>,
    /// Reason this delivery was parked.
    pub reason: String,
    /// Time the parking committed.
    pub parked_at: DateTime<Utc>,
    /// Operator who replayed it, if any.
    pub replayed_by: Option<String>,
    /// Time the replay was requested, if any.
    pub replayed_at: Option<DateTime<Utc>>,
    /// Operator who discarded it, if any.
    pub discarded_by: Option<String>,
    /// Operator's discard reason, if any.
    pub discarded_reason: Option<String>,
    /// Time the discard was recorded, if any.
    pub discarded_at: Option<DateTime<Utc>>,
}

/// One event waiting for or released from a prerequisite.
#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct HeldSummary {
    /// Event identity.
    pub event_id: Uuid,
    /// Prerequisite key.
    pub key: String,
    /// Whether the prerequisite was released.
    pub released: bool,
    /// Retry time for a failed released re-run.
    pub retry_at: Option<DateTime<Utc>>,
}

/// One held event with its original message bytes.
#[derive(Debug, Serialize, sqlx::FromRow)]
pub struct HeldDetail {
    /// Consumer owning the held event.
    pub consumer: String,
    /// Event identity.
    pub event_id: Uuid,
    /// Prerequisite key.
    pub key: String,
    /// Original message bytes.
    #[serde(skip_serializing)]
    pub wire_bytes: Vec<u8>,
    /// Whether the prerequisite was released.
    pub released: bool,
    /// Retry time for a failed released re-run.
    pub retry_at: Option<DateTime<Utc>>,
}

/// List open parked events owned by one consumer.
pub async fn list_parked(
    database: &Database,
    consumer: &str,
) -> Result<Vec<ParkedSummary>, sqlx::Error> {
    sqlx::query_as(
        "SELECT id, event_id, reason, parked_at FROM events.parked \
         WHERE consumer = $1 AND replayed_at IS NULL AND discarded_at IS NULL \
         ORDER BY id",
    )
    .bind(consumer)
    .fetch_all(database.pool())
    .await
}

/// Show one parking, including replayed or discarded history.
pub async fn show_parked(
    database: &Database,
    id: i64,
) -> Result<Option<ParkedDetail>, sqlx::Error> {
    sqlx::query_as(
        "SELECT id, consumer, event_id, wire_bytes, reason, parked_at, replayed_by, replayed_at, \
         discarded_by, discarded_reason, discarded_at \
         FROM events.parked WHERE id = $1",
    )
    .bind(id)
    .fetch_optional(database.pool())
    .await
}

/// List held events, including released events awaiting their next run.
pub async fn list_held(
    database: &Database,
    consumer: &str,
) -> Result<Vec<HeldSummary>, sqlx::Error> {
    sqlx::query_as(
        "SELECT event_id, key, released, retry_at FROM events.held \
         WHERE consumer = $1 ORDER BY event_id",
    )
    .bind(consumer)
    .fetch_all(database.pool())
    .await
}

/// Show one held event owned by a consumer.
pub async fn show_held(
    database: &Database,
    consumer: &str,
    event_id: Uuid,
) -> Result<Option<HeldDetail>, sqlx::Error> {
    sqlx::query_as(
        "SELECT consumer, event_id, key, wire_bytes, released, retry_at FROM events.held \
         WHERE consumer = $1 AND event_id = $2",
    )
    .bind(consumer)
    .bind(event_id)
    .fetch_optional(database.pool())
    .await
}

/// Move one open parking into the released queue with a fresh attempt budget.
pub async fn replay_parked(database: &Database, id: i64, operator: &str) -> anyhow::Result<bool> {
    let identity: Option<(String, Uuid)> =
        sqlx::query_as("SELECT consumer, event_id FROM events.parked WHERE id = $1")
            .bind(id)
            .fetch_optional(database.pool())
            .await?;
    let Some((consumer, event_id)) = identity else {
        return Ok(false);
    };
    let mut connection = database.pool().acquire().await?.detach();
    lock(&mut connection, &format!("event:{consumer}:{event_id}")).await?;
    let mut uow = UnitOfWork::begin_on(&mut connection).await?;
    let row: Option<(Option<Json<serde_json::Value>>, Vec<u8>, bool)> = sqlx::query_as(
        "SELECT message, wire_bytes, replayed_at IS NULL AND discarded_at IS NULL \
         FROM events.parked WHERE id = $1 FOR UPDATE",
    )
    .bind(id)
    .fetch_optional(uow.connection())
    .await?;
    let Some((message, wire_bytes, true)) = row else {
        uow.rollback().await?;
        return Ok(false);
    };
    let removed = sqlx::query(
        "DELETE FROM events.consumed \
         WHERE consumer = $1 AND event_id = $2 AND disposition = 'parked'",
    )
    .bind(&consumer)
    .bind(event_id)
    .execute(uow.connection())
    .await?;
    if removed.rows_affected() == 0 {
        uow.rollback().await?;
        return Ok(false);
    }
    sqlx::query("DELETE FROM events.attempts WHERE consumer = $1 AND event_id = $2")
        .bind(&consumer)
        .bind(event_id)
        .execute(uow.connection())
        .await?;
    sqlx::query(
        "INSERT INTO events.held (consumer, event_id, key, message, wire_bytes, released) \
         VALUES ($1, $2, 'operator-replay', $3, $4, true)",
    )
    .bind(&consumer)
    .bind(event_id)
    .bind(message)
    .bind(wire_bytes)
    .execute(uow.connection())
    .await?;
    sqlx::query("UPDATE events.parked SET replayed_by = $2, replayed_at = now() WHERE id = $1")
        .bind(id)
        .bind(operator)
        .execute(uow.connection())
        .await?;
    uow.commit().await?;
    Ok(true)
}

/// Record an operator's decision to never run one open parking.
pub async fn discard_parked(
    database: &Database,
    id: i64,
    operator: &str,
    reason: &str,
) -> anyhow::Result<bool> {
    let identity: Option<(String, Uuid)> =
        sqlx::query_as("SELECT consumer, event_id FROM events.parked WHERE id = $1")
            .bind(id)
            .fetch_optional(database.pool())
            .await?;
    let Some((consumer, event_id)) = identity else {
        return Ok(false);
    };
    let mut connection = database.pool().acquire().await?.detach();
    lock(&mut connection, &format!("event:{consumer}:{event_id}")).await?;
    let mut uow = UnitOfWork::begin_on(&mut connection).await?;
    let changed = sqlx::query(
        "UPDATE events.consumed SET disposition = 'discarded' \
         WHERE consumer = $1 AND event_id = $2 AND disposition = 'parked'",
    )
    .bind(&consumer)
    .bind(event_id)
    .execute(uow.connection())
    .await?;
    if changed.rows_affected() == 0 {
        uow.rollback().await?;
        return Ok(false);
    }
    let parking = sqlx::query(
        "UPDATE events.parked SET discarded_by = $2, discarded_reason = $3, discarded_at = now() \
         WHERE id = $1 AND replayed_at IS NULL AND discarded_at IS NULL",
    )
    .bind(id)
    .bind(operator)
    .bind(reason)
    .execute(uow.connection())
    .await?;
    if parking.rows_affected() == 0 {
        uow.rollback().await?;
        return Ok(false);
    }
    uow.commit().await?;
    Ok(true)
}
