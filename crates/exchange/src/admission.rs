//! Private exchange admission and request accounting listener.

use axum::{
    Json, Router,
    extract::{Path, State, rejection::JsonRejection},
    http::{HeaderMap, StatusCode, header},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use concurro_contracts::{AdmissionRequest, FinishRequest, SessionStatusRequest};
use concurro_exchange_postgres::Store;
use uuid::Uuid;

use crate::ApiError;

/// Exchange admission authority available only to the gateway.
#[derive(Clone, Debug)]
pub struct AdmissionState {
    /// Durable exchange state.
    pub store: Store,
    /// Private gateway credential.
    pub bearer: String,
}

/// Bind the three private admission operations and health endpoint.
pub fn admission_router(state: AdmissionState) -> Router {
    Router::new()
        .route("/internal/v1/requests", post(admit))
        .route("/internal/v1/requests/{request}/finish", post(finish))
        .route("/internal/v1/sessions/status", post(sessions_status))
        .route("/healthz", get(|| async { StatusCode::NO_CONTENT }))
        .with_state(state)
}

/// Require the gateway's own bearer before parsing or consulting state.
fn authorize(state: &AdmissionState, headers: &HeaderMap) -> Result<(), ApiError> {
    let supplied = headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok());
    if supplied == Some(format!("Bearer {}", state.bearer).as_str()) {
        Ok(())
    } else {
        Err(ApiError(StatusCode::UNAUTHORIZED, "unauthorized"))
    }
}

/// Freeze an admitted provider request, or answer a model probe without recording one.
async fn admit(
    State(state): State<AdmissionState>,
    headers: HeaderMap,
    body: Result<Json<AdmissionRequest>, JsonRejection>,
) -> Result<Response, ApiError> {
    authorize(&state, &headers)?;
    let Json(body) =
        body.map_err(|_| ApiError(StatusCode::UNPROCESSABLE_ENTITY, "invalid_body"))?;
    match state.store.admit(&body).await? {
        Ok(admission) => Ok(Json(admission).into_response()),
        Err(probe) => Ok(Json(probe).into_response()),
    }
}

/// Persist the first finish report and ignore later repeats.
async fn finish(
    State(state): State<AdmissionState>,
    Path(request): Path<Uuid>,
    headers: HeaderMap,
    body: Result<Json<FinishRequest>, JsonRejection>,
) -> Result<StatusCode, ApiError> {
    authorize(&state, &headers)?;
    let Json(body) =
        body.map_err(|_| ApiError(StatusCode::UNPROCESSABLE_ENTITY, "invalid_body"))?;
    state.store.finish_request(request, &body).await?;
    Ok(StatusCode::NO_CONTENT)
}

/// Return only sessions that can still own open provider streams.
async fn sessions_status(
    State(state): State<AdmissionState>,
    headers: HeaderMap,
    body: Result<Json<SessionStatusRequest>, JsonRejection>,
) -> Result<Json<concurro_contracts::SessionStatusResponse>, ApiError> {
    authorize(&state, &headers)?;
    let Json(body) =
        body.map_err(|_| ApiError(StatusCode::UNPROCESSABLE_ENTITY, "invalid_body"))?;
    Ok(Json(state.store.sessions_status(&body.session_ids).await?))
}
