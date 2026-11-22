//! Native store maintenance: material destroy jobs, pending settlement, expiry and the
//! `last_used` consumer.

use std::time::Duration;

use concurro_accounts_postgres::credentials as storage;
use concurro_contracts::{Envelope, InferenceUsage};
use concurro_db::UnitOfWork;
use concurro_events::{Handler, Outcome};
use graphile_worker::{IntoTaskHandlerResult, TaskHandler, WorkerContext};
use serde::{Deserialize, Serialize};

use super::{Native, view};

/// How often pending versions are checked for settlement.
const SETTLE_INTERVAL: Duration = Duration::from_secs(30);
/// How often known expiries are applied.
const EXPIRY_INTERVAL: Duration = Duration::from_secs(3600);

/// Destroy one version's OpenBao material, retried until OpenBao confirms.
#[derive(Deserialize, Serialize)]
pub struct DestroyMaterialJob {
    /// The version whose material is destroyed.
    pub record_id: String,
}

impl TaskHandler for DestroyMaterialJob {
    const IDENTIFIER: &'static str = "destroy_credential_material";

    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let native = context
            .get_ext::<Native>()
            .expect("native credential store");
        native
            .destroy(&self.record_id)
            .await
            .map_err(|_| "credential material destroy pending".to_owned())
    }
}

/// Settle stale pending versions and relay reservations, and apply known expiries, until
/// the process stops.
pub async fn run_maintenance(native: Native) {
    let mut settle = tokio::time::interval(SETTLE_INTERVAL);
    let mut expire = tokio::time::interval(EXPIRY_INTERVAL);
    loop {
        tokio::select! {
            _ = settle.tick() => {
                if native.settle_stale().await.is_err() {
                    tracing::warn!("credential settlement unavailable");
                }
                if native.settle_intents().await.is_err() {
                    tracing::warn!("relay reservation settlement unavailable");
                }
            }
            _ = expire.tick() => {
                if native.expire().await.is_err() {
                    tracing::warn!("credential expiry unavailable");
                }
            }
        }
    }
}

/// Records each personal version's latest inference use from `inference.usage`.
#[derive(Clone, Copy, Debug, Default)]
pub struct UsageHandler;

impl Handler<InferenceUsage> for UsageHandler {
    async fn handle(&self, work: &mut UnitOfWork<'_>, event: Envelope<InferenceUsage>) -> Outcome {
        let Some(record_id) = &event.data.record_id else {
            return Outcome::Done;
        };
        match storage::record_use(work, record_id, &view::stamp(event.data.finished.0)).await {
            Ok(()) => Outcome::Done,
            Err(error) => Outcome::Retry(error.to_string()),
        }
    }
}
