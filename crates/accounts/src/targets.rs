//! Current task-owned target identities for viewer authorization.

use std::{
    sync::Arc,
    time::{Duration, Instant},
};

use anyhow::{Context, Result};
use concurro_contracts::ConfigTarget;
use reqwest::Client;
use serde::Deserialize;
use tokio::sync::RwLock;

/// Revision and identities returned by the task application.
#[derive(Deserialize)]
struct Snapshot {
    /// Durable target revision.
    revision: i64,
    /// Target identities from the same row.
    targets: Vec<ConfigTarget>,
}

/// Cached target list and the time it was read.
struct Cached {
    /// Monotonic refresh timestamp.
    checked_at: Instant,
    /// Durable source revision.
    revision: i64,
    /// Current target identities.
    targets: Vec<ConfigTarget>,
}

/// One-minute cache of the task application's revisioned target row.
#[derive(Clone)]
pub struct Targets {
    client: Client,
    task_api_url: String,
    refresh_interval: Duration,
    cache: Arc<RwLock<Option<Cached>>>,
}

impl Targets {
    /// Bind the private task-service endpoint.
    pub fn new(client: Client, task_api_url: String) -> Self {
        Self::with_refresh_interval(client, task_api_url, Duration::from_secs(60))
    }

    /// Set a refresh interval for bounded local integration tests.
    pub fn with_refresh_interval(
        client: Client,
        task_api_url: String,
        refresh_interval: Duration,
    ) -> Self {
        Self {
            client,
            task_api_url,
            refresh_interval,
            cache: Arc::new(RwLock::new(None)),
        }
    }

    /// Refresh both revision and targets from one HTTP response.
    pub async fn refresh(&self) -> Result<()> {
        let snapshot: Snapshot = self
            .client
            .get(format!(
                "{}/v1/targets",
                self.task_api_url.trim_end_matches('/')
            ))
            .send()
            .await
            .context("target source unavailable")?
            .error_for_status()
            .context("target source unavailable")?
            .json()
            .await
            .context("target source invalid")?;
        *self.cache.write().await = Some(Cached {
            checked_at: Instant::now(),
            revision: snapshot.revision,
            targets: snapshot.targets,
        });
        Ok(())
    }

    /// Find a target, refreshing stale or unknown identities before deciding.
    pub async fn find(&self, upstream: &str) -> Result<Option<ConfigTarget>> {
        let stale = self
            .cache
            .read()
            .await
            .as_ref()
            .is_none_or(|cache| cache.checked_at.elapsed() >= self.refresh_interval);
        if stale {
            let result = self.refresh().await;
            if self.cache.read().await.is_none() {
                result?;
            }
        }
        let found = self.cache.read().await.as_ref().and_then(|cache| {
            cache
                .targets
                .iter()
                .find(|target| target.upstream.eq_ignore_ascii_case(upstream))
                .cloned()
        });
        if found.is_some() {
            return Ok(found);
        }
        if !stale {
            let _ = self.refresh().await;
        }
        Ok(self.cache.read().await.as_ref().and_then(|cache| {
            cache
                .targets
                .iter()
                .find(|target| target.upstream.eq_ignore_ascii_case(upstream))
                .cloned()
        }))
    }

    /// Return the last observed source revision for diagnostics.
    pub async fn revision(&self) -> Option<i64> {
        self.cache.read().await.as_ref().map(|cache| cache.revision)
    }
}
