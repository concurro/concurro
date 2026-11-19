//! Docker-backed outbox and JetStream forwarding checks.

use std::time::Duration;

use async_nats::jetstream::{self, stream};
use concurro_contracts::{Contract, Envelope, EventId, EventKind, GithubIssues};
use concurro_db::Database;
use concurro_events_nats::{Forwarder, ensure_stream};
use concurro_events_postgres::{ForwarderLease, Outbox, migrate};
use concurro_testkit::{NatsContainer, PostgresContainer};
use serde::Serialize;

/// A test contract with the next major wire version of GitHub issues.
#[derive(Clone, Serialize)]
#[serde(transparent)]
struct GithubIssuesV2(GithubIssues);

impl Contract for GithubIssuesV2 {
    const FAMILY: &'static str = GithubIssues::FAMILY;
    const VERSION: u32 = 2;
    const KIND: EventKind = GithubIssues::KIND;

    fn event_type(&self) -> String {
        self.0.event_type()
    }

    fn subject(&self) -> String {
        self.0.subject()
    }
}

/// Replacing a stream creates a new journal generation even when its sequence restarts.
#[tokio::test]
async fn journal_position_tracks_stream_replacement() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let nats = NatsContainer::start().await.expect("NATS container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    let client = async_nats::connect(nats.url()).await.expect("NATS");
    let context = jetstream::new(client.clone());
    let config = stream::Config {
        name: "GITHUB".to_owned(),
        subjects: vec!["github.>".to_owned()],
        ..Default::default()
    };
    ensure_stream(&context, config.clone())
        .await
        .expect("stream");
    let raw = include_str!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let first: Envelope<GithubIssues> = serde_json::from_str(raw).expect("fixture");
    let mut second = first.clone();
    second.id = EventId::mint();
    let outbox = Outbox::new(1024 * 1024, vec!["github.>".to_owned()]);
    let mut lease = ForwarderLease::try_acquire(&database)
        .await
        .expect("lease")
        .expect("owner");
    let forwarder = Forwarder::new(client, Duration::from_millis(20)).with_journal_stream("GITHUB");
    let mut uow = database.begin().await.expect("begin");
    outbox.append(&mut uow, &first).await.expect("first append");
    uow.commit().await.expect("first commit");
    assert!(
        forwarder
            .forward_one(&mut lease)
            .await
            .expect("first publish")
    );
    let first_position: (i64, i64) = sqlx::query_as(
        "SELECT stream_generation, stream_sequence FROM events.outbox WHERE id = $1",
    )
    .bind(first.id.0)
    .fetch_one(database.pool())
    .await
    .expect("first position");
    assert_eq!(first_position.1, 1);
    context
        .delete_stream("GITHUB")
        .await
        .expect("delete stream");
    let mut uow = database.begin().await.expect("begin");
    outbox
        .append(&mut uow, &second)
        .await
        .expect("second append");
    uow.commit().await.expect("second commit");
    assert!(forwarder.forward_one(&mut lease).await.is_err());
    let pending: (i32, Option<String>, Option<bool>) = sqlx::query_as(
        "SELECT failure_count, last_error, retry_at > now() FROM events.outbox WHERE id = $1",
    )
    .bind(second.id.0)
    .fetch_one(database.pool())
    .await
    .expect("pending row after stream inspection failure");
    assert_eq!(pending, (0, None, None));
    ensure_stream(&context, config)
        .await
        .expect("replacement stream");
    assert!(
        forwarder
            .forward_one(&mut lease)
            .await
            .expect("second publish")
    );
    let second_position: (i64, i64) = sqlx::query_as(
        "SELECT stream_generation, stream_sequence FROM events.outbox WHERE id = $1",
    )
    .bind(second.id.0)
    .fetch_one(database.pool())
    .await
    .expect("second position");
    assert_eq!(second_position.1, 1);
    assert!(second_position.0 > first_position.0);
}

/// Forward the admitted bytes exactly and leave rolled-back rows unpublished.
#[tokio::test]
async fn outbox_to_jetstream() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let nats = NatsContainer::start().await.expect("NATS container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    let client = async_nats::connect(nats.url()).await.expect("NATS");
    let context = jetstream::new(client.clone());
    let config = stream::Config {
        name: "INGRESS".to_owned(),
        subjects: vec!["github.>".to_owned()],
        max_age: Duration::from_secs(60),
        duplicate_window: Duration::from_secs(3),
        num_replicas: 1,
        storage: stream::StorageType::File,
        ..Default::default()
    };
    ensure_stream(&context, config).await.expect("stream");
    let fixture = include_str!(
        "../../../contracts/fixtures/github.issues.v1/valid/labeled_unknown_open.json"
    );
    let event: Envelope<GithubIssues> = serde_json::from_str(fixture).expect("fixture");
    let outbox = Outbox::new(1024 * 1024, vec!["github.>".to_owned()]);
    let mut uow = database.begin().await.expect("transaction");
    outbox.append(&mut uow, &event).await.expect("append");
    uow.commit().await.expect("commit");
    let mut lease = ForwarderLease::try_acquire(&database)
        .await
        .expect("lease")
        .expect("sole forwarder");
    assert!(
        ForwarderLease::try_acquire(&database)
            .await
            .expect("second lease")
            .is_none()
    );
    let forwarder = Forwarder::new(client.clone(), Duration::from_millis(50));
    assert!(forwarder.forward_one(&mut lease).await.expect("forward"));
    assert!(!forwarder.forward_one(&mut lease).await.expect("idle"));
    let mut stream = context.get_stream("INGRESS").await.expect("stream");
    assert_eq!(stream.info().await.expect("stream info").state.messages, 1);
    let published = stream.get_raw_message(1).await.expect("published event");
    assert_eq!(published.subject.as_str(), event.subject());
    assert_eq!(
        published.payload.as_ref(),
        serde_json::to_vec(&event).expect("wire").as_slice()
    );
    assert_eq!(
        published
            .headers
            .get("Nats-Msg-Id")
            .expect("message id")
            .as_str(),
        format!("{}:{}", event.id.0, event.version)
    );
    let mut rolled_back = event.clone();
    rolled_back.id = EventId::mint();
    let mut uow = database.begin().await.expect("rollback transaction");
    outbox
        .append(&mut uow, &rolled_back)
        .await
        .expect("append rollback");
    uow.rollback().await.expect("rollback");
    assert!(
        !forwarder
            .forward_one(&mut lease)
            .await
            .expect("idle after rollback")
    );

    let misconfigured = Forwarder::new(client.clone(), Duration::from_millis(20));
    assert!(
        misconfigured
            .run(
                &database,
                &mut lease,
                Duration::ZERO,
                client.max_payload() + 1
            )
            .await
            .is_err()
    );
    let notified_forwarder = Forwarder::new(client, Duration::from_secs(30));
    let running = notified_forwarder.run(&database, &mut lease, Duration::ZERO, 1024 * 1024);
    tokio::pin!(running);
    tokio::select! {
        result = &mut running => panic!("forwarder stopped before notification: {result:?}"),
        () = tokio::time::sleep(Duration::from_millis(150)) => {}
    }
    let mut notified = event.clone();
    notified.id = EventId::mint();
    let mut uow = database.begin().await.expect("notified transaction");
    outbox
        .append(&mut uow, &notified)
        .await
        .expect("notified append");
    uow.commit().await.expect("notified commit");
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            let remaining: i64 = sqlx::query_scalar("SELECT count(*) FROM events.outbox")
                .fetch_one(database.pool())
                .await
                .expect("remaining rows");
            let messages = stream.info().await.expect("stream info").state.messages;
            if remaining == 0 && messages == 2 {
                break;
            }
            tokio::select! {
                result = &mut running => panic!("forwarder stopped after notification: {result:?}"),
                () = tokio::time::sleep(Duration::from_millis(20)) => {}
            }
        }
    })
    .await
    .expect("NOTIFY wakes forwarder before long polling interval");
}

/// A rejected row backs off without blocking later rows and publishes after repair.
#[tokio::test]
async fn rejected_row_keeps_its_history_and_does_not_block() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let nats = NatsContainer::start().await.expect("NATS container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    let client = async_nats::connect(nats.url()).await.expect("NATS");
    let context = jetstream::new(client.clone());
    ensure_stream(
        &context,
        stream::Config {
            name: "INGRESS".to_owned(),
            subjects: vec!["github.>".to_owned()],
            ..Default::default()
        },
    )
    .await
    .expect("stream");
    let fixture = include_str!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let failed: Envelope<GithubIssues> = serde_json::from_str(fixture).expect("fixture");
    let mut following = failed.clone();
    following.id = EventId::mint();
    let outbox = Outbox::new(1024 * 1024, vec!["github.>".to_owned()]);
    let mut uow = database.begin().await.expect("transaction");
    let failed_sequence = outbox.append(&mut uow, &failed).await.expect("failed row");
    outbox
        .append(&mut uow, &following)
        .await
        .expect("following row");
    uow.commit().await.expect("commit");
    sqlx::query("UPDATE events.outbox SET subject = 'unrouted.subject' WHERE sequence = $1")
        .bind(failed_sequence)
        .execute(database.pool())
        .await
        .expect("induce row rejection");
    let mut lease = ForwarderLease::try_acquire(&database)
        .await
        .expect("lease")
        .expect("owner");
    let forwarder = Forwarder::new(client, Duration::from_millis(20));
    assert!(
        forwarder
            .forward_one(&mut lease)
            .await
            .expect("first rejection")
    );
    let first: (i32, Option<String>, bool) = sqlx::query_as(
        "SELECT failure_count, last_error, retry_at > now() FROM events.outbox WHERE sequence = $1",
    )
    .bind(failed_sequence)
    .fetch_one(database.pool())
    .await
    .expect("first failure state");
    assert_eq!(first.0, 1);
    assert!(first.1.is_some());
    assert!(first.2);
    assert!(
        forwarder
            .forward_one(&mut lease)
            .await
            .expect("following row")
    );
    let mut stream = context.get_stream("INGRESS").await.expect("stream");
    assert_eq!(stream.info().await.expect("info").state.messages, 1);
    tokio::time::sleep(Duration::from_millis(1100)).await;
    assert!(
        forwarder
            .forward_one(&mut lease)
            .await
            .expect("second rejection")
    );
    let second: i32 =
        sqlx::query_scalar("SELECT failure_count FROM events.outbox WHERE sequence = $1")
            .bind(failed_sequence)
            .fetch_one(database.pool())
            .await
            .expect("second failure state");
    assert_eq!(second, 2);
    sqlx::query("UPDATE events.outbox SET subject = $2 WHERE sequence = $1")
        .bind(failed_sequence)
        .bind(failed.subject())
        .execute(database.pool())
        .await
        .expect("repair row");
    tokio::time::sleep(Duration::from_millis(2100)).await;
    assert!(
        forwarder
            .forward_one(&mut lease)
            .await
            .expect("repaired publish")
    );
    assert_eq!(stream.info().await.expect("info").state.messages, 2);
    let restored: (i32, Option<String>, bool) = sqlx::query_as(
        "SELECT failure_count, last_error, published_at IS NOT NULL FROM events.outbox WHERE sequence = $1"
    ).bind(failed_sequence).fetch_one(database.pool()).await.expect("restored row state");
    assert_eq!(restored, (0, None, true));
}

/// Two major versions with one event ID survive JetStream's duplicate window.
#[tokio::test]
async fn versions_of_one_event_publish_as_distinct_messages() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let nats = NatsContainer::start().await.expect("NATS container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    let client = async_nats::connect(nats.url()).await.expect("NATS");
    let context = jetstream::new(client.clone());
    ensure_stream(
        &context,
        stream::Config {
            name: "INGRESS".to_owned(),
            subjects: vec!["github.>".to_owned()],
            duplicate_window: Duration::from_secs(30),
            ..Default::default()
        },
    )
    .await
    .expect("stream");
    let raw = include_str!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let first: Envelope<GithubIssues> = serde_json::from_str(raw).expect("fixture");
    let second = Envelope::fact(
        first.id,
        first.producer.clone(),
        first.occurred_at,
        GithubIssuesV2(first.data.clone()),
    );
    let outbox = Outbox::new(1024 * 1024, vec!["github.>".to_owned()]);
    let mut uow = database.begin().await.expect("producer transaction");
    outbox.append(&mut uow, &first).await.expect("version one");
    outbox.append(&mut uow, &second).await.expect("version two");
    uow.commit().await.expect("commit both versions");
    let mut lease = ForwarderLease::try_acquire(&database)
        .await
        .expect("lease")
        .expect("owner");
    let forwarder = Forwarder::new(client, Duration::from_millis(20));
    assert!(
        forwarder
            .forward_one(&mut lease)
            .await
            .expect("first publish")
    );
    assert!(
        forwarder
            .forward_one(&mut lease)
            .await
            .expect("second publish")
    );
    let mut stream = context.get_stream("INGRESS").await.expect("stream");
    assert_eq!(stream.info().await.expect("info").state.messages, 2);
    for (sequence, version) in [(1, 1), (2, 2)] {
        let message = stream
            .get_raw_message(sequence)
            .await
            .expect("versioned message");
        assert_eq!(
            message
                .headers
                .get("Nats-Msg-Id")
                .expect("message ID")
                .as_str(),
            format!("{}:{version}", first.id.0)
        );
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&message.payload).expect("wire JSON")["version"],
            version
        );
    }
}

/// A stop after publish leaves the row for idempotent recovery.
#[cfg(feature = "fault-injection")]
#[tokio::test]
async fn stopped_after_publish_republishes_with_same_id() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let nats = NatsContainer::start().await.expect("NATS container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    let client = async_nats::connect(nats.url()).await.expect("NATS");
    let context = jetstream::new(client.clone());
    ensure_stream(
        &context,
        stream::Config {
            name: "INGRESS".to_owned(),
            subjects: vec!["github.>".to_owned()],
            duplicate_window: Duration::from_secs(1),
            max_age: Duration::from_secs(2),
            ..Default::default()
        },
    )
    .await
    .expect("stream");
    let fixture = include_str!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let event: Envelope<GithubIssues> = serde_json::from_str(fixture).expect("fixture");
    let outbox = Outbox::new(1024 * 1024, vec!["github.>".to_owned()]);
    let mut uow = database.begin().await.expect("transaction");
    outbox.append(&mut uow, &event).await.expect("append");
    uow.commit().await.expect("commit");
    let mut lease = ForwarderLease::try_acquire(&database)
        .await
        .expect("lease")
        .expect("sole forwarder");
    let broken =
        Forwarder::new(client.clone(), Duration::from_millis(50)).with_after_publish_fault();
    assert!(broken.forward_one(&mut lease).await.is_err());
    let pending: i64 =
        sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE published_at IS NULL")
            .fetch_one(database.pool())
            .await
            .expect("pending row");
    assert_eq!(pending, 1);
    let recovered = Forwarder::new(client, Duration::from_millis(50));
    assert!(recovered.forward_one(&mut lease).await.expect("recovery"));
    let mut stream = context.get_stream("INGRESS").await.expect("stream");
    assert_eq!(stream.info().await.expect("stream info").state.messages, 1);

    let mut after_window = event.clone();
    after_window.id = EventId::mint();
    let mut uow = database.begin().await.expect("second transaction");
    outbox
        .append(&mut uow, &after_window)
        .await
        .expect("second append");
    uow.commit().await.expect("second commit");
    assert!(broken.forward_one(&mut lease).await.is_err());
    tokio::time::sleep(Duration::from_millis(1200)).await;
    assert!(
        recovered
            .forward_one(&mut lease)
            .await
            .expect("outside window")
    );
    assert_eq!(
        stream
            .info()
            .await
            .expect("stream info")
            .state
            .last_sequence,
        3
    );

    let mut after_expiry = event.clone();
    after_expiry.id = EventId::mint();
    let mut uow = database.begin().await.expect("third transaction");
    outbox
        .append(&mut uow, &after_expiry)
        .await
        .expect("third append");
    uow.commit().await.expect("third commit");
    assert!(broken.forward_one(&mut lease).await.is_err());
    let original_sequence = stream
        .info()
        .await
        .expect("stream info")
        .state
        .last_sequence;
    tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            if stream.get_raw_message(original_sequence).await.is_err() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    })
    .await
    .expect("original message expires after max age");
    assert!(
        recovered
            .forward_one(&mut lease)
            .await
            .expect("after max age")
    );
    assert_eq!(
        stream
            .info()
            .await
            .expect("stream info")
            .state
            .last_sequence,
        original_sequence + 1
    );
    assert!(
        lease
            .oldest_unpublished()
            .await
            .expect("empty backlog")
            .is_none()
    );
    let mut delayed = event.clone();
    delayed.id = EventId::mint();
    let mut uow = database.begin().await.expect("delayed transaction");
    let delayed_sequence = outbox
        .append(&mut uow, &delayed)
        .await
        .expect("delayed append");
    uow.commit().await.expect("delayed commit");
    lease
        .record_failure(delayed_sequence, "injected publish failure", 1)
        .await
        .expect("failure record");
    let status = lease
        .oldest_unpublished()
        .await
        .expect("backlog status")
        .expect("pending row");
    assert!(status.age_seconds >= 0.0);
    assert_eq!(
        status.last_error.as_deref(),
        Some("injected publish failure")
    );
    assert_eq!(lease.purge_published(0).await.expect("purge"), 3);
}
