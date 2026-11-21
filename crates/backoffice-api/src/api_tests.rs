use axum::{
    Json, Router,
    http::{HeaderMap, StatusCode},
    routing::get,
};
use concurro_db::Database;
use concurro_testkit::PostgresContainer;
use serde_json::{Value, json};
use sqlx::types::Json as SqlJson;
use std::sync::{Arc, RwLock};
use uuid::Uuid;

use crate::{Target, model};

use super::{ApiState, router};

/// Reader responses exclude every other person's consent and all turn details.
#[tokio::test]
async fn issue_tiers_scope_task_records_and_diagnostics() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    model::migrate(&database).await.expect("read model schema");
    sqlx::query("UPDATE backoffice.model_state SET ready=true WHERE singleton")
        .execute(database.pool())
        .await
        .expect("ready model");
    let task = Uuid::now_v7();
    let turn = Uuid::now_v7();
    for (entity, id, body) in [
        (
            "contribution_task",
            task.to_string(),
            json!({"task_id":task,"repository_id":8,"issue_number":7,
                    "state":{"stage":"Ready","stage_reason":null}}),
        ),
        (
            "turn",
            turn.to_string(),
            json!({"turn_id":turn,"task_id":task,"submit_body_base64":"private prompt"}),
        ),
        (
            "consent",
            "mine".to_owned(),
            json!({"repository_id":8,"issue_number":7,"fact":{"actor":{"id":42}}}),
        ),
        (
            "consent",
            "theirs".to_owned(),
            json!({"repository_id":8,"issue_number":7,"fact":{"actor":{"id":99}}}),
        ),
        (
            "consent",
            "pre-enrollment".to_owned(),
            json!({"repository_id":8,"issue_number":8,"fact":{"actor":{"id":42}}}),
        ),
        (
            "consent_state",
            task.to_string(),
            json!({"task_id":task,"repository_id":8,"issue_number":7,
                "slots":{"openai":{"sponsor_id":"42"},
                    "anthropic":{"sponsor_id":"99"}},
                "provider_funding":{"openai":"contributed","anthropic":"org"}}),
        ),
    ] {
        sqlx::query(
            "INSERT INTO backoffice.model_items (entity,entity_id,revision,body) \
                 VALUES ($1,$2,1,$3)",
        )
        .bind(entity)
        .bind(id)
        .bind(SqlJson(body))
        .execute(database.pool())
        .await
        .expect("model row");
    }
    let authority = Router::new()
        .route("/v1/history", get(|| async { Json(json!({"plans":[]})) }))
        .route(
            "/v1/index/item",
            get(|| async { Json(json!({"document":{"number":8,"title":"current issue"}})) }),
        )
        .route(
            "/internal/v1/viewer",
            get(|headers: HeaderMap| async move {
                let tier = match headers
                    .get("x-concurro-viewer")
                    .and_then(|value| value.to_str().ok())
                {
                    Some("writer") => "writer",
                    Some("reader") => "reader",
                    _ => "none",
                };
                Json(json!({"user":{"id":42,"login":"octo"},"tier":tier,
                    "checked_at":"2026-11-20T00:00:00Z"}))
            }),
        );
    let auth_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("authority bind");
    let auth_address = auth_listener.local_addr().expect("authority address");
    let auth_task = tokio::spawn(async move { axum::serve(auth_listener, authority).await });
    let state = ApiState {
        database,
        client: reqwest::Client::new(),
        authority_url: format!("http://{auth_address}"),
        authority_bearer: "read-service".to_owned(),
        task_api_url: format!("http://{auth_address}"),
        task_read_bearer: "read-task".to_owned(),
        targets: Arc::new(RwLock::new(vec![Target {
            repository_id: 8,
            upstream_repository_id: None,
            upstream: "acme/demo".to_owned(),
            fork: "acme/fork".to_owned(),
            issue_repository: "acme/demo".to_owned(),
        }])),
    };
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("BFF bind");
    let address = listener.local_addr().expect("BFF address");
    let api_task = tokio::spawn(async move { axum::serve(listener, router(state)).await });
    let client = reqwest::Client::new();
    let url = format!("http://{address}/api/v2/tasks/{task}");
    let reader = client
        .get(&url)
        .header("x-concurro-viewer", "reader")
        .send()
        .await
        .expect("reader request");
    assert_eq!(reader.status(), StatusCode::OK);
    let body: Value = reader.json().await.expect("reader body");
    assert_eq!(
        body["own_consent"].as_array().expect("consent").len(),
        2,
        "{body}"
    );
    assert!(!body.to_string().contains("\"99\""));
    assert!(!body.to_string().contains("provider_funding"));
    assert_eq!(body["task"]["stage"], "ready");
    assert!(body.get("records").is_none());
    assert!(!body.to_string().contains("private prompt"));
    let writer: Value = client
        .get(&url)
        .header("x-concurro-viewer", "writer")
        .send()
        .await
        .expect("writer request")
        .json()
        .await
        .expect("writer body");
    assert!(writer.to_string().contains("private prompt"));
    assert_eq!(writer["history"]["plans"], json!([]));
    assert!(writer.to_string().contains("provider_funding"));
    assert!(writer["history"]["plans"].as_array().is_some());
    let before_start: Value = client
        .get(format!("http://{address}/api/v2/issues/acme/demo/8"))
        .header("x-concurro-viewer", "reader")
        .send()
        .await
        .expect("pre-enrollment request")
        .json()
        .await
        .expect("pre-enrollment body");
    assert!(before_start["task"].is_null());
    assert_eq!(before_start["issue"]["document"]["title"], "current issue");
    assert_eq!(before_start["consent"].as_array().unwrap().len(), 1);
    let mine: Value = client
        .get(format!("http://{address}/api/v2/me/sponsorships"))
        .header("x-concurro-viewer", "reader")
        .send()
        .await
        .expect("own history request")
        .json()
        .await
        .expect("own history body");
    assert_eq!(mine["facts"].as_array().unwrap().len(), 3);
    assert!(!mine.to_string().contains("provider_funding"));
    assert_eq!(
        client
            .get(&url)
            .header("x-concurro-viewer", "outsider")
            .send()
            .await
            .expect("outsider request")
            .status(),
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        client
            .get(&url)
            .header("cookie", "concurro_session=browser-command-cookie")
            .send()
            .await
            .expect("command cookie request")
            .status(),
        StatusCode::UNAUTHORIZED
    );
    api_task.abort();
    auth_task.abort();
}
