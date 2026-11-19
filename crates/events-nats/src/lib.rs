//! NATS JetStream transport for Concurro events.

mod consumer;

#[cfg(feature = "fault-injection")]
pub use consumer::{ConsumerFault, run_consumer_with_fault};
pub use consumer::{
    ConsumerStream, StreamStatus, ensure_pull_consumer, inspect_consumer, run_consumer,
};

use std::time::Duration;

use anyhow::{Result, bail};
use async_nats::{
    Client, HeaderMap,
    jetstream::{
        self, Context,
        context::{PublishError, PublishErrorKind},
        stream,
    },
};
use concurro_db::Database;
use concurro_events::publish_size;
use concurro_events_postgres::ForwarderLease;
use sqlx::postgres::PgListener;

/// Read a stream's exact broker identity and retention edge.
pub async fn stream_identity(context: &Context, name: &str) -> Result<(String, u64)> {
    let response = context
        .client()
        .request(format!("$JS.API.STREAM.INFO.{name}"), Default::default())
        .await?;
    parse_stream_identity(&response.payload)
}

/// Extract the broker timestamp without rounding its fractional seconds.
fn parse_stream_identity(payload: &[u8]) -> Result<(String, u64)> {
    let info: serde_json::Value = serde_json::from_slice(payload)?;
    if let Some(error) = info.get("error") {
        bail!("stream info failed: {error}");
    }
    let created = info["created"]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("stream info lacks creation timestamp"))?;
    let first_sequence = info["state"]["first_seq"]
        .as_u64()
        .ok_or_else(|| anyhow::anyhow!("stream info lacks first sequence"))?;
    Ok((created.to_owned(), first_sequence))
}

/// Create a producer stream and reconcile its current configuration.
pub async fn ensure_stream(context: &Context, config: stream::Config) -> Result<()> {
    context.get_or_create_stream(config.clone()).await?;
    context.update_stream(config).await?;
    Ok(())
}

/// Identify failures shared by the NATS connection rather than one outbox row.
fn publish_outage(error: &anyhow::Error) -> bool {
    error.downcast_ref::<PublishError>().is_some_and(|publish| {
        matches!(
            publish.kind(),
            PublishErrorKind::TimedOut
                | PublishErrorKind::BrokenPipe
                | PublishErrorKind::MaxAckPending
        )
    })
}

/// Publish admitted outbox bytes and mark rows only after JetStream acknowledges them.
pub struct Forwarder {
    client: Client,
    context: Context,
    poll_interval: Duration,
    journal_stream: Option<String>,
    #[cfg(feature = "fault-injection")]
    fault_after_publish: bool,
}

impl Forwarder {
    /// Bind a forwarder to an established NATS connection.
    pub fn new(client: Client, poll_interval: Duration) -> Self {
        let context = jetstream::new(client.clone());
        Self {
            client,
            context,
            poll_interval,
            journal_stream: None,
            #[cfg(feature = "fault-injection")]
            fault_after_publish: false,
        }
    }

    /// Record acknowledged stream positions for replayable producer events.
    pub fn with_journal_stream(mut self, stream_name: impl Into<String>) -> Self {
        self.journal_stream = Some(stream_name.into());
        self
    }

    /// Read a stream's current incarnation before or after a publish.
    async fn stream_created(&self, name: &str) -> Result<String> {
        Ok(stream_identity(&self.context, name).await?.0)
    }

    /// Stop after JetStream accepts a row, before Postgres marks it published.
    #[cfg(feature = "fault-injection")]
    pub fn with_after_publish_fault(mut self) -> Self {
        self.fault_after_publish = true;
        self
    }

    /// Forward one eligible row, returning false when there is no work.
    pub async fn forward_one(&self, lease: &mut ForwarderLease) -> Result<bool> {
        let Some(row) = lease.next().await? else {
            return Ok(false);
        };
        let message_id = format!("{}:{}", row.id, row.version);
        let mut inspection_failed = false;
        let result: Result<Option<(String, i64)>> = async {
            let total_size = publish_size(row.wire_bytes.len(), &message_id);
            if total_size > self.client.max_payload() {
                bail!(
                    "event and Nats-Msg-Id header are {total_size} bytes; server max_payload is {}",
                    self.client.max_payload()
                );
            }
            let mut headers = HeaderMap::new();
            headers.insert("Nats-Msg-Id", message_id.as_str());
            let before = if let Some(name) = &self.journal_stream {
                Some(match self.stream_created(name).await {
                    Ok(created) => created,
                    Err(error) => {
                        inspection_failed = true;
                        return Err(error);
                    }
                })
            } else {
                None
            };
            let acknowledgment = self
                .context
                .publish_with_headers(row.subject, headers, row.wire_bytes.into())
                .await?;
            let acknowledged = acknowledgment.await?;
            if let Some(name) = &self.journal_stream {
                if acknowledged.stream != *name {
                    bail!(
                        "publish ack came from unexpected stream {}",
                        acknowledged.stream
                    );
                }
                let after = match self.stream_created(name).await {
                    Ok(created) => created,
                    Err(error) => {
                        inspection_failed = true;
                        return Err(error);
                    }
                };
                if before.as_deref() != Some(after.as_str()) {
                    bail!("stream incarnation changed across publish acknowledgement");
                }
                return Ok(Some((after, i64::try_from(acknowledged.sequence)?)));
            }
            Ok(None)
        }
        .await;
        #[cfg(feature = "fault-injection")]
        if self.fault_after_publish && result.is_ok() {
            bail!("injected stop after publish");
        }
        match result {
            Ok(position) => {
                lease
                    .mark_published(
                        row.sequence,
                        position
                            .as_ref()
                            .map(|(created, sequence)| (created.as_str(), *sequence)),
                    )
                    .await?
            }
            Err(error) => {
                if inspection_failed || publish_outage(&error) {
                    return Err(error);
                }
                let delay = 1_i64 << row.failure_count.clamp(0, 10);
                lease
                    .record_failure(row.sequence, &error.to_string(), delay)
                    .await?;
                if let Some(status) = lease.oldest_unpublished().await? {
                    tracing::warn!(
                        age_seconds = status.age_seconds,
                        last_error = status.last_error.as_deref().unwrap_or("none"),
                        "oldest unpublished outbox event remains pending"
                    );
                }
            }
        }
        Ok(true)
    }

    /// Hold the producer lease and wake on committed appends or the poll interval.
    pub async fn run(
        &self,
        database: &Database,
        lease: &mut ForwarderLease,
        published_retention: Duration,
        max_message_bytes: usize,
    ) -> Result<()> {
        if max_message_bytes > self.client.max_payload() {
            bail!(
                "outbox message cap {max_message_bytes} exceeds server max_payload {}",
                self.client.max_payload()
            );
        }
        let retention_seconds = i64::try_from(published_retention.as_secs())?;
        let mut listener = None;
        loop {
            if listener.is_none()
                && let Ok(mut connected) = PgListener::connect_with(database.pool()).await
                && connected.listen("concurro_outbox").await.is_ok()
            {
                listener = Some(connected);
            }
            let forwarded = match self.forward_one(lease).await {
                Ok(forwarded) => forwarded,
                Err(error) if publish_outage(&error) => {
                    tracing::warn!(%error, "NATS publish outage; keeping outbox retry state unchanged");
                    tokio::time::sleep(Duration::from_secs(1)).await;
                    continue;
                }
                Err(error) => return Err(error),
            };
            if !forwarded {
                lease.purge_published(retention_seconds).await?;
                if let Some(connected) = listener.as_mut() {
                    tokio::select! {
                        notification = connected.recv() => {
                            if notification.is_err() {
                                listener = None;
                            }
                        }
                        () = tokio::time::sleep(self.poll_interval) => {}
                    }
                } else {
                    tokio::time::sleep(self.poll_interval).await;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{PublishError, PublishErrorKind, parse_stream_identity, publish_outage};

    /// The Python consumer and Rust forwarder must use identical broker bytes.
    #[test]
    fn broker_identity_retains_nine_fractional_digits() {
        let raw = br#"{"created":"2026-11-19T01:02:03.123456789Z","state":{"first_seq":7}}"#;
        assert_eq!(
            parse_stream_identity(raw).expect("stream info"),
            ("2026-11-19T01:02:03.123456789Z".to_owned(), 7)
        );
    }

    /// Distinguish transport outages from rejections tied to one row.
    #[test]
    fn publish_failure_classification() {
        for kind in [
            PublishErrorKind::TimedOut,
            PublishErrorKind::BrokenPipe,
            PublishErrorKind::MaxAckPending,
        ] {
            assert!(publish_outage(&PublishError::new(kind).into()));
        }
        for kind in [
            PublishErrorKind::StreamNotFound,
            PublishErrorKind::MaxPayloadExceeded,
            PublishErrorKind::Other,
        ] {
            assert!(!publish_outage(&PublishError::new(kind).into()));
        }
    }
}
