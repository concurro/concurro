//! Durable JetStream pull consumption over the shared Postgres processor.

use std::time::Duration;

use anyhow::Result;
#[cfg(feature = "fault-injection")]
use anyhow::bail;
use async_nats::jetstream::{
    Context,
    consumer::{self, pull},
    message::AckKind,
};
use concurro_contracts::Contract;
use concurro_db::Database;
use concurro_events::{Handler, ProcessResult};
use concurro_events_postgres::{process_delivery, process_released};
use futures_util::StreamExt;
use serde::de::DeserializeOwned;

use crate::stream_identity;

/// JetStream state needed for durable continuity checks.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StreamStatus {
    /// Stream incarnation encoded as RFC 3339.
    pub created: String,
    /// First sequence the current stream still retains.
    pub first_sequence: u64,
    /// Consumer's contiguous acknowledgement floor.
    pub ack_floor: u64,
}

/// Read the stream incarnation, retention edge, and durable acknowledgement floor.
pub async fn inspect_consumer(
    context: &Context,
    stream_name: &str,
    consumer: &mut consumer::PullConsumer,
) -> Result<StreamStatus> {
    let (created, _) = stream_identity(context, stream_name).await?;
    let ack_floor = consumer.info().await?.ack_floor.stream_sequence;
    let (after, first_sequence) = stream_identity(context, stream_name).await?;
    if created != after {
        anyhow::bail!("stream incarnation changed during consumer inspection");
    }
    Ok(StreamStatus {
        created: after,
        first_sequence,
        ack_floor,
    })
}

/// Stop after a durable decision but before its transport acknowledgement.
#[cfg(feature = "fault-injection")]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ConsumerFault {
    /// Commit a handler effect and stop before acknowledging.
    AfterCommitBeforeAck,
    /// Store a hold and stop before acknowledging.
    AfterHoldBeforeAck,
    /// Park an event and stop before terminating the message.
    AfterParkBeforeTerm,
}

/// Wait for the producer stream, then create a durable explicit-ack pull consumer.
pub async fn ensure_pull_consumer(
    context: &Context,
    stream_name: &str,
    durable_name: &str,
    filter_subject: &str,
    ack_wait: Duration,
    retry_interval: Duration,
) -> Result<consumer::PullConsumer> {
    let stream = loop {
        match context.get_stream(stream_name).await {
            Ok(stream) => break stream,
            Err(error) => {
                use async_nats::jetstream::context::GetStreamErrorKind;
                match error.kind() {
                    GetStreamErrorKind::JetStream(ref jetstream) if jetstream.code() == 404 => {
                        tokio::time::sleep(retry_interval).await;
                    }
                    _ => return Err(error.into()),
                }
            }
        }
    };
    let config = pull::Config {
        durable_name: Some(durable_name.to_owned()),
        filter_subject: filter_subject.to_owned(),
        ack_policy: consumer::AckPolicy::Explicit,
        ack_wait,
        max_deliver: -1,
        ..Default::default()
    };
    stream
        .get_or_create_consumer(durable_name, config.clone())
        .await?;
    Ok(stream.update_consumer(config).await?)
}

/// Pull subscription and the stream identity needed for headerless deliveries.
pub struct ConsumerStream<'a> {
    /// Durable pull subscription.
    pub pull: &'a consumer::PullConsumer,
    /// JetStream context used to inspect the stream incarnation.
    pub context: &'a Context,
    /// Broker stream name.
    pub name: &'a str,
}

/// Consume deliveries and released held rows until the stream ends or the task stops.
pub async fn run_consumer<T, H>(
    stream: ConsumerStream<'_>,
    database: &Database,
    consumer_name: &str,
    max_attempts: i32,
    held_poll_interval: Duration,
    handler: &H,
) -> Result<()>
where
    T: Contract + DeserializeOwned + Clone + Send,
    H: Handler<T>,
{
    run_consumer_inner(
        stream,
        database,
        consumer_name,
        max_attempts,
        held_poll_interval,
        handler,
        #[cfg(feature = "fault-injection")]
        None,
    )
    .await
}

/// Run with one named stop point for crash and redelivery tests.
#[cfg(feature = "fault-injection")]
pub async fn run_consumer_with_fault<T, H>(
    stream: ConsumerStream<'_>,
    database: &Database,
    consumer_name: &str,
    max_attempts: i32,
    held_poll_interval: Duration,
    handler: &H,
    fault: ConsumerFault,
) -> Result<()>
where
    T: Contract + DeserializeOwned + Clone + Send,
    H: Handler<T>,
{
    run_consumer_inner(
        stream,
        database,
        consumer_name,
        max_attempts,
        held_poll_interval,
        handler,
        Some(fault),
    )
    .await
}

/// Drive both the pull stream and the released held queue.
async fn run_consumer_inner<T, H>(
    stream: ConsumerStream<'_>,
    database: &Database,
    consumer_name: &str,
    max_attempts: i32,
    held_poll_interval: Duration,
    handler: &H,
    #[cfg(feature = "fault-injection")] fault: Option<ConsumerFault>,
) -> Result<()>
where
    T: Contract + DeserializeOwned + Clone + Send,
    H: Handler<T>,
{
    let mut held_tick = tokio::time::interval(held_poll_interval);
    'restart: loop {
        let stream_created = match stream_identity(stream.context, stream.name).await {
            Ok((created, _)) => created,
            Err(error) => {
                tracing::warn!(%error, "JetStream stream identity unavailable");
                tokio::time::sleep(Duration::from_secs(1)).await;
                continue;
            }
        };
        let mut messages = match stream.pull.messages().await {
            Ok(messages) => messages,
            Err(error) => {
                tracing::warn!(%error, "JetStream consumer stream could not start");
                tokio::time::sleep(Duration::from_secs(1)).await;
                continue;
            }
        };
        loop {
            tokio::select! {
                next = messages.next() => {
                    let Some(Ok(message)) = next else {
                        tracing::warn!("JetStream consumer stream ended or failed; reconnecting");
                        tokio::time::sleep(Duration::from_secs(1)).await;
                        continue 'restart;
                    };
                    let fallback = if let Some(value) = message.message.headers.as_ref()
                        .and_then(|headers| headers.get("Nats-Msg-Id")) {
                        value.as_str().to_owned()
                    } else {
                        let Ok(info) = message.info() else {
                            let _ = message.ack_with(AckKind::Nak(Some(Duration::from_secs(1)))).await;
                            continue;
                        };
                        match stream_identity(stream.context, stream.name).await {
                            Ok((created, _)) if created == stream_created => {}
                            Ok(_) => {
                                let _ = message.ack_with(AckKind::Nak(Some(Duration::from_secs(1)))).await;
                                continue 'restart;
                            }
                            Err(error) => {
                                tracing::warn!(%error, "JetStream stream identity unavailable");
                                let _ = message.ack_with(AckKind::Nak(Some(Duration::from_secs(1)))).await;
                                tokio::time::sleep(Duration::from_secs(1)).await;
                                continue 'restart;
                            }
                        }
                        format!("{}:{}:{}", info.stream, stream_created, info.stream_sequence)
                    };
                    let result = match process_delivery::<T, H>(
                        database,
                        consumer_name,
                        max_attempts,
                        &message.message.payload,
                        &fallback,
                        handler,
                    ).await {
                        Ok(result) => result,
                        Err(error) => {
                            tracing::warn!(%error, "durable event processing failed; requesting redelivery");
                            let _ = message.ack_with(AckKind::Nak(Some(Duration::from_secs(1)))).await;
                            continue;
                        }
                    };
                    #[cfg(feature = "fault-injection")]
                    if matches!(
                        (fault, &result),
                        (Some(ConsumerFault::AfterCommitBeforeAck), ProcessResult::Done)
                            | (Some(ConsumerFault::AfterHoldBeforeAck), ProcessResult::Held)
                            | (Some(ConsumerFault::AfterParkBeforeTerm), ProcessResult::Parked)
                    ) {
                        bail!("injected stop before consumer acknowledgement");
                    }
                    let ack = match result {
                        ProcessResult::Done | ProcessResult::Skipped | ProcessResult::Held => {
                            message.ack().await
                        }
                        ProcessResult::Parked => {
                            message.ack_with(AckKind::Term).await
                        }
                        ProcessResult::Retry(delay) => {
                            message.ack_with(AckKind::Nak(Some(delay))).await
                        }
                    };
                    if ack.is_err() {
                    tracing::warn!("JetStream acknowledgement failed; reconnecting consumer stream");
                        tokio::time::sleep(Duration::from_secs(1)).await;
                        continue 'restart;
                    }
                }
                _ = held_tick.tick() => {
                loop {
                    match process_released::<T, H>(database, consumer_name, max_attempts, handler).await {
                        Ok(Some(_)) => {}
                        Ok(None) => break,
                        Err(error) => {
                            tracing::warn!(%error, "released event processing failed; retrying on next tick");
                            break;
                        }
                    }
                }
                }
            }
        }
    }
}
