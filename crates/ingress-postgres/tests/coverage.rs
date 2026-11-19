//! Coverage gaps wait behind every captured unpublished outbox row.

use concurro_db::Database;
use concurro_events_postgres::{ForwarderLease, Outbox, migrate as migrate_events};
use concurro_ingress_postgres::{
    CoverageError, SignedDelivery, advance_coverage, database_time, declare_gap, finalize_coverage,
    migrate, record_delivery, record_delivery_in, status,
};
use concurro_testkit::PostgresContainer;
use std::time::Duration;

/// Initial coverage emits a gap first and advances only after its publish ack.
#[tokio::test]
async fn initial_gap_precedes_coverage_fact() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate_events(&database).await.expect("events schema");
    migrate(&database).await.expect("ingress schema");
    let outbox = Outbox::new(1024 * 1024, vec!["ingress.>".to_owned()]);
    let started = database_time(&database).await.expect("scan start");
    assert_eq!(
        advance_coverage(&database, &outbox, started, true, &[42, 43])
            .await
            .expect("advance"),
        Some(started)
    );
    assert!(
        !finalize_coverage(&database, &outbox)
            .await
            .expect("unpublished gaps")
    );
    let gaps: Vec<(i64, String)> =
        sqlx::query_as("SELECT sequence, type FROM events.outbox ORDER BY sequence")
            .fetch_all(database.pool())
            .await
            .expect("gap rows");
    assert_eq!(gaps.len(), 2);
    assert!(gaps.iter().all(|(_, kind)| kind == "ingress.coverage.gap"));
    let mut lease = ForwarderLease::try_acquire(&database)
        .await
        .expect("lease")
        .expect("forwarder");
    for (index, (sequence, _)) in gaps.iter().enumerate() {
        lease
            .mark_published(*sequence, Some(("2026-11-19T00:00:00Z", index as i64 + 1)))
            .await
            .expect("publish gap");
    }
    assert!(
        finalize_coverage(&database, &outbox)
            .await
            .expect("coverage fact")
    );
    let final_type: String =
        sqlx::query_scalar("SELECT type FROM events.outbox ORDER BY sequence DESC LIMIT 1")
            .fetch_one(database.pool())
            .await
            .expect("last fact");
    assert_eq!(final_type, "ingress.coverage.advanced");
}

/// An unmappable receipt stops initial coverage until it is repaired.
#[tokio::test]
async fn unmappable_delivery_blocks_initial_coverage() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate_events(&database).await.expect("events schema");
    migrate(&database).await.expect("ingress schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec!["github.>".to_owned(), "ingress.>".to_owned()],
    );
    record_delivery(
        &database,
        &outbox,
        &SignedDelivery {
            guid: "broken",
            event: "issues",
            payload: b"{broken",
            installations: &[42],
        },
    )
    .await
    .expect("unmappable receipt");
    let started = database_time(&database).await.expect("scan start");
    assert_eq!(
        advance_coverage(&database, &outbox, started, true, &[42])
            .await
            .expect("blocked advance"),
        None
    );
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM events.outbox")
        .fetch_one(database.pool())
        .await
        .expect("gap count");
    assert_eq!(count, 0);
    let expired_at = started + chrono::Duration::days(4);
    assert_eq!(
        advance_coverage(&database, &outbox, expired_at, false, &[42])
            .await
            .expect("expired initial scan"),
        Some(expired_at)
    );
    let gaps: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM events.outbox WHERE type = 'ingress.coverage.gap'",
    )
    .fetch_one(database.pool())
    .await
    .expect("gap count");
    assert_eq!(gaps, 1);
    assert_eq!(
        status(&database, expired_at)
            .await
            .expect("status")
            .covered_until,
        Some(expired_at)
    );
    let next = expired_at + chrono::Duration::minutes(5);
    assert_eq!(
        advance_coverage(&database, &outbox, next, true, &[42])
            .await
            .expect("after expired gap"),
        Some(next)
    );
}

/// An unresolved scan emits an expired gap once the covered period leaves history.
#[tokio::test]
async fn unresolved_recovery_expires_to_a_gap() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate_events(&database).await.expect("events schema");
    migrate(&database).await.expect("ingress schema");
    let outbox = Outbox::new(1024 * 1024, vec!["ingress.>".to_owned()]);
    let started = database_time(&database).await.expect("scan start");
    advance_coverage(&database, &outbox, started, true, &[42])
        .await
        .expect("initial coverage");
    assert_eq!(
        advance_coverage(
            &database,
            &outbox,
            started + chrono::Duration::days(2),
            false,
            &[42],
        )
        .await
        .expect("unresolved scan"),
        None
    );
    let expired_at = started + chrono::Duration::days(4);
    assert_eq!(
        advance_coverage(&database, &outbox, expired_at, false, &[42])
            .await
            .expect("expired scan"),
        Some(expired_at)
    );
    let health = status(&database, started).await.expect("status");
    assert_eq!(health.covered_until, Some(expired_at));
    let gaps: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM events.outbox WHERE type = 'ingress.coverage.gap'",
    )
    .fetch_one(database.pool())
    .await
    .expect("gap count");
    assert_eq!(gaps, 2);
}

/// A writer inside the receipt lock is covered; one entering later is not.
#[tokio::test]
async fn coverage_and_receipts_follow_lock_order() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 4)
        .await
        .expect("database");
    migrate_events(&database).await.expect("events schema");
    migrate(&database).await.expect("ingress schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec!["github.>".to_owned(), "ingress.>".to_owned()],
    );
    let raw = include_bytes!(
        "../../../contracts/fixtures/github.issue_comment.v1/raw/representative.json"
    );
    let mut first = database.begin().await.expect("first writer");
    record_delivery_in(
        &mut first,
        &outbox,
        &SignedDelivery {
            guid: "before-scan",
            event: "issue_comment",
            payload: raw,
            installations: &[7],
        },
    )
    .await
    .expect("first receipt");
    let started = database_time(&database).await.expect("scan clock");
    let scan_database = database.clone();
    let scan_outbox = outbox.clone();
    let scan = tokio::spawn(async move {
        advance_coverage(&scan_database, &scan_outbox, started, true, &[7]).await
    });
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert!(
        !scan.is_finished(),
        "coverage must wait for the first writer"
    );
    first.commit().await.expect("first receipt commit");
    assert_eq!(scan.await.expect("scan task").expect("scan"), Some(started));
    let next_scan = database_time(&database).await.expect("next scan clock");
    let mut gate = database.begin().await.expect("coverage row lock");
    sqlx::query("SELECT id FROM ingress.coverage WHERE id FOR UPDATE")
        .execute(gate.connection())
        .await
        .expect("hold coverage after its receipt lock");
    let scan_database = database.clone();
    let scan_outbox = outbox.clone();
    let scan = tokio::spawn(async move {
        advance_coverage(&scan_database, &scan_outbox, next_scan, true, &[7]).await
    });
    let mut scan_locked = false;
    for _ in 0..50 {
        scan_locked = sqlx::query_scalar(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE relation = 'ingress.deliveries'::regclass \
             AND mode = 'ShareLock' AND granted)",
        )
        .fetch_one(database.pool())
        .await
        .expect("coverage receipt lock");
        if scan_locked {
            break;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    assert!(scan_locked, "coverage must hold the receipt lock");
    let writer_database = database.clone();
    let writer_outbox = outbox.clone();
    let later = tokio::spawn(async move {
        record_delivery(
            &writer_database,
            &writer_outbox,
            &SignedDelivery {
                guid: "after-scan",
                event: "issue_comment",
                payload: raw,
                installations: &[7],
            },
        )
        .await
    });
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert!(
        !later.is_finished(),
        "writer must wait before its receipt lock"
    );
    gate.commit().await.expect("release coverage");
    assert_eq!(
        scan.await.expect("scan task").expect("scan"),
        Some(next_scan)
    );
    later.await.expect("writer task").expect("later receipt");
    let times: Vec<(String, chrono::DateTime<chrono::Utc>)> =
        sqlx::query_as("SELECT guid, received_at FROM ingress.deliveries ORDER BY received_at")
            .fetch_all(database.pool())
            .await
            .expect("receipt times");
    assert_eq!(times[0].0, "before-scan");
    assert!(times[0].1 <= started);
    assert_eq!(times[1].0, "after-scan");
    assert!(times[1].1 > next_scan);
}

/// An operator declaration emits one gap per installation and preserves coverage state.
#[tokio::test]
async fn declared_gap_is_a_durable_fact() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate_events(&database).await.expect("events schema");
    migrate(&database).await.expect("ingress schema");
    let outbox = Outbox::new(1024 * 1024, vec!["ingress.>".to_owned()]);
    let to = database_time(&database).await.expect("clock");
    let from = to - chrono::Duration::hours(2);
    std::assert_matches!(
        declare_gap(
            &database,
            &outbox,
            from,
            to + chrono::Duration::hours(1),
            &[42]
        )
        .await,
        Err(CoverageError::FutureInterval)
    );
    declare_gap(&database, &outbox, from, to, &[42, 43])
        .await
        .expect("declare gap");
    let health = status(&database, from).await.expect("status");
    assert_eq!(health.covered_until, Some(to));
    let gaps: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM events.outbox WHERE type = 'ingress.coverage.gap'",
    )
    .fetch_one(database.pool())
    .await
    .expect("gap count");
    assert_eq!(gaps, 2);
    let first_started: chrono::DateTime<chrono::Utc> =
        sqlx::query_scalar("SELECT first_started_at FROM ingress.coverage WHERE id")
            .fetch_one(database.pool())
            .await
            .expect("initial scan time");
    let gap_from: chrono::DateTime<chrono::Utc> = sqlx::query_scalar::<_, String>(
        "SELECT message->'data'->>'from' FROM events.outbox WHERE type = 'ingress.coverage.gap' LIMIT 1",
    )
    .fetch_one(database.pool())
    .await
    .expect("gap from")
    .parse()
    .expect("timestamp");
    assert_eq!(gap_from, first_started - chrono::Duration::days(3));
    assert!(
        !finalize_coverage(&database, &outbox)
            .await
            .expect("barrier pending")
    );
}
