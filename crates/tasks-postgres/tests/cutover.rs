//! Stopped-controller cutover identities survive retries without live admissions.

use chrono::{TimeZone, Utc};
use concurro_db::Database;
use concurro_tasks_postgres::{
    cutover_floor, import_cutover_manifest, migrate, source_after_activation,
};
use concurro_testkit::PostgresContainer;
use serde_json::json;

/// An empty inventory and an exact replay are valid; changed floors are refused.
#[tokio::test]
async fn imports_activation_floors_and_historical_pr_once() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("schema");
    import_cutover_manifest(&database, &json!({}))
        .await
        .expect("empty import");
    let manifest = json!({
        "targets":[{"repository_id":7,"activation_at":"2026-11-21T12:00:00Z",
            "source_incarnation":"python-a","source_sequence":44}],
        "floors":[{"repository_id":7,"issue_number":12,"generation":3,
            "openai_epoch":5,"anthropic_epoch":2}],
        "pull_requests":[{"upstream":"Acme/Source","pr_number":99,
            "document":{"state":"closed"},"owned_contribution":true},
            {"upstream":"Acme/Source","pr_number":100,
            "document":{"state":"open"}}]
    });
    for _ in 0..2 {
        import_cutover_manifest(&database, &manifest)
            .await
            .expect("exact replay");
    }
    let mut work = database.begin().await.expect("work");
    let floor = cutover_floor(&mut work, 7, 12)
        .await
        .expect("floor")
        .expect("present");
    assert_eq!(floor.generation, 3);
    assert_eq!(floor.openai_epoch, 5);
    assert!(
        !source_after_activation(
            &mut work,
            7,
            Utc.with_ymd_and_hms(2026, 11, 21, 11, 59, 59).unwrap()
        )
        .await
        .expect("prior source")
    );
    assert!(
        source_after_activation(
            &mut work,
            7,
            Utc.with_ymd_and_hms(2026, 11, 21, 12, 0, 0).unwrap()
        )
        .await
        .expect("boundary source")
    );
    work.commit().await.expect("commit");
    let owner: String = sqlx::query_scalar(
        "SELECT owner_kind FROM tasks.pr_ownership WHERE upstream='acme/source' AND pr_number=99",
    )
    .fetch_one(database.pool())
    .await
    .expect("reserved PR");
    assert_eq!(owner, "python");
    let historical_only: bool = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM tasks.pr_ownership \
         WHERE upstream='acme/source' AND pr_number=100)",
    )
    .fetch_one(database.pool())
    .await
    .expect("historical PR ownership");
    assert!(!historical_only);
    let mut changed = manifest.clone();
    changed["floors"][0]["generation"] = json!(4);
    assert!(import_cutover_manifest(&database, &changed).await.is_err());
}
