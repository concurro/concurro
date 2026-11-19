//! JetStream delivery and Postgres deduplication across consumer restarts.

use std::{path::Path, process::Command, time::Duration};

use async_nats::{
    HeaderMap,
    jetstream::{self, stream},
};
use concurro_contracts::{Envelope, EventId, GithubIssues};
use concurro_db::{Database, UnitOfWork};
#[cfg(feature = "fault-injection")]
use concurro_events::ProcessResult;
use concurro_events::{Handler, Outcome};
#[cfg(feature = "fault-injection")]
use concurro_events_nats::{ConsumerFault, run_consumer_with_fault};
use concurro_events_nats::{
    ConsumerStream, Forwarder, ensure_pull_consumer, ensure_stream, run_consumer, stream_identity,
};
use concurro_events_postgres::{ForwarderLease, Outbox, migrate};
#[cfg(feature = "fault-injection")]
use concurro_events_postgres::{process_released, release_held};
use concurro_testkit::{NatsContainer, PostgresContainer, TestState};

/// Write one producer event's domain effect through the consumer transaction.
struct StateHandler;

impl Handler<GithubIssues> for StateHandler {
    async fn handle(&self, uow: &mut UnitOfWork<'_>, _event: Envelope<GithubIssues>) -> Outcome {
        TestState::insert(uow, 1).await.expect("consumer effect");
        Outcome::Done
    }
}

/// Store each event's identity as the consumer effect.
struct EventIdHandler;

impl Handler<GithubIssues> for EventIdHandler {
    async fn handle(&self, uow: &mut UnitOfWork<'_>, event: Envelope<GithubIssues>) -> Outcome {
        sqlx::query("INSERT INTO applied_ids (id) VALUES ($1)")
            .bind(event.id.0)
            .execute(uow.connection())
            .await
            .expect("consumer identity insert");
        Outcome::Done
    }
}

/// Run the Python consumer against the same disposable JetStream stream.
fn run_python_probe(
    nats_url: &str,
    sqlite_path: &Path,
    deliveries: usize,
    stop_before_ack: bool,
    mode: Option<&str>,
) -> Option<serde_json::Value> {
    let python_root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../contracts/python");
    let local_python = python_root.join(".venv/bin/python");
    let python = if local_python.exists() {
        local_python.as_os_str()
    } else {
        std::ffi::OsStr::new("python3")
    };
    let mut command = Command::new(python);
    command
        .arg(python_root.join("consumer/transport_probe.py"))
        .arg(nats_url)
        .arg(sqlite_path)
        .arg(deliveries.to_string())
        .env("PYTHONPATH", &python_root);
    if stop_before_ack {
        command.arg("stop-before-ack");
    }
    if let Some(mode) = mode {
        command.env("CONCURRO_PROBE_MODE", mode);
    }
    let output = command.output().expect("Python consumer process");
    if stop_before_ack {
        assert_eq!(output.status.code(), Some(17));
        return None;
    }
    assert!(
        output.status.success(),
        "Python consumer failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    Some(serde_json::from_slice(&output.stdout).expect("Python result"))
}

/// Return one configured decision for acknowledgement fault tests.
#[cfg(feature = "fault-injection")]
struct DecisionHandler(Outcome);

#[cfg(feature = "fault-injection")]
impl Handler<GithubIssues> for DecisionHandler {
    async fn handle(&self, _uow: &mut UnitOfWork<'_>, _event: Envelope<GithubIssues>) -> Outcome {
        self.0.clone()
    }
}

/// A duplicate outside the JetStream window is acknowledged without a second effect.
#[tokio::test]
async fn transport_consumer_applies_once() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let nats = NatsContainer::start().await.expect("NATS container");
    let database = Database::connect(postgres.url(), 4)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    TestState::migrate(&database).await.expect("state schema");
    let client = async_nats::connect(nats.url()).await.expect("NATS");
    let context = jetstream::new(client.clone());
    ensure_stream(
        &context,
        stream::Config {
            name: "INGRESS".to_owned(),
            subjects: vec!["github.>".to_owned()],
            duplicate_window: Duration::from_secs(1),
            max_age: Duration::from_secs(8),
            ..Default::default()
        },
    )
    .await
    .expect("stream");
    let consumer = ensure_pull_consumer(
        &context,
        "INGRESS",
        "rust-test",
        "github.>",
        Duration::from_millis(500),
        Duration::from_millis(20),
    )
    .await
    .expect("pull consumer");
    let mut reconciled = ensure_pull_consumer(
        &context,
        "INGRESS",
        "rust-test",
        "github.>",
        Duration::from_millis(700),
        Duration::from_millis(20),
    )
    .await
    .expect("reconcile pull consumer");
    assert_eq!(
        reconciled
            .info()
            .await
            .expect("updated consumer")
            .config
            .ack_wait,
        Duration::from_millis(700)
    );
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let event: Envelope<GithubIssues> = serde_json::from_slice(raw).expect("fixture");
    let outbox = Outbox::new(1024 * 1024, vec!["github.>".to_owned()]);
    let mut uow = database.begin().await.expect("producer transaction");
    outbox
        .append(&mut uow, &event)
        .await
        .expect("outbox append");
    uow.commit().await.expect("producer commit");
    let mut lease = ForwarderLease::try_acquire(&database)
        .await
        .expect("lease")
        .expect("sole forwarder");
    Forwarder::new(client, Duration::from_millis(20))
        .forward_one(&mut lease)
        .await
        .expect("publish");
    #[cfg(feature = "fault-injection")]
    {
        let stopped = run_consumer_with_fault::<GithubIssues, _>(
            ConsumerStream {
                pull: &consumer,
                context: &context,
                name: "INGRESS",
            },
            &database,
            "rust-test",
            3,
            Duration::from_millis(20),
            &StateHandler,
            ConsumerFault::AfterCommitBeforeAck,
        )
        .await;
        assert!(stopped.is_err());
        let state: i64 = sqlx::query_scalar("SELECT count(*) FROM test_state")
            .fetch_one(database.pool())
            .await
            .expect("committed effect before ack");
        assert_eq!(state, 1);
    }

    for expected_ack in 1..=2 {
        if expected_ack == 2 {
            tokio::time::sleep(Duration::from_millis(1200)).await;
            let mut headers = HeaderMap::new();
            headers.insert("Nats-Msg-Id", format!("{}:{}", event.id.0, event.version));
            context
                .publish_with_headers(event.subject(), headers, raw.to_vec().into())
                .await
                .expect("duplicate publish")
                .await
                .expect("duplicate ack");
        }
        let mut observer = consumer.clone();
        tokio::time::timeout(Duration::from_secs(10), async {
            tokio::select! {
                result = run_consumer::<GithubIssues, _>(
                    ConsumerStream { pull: &consumer, context: &context, name: "INGRESS" }, &database, "rust-test", 3, Duration::from_millis(20),
                    &StateHandler
                ) => panic!("consumer ended: {result:?}"),
                () = async {
                    loop {
                        if observer.info().await.expect("consumer info").ack_floor.stream_sequence >= expected_ack {
                            break;
                        }
                        tokio::time::sleep(Duration::from_millis(20)).await;
                    }
                } => {}
            }
        }).await.expect("consumer progress");
    }
    let state: i64 = sqlx::query_scalar("SELECT count(*) FROM test_state")
        .fetch_one(database.pool())
        .await
        .expect("state count");
    assert_eq!(state, 1);
    let consumed: i64 = sqlx::query_scalar("SELECT count(*) FROM events.consumed")
        .fetch_one(database.pool())
        .await
        .expect("consumed count");
    assert_eq!(consumed, 1);
    let mut stream = context.get_stream("INGRESS").await.expect("stream");
    assert_eq!(stream.info().await.expect("stream info").state.messages, 2);

    let sqlite_path =
        std::env::temp_dir().join(format!("concurro-python-{}.sqlite", EventId::mint().0));
    assert!(run_python_probe(nats.url(), &sqlite_path, 1, true, None).is_none());
    tokio::time::sleep(Duration::from_millis(600)).await;
    for _ in 0..2 {
        let result =
            run_python_probe(nats.url(), &sqlite_path, 1, false, None).expect("Python result");
        assert_eq!(result["action"], "ack");
        assert_eq!(result["count"], 1);
        assert_eq!(
            result["event"],
            serde_json::to_value(&event).expect("Rust event value")
        );
    }
    tokio::time::timeout(Duration::from_secs(12), async {
        loop {
            if stream.get_raw_message(2).await.is_err() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    })
    .await
    .expect("original stream copy expires");
    let mut headers = HeaderMap::new();
    headers.insert("Nats-Msg-Id", format!("{}:{}", event.id.0, event.version));
    context
        .publish_with_headers(event.subject(), headers, raw.to_vec().into())
        .await
        .expect("publish after max age")
        .await
        .expect("publish acknowledgement");
    let mut observer = consumer.clone();
    tokio::time::timeout(Duration::from_secs(10), async {
        tokio::select! {
            result = run_consumer::<GithubIssues, _>(
                ConsumerStream { pull: &consumer, context: &context, name: "INGRESS" }, &database, "rust-test", 3, Duration::from_millis(20),
                &StateHandler
            ) => panic!("consumer ended: {result:?}"),
            () = async {
                loop {
                    if observer.info().await.expect("consumer info").ack_floor.stream_sequence >= 3 {
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(20)).await;
                }
            } => {}
        }
    })
    .await
    .expect("copy after max age acknowledged");
    let result = run_python_probe(nats.url(), &sqlite_path, 1, false, None).expect("Python result");
    assert_eq!(result["count"], 1);
    let state: i64 = sqlx::query_scalar("SELECT count(*) FROM test_state")
        .fetch_one(database.pool())
        .await
        .expect("state after max age");
    assert_eq!(state, 1);
    std::fs::remove_file(&sqlite_path).expect("remove test SQLite file");
    std::fs::remove_file(sqlite_path.with_extension("sqlite.lock")).expect("remove SQLite lock");

    #[cfg(feature = "fault-injection")]
    {
        let mut held_event = event.clone();
        held_event.id = EventId::mint();
        let mut headers = HeaderMap::new();
        headers.insert(
            "Nats-Msg-Id",
            format!("{}:{}", held_event.id.0, held_event.version),
        );
        context
            .publish_with_headers(
                held_event.subject(),
                headers,
                serde_json::to_vec(&held_event).expect("held wire").into(),
            )
            .await
            .expect("held publish")
            .await
            .expect("held publish ack");
        let holding = DecisionHandler(Outcome::Hold("ready".to_owned()));
        assert!(
            run_consumer_with_fault::<GithubIssues, _>(
                ConsumerStream {
                    pull: &consumer,
                    context: &context,
                    name: "INGRESS"
                },
                &database,
                "rust-test",
                3,
                Duration::from_millis(20),
                &holding,
                ConsumerFault::AfterHoldBeforeAck,
            )
            .await
            .is_err()
        );
        let held: i64 = sqlx::query_scalar("SELECT count(*) FROM events.held")
            .fetch_one(database.pool())
            .await
            .expect("held count");
        assert_eq!(held, 1);
        tokio::time::sleep(Duration::from_millis(550)).await;
        let mut observer = consumer.clone();
        tokio::time::timeout(Duration::from_secs(10), async {
            tokio::select! {
                result = run_consumer::<GithubIssues, _>(
                    ConsumerStream { pull: &consumer, context: &context, name: "INGRESS" }, &database, "rust-test", 3,
                    Duration::from_millis(20), &holding
                ) => panic!("consumer ended: {result:?}"),
                () = async {
                    loop {
                        if observer.info().await.expect("consumer info").ack_floor.stream_sequence >= 4 {
                            break;
                        }
                        tokio::time::sleep(Duration::from_millis(20)).await;
                    }
                } => {}
            }
        })
        .await
        .expect("held redelivery acknowledged");
        let mut uow = database.begin().await.expect("release transaction");
        assert_eq!(
            release_held(&mut uow, "rust-test", "ready")
                .await
                .expect("release"),
            1
        );
        uow.commit().await.expect("release commit");
        assert_eq!(
            process_released::<GithubIssues, _>(
                &database,
                "rust-test",
                3,
                &DecisionHandler(Outcome::Done)
            )
            .await
            .expect("released event"),
            Some(ProcessResult::Done)
        );

        let mut rejected = event.clone();
        rejected.id = EventId::mint();
        let mut headers = HeaderMap::new();
        headers.insert(
            "Nats-Msg-Id",
            format!("{}:{}", rejected.id.0, rejected.version),
        );
        context
            .publish_with_headers(
                rejected.subject(),
                headers,
                serde_json::to_vec(&rejected).expect("rejected wire").into(),
            )
            .await
            .expect("rejected publish")
            .await
            .expect("rejected publish ack");
        let rejecting = DecisionHandler(Outcome::Reject("operator review".to_owned()));
        assert!(
            run_consumer_with_fault::<GithubIssues, _>(
                ConsumerStream {
                    pull: &consumer,
                    context: &context,
                    name: "INGRESS"
                },
                &database,
                "rust-test",
                3,
                Duration::from_millis(20),
                &rejecting,
                ConsumerFault::AfterParkBeforeTerm,
            )
            .await
            .is_err()
        );
        let parked: i64 = sqlx::query_scalar("SELECT count(*) FROM events.parked")
            .fetch_one(database.pool())
            .await
            .expect("parked count");
        assert_eq!(parked, 1);
        tokio::time::sleep(Duration::from_millis(550)).await;
        let mut observer = consumer.clone();
        tokio::time::timeout(Duration::from_secs(10), async {
            tokio::select! {
                result = run_consumer::<GithubIssues, _>(
                    ConsumerStream { pull: &consumer, context: &context, name: "INGRESS" }, &database, "rust-test", 3,
                    Duration::from_millis(20), &rejecting
                ) => panic!("consumer ended: {result:?}"),
                () = async {
                    loop {
                        if observer.info().await.expect("consumer info").ack_floor.stream_sequence >= 5 {
                            break;
                        }
                        tokio::time::sleep(Duration::from_millis(20)).await;
                    }
                } => {}
            }
        })
        .await
        .expect("parked redelivery acknowledged");
        let parked: i64 = sqlx::query_scalar("SELECT count(*) FROM events.parked")
            .fetch_one(database.pool())
            .await
            .expect("parked count");
        assert_eq!(parked, 1);
    }
}

/// Concurrent commits all reach the pull consumer and apply exactly once.
#[tokio::test]
async fn thousand_concurrent_events_apply_once() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let nats = NatsContainer::start().await.expect("NATS container");
    let database = Database::connect(postgres.url(), 24)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    sqlx::query("CREATE TABLE applied_ids (id uuid PRIMARY KEY)")
        .execute(database.pool())
        .await
        .expect("state table");
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
    let consumer = ensure_pull_consumer(
        &context,
        "INGRESS",
        "rust-volume",
        "github.>",
        Duration::from_secs(30),
        Duration::from_millis(20),
    )
    .await
    .expect("pull consumer");
    let fixture = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let event: Envelope<GithubIssues> = serde_json::from_slice(fixture).expect("fixture");
    let outbox = Outbox::new(1024 * 1024, vec!["github.>".to_owned()]);
    let mut producers = tokio::task::JoinSet::new();
    for _ in 0..1000 {
        let database = database.clone();
        let outbox = outbox.clone();
        let mut event = event.clone();
        event.id = EventId::mint();
        producers.spawn(async move {
            let mut uow = database.begin().await.expect("producer transaction");
            outbox.append(&mut uow, &event).await.expect("append");
            uow.commit().await.expect("producer commit");
        });
    }
    while let Some(result) = producers.join_next().await {
        result.expect("producer task");
    }
    let mut lease = ForwarderLease::try_acquire(&database)
        .await
        .expect("lease")
        .expect("sole forwarder");
    let forwarder = Forwarder::new(client, Duration::from_millis(20));
    for _ in 0..1000 {
        assert!(forwarder.forward_one(&mut lease).await.expect("forward"));
    }
    assert!(!forwarder.forward_one(&mut lease).await.expect("idle"));
    let mut observer = consumer.clone();
    tokio::time::timeout(Duration::from_secs(120), async {
        tokio::select! {
            result = run_consumer::<GithubIssues, _>(
                ConsumerStream { pull: &consumer, context: &context, name: "INGRESS" }, &database, "rust-volume", 3, Duration::from_millis(100),
                &EventIdHandler
            ) => panic!("consumer ended: {result:?}"),
            () = async {
                loop {
                    if observer.info().await.expect("consumer info").ack_floor.stream_sequence >= 1000 {
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(100)).await;
                }
            } => {}
        }
    })
    .await
    .expect("all events consumed");
    let effects: i64 = sqlx::query_scalar("SELECT count(*) FROM applied_ids")
        .fetch_one(database.pool())
        .await
        .expect("effect count");
    assert_eq!(effects, 1000);
    let sqlite_path = std::env::temp_dir().join(format!(
        "concurro-python-volume-{}.sqlite",
        EventId::mint().0
    ));
    let result = run_python_probe(nats.url(), &sqlite_path, 1000, false, Some("volume"))
        .expect("Python result");
    eprintln!(
        "Python volume probe: {} deliveries for {} effects",
        result["deliveries"], result["count"]
    );
    assert_eq!(result["action"], "ack");
    assert_eq!(result["count"], 1000, "Python probe: {result}");
    assert_eq!(result["parked"], 0, "Python probe: {result}");
    assert_eq!(result["event"]["data"]["issue"]["number"], 4294967301_u64);
    std::fs::remove_file(&sqlite_path).expect("remove test SQLite file");
    std::fs::remove_file(sqlite_path.with_extension("sqlite.lock")).expect("remove SQLite lock");
}

/// Both consumers apply v1 and skip v2 with the same event ID without parking.
#[tokio::test]
async fn versioned_messages_reach_both_consumers() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let nats = NatsContainer::start().await.expect("NATS container");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    TestState::migrate(&database).await.expect("state schema");
    let client = async_nats::connect(nats.url()).await.expect("NATS");
    let context = jetstream::new(client);
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
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let event: Envelope<GithubIssues> = serde_json::from_slice(raw).expect("v1 fixture");
    let mut v2: serde_json::Value = serde_json::from_slice(raw).expect("v2 value");
    v2["version"] = serde_json::json!(2);
    for (version, wire) in [
        (1, raw.to_vec()),
        (2, serde_json::to_vec(&v2).expect("v2 wire")),
    ] {
        let mut headers = HeaderMap::new();
        headers.insert("Nats-Msg-Id", format!("{}:{version}", event.id.0));
        context
            .publish_with_headers("github.issues.opened", headers, wire.into())
            .await
            .expect("publish version")
            .await
            .expect("publish ack");
    }
    let consumer = ensure_pull_consumer(
        &context,
        "INGRESS",
        "rust-versions",
        "github.>",
        Duration::from_millis(500),
        Duration::from_millis(20),
    )
    .await
    .expect("Rust consumer");
    let mut observer = consumer.clone();
    tokio::time::timeout(Duration::from_secs(15), async {
        tokio::select! {
            result = run_consumer::<GithubIssues, _>(
                ConsumerStream { pull: &consumer, context: &context, name: "INGRESS" }, &database, "rust-versions", 1, Duration::from_millis(20),
                &StateHandler
            ) => panic!("Rust consumer ended: {result:?}"),
            () = async {
                loop {
                    if observer.info().await.expect("consumer info").ack_floor.stream_sequence >= 2 {
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(20)).await;
                }
            } => {}
        }
    })
    .await
    .expect("Rust consumed both versions");
    let effects: i64 = sqlx::query_scalar("SELECT count(*) FROM test_state")
        .fetch_one(database.pool())
        .await
        .expect("Rust effects");
    let parked: i64 = sqlx::query_scalar("SELECT count(*) FROM events.parked")
        .fetch_one(database.pool())
        .await
        .expect("Rust parked");
    assert_eq!(effects, 1);
    assert_eq!(parked, 0);
    let sqlite_path = std::env::temp_dir().join(format!(
        "concurro-python-versions-{}.sqlite",
        EventId::mint().0
    ));
    let result = run_python_probe(nats.url(), &sqlite_path, 2, false, None)
        .expect("Python consumed both versions");
    assert_eq!(result["action"], "ack");
    assert_eq!(result["count"], 1);
    assert_eq!(result["parked"], 0);
    assert_eq!(result["event"]["version"], 1);
    std::fs::remove_file(&sqlite_path).expect("remove test SQLite file");
    std::fs::remove_file(sqlite_path.with_extension("sqlite.lock")).expect("remove SQLite lock");
}

/// Headerless malformed JetStream deliveries keep one identity in both consumers.
#[tokio::test]
async fn malformed_without_header_parks_both_consumers_once() {
    let nats = NatsContainer::start().await.expect("NATS container");
    let client = async_nats::connect(nats.url()).await.expect("NATS");
    let context = jetstream::new(client);
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
    context
        .publish("github.issues.opened", b"{invalid".to_vec().into())
        .await
        .expect("malformed publish")
        .await
        .expect("malformed publish ack");
    let sqlite_path = std::env::temp_dir().join(format!(
        "concurro-python-malformed-{}.sqlite",
        EventId::mint().0
    ));
    for attempt in 1..=4 {
        let result =
            run_python_probe(nats.url(), &sqlite_path, 1, false, None).expect("Python result");
        assert_eq!(result["action"], if attempt == 4 { "term" } else { "nak" });
        assert_eq!(result["parked"], if attempt == 4 { 1 } else { 0 });
    }
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    let consumer = ensure_pull_consumer(
        &context,
        "INGRESS",
        "rust-malformed",
        "github.>",
        Duration::from_millis(500),
        Duration::from_millis(20),
    )
    .await
    .expect("Rust consumer");
    tokio::time::timeout(Duration::from_secs(15), async {
        tokio::select! {
            result = run_consumer::<GithubIssues, _>(
                ConsumerStream { pull: &consumer, context: &context, name: "INGRESS" }, &database, "rust-malformed", 1, Duration::from_millis(20),
                &StateHandler
            ) => panic!("Rust consumer ended: {result:?}"),
            () = async {
                loop {
                    let parked: i64 = sqlx::query_scalar("SELECT count(*) FROM events.parked")
                        .fetch_one(database.pool()).await.expect("Rust parked count");
                    if parked == 1 { break; }
                    tokio::time::sleep(Duration::from_millis(20)).await;
                }
            } => {}
        }
    })
    .await
    .expect("Rust malformed delivery parks");
    let (fallback_key, attempts, wire_bytes): (String, i32, Vec<u8>) = sqlx::query_as(
        "SELECT a.fallback_key, t.count, p.wire_bytes \
         FROM events.identity_aliases a \
         JOIN events.attempts t USING (consumer, event_id) \
         JOIN events.parked p USING (consumer, event_id)",
    )
    .fetch_one(database.pool())
    .await
    .expect("stable Rust fallback");
    let (created, _) = stream_identity(&context, "INGRESS")
        .await
        .expect("stream identity");
    assert_eq!(fallback_key, format!("INGRESS:{created}:1"));
    assert_eq!(attempts, 2);
    assert_eq!(wire_bytes, b"{invalid");
    std::fs::remove_file(&sqlite_path).expect("remove test SQLite file");
    std::fs::remove_file(sqlite_path.with_extension("sqlite.lock")).expect("remove SQLite lock");
}

/// Python retries, parks, holds, and releases real JetStream deliveries.
#[tokio::test]
async fn python_transport_failure_matrix() {
    let nats = NatsContainer::start().await.expect("NATS container");
    let client = async_nats::connect(nats.url()).await.expect("NATS");
    let context = jetstream::new(client);
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
    let sqlite_path = std::env::temp_dir().join(format!(
        "concurro-python-matrix-{}.sqlite",
        EventId::mint().0
    ));
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    for (mode, deliveries, final_action, parked, held) in [
        ("retry", 4, "term", 1, 0),
        ("reject", 1, "term", 2, 0),
        ("hold", 1, "ack", 2, 1),
    ] {
        let mut event: serde_json::Value = serde_json::from_slice(raw).expect("fixture");
        let id = EventId::mint().0;
        event["id"] = serde_json::json!(id.to_string());
        let mut headers = HeaderMap::new();
        headers.insert("Nats-Msg-Id", format!("{id}:1"));
        context
            .publish_with_headers(
                "github.issues.opened",
                headers,
                serde_json::to_vec(&event).expect("wire").into(),
            )
            .await
            .expect("publish")
            .await
            .expect("publish ack");
        for attempt in 1..=deliveries {
            let result = run_python_probe(nats.url(), &sqlite_path, 1, false, Some(mode))
                .expect("Python consumer result");
            let expected = if mode == "retry" && attempt < deliveries {
                "nak"
            } else {
                final_action
            };
            assert_eq!(result["action"], expected, "mode {mode} attempt {attempt}");
            if attempt == deliveries {
                assert_eq!(result["parked"], parked, "mode {mode}");
                assert_eq!(result["held"], held, "mode {mode}");
            }
        }
    }
    let released = run_python_probe(nats.url(), &sqlite_path, 0, false, Some("release"))
        .expect("released Python row");
    assert_eq!(released["action"], "ack");
    assert_eq!(released["count"], 1);
    assert_eq!(released["held"], 0);
    assert_eq!(released["parked"], 2);
    std::fs::remove_file(&sqlite_path).expect("remove test SQLite file");
    std::fs::remove_file(sqlite_path.with_extension("sqlite.lock")).expect("remove SQLite lock");
}

/// Rust transport retries a failing handler and parks after its durable budget.
#[cfg(feature = "fault-injection")]
#[tokio::test]
async fn rust_transport_retry_parks_at_limit() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let nats = NatsContainer::start().await.expect("NATS container");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    let client = async_nats::connect(nats.url()).await.expect("NATS");
    let context = jetstream::new(client);
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
    let consumer = ensure_pull_consumer(
        &context,
        "INGRESS",
        "rust-retry",
        "github.>",
        Duration::from_millis(500),
        Duration::from_millis(20),
    )
    .await
    .expect("consumer");
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let event: Envelope<GithubIssues> = serde_json::from_slice(raw).expect("fixture");
    let mut headers = HeaderMap::new();
    headers.insert("Nats-Msg-Id", format!("{}:1", event.id.0));
    context
        .publish_with_headers(event.subject(), headers, raw.to_vec().into())
        .await
        .expect("publish")
        .await
        .expect("publish ack");
    let retrying = DecisionHandler(Outcome::Retry("temporary".to_owned()));
    tokio::time::timeout(Duration::from_secs(30), async {
        tokio::select! {
            result = run_consumer::<GithubIssues, _>(
                ConsumerStream { pull: &consumer, context: &context, name: "INGRESS" }, &database, "rust-retry", 3, Duration::from_millis(20),
                &retrying
            ) => panic!("consumer stopped: {result:?}"),
            () = async {
                loop {
                    let parked: i64 = sqlx::query_scalar("SELECT count(*) FROM events.parked")
                        .fetch_one(database.pool()).await.expect("parked count");
                    if parked == 1 { break; }
                    tokio::time::sleep(Duration::from_millis(50)).await;
                }
            } => {}
        }
    })
    .await
    .expect("fourth delivery parks event");
    let count: i32 =
        sqlx::query_scalar("SELECT count FROM events.attempts WHERE consumer = 'rust-retry'")
            .fetch_one(database.pool())
            .await
            .expect("durable attempts");
    assert_eq!(count, 4);
}
