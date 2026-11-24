//! Durable browser action identity and exact-content replay.

use anyhow::{Result, ensure};
use chrono::{DateTime, Utc};
use concurro_db::{Database, UnitOfWork};
use concurro_tasks_core::Request;
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

use crate::record_request;

/// Columns read by `request_record`.
type RequestRecordRow = (String, Option<String>, Option<Uuid>, Option<DateTime<Utc>>);

/// Columns read by `browser_action`.
type BrowserActionRow = (i64, Json<Value>, Uuid, String, Option<String>);

/// The original outcome of a browser action, including one still awaiting decision.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BrowserAction {
    /// Task request created for this action.
    pub request_id: Uuid,
    /// Current durable decision state.
    pub status: String,
    /// Refusal or supersession reason, when known.
    pub reason: Option<String>,
}

/// Current outcome of any request ID, including a pending browser command.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RequestRecord {
    /// Stable task request identity.
    pub request_id: Uuid,
    /// Current disposition or pending state.
    pub status: String,
    /// Refusal or supersession reason.
    pub reason: Option<String>,
    /// Task created or affected by this decision, when known.
    pub task_id: Option<Uuid>,
    /// Time at which the decision was durably recorded.
    pub applied_at: Option<DateTime<Utc>>,
}

/// Read one durable request disposition for the internal requests API.
pub async fn request_record(
    database: &Database,
    request_id: Uuid,
) -> Result<Option<RequestRecord>> {
    let row: Option<RequestRecordRow> =
        sqlx::query_as("SELECT status,reason,task_id,applied_at FROM tasks.requests WHERE id=$1")
            .bind(request_id)
            .fetch_optional(database.pool())
            .await?;
    Ok(
        row.map(|(status, reason, task_id, applied_at)| RequestRecord {
            request_id,
            status,
            reason,
            task_id,
            applied_at,
        }),
    )
}

/// Read the signed retry feedback recorded with a browser action.
pub async fn browser_retry_feedback(
    database: &Database,
    request_id: Uuid,
) -> Result<Option<String>> {
    Ok(sqlx::query_scalar(
        "SELECT content->'detail'->>'feedback' FROM tasks.browser_actions WHERE request_id=$1",
    )
    .bind(request_id)
    .fetch_optional(database.pool())
    .await?
    .flatten())
}

/// Read an existing action without needing the issue to remain available on GitHub.
pub async fn browser_action(
    database: &Database,
    action_id: Uuid,
    actor_id: i64,
    content: &Value,
) -> Result<Option<BrowserAction>> {
    let row: Option<BrowserActionRow> = sqlx::query_as(
        "SELECT b.actor_id,b.content,b.request_id,r.status,r.reason \
         FROM tasks.browser_actions b JOIN tasks.requests r ON r.id=b.request_id \
         WHERE b.action_id=$1",
    )
    .bind(action_id)
    .fetch_optional(database.pool())
    .await?;
    let Some((stored_actor, Json(stored_content), request_id, status, reason)) = row else {
        return Ok(None);
    };
    ensure!(
        stored_actor == actor_id && stored_content == *content,
        "idempotency_conflict"
    );
    Ok(Some(BrowserAction {
        request_id,
        status,
        reason,
    }))
}

/// Record a new browser request and its replay identity in one issue transaction.
pub async fn record_browser_action(
    uow: &mut UnitOfWork<'_>,
    action_id: Uuid,
    content: &Value,
    request: &Request,
) -> Result<BrowserAction> {
    let request_id = record_request(uow, request).await?;
    sqlx::query(
        "INSERT INTO tasks.browser_actions (action_id,actor_id,content,request_id) \
         VALUES ($1,$2,$3,$4) ON CONFLICT (action_id) DO NOTHING",
    )
    .bind(action_id)
    .bind(request.actor.id)
    .bind(Json(content))
    .bind(request_id)
    .execute(uow.connection())
    .await?;
    let row: (i64, Json<Value>, Uuid, String, Option<String>) = sqlx::query_as(
        "SELECT b.actor_id,b.content,b.request_id,r.status,r.reason \
         FROM tasks.browser_actions b JOIN tasks.requests r ON r.id=b.request_id \
         WHERE b.action_id=$1 FOR UPDATE OF b",
    )
    .bind(action_id)
    .fetch_one(uow.connection())
    .await?;
    ensure!(
        row.0 == request.actor.id && row.1.0 == *content,
        "idempotency_conflict"
    );
    Ok(BrowserAction {
        request_id: row.2,
        status: row.3,
        reason: row.4,
    })
}
