//! Stateless org inference routing through exchange admission.

use std::{collections::HashMap, sync::Arc, time::Duration};

use axum::{
    Json, Router,
    body::{Body, Bytes},
    extract::{DefaultBodyLimit, Path, State},
    http::{HeaderMap, StatusCode, Uri, header},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use chrono::Utc;
use concurro_contracts::{AdmissionRequest, FinishRequest, SessionStatusRequest};
use concurro_exchange_client::{AdmissionClient, ClientError};
use concurro_relay_hosted_api::dispatch::{
    ALLOWANCE_SPENT_CODE, CLAIM_HEADER, CLAIM_REFUSED_CODE, MESSAGES_PATH, REQUEST_HEADER,
    REQUEST_REUSED_CODE, RESPONSES_PATH,
};
use concurro_secrets::{Error as SecretError, SecretRef, SecretString, Secrets};
use futures_util::{StreamExt, stream};
use reqwest::Client;
use secrecy::ExposeSecret;
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::{
    sync::{mpsc, watch},
    task::JoinHandle,
};
use uuid::Uuid;

mod google;
mod protocol;

/// Hosted dispatch endpoint for Google subscription envelopes.
const ANTIGRAVITY_PATH: &str = "/dispatch/v2/antigravity";
use protocol::{Observation, RouteKind};

/// Inputs shared by `pump`.
pub struct StreamLifetime {
    /// Cancellation signal for the active upstream response stream.
    pub cancelled: watch::Receiver<bool>,
    /// Admission-status monitor to stop after stream completion.
    pub monitor: JoinHandle<()>,
    /// Incremental provider usage and terminal-response parser.
    pub observation: Observation,
    /// Provider retry hint retained with the final usage observation.
    pub retry_after: Option<f64>,
}

/// Report uncertain dispatch if a downstream future disappears before handoff.
struct Inflight {
    admission: AdmissionClient,
    request: Uuid,
    monitor: Option<JoinHandle<()>>,
    active: bool,
}

impl Drop for Inflight {
    fn drop(&mut self) {
        if let Some(monitor) = self.monitor.take() {
            monitor.abort();
        }
        if self.active {
            report_unknown(self.admission.clone(), self.request);
        }
    }
}

/// One operator configured org route; the exchange chooses it from the grant.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OrgRoute {
    /// Provider API prefix, overridable with a fake in tests.
    pub upstream: String,
    /// Secret token or API key.
    pub token: SecretRef,
    /// `bearer`, `x-api-key`, or Gemini `x-goog-api-key` injection.
    pub auth: String,
    /// Optional ChatGPT account header.
    pub account: Option<SecretRef>,
    /// Optional server-owned Anthropic beta value, including OAuth routing.
    pub anthropic_beta: Option<String>,
}

/// Provider destination for a personal OpenBao version; authorization comes from that version.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PersonalRoute {
    /// Provider API prefix.
    pub upstream: String,
    /// Server-owned Anthropic beta value, when the route needs one.
    pub anthropic_beta: Option<String>,
}

/// Private hosted verifier and its inference credential.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RelayDispatch {
    /// Hosted verifier base URL.
    pub verifier: String,
    /// Private inference bearer, separate from the runner's derived key.
    pub credential: SecretRef,
}

/// Gateway dependencies shared by request handlers.
#[derive(Clone)]
pub struct GatewayState {
    /// Private exchange admission client.
    pub admission: AdmissionClient,
    /// Provider HTTP transport.
    pub provider: Client,
    /// Org route table, indexed by frozen grant route.
    pub routes: Arc<HashMap<String, OrgRoute>>,
    /// Personal provider destinations keyed by approved credential route.
    pub personal_routes: Arc<HashMap<String, PersonalRoute>>,
    /// Hosted verifier for relay-custody versions.
    pub relay: Option<RelayDispatch>,
    /// Cached OpenBao and file secret resolver.
    pub secrets: Arc<Secrets>,
}

/// Org inference router for the runner's Responses and Messages APIs.
pub fn router(state: GatewayState) -> Router {
    Router::new()
        .route("/v1/models", get(models))
        .route("/v1/{*path}", post(forward))
        .route("/v1beta/{*path}", post(forward))
        .layer(DefaultBodyLimit::max(48 * 1024 * 1024))
        .with_state(state)
}

/// Extract the runner's derived session key without accepting alternate authority.
fn key(headers: &HeaderMap) -> Option<&str> {
    headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "))
        .or_else(|| {
            headers
                .get("x-api-key")
                .and_then(|value| value.to_str().ok())
        })
        .or_else(|| {
            headers
                .get("x-goog-api-key")
                .and_then(|value| value.to_str().ok())
        })
        .filter(|value| !value.is_empty())
}

/// Return a uniform body for refused or unavailable private requests.
fn refusal(status: StatusCode) -> Response {
    (
        status,
        Json(json!({"error":{"type":"unknown","message":"private request refused"}})),
    )
        .into_response()
}

/// Preserve exchange's status without reflecting its private refusal details.
fn admission_status(error: &ClientError) -> StatusCode {
    match error {
        ClientError::Refused { status, .. } => {
            StatusCode::from_u16(*status).unwrap_or(StatusCode::BAD_GATEWAY)
        }
        ClientError::Unavailable | ClientError::InvalidResponse => StatusCode::SERVICE_UNAVAILABLE,
    }
}

/// Exclude a version only when its immutable personal material is absent or incomplete.
fn secret_failure(error: &SecretError, personal: bool) -> &'static str {
    match error {
        SecretError::NotFound { .. } | SecretError::MissingField { .. } if personal => "auth",
        SecretError::Unavailable { .. } => "unavailable",
        _ => "unknown",
    }
}

/// Keep session-claim refusals separate from permanent personal credential failures.
fn relay_refusal_category(value: &Value) -> Option<&'static str> {
    match value["error"]["code"].as_str() {
        Some(CLAIM_REFUSED_CODE | REQUEST_REUSED_CODE) => Some("unknown"),
        Some(ALLOWANCE_SPENT_CODE) => Some("budget_denied"),
        Some("unavailable") => Some("unavailable"),
        _ => None,
    }
}

/// Resolve a provider credential, keeping same-path OpenBao account data paired.
async fn credential_material(
    secrets: &Secrets,
    token: &SecretRef,
    account: Option<&SecretRef>,
    personal: bool,
    fresh: bool,
) -> Result<(SecretString, Option<String>), SecretError> {
    let (token, account) = if fresh {
        secrets.refresh_pair(token, account).await?
    } else {
        secrets.get_pair(token, account).await?
    };
    let account = match account {
        Some(value) if personal => {
            let id = serde_json::from_str::<Value>(value.expose_secret())
                .ok()
                .and_then(|body| body["chatgpt_account_id"].as_str().map(str::to_owned))
                .ok_or_else(|| SecretError::MissingField {
                    path: "personal account".to_owned(),
                    field: "chatgpt_account_id".to_owned(),
                })?;
            Some(id)
        }
        Some(value) => Some(value.expose_secret().to_owned()),
        None => None,
    };
    Ok((token, account))
}

/// Apply only server-owned provider or verifier authority to a copied request.
fn authorized_request(
    mut request: reqwest::RequestBuilder,
    auth: &str,
    token: &SecretString,
    account: Option<&str>,
    claim: Option<&str>,
    request_id: Uuid,
) -> reqwest::RequestBuilder {
    request = match auth {
        "x-api-key" => request.header("x-api-key", token.expose_secret()),
        "x-goog-api-key" => request.header("x-goog-api-key", token.expose_secret()),
        _ => request.bearer_auth(token.expose_secret()),
    };
    if let Some(account) = account {
        request = request.header("chatgpt-account-id", account);
    }
    if let Some(claim) = claim {
        request = request
            .header(CLAIM_HEADER, claim)
            .header(REQUEST_HEADER, request_id.to_string());
    }
    request
}

/// Probe the current session without recording a provider request.
async fn models(State(state): State<GatewayState>, headers: HeaderMap) -> Response {
    let Some(key) = key(&headers) else {
        return refusal(StatusCode::UNAUTHORIZED);
    };
    match state.admission.probe(key).await {
        Ok(probe) => Json(json!({"object":"list","data":probe.models.into_iter().map(|id| json!({"id":id,"object":"model"})).collect::<Vec<_>>() })).into_response(),
        Err(error) => refusal(admission_status(&error)),
    }
}

/// Admit and forward one provider call; the response body remains a byte stream.
async fn forward(
    State(state): State<GatewayState>,
    Path(path): Path<String>,
    uri: Uri,
    headers: HeaderMap,
    mut body: Bytes,
) -> Response {
    let Some(key) = key(&headers) else {
        return refusal(StatusCode::UNAUTHORIZED);
    };
    let google = if uri.path().starts_with("/v1beta/") {
        match google::Request::parse(&path, uri.query()) {
            Ok(request) => Some(request),
            Err(_) => return refusal(StatusCode::UNPROCESSABLE_ENTITY),
        }
    } else {
        None
    };
    let parsed = match serde_json::from_slice::<Value>(&body) {
        Ok(value) if value.is_object() => value,
        _ => return refusal(StatusCode::UNPROCESSABLE_ENTITY),
    };
    let model = google
        .as_ref()
        .map(|request| request.model.clone())
        .or_else(|| parsed["model"].as_str().map(str::to_owned));
    if let Some(request) = &google
        && parsed
            .get("model")
            .is_some_and(|model| model.as_str() != Some(&request.model))
    {
        return refusal(StatusCode::UNPROCESSABLE_ENTITY);
    }
    let Some(model) = model else {
        return refusal(StatusCode::UNPROCESSABLE_ENTITY);
    };
    let admission = match state
        .admission
        .request(&AdmissionRequest {
            key: key.to_owned(),
            model,
            request_id: Some(Uuid::now_v7()),
            probe: None,
        })
        .await
    {
        Ok(value) => value,
        Err(error) => return refusal(admission_status(&error)),
    };
    let route = &admission.credential.route;
    let google_route = match admission.credential.custody.as_deref() {
        None => state
            .routes
            .get(route)
            .is_some_and(|route| route.auth == "x-goog-api-key"),
        Some("secret_store") => route == "gemini-api-key",
        Some("relay") => route == "antigravity-subscription",
        _ => false,
    };
    if google.is_some() != google_route {
        finish_local(&state.admission, admission.request_id, "unknown").await;
        return refusal(StatusCode::UNPROCESSABLE_ENTITY);
    }
    let path = google.as_ref().map_or(path, |request| request.path.clone());
    if route == "antigravity-subscription" {
        match google
            .as_ref()
            .expect("Google route checked")
            .subscription(&body, &admission.request_id.to_string())
        {
            Ok(envelope) => body = Bytes::from(envelope),
            Err(_) => {
                finish_local(&state.admission, admission.request_id, "unknown").await;
                return refusal(StatusCode::UNPROCESSABLE_ENTITY);
            }
        }
    }
    let (upstream, auth, token_ref, account_ref, beta, path, relay) =
        match admission.credential.custody.as_deref() {
            None => {
                let Some(org) = state.routes.get(route) else {
                    finish_local(&state.admission, admission.request_id, "unknown").await;
                    return refusal(StatusCode::BAD_GATEWAY);
                };
                (
                    org.upstream.clone(),
                    org.auth.as_str(),
                    org.token.clone(),
                    org.account.clone(),
                    org.anthropic_beta.clone(),
                    path,
                    false,
                )
            }
            Some("secret_store") => {
                let Some(personal) = state.personal_routes.get(route) else {
                    finish_local(&state.admission, admission.request_id, "unknown").await;
                    return refusal(StatusCode::BAD_GATEWAY);
                };
                let Some(record) = admission.credential.record_id.as_deref() else {
                    finish_local(&state.admission, admission.request_id, "unknown").await;
                    return refusal(StatusCode::BAD_GATEWAY);
                };
                let source = format!("personal/{record}");
                let account = (route == "chatgpt-subscription").then(|| SecretRef::OpenBao {
                    path: source.clone(),
                    field: "account".to_owned(),
                });
                (
                    personal.upstream.clone(),
                    if route == "anthropic-api-key" {
                        "x-api-key"
                    } else if route == "gemini-api-key" {
                        "x-goog-api-key"
                    } else {
                        "bearer"
                    },
                    SecretRef::OpenBao {
                        path: source,
                        field: "secret".to_owned(),
                    },
                    account,
                    personal.anthropic_beta.clone(),
                    path,
                    false,
                )
            }
            Some("relay") => {
                let Some(relay) = &state.relay else {
                    finish_local(&state.admission, admission.request_id, "unknown").await;
                    return refusal(StatusCode::BAD_GATEWAY);
                };
                let destination = if google_route {
                    ANTIGRAVITY_PATH
                } else if path.ends_with("messages") {
                    MESSAGES_PATH
                } else if path.ends_with("responses") {
                    RESPONSES_PATH
                } else {
                    finish_local(&state.admission, admission.request_id, "unknown").await;
                    return refusal(StatusCode::UNPROCESSABLE_ENTITY);
                };
                (
                    relay.verifier.clone(),
                    "bearer",
                    relay.credential.clone(),
                    None,
                    None,
                    destination.trim_start_matches('/').to_owned(),
                    true,
                )
            }
            Some(_) => {
                finish_local(&state.admission, admission.request_id, "unknown").await;
                return refusal(StatusCode::BAD_GATEWAY);
            }
        };
    let personal = admission.credential.custody.as_deref() == Some("secret_store");
    let (token, account) = match credential_material(
        &state.secrets,
        &token_ref,
        account_ref.as_ref(),
        personal,
        false,
    )
    .await
    {
        Ok(value) => value,
        Err(error) => {
            finish_local(
                &state.admission,
                admission.request_id,
                secret_failure(&error, personal),
            )
            .await;
            return refusal(StatusCode::BAD_GATEWAY);
        }
    };
    let url = format!("{}/{}", upstream.trim_end_matches('/'), path);
    let mut request = state
        .provider
        .post(url)
        .body(body)
        .headers(forward_headers(&headers, true));
    let client_beta = headers
        .get("anthropic-beta")
        .and_then(|value| value.to_str().ok());
    if let Some(server_beta) = &beta {
        request = request.header(
            "anthropic-beta",
            match client_beta {
                Some(client_beta) if !client_beta.is_empty() => {
                    format!("{client_beta},{server_beta}")
                }
                _ => server_beta.clone(),
            },
        );
    } else if let Some(client_beta) = client_beta {
        request = request.header("anthropic-beta", client_beta);
    }
    if !matches!(auth, "bearer" | "x-api-key" | "x-goog-api-key") {
        finish_local(&state.admission, admission.request_id, "unknown").await;
        return refusal(StatusCode::BAD_GATEWAY);
    }
    if relay && admission.claim.is_none() {
        finish_local(&state.admission, admission.request_id, "unknown").await;
        return refusal(StatusCode::BAD_GATEWAY);
    }
    request = request.header(header::ACCEPT_ENCODING, "identity");
    let retry_request = request.try_clone();
    let request = authorized_request(
        request,
        auth,
        &token,
        account.as_deref(),
        admission.claim.as_deref(),
        admission.request_id,
    );
    let (cancel_tx, mut cancelled) = watch::channel(false);
    let monitor = tokio::spawn(monitor_session(
        state.admission.clone(),
        admission.session_id,
        admission.deadline.0,
        cancel_tx,
    ));
    let mut inflight = Inflight {
        admission: state.admission.clone(),
        request: admission.request_id,
        monitor: Some(monitor),
        active: true,
    };
    let mut provider = match tokio::select! {
        result = request.send() => result,
        _ = cancelled.changed() => return refusal(StatusCode::SERVICE_UNAVAILABLE),
    } {
        Ok(value) => value,
        // A transport error may follow provider receipt of the request body.
        Err(_) => return refusal(StatusCode::BAD_GATEWAY),
    };
    // A provider's 401 can be caused by a rotated org secret. Repeat the exact
    // admitted request only when a fresh snapshot actually changed authority.
    if !personal && !relay && provider.status() == StatusCode::UNAUTHORIZED {
        let fresh = tokio::select! {
            result = credential_material(
                &state.secrets,
                &token_ref,
                account_ref.as_ref(),
                false,
                true,
            ) => result,
            _ = cancelled.changed() => return refusal(StatusCode::SERVICE_UNAVAILABLE),
        };
        if let (Some(retry_request), Ok((fresh_token, fresh_account))) = (retry_request, fresh)
            && (fresh_token.expose_secret() != token.expose_secret() || fresh_account != account)
        {
            drop(provider);
            let retry = authorized_request(
                retry_request,
                auth,
                &fresh_token,
                fresh_account.as_deref(),
                None,
                admission.request_id,
            );
            provider = match tokio::select! {
                result = retry.send() => result,
                _ = cancelled.changed() => return refusal(StatusCode::SERVICE_UNAVAILABLE),
            } {
                Ok(value) => value,
                Err(_) => return refusal(StatusCode::BAD_GATEWAY),
            };
        }
    }
    let status = provider.status();
    if provider
        .headers()
        .get(header::CONTENT_ENCODING)
        .is_some_and(|value| value != "identity")
    {
        drop(provider);
        return refusal(StatusCode::BAD_GATEWAY);
    }
    let response_headers = forward_headers(provider.headers(), false);
    let media = provider
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .map(str::to_owned);
    let kind = if google_route {
        RouteKind::Google
    } else {
        RouteKind::for_request(
            route == "chatgpt-subscription" || account_ref.is_some(),
            &path,
        )
    };
    let retry_after = provider
        .headers()
        .get(header::RETRY_AFTER)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<f64>().ok());
    if !status.is_success() {
        let mut bytes = Vec::new();
        let mut parts = provider.bytes_stream();
        loop {
            let part = tokio::select! {
                part = parts.next() => part,
                _ = cancelled.changed() => break,
            };
            match part {
                Some(Ok(chunk))
                    if bytes.len().saturating_add(chunk.len()) <= protocol::MAX_EVENT =>
                {
                    bytes.extend_from_slice(&chunk)
                }
                _ => break,
            }
        }
        drop(parts);
        if *cancelled.borrow() {
            return refusal(StatusCode::SERVICE_UNAVAILABLE);
        }
        let value = serde_json::from_slice::<Value>(&bytes).ok();
        let category = value
            .as_ref()
            .and_then(|value| {
                if relay && let Some(category) = relay_refusal_category(value) {
                    return Some(category);
                }
                protocol::classify(kind, value)
            })
            .unwrap_or("unknown");
        let report = FinishRequest {
            dispatched: true,
            category: category.to_owned(),
            usage: None,
            usage_unavailable: Some("provider_error".to_owned()),
            retry_after,
        };
        inflight.active = false;
        tokio::spawn(report_finish(
            state.admission.clone(),
            admission.request_id,
            report,
        ));
        let mut response = refusal(status);
        response.headers_mut().extend(response_headers);
        response.headers_mut().insert(
            header::CONTENT_TYPE,
            "application/json".parse().expect("JSON media"),
        );
        return response;
    }
    inflight.active = false;
    let monitor = inflight.monitor.take().expect("request monitor");
    let (sender, receiver) = mpsc::channel::<Result<Bytes, std::io::Error>>(4);
    tokio::spawn(pump(
        provider,
        sender,
        state.admission.clone(),
        admission.request_id,
        StreamLifetime {
            cancelled,
            monitor,
            observation: Observation::new(kind, media.as_deref()),
            retry_after,
        },
    ));
    let output = stream::unfold(receiver, |mut receiver| async move {
        receiver.recv().await.map(|chunk| (chunk, receiver))
    });
    let mut response = Response::new(Body::from_stream(output));
    *response.status_mut() = status;
    response.headers_mut().extend(response_headers);
    response
}

/// Recheck a session throughout provider headers, body and downstream backpressure.
async fn monitor_session(
    admission: AdmissionClient,
    session: Uuid,
    deadline: chrono::DateTime<Utc>,
    cancelled: watch::Sender<bool>,
) {
    let mut tick = tokio::time::interval(Duration::from_secs(10));
    let remaining = (deadline - Utc::now()).to_std().unwrap_or_default();
    let expiry = tokio::time::sleep(remaining);
    tokio::pin!(expiry);
    let mut last_success = tokio::time::Instant::now();
    loop {
        tokio::select! {
            _ = &mut expiry => break,
            _ = tick.tick() => {
                match admission.sessions_status(&SessionStatusRequest { session_ids: vec![session] }).await {
                    Ok(value) if value.live.contains(&session) => last_success = tokio::time::Instant::now(),
                    Ok(_) => break,
                    Err(_) if last_success.elapsed() >= Duration::from_secs(30) => break,
                    Err(_) => {},
                }
            },
        }
    }
    let _ = cancelled.send(true);
}

/// Settle an uncertain transport result without claiming definite non-dispatch.
fn report_unknown(admission: AdmissionClient, request: Uuid) {
    tokio::spawn(report_finish(
        admission,
        request,
        FinishRequest {
            dispatched: true,
            category: "unknown".to_owned(),
            usage: None,
            usage_unavailable: Some("incomplete".to_owned()),
            retry_after: None,
        },
    ));
}

/// Remove transport, internal and caller credential authority while retaining native metadata.
fn forward_headers(headers: &HeaderMap, request: bool) -> HeaderMap {
    let nominated: Vec<String> = headers
        .get_all(header::CONNECTION)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .flat_map(|value| value.split(','))
        .map(|name| name.trim().to_ascii_lowercase())
        .collect();
    let mut forwarded = HeaderMap::new();
    for (name, value) in headers {
        let field = name.as_str();
        if nominated.iter().any(|item| item == field)
            || field.starts_with("x-concurro-")
            || field.starts_with("x-phase0-")
            || field.starts_with("x-internal-")
            || matches!(
                field,
                "host"
                    | "connection"
                    | "transfer-encoding"
                    | "content-length"
                    | "accept-encoding"
                    | "keep-alive"
                    | "proxy-authorization"
                    | "proxy-authenticate"
                    | "trailer"
                    | "upgrade"
                    | "te"
            )
            || (!request && field == "content-encoding")
            || (request
                && matches!(
                    field,
                    "authorization"
                        | "x-api-key"
                        | "x-goog-api-key"
                        | "x-goog-user-project"
                        | "chatgpt-account-id"
                        | "cookie"
                        | "origin"
                        | "anthropic-beta"
                ))
            || (!request && matches!(field, "set-cookie" | "www-authenticate"))
        {
            continue;
        }
        forwarded.append(name.clone(), value.clone());
    }
    forwarded
}

/// Record a failure before any provider dispatch.
async fn finish_local(admission: &AdmissionClient, id: Uuid, category: &str) {
    tokio::spawn(report_finish(
        admission.clone(),
        id,
        FinishRequest {
            dispatched: false,
            category: category.to_owned(),
            usage: None,
            usage_unavailable: Some("provider_error".to_owned()),
            retry_after: None,
        },
    ));
}

/// Retry an idempotent finish until acknowledged or the exchange's closure window ends.
async fn report_finish(admission: AdmissionClient, request: Uuid, report: FinishRequest) {
    let until = tokio::time::Instant::now() + Duration::from_secs(300);
    loop {
        if admission.finish(request, &report).await.is_ok() || tokio::time::Instant::now() >= until
        {
            break;
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}

/// Copy upstream bytes while checking session liveness and collecting bounded usage evidence.
async fn pump(
    provider: reqwest::Response,
    sender: mpsc::Sender<Result<Bytes, std::io::Error>>,
    admission: AdmissionClient,
    request: Uuid,
    inputs: StreamLifetime,
) {
    let StreamLifetime {
        mut cancelled,
        monitor,
        mut observation,
        retry_after,
    } = inputs;
    let mut stream = provider.bytes_stream();
    let mut complete = false;
    loop {
        tokio::select! {
            _ = sender.closed() => break,
            _ = cancelled.changed() => break,
            part = stream.next() => match part {
                Some(Ok(bytes)) => {
                    let output = observation.feed(&bytes);
                    for bytes in output {
                        tokio::select! {
                            _ = sender.closed() => break,
                            _ = cancelled.changed() => break,
                            result = sender.send(Ok(bytes)) => if result.is_err() { break; },
                        }
                    }
                    if observation.failed || observation.ambiguous || *cancelled.borrow() { break; }
                }
                Some(Err(_)) => { observation.ambiguous = true; break; }
                None => { complete = true; break; }
            },
        }
    }
    drop(stream);
    let (observation, body) = observation.finish();
    if complete
        && !*cancelled.borrow()
        && let Some(bytes) = body
    {
        // A JSON response is held until its terminal shape has been inspected.
        tokio::select! {
            _ = sender.closed() => {},
            _ = cancelled.changed() => {},
            _ = sender.send(Ok(bytes)) => {},
        }
    }
    monitor.abort();
    let usage = if complete && !observation.ambiguous && !observation.failed {
        observation.usage
    } else {
        None
    };
    let category = if !complete && !observation.failed {
        "unknown"
    } else {
        observation.category.unwrap_or("ok")
    };
    let unavailable = usage.is_none();
    let report = FinishRequest {
        dispatched: true,
        category: category.to_owned(),
        usage,
        usage_unavailable: if unavailable {
            Some(
                if observation.ambiguous {
                    "ambiguous"
                } else if observation.failed {
                    "provider_error"
                } else if !complete {
                    "incomplete"
                } else {
                    "unparsed_shape"
                }
                .to_owned(),
            )
        } else {
            None
        },
        retry_after,
    };
    drop(sender);
    report_finish(admission, request, report).await;
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{relay_refusal_category, secret_failure};
    use concurro_secrets::Error as SecretError;

    /// Google native credentials work while Bearer keeps its existing precedence.
    #[test]
    fn native_credentials_preserve_bearer_precedence() {
        let mut headers = super::HeaderMap::new();
        headers.insert("x-goog-api-key", "derived-key".parse().unwrap());
        assert_eq!(super::key(&headers), Some("derived-key"));
        headers.insert("authorization", "Bearer derived-key".parse().unwrap());
        assert_eq!(super::key(&headers), Some("derived-key"));
        headers.append("x-goog-api-key", "other-key".parse().unwrap());
        assert_eq!(super::key(&headers), Some("derived-key"));
    }

    /// S1 claim, allowance, reuse and link failures retain their distinct accounting.
    #[test]
    fn relay_refusals_preserve_credential_attribution() {
        for (code, category) in [
            ("claim_refused", "unknown"),
            ("request_reused", "unknown"),
            ("allowance_spent", "budget_denied"),
            ("unavailable", "unavailable"),
        ] {
            assert_eq!(
                relay_refusal_category(&json!({"error":{"code":code}})),
                Some(category)
            );
        }
        let missing = SecretError::NotFound {
            path: "personal/record".to_owned(),
        };
        assert_eq!(secret_failure(&missing, true), "auth");
        assert_eq!(secret_failure(&missing, false), "unknown");
        let unavailable = SecretError::Unavailable {
            path: "personal/record".to_owned(),
            status: None,
        };
        assert_eq!(secret_failure(&unavailable, true), "unavailable");
        assert_eq!(secret_failure(&unavailable, false), "unavailable");
        let refused = SecretError::Refused {
            path: "personal/record".to_owned(),
        };
        assert_eq!(secret_failure(&refused, true), "unknown");
    }
}
