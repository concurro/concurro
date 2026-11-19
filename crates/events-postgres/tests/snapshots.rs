//! Revision ordering under a consumer's transaction.

use chrono::Utc;
use concurro_contracts::{Contract, Envelope, EventId, EventKind, Timestamp};
use concurro_db::Database;
use concurro_events_postgres::{apply_snapshot, migrate};
use concurro_testkit::PostgresContainer;
use serde::Serialize;

/// Minimal state snapshot payload for storage checks.
#[derive(Clone, Serialize)]
struct StateSnapshot;

impl Contract for StateSnapshot {
    const FAMILY: &'static str = "task.state";
    const VERSION: u32 = 1;
    const KIND: EventKind = EventKind::Snapshot;

    fn event_type(&self) -> String {
        "task.state".to_owned()
    }

    fn subject(&self) -> String {
        "task.state".to_owned()
    }
}

/// Stale revisions skip and a rolled-back revision is not retained.
#[tokio::test]
async fn highest_revision_is_transactional() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    let mut snapshot = Envelope {
        id: EventId::mint(),
        event_type: "task.state".to_owned(),
        version: 1,
        kind: EventKind::Snapshot,
        producer: "test".to_owned(),
        occurred_at: Timestamp(Utc::now()),
        entity: Some("task-1".to_owned()),
        revision: Some(2),
        causation: None,
        data: StateSnapshot,
    };
    let mut uow = database.begin().await.expect("transaction");
    assert!(
        apply_snapshot(&mut uow, "worker", &snapshot)
            .await
            .expect("revision 2")
    );
    uow.commit().await.expect("commit");
    let mut uow = database.begin().await.expect("transaction");
    assert!(
        !apply_snapshot(&mut uow, "worker", &snapshot)
            .await
            .expect("equal revision")
    );
    snapshot.revision = Some(1);
    assert!(
        !apply_snapshot(&mut uow, "worker", &snapshot)
            .await
            .expect("stale revision")
    );
    snapshot.revision = Some(3);
    assert!(
        apply_snapshot(&mut uow, "worker", &snapshot)
            .await
            .expect("revision 3")
    );
    uow.rollback().await.expect("rollback");
    let mut uow = database.begin().await.expect("transaction");
    assert!(
        apply_snapshot(&mut uow, "worker", &snapshot)
            .await
            .expect("revision 3 again")
    );
    uow.commit().await.expect("commit");
}
