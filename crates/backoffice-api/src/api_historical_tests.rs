//! Verified Python display exports through Rust storage and viewer authorization.

use super::{ApiState, router};
use crate::{Target, model};
use axum::{
    Json, Router,
    extract::Query,
    http::{HeaderMap, StatusCode},
    routing::get,
};
use concurro_db::Database;
use concurro_tasks_postgres::{
    historical_display, import_historical_display, own_charged_calls, unavailable_historical_usage,
};
use concurro_testkit::PostgresContainer;
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    sync::{Arc, RwLock},
};

/// Import is immutable and display-only, and all historical reports require current writer authority.
#[tokio::test]
async fn historical_projector_evidence_is_immutable_and_writer_scoped() {
    let postgres = PostgresContainer::start().await.unwrap();
    let database = Database::connect(postgres.url(), 3).await.unwrap();
    concurro_tasks_postgres::migrate(&database).await.unwrap();
    model::migrate(&database).await.unwrap();
    let manifest: Value = serde_json::from_str(include_str!(
        "../../tasks-postgres/tests/fixtures/historical_display.json"
    ))
    .unwrap();
    for _ in 0..2 {
        assert_eq!(
            import_historical_display(&database, &manifest)
                .await
                .unwrap()["views"],
            2
        );
    }
    let mut changed = manifest.clone();
    changed["views"][0]["view"]["task"]["title"] = json!("changed");
    assert!(
        import_historical_display(&database, &changed)
            .await
            .is_err()
    );
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM tasks.task_state")
        .fetch_one(database.pool())
        .await
        .unwrap();
    assert_eq!(
        count, 0,
        "a display import never resurrects a controller task"
    );
    sqlx::query("UPDATE backoffice.model_state SET ready=true WHERE singleton")
        .execute(database.pool())
        .await
        .unwrap();
    for number in [7, 88] {
        let task = uuid::Uuid::now_v7();
        for (entity, id, body) in [
            (
                "contribution_task",
                task,
                json!({"task_id":task,"repository_id":8,"issue_number":number,"snapshot":{"stage":"review"}}),
            ),
            (
                "turn",
                uuid::Uuid::now_v7(),
                json!({"task_id":task,"operation":"review","admitted_at":"2026-11-21T09:00:00Z","status":"finished","result":{"outcome":"ok"},"prompt_body_base64":"private-prompt"}),
            ),
        ] {
            sqlx::query("INSERT INTO backoffice.model_items(entity,entity_id,revision,body) VALUES ($1,$2,1,$3)").bind(entity).bind(id.to_string()).bind(body).execute(database.pool()).await.unwrap();
        }
    }
    assert_eq!(
        own_charged_calls(&database, 42)
            .await
            .unwrap()
            .iter()
            .find(|row| row["id"] == "legacy-debit")
            .unwrap()["calls"],
        3
    );
    assert!(own_charged_calls(&database, 43).await.unwrap().is_empty());
    let billing_database = database.clone();
    let source = database.clone();
    let private = Router::new()
        .route("/internal/v1/viewer",get(|headers: HeaderMap, Query(query):Query<HashMap<String,String>>| async move {
            let tier = if headers.get("x-concurro-viewer").and_then(|v| v.to_str().ok()) == Some("writer") && query["issue_number"] != "88" {"writer"} else {"reader"};
            Json(json!({"user":{"id":42,"login":"octo"},"tier":tier,"checked_at":"2026-11-21T10:00:00Z"}))
        }))
        .route("/v1/billing/{payer}",get(move |axum::extract::Path(payer):axum::extract::Path<i64>| {
            let database = billing_database.clone(); async move { Json(json!({"rows":own_charged_calls(&database,payer).await.unwrap(),"unavailable":unavailable_historical_usage(&database).await.unwrap()})) }
        }))
        .route("/v1/historical",get(move |headers:HeaderMap, Query(query):Query<HashMap<String,String>>| {
            let source = source.clone(); async move {
                assert_eq!(headers["authorization"],"Bearer read-task");
                let rows = historical_display(&source,&query["repository"],&query["kind"],0).await.unwrap();
                Json(json!({"rows":rows,"next_cursor":null}))
            }
        }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let server = tokio::spawn(async move { axum::serve(listener, private).await });
    let repository = manifest["views"][0]["repository"].as_str().unwrap();
    let state = ApiState {
        database,
        client: reqwest::Client::new(),
        authority_url: url.clone(),
        authority_bearer: "read-service".into(),
        task_api_url: url,
        task_read_bearer: "read-task".into(),
        targets: Arc::new(RwLock::new(vec![Target {
            repository_id: 8,
            upstream_repository_id: None,
            upstream: repository.into(),
            issue_repository: repository.into(),
            fork: repository.into(),
        }])),
    };
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!(
        "http://{}/api/v2/targets/{repository}/historical",
        listener.local_addr().unwrap()
    );
    let public = tokio::spawn(async move { axum::serve(listener, router(state)).await });
    let client = reqwest::Client::new();
    assert_eq!(
        client
            .get(&url)
            .query(&[("kind", "contribution")])
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        client
            .get(&url)
            .header("x-concurro-viewer", "reader")
            .query(&[("kind", "review")])
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::FORBIDDEN
    );
    for (kind, index) in [("contribution", 0), ("review", 1)] {
        let result: Value = client
            .get(&url)
            .header("x-concurro-viewer", "writer")
            .query(&[("kind", kind)])
            .send()
            .await
            .unwrap()
            .error_for_status()
            .unwrap()
            .json()
            .await
            .unwrap();
        assert_eq!(result["rows"][0]["record"], manifest["views"][index]);
        assert_eq!(
            result["rows"][0]["observed_at"],
            "2026-11-21T10:00:00+00:00"
        );
    }
    let activity_url = url.split("/targets/").next().unwrap().to_owned() + "/activity";
    for (viewer, expected) in [("reader", 0), ("writer", 1)] {
        let result: Value = client
            .get(&activity_url)
            .header("x-concurro-viewer", viewer)
            .send()
            .await
            .unwrap()
            .error_for_status()
            .unwrap()
            .json()
            .await
            .unwrap();
        assert_eq!(result["events"].as_array().unwrap().len(), expected);
        assert!(!result.to_string().contains("private-prompt"));
    }
    let own_url = url.split("/targets/").next().unwrap().to_owned() + "/me/sponsorships";
    let own: Value = client
        .get(own_url)
        .header("x-concurro-viewer", "reader")
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(own["charged_calls"][0]["id"], "legacy-debit");
    assert_eq!(own["charged_calls"][0]["calls"], 3);
    assert!(
        own["facts"].as_array().unwrap().is_empty(),
        "old debits do not create active consent"
    );
    public.abort();
    server.abort();
}
