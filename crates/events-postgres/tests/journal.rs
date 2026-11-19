//! Retained outbox positions, replay, and coverage barrier tests.

use chrono::{DateTime, Utc};
use concurro_contracts::{CoverageGap, Envelope, EventId, GithubIssues};
use concurro_db::Database;
use concurro_events::{ContinuityDecision, JournalPosition, ReplayRequest};
use concurro_events_postgres::{
    ForwarderLease, Outbox, capture_barrier, consumer_coverage_applied, migrate, observe_consumer,
    replay_retained, resolve_consumer_loss, take_ready_barrier,
};
use concurro_testkit::PostgresContainer;
use std::time::Duration;

/// A large replay releases the writer lock between batches and resumes its cursor.
#[tokio::test]
async fn replay_batches_allow_writers_and_resume() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 4)
        .await
        .expect("database");
    migrate(&database).await.expect("events migration");
    let created = "2026-11-19T01:02:03.123456789Z";
    let generation: i64 = sqlx::query_scalar(
        "INSERT INTO events.stream_generations (stream_created) VALUES ($1) RETURNING id",
    )
    .bind(created)
    .fetch_one(database.pool())
    .await
    .expect("generation");
    sqlx::query(
        "INSERT INTO events.outbox \
         (id, subject, type, version, message, wire_bytes, published_at, stream_generation, stream_sequence) \
         SELECT gen_random_uuid(), 'github.issues', 'github.issues.opened', 1, '{}'::jsonb, \
         decode('7b7d', 'hex'), now(), $1, n FROM generate_series(1, 2000) AS n",
    )
    .bind(generation)
    .execute(database.pool())
    .await
    .expect("journal rows");
    let request = ReplayRequest {
        floor: JournalPosition {
            stream_created: created.to_owned(),
            sequence: 0,
        },
        target_created: "target-b".to_owned(),
    };
    let replay_database = database.clone();
    let replay_request = request.clone();
    let replay = tokio::spawn(async move {
        replay_retained(&replay_database, "service", &replay_request, 1).await
    });
    let forwarder_database = database.clone();
    let forwarder = tokio::spawn(async move {
        let mut lease = ForwarderLease::try_acquire(&forwarder_database)
            .await
            .expect("forwarder lease")
            .expect("sole forwarder");
        let sequence: i64 = loop {
            let candidate: Option<i64> = sqlx::query_scalar(
                "SELECT sequence FROM events.outbox WHERE published_at IS NULL \
                 AND stream_generation = $1 ORDER BY sequence LIMIT 1",
            )
            .bind(generation)
            .fetch_optional(forwarder_database.pool())
            .await
            .expect("requeued row");
            if let Some(sequence) = candidate {
                break sequence;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        };
        lease
            .mark_published(sequence, Some((created, 3000)))
            .await
            .expect("concurrent republish");
        sequence
    });
    let cursor: i64 = tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let value: Option<i64> = sqlx::query_scalar(
                "SELECT cursor_sequence FROM events.replay_requests WHERE completed_at IS NULL",
            )
            .fetch_optional(database.pool())
            .await
            .expect("cursor");
            if let Some(value) = value.filter(|value| *value > 0 && *value < 2000) {
                break value;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("replay in progress");
    let outbox = Outbox::new(1024 * 1024, vec!["github.>".to_owned()]);
    let event: Envelope<GithubIssues> = serde_json::from_slice(include_bytes!(
        "../../../contracts/fixtures/github.issues.v1/valid/opened.json"
    ))
    .expect("fixture");
    tokio::time::timeout(Duration::from_secs(2), async {
        let mut uow = database.begin().await.expect("writer");
        outbox.append(&mut uow, &event).await.expect("append");
        uow.commit().await.expect("writer commit");
    })
    .await
    .expect("replay released the outbox lock");
    let republished = tokio::time::timeout(Duration::from_secs(2), forwarder)
        .await
        .expect("forwarder advanced between replay batches")
        .expect("forwarder task");
    replay.abort();
    let _ = replay.await;
    let persisted: i64 = sqlx::query_scalar(
        "SELECT cursor_sequence FROM events.replay_requests WHERE completed_at IS NULL",
    )
    .fetch_one(database.pool())
    .await
    .expect("persisted cursor");
    assert!(persisted >= cursor);
    replay_retained(&database, "service", &request, 100)
        .await
        .expect("resume replay");
    let unpublished: i64 =
        sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE published_at IS NULL")
            .fetch_one(database.pool())
            .await
            .expect("unpublished count");
    assert_eq!(unpublished, 2000);
    let republished_at: Option<chrono::DateTime<Utc>> =
        sqlx::query_scalar("SELECT published_at FROM events.outbox WHERE sequence = $1")
            .bind(republished)
            .fetch_one(database.pool())
            .await
            .expect("republished row");
    assert!(
        republished_at.is_some(),
        "replay must not chase the row's new position"
    );
}

/// A second stream loss restarts a partial replay even with the same old consumer floor.
#[tokio::test]
async fn partial_replay_restarts_for_new_target_stream() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 4)
        .await
        .expect("database");
    migrate(&database).await.expect("events migration");
    let old = "2026-11-19T01:02:03.123456789Z";
    let generation: i64 = sqlx::query_scalar(
        "INSERT INTO events.stream_generations (stream_created) VALUES ($1) RETURNING id",
    )
    .bind(old)
    .fetch_one(database.pool())
    .await
    .expect("old generation");
    sqlx::query(
        "INSERT INTO events.outbox \
         (id, subject, type, version, message, wire_bytes, published_at, stream_generation, stream_sequence) \
         SELECT gen_random_uuid(), 'github.issues', 'github.issues.opened', 1, '{}'::jsonb, \
         decode('7b7d', 'hex'), now(), $1, n FROM generate_series(1, 2000) AS n",
    )
    .bind(generation)
    .execute(database.pool())
    .await
    .expect("journal rows");
    let request = ReplayRequest {
        floor: JournalPosition {
            stream_created: old.to_owned(),
            sequence: 0,
        },
        target_created: "target-b".to_owned(),
    };
    let replay_database = database.clone();
    let replay_request = request.clone();
    let replay = tokio::spawn(async move {
        replay_retained(&replay_database, "service", &replay_request, 1).await
    });
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            let cursor: Option<i64> = sqlx::query_scalar(
                "SELECT cursor_sequence FROM events.replay_requests WHERE target_created = 'target-b'",
            )
            .fetch_optional(database.pool())
            .await
            .expect("partial cursor");
            if cursor.is_some_and(|cursor| cursor > 0 && cursor < 2000) {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("partial replay");
    replay.abort();
    let _ = replay.await;
    let first: i64 = sqlx::query_scalar(
        "SELECT sequence FROM events.outbox WHERE published_at IS NULL ORDER BY sequence LIMIT 1",
    )
    .fetch_one(database.pool())
    .await
    .expect("first replayed row");
    let mut forwarder = ForwarderLease::try_acquire(&database)
        .await
        .expect("lease")
        .expect("sole forwarder");
    forwarder
        .mark_published(first, Some(("target-b", 1)))
        .await
        .expect("published to first target");
    let outbox = Outbox::new(1024 * 1024, vec!["github.>".to_owned()]);
    let mut added: Envelope<GithubIssues> = serde_json::from_slice(include_bytes!(
        "../../../contracts/fixtures/github.issues.v1/valid/opened.json"
    ))
    .expect("fixture");
    added.id = EventId::mint();
    let mut producer = database.begin().await.expect("later producer");
    let newest = outbox
        .append(&mut producer, &added)
        .await
        .expect("row beyond first replay horizon");
    producer.commit().await.expect("later producer commit");
    forwarder
        .mark_published(newest, Some(("target-b", 2)))
        .await
        .expect("later row published to first target");
    let next = ReplayRequest {
        target_created: "target-c".to_owned(),
        ..request
    };
    replay_retained(&database, "service", &next, 200)
        .await
        .expect("restart for second target");
    let published_at: Option<chrono::DateTime<Utc>> =
        sqlx::query_scalar("SELECT published_at FROM events.outbox WHERE sequence = $1")
            .bind(first)
            .fetch_one(database.pool())
            .await
            .expect("first row status");
    assert!(
        published_at.is_none(),
        "target C must requeue the row lost with B"
    );
    let newest_published_at: Option<chrono::DateTime<Utc>> =
        sqlx::query_scalar("SELECT published_at FROM events.outbox WHERE sequence = $1")
            .bind(newest)
            .fetch_one(database.pool())
            .await
            .expect("newest row status");
    assert!(
        newest_published_at.is_none(),
        "target C must refresh the horizon beyond target B's replay"
    );
    let jobs: Vec<(String, bool)> = sqlx::query_as(
        "SELECT target_created, completed_at IS NOT NULL FROM events.replay_requests ORDER BY id",
    )
    .fetch_all(database.pool())
    .await
    .expect("replay jobs");
    assert_eq!(
        jobs,
        vec![
            ("target-b".to_owned(), false),
            ("target-c".to_owned(), true)
        ]
    );
}

/// A replay re-queue blocks coverage until all earlier rows are published again.
#[tokio::test]
async fn replayed_rows_block_coverage_barrier() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("events migration");
    let outbox = Outbox::new(
        1024 * 1024,
        vec!["github.>".to_owned(), "ingress.>".to_owned()],
    );
    let gap: Envelope<CoverageGap> = serde_json::from_slice(include_bytes!(
        "../../../contracts/fixtures/ingress.coverage.gap.v1/valid/gap.json"
    ))
    .expect("gap fixture");
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let event: Envelope<GithubIssues> = serde_json::from_slice(raw).expect("fixture");
    let mut producer = database.begin().await.expect("producer");
    let first = outbox.append(&mut producer, &gap).await.expect("gap row");
    let second = outbox
        .append(&mut producer, &event)
        .await
        .expect("delivery row");
    producer.commit().await.expect("producer commit");
    let mut lease = ForwarderLease::try_acquire(&database)
        .await
        .expect("lease")
        .expect("sole forwarder");
    let created = "2026-11-19T00:00:00Z";
    lease
        .mark_published(first, Some((created, 1)))
        .await
        .expect("initial gap publish");
    lease
        .mark_published(second, Some((created, 2)))
        .await
        .expect("initial delivery publish");
    let covered_until: DateTime<Utc> = "2026-11-19T00:01:00Z".parse().expect("time");
    let mut capture = database.begin().await.expect("capture transaction");
    assert_eq!(
        capture_barrier(&mut capture, covered_until)
            .await
            .expect("capture"),
        second
    );
    capture.commit().await.expect("capture commit");
    let answer = replay_retained(
        &database,
        "service",
        &ReplayRequest {
            floor: JournalPosition {
                stream_created: created.to_owned(),
                sequence: 0,
            },
            target_created: "replacement".to_owned(),
        },
        1,
    )
    .await
    .expect("replay");
    assert_eq!(answer.replayed_through.expect("last row").sequence, 2);
    let mut finalize = database.begin().await.expect("finalize transaction");
    assert_eq!(
        take_ready_barrier(&mut finalize).await.expect("blocked"),
        None
    );
    finalize.commit().await.expect("blocked commit");
    lease
        .mark_published(second, Some((created, 3)))
        .await
        .expect("delivery republishes while gap fails");
    let mut finalize = database.begin().await.expect("second finalize");
    assert_eq!(
        take_ready_barrier(&mut finalize)
            .await
            .expect("still blocked"),
        None
    );
    finalize.commit().await.expect("second blocked commit");
    lease
        .mark_published(first, Some((created, 4)))
        .await
        .expect("gap finally publishes");
    let mut finalize = database.begin().await.expect("third finalize");
    assert_eq!(
        take_ready_barrier(&mut finalize).await.expect("ready"),
        Some(covered_until)
    );
    finalize.commit().await.expect("ready commit");
}

/// Replay follows acknowledged stream positions and reports pruned history.
#[tokio::test]
async fn replay_orders_by_stream_and_reports_unrecoverable_history() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("events migration");
    let outbox = Outbox::new(1024 * 1024, vec!["github.>".to_owned()]);
    let raw = include_bytes!("../../../contracts/fixtures/github.issues.v1/valid/opened.json");
    let mut event: Envelope<GithubIssues> = serde_json::from_slice(raw).expect("fixture");
    let mut producer = database.begin().await.expect("producer");
    let older_row = outbox
        .append(&mut producer, &event)
        .await
        .expect("older row");
    event.id = EventId::mint();
    let newer_row = outbox
        .append(&mut producer, &event)
        .await
        .expect("newer row");
    producer.commit().await.expect("producer commit");
    let mut lease = ForwarderLease::try_acquire(&database)
        .await
        .expect("lease")
        .expect("sole forwarder");
    let created = "2026-11-19T00:00:00Z";
    lease
        .mark_published(older_row, Some((created, 5)))
        .await
        .expect("late old row");
    lease
        .mark_published(newer_row, Some((created, 4)))
        .await
        .expect("early new row");
    let answer = replay_retained(
        &database,
        "service",
        &ReplayRequest {
            floor: JournalPosition {
                stream_created: created.to_owned(),
                sequence: 4,
            },
            target_created: "replacement".to_owned(),
        },
        100,
    )
    .await
    .expect("position replay");
    assert_eq!(
        answer.replayed_through.expect("replay position").sequence,
        5
    );
    assert_eq!(answer.unrecoverable, None);
    let unpublished: Vec<i64> = sqlx::query_scalar(
        "SELECT sequence FROM events.outbox WHERE published_at IS NULL ORDER BY sequence",
    )
    .fetch_all(database.pool())
    .await
    .expect("requeued rows");
    assert_eq!(unpublished, vec![older_row]);
    sqlx::query(
        "UPDATE events.outbox SET published_at = now() - interval '2 days' WHERE sequence = $1",
    )
    .bind(newer_row)
    .execute(database.pool())
    .await
    .expect("age published row");
    assert_eq!(lease.purge_published(86_400).await.expect("prune"), 1);
    let answer = replay_retained(
        &database,
        "service",
        &ReplayRequest {
            floor: JournalPosition {
                stream_created: created.to_owned(),
                sequence: 3,
            },
            target_created: "replacement".to_owned(),
        },
        100,
    )
    .await
    .expect("pruned replay");
    let oldest_created: DateTime<Utc> =
        sqlx::query_scalar("SELECT created_at FROM events.outbox WHERE sequence = $1")
            .bind(older_row)
            .fetch_one(database.pool())
            .await
            .expect("oldest retained event");
    assert_eq!(
        answer.unrecoverable.expect("gap").before.as_deref(),
        Some(oldest_created.to_rfc3339().as_str())
    );
}

/// A durable floor survives stream replacement until explicit recovery resolves it.
#[tokio::test]
async fn consumer_loss_keeps_old_floor_and_blocks_coverage() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("events migration");
    assert_eq!(
        observe_consumer(&database, "before-ack", "first", 1, 0)
            .await
            .expect("floor before first acknowledgement"),
        ContinuityDecision::Initialize(JournalPosition {
            stream_created: "first".to_owned(),
            sequence: 0,
        })
    );
    assert_eq!(
        observe_consumer(&database, "before-ack", "replacement", 1, 0)
            .await
            .expect("replacement before first acknowledgement"),
        ContinuityDecision::Lost(JournalPosition {
            stream_created: "first".to_owned(),
            sequence: 0,
        })
    );
    assert_eq!(
        observe_consumer(&database, "service", "old", 1, 0)
            .await
            .expect("initial floor"),
        ContinuityDecision::Initialize(JournalPosition {
            stream_created: "old".to_owned(),
            sequence: 0,
        })
    );
    assert_eq!(
        observe_consumer(&database, "service", "old", 1, 2)
            .await
            .expect("advance floor"),
        ContinuityDecision::Advance(JournalPosition {
            stream_created: "old".to_owned(),
            sequence: 2,
        })
    );
    let lost = observe_consumer(&database, "service", "new", 1, 0)
        .await
        .expect("new stream loss");
    assert_eq!(
        lost,
        ContinuityDecision::Lost(JournalPosition {
            stream_created: "old".to_owned(),
            sequence: 2,
        })
    );
    let current = JournalPosition {
        stream_created: "new".to_owned(),
        sequence: 1,
    };
    assert!(
        !consumer_coverage_applied(&database, "service", &current, &current)
            .await
            .expect("loss blocks coverage")
    );
    resolve_consumer_loss(&database, "service", &current)
        .await
        .expect("resolved loss");
    assert!(
        consumer_coverage_applied(&database, "service", &current, &current)
            .await
            .expect("coverage after recovery")
    );
    assert!(
        !consumer_coverage_applied(
            &database,
            "service",
            &JournalPosition {
                stream_created: "old".to_owned(),
                sequence: 2,
            },
            &current
        )
        .await
        .expect("old coverage stays stale")
    );
}
