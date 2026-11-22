//! Exchange operator service, inactive until the Phase 5 cutover.

use std::time::Duration;

use anyhow::{Result, ensure};
use async_nats::jetstream::{self, stream};
use chrono::{Duration as ChronoDuration, Utc};
use concurro_config::{Validate, load, read_secret};
use concurro_db::Database;
use concurro_events_nats::{Forwarder, connect as connect_nats, ensure_stream};
use concurro_events_postgres::{ForwarderLease, Outbox, migrate as migrate_events};
use concurro_exchange::{OperatorState, operator_router};
use concurro_exchange_postgres::{Store, migrate as migrate_exchange};
use serde::Deserialize;

/// Private listener, database, and exchange event transport settings.
#[derive(Clone, Deserialize)]
struct Settings {
    /// Operator listener bound to the platform network.
    operator_listen: String,
    /// PostgreSQL URL file.
    database_url_file: String,
    /// Task application's operator bearer file.
    operator_bearer_file: String,
    /// NATS endpoint for exchange facts.
    nats_url: String,
    /// Optional NATS credential file.
    nats_credentials_file: Option<String>,
    /// Outbox publish cap.
    #[serde(default = "default_max_message_bytes")]
    max_message_bytes: usize,
}

impl Validate for Settings {
    fn validate(&self) -> Result<()> {
        ensure!(
            !self.operator_listen.is_empty() && !self.nats_url.is_empty(),
            "exchange listener and NATS URL required"
        );
        ensure!(
            self.max_message_bytes >= 1024,
            "exchange message cap too small"
        );
        Ok(())
    }
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
    let state = OperatorState {
        store: Store::new(database.clone(), outbox),
        bearer: read_secret(&settings.operator_bearer_file)?,
    };
    tokio::spawn(run_forwarder(settings.clone(), database));
    tokio::spawn(run_pruner(state.store.clone()));
    let listener = tokio::net::TcpListener::bind(&settings.operator_listen).await?;
    tracing::info!(listener = %settings.operator_listen, "exchange operator ready");
    axum::serve(listener, operator_router(state)).await?;
    Ok(())
}
