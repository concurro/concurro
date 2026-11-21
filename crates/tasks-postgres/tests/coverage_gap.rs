//! Persistent unresolved source receipts for ingress-gap recovery.

use chrono::Utc;
use concurro_db::Database;
use concurro_tasks_postgres::{
    fail_coverage_gap, migrate, pending_coverage_gaps, record_coverage_gap,
    record_coverage_source_failure, resolve_coverage_source_failure,
    unresolved_coverage_source_count,
};
use concurro_testkit::PostgresContainer;
use uuid::Uuid;

/// An ambiguous source survives a scan failure and clears only after authenticated recovery.
#[tokio::test]
async fn ambiguous_source_remains_pending_until_resolved() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    let id = Uuid::now_v7();
    let mut work = database.begin().await.expect("transaction");
    record_coverage_gap(&mut work, id, Utc::now(), Utc::now(), 42)
        .await
        .expect("coverage gap");
    work.commit().await.expect("commit");
    record_coverage_source_failure(
        &database,
        id,
        8,
        "edited_command",
        "123:time",
        "editor unknown",
    )
    .await
    .expect("unresolved source");
    assert_eq!(
        unresolved_coverage_source_count(&database, id, 8)
            .await
            .expect("count"),
        1
    );
    fail_coverage_gap(&database, id, "source pending")
        .await
        .expect("backoff");
    assert!(
        pending_coverage_gaps(&database)
            .await
            .expect("pending")
            .is_empty()
    );
    resolve_coverage_source_failure(&database, id, 8, "edited_command", "123:time")
        .await
        .expect("resolved source");
    assert_eq!(
        unresolved_coverage_source_count(&database, id, 8)
            .await
            .expect("count"),
        0
    );
}
