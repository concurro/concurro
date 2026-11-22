//! S3 producer/subscription wrappers against disposable NATS and PostgreSQL.

use std::time::Duration;

use async_nats::jetstream::stream;
use concurro_contracts::{Envelope, GithubIssues};
use concurro_db::{Database, UnitOfWork};
use concurro_events::{Handler, Outcome};
use concurro_events_nats::{Producer, Subscription, run_producer, run_subscription};
use concurro_events_postgres::{Outbox, migrate};
use concurro_testkit::{NatsContainer, PostgresContainer, TestState};

/// Record one durable consumer effect through the shared unit of work.
struct Applied;

impl Handler<GithubIssues> for Applied {
    async fn handle(&self, work: &mut UnitOfWork<'_>, _event: Envelope<GithubIssues>) -> Outcome {
        match TestState::insert(work, 7).await {
            Ok(()) => Outcome::Done,
            Err(error) => Outcome::Retry(error.to_string()),
        }
    }
}

/// The S3 lifecycle helpers move a committed event through real JetStream once.
#[tokio::test]
async fn producer_and_subscription_restartable_paths_deliver() {
    let postgres = PostgresContainer::start().await.expect("PostgreSQL");
    let nats = NatsContainer::start().await.expect("NATS");
    let database = Database::connect(postgres.url(), 4)
        .await
        .expect("database");
    migrate(&database).await.expect("event schema");
    TestState::migrate(&database).await.expect("effect table");
    let raw = include_str!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let event: Envelope<GithubIssues> = serde_json::from_str(raw).expect("fixture");
    let outbox = Outbox::new(1_048_576, vec!["github.>".to_owned()]);
    let mut work = database.begin().await.expect("event work");
    outbox.append(&mut work, &event).await.expect("append");
    work.commit().await.expect("commit");
    let producer = tokio::spawn(run_producer(
        Producer {
            nats_url: nats.url().to_owned(),
            credentials_file: None,
            stream: stream::Config {
                name: "INGRESS".to_owned(),
                subjects: vec!["github.>".to_owned()],
                ..Default::default()
            },
            poll_interval: Duration::from_millis(20),
            published_retention: Duration::from_secs(3600),
            max_message_bytes: 1_048_576,
        },
        database.clone(),
    ));
    let subscriber = tokio::spawn(run_subscription::<GithubIssues, _>(
        Subscription {
            nats_url: nats.url().to_owned(),
            credentials_file: None,
            stream: "INGRESS".to_owned(),
            consumer: "s3-lifecycle".to_owned(),
            subject: "github.>".to_owned(),
            max_attempts: 3,
            held_poll_interval: Duration::from_millis(20),
        },
        database.clone(),
        Applied,
    ));
    tokio::time::timeout(Duration::from_secs(15), async {
        loop {
            let count: i64 = sqlx::query_scalar("SELECT count(*) FROM test_state WHERE id=7")
                .fetch_one(database.pool())
                .await
                .expect("effect count");
            if count == 1 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(30)).await;
        }
    })
    .await
    .expect("producer and subscriber delivery");
    let published: bool =
        sqlx::query_scalar("SELECT published_at IS NOT NULL FROM events.outbox WHERE id=$1")
            .bind(event.id.0)
            .fetch_one(database.pool())
            .await
            .expect("published row");
    assert!(published);
    subscriber.abort();
    producer.abort();
}
