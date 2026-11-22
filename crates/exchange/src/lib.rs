//! Exchange operator HTTP surface for Phase 5 turn admission and retirement.

use axum::{
    Json, Router,
    extract::{Path, State, rejection::JsonRejection},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post, put},
};
use concurro_exchange_postgres::{Store, StoreError};
use serde_json::{Value, json};
use uuid::Uuid;

/// Authenticated operator listener state.
#[derive(Clone, Debug)]
pub struct OperatorState {
    /// Durable exchange workflow.
    pub store: Store,
    /// Task application's private bearer.
    pub bearer: String,
}

/// Route the four task-facing operator calls.
pub fn operator_router(state: OperatorState) -> Router {
    Router::new()
        .route(
            "/v1/turns/{turn_id}",
            put(submit).get(observe).delete(cancel),
        )
        .route("/v1/grants/{grant_id}/revoke", post(revoke))
        .route("/healthz", get(|| async { StatusCode::NO_CONTENT }))
        .with_state(state)
}

/// Stable public API refusal with no internal error or bearer content.
#[derive(Debug)]
struct ApiError(StatusCode, &'static str);

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"error": self.1}))).into_response()
    }
}

impl From<StoreError> for ApiError {
    fn from(error: StoreError) -> Self {
        match error {
            StoreError::Refused { status, code } => {
                Self(StatusCode::from_u16(status).expect("valid refusal"), code)
            }
            _ => {
                tracing::error!(%error, "exchange operator transaction failed");
                Self(StatusCode::SERVICE_UNAVAILABLE, "exchange_unavailable")
            }
        }
    }
}

/// Require the operator bearer before consulting any state.
fn authorize(state: &OperatorState, headers: &HeaderMap) -> Result<(), ApiError> {
    let bearer = headers
        .get(axum::http::header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok());
    if bearer == Some(format!("Bearer {}", state.bearer).as_str()) {
        Ok(())
    } else {
        Err(ApiError(StatusCode::UNAUTHORIZED, "unauthorized"))
    }
}

/// Submit an immutable turn under its attempt ID.
async fn submit(
    State(state): State<OperatorState>,
    Path(turn): Path<Uuid>,
    headers: HeaderMap,
    body: Result<Json<Value>, JsonRejection>,
) -> Result<Json<concurro_contracts::TurnObservation>, ApiError> {
    authorize(&state, &headers)?;
    let Json(body) =
        body.map_err(|_| ApiError(StatusCode::UNPROCESSABLE_ENTITY, "invalid_body"))?;
    Ok(Json(state.store.submit(turn, &body).await?))
}

/// Read a current turn observation for reconciliation.
async fn observe(
    State(state): State<OperatorState>,
    Path(turn): Path<Uuid>,
    headers: HeaderMap,
) -> Result<Json<concurro_contracts::TurnObservation>, ApiError> {
    authorize(&state, &headers)?;
    Ok(Json(state.store.observation(turn).await?))
}

/// Cancel or retire a turn and return its full session set.
async fn cancel(
    State(state): State<OperatorState>,
    Path(turn): Path<Uuid>,
    headers: HeaderMap,
) -> Result<Json<concurro_contracts::RetiredSessions>, ApiError> {
    authorize(&state, &headers)?;
    Ok(Json(state.store.cancel(turn).await?))
}

/// Revoke a grant, including one no turn has used yet.
async fn revoke(
    State(state): State<OperatorState>,
    Path(grant): Path<String>,
    headers: HeaderMap,
) -> Result<Json<concurro_contracts::RetiredSessions>, ApiError> {
    authorize(&state, &headers)?;
    Ok(Json(state.store.revoke(&grant).await?))
}
