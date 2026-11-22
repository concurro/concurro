//! Platform-only viewer lookup over the memory session and current GitHub state.

use std::time::{Duration, Instant};

use axum::{
    Json, Router,
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode, header},
    response::{IntoResponse, Response},
    routing::get,
};
use chrono::Utc;
use serde::Deserialize;
use serde_json::json;

use crate::{AccountState, credentials::CredentialError, github::AccessError};

/// Issue-specific target requested by the read API.
#[derive(Deserialize)]
struct ViewerQuery {
    /// Configured upstream identity.
    upstream: String,
    /// Source repository for the issue.
    issue_repository: String,
    /// Source issue number, or zero for a writer-only target check.
    issue_number: i64,
}

/// Viewer authority error with a stable detail string.
struct ApiError(StatusCode, &'static str);

impl IntoResponse for ApiError {
    /// Encode an authority error.
    fn into_response(self) -> Response {
        (self.0, Json(json!({"detail":self.1}))).into_response()
    }
}

/// The optional as-of instant of a credential metadata read.
#[derive(Deserialize)]
struct MetadataQuery {
    /// Read as of this instant instead of now.
    at: Option<String>,
}

/// Build the platform listener's viewer lookup and credential metadata reads.
pub fn router(state: AccountState) -> Router {
    Router::new()
        .route("/internal/v1/viewer", get(viewer))
        .route("/v1/credentials/{user_id}", get(credential_metadata))
        .with_state(state)
}

/// Serve one owner's nonsecret credential metadata to the task application.
async fn credential_metadata(
    State(state): State<AccountState>,
    headers: HeaderMap,
    Path(user_id): Path<i64>,
    Query(query): Query<MetadataQuery>,
) -> Result<Json<serde_json::Value>, CredentialError> {
    let Some((native, bearer)) = &state.credential_metadata else {
        return Err(CredentialError::new(StatusCode::NOT_FOUND, "not found"));
    };
    let supplied = headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok());
    if supplied != Some(format!("Bearer {bearer}").as_str()) {
        return Err(CredentialError::new(
            StatusCode::UNAUTHORIZED,
            "service credential required",
        ));
    }
    Ok(Json(native.metadata(user_id, query.at.as_deref()).await?))
}

/// Resolve the current session and issue-specific GitHub tier.
async fn viewer(
    State(state): State<AccountState>,
    headers: HeaderMap,
    Query(query): Query<ViewerQuery>,
) -> Result<Json<serde_json::Value>, ApiError> {
    let bearer = headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok());
    if bearer != Some(format!("Bearer {}", state.authority_bearer).as_str()) {
        return Err(ApiError(
            StatusCode::UNAUTHORIZED,
            "service credential required",
        ));
    }
    let token = headers
        .get("x-concurro-viewer")
        .and_then(|value| value.to_str().ok());
    let session = state
        .sessions
        .viewer(token)
        .ok_or(ApiError(StatusCode::UNAUTHORIZED, "viewer token expired"))?;
    let target = state
        .targets
        .find(&query.upstream)
        .await
        .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "targets unavailable"))?
        .ok_or(ApiError(StatusCode::NOT_FOUND, "target not found"))?;
    if ![&target.upstream, &target.issue_repository, &target.fork]
        .iter()
        .any(|repository| repository.eq_ignore_ascii_case(&query.issue_repository))
    {
        return Err(ApiError(StatusCode::NOT_FOUND, "target not found"));
    }
    let key = (
        session.id.clone(),
        query.upstream.to_ascii_lowercase(),
        query.issue_repository.to_ascii_lowercase(),
        query.issue_number,
    );
    let cached = state
        .tier_cache
        .lock()
        .await
        .get(&key)
        .copied()
        .filter(|(checked, _)| checked.elapsed() < Duration::from_secs(60));
    let tier = if let Some((_, tier)) = cached {
        tier
    } else {
        match state
            .github
            .tier(
                &session.token,
                &session.user,
                &target,
                &query.issue_repository,
                query.issue_number,
            )
            .await
        {
            Ok(tier) => {
                state
                    .tier_cache
                    .lock()
                    .await
                    .insert(key, (Instant::now(), tier));
                tier
            }
            Err(AccessError::Revoked) => {
                state.sessions.end(Some(&session.id));
                return Err(ApiError(StatusCode::UNAUTHORIZED, "viewer token expired"));
            }
            Err(_) => {
                return Err(ApiError(
                    StatusCode::SERVICE_UNAVAILABLE,
                    "GitHub unavailable",
                ));
            }
        }
    };
    Ok(Json(
        json!({"user":{"id":session.user.id,"login":session.user.login},
        "tier":tier,"checked_at":Utc::now().to_rfc3339()}),
    ))
}
