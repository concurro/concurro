//! Exchange operator service, inactive until the Phase 5 cutover.

use std::{sync::Arc, time::Duration};

use anyhow::{Result, ensure};
use async_nats::jetstream::{self, stream};
use chrono::{Duration as ChronoDuration, Utc};
use concurro_config::{Validate, load, read_secret};
use concurro_db::Database;
use concurro_events_nats::{Forwarder, connect as connect_nats, ensure_stream};
use concurro_events_postgres::{ForwarderLease, Outbox, migrate as migrate_events};
use concurro_exchange::{
    AdmissionState, OperatorState, RunnerState, admission_router, operator_router, runner_router,
};
use concurro_exchange_postgres::{Store, migrate as migrate_exchange};
use concurro_secrets::{OpenBao, OpenBaoSettings, SecretRef, Secrets};
use object_store::aws::AmazonS3Builder;
use serde::Deserialize;
use tokio::sync::Mutex;

/// Private listener, database, and exchange event transport settings.
#[derive(Clone, Deserialize)]
struct Settings {
    /// Operator listener bound to the platform network.
    operator_listen: String,
    /// Runner listener behind the public edge route.
    runner_listen: String,
    /// Private gateway admission listener.
    admission_listen: String,
    /// PostgreSQL URL file.
    database_url_file: String,
    /// Task application's operator bearer file.
    operator_bearer_file: String,
    /// Gateway's private admission credential file.
    admission_bearer_file: String,
    /// NATS endpoint for exchange facts.
    nats_url: String,
    /// Optional NATS credential file.
    nats_credentials_file: Option<String>,
    /// Secret key used for deterministic runner and gateway credentials.
    token_key: SecretRef,
    /// OpenBao AppRole settings when token_key uses OpenBao.
    openbao: Option<OpenBaoSettings>,
    /// GitHub Actions signing keys, replaceable by a local fake in tests.
    #[serde(default = "default_jwks_url")]
    jwks_url: String,
    /// Provider gateway base URL handed to the engine.
    inference_base_url: String,
    /// S3-compatible archive endpoint.
    archive_endpoint: String,
    /// Archive bucket.
    archive_bucket: String,
    /// Archive region.
    archive_region: String,
    /// File with object-store access key fields.
    archive_credentials_file: String,
    /// Shared content-addressed object prefix.
    archive_prefix: String,
    /// Outbox publish cap.
    #[serde(default = "default_max_message_bytes")]
    max_message_bytes: usize,
}

impl Validate for Settings {
    fn validate(&self) -> Result<()> {
        ensure!(
            !self.operator_listen.is_empty()
                && !self.runner_listen.is_empty()
                && !self.admission_listen.is_empty()
                && !self.nats_url.is_empty()
                && !self.inference_base_url.is_empty(),
            "exchange listeners, NATS and inference URLs required"
        );
        ensure!(
            self.max_message_bytes >= 1024,
            "exchange message cap too small"
        );
        Ok(())
    }
}

/// Object-store credentials read only from a private file.
#[derive(Deserialize)]
struct ArchiveCredentials {
    /// S3 access key ID.
    access_key_id: String,
    /// S3 secret key.
    secret_access_key: String,
}

/// GitHub's official Actions OIDC signing-key endpoint.
fn default_jwks_url() -> String {
    "https://token.actions.githubusercontent.com/.well-known/jwks".to_owned()
}

/// Default to the shared stream's one-megabyte event cap.
fn default_max_message_bytes() -> usize {
    1_048_576
}

/// Forward committed exchange events, reconnecting after transport outages.
async fn run_forwarder(settings: Settings, database: Database) {
    loop {
        let result: Result<()> = async {
            let client = connect_nats(
                &settings.nats_url,
                settings.nats_credentials_file.as_deref(),
            )
            .await?;
            let jetstream = jetstream::new(client.clone());
            ensure_stream(
                &jetstream,
                stream::Config {
                    name: "EXCHANGE".to_owned(),
                    subjects: vec!["exchange.turn.>".to_owned(), "inference.usage".to_owned()],
                    ..Default::default()
                },
            )
            .await?;
            let Some(mut lease) = ForwarderLease::try_acquire(&database).await? else {
                tokio::time::sleep(Duration::from_secs(1)).await;
                return Ok(());
            };
            Forwarder::new(client, Duration::from_millis(250))
                .with_journal_stream("EXCHANGE")
                .run(
                    &database,
                    &mut lease,
                    Duration::from_secs(7 * 24 * 3600),
                    settings.max_message_bytes,
                )
                .await
        }
        .await;
        if let Err(error) = result {
            tracing::warn!(%error, "exchange event forwarder disconnected");
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}

/// Preserve terminal identities as tombstones after the one-day live-turn window.
async fn run_pruner(store: Store) {
    let mut interval = tokio::time::interval(Duration::from_secs(60));
    loop {
        interval.tick().await;
        if let Err(error) = store.prune(Utc::now() - ChronoDuration::days(1)).await {
            tracing::warn!(%error, "exchange turn pruning pending");
        }
        if let Err(error) = store.reconcile_sessions().await {
            tracing::warn!(%error, "exchange runner reconciliation pending");
        }
        if let Err(error) = store.reconcile_requests().await {
            tracing::warn!(%error, "exchange usage reconciliation pending");
        }
    }
}

/// Start durable operator workflow and its event forwarder.
#[tokio::main]
async fn main() -> Result<()> {
    concurro_telemetry::init("info").map_err(|error| anyhow::anyhow!(error.to_string()))?;
    let settings: Settings = load("CONCURRO_EXCHANGE_CONFIG")?;
    let database = Database::connect(&read_secret(&settings.database_url_file)?, 8).await?;
    migrate_events(&database).await?;
    migrate_exchange(&database).await?;
    let outbox = Outbox::new(
        settings.max_message_bytes,
        vec!["exchange.turn.>".to_owned(), "inference.usage".to_owned()],
    );
    let store = Store::new(database.clone(), outbox);
    let state = OperatorState {
        store: store.clone(),
        bearer: read_secret(&settings.operator_bearer_file)?,
    };
    let openbao = match settings.openbao.clone() {
        Some(settings) => Some(OpenBao::connect(settings).await?),
        None => None,
    };
    let credentials: ArchiveCredentials =
        serde_json::from_str(&read_secret(&settings.archive_credentials_file)?)?;
    let objects = AmazonS3Builder::new()
        .with_endpoint(&settings.archive_endpoint)
        .with_bucket_name(&settings.archive_bucket)
        .with_region(&settings.archive_region)
        .with_access_key_id(credentials.access_key_id)
        .with_secret_access_key(credentials.secret_access_key)
        .with_allow_http(settings.archive_endpoint.starts_with("http://"))
        .build()?;
    let runner = RunnerState {
        store: store.clone(),
        secrets: Arc::new(Secrets::new(openbao, Duration::from_secs(60))),
        token_key: settings.token_key.clone(),
        jwks_url: settings.jwks_url.clone(),
        inference_base_url: settings.inference_base_url.clone(),
        objects: Arc::new(objects),
        object_prefix: settings.archive_prefix.clone(),
        http: reqwest::Client::new(),
        jwks: Arc::new(Mutex::new(None)),
    };
    tokio::spawn(run_forwarder(settings.clone(), database));
    tokio::spawn(run_pruner(state.store.clone()));
    let listener = tokio::net::TcpListener::bind(&settings.operator_listen).await?;
    let runner_listener = tokio::net::TcpListener::bind(&settings.runner_listen).await?;
    let admission_listener = tokio::net::TcpListener::bind(&settings.admission_listen).await?;
    let admission = AdmissionState {
        store,
        bearer: read_secret(&settings.admission_bearer_file)?,
    };
    tracing::info!(listener = %settings.operator_listen, "exchange operator ready");
    tracing::info!(listener = %settings.runner_listen, "exchange runner ready");
    tracing::info!(listener = %settings.admission_listen, "exchange admission ready");
    tokio::try_join!(
        axum::serve(listener, operator_router(state)),
        axum::serve(runner_listener, runner_router(runner)),
        axum::serve(admission_listener, admission_router(admission)),
    )?;
    Ok(())
}
