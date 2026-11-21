//! Lower-privilege backoffice API with a durable task read model.

use std::{
    sync::{Arc, RwLock},
    time::Duration,
};

use anyhow::Result;
use async_nats::jetstream::{self, consumer::PullConsumer};
use concurro_backoffice_api::{api, model};
use concurro_config::{Validate, load, read_secret};
use concurro_contracts::{
    ConfigTargets, Contract, Envelope, ReadModelRecord, RequestOutcome, TaskNotice, TaskReport,
    TaskSnapshot, TurnFinished, TurnStatus, TurnSubmitted,
};
use concurro_db::Database;
use concurro_events::{Handler, Outcome};
use concurro_events_nats::{
    ConsumerStream, connect as connect_nats, ensure_pull_consumer, inspect_consumer, run_consumer,
};
use serde::{Deserialize, Serialize, de::DeserializeOwned};

use crate::{api::ApiState, model::TaskEvent};

/// Read-only process settings; private keys and command credentials are absent.
#[derive(Deserialize)]
struct Settings {
    /// Platform listener address.
    listen: String,
    /// File containing the backoffice-owned PostgreSQL URL.
    database_url_file: String,
    /// Private task-service API address.
    task_api_url: String,
    /// Path to the task service's read-scoped bearer.
    task_api_bearer_file: String,
    /// Accounts service's separate platform listener.
    viewer_authority_url: String,
    /// Path to its read-scoped service bearer.
    viewer_authority_bearer_file: String,
    /// Task event broker URL.
    nats_url: String,
    /// Private JSON file containing the NATS user and password.
    nats_credentials_file: Option<String>,
    /// Prefix for three durable task, request and turn consumers.
    consumer_name: String,
}

impl Validate for Settings {
    /// The read process has no additional settings constraints.
    fn validate(&self) -> Result<()> {
        Ok(())
    }
}

/// Project each typed source event in the shared consumer's unit of work.
struct ReadHandler;

impl<T> Handler<T> for ReadHandler
where
    T: Contract + Serialize + Send + Sync,
{
    async fn handle(
        &self,
        work: &mut concurro_db::UnitOfWork<'_>,
        envelope: Envelope<T>,
    ) -> Outcome {
        let event = TaskEvent {
            id: envelope.id.0,
            event_type: envelope.event_type,
            entity: envelope.entity,
            revision: envelope
                .revision
                .and_then(|revision| i64::try_from(revision).ok()),
            data: match serde_json::to_value(envelope.data) {
                Ok(data) => data,
                Err(error) => return Outcome::Reject(error.to_string()),
            },
        };
        match model::ingest_event_in_work(work, &event).await {
            Ok(()) => Outcome::Done,
            Err(error) => Outcome::Retry(error.to_string()),
        }
    }
}

/// Keep a durable typed pull consumer connected through broker interruptions.
async fn run_feed<T>(
    database: Database,
    context: jetstream::Context,
    consumer: PullConsumer,
    name: String,
) where
    T: Contract + DeserializeOwned + Serialize + Clone + Send + Sync,
{
    loop {
        let result = run_consumer::<T, _>(
            ConsumerStream {
                pull: &consumer,
                context: &context,
                name: "TASKS",
            },
            &database,
            &name,
            10,
            Duration::from_secs(1),
            &ReadHandler,
        )
        .await;
        if let Err(error) = result {
            tracing::warn!(%error, consumer = %name, "backoffice event consumer reconnecting");
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}

/// Start durable consumers before freezing the producer's baseline.
#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt::init();
    let settings: Settings = load("CONCURRO_BACKOFFICE_CONFIG")?;
    let database = Database::connect(&read_secret(&settings.database_url_file)?, 8).await?;
    concurro_events_postgres::migrate(&database).await?;
    model::migrate(&database).await?;
    let targets = Arc::new(RwLock::new(Vec::new()));
    let client = connect_nats(
        &settings.nats_url,
        settings.nats_credentials_file.as_deref(),
    )
    .await?;
    let stream = jetstream::new(client);
    let mut watched_consumers = Vec::new();
    for (suffix, subject) in [
        ("task-state", "task.*.state"),
        ("task-notice", "task.*.notice"),
        ("task-report", "task.*.report"),
        ("request", "request.>"),
        ("turn-submitted", "turn.*.submitted"),
        ("turn-status", "turn.*.status"),
        ("turn-finished", "turn.*.finished"),
        ("config", "config.targets"),
        ("view", "view.record"),
    ] {
        let consumer = ensure_pull_consumer(
            &stream,
            "TASKS",
            &format!("{}-{suffix}", settings.consumer_name),
            subject,
            Duration::from_secs(60),
            Duration::from_secs(1),
        )
        .await?;
        watched_consumers.push(consumer.clone());
        let name = format!("{}-{suffix}", settings.consumer_name);
        match suffix {
            "task-state" => tokio::spawn(run_feed::<TaskSnapshot>(
                database.clone(),
                stream.clone(),
                consumer,
                name,
            )),
            "task-notice" => tokio::spawn(run_feed::<TaskNotice>(
                database.clone(),
                stream.clone(),
                consumer,
                name,
            )),
            "task-report" => tokio::spawn(run_feed::<TaskReport>(
                database.clone(),
                stream.clone(),
                consumer,
                name,
            )),
            "request" => tokio::spawn(run_feed::<RequestOutcome>(
                database.clone(),
                stream.clone(),
                consumer,
                name,
            )),
            "turn-submitted" => tokio::spawn(run_feed::<TurnSubmitted>(
                database.clone(),
                stream.clone(),
                consumer,
                name,
            )),
            "turn-status" => tokio::spawn(run_feed::<TurnStatus>(
                database.clone(),
                stream.clone(),
                consumer,
                name,
            )),
            "turn-finished" => tokio::spawn(run_feed::<TurnFinished>(
                database.clone(),
                stream.clone(),
                consumer,
                name,
            )),
            "config" => tokio::spawn(run_feed::<ConfigTargets>(
                database.clone(),
                stream.clone(),
                consumer,
                name,
            )),
            "view" => tokio::spawn(run_feed::<ReadModelRecord>(
                database.clone(),
                stream.clone(),
                consumer,
                name,
            )),
            _ => unreachable!(),
        };
    }
    let http = reqwest::Client::new();
    let task_bearer = read_secret(&settings.task_api_bearer_file)?;
    let viewer_bearer = read_secret(&settings.viewer_authority_bearer_file)?;
    loop {
        match model::rebuild(&database, &http, &settings.task_api_url, &task_bearer).await {
            Ok(snapshot) => {
                tracing::info!(%snapshot, "backoffice baseline ready");
                break;
            }
            Err(error) => {
                tracing::warn!(%error, "backoffice baseline unavailable; retrying");
                tokio::time::sleep(Duration::from_secs(5)).await;
            }
        }
    }
    let configured = model::configured_targets(&database).await?;
    *targets
        .write()
        .map_err(|_| anyhow::anyhow!("target cache unavailable"))? = configured;
    let baseline_database = database.clone();
    let baseline_http = http.clone();
    let baseline_url = settings.task_api_url.clone();
    let baseline_bearer = task_bearer.clone();
    let baseline_targets = targets.clone();
    tokio::spawn(async move {
        let mut known = Vec::new();
        for consumer in &mut watched_consumers {
            known.push(inspect_consumer(&stream, "TASKS", consumer).await.ok());
        }
        let mut rebuilt_gaps = vec![None::<(String, u64)>; known.len()];
        loop {
            tokio::time::sleep(Duration::from_secs(30)).await;
            let mut observed = Vec::new();
            for consumer in &mut watched_consumers {
                observed.push(inspect_consumer(&stream, "TASKS", consumer).await.ok());
            }
            let lost =
                observed
                    .iter()
                    .zip(&known)
                    .enumerate()
                    .any(|(index, (current, previous))| {
                        current.as_ref().is_some_and(|current| {
                            let gap = current.first_sequence > current.ack_floor.saturating_add(1);
                            let already_rebuilt =
                                rebuilt_gaps[index]
                                    .as_ref()
                                    .is_some_and(|(created, floor)| {
                                        *created == current.created && *floor == current.ack_floor
                                    });
                            (gap && !already_rebuilt)
                                || previous
                                    .as_ref()
                                    .is_some_and(|previous| current.created != previous.created)
                        })
                    });
            if !lost {
                for (old, current) in known.iter_mut().zip(observed) {
                    if current.is_some() {
                        *old = current;
                    }
                }
                if let Ok(configured) = model::configured_targets(&baseline_database).await
                    && let Ok(mut current) = baseline_targets.write()
                {
                    *current = configured;
                }
                continue;
            }
            tracing::warn!("backoffice stream continuity lost; rebuilding source projection");
            match model::rebuild(
                &baseline_database,
                &baseline_http,
                &baseline_url,
                &baseline_bearer,
            )
            .await
            {
                Ok(_) => match model::configured_targets(&baseline_database).await {
                    Ok(configured) => {
                        if let Ok(mut current) = baseline_targets.write() {
                            *current = configured;
                        }
                        rebuilt_gaps = observed
                            .iter()
                            .map(|current| {
                                current
                                    .as_ref()
                                    .map(|status| (status.created.clone(), status.ack_floor))
                            })
                            .collect();
                        known = observed;
                    }
                    Err(error) => {
                        tracing::warn!(%error, "target configuration refresh unavailable")
                    }
                },
                Err(error) => tracing::warn!(%error, "backoffice loss recovery pending"),
            }
        }
    });
    let state = ApiState {
        database,
        client: http,
        authority_url: settings.viewer_authority_url,
        authority_bearer: viewer_bearer,
        task_api_url: settings.task_api_url,
        task_read_bearer: task_bearer,
        targets,
    };
    let listener = tokio::net::TcpListener::bind(&settings.listen).await?;
    axum::serve(listener, api::router(state)).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use chrono::Utc;
    use concurro_contracts::{Envelope, EventId, ReadModelRecord, Timestamp};
    use concurro_events::ProcessResult;
    use concurro_events_postgres::process_delivery;
    use concurro_testkit::PostgresContainer;
    use serde_json::{Value, json};
    use sqlx::types::Json;
    use uuid::Uuid;

    use super::{ReadHandler, model};
    use concurro_db::Database;

    /// Shared delivery receipts and the projection commit survive duplicate view events.
    #[tokio::test]
    async fn typed_read_model_record_applies_once() {
        let postgres = PostgresContainer::start().await.expect("Postgres");
        let database = Database::connect(postgres.url(), 3)
            .await
            .expect("database");
        concurro_events_postgres::migrate(&database)
            .await
            .expect("event schema");
        model::migrate(&database).await.expect("read model schema");
        sqlx::query("UPDATE backoffice.model_state SET ready=true WHERE singleton")
            .execute(database.pool())
            .await
            .expect("ready model");
        let id = Uuid::now_v7();
        let record = ReadModelRecord {
            entity: "consent_state".to_owned(),
            entity_id: id.to_string(),
            body: json!({"task_id":id,"slots":{"openai":{"sponsor_id":"7"}}}),
        };
        let envelope = Envelope::snapshot(
            EventId(Uuid::now_v7()),
            "tasks",
            Timestamp(Utc::now()),
            format!("view:consent_state:{id}"),
            1,
            record,
        );
        let raw = serde_json::to_vec(&envelope).expect("wire event");
        for expected in [ProcessResult::Done, ProcessResult::Skipped] {
            let result = process_delivery::<ReadModelRecord, _>(
                &database,
                "backoffice-view",
                3,
                &raw,
                "delivery",
                &ReadHandler,
            )
            .await
            .expect("delivery");
            assert_eq!(result, expected);
        }
        let Json(body): Json<Value> = sqlx::query_scalar(
            "SELECT body FROM backoffice.model_items WHERE entity='consent_state' AND entity_id=$1",
        )
        .bind(id.to_string())
        .fetch_one(database.pool())
        .await
        .expect("projected row");
        assert_eq!(body["slots"]["openai"]["sponsor_id"], "7");
    }
}
