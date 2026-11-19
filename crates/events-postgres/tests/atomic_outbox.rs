//! The producer's state and outbox use one Postgres commit boundary.

use std::assert_matches;

use concurro_contracts::{Envelope, EventId, GithubIssues};
use concurro_db::Database;
use concurro_events::publish_size;
use concurro_events_postgres::{Outbox, OutboxError, migrate};
use concurro_testkit::{PostgresContainer, TestState};
use sqlx::migrate::Migrator;
use sqlx::types::Json;

static SERVICE_MIGRATIONS: Migrator = sqlx::migrate!("tests/service_migrations");
static FAIL_MIGRATIONS: Migrator = sqlx::migrate!("tests/fail_migrations");
static SLEEP_MIGRATIONS: Migrator = sqlx::migrate!("tests/sleep_migrations");

/// Escaped NUL text stays in the authoritative bytes when jsonb cannot store it.
#[tokio::test]
async fn nul_in_wire_message_does_not_abort_outbox() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 1)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    let fixture = include_str!(
        "../../../contracts/fixtures/github.issues.v1/valid/edited_null_optional.json"
    );
    let mut event: Envelope<GithubIssues> = serde_json::from_str(fixture).expect("fixture");
    event.id = EventId::mint();
    event.data.issue.body = Some("contains \u{0} marker".to_owned());
    let wire = serde_json::to_vec(&event).expect("wire bytes");
    let outbox = Outbox::new(1024 * 1024, vec!["github.>".to_owned()]);
    let mut uow = database.begin().await.expect("begin");
    outbox.append(&mut uow, &event).await.expect("append");
    uow.commit().await.expect("commit");
    let stored: (Option<Json<serde_json::Value>>, Vec<u8>) =
        sqlx::query_as("SELECT message, wire_bytes FROM events.outbox WHERE id = $1")
            .bind(event.id.0)
            .fetch_one(database.pool())
            .await
            .expect("stored");
    assert!(stored.0.is_none());
    assert_eq!(stored.1, wire);
}

/// A rollback removes both the state row and event; a commit retains both.
#[tokio::test]
async fn state_and_outbox_commit_together() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 1)
        .await
        .expect("database");
    migrate(&database).await.expect("events schema");
    migrate(&database).await.expect("repeatable migration");
    let migration_table: Option<String> =
        sqlx::query_scalar("SELECT to_regclass('events._sqlx_migrations')::text")
            .fetch_one(database.pool())
            .await
            .expect("migration table");
    assert_eq!(migration_table.as_deref(), Some("events._sqlx_migrations"));
    let path: String = sqlx::query_scalar("SHOW search_path")
        .fetch_one(database.pool())
        .await
        .expect("search path");
    assert_eq!(path, "\"$user\", public");
    TestState::migrate(&database).await.expect("state schema");
    let fixture = include_str!(
        "../../../contracts/fixtures/github.issues.v1/valid/edited_null_optional.json"
    );
    let event: Envelope<GithubIssues> = serde_json::from_str(fixture).expect("fixture");
    let outbox = Outbox::new(1024 * 1024, vec!["github.>".to_owned()]);

    let mut rolled_back = database.begin().await.expect("begin rollback");
    TestState::insert(&mut rolled_back, 1)
        .await
        .expect("state insert");
    outbox
        .append(&mut rolled_back, &event)
        .await
        .expect("outbox append");
    rolled_back.rollback().await.expect("rollback");
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM test_state")
        .fetch_one(database.pool())
        .await
        .expect("state count");
    assert_eq!(count, 0);
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM events.outbox")
        .fetch_one(database.pool())
        .await
        .expect("outbox count");
    assert_eq!(count, 0);

    let mut committed = database.begin().await.expect("begin commit");
    TestState::insert(&mut committed, 2)
        .await
        .expect("state insert");
    outbox
        .append(&mut committed, &event)
        .await
        .expect("outbox append");
    committed.commit().await.expect("commit");
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM test_state")
        .fetch_one(database.pool())
        .await
        .expect("state count");
    assert_eq!(count, 1);
    let stored: (String, String, i32, Json<serde_json::Value>, Vec<u8>) = sqlx::query_as(
        "SELECT subject, type, version, message, wire_bytes FROM events.outbox WHERE id = $1",
    )
    .bind(event.id.0)
    .fetch_one(database.pool())
    .await
    .expect("stored event");
    assert_eq!(stored.0, event.subject());
    assert_eq!(stored.1, event.event_type);
    assert_eq!(stored.2, event.version as i32);
    assert_eq!(
        stored.3.0,
        serde_json::to_value(&event).expect("event value")
    );
    assert_eq!(stored.4, serde_json::to_vec(&event).expect("wire bytes"));

    let mut admissions = database.begin().await.expect("admission transaction");
    let outside = Outbox::new(1024 * 1024, vec!["tasks.>".to_owned()]);
    assert_matches!(
        outside.append(&mut admissions, &event).await,
        Err(OutboxError::SubjectOutsideStream(_))
    );
    let small = Outbox::new(1, vec!["github.>".to_owned()]);
    assert_matches!(
        small.append(&mut admissions, &event).await,
        Err(OutboxError::MessageTooLarge { .. })
    );
    let mut at_boundary = event.clone();
    at_boundary.id = EventId::mint();
    let body = serde_json::to_vec(&at_boundary).expect("wire bytes");
    let size = publish_size(
        body.len(),
        &format!("{}:{}", at_boundary.id.0, at_boundary.version),
    );
    let mut boundary_check = admissions.savepoint().await.expect("boundary savepoint");
    let exact = Outbox::new(size, vec!["github.>".to_owned()]);
    exact
        .append(&mut boundary_check, &at_boundary)
        .await
        .expect("header-inclusive boundary admits event");
    let one_less = Outbox::new(size - 1, vec!["github.>".to_owned()]);
    let mut too_large = at_boundary.clone();
    too_large.id = EventId::mint();
    assert_matches!(
        one_less.append(&mut boundary_check, &too_large).await,
        Err(OutboxError::MessageTooLarge { size: actual, max }) if actual == size && max == size - 1
    );
    boundary_check.rollback().await.expect("boundary rollback");
    let mut invalid = event.clone();
    invalid.entity = Some("issue:1".to_owned());
    assert_matches!(
        outbox.append(&mut admissions, &invalid).await,
        Err(OutboxError::Contract(_))
    );
    assert_matches!(
        outbox.append(&mut admissions, &event).await,
        Err(OutboxError::Duplicate)
    );
    TestState::insert(&mut admissions, 5)
        .await
        .expect("write after duplicate");
    admissions.commit().await.expect("commit after duplicate");
    let after_duplicate: i64 = sqlx::query_scalar("SELECT count(*) FROM test_state WHERE id = 5")
        .fetch_one(database.pool())
        .await
        .expect("state after duplicate");
    assert_eq!(after_duplicate, 1);
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM events.outbox")
        .fetch_one(database.pool())
        .await
        .expect("outbox count");
    assert_eq!(count, 1);

    let mut outer = database.begin().await.expect("begin outer");
    TestState::insert(&mut outer, 6)
        .await
        .expect("outer state before savepoint");
    let mut nested_event = event.clone();
    nested_event.id = EventId::mint();
    let mut nested = outer.savepoint().await.expect("savepoint");
    TestState::insert(&mut nested, 3)
        .await
        .expect("nested state");
    outbox
        .append(&mut nested, &nested_event)
        .await
        .expect("nested event");
    nested.rollback().await.expect("savepoint rollback");
    TestState::insert(&mut outer, 4)
        .await
        .expect("outer still writable");
    outer.commit().await.expect("outer commit");
    let state_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM test_state WHERE id IN (3, 4, 6)")
            .fetch_one(database.pool())
            .await
            .expect("state count after savepoint");
    assert_eq!(state_count, 2);
    let event_count: i64 = sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE id = $1")
        .bind(nested_event.id.0)
        .fetch_one(database.pool())
        .await
        .expect("event count after savepoint");
    assert_eq!(event_count, 0);
}

/// A failed library migration leaves the service's connection and migrator isolated.
#[tokio::test]
async fn failed_migration_does_not_poison_pool() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    assert!(
        database
            .migrate_in_schema("bad", &FAIL_MIGRATIONS)
            .await
            .is_err()
    );
    let path: String = sqlx::query_scalar("SHOW search_path")
        .fetch_one(database.pool())
        .await
        .expect("search path");
    assert_eq!(path, "\"$user\", public");
    database
        .migrate(&SERVICE_MIGRATIONS)
        .await
        .expect("service migration");
    let (first, second) = tokio::join!(migrate(&database), migrate(&database));
    first.expect("first events migration");
    second.expect("concurrent events migration");
    let service: Option<String> =
        sqlx::query_scalar("SELECT to_regclass('public.service_state')::text")
            .fetch_one(database.pool())
            .await
            .expect("service table");
    assert_eq!(service.as_deref(), Some("service_state"));
}

/// Cancelling a migration cannot return its session state to the shared pool.
#[tokio::test]
async fn cancelled_migration_does_not_poison_pool() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    tokio::select! {
        result = database.migrate_in_schema("sleeping", &SLEEP_MIGRATIONS) => {
            panic!("sleeping migration finished unexpectedly: {result:?}");
        }
        () = async {
            for _ in 0..100 {
                let active: bool = sqlx::query_scalar("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE query LIKE $1 AND state = 'active' AND pid <> pg_backend_pid())")
                    .bind("%pg_sleep(2)%")
                    .fetch_one(database.pool()).await.expect("activity");
                if active { return; }
                tokio::time::sleep(std::time::Duration::from_millis(20)).await;
            }
            panic!("sleeping migration did not reach the database");
        } => {}
    }
    let path: String = sqlx::query_scalar("SHOW search_path")
        .fetch_one(database.pool())
        .await
        .expect("search path");
    assert_eq!(path, "\"$user\", public");
    database
        .migrate(&SERVICE_MIGRATIONS)
        .await
        .expect("service migration");
}

/// A service role can connect to its own database but not a peer database.
#[tokio::test]
async fn service_database_roles_are_isolated() {
    let postgres = PostgresContainer::start()
        .await
        .expect("Postgres container");
    let admin = Database::connect(postgres.url(), 1)
        .await
        .expect("admin database");
    let setup = include_str!("../../../deploy/dev/postgres-setup.sql");
    let statements = setup
        .lines()
        .filter(|line| !line.trim_start().starts_with("--"))
        .collect::<Vec<_>>()
        .join("\n");
    for statement in statements
        .split(';')
        .map(str::trim)
        .filter(|part| !part.is_empty())
    {
        sqlx::query(sqlx::AssertSqlSafe(statement.to_owned()))
            .execute(admin.pool())
            .await
            .expect("role setup");
    }
    let own_url = postgres
        .url()
        .replace("postgres:test", "ingress:ingress-dev")
        .replace("/concurro_test", "/ingress");
    Database::connect(&own_url, 1)
        .await
        .expect("own database is accessible");
    let peer_url = postgres
        .url()
        .replace("postgres:test", "ingress:ingress-dev")
        .replace("/concurro_test", "/orchestrator");
    assert_matches!(
        Database::connect(&peer_url, 1).await,
        Err(sqlx::Error::Database(error)) if error.code().as_deref() == Some("42501")
    );
}
