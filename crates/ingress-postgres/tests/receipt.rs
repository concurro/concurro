//! Atomic signed-delivery receipt and size policy tests.

use concurro_db::Database;
use concurro_events_postgres::{Outbox, migrate as migrate_events};
use concurro_ingress_postgres::{
    ReceiptOutcome, RecordResult, SignedDelivery, database_time, get_delivery, migrate,
    record_delivery, record_delivery_in, remap_candidates, remap_delivery, status,
};
use concurro_testkit::PostgresContainer;

/// Receipt, compressed body, and mapped outbox event commit together.
#[tokio::test]
async fn receipt_is_atomic_and_deduplicated() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate_events(&database).await.expect("events schema");
    migrate(&database).await.expect("ingress schema");
    let raw = serde_json::to_vec(&serde_json::json!({
        "action": "opened",
        "installation": {"id": 42},
        "repository": {
            "id": 10, "full_name": "owner/repo", "owner": {"id": 11}
        },
        "sender": {"id": 12, "login": "writer", "type": "User"},
        "issue": {
            "number": 7, "title": "Test", "body": "Body", "state": "open",
            "state_reason": null,
            "user": {"id": 13, "login": "author", "type": "User"},
            "labels": [{"name": "concurro"}], "assignees": [],
            "created_at": "2026-11-19T00:00:00Z",
            "updated_at": "2026-11-19T00:00:00Z"
        }
    }))
    .expect("wire body");
    let outbox = Outbox::new(1024 * 1024, vec!["github.>".to_owned()]);
    let delivery = SignedDelivery {
        guid: "first-guid",
        event: "issues",
        payload: &raw,
        installations: &[42],
    };
    let mut rolled_back = database.begin().await.expect("transaction");
    assert_eq!(
        record_delivery_in(&mut rolled_back, &outbox, &delivery)
            .await
            .expect("tentative receipt"),
        RecordResult::Recorded(ReceiptOutcome::Published)
    );
    rolled_back.rollback().await.expect("rollback");
    assert!(
        get_delivery(&database, "first-guid")
            .await
            .expect("missing receipt")
            .is_none()
    );
    assert_eq!(
        record_delivery(&database, &outbox, &delivery)
            .await
            .expect("receipt"),
        RecordResult::Recorded(ReceiptOutcome::Published)
    );
    assert_eq!(
        record_delivery(&database, &outbox, &delivery)
            .await
            .expect("duplicate"),
        RecordResult::Duplicate
    );
    let stored = get_delivery(&database, "first-guid")
        .await
        .expect("read receipt")
        .expect("stored receipt");
    assert_eq!(stored.outcome, "published");
    assert_eq!(stored.mapping_revision, Some(1));
    assert_eq!(
        zstd::bulk::decompress(&stored.payload.expect("payload"), 25_000_000).expect("decompress"),
        raw
    );
    let events: i64 = sqlx::query_scalar("SELECT count(*) FROM events.outbox")
        .fetch_one(database.pool())
        .await
        .expect("event count");
    assert_eq!(events, 1);
    sqlx::query(
        "UPDATE ingress.deliveries SET event = 'pull_request', mapping_revision = 0 WHERE guid = 'first-guid'",
    )
    .execute(database.pool())
    .await
    .expect("older mapping revision");
    assert_eq!(
        remap_delivery(&database, &outbox, "first-guid", &[42])
            .await
            .expect("failed newer mapping"),
        RecordResult::Duplicate
    );
    let outcome: String =
        sqlx::query_scalar("SELECT outcome FROM ingress.deliveries WHERE guid = 'first-guid'")
            .fetch_one(database.pool())
            .await
            .expect("preserved outcome");
    assert_eq!(outcome, "published");
}

/// Oversized mapped events keep a signed receipt and no outbox event.
#[tokio::test]
async fn oversized_mapped_event_is_unmappable() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate_events(&database).await.expect("events schema");
    migrate(&database).await.expect("ingress schema");
    let raw = serde_json::to_vec(&serde_json::json!({
        "action": "opened",
        "installation": {"id": 42},
        "repository": {
            "id": 10, "full_name": "owner/repo", "owner": {"id": 11}
        },
        "sender": {"id": 12, "login": "writer", "type": "User"},
        "issue": {
            "number": 7, "title": "Test", "body": "Body", "state": "open",
            "user": {"id": 13, "login": "author", "type": "User"},
            "labels": [], "assignees": [],
            "created_at": "2026-11-19T00:00:00Z",
            "updated_at": "2026-11-19T00:00:00Z"
        }
    }))
    .expect("wire body");
    let outbox = Outbox::new(100, vec!["github.>".to_owned()]);
    assert_eq!(
        record_delivery(
            &database,
            &outbox,
            &SignedDelivery {
                guid: "large-guid",
                event: "issues",
                payload: &raw,
                installations: &[42],
            }
        )
        .await
        .expect("record large"),
        RecordResult::Recorded(ReceiptOutcome::Unmappable)
    );
    let state: (String, Option<i32>, Option<String>) = sqlx::query_as(
        "SELECT outcome, mapping_revision, error FROM ingress.deliveries WHERE guid = 'large-guid'",
    )
    .fetch_one(database.pool())
    .await
    .expect("large receipt");
    assert_eq!(
        state,
        (
            "unmappable".to_owned(),
            None,
            Some("event_too_large".to_owned())
        )
    );
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM events.outbox")
        .fetch_one(database.pool())
        .await
        .expect("outbox count");
    assert_eq!(count, 0);
    let from = database_time(&database).await.expect("clock") - chrono::Duration::hours(1);
    let to = from + chrono::Duration::hours(2);
    let candidates = remap_candidates(&database, from, to, Some("issues"), 1, None, 10)
        .await
        .expect("remap selection");
    assert_eq!(candidates.len(), 1);
    assert_eq!(candidates[0].guid, "large-guid");
    let repaired = Outbox::new(1024 * 1024, vec!["github.>".to_owned()]);
    assert_eq!(
        remap_delivery(&database, &repaired, "large-guid", &[99])
            .await
            .expect("other installation"),
        RecordResult::Recorded(ReceiptOutcome::OtherInstallation)
    );
    let settled: (String, Option<String>) =
        sqlx::query_as("SELECT outcome, error FROM ingress.deliveries WHERE guid = 'large-guid'")
            .fetch_one(database.pool())
            .await
            .expect("settled outcome");
    assert_eq!(settled, ("other_installation".to_owned(), None));
    assert_eq!(
        remap_delivery(&database, &repaired, "large-guid", &[42])
            .await
            .expect("remap after cap increase"),
        RecordResult::Recorded(ReceiptOutcome::Published)
    );
    let health = status(&database, from).await.expect("operator status");
    assert_eq!(health.unmappable_count, 0);
    assert_eq!(
        remap_delivery(&database, &repaired, "large-guid", &[42])
            .await
            .expect("repeat remap"),
        RecordResult::Duplicate
    );
}
