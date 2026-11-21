//! GitHub index scans preserve newer webhook facts and retain completed rows.

use chrono::{Duration, Utc};
use concurro_contracts::{ConfigLauncher, ConfigTarget, ConfigTargets};
use concurro_db::Database;
use concurro_events_postgres::Outbox;
use concurro_events_postgres::migrate as migrate_events;
use concurro_tasks_postgres::{
    IndexRow, baseline_page, create_baseline, index_item, index_page, index_scan_started, migrate,
    replace_target_index, sync_config_targets, upsert_index_event,
};
use concurro_testkit::PostgresContainer;
use serde_json::json;

/// A delivery applied while pages are fetched wins over an older list snapshot.
#[tokio::test]
async fn newer_event_survives_scan_and_completed_run_survives_stream_expiry() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate_events(&database).await.expect("event schema");
    migrate(&database).await.expect("schema");
    let config = ConfigTargets {
        targets: vec![ConfigTarget {
            repository_id: 7,
            upstream_repository_id: None,
            upstream: "acme/source".to_owned(),
            fork: "acme/fork".to_owned(),
            issue_repository: "acme/source".to_owned(),
            reviewer_user_id: None,
            launcher: ConfigLauncher {
                workflow: "concurro-executor.yml".to_owned(),
                r#ref: "refs/heads/main".to_owned(),
            },
        }],
    };
    let outbox = Outbox::new(1_000_000, vec!["config.>".to_owned()]);
    assert_eq!(
        sync_config_targets(&database, &outbox, &config)
            .await
            .expect("config"),
        1
    );
    assert_eq!(
        sync_config_targets(&database, &outbox, &config)
            .await
            .expect("same config"),
        1
    );
    let started = index_scan_started(&database).await.expect("scan start");
    let now = Utc::now();
    let event = IndexRow {
        repository_id: 7,
        kind: "pull",
        item_id: "9".to_owned(),
        source_at: now,
        body: json!({"repository":"acme/source","number":9,"state":"closed"}),
    };
    let mut work = database.begin().await.expect("event transaction");
    upsert_index_event(&mut work, &event).await.expect("event");
    work.commit().await.expect("event commit");
    let stale = IndexRow {
        repository_id: 7,
        kind: "pull",
        item_id: "9".to_owned(),
        source_at: now - Duration::seconds(1),
        body: json!({"repository":"acme/source","number":9,"state":"open"}),
    };
    replace_target_index(&database, 7, "pull", started, &[stale])
        .await
        .expect("older scan");
    let pulls = index_page(&database, "acme/source", "pull", "")
        .await
        .expect("pulls");
    assert_eq!(pulls.len(), 1);
    assert_eq!(pulls[0].document["state"], "closed");
    let pull = index_item(&database, "acme/source", "pull", "9")
        .await
        .expect("single pull")
        .expect("indexed pull");
    assert_eq!(pull.document["state"], "closed");

    let started = index_scan_started(&database).await.expect("run scan start");
    replace_target_index(
        &database,
        8,
        "run",
        started,
        &[IndexRow {
            repository_id: 8,
            kind: "run",
            item_id: "42/2".to_owned(),
            source_at: now,
            body: json!({"repository":"acme/fork","id":42,"status":"completed"}),
        }],
    )
    .await
    .expect("completed run");
    let baseline = create_baseline(&database).await.expect("baseline");
    let rows = baseline_page(&database, baseline, 0, 100)
        .await
        .expect("page")
        .expect("snapshot");
    assert!(rows.iter().any(|row| row.entity == "target_index"
        && row.body["kind"] == "run"
        && row.body["item_id"] == "42/2"));
    assert!(
        rows.iter()
            .any(|row| row.entity == "config_targets" && row.body[0]["upstream"] == "acme/source")
    );
}
