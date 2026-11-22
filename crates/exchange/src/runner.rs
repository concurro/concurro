//! GitHub OIDC runner API v2 and content-addressed artifacts.

use std::{
    sync::Arc,
    time::{Duration, Instant},
};

use axum::{
    Json, Router,
    body::{Body, Bytes, to_bytes},
    extract::{Path as RoutePath, Query, State},
    http::{HeaderMap, Request, StatusCode, header},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use chrono::{DateTime, Utc};
use concurro_contracts::{
    RunnerClaimReply, RunnerKeyReply, RunnerKeyRequest, RunnerNextTurn, Timestamp, TurnResultV2,
};
use concurro_exchange_postgres::{ClaimContext, Store, StoreError};
use concurro_secrets::{ExposeSecret, SecretRef, Secrets};
use hmac::{Hmac, KeyInit, Mac};
use jsonwebtoken::{Algorithm, DecodingKey, Validation, decode, decode_header, jwk::JwkSet};
use object_store::{ObjectStore, ObjectStoreExt, PutMode, PutOptions, path::Path};
use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use tokio::sync::Mutex;
use uuid::Uuid;

const ISSUER: &str = "https://token.actions.githubusercontent.com";
const AUDIENCE: &str = "concurro-gateway";
const MAX_BLOB: usize = 256 * 1024 * 1024;
const MAX_RESULT: usize = 1024 * 1024;

/// Runner listener dependencies; the secret value never appears in diagnostics.
#[derive(Clone)]
pub struct RunnerState {
    /// Shared operator storage and event outbox.
    pub store: Store,
    /// File or OpenBao secret resolver.
    pub secrets: Arc<Secrets>,
    /// `ops/exchange/token-key` field or a local test file.
    pub token_key: SecretRef,
    /// GitHub's JWKS endpoint, overridable for local fixture tests.
    pub jwks_url: String,
    /// Provider gateway URL returned after the engine handshake.
    pub inference_base_url: String,
    /// Content-addressed artifact bucket.
    pub objects: Arc<dyn ObjectStore>,
    /// Key prefix shared with task archive ingestion.
    pub object_prefix: String,
    /// HTTP transport for JWKS refresh.
    pub http: reqwest::Client,
    /// Cached verification keys.
    pub jwks: Arc<Mutex<Option<(Instant, JwkSet)>>>,
}

/// Runner-only routes; the edge forwards `/runner/v2/*` here.
pub fn runner_router(state: RunnerState) -> Router {
    Router::new()
        .route("/runner/v2/sessions/{session}/claim", post(claim))
        .route("/runner/v2/sessions/{session}/key", post(key))
        .route("/runner/v2/sessions/{session}/heartbeat", post(heartbeat))
        .route("/runner/v2/sessions/{session}/turns/next", get(next))
        .route(
            "/runner/v2/sessions/{session}/turns/{turn}/result",
            axum::routing::put(result),
        )
        .route(
            "/runner/v2/sessions/{session}/blobs/{kind}/{sha}",
            axum::routing::put(upload),
        )
        .route("/runner/v2/sessions/{session}/blobs/{sha}", get(download))
        .route("/runner/v2/sessions/{session}", axum::routing::delete(end))
        .route("/healthz", get(|| async { StatusCode::NO_CONTENT }))
        .with_state(state)
}

/// Stable, credential-free runner refusal.
#[derive(Debug)]
struct ApiError(StatusCode, &'static str);

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(serde_json::json!({"error": self.1}))).into_response()
    }
}

impl From<StoreError> for ApiError {
    fn from(error: StoreError) -> Self {
        match error {
            StoreError::Refused { status, code } => {
                Self(StatusCode::from_u16(status).expect("status"), code)
            }
            _ => {
                tracing::error!(%error, "exchange runner transaction failed");
                Self(StatusCode::SERVICE_UNAVAILABLE, "exchange_unavailable")
            }
        }
    }
}

/// Read a single bearer without including it in an error or log.
fn bearer(headers: &HeaderMap) -> Result<&str, ApiError> {
    headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "))
        .filter(|value| !value.is_empty())
        .ok_or(ApiError(StatusCode::UNAUTHORIZED, "missing_bearer"))
}

/// SHA-256 of an opaque runner credential.
fn token_hash(value: &str) -> String {
    hex::encode(Sha256::digest(value.as_bytes()))
}

/// Derive a stable opaque token from the OpenBao key and one session label.
async fn derived(state: &RunnerState, label: &str, prefix: &str) -> Result<String, ApiError> {
    let key = state
        .secrets
        .get(&state.token_key)
        .await
        .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "token_key_unavailable"))?;
    let mut mac = Hmac::<Sha256>::new_from_slice(key.expose_secret().as_bytes())
        .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "token_key_unavailable"))?;
    mac.update(label.as_bytes());
    Ok(format!(
        "{prefix}{}",
        hex::encode(mac.finalize().into_bytes())
    ))
}

/// Refresh and reuse GitHub's signed-key set for a short bounded interval.
async fn jwks(state: &RunnerState, kid: &str) -> Result<JwkSet, ApiError> {
    let mut cached = state.jwks.lock().await;
    if let Some((at, keys)) = cached.as_ref()
        && at.elapsed() < Duration::from_secs(300)
        && keys.find(kid).is_some()
    {
        return Ok(keys.clone());
    }
    let keys: JwkSet = state
        .http
        .get(&state.jwks_url)
        .timeout(Duration::from_secs(5))
        .send()
        .await
        .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "jwks_unavailable"))?
        .error_for_status()
        .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "jwks_unavailable"))?
        .json()
        .await
        .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "jwks_unavailable"))?;
    *cached = Some((Instant::now(), keys.clone()));
    Ok(keys)
}

/// Verify the signed GitHub proof before consulting session authority.
async fn oidc(state: &RunnerState, token: &str) -> Result<Value, ApiError> {
    let header =
        decode_header(token).map_err(|_| ApiError(StatusCode::UNAUTHORIZED, "invalid_oidc"))?;
    if header.alg != Algorithm::RS256 {
        return Err(ApiError(StatusCode::UNAUTHORIZED, "invalid_oidc"));
    }
    let kid = header
        .kid
        .ok_or(ApiError(StatusCode::UNAUTHORIZED, "invalid_oidc"))?;
    let set = jwks(state, &kid).await?;
    let key = set
        .find(&kid)
        .and_then(|value| DecodingKey::from_jwk(value).ok())
        .ok_or(ApiError(StatusCode::UNAUTHORIZED, "invalid_oidc"))?;
    let mut validation = Validation::new(Algorithm::RS256);
    validation.validate_exp = false;
    validation.validate_nbf = false;
    validation.validate_aud = false;
    validation.required_spec_claims.clear();
    let claims = decode::<Value>(token, &key, &validation)
        .map_err(|_| ApiError(StatusCode::UNAUTHORIZED, "invalid_oidc"))?
        .claims;
    let now = Utc::now().timestamp();
    let number = |name| claims.get(name).and_then(Value::as_i64);
    let audience = match claims.get("aud") {
        Some(Value::String(value)) => value == AUDIENCE,
        Some(Value::Array(values)) => values.iter().any(|value| value.as_str() == Some(AUDIENCE)),
        _ => false,
    };
    if claims.get("iss").and_then(Value::as_str) != Some(ISSUER)
        || !audience
        || !number("exp").is_some_and(|value| value >= now - 60)
        || number("nbf").is_some_and(|value| value > now + 60)
        || !number("iat").is_some_and(|value| now - 600 <= value && value <= now + 60)
        || claims
            .get("jti")
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
    {
        return Err(ApiError(StatusCode::UNAUTHORIZED, "invalid_oidc"));
    }
    Ok(claims)
}

/// Compare signed claims to the frozen session, recording only same-repository refusals.
async fn match_claims(
    state: &RunnerState,
    session: Uuid,
    claims: &Value,
    context: &ClaimContext,
) -> Result<(String, String, DateTime<Utc>), ApiError> {
    let text = |name: &str| match claims.get(name) {
        Some(Value::String(value)) => value.clone(),
        Some(value) => value.to_string(),
        None => "None".to_owned(),
    };
    if text("repository_id") != context.expected.repository_id
        || text("repository_owner_id") != context.expected.repository_owner_id
    {
        return Err(ApiError(StatusCode::FORBIDDEN, "foreign_repository"));
    }
    let expected = &context.expected;
    let mut mismatch = text("job_workflow_ref") != expected.job_workflow_ref
        || text("actor_id") != expected.actor_id;
    if let Some(sha) = expected.job_workflow_sha.as_deref() {
        mismatch |= text("job_workflow_sha") != sha;
    }
    mismatch |= text("event_name") != "workflow_dispatch"
        || text("ref") != format!("refs/heads/concurro/runs/{session}")
        || text("runner_environment") != "github-hosted"
        || text("run_attempt") != "1";
    let run = format!("{}/{}", text("run_id"), text("run_attempt"));
    mismatch |=
        text("run_id") == "None" || context.run_id.as_deref().is_some_and(|bound| bound != run);
    if mismatch {
        state
            .store
            .claim_refusal(session, "token does not match the session")
            .await?;
        return Err(ApiError(StatusCode::FORBIDDEN, "claim_mismatch"));
    }
    let jti = text("jti");
    let exp = DateTime::from_timestamp(claims["exp"].as_i64().expect("verified expiry"), 0)
        .ok_or(ApiError(StatusCode::UNAUTHORIZED, "invalid_oidc"))?;
    Ok((jti, run, exp))
}

/// Bind a single-use OIDC token and return its deterministic runner credential.
async fn claim(
    State(state): State<RunnerState>,
    RoutePath(session): RoutePath<Uuid>,
    headers: HeaderMap,
) -> Result<Json<RunnerClaimReply>, ApiError> {
    let claims = oidc(&state, bearer(&headers)?).await?;
    let context = state.store.claim_context(session).await?;
    let (jti, run, expiry) = match_claims(&state, session, &claims, &context).await?;
    let runner = derived(&state, &format!("runner/{session}/{run}"), "rt-concurro-").await?;
    state
        .store
        .bind_claim(session, &jti, expiry, &run, &token_hash(&runner))
        .await?;
    Ok(Json(RunnerClaimReply {
        engine: context.binding.engine,
        runner_token: runner,
        deadline: Timestamp(context.deadline),
    }))
}

/// Authenticate a runner bearer and persist a heartbeat.
async fn authenticate(
    state: &RunnerState,
    session: Uuid,
    headers: &HeaderMap,
) -> Result<(), ApiError> {
    state
        .store
        .runner_touch(session, &token_hash(bearer(headers)?))
        .await?;
    Ok(())
}

/// Return the derived inference key only for the claimed engine commit.
async fn key(
    State(state): State<RunnerState>,
    RoutePath(session): RoutePath<Uuid>,
    headers: HeaderMap,
    Json(body): Json<RunnerKeyRequest>,
) -> Result<Json<RunnerKeyReply>, ApiError> {
    authenticate(&state, session, &headers).await?;
    let secret = derived(&state, &format!("key/{session}"), "sk-concurro-").await?;
    let context = state
        .store
        .runner_key(session, &body.engine_sha, &token_hash(&secret))
        .await?;
    Ok(Json(RunnerKeyReply {
        session_key: secret,
        inference_base_url: state.inference_base_url,
        models: context.models,
        deadline: Timestamp(context.deadline),
        binding: context.binding,
    }))
}

/// Renew one live session during target setup or a long native turn.
async fn heartbeat(
    State(state): State<RunnerState>,
    RoutePath(session): RoutePath<Uuid>,
    headers: HeaderMap,
) -> Result<StatusCode, ApiError> {
    authenticate(&state, session, &headers).await?;
    Ok(StatusCode::NO_CONTENT)
}

/// Bounded long-poll request from a runner.
#[derive(Deserialize)]
struct Wait {
    wait: Option<u64>,
}

/// Poll for the assigned turn while renewing the runner's heartbeat.
async fn next(
    State(state): State<RunnerState>,
    RoutePath(session): RoutePath<Uuid>,
    Query(wait): Query<Wait>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    authenticate(&state, session, &headers).await?;
    let deadline = Instant::now() + Duration::from_secs(wait.wait.unwrap_or(0).min(50));
    loop {
        if let Some(turn) = state.store.runner_next(session).await? {
            return Ok(Json(RunnerNextTurn {
                turn_id: turn.turn_id,
                payload: turn.payload,
                deadline: Timestamp(turn.deadline),
            })
            .into_response());
        }
        if Instant::now() >= deadline {
            return Ok(StatusCode::NO_CONTENT.into_response());
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
        authenticate(&state, session, &headers).await?;
    }
}

/// Encode the documented object key for one validated digest and kind.
fn object_key(state: &RunnerState, kind: &str, sha: &str) -> Result<Path, ApiError> {
    let suffix = match kind {
        "archive" => "tar.gz",
        "workspace" => "json",
        _ => {
            return Err(ApiError(
                StatusCode::UNPROCESSABLE_ENTITY,
                "invalid_blob_kind",
            ));
        }
    };
    if sha.len() != 64
        || !sha
            .bytes()
            .all(|value| value.is_ascii_hexdigit() && !value.is_ascii_uppercase())
    {
        return Err(ApiError(
            StatusCode::UNPROCESSABLE_ENTITY,
            "invalid_blob_digest",
        ));
    }
    Path::parse(format!(
        "{}/sha256/{sha}.{suffix}",
        state.object_prefix.trim_matches('/')
    ))
    .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "object_store_unavailable"))
}

/// Upload an immutable artifact after authenticating and verifying its digest.
async fn upload(
    State(state): State<RunnerState>,
    RoutePath((session, kind, sha)): RoutePath<(Uuid, String, String)>,
    headers: HeaderMap,
    request: Request<Body>,
) -> Result<StatusCode, ApiError> {
    authenticate(&state, session, &headers).await?;
    let path = object_key(&state, &kind, &sha)?;
    let bytes = to_bytes(request.into_body(), MAX_BLOB + 1)
        .await
        .map_err(|_| ApiError(StatusCode::PAYLOAD_TOO_LARGE, "blob_too_large"))?;
    if bytes.len() > MAX_BLOB {
        return Err(ApiError(StatusCode::PAYLOAD_TOO_LARGE, "blob_too_large"));
    }
    if hex::encode(Sha256::digest(&bytes)) != sha {
        return Err(ApiError(
            StatusCode::UNPROCESSABLE_ENTITY,
            "blob_digest_mismatch",
        ));
    }
    let put = state
        .objects
        .put_opts(
            &path,
            bytes.clone().into(),
            PutOptions {
                mode: PutMode::Create,
                ..Default::default()
            },
        )
        .await;
    if let Err(error) = put {
        if !matches!(error, object_store::Error::AlreadyExists { .. }) {
            return Err(ApiError(
                StatusCode::SERVICE_UNAVAILABLE,
                "object_store_unavailable",
            ));
        }
        let existing = state
            .objects
            .get(&path)
            .await
            .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "object_store_unavailable"))?
            .bytes()
            .await
            .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "object_store_unavailable"))?;
        if existing != bytes {
            return Err(ApiError(StatusCode::CONFLICT, "blob_conflict"));
        }
    }
    state
        .store
        .record_blob(session, &sha, &kind, bytes.len())
        .await?;
    Ok(StatusCode::NO_CONTENT)
}

/// Return only a blob referenced by this runner's turn history.
async fn download(
    State(state): State<RunnerState>,
    RoutePath((session, sha)): RoutePath<(Uuid, String)>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    authenticate(&state, session, &headers).await?;
    let kind = state.store.blob_kind(session, &sha).await?;
    let path = object_key(&state, &kind, &sha)?;
    let bytes = state
        .objects
        .get(&path)
        .await
        .map_err(|_| ApiError(StatusCode::NOT_FOUND, "unknown_blob"))?
        .bytes()
        .await
        .map_err(|_| ApiError(StatusCode::SERVICE_UNAVAILABLE, "object_store_unavailable"))?;
    if hex::encode(Sha256::digest(&bytes)) != sha {
        return Err(ApiError(
            StatusCode::SERVICE_UNAVAILABLE,
            "object_store_unavailable",
        ));
    }
    Ok((StatusCode::OK, bytes).into_response())
}

/// Persist one bounded result and its previously uploaded artifact references.
async fn result(
    State(state): State<RunnerState>,
    RoutePath((session, turn)): RoutePath<(Uuid, Uuid)>,
    headers: HeaderMap,
    request: Request<Body>,
) -> Result<StatusCode, ApiError> {
    let runner_hash = token_hash(bearer(&headers)?);
    let bytes: Bytes = to_bytes(request.into_body(), MAX_RESULT + 1)
        .await
        .map_err(|_| ApiError(StatusCode::PAYLOAD_TOO_LARGE, "result_too_large"))?;
    if bytes.len() > MAX_RESULT {
        return Err(ApiError(StatusCode::PAYLOAD_TOO_LARGE, "result_too_large"));
    }
    let result: TurnResultV2 = serde_json::from_slice(&bytes)
        .map_err(|_| ApiError(StatusCode::UNPROCESSABLE_ENTITY, "invalid_result"))?;
    state
        .store
        .runner_result(session, turn, &runner_hash, &result)
        .await?;
    Ok(StatusCode::NO_CONTENT)
}

/// Close an authenticated runner session and its active turn.
async fn end(
    State(state): State<RunnerState>,
    RoutePath(session): RoutePath<Uuid>,
    headers: HeaderMap,
) -> Result<StatusCode, ApiError> {
    authenticate(&state, session, &headers).await?;
    state.store.runner_end(session).await?;
    Ok(StatusCode::NO_CONTENT)
}
