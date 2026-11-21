//! Public GitHub webhook receipt and private retained-event replay.

use std::{
    collections::{HashMap, HashSet},
    net::SocketAddr,
    sync::Arc,
    time::Duration,
};

use anyhow::{Context, Result};
use async_nats::jetstream::{self, stream};
use axum::{
    Json, Router,
    body::{Body, Bytes, to_bytes},
    extract::{DefaultBodyLimit, Request, State},
    http::{HeaderMap, StatusCode},
    middleware::{Next, from_fn_with_state},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use chrono::{DateTime, Duration as ChronoDuration, Utc};
use concurro_config::{Validate, load, read_secret};
use concurro_db::Database;
use concurro_events::{ReplayAnswer, ReplayRequest};
use concurro_events_nats::{Forwarder, connect as connect_nats, ensure_stream};
use concurro_events_postgres::{ForwarderLease, Outbox, replay_retained};
use concurro_ingress_core::{DeliveryAttempt, DeliveryPage, decide_recovery};
use concurro_ingress_postgres::{
    RecordResult, RecoveryLease, SignedDelivery, advance_coverage, database_time, declare_gap,
    finalize_coverage, prune_payloads, received_guids, record_delivery, record_recovery_scan,
    remap_candidates, remap_delivery, status,
};
use hmac::{Hmac, KeyInit, Mac};
use serde::Deserialize;
use sha2::Sha256;
use subtle::ConstantTimeEq;
use tokio::sync::Semaphore;

/// Maximum wait for a webhook request slot before returning a retryable response.
const WEBHOOK_PERMIT_WAIT: Duration = Duration::from_millis(500);

/// Service addresses, storage, stream, and file-backed credentials.
#[derive(Clone, Deserialize)]
struct Settings {
    /// Public edge-facing listener.
    public_listen: SocketAddr,
    /// Private replay listener.
    internal_listen: String,
    /// File containing the PostgreSQL connection URL.
    database_url_file: String,
    /// NATS endpoint.
    nats_url: String,
    /// NATS user credential file for the ingress publisher.
    nats_credentials_file: Option<String>,
    /// File containing the webhook HMAC secret.
    webhook_secret_file: String,
    /// Installation IDs this ingress accepts.
    accepted_installations: Vec<i64>,
    /// Consumer name to bearer-token file.
    replay_bearer_files: HashMap<String, String>,
    /// Maximum total NATS publish bytes admitted to the outbox.
    max_message_bytes: usize,
    /// Retained published journal duration.
    published_retention_seconds: u64,
    /// Retained raw webhook body duration for remapping.
    payload_retention_seconds: u64,
    /// JetStream message lifetime.
    stream_max_age_seconds: u64,
    /// JetStream duplicate suppression window.
    duplicate_window_seconds: u64,
    /// Forwarder idle polling interval.
    forwarder_poll_ms: u64,
    /// Internal Python delivery API base URL.
    delivery_recovery_url: String,
    /// File containing the bearer for that API.
    delivery_recovery_bearer_file: String,
    /// Seconds between complete App delivery scans.
    recovery_interval_seconds: u64,
}

impl Validate for Settings {
    fn validate(&self) -> Result<()> {
        anyhow::ensure!(
            !self.accepted_installations.is_empty(),
            "accepted installations are empty"
        );
        anyhow::ensure!(
            !self.replay_bearer_files.is_empty(),
            "replay bearers are empty"
        );
        anyhow::ensure!(self.max_message_bytes > 0, "message limit must be positive");
        anyhow::ensure!(
            self.forwarder_poll_ms > 0,
            "forwarder interval must be positive"
        );
        anyhow::ensure!(
            self.recovery_interval_seconds > 0,
            "recovery interval must be positive"
        );
        anyhow::ensure!(
            self.duplicate_window_seconds > 0,
            "duplicate window must be positive"
        );
        anyhow::ensure!(
            self.stream_max_age_seconds > 0,
            "stream age must be positive"
        );
        anyhow::ensure!(
            self.published_retention_seconds > self.stream_max_age_seconds,
            "journal retention must exceed stream age"
        );
        anyhow::ensure!(
            self.payload_retention_seconds >= 3 * 24 * 60 * 60,
            "raw payload retention must cover the three-day recovery horizon"
        );
        Ok(())
    }
}

/// Shared immutable HTTP configuration and database access.
#[derive(Clone)]
struct AppState {
    /// Service database.
    database: Database,
    /// Shared atomic outbox admission.
    outbox: Outbox,
    /// GitHub webhook secret bytes.
    webhook_secret: Vec<u8>,
    /// Accepted installation IDs.
    installations: Vec<i64>,
    /// Per-consumer replay bearer values.
    replay_bearers: HashMap<String, String>,
}

/// Bound webhook body extraction and handling across all connections.
async fn limit_webhook_inflight(
    State((limiter, body_deadline)): State<(Arc<Semaphore>, Duration)>,
    request: Request,
    next: Next,
) -> Response {
    let _permit = match tokio::time::timeout(WEBHOOK_PERMIT_WAIT, limiter.acquire()).await {
        Ok(Ok(permit)) => permit,
        Ok(Err(_)) | Err(_) => return StatusCode::SERVICE_UNAVAILABLE.into_response(),
    };
    let (parts, body) = request.into_parts();
    let raw = match tokio::time::timeout(body_deadline, to_bytes(body, 25_000_000)).await {
        Ok(Ok(raw)) => raw,
        Ok(Err(_)) => return StatusCode::PAYLOAD_TOO_LARGE.into_response(),
        Err(_) => return StatusCode::REQUEST_TIMEOUT.into_response(),
    };
    next.run(Request::from_parts(parts, Body::from(raw))).await
}

/// Verify a GitHub HMAC header over the exact request body.
fn valid_signature(headers: &HeaderMap, raw: &[u8], secret: &[u8]) -> bool {
    let Some(encoded) = headers
        .get("X-Hub-Signature-256")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("sha256="))
    else {
        return false;
    };
    let Ok(signature) = hex::decode(encoded) else {
        return false;
    };
    let Ok(mut mac) = Hmac::<Sha256>::new_from_slice(secret) else {
        return false;
    };
    mac.update(raw);
    mac.verify_slice(&signature).is_ok()
}

/// Authenticate and durably record one GitHub webhook delivery.
async fn webhook(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    body: Bytes,
) -> (StatusCode, &'static str) {
    if !valid_signature(&headers, &body, &state.webhook_secret) {
        return (StatusCode::UNAUTHORIZED, "invalid signature");
    }
    let event = headers
        .get("X-GitHub-Event")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("");
    if event == "ping" {
        return (StatusCode::OK, "pong");
    }
    let Some(guid) = headers
        .get("X-GitHub-Delivery")
        .and_then(|value| value.to_str().ok())
        .filter(|value| !value.is_empty())
    else {
        return (StatusCode::BAD_REQUEST, "missing delivery");
    };
    let delivery = SignedDelivery {
        guid,
        event,
        payload: &body,
        installations: &state.installations,
    };
    match record_delivery(&state.database, &state.outbox, &delivery).await {
        Ok(RecordResult::Duplicate) => (StatusCode::OK, "duplicate"),
        Ok(RecordResult::Recorded(_)) => (StatusCode::ACCEPTED, "recorded"),
        Err(error) => {
            tracing::warn!(%error, "delivery receipt failed");
            (StatusCode::SERVICE_UNAVAILABLE, "database unavailable")
        }
    }
}

/// Authenticate an internal consumer and re-queue its retained journal batch.
async fn replay(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(request): Json<ReplayRequest>,
) -> Result<Json<ReplayAnswer>, StatusCode> {
    let token = headers
        .get("Authorization")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "))
        .ok_or(StatusCode::UNAUTHORIZED)?;
    let consumer = state
        .replay_bearers
        .iter()
        .find_map(|(consumer, expected)| {
            bool::from(expected.as_bytes().ct_eq(token.as_bytes())).then_some(consumer.as_str())
        })
        .ok_or(StatusCode::UNAUTHORIZED)?;
    replay_retained(&state.database, consumer, &request, 500)
        .await
        .map(Json)
        .map_err(|error| {
            tracing::warn!(%error, "replay failed");
            StatusCode::SERVICE_UNAVAILABLE
        })
}

/// Reconnect the producer transport without interrupting durable HTTP receipt.
async fn run_forwarder(settings: Settings, database: Database) {
    loop {
        let result: Result<()> = async {
            let client = connect_nats(
                &settings.nats_url,
                settings.nats_credentials_file.as_deref(),
            )
            .await?;
            let context = jetstream::new(client.clone());
            ensure_stream(
                &context,
                stream::Config {
                    name: "GITHUB".to_owned(),
                    subjects: vec!["github.>".to_owned(), "ingress.>".to_owned()],
                    max_age: Duration::from_secs(settings.stream_max_age_seconds),
                    duplicate_window: Duration::from_secs(settings.duplicate_window_seconds),
                    ..Default::default()
                },
            )
            .await?;
            let Some(mut lease) = ForwarderLease::try_acquire(&database).await? else {
                tokio::time::sleep(Duration::from_secs(1)).await;
                return Ok(());
            };
            Forwarder::new(client, Duration::from_millis(settings.forwarder_poll_ms))
                .with_journal_stream("GITHUB")
                .run(
                    &database,
                    &mut lease,
                    Duration::from_secs(settings.published_retention_seconds),
                    settings.max_message_bytes,
                )
                .await
        }
        .await;
        if let Err(error) = result {
            tracing::warn!(%error, "forwarder disconnected");
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}

/// Fetch every retained App delivery page before making recovery decisions.
async fn scan_recovery(
    client: &reqwest::Client,
    settings: &Settings,
    database: &Database,
    outbox: &Outbox,
    bearer: &str,
) -> Result<()> {
    let scan_started = database_time(database).await?;
    let mut attempts: Vec<DeliveryAttempt> = Vec::new();
    let mut cursor: Option<String> = None;
    let mut seen = HashSet::new();
    loop {
        let mut request = client
            .get(format!(
                "{}/v1/deliveries",
                settings.delivery_recovery_url.trim_end_matches('/')
            ))
            .bearer_auth(bearer);
        if let Some(value) = &cursor {
            request = request.query(&[("cursor", value)]);
        }
        let page: DeliveryPage = request.send().await?.error_for_status()?.json().await?;
        attempts.extend(page.deliveries);
        let Some(next) = page.next_cursor else {
            break;
        };
        if !seen.insert(next.clone()) {
            anyhow::bail!("delivery recovery API repeated a page cursor");
        }
        cursor = Some(next);
    }
    let guids: Vec<String> = attempts
        .iter()
        .map(|attempt| attempt.guid.clone())
        .collect();
    let received = received_guids(database, &guids).await?;
    let decision = decide_recovery(&attempts, &received, &settings.accepted_installations);
    for &attempt_id in &decision.redeliver {
        client
            .post(format!(
                "{}/v1/deliveries/{attempt_id}/redeliver",
                settings.delivery_recovery_url.trim_end_matches('/')
            ))
            .bearer_auth(bearer)
            .send()
            .await?
            .error_for_status()?;
    }
    record_recovery_scan(database, scan_started, &decision).await?;
    advance_coverage(
        database,
        outbox,
        scan_started,
        decision.unresolved == 0,
        &settings.accepted_installations,
    )
    .await?;
    Ok(())
}

/// Bound each delivery API request, including a stalled response body.
fn recovery_client(timeout: Duration) -> Result<reqwest::Client> {
    Ok(reqwest::Client::builder().timeout(timeout).build()?)
}

/// Hold one recovery lease per scan and retry on the configured interval.
async fn run_recovery(settings: Settings, database: Database, outbox: Outbox, bearer: String) {
    let client = recovery_client(Duration::from_secs(30)).expect("recovery HTTP client");
    loop {
        match RecoveryLease::try_acquire(&database).await {
            Ok(Some(_lease)) => {
                if let Err(error) =
                    scan_recovery(&client, &settings, &database, &outbox, &bearer).await
                {
                    tracing::warn!(%error, "delivery recovery scan failed");
                    if let Ok(now) = database_time(&database).await
                        && let Err(error) = advance_coverage(
                            &database,
                            &outbox,
                            now,
                            false,
                            &settings.accepted_installations,
                        )
                        .await
                    {
                        tracing::warn!(%error, "expired coverage gap failed");
                    }
                }
            }
            Ok(None) => {}
            Err(error) => tracing::warn!(%error, "delivery recovery lease failed"),
        }
        if let Err(error) = prune_payloads(
            &database,
            i64::try_from(settings.payload_retention_seconds).unwrap_or(i64::MAX),
        )
        .await
        {
            tracing::warn!(%error, "delivery payload pruning failed");
        }
        tokio::time::sleep(Duration::from_secs(settings.recovery_interval_seconds)).await;
    }
}

/// Retry publication barriers without waiting for a failed outbox row.
async fn run_finalizer(database: Database, outbox: Outbox, poll_interval: Duration) {
    loop {
        if let Err(error) = finalize_coverage(&database, &outbox).await {
            tracing::warn!(%error, "coverage finalization failed");
        }
        tokio::time::sleep(poll_interval).await;
    }
}

/// Parse one operator-supplied RFC 3339 time into UTC.
fn operator_time(value: &str) -> Result<DateTime<Utc>> {
    Ok(DateTime::parse_from_rfc3339(value)?.with_timezone(&Utc))
}

/// Run a bounded status, remap or declared-gap operator command.
async fn run_operator(
    arguments: &[String],
    settings: &Settings,
    database: &Database,
    outbox: &Outbox,
) -> Result<()> {
    let words: Vec<&str> = arguments.iter().map(String::as_str).collect();
    match words.as_slice() {
        ["status"] | ["status", "--since", _] => {
            let since = match words.as_slice() {
                [_, _, value] => operator_time(value)?,
                _ => Utc::now() - ChronoDuration::days(3),
            };
            println!(
                "{}",
                serde_json::to_string_pretty(&status(database, since).await?)?
            );
        }
        ["remap", "--from", from, "--to", to]
        | ["remap", "--from", from, "--to", to, "--event", _] => {
            let from = operator_time(from)?;
            let to = operator_time(to)?;
            anyhow::ensure!(from < to, "remap range must have from before to");
            let event = words.get(6).copied();
            let mut after = None;
            let mut mapped = 0u64;
            let mut examined = 0u64;
            loop {
                let page = remap_candidates(
                    database,
                    from,
                    to,
                    event,
                    i32::try_from(concurro_ingress_core::MAPPING_REVISION)?,
                    after.as_ref(),
                    500,
                )
                .await?;
                if page.is_empty() {
                    break;
                }
                for candidate in page {
                    let result = remap_delivery(
                        database,
                        outbox,
                        &candidate.guid,
                        &settings.accepted_installations,
                    )
                    .await?;
                    examined += 1;
                    if result
                        == RecordResult::Recorded(
                            concurro_ingress_postgres::ReceiptOutcome::Published,
                        )
                    {
                        mapped += 1;
                    }
                    after = Some(candidate);
                }
            }
            println!(
                "{}",
                serde_json::json!({"examined": examined, "published": mapped})
            );
        }
        ["gap", "--from", from, "--to", to] => {
            declare_gap(
                database,
                outbox,
                operator_time(from)?,
                operator_time(to)?,
                &settings.accepted_installations,
            )
            .await?;
            println!(
                "declared gap for {} installations",
                settings.accepted_installations.len()
            );
        }
        _ => anyhow::bail!(
            "usage: concurro-ingress [status [--since TIME] | remap --from TIME --to TIME [--event FAMILY] | gap --from TIME --to TIME]"
        ),
    }
    Ok(())
}

/// Start the public and private listeners after local migrations succeed.
#[tokio::main]
async fn main() -> Result<()> {
    concurro_telemetry::init("info").map_err(|error| anyhow::anyhow!(error.to_string()))?;
    let settings: Settings = load("CONCURRO_INGRESS_CONFIG")?;
    let database_url = read_secret(&settings.database_url_file)?;
    let database = Database::connect(&database_url, 10)
        .await
        .context("connect ingress database")?;
    concurro_events_postgres::migrate(&database).await?;
    concurro_ingress_postgres::migrate(&database).await?;
    let outbox = Outbox::new(
        settings.max_message_bytes,
        vec!["github.>".to_owned(), "ingress.>".to_owned()],
    );
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    if !arguments.is_empty() {
        return run_operator(&arguments, &settings, &database, &outbox).await;
    }
    let webhook_secret = read_secret(&settings.webhook_secret_file)?.into_bytes();
    let recovery_bearer = read_secret(&settings.delivery_recovery_bearer_file)?;
    let mut replay_bearers = HashMap::new();
    for (consumer, path) in &settings.replay_bearer_files {
        replay_bearers.insert(consumer.clone(), read_secret(path)?);
    }
    let state = Arc::new(AppState {
        database: database.clone(),
        outbox: outbox.clone(),
        webhook_secret,
        installations: settings.accepted_installations.clone(),
        replay_bearers,
    });
    let public = Router::new()
        .route("/health", get(|| async { StatusCode::OK }))
        .route(
            "/webhook",
            post(webhook).layer(from_fn_with_state(
                (Arc::new(Semaphore::new(8)), Duration::from_secs(5)),
                limit_webhook_inflight,
            )),
        )
        .layer(DefaultBodyLimit::max(25_000_000))
        .with_state(state.clone());
    let internal = Router::new()
        .route("/v1/replays", post(replay))
        .with_state(state);
    let public_listener = tokio::net::TcpListener::bind(settings.public_listen).await?;
    let internal_listener = tokio::net::TcpListener::bind(&settings.internal_listen).await?;
    tokio::spawn(run_recovery(
        settings.clone(),
        database.clone(),
        outbox.clone(),
        recovery_bearer,
    ));
    tokio::spawn(run_finalizer(
        database.clone(),
        outbox,
        Duration::from_millis(settings.forwarder_poll_ms),
    ));
    tokio::spawn(run_forwarder(settings, database));
    tokio::try_join!(
        axum::serve(public_listener, public),
        axum::serve(internal_listener, internal)
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{
        body::{Body, Bytes},
        http::Request,
    };
    use concurro_testkit::PostgresContainer;
    use futures_util::stream;
    use std::{
        sync::atomic::{AtomicUsize, Ordering},
        task::Poll,
    };
    use tokio::sync::{Semaphore, mpsc};
    use tower::ServiceExt;

    /// Required recovery and retention settings must not silently disable coverage.
    #[test]
    fn settings_validate_recovery_invariants() {
        let mut settings = Settings {
            public_listen: "127.0.0.1:8789".parse().expect("listen"),
            internal_listen: "127.0.0.1:8790".to_owned(),
            database_url_file: "database-url".to_owned(),
            nats_url: "nats://localhost:4222".to_owned(),
            nats_credentials_file: None,
            webhook_secret_file: "webhook-secret".to_owned(),
            accepted_installations: vec![7],
            replay_bearer_files: HashMap::from([("service".to_owned(), "bearer".to_owned())]),
            max_message_bytes: 1024,
            published_retention_seconds: 30 * 24 * 60 * 60,
            payload_retention_seconds: 30 * 24 * 60 * 60,
            stream_max_age_seconds: 7 * 24 * 60 * 60,
            duplicate_window_seconds: 120,
            forwarder_poll_ms: 1000,
            delivery_recovery_url: "http://localhost:8787".to_owned(),
            delivery_recovery_bearer_file: "delivery-bearer".to_owned(),
            recovery_interval_seconds: 300,
        };
        settings.validate().expect("valid recovery settings");
        settings.accepted_installations.clear();
        assert!(settings.validate().is_err());
        settings.accepted_installations.push(7);
        settings.published_retention_seconds = settings.stream_max_age_seconds;
        assert!(settings.validate().is_err());
        settings.published_retention_seconds += 1;
        settings.recovery_interval_seconds = 0;
        assert!(settings.validate().is_err());
    }

    /// Saturated slots reject unread bodies while admitted handlers finish and release capacity.
    #[tokio::test]
    async fn webhook_body_concurrency_is_bounded() {
        let (entered_tx, mut entered_rx) = mpsc::unbounded_channel();
        let gate = Arc::new(Semaphore::new(0));
        let limiter = Arc::new(Semaphore::new(8));
        let app = Router::new().route(
            "/webhook",
            post({
                let gate = gate.clone();
                let entered_tx = entered_tx.clone();
                move |_body: Bytes| {
                    let gate = gate.clone();
                    let entered_tx = entered_tx.clone();
                    async move {
                        entered_tx.send(()).expect("handler arrival");
                        let _permit = gate.acquire().await.expect("test gate");
                        StatusCode::OK
                    }
                }
            })
            .layer(from_fn_with_state(
                (limiter.clone(), Duration::from_secs(1)),
                limit_webhook_inflight,
            )),
        );
        let mut requests = Vec::new();
        for _ in 0..8 {
            let request = Request::post("/webhook")
                .body(Body::from("held"))
                .expect("request");
            let app = app.clone();
            requests.push(tokio::spawn(async move {
                app.oneshot(request).await.expect("response")
            }));
        }
        for _ in 0..8 {
            tokio::time::timeout(Duration::from_secs(2), entered_rx.recv())
                .await
                .expect("handler arrival deadline")
                .expect("handler arrival");
        }
        let polled = Arc::new(AtomicUsize::new(0));
        let body = Body::from_stream(stream::once({
            let polled = polled.clone();
            async move {
                polled.fetch_add(1, Ordering::SeqCst);
                Ok::<_, std::io::Error>(Bytes::from_static(b"queued"))
            }
        }));
        let queued = Request::post("/webhook")
            .body(body)
            .expect("queued request");
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(2), app.clone().oneshot(queued))
                .await
                .expect("permit wait deadline")
                .expect("queued response")
                .status(),
            StatusCode::SERVICE_UNAVAILABLE
        );
        assert_eq!(polled.load(Ordering::SeqCst), 0);
        assert_eq!(limiter.available_permits(), 0);
        gate.add_permits(8);
        for request in requests {
            assert_eq!(request.await.expect("task").status(), StatusCode::OK);
        }
        assert_eq!(limiter.available_permits(), 8);
        gate.add_permits(1);
        let later = Request::post("/webhook")
            .body(Body::from("later"))
            .expect("later request");
        assert_eq!(
            app.oneshot(later).await.expect("later response").status(),
            StatusCode::OK
        );
    }

    /// A stalled body loses its permit without cancelling a later handler.
    #[tokio::test]
    async fn stalled_webhook_bodies_release_permits() {
        let polled = Arc::new(AtomicUsize::new(0));
        let app = Router::new().route(
            "/webhook",
            post(|_body: Bytes| async {
                tokio::time::sleep(Duration::from_millis(300)).await;
                StatusCode::OK
            })
            .layer(from_fn_with_state(
                (Arc::new(Semaphore::new(8)), Duration::from_millis(250)),
                limit_webhook_inflight,
            )),
        );
        let mut stalled = Vec::new();
        for _ in 0..8 {
            let polled = polled.clone();
            let body = Body::from_stream(stream::poll_fn(move |_| {
                polled.fetch_add(1, Ordering::SeqCst);
                Poll::<Option<Result<Bytes, std::io::Error>>>::Pending
            }));
            let request = Request::post("/webhook")
                .body(body)
                .expect("stalled request");
            let app = app.clone();
            stalled.push(tokio::spawn(async move {
                app.oneshot(request).await.expect("stalled response")
            }));
        }
        tokio::time::timeout(Duration::from_secs(2), async {
            while polled.load(Ordering::SeqCst) < 8 {
                tokio::time::sleep(Duration::from_millis(1)).await;
            }
        })
        .await
        .expect("all slots reading stalled bodies");
        let request = Request::post("/webhook")
            .body(Body::from("valid"))
            .expect("later request");
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(2), app.oneshot(request))
                .await
                .expect("later request completes")
                .expect("later response")
                .status(),
            StatusCode::OK
        );
        for request in stalled {
            assert_eq!(
                request.await.expect("stalled task").status(),
                StatusCode::REQUEST_TIMEOUT
            );
        }
    }

    /// The public route checks signatures and size before committing accepted receipts.
    #[tokio::test]
    async fn webhook_http_receipt_paths() {
        let postgres = PostgresContainer::start().await.expect("Postgres");
        let database = Database::connect(postgres.url(), 2)
            .await
            .expect("database");
        concurro_events_postgres::migrate(&database)
            .await
            .expect("events schema");
        concurro_ingress_postgres::migrate(&database)
            .await
            .expect("ingress schema");
        let state = Arc::new(AppState {
            database: database.clone(),
            outbox: Outbox::new(1024 * 1024, vec!["github.>".to_owned()]),
            webhook_secret: b"test-secret".to_vec(),
            installations: vec![7],
            replay_bearers: HashMap::new(),
        });
        let app = Router::new()
            .route(
                "/webhook",
                post(webhook).layer(from_fn_with_state(
                    (Arc::new(Semaphore::new(8)), Duration::from_secs(1)),
                    limit_webhook_inflight,
                )),
            )
            .layer(DefaultBodyLimit::max(25_000_000))
            .with_state(state);
        let raw = include_bytes!(
            "../../../contracts/fixtures/github.issue_comment.v1/raw/representative.json"
        );
        let bad = Request::builder()
            .method("POST")
            .uri("/webhook")
            .header("X-Hub-Signature-256", "sha256=deadbeef")
            .header("X-GitHub-Event", "issue_comment")
            .header("X-GitHub-Delivery", "bad")
            .body(Body::from(raw.as_slice()))
            .expect("bad request");
        assert_eq!(
            app.clone()
                .oneshot(bad)
                .await
                .expect("bad response")
                .status(),
            StatusCode::UNAUTHORIZED
        );
        let large = Request::builder()
            .method("POST")
            .uri("/webhook")
            .body(Body::from(vec![b'x'; 25_000_001]))
            .expect("large request");
        assert_eq!(
            app.clone()
                .oneshot(large)
                .await
                .expect("large response")
                .status(),
            StatusCode::PAYLOAD_TOO_LARGE
        );
        let mut mac = Hmac::<Sha256>::new_from_slice(b"test-secret").expect("HMAC");
        mac.update(raw);
        let signature = format!("sha256={}", hex::encode(mac.finalize().into_bytes()));
        for (guid, expected) in [("first", StatusCode::ACCEPTED), ("first", StatusCode::OK)] {
            let request = Request::builder()
                .method("POST")
                .uri("/webhook")
                .header("X-Hub-Signature-256", &signature)
                .header("X-GitHub-Event", "issue_comment")
                .header("X-GitHub-Delivery", guid)
                .body(Body::from(raw.as_slice()))
                .expect("signed request");
            assert_eq!(
                app.clone()
                    .oneshot(request)
                    .await
                    .expect("signed response")
                    .status(),
                expected
            );
        }
        let mut other: serde_json::Value = serde_json::from_slice(raw).expect("fixture");
        other["installation"]["id"] = 99.into();
        let other = serde_json::to_vec(&other).expect("other installation");
        let mut mac = Hmac::<Sha256>::new_from_slice(b"test-secret").expect("HMAC");
        mac.update(&other);
        let other_signature = format!("sha256={}", hex::encode(mac.finalize().into_bytes()));
        let request = Request::builder()
            .method("POST")
            .uri("/webhook")
            .header("X-Hub-Signature-256", other_signature)
            .header("X-GitHub-Event", "issue_comment")
            .header("X-GitHub-Delivery", "other")
            .body(Body::from(other))
            .expect("other request");
        assert_eq!(
            app.clone()
                .oneshot(request)
                .await
                .expect("other response")
                .status(),
            StatusCode::ACCEPTED
        );
        let events: i64 = sqlx::query_scalar("SELECT count(*) FROM events.outbox")
            .fetch_one(database.pool())
            .await
            .expect("outbox count");
        assert_eq!(events, 1);
        database.pool().close().await;
        let request = Request::builder()
            .method("POST")
            .uri("/webhook")
            .header("X-Hub-Signature-256", &signature)
            .header("X-GitHub-Event", "issue_comment")
            .header("X-GitHub-Delivery", "database-down")
            .body(Body::from(raw.as_slice()))
            .expect("outage request");
        assert_eq!(
            app.oneshot(request)
                .await
                .expect("outage response")
                .status(),
            StatusCode::SERVICE_UNAVAILABLE
        );
    }

    /// A failed App attempt is retried before the first coverage gap can advance.
    #[tokio::test]
    async fn recovery_bridge_redelivers_before_coverage() {
        let postgres = PostgresContainer::start().await.expect("Postgres");
        let database = Database::connect(postgres.url(), 3)
            .await
            .expect("database");
        concurro_events_postgres::migrate(&database)
            .await
            .expect("events schema");
        concurro_ingress_postgres::migrate(&database)
            .await
            .expect("ingress schema");
        let outbox = Outbox::new(
            1024 * 1024,
            vec!["github.>".to_owned(), "ingress.>".to_owned()],
        );
        let retries = Arc::new(AtomicUsize::new(0));
        let listing = Router::new()
            .route(
                "/v1/deliveries",
                get(|| async {
                    Json(serde_json::json!({
                        "deliveries": [{
                            "id": 11,
                            "guid": "recovered-guid",
                            "installation_id": 7,
                            "status": "failed",
                            "status_code": 503
                        }],
                        "next_cursor": null
                    }))
                }),
            )
            .route(
                "/v1/deliveries/11/redeliver",
                post({
                    let retries = retries.clone();
                    move || {
                        let retries = retries.clone();
                        async move {
                            retries.fetch_add(1, Ordering::SeqCst);
                            StatusCode::ACCEPTED
                        }
                    }
                }),
            );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("fake API bind");
        let address = listener.local_addr().expect("fake API address");
        let server = tokio::spawn(async move { axum::serve(listener, listing).await });
        let settings = Settings {
            public_listen: "127.0.0.1:8789".parse().expect("listen"),
            internal_listen: "127.0.0.1:8790".to_owned(),
            database_url_file: String::new(),
            nats_url: String::new(),
            nats_credentials_file: None,
            webhook_secret_file: String::new(),
            accepted_installations: vec![7],
            replay_bearer_files: HashMap::new(),
            max_message_bytes: 1024 * 1024,
            published_retention_seconds: 0,
            payload_retention_seconds: 0,
            stream_max_age_seconds: 0,
            duplicate_window_seconds: 0,
            forwarder_poll_ms: 1000,
            delivery_recovery_url: format!("http://{address}"),
            delivery_recovery_bearer_file: String::new(),
            recovery_interval_seconds: 300,
        };
        let client = reqwest::Client::new();
        scan_recovery(&client, &settings, &database, &outbox, "test")
            .await
            .expect("unresolved scan");
        assert_eq!(retries.load(Ordering::SeqCst), 1);
        assert_eq!(
            status(&database, Utc::now())
                .await
                .expect("status")
                .covered_until,
            None
        );
        let raw = include_bytes!(
            "../../../contracts/fixtures/github.issue_comment.v1/raw/representative.json"
        );
        record_delivery(
            &database,
            &outbox,
            &SignedDelivery {
                guid: "recovered-guid",
                event: "issue_comment",
                payload: raw,
                installations: &[7],
            },
        )
        .await
        .expect("redelivered receipt");
        scan_recovery(&client, &settings, &database, &outbox, "test")
            .await
            .expect("resolved scan");
        assert_eq!(retries.load(Ordering::SeqCst), 1);
        let health = status(&database, Utc::now()).await.expect("status");
        assert!(health.covered_until.is_some());
        assert_eq!(
            health.last_scan.expect("scan").unresolved_guids,
            Vec::<String>::new()
        );
        let gap_count: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM events.outbox WHERE type = 'ingress.coverage.gap'",
        )
        .fetch_one(database.pool())
        .await
        .expect("gap count");
        assert_eq!(gap_count, 1);
        server.abort();
    }

    /// A connected bridge that never answers cannot hold the scan lease forever.
    #[tokio::test]
    async fn recovery_client_times_out_stalled_bridge() {
        let app = Router::new().route(
            "/v1/deliveries",
            get(|| async { std::future::pending::<Json<serde_json::Value>>().await }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bridge bind");
        let address = listener.local_addr().expect("bridge address");
        let server = tokio::spawn(async move { axum::serve(listener, app).await });
        let client = recovery_client(Duration::from_millis(100)).expect("client");
        let error = client
            .get(format!("http://{address}/v1/deliveries"))
            .send()
            .await
            .expect_err("bridge must time out");
        assert!(error.is_timeout());
        server.abort();
    }
}
