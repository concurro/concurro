use std::collections::HashMap;

use axum::{Json, Router, extract::Query, routing::get};
use concurro_db::Database;
use concurro_testkit::PostgresContainer;
use serde_json::{Value, json};
use sqlx::types::Json as SqlJson;
use uuid::Uuid;

use super::{TaskEvent, ingest_event, migrate, rebuild};

/// An event already applied between pages survives a live baseline replacement.
#[tokio::test]
async fn applied_event_wins_over_frozen_baseline_page() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("read model schema");
    sqlx::query("UPDATE backoffice.model_state SET ready=true WHERE singleton")
        .execute(database.pool())
        .await
        .expect("existing projection");
    let snapshot = Uuid::now_v7();
    let task_a = Uuid::now_v7();
    let task_b = Uuid::now_v7();
    let event_id = Uuid::now_v7();
    let source = database.clone();
    let server = Router::new().route(
        "/v1/baseline",
        get(move |Query(query): Query<HashMap<String, String>>| {
            let database = source.clone();
            async move {
                if query.contains_key("cursor") {
                    ingest_event(
                        &database,
                        &TaskEvent {
                            id: event_id,
                            event_type: format!("task.{task_a}.state"),
                            entity: Some(format!("task:{task_a}")),
                            revision: Some(2),
                            data: json!({"task_id":task_a,"issue":{"repository_id":8,
                                    "number":1},"stage":"ready"}),
                        },
                    )
                    .await
                    .expect("buffered event");
                    Json(json!({"snapshot_id":snapshot,"rows":[{
                            "ordinal":2,"entity":"contribution_task","entity_id":task_b,
                            "revision":1,"body":{"task_id":task_b,"repository_id":8,
                                "issue_number":2,"state":{"stage":"Plan"}}
                        }],"next_cursor":null}))
                } else {
                    Json(json!({"snapshot_id":snapshot,"rows":[{
                            "ordinal":1,"entity":"contribution_task","entity_id":task_a,
                            "revision":1,"body":{"task_id":task_a,"repository_id":8,
                                "issue_number":1,"state":{"stage":"Plan"}}
                        }],"next_cursor":format!("{snapshot}:1")}))
                }
            }
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let address = listener.local_addr().expect("address");
    let task = tokio::spawn(async move { axum::serve(listener, server).await });
    let rebuilt = rebuild(
        &database,
        &reqwest::Client::new(),
        &format!("http://{address}"),
        "read-only",
    )
    .await
    .expect("rebuild");
    assert_eq!(rebuilt, snapshot);
    let (revision, SqlJson(body)): (i64, SqlJson<Value>) = sqlx::query_as(
        "SELECT revision,body FROM backoffice.model_items \
             WHERE entity='contribution_task' AND entity_id=$1",
    )
    .bind(task_a.to_string())
    .fetch_one(database.pool())
    .await
    .expect("merged task");
    assert_eq!(revision, 2);
    assert_eq!(body["snapshot"]["stage"], "ready");
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM backoffice.model_items")
        .fetch_one(database.pool())
        .await
        .expect("model count");
    assert_eq!(count, 2);
    let turn = Uuid::now_v7();
    for event in [
        TaskEvent {
            id: Uuid::now_v7(),
            event_type: format!("turn.{turn}.submitted"),
            entity: None,
            revision: None,
            data: json!({"turn_id":turn,"task_id":task_a,"role":"reviewer"}),
        },
        TaskEvent {
            id: Uuid::now_v7(),
            event_type: format!("turn.{turn}.status"),
            entity: Some(format!("turn:{turn}")),
            revision: Some(1),
            data: json!({"turn_id":turn,"status":"running"}),
        },
        TaskEvent {
            id: Uuid::now_v7(),
            event_type: format!("turn.{turn}.finished"),
            entity: None,
            revision: None,
            data: json!({"turn_id":turn,"outcome":"succeeded"}),
        },
    ] {
        ingest_event(&database, &event).await.expect("turn event");
    }
    let SqlJson(turn_body): SqlJson<Value> = sqlx::query_scalar(
        "SELECT body FROM backoffice.model_items WHERE entity='turn' AND entity_id=$1",
    )
    .bind(turn.to_string())
    .fetch_one(database.pool())
    .await
    .expect("projected turn");
    assert_eq!(turn_body["task_id"], json!(task_a));
    assert_eq!(turn_body["status"], "running");
    assert_eq!(turn_body["finished"]["outcome"], "succeeded");
    task.abort();
}
