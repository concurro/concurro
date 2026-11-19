//! Phase 1 outbox rows survive the phase 2 journal migration.

use std::{fs, path::PathBuf};

use concurro_contracts::{Envelope, GithubIssues};
use concurro_db::Database;
use concurro_events_postgres::{Outbox, migrate};
use concurro_testkit::PostgresContainer;
use sqlx::migrate::Migrator;

/// A previously published row is requeued for an acknowledged journal position.
#[tokio::test]
async fn phase_one_outbox_moves_forward_without_losing_wire_bytes() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let directory = std::env::temp_dir().join(format!(
        "concurro-phase1-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("clock")
            .as_nanos()
    ));
    fs::create_dir(&directory).expect("temporary migration directory");
    fs::copy(
        root.join("migrations/0001_events.sql"),
        directory.join("0001_events.sql"),
    )
    .expect("copy phase 1 migration");
    let phase_one = Migrator::new(directory.clone())
        .await
        .expect("phase 1 migrator");
    database
        .migrate_in_schema("events", &phase_one)
        .await
        .expect("phase 1 schema");
    fs::remove_dir_all(&directory).expect("remove temporary migration directory");

    let event: Envelope<GithubIssues> = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/github.issues.v1/valid/opened.json"
    ))
    .expect("fixture");
    let wire = serde_json::to_vec(&event).expect("wire bytes");
    let outbox = Outbox::new(1024 * 1024, vec!["github.>".to_owned()]);
    let mut uow = database.begin().await.expect("begin");
    outbox.append(&mut uow, &event).await.expect("append");
    uow.commit().await.expect("commit");
    sqlx::query("UPDATE events.outbox SET published_at = now() WHERE id = $1")
        .bind(event.id.0)
        .execute(database.pool())
        .await
        .expect("phase 1 publication");

    migrate(&database).await.expect("forward journal migration");
    let (published_at, stream_generation, stored_wire): (
        Option<chrono::DateTime<chrono::Utc>>,
        Option<i64>,
        Vec<u8>,
    ) = sqlx::query_as(
        "SELECT published_at, stream_generation, wire_bytes FROM events.outbox WHERE id = $1",
    )
    .bind(event.id.0)
    .fetch_one(database.pool())
    .await
    .expect("migrated row");
    assert!(published_at.is_none());
    assert!(stream_generation.is_none());
    assert_eq!(stored_wire, wire);
}
