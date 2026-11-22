//! Inactive Phase 5 org inference gateway process.

use std::{collections::HashMap, sync::Arc, time::Duration};

use anyhow::{Result, ensure};
use concurro_config::{Validate, load, read_secret};
use concurro_exchange_client::AdmissionClient;
use concurro_inference_gateway::{GatewayState, OrgRoute, router};
use concurro_secrets::{OpenBao, OpenBaoSettings, Secrets};
use serde::Deserialize;

/// Private admission and frozen org provider route settings.
#[derive(Deserialize)]
struct Settings {
    /// HTTP listener for runner inference traffic.
    listen: String,
    /// Exchange private admission API.
    exchange_admission_url: String,
    /// File with the admission bearer.
    admission_bearer_file: String,
    /// Org routes keyed by the grant's route name.
    routes: HashMap<String, OrgRoute>,
    /// OpenBao AppRole when route tokens use OpenBao.
    openbao: Option<OpenBaoSettings>,
}

impl Validate for Settings {
    fn validate(&self) -> Result<()> {
        ensure!(
            !self.listen.is_empty() && !self.exchange_admission_url.is_empty(),
            "gateway listener and admission URL required"
        );
        ensure!(!self.routes.is_empty(), "gateway needs org routes");
        for route in self.routes.values() {
            ensure!(
                matches!(route.auth.as_str(), "bearer" | "x-api-key"),
                "invalid org auth mode"
            );
        }
        Ok(())
    }
}

/// Start the standalone org gateway against the configured private APIs.
#[tokio::main]
async fn main() -> Result<()> {
    concurro_telemetry::init("info").map_err(|error| anyhow::anyhow!(error.to_string()))?;
    let settings: Settings = load("CONCURRO_INFERENCE_GATEWAY_CONFIG")?;
    let openbao = match settings.openbao {
        Some(settings) => Some(OpenBao::connect(settings).await?),
        None => None,
    };
    let http = reqwest::Client::new();
    let state = GatewayState {
        admission: AdmissionClient::new(
            http.clone(),
            settings.exchange_admission_url,
            read_secret(&settings.admission_bearer_file)?,
        ),
        provider: http,
        routes: Arc::new(settings.routes),
        secrets: Arc::new(Secrets::new(openbao, Duration::from_secs(60))),
    };
    let listener = tokio::net::TcpListener::bind(&settings.listen).await?;
    tracing::info!(listener = %settings.listen, "org inference gateway ready");
    axum::serve(listener, router(state)).await?;
    Ok(())
}
