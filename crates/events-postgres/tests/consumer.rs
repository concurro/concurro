//! Postgres-backed event processing and durable attempt tests.

use std::{
    assert_matches,
    process::Command,
    sync::Arc,
    sync::atomic::{AtomicUsize, Ordering},
    time::Duration,
};

use concurro_contracts::{Contract, Envelope, EventId, EventKind, GithubIssues};
use concurro_db::{Database, UnitOfWork};
use concurro_events::{Handler, Outcome, ProcessResult};
use concurro_events_postgres::{
    discard_parked, list_parked, migrate, process_delivery, process_released, release_held,
    replay_parked, run_admin, show_held, show_parked,
};
use concurro_testkit::{PostgresContainer, TestState};
use serde::Deserialize;
use tokio::sync::Notify;
use uuid::Uuid;

/// Version-two reader used to simulate a binary upgrade over retained v1 bytes.
#[derive(Clone, Deserialize)]
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

/// A handler with a configured outcome and a tentative effect for Done or Retry.
struct TestHandler {
    /// Result to return on each invocation.
    outcome: Outcome,
    /// Number of times domain logic ran.
    calls: AtomicUsize,
}

impl Handler<GithubIssues> for TestHandler {
    async fn handle(&self, uow: &mut UnitOfWork<'_>, _event: Envelope<GithubIssues>) -> Outcome {
        self.calls.fetch_add(1, Ordering::SeqCst);
        if !matches!(self.outcome, Outcome::Reject(_)) {
            TestState::insert(uow, 7)
                .await
                .expect("handler state write");
        }
        self.outcome.clone()
    }
}

/// Hold until the prerequisite row is committed.
struct HoldHandler;

impl Handler<GithubIssues> for HoldHandler {
    async fn handle(&self, uow: &mut UnitOfWork<'_>, _event: Envelope<GithubIssues>) -> Outcome {
        TestState::insert(uow, 7).await.expect("tentative state");
        let ready: bool =
            sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM test_state WHERE id = 8)")
                .fetch_one(uow.connection())
                .await
                .expect("prerequisite");
        if ready {
            Outcome::Done
        } else {
            Outcome::Hold("ready".to_owned())
        }
    }
}

/// Simulate a long handler that can be observed before it commits.
struct SlowHandler {
    /// Delay while the handler transaction stays open.
    delay: Duration,
    /// Signal that the durable attempt has committed and the handler started.
    started: Option<Arc<Notify>>,
    /// Number of times domain logic started.
    calls: AtomicUsize,
}

impl Handler<GithubIssues> for SlowHandler {
    async fn handle(&self, uow: &mut UnitOfWork<'_>, _event: Envelope<GithubIssues>) -> Outcome {
        self.calls.fetch_add(1, Ordering::SeqCst);
        TestState::insert(uow, 7).await.expect("tentative state");
        if let Some(started) = &self.started {
            started.notify_one();
        }
        tokio::time::sleep(self.delay).await;
        Outcome::Done
    }
}

/// Pause the first hold decision while its prerequisite commits.
struct RacingHoldHandler {
    /// Signal the first missing-prerequisite decision.
    decided: Arc<Notify>,
    /// Resume the paused handler after the prerequisite commit.
    resume: Arc<Notify>,
    /// Number of handler invocations.
    calls: AtomicUsize,
}

impl Handler<GithubIssues> for RacingHoldHandler {
    async fn handle(&self, uow: &mut UnitOfWork<'_>, _event: Envelope<GithubIssues>) -> Outcome {
        let ready: bool =
            sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM test_state WHERE id = 8)")
                .fetch_one(uow.connection())
                .await
                .expect("prerequisite");
        let call = self.calls.fetch_add(1, Ordering::SeqCst);
        if !ready && call == 0 {
            self.decided.notify_one();
            self.resume.notified().await;
            Outcome::Hold("ready".to_owned())
        } else if ready {
            TestState::insert(uow, 7).await.expect("dependent effect");
            Outcome::Done
        } else {
            Outcome::Hold("ready".to_owned())
        }
    }
}

/// Change the prerequisite key once while confirming a Hold.
struct ChangingKeyHandler(AtomicUsize);

impl Handler<GithubIssues> for ChangingKeyHandler {
    async fn handle(&self, _uow: &mut UnitOfWork<'_>, _event: Envelope<GithubIssues>) -> Outcome {
        let key = if self.0.fetch_add(1, Ordering::SeqCst) == 0 {
            "first"
        } else {
            "second"
        };
        Outcome::Hold(key.to_owned())
    }
}

/// Pause inside confirmation while its prerequisite key lock is held.
struct BlockingConfirmHandler {
    /// Signal entry into the confirming handler call.
    started: Arc<Notify>,
    /// Release that handler call after the competing release is waiting.
    resume: Arc<Notify>,
    /// Number of handler invocations.
    calls: AtomicUsize,
}

impl Handler<GithubIssues> for BlockingConfirmHandler {
    async fn handle(&self, uow: &mut UnitOfWork<'_>, _event: Envelope<GithubIssues>) -> Outcome {
        let ready: bool =
            sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM test_state WHERE id = 8)")
                .fetch_one(uow.connection())
                .await
                .expect("prerequisite");
        if ready {
            return Outcome::Done;
        }
        if self.calls.fetch_add(1, Ordering::SeqCst) == 1 {
            self.started.notify_one();
            self.resume.notified().await;
        }
        Outcome::Hold("ready".to_owned())
    }
}

/// Wait for five prerequisites, one key at a time.
struct ChainHandler;

impl Handler<GithubIssues> for ChainHandler {
    async fn handle(&self, uow: &mut UnitOfWork<'_>, _event: Envelope<GithubIssues>) -> Outcome {
        let ready: i64 =
            sqlx::query_scalar("SELECT count(*) FROM test_state WHERE id BETWEEN 1 AND 5")
                .fetch_one(uow.connection())
                .await
                .expect("prerequisite count");
        if ready == 5 {
            TestState::insert(uow, 7).await.expect("dependent effect");
            Outcome::Done
        } else {
            Outcome::Hold(format!("prerequisite-{}", ready + 1))
        }
    }
}

/// Return Done after deliberately ignoring a SQL error in the handler transaction.
struct SwallowingHandler {
    /// Whether the first call enters hold confirmation.
    hold_first: bool,
    /// Number of handler invocations.
    calls: AtomicUsize,
}

/// Count successful applications without a domain table dependency.
struct CountingHandler(AtomicUsize);

impl Handler<GithubIssues> for CountingHandler {
    async fn handle(&self, _uow: &mut UnitOfWork<'_>, _event: Envelope<GithubIssues>) -> Outcome {
        self.0.fetch_add(1, Ordering::SeqCst);
        Outcome::Done
    }
}

impl Handler<GithubIssuesV2> for CountingHandler {
    async fn handle(&self, _uow: &mut UnitOfWork<'_>, _event: Envelope<GithubIssuesV2>) -> Outcome {
        self.0.fetch_add(1, Ordering::SeqCst);
        Outcome::Done
    }
}

impl Handler<GithubIssues> for SwallowingHandler {
    async fn handle(&self, uow: &mut UnitOfWork<'_>, _event: Envelope<GithubIssues>) -> Outcome {
        let call = self.calls.fetch_add(1, Ordering::SeqCst);
        if self.hold_first && call == 0 {
            return Outcome::Hold("ready".to_owned());
        }
        let _ = TestState::insert(uow, 1).await;
        Outcome::Done
    }
}

/// Applied effects commit once even when the same event is delivered again.
#[tokio::test]
async fn done_is_deduplicated() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    TestState::migrate(&database).await.expect("state schema");
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let handler = TestHandler {
        outcome: Outcome::Done,
        calls: AtomicUsize::new(0),
    };
    let first =
        process_delivery::<GithubIssues, _>(&database, "worker", 1, raw, "message-1", &handler)
            .await
            .expect("first delivery");
    let second =
        process_delivery::<GithubIssues, _>(&database, "worker", 1, raw, "message-1", &handler)
            .await
            .expect("duplicate delivery");
    assert_eq!(first, ProcessResult::Done);
    assert_eq!(second, ProcessResult::Skipped);
    assert_eq!(handler.calls.load(Ordering::SeqCst), 1);
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM test_state")
        .fetch_one(database.pool())
        .await
        .expect("state count");
    assert_eq!(count, 1);
}

/// A swallowed SQL error cannot turn a rolled-back handler into an ACK.
#[tokio::test]
async fn aborted_handler_transaction_retries() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    TestState::migrate(&database).await.expect("state schema");
    let mut setup = database.begin().await.expect("setup transaction");
    TestState::insert(&mut setup, 1)
        .await
        .expect("conflicting row");
    setup.commit().await.expect("setup commit");
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let handler = SwallowingHandler {
        hold_first: false,
        calls: AtomicUsize::new(0),
    };
    let first = process_delivery::<GithubIssues, _>(&database, "worker", 1, raw, "first", &handler)
        .await
        .expect("first delivery");
    assert_matches!(first, ProcessResult::Retry(_));
    let consumed: i64 = sqlx::query_scalar("SELECT count(*) FROM events.consumed")
        .fetch_one(database.pool())
        .await
        .expect("consumed rows");
    assert_eq!(consumed, 0);
    assert_eq!(
        process_delivery::<GithubIssues, _>(&database, "worker", 1, raw, "first", &handler)
            .await
            .expect("limit delivery"),
        ProcessResult::Parked
    );
    assert_eq!(handler.calls.load(Ordering::SeqCst), 1);

    let mut held_event: serde_json::Value = serde_json::from_slice(raw).expect("fixture");
    held_event["id"] = serde_json::json!(EventId::mint().0.to_string());
    let held_event = serde_json::to_vec(&held_event).expect("held event");
    let confirming = SwallowingHandler {
        hold_first: true,
        calls: AtomicUsize::new(0),
    };
    let result = process_delivery::<GithubIssues, _>(
        &database,
        "worker",
        1,
        &held_event,
        "held",
        &confirming,
    )
    .await
    .expect("hold confirmation");
    assert_matches!(result, ProcessResult::Retry(_));
    assert_eq!(confirming.calls.load(Ordering::SeqCst), 2);
}

/// A retry rolls back domain writes and reaches one durable parking.
#[tokio::test]
async fn retries_park_at_limit() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    TestState::migrate(&database).await.expect("state schema");
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let handler = TestHandler {
        outcome: Outcome::Retry("temporary failure".to_owned()),
        calls: AtomicUsize::new(0),
    };
    for _ in 0..3 {
        let result =
            process_delivery::<GithubIssues, _>(&database, "worker", 3, raw, "message-1", &handler)
                .await
                .expect("retry delivery");
        assert_matches!(result, ProcessResult::Retry(_));
    }
    let parked =
        process_delivery::<GithubIssues, _>(&database, "worker", 3, raw, "message-1", &handler)
            .await
            .expect("limit delivery");
    assert_eq!(parked, ProcessResult::Parked);
    let duplicate =
        process_delivery::<GithubIssues, _>(&database, "worker", 3, raw, "message-1", &handler)
            .await
            .expect("duplicate parked delivery");
    assert_eq!(duplicate, ProcessResult::Skipped);
    assert_eq!(handler.calls.load(Ordering::SeqCst), 3);
    let state: i64 = sqlx::query_scalar("SELECT count(*) FROM test_state")
        .fetch_one(database.pool())
        .await
        .expect("state count");
    let parkings: i64 = sqlx::query_scalar("SELECT count(*) FROM events.parked")
        .fetch_one(database.pool())
        .await
        .expect("parked count");
    assert_eq!(state, 0);
    assert_eq!(parkings, 1);
}

/// Malformed data consumes the configured attempt budget and parks exact bytes.
#[tokio::test]
async fn invalid_envelope_is_parked_once() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    let raw = b"{invalid";
    let handler = TestHandler {
        outcome: Outcome::Done,
        calls: AtomicUsize::new(0),
    };
    let first =
        process_delivery::<GithubIssues, _>(&database, "worker", 2, raw, "nats-id-1", &handler)
            .await
            .expect("invalid first delivery");
    let second =
        process_delivery::<GithubIssues, _>(&database, "worker", 2, raw, "nats-id-1", &handler)
            .await
            .expect("invalid second delivery");
    let third =
        process_delivery::<GithubIssues, _>(&database, "worker", 2, raw, "nats-id-1", &handler)
            .await
            .expect("invalid limit delivery");
    assert_matches!(first, ProcessResult::Retry(_));
    assert_matches!(second, ProcessResult::Retry(_));
    assert_eq!(third, ProcessResult::Parked);
    assert_eq!(handler.calls.load(Ordering::SeqCst), 0);
    let stored: (Option<serde_json::Value>, Vec<u8>) =
        sqlx::query_as("SELECT message, wire_bytes FROM events.parked")
            .fetch_one(database.pool())
            .await
            .expect("parking");
    assert_eq!(stored.0, None);
    assert_eq!(stored.1, raw);

    let mut newer: serde_json::Value = serde_json::from_slice(include_bytes!(
        "../../../contracts/fixtures/github.issues.v1/valid/opened.json"
    ))
    .expect("version 1 fixture");
    newer["version"] = serde_json::json!(2);
    let newer = serde_json::to_vec(&newer).expect("version 2 wire");
    assert_eq!(
        process_delivery::<GithubIssues, _>(&database, "worker", 2, &newer, "v2", &handler)
            .await
            .expect("unsupported version"),
        ProcessResult::Skipped
    );
    let parked: i64 = sqlx::query_scalar("SELECT count(*) FROM events.parked")
        .fetch_one(database.pool())
        .await
        .expect("parked count");
    assert_eq!(parked, 1);
    for key in ["bad\0header".to_owned(), "x".repeat(3000)] {
        assert_matches!(
            process_delivery::<GithubIssues, _>(&database, "worker", 1, raw, &key, &handler)
                .await
                .expect("first malformed keyed attempt"),
            ProcessResult::Retry(_)
        );
        assert_eq!(
            process_delivery::<GithubIssues, _>(&database, "worker", 1, raw, &key, &handler)
                .await
                .expect("park malformed keyed attempt"),
            ProcessResult::Parked
        );
    }
    let aliases: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM events.identity_aliases WHERE fallback_key LIKE 'sha256:%'",
    )
    .fetch_one(database.pool())
    .await
    .expect("bounded aliases");
    assert_eq!(aliases, 2);
}

/// Replayed events keep their original identity across a decoder repair.
#[tokio::test]
async fn decoder_repair_preserves_real_and_synthetic_identities() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    let fixture: serde_json::Value = serde_json::from_slice(include_bytes!(
        "../../../contracts/fixtures/github.issues.v1/valid/opened.json"
    ))
    .expect("fixture");
    let handler = CountingHandler(AtomicUsize::new(0));
    for (index, key) in ["valid-id", "synthetic-id"].into_iter().enumerate() {
        let mut fixed = fixture.clone();
        fixed["id"] = serde_json::json!(EventId::mint().0.to_string());
        let mut invalid = fixed.clone();
        if index == 0 {
            invalid["type"] = serde_json::json!("github.issues.closed");
        } else {
            invalid["id"] = serde_json::json!("invalid-id");
        }
        let invalid = serde_json::to_vec(&invalid).expect("invalid event");
        let fixed = serde_json::to_vec(&fixed).expect("repaired event");
        assert_matches!(
            process_delivery::<GithubIssues, _>(&database, "worker", 1, &invalid, key, &handler)
                .await
                .expect("first invalid delivery"),
            ProcessResult::Retry(_)
        );
        assert_eq!(
            process_delivery::<GithubIssues, _>(&database, "worker", 1, &invalid, key, &handler)
                .await
                .expect("parking"),
            ProcessResult::Parked
        );
        let parked = list_parked(&database, "worker")
            .await
            .expect("parked rows")
            .pop()
            .expect("open parking");
        let expected_id = if index == 0 {
            serde_json::from_slice::<Envelope<GithubIssues>>(&fixed)
                .expect("fixed envelope")
                .id
                .0
        } else {
            Uuid::new_v5(&Uuid::NAMESPACE_OID, format!("worker:raw:{key}").as_bytes())
        };
        assert_eq!(parked.event_id, expected_id);
        assert!(
            replay_parked(&database, parked.id, "operator")
                .await
                .expect("replay")
        );
        let message: serde_json::Value = serde_json::from_slice(&fixed).expect("fixed value");
        sqlx::query(
            "UPDATE events.held SET message = $2, wire_bytes = $3 \
             WHERE consumer = 'worker' AND event_id = $1",
        )
        .bind(expected_id)
        .bind(sqlx::types::Json(message))
        .bind(&fixed)
        .execute(database.pool())
        .await
        .expect("decoder repair fixture");
        assert_eq!(
            process_released::<GithubIssues, _>(&database, "worker", 1, &handler)
                .await
                .expect("replayed event"),
            Some(ProcessResult::Done)
        );
        assert_eq!(
            process_delivery::<GithubIssues, _>(&database, "worker", 1, &fixed, key, &handler)
                .await
                .expect("normal redelivery after repair"),
            ProcessResult::Skipped
        );
    }
    assert_eq!(handler.0.load(Ordering::SeqCst), 2);
}

/// A hold clears attempts and resumes after its prerequisite commits.
#[tokio::test]
async fn held_event_releases_and_applies_once() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    TestState::migrate(&database).await.expect("state schema");
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let held =
        process_delivery::<GithubIssues, _>(&database, "worker", 2, raw, "message-1", &HoldHandler)
            .await
            .expect("hold");
    assert_eq!(held, ProcessResult::Held);
    let duplicate =
        process_delivery::<GithubIssues, _>(&database, "worker", 2, raw, "message-1", &HoldHandler)
            .await
            .expect("duplicate hold");
    assert_eq!(duplicate, ProcessResult::Skipped);
    let event: Envelope<GithubIssues> = serde_json::from_slice(raw).expect("held fixture");
    let held_detail = show_held(&database, "worker", event.id.0)
        .await
        .expect("held detail")
        .expect("held row");
    assert_eq!(held_detail.key, "ready");
    assert_eq!(held_detail.wire_bytes, raw);
    let attempts: i64 = sqlx::query_scalar("SELECT count(*) FROM events.attempts")
        .fetch_one(database.pool())
        .await
        .expect("attempt count");
    assert_eq!(attempts, 0);
    let state: i64 = sqlx::query_scalar("SELECT count(*) FROM test_state")
        .fetch_one(database.pool())
        .await
        .expect("state count");
    assert_eq!(state, 0);
    let mut prerequisite = database.begin().await.expect("prerequisite transaction");
    TestState::insert(&mut prerequisite, 8)
        .await
        .expect("ready state");
    assert_eq!(
        release_held(&mut prerequisite, "worker", "ready")
            .await
            .expect("release"),
        1
    );
    prerequisite.commit().await.expect("ready commit");
    let result = process_released::<GithubIssues, _>(&database, "worker", 2, &HoldHandler)
        .await
        .expect("released event");
    assert_eq!(result, Some(ProcessResult::Done));
    assert_eq!(
        process_released::<GithubIssues, _>(&database, "worker", 2, &HoldHandler)
            .await
            .expect("empty held queue"),
        None
    );
    let state: i64 = sqlx::query_scalar("SELECT count(*) FROM test_state")
        .fetch_one(database.pool())
        .await
        .expect("final state count");
    assert_eq!(state, 2);
    let held: i64 = sqlx::query_scalar("SELECT count(*) FROM events.held")
        .fetch_one(database.pool())
        .await
        .expect("held count");
    assert_eq!(held, 0);
}

/// Escaped NUL bytes survive hold, release, retry, and parking without jsonb.
#[tokio::test]
async fn nul_escape_uses_authoritative_wire_bytes() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    TestState::migrate(&database).await.expect("state schema");
    let mut message: serde_json::Value = serde_json::from_slice(include_bytes!(
        "../../../contracts/fixtures/github.issues.v1/valid/opened.json"
    ))
    .expect("fixture");
    message["extra"] = serde_json::json!("\u{0000}");
    let raw = serde_json::to_vec(&message).expect("wire bytes");
    assert!(raw.windows(6).any(|window| window == br"\u0000"));
    let event: Envelope<GithubIssues> = serde_json::from_slice(&raw).expect("typed event");
    assert_eq!(
        process_delivery::<GithubIssues, _>(
            &database,
            "worker",
            1,
            &raw,
            "nul-event",
            &HoldHandler,
        )
        .await
        .expect("hold"),
        ProcessResult::Held
    );
    let held_message: Option<serde_json::Value> = sqlx::query_scalar(
        "SELECT message FROM events.held WHERE consumer = 'worker' AND event_id = $1",
    )
    .bind(event.id.0)
    .fetch_one(database.pool())
    .await
    .expect("held jsonb");
    assert_eq!(held_message, None);
    let mut release = database.begin().await.expect("release transaction");
    assert_eq!(
        release_held(&mut release, "worker", "ready")
            .await
            .expect("release"),
        1
    );
    release.commit().await.expect("release commit");
    let retrying = TestHandler {
        outcome: Outcome::Retry("temporary".to_owned()),
        calls: AtomicUsize::new(0),
    };
    assert_matches!(
        process_released::<GithubIssues, _>(&database, "worker", 1, &retrying)
            .await
            .expect("retry held"),
        Some(ProcessResult::Retry(_))
    );
    sqlx::query("UPDATE events.held SET retry_at = now() WHERE consumer = 'worker'")
        .execute(database.pool())
        .await
        .expect("make retry due");
    assert_eq!(
        process_released::<GithubIssues, _>(&database, "worker", 1, &retrying)
            .await
            .expect("park held"),
        Some(ProcessResult::Parked)
    );
    assert_eq!(retrying.calls.load(Ordering::SeqCst), 1);
    let parked: (Option<serde_json::Value>, Vec<u8>) =
        sqlx::query_as("SELECT message, wire_bytes FROM events.parked")
            .fetch_one(database.pool())
            .await
            .expect("parked bytes");
    assert_eq!(parked.0, None);
    assert_eq!(parked.1, raw);
}

/// A release between the first Hold and its confirmation is never missed.
#[tokio::test]
async fn prerequisite_commits_during_hold_decision() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    TestState::migrate(&database).await.expect("state schema");
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let decided = Arc::new(Notify::new());
    let resume = Arc::new(Notify::new());
    let handler = RacingHoldHandler {
        decided: decided.clone(),
        resume: resume.clone(),
        calls: AtomicUsize::new(0),
    };
    let mut delivery = Box::pin(process_delivery::<GithubIssues, _>(
        &database,
        "worker",
        2,
        raw,
        "message-1",
        &handler,
    ));
    tokio::select! {
        result = &mut delivery => panic!("delivery completed before first hold: {result:?}"),
        () = decided.notified() => {}
    }
    let mut prerequisite = database.begin().await.expect("prerequisite transaction");
    TestState::insert(&mut prerequisite, 8)
        .await
        .expect("ready state");
    assert_eq!(
        release_held(&mut prerequisite, "worker", "ready")
            .await
            .expect("early release"),
        0
    );
    prerequisite.commit().await.expect("ready commit");
    resume.notify_one();
    assert_eq!(
        delivery.await.expect("confirmed event"),
        ProcessResult::Done
    );
    assert_eq!(handler.calls.load(Ordering::SeqCst), 2);
    let state: i64 = sqlx::query_scalar("SELECT count(*) FROM test_state")
        .fetch_one(database.pool())
        .await
        .expect("state count");
    let held: i64 = sqlx::query_scalar("SELECT count(*) FROM events.held")
        .fetch_one(database.pool())
        .await
        .expect("held count");
    assert_eq!(state, 2);
    assert_eq!(held, 0);
}

/// Hold confirmation waits for an in-flight prerequisite release transaction.
#[tokio::test]
async fn prerequisite_release_blocks_hold_confirmation_until_commit() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    TestState::migrate(&database).await.expect("state schema");
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let decided = Arc::new(Notify::new());
    let resume = Arc::new(Notify::new());
    let handler = RacingHoldHandler {
        decided: decided.clone(),
        resume: resume.clone(),
        calls: AtomicUsize::new(0),
    };
    let mut delivery = Box::pin(process_delivery::<GithubIssues, _>(
        &database,
        "worker",
        2,
        raw,
        "blocked-release",
        &handler,
    ));
    tokio::select! {
        result = &mut delivery => panic!("delivery completed before first hold: {result:?}"),
        () = decided.notified() => {}
    }
    let mut prerequisite = database.begin().await.expect("prerequisite transaction");
    TestState::insert(&mut prerequisite, 8)
        .await
        .expect("ready state");
    assert_eq!(
        release_held(&mut prerequisite, "worker", "ready")
            .await
            .expect("early release"),
        0
    );
    resume.notify_one();
    assert!(
        tokio::time::timeout(Duration::from_millis(100), &mut delivery)
            .await
            .is_err()
    );
    prerequisite.commit().await.expect("prerequisite commit");
    assert_eq!(
        delivery.await.expect("confirmed event"),
        ProcessResult::Done
    );
    assert_eq!(handler.calls.load(Ordering::SeqCst), 2);
    let held: i64 = sqlx::query_scalar("SELECT count(*) FROM events.held")
        .fetch_one(database.pool())
        .await
        .expect("held count");
    assert_eq!(held, 0);
}

/// Confirmation switches to the new key before persisting the held row.
#[tokio::test]
async fn hold_confirmation_rechecks_changed_prerequisite_key() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let handler = ChangingKeyHandler(AtomicUsize::new(0));
    assert_eq!(
        process_delivery::<GithubIssues, _>(&database, "worker", 2, raw, "changing-key", &handler)
            .await
            .expect("changed hold"),
        ProcessResult::Held
    );
    assert_eq!(handler.0.load(Ordering::SeqCst), 3);
    let key: String = sqlx::query_scalar("SELECT key FROM events.held WHERE consumer = 'worker'")
        .fetch_one(database.pool())
        .await
        .expect("held key");
    assert_eq!(key, "second");
}

/// A release waits for confirmation's key lock, then releases its committed Hold.
#[tokio::test]
async fn prerequisite_release_waits_for_hold_confirmation() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    TestState::migrate(&database).await.expect("state schema");
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let started = Arc::new(Notify::new());
    let resume = Arc::new(Notify::new());
    let handler = Arc::new(BlockingConfirmHandler {
        started: started.clone(),
        resume: resume.clone(),
        calls: AtomicUsize::new(0),
    });
    let delivery_database = database.clone();
    let delivery_handler = handler.clone();
    let delivery = tokio::spawn(async move {
        process_delivery::<GithubIssues, _>(
            &delivery_database,
            "worker",
            2,
            raw,
            "blocked-confirmation",
            delivery_handler.as_ref(),
        )
        .await
    });
    started.notified().await;
    let mut prerequisite = database.begin().await.expect("prerequisite transaction");
    TestState::insert(&mut prerequisite, 8)
        .await
        .expect("ready state");
    let mut release = tokio::spawn(async move {
        let changed = release_held(&mut prerequisite, "worker", "ready").await?;
        prerequisite.commit().await?;
        anyhow::Ok(changed)
    });
    assert!(
        tokio::time::timeout(Duration::from_millis(100), &mut release)
            .await
            .is_err()
    );
    resume.notify_one();
    assert_eq!(
        delivery.await.expect("delivery task").expect("hold"),
        ProcessResult::Held
    );
    assert_eq!(release.await.expect("release task").expect("release"), 1);
    assert_eq!(
        process_released::<GithubIssues, _>(&database, "worker", 2, handler.as_ref())
            .await
            .expect("released row"),
        Some(ProcessResult::Done)
    );
}

/// Holds through more keys than the failure budget never park the event.
#[tokio::test]
async fn prerequisite_chain_outlives_attempt_limit() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    TestState::migrate(&database).await.expect("state schema");
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    assert_eq!(
        process_delivery::<GithubIssues, _>(&database, "worker", 2, raw, "chain", &ChainHandler)
            .await
            .expect("initial hold"),
        ProcessResult::Held
    );
    for step in 1..=5 {
        let mut prerequisite = database.begin().await.expect("prerequisite transaction");
        TestState::insert(&mut prerequisite, step)
            .await
            .expect("prerequisite state");
        assert_eq!(
            release_held(&mut prerequisite, "worker", &format!("prerequisite-{step}"))
                .await
                .expect("release"),
            1
        );
        prerequisite.commit().await.expect("prerequisite commit");
        let result = process_released::<GithubIssues, _>(&database, "worker", 2, &ChainHandler)
            .await
            .expect("released chain event");
        assert_eq!(
            result,
            Some(if step == 5 {
                ProcessResult::Done
            } else {
                ProcessResult::Held
            })
        );
    }
    let parked: i64 = sqlx::query_scalar("SELECT count(*) FROM events.parked")
        .fetch_one(database.pool())
        .await
        .expect("parked count");
    let held: i64 = sqlx::query_scalar("SELECT count(*) FROM events.held")
        .fetch_one(database.pool())
        .await
        .expect("held count");
    assert_eq!(parked, 0);
    assert_eq!(held, 0);
}

/// An unsupported released version parks without starving the next due row.
#[tokio::test]
async fn unsupported_released_row_does_not_starve_queue() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    TestState::migrate(&database).await.expect("state schema");
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let valid: Envelope<GithubIssues> = serde_json::from_slice(raw).expect("fixture");
    let mut unsupported: serde_json::Value = serde_json::from_slice(raw).expect("fixture value");
    unsupported["version"] = serde_json::json!(2);
    unsupported["id"] = serde_json::json!(Uuid::nil().to_string());
    let unsupported = serde_json::to_vec(&unsupported).expect("unsupported wire");
    for (id, key, wire) in [
        (Uuid::nil(), "obsolete", unsupported.as_slice()),
        (valid.id.0, "ready", raw.as_slice()),
    ] {
        sqlx::query(
            "INSERT INTO events.held (consumer, event_id, key, wire_bytes, released) \
             VALUES ('worker', $1, $2, $3, true)",
        )
        .bind(id)
        .bind(key)
        .bind(wire)
        .execute(database.pool())
        .await
        .expect("released row");
    }
    let handler = TestHandler {
        outcome: Outcome::Done,
        calls: AtomicUsize::new(0),
    };
    assert_eq!(
        process_released::<GithubIssues, _>(&database, "worker", 2, &handler)
            .await
            .expect("obsolete row"),
        Some(ProcessResult::Parked)
    );
    assert_eq!(
        process_released::<GithubIssues, _>(&database, "worker", 2, &handler)
            .await
            .expect("next row"),
        Some(ProcessResult::Done)
    );
    let held: i64 = sqlx::query_scalar("SELECT count(*) FROM events.held")
        .fetch_one(database.pool())
        .await
        .expect("held count");
    assert_eq!(held, 0);
    let parked = list_parked(&database, "worker").await.expect("parked rows");
    assert_eq!(parked.len(), 1);
    assert!(parked[0].reason.contains("unsupported contract version 2"));
    assert!(
        replay_parked(&database, parked[0].id, "operator")
            .await
            .expect("replay unsupported row")
    );
    assert_eq!(
        process_released::<GithubIssues, _>(&database, "worker", 2, &handler)
            .await
            .expect("unsupported replay"),
        Some(ProcessResult::Parked)
    );
    assert!(
        show_held(&database, "worker", Uuid::nil())
            .await
            .expect("held query")
            .is_none()
    );
    assert_eq!(handler.calls.load(Ordering::SeqCst), 1);
}

/// A fresh version-two copy cannot take ownership from a version-one retry.
#[tokio::test]
async fn dual_published_unsupported_copy_skips_owned_retry() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    TestState::migrate(&database).await.expect("state schema");
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let handler = TestHandler {
        outcome: Outcome::Retry("backing off".to_owned()),
        calls: AtomicUsize::new(0),
    };
    assert_matches!(
        process_delivery::<GithubIssues, _>(&database, "worker", 3, raw, "old", &handler)
            .await
            .expect("first attempt"),
        ProcessResult::Retry(_)
    );
    let mut unsupported: serde_json::Value = serde_json::from_slice(raw).expect("fixture");
    unsupported["version"] = serde_json::json!(2);
    let unsupported = serde_json::to_vec(&unsupported).expect("unsupported wire");
    assert_eq!(
        process_delivery::<GithubIssues, _>(&database, "worker", 3, &unsupported, "new", &handler)
            .await
            .expect("fresh unsupported copy"),
        ProcessResult::Skipped
    );
    let applying = CountingHandler(AtomicUsize::new(0));
    assert_eq!(
        process_delivery::<GithubIssues, _>(&database, "worker", 3, raw, "old", &applying)
            .await
            .expect("supported retry"),
        ProcessResult::Done
    );
    assert_eq!(applying.0.load(Ordering::SeqCst), 1);
    let parked = list_parked(&database, "worker").await.expect("parked rows");
    assert!(parked.is_empty());
    let mut fresh: serde_json::Value = serde_json::from_slice(&unsupported).expect("wire");
    fresh["id"] = serde_json::json!(EventId::mint().0.to_string());
    let fresh = serde_json::to_vec(&fresh).expect("fresh wire");
    assert_eq!(
        process_delivery::<GithubIssues, _>(&database, "worker", 3, &fresh, "fresh", &handler)
            .await
            .expect("unowned unsupported copy"),
        ProcessResult::Skipped
    );
    let aliases: i64 = sqlx::query_scalar("SELECT count(*) FROM events.identity_aliases")
        .fetch_one(database.pool())
        .await
        .expect("aliases");
    assert_eq!(aliases, 1);
}

/// The same retried bytes become an audited parking after a decoder upgrade.
#[tokio::test]
async fn unsupported_version_parks_after_reader_upgrade() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    TestState::migrate(&database).await.expect("state schema");
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let retrying = TestHandler {
        outcome: Outcome::Retry("backing off".to_owned()),
        calls: AtomicUsize::new(0),
    };
    assert_matches!(
        process_delivery::<GithubIssues, _>(&database, "worker", 3, raw, "event:1", &retrying)
            .await
            .expect("old reader attempt"),
        ProcessResult::Retry(_)
    );
    let new_reader = CountingHandler(AtomicUsize::new(0));
    assert_eq!(
        process_delivery::<GithubIssuesV2, _>(&database, "worker", 3, raw, "event:1", &new_reader)
            .await
            .expect("same bytes after upgrade"),
        ProcessResult::Parked
    );
    assert_eq!(new_reader.0.load(Ordering::SeqCst), 0);
    let parked = list_parked(&database, "worker").await.expect("parked rows");
    assert_eq!(parked.len(), 1);
    assert!(parked[0].reason.contains("version 1"));
    assert_eq!(
        show_parked(&database, parked[0].id)
            .await
            .expect("parking detail")
            .expect("parking")
            .wire_bytes,
        raw
    );
    let mut newer: serde_json::Value = serde_json::from_slice(raw).expect("fixture value");
    newer["version"] = serde_json::json!(2);
    let newer = serde_json::to_vec(&newer).expect("new version wire");
    assert_eq!(
        process_delivery::<GithubIssuesV2, _>(
            &database,
            "worker",
            3,
            &newer,
            "event:2",
            &new_reader
        )
        .await
        .expect("readable newer copy"),
        ProcessResult::Skipped
    );
    assert_eq!(new_reader.0.load(Ordering::SeqCst), 0);
    assert!(
        replay_parked(&database, parked[0].id, "operator")
            .await
            .expect("replay old version")
    );
    assert_eq!(
        process_delivery::<GithubIssuesV2, _>(
            &database,
            "worker",
            3,
            &newer,
            "event:2",
            &new_reader
        )
        .await
        .expect("newer copy during replay"),
        ProcessResult::Skipped
    );
    assert_eq!(
        process_released::<GithubIssuesV2, _>(&database, "worker", 3, &new_reader)
            .await
            .expect("old version repark"),
        Some(ProcessResult::Parked)
    );
    assert_eq!(
        process_delivery::<GithubIssuesV2, _>(
            &database,
            "worker",
            3,
            &newer,
            "event:2",
            &new_reader
        )
        .await
        .expect("newer copy after repark"),
        ProcessResult::Skipped
    );
    assert_eq!(new_reader.0.load(Ordering::SeqCst), 0);
    let open = list_parked(&database, "worker")
        .await
        .expect("open parkings");
    assert_eq!(open.len(), 1);
    assert!(open[0].reason.contains("version 1"));
    assert_eq!(
        show_parked(&database, open[0].id)
            .await
            .expect("repark detail")
            .expect("repark")
            .wire_bytes,
        raw
    );
    assert!(
        discard_parked(&database, open[0].id, "operator", "version reviewed")
            .await
            .expect("discard parking")
    );
}

/// A duplicate at the attempt limit cannot park a confirming hold.
#[tokio::test]
async fn hold_confirmation_beats_duplicate_parking() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    TestState::migrate(&database).await.expect("state schema");
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let decided = Arc::new(Notify::new());
    let resume = Arc::new(Notify::new());
    let handler = RacingHoldHandler {
        decided: decided.clone(),
        resume: resume.clone(),
        calls: AtomicUsize::new(0),
    };
    let mut first = Box::pin(process_delivery::<GithubIssues, _>(
        &database, "worker", 1, raw, "first", &handler,
    ));
    tokio::select! {
        result = &mut first => panic!("first delivery completed before hold: {result:?}"),
        () = decided.notified() => {}
    }
    let mut second = Box::pin(process_delivery::<GithubIssues, _>(
        &database,
        "worker",
        1,
        raw,
        "redelivery",
        &handler,
    ));
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            tokio::select! {
                result = &mut first => panic!("first delivery completed during gate: {result:?}"),
                result = &mut second => panic!("duplicate completed during gate: {result:?}"),
                () = tokio::time::sleep(Duration::from_millis(20)) => {
                    let waiting: i64 = sqlx::query_scalar(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                    ).fetch_one(database.pool()).await.expect("lock wait");
                    if waiting > 0 {
                        break;
                    }
                }
            }
        }
    })
    .await
    .expect("duplicate waits on first event lock");
    resume.notify_one();
    let (first, second) = tokio::join!(first, second);
    assert_eq!(first.expect("first delivery"), ProcessResult::Held);
    assert_eq!(second.expect("duplicate delivery"), ProcessResult::Skipped);
    let parked: i64 = sqlx::query_scalar("SELECT count(*) FROM events.parked")
        .fetch_one(database.pool())
        .await
        .expect("parked count");
    assert_eq!(parked, 0);
    let mut prerequisite = database.begin().await.expect("prerequisite transaction");
    TestState::insert(&mut prerequisite, 8)
        .await
        .expect("ready state");
    assert_eq!(
        release_held(&mut prerequisite, "worker", "ready")
            .await
            .expect("release"),
        1
    );
    prerequisite.commit().await.expect("ready commit");
    assert_eq!(
        process_released::<GithubIssues, _>(&database, "worker", 1, &handler)
            .await
            .expect("released event"),
        Some(ProcessResult::Done)
    );
}

/// Replay and discard preserve parking history and serialize with deliveries.
#[tokio::test]
async fn operator_replay_and_discard() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    TestState::migrate(&database).await.expect("state schema");
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let rejecting = TestHandler {
        outcome: Outcome::Reject("operator review".to_owned()),
        calls: AtomicUsize::new(0),
    };
    assert_eq!(
        process_delivery::<GithubIssues, _>(&database, "worker", 3, raw, "message-1", &rejecting)
            .await
            .expect("park"),
        ProcessResult::Parked
    );
    let open = list_parked(&database, "worker").await.expect("parked list");
    assert_eq!(open.len(), 1);
    let output = Command::new(env!("CARGO_BIN_EXE_events_admin"))
        .args(["parked", "list"])
        .env("DATABASE_URL", postgres.url())
        .env("CONSUMER", "worker")
        .output()
        .expect("parked list command");
    assert!(output.status.success());
    let listed: serde_json::Value =
        serde_json::from_slice(&output.stdout).expect("parked list JSON");
    assert_eq!(listed.as_array().expect("parking list").len(), 1);
    let detail = show_parked(&database, open[0].id)
        .await
        .expect("parking")
        .expect("row");
    assert_eq!(detail.wire_bytes, raw);
    let shown: serde_json::Value = serde_json::from_str(
        &run_admin(
            &database,
            "worker",
            &[
                "parked".to_owned(),
                "show".to_owned(),
                detail.id.to_string(),
            ],
        )
        .await
        .expect("shared admin entry point"),
    )
    .expect("readable operator JSON");
    assert_eq!(
        shown["wire_message"],
        serde_json::from_slice::<serde_json::Value>(raw).expect("event JSON")
    );
    assert!(shown.get("wire_bytes").is_none());
    assert!(shown["parked_at"].is_string());
    let output = Command::new(env!("CARGO_BIN_EXE_events_admin"))
        .args(["parked", "replay", &detail.id.to_string(), "operator"])
        .env("DATABASE_URL", postgres.url())
        .env("CONSUMER", "worker")
        .output()
        .expect("parked replay command");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(output.stdout, b"true\n");
    assert!(
        !replay_parked(&database, detail.id, "operator")
            .await
            .expect("repeat replay")
    );
    let applying = TestHandler {
        outcome: Outcome::Done,
        calls: AtomicUsize::new(0),
    };
    assert_eq!(
        process_delivery::<GithubIssues, _>(&database, "worker", 3, raw, "message-1", &applying)
            .await
            .expect("copy during replay"),
        ProcessResult::Skipped
    );
    assert_eq!(
        process_released::<GithubIssues, _>(&database, "worker", 3, &applying)
            .await
            .expect("released replay"),
        Some(ProcessResult::Done)
    );
    assert!(
        list_parked(&database, "worker")
            .await
            .expect("open parking list")
            .is_empty()
    );
    let history = show_parked(&database, detail.id)
        .await
        .expect("history")
        .expect("row");
    assert_eq!(history.replayed_by.as_deref(), Some("operator"));
    assert_eq!(applying.calls.load(Ordering::SeqCst), 1);

    let mut second: serde_json::Value = serde_json::from_slice(raw).expect("event value");
    second["id"] = serde_json::Value::String(EventId::mint().0.to_string());
    let second = serde_json::to_vec(&second).expect("second event");
    assert_eq!(
        process_delivery::<GithubIssues, _>(
            &database,
            "worker",
            3,
            &second,
            "message-2",
            &rejecting
        )
        .await
        .expect("second parking"),
        ProcessResult::Parked
    );
    let open = list_parked(&database, "worker").await.expect("open list");
    assert_eq!(open.len(), 1);
    assert!(
        discard_parked(&database, open[0].id, "operator", "obsolete")
            .await
            .expect("discard")
    );
    assert!(
        !discard_parked(&database, open[0].id, "operator", "obsolete")
            .await
            .expect("repeat discard")
    );
    assert_eq!(
        process_delivery::<GithubIssues, _>(
            &database,
            "worker",
            3,
            &second,
            "message-2",
            &applying
        )
        .await
        .expect("discarded duplicate"),
        ProcessResult::Skipped
    );
    let history = show_parked(&database, open[0].id)
        .await
        .expect("history")
        .expect("row");
    assert_eq!(history.discarded_by.as_deref(), Some("operator"));
}

/// A copy arriving during operator replay cannot apply outside the released row.
#[tokio::test]
async fn parked_replay_serializes_with_delivery() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 4)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    TestState::migrate(&database).await.expect("state schema");
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let rejecting = TestHandler {
        outcome: Outcome::Reject("operator review".to_owned()),
        calls: AtomicUsize::new(0),
    };
    assert_eq!(
        process_delivery::<GithubIssues, _>(&database, "worker", 3, raw, "first", &rejecting)
            .await
            .expect("initial parking"),
        ProcessResult::Parked
    );
    let parking = list_parked(&database, "worker")
        .await
        .expect("open parking")
        .pop()
        .expect("parking row");
    let mut prerequisite = database.begin().await.expect("prerequisite transaction");
    TestState::insert(&mut prerequisite, 8)
        .await
        .expect("ready state");
    prerequisite.commit().await.expect("ready commit");
    let event: Envelope<GithubIssues> = serde_json::from_slice(raw).expect("fixture");
    let lock_key = format!("event:worker:{}", event.id.0);
    let mut gate = database.pool().acquire().await.expect("gate connection");
    sqlx::query("SELECT pg_advisory_lock(hashtextextended($1, 0))")
        .bind(&lock_key)
        .execute(&mut *gate)
        .await
        .expect("hold event lock");
    let mut replay = Box::pin(replay_parked(&database, parking.id, "operator"));
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            tokio::select! {
                result = &mut replay => panic!("replay completed during gate: {result:?}"),
                () = tokio::time::sleep(Duration::from_millis(20)) => {
                    let waiting: i64 = sqlx::query_scalar(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                    ).fetch_one(database.pool()).await.expect("replay lock wait");
                    if waiting >= 1 { break; }
                }
            }
        }
    })
    .await
    .expect("replay waits on event lock");
    let mut copy = Box::pin(process_delivery::<GithubIssues, _>(
        &database,
        "worker",
        3,
        raw,
        "copy",
        &HoldHandler,
    ));
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            tokio::select! {
                result = &mut replay => panic!("replay completed during gate: {result:?}"),
                result = &mut copy => panic!("copy completed during gate: {result:?}"),
                () = tokio::time::sleep(Duration::from_millis(20)) => {
                    let waiting: i64 = sqlx::query_scalar(
                        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
                    ).fetch_one(database.pool()).await.expect("both lock waits");
                    if waiting >= 2 { break; }
                }
            }
        }
    })
    .await
    .expect("replay and copy both wait on event lock");
    let unlocked: bool = sqlx::query_scalar("SELECT pg_advisory_unlock(hashtextextended($1, 0))")
        .bind(&lock_key)
        .fetch_one(&mut *gate)
        .await
        .expect("release event lock");
    assert!(unlocked);
    let (replayed, duplicate) = tokio::join!(replay, copy);
    assert!(replayed.expect("replay"));
    assert_eq!(duplicate.expect("concurrent copy"), ProcessResult::Skipped);
    assert_eq!(
        process_released::<GithubIssues, _>(&database, "worker", 3, &HoldHandler)
            .await
            .expect("released replay"),
        Some(ProcessResult::Done)
    );
    let history: i64 = sqlx::query_scalar("SELECT count(*) FROM events.parked")
        .fetch_one(database.pool())
        .await
        .expect("parking history");
    let held: i64 = sqlx::query_scalar("SELECT count(*) FROM events.held")
        .fetch_one(database.pool())
        .await
        .expect("held count");
    let state: i64 = sqlx::query_scalar("SELECT count(*) FROM test_state")
        .fetch_one(database.pool())
        .await
        .expect("state count");
    assert_eq!(history, 1);
    assert_eq!(held, 0);
    assert_eq!(state, 2);
}

/// Concurrent deliveries cannot park while the first handler completes.
#[tokio::test]
async fn slow_success_beats_attempt_limit() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    TestState::migrate(&database).await.expect("state schema");
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let handler = SlowHandler {
        delay: Duration::from_millis(200),
        started: None,
        calls: AtomicUsize::new(0),
    };
    let (first, second) = tokio::join!(
        process_delivery::<GithubIssues, _>(&database, "worker", 1, raw, "message-1", &handler),
        process_delivery::<GithubIssues, _>(&database, "worker", 1, raw, "message-1", &handler),
    );
    let results = [
        first.expect("first delivery"),
        second.expect("second delivery"),
    ];
    assert!(results.contains(&ProcessResult::Done));
    assert!(results.contains(&ProcessResult::Skipped));
    assert_eq!(handler.calls.load(Ordering::SeqCst), 1);
    let parked: i64 = sqlx::query_scalar("SELECT count(*) FROM events.parked")
        .fetch_one(database.pool())
        .await
        .expect("parked count");
    assert_eq!(parked, 0);
}

/// Aborting a handler keeps each attempt durable until the next delivery parks.
#[tokio::test]
async fn aborted_handlers_exhaust_budget() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    TestState::migrate(&database).await.expect("state schema");
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let started = Arc::new(Notify::new());
    let slow = SlowHandler {
        delay: Duration::from_secs(30),
        started: Some(started.clone()),
        calls: AtomicUsize::new(0),
    };
    for _ in 0..3 {
        let mut attempt = Box::pin(process_delivery::<GithubIssues, _>(
            &database,
            "worker",
            3,
            raw,
            "message-1",
            &slow,
        ));
        tokio::select! {
            result = &mut attempt => panic!("handler completed before abort: {result:?}"),
            () = started.notified() => {}
        }
        drop(attempt);
    }
    let count: i32 = sqlx::query_scalar("SELECT count FROM events.attempts")
        .fetch_one(database.pool())
        .await
        .expect("durable attempts");
    assert_eq!(count, 3);
    let done = TestHandler {
        outcome: Outcome::Done,
        calls: AtomicUsize::new(0),
    };
    let result =
        process_delivery::<GithubIssues, _>(&database, "worker", 3, raw, "message-1", &done)
            .await
            .expect("limit delivery");
    assert_eq!(result, ProcessResult::Parked);
    assert_eq!(done.calls.load(Ordering::SeqCst), 0);
    let state: i64 = sqlx::query_scalar("SELECT count(*) FROM test_state")
        .fetch_one(database.pool())
        .await
        .expect("rolled-back effects");
    assert_eq!(state, 0);
}
