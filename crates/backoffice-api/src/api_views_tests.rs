//! Integration coverage for current configuration and scoped operational reads.

use std::{
    collections::HashMap,
    sync::{
        Arc, RwLock,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
};

use axum::{
    Json, Router,
    extract::Query,
    http::{HeaderMap, StatusCode},
    response::IntoResponse,
    routing::get,
};
use concurro_db::Database;
use concurro_testkit::PostgresContainer;
use serde_json::{Value, json};
use sqlx::types::Json as SqlJson;
use uuid::Uuid;

use super::{ApiState, router};
use crate::{Target, model};

/// Operations are issue-scoped even when the viewer can administer another target.
#[tokio::test]
async fn current_configuration_and_operator_are_writer_scoped_and_fail_visibly() {
    let postgres = PostgresContainer::start().await.unwrap();
    let database = Database::connect(postgres.url(), 3).await.unwrap();
    model::migrate(&database).await.unwrap();
    let visible = Uuid::now_v7();
    let standalone = Uuid::now_v7();
    let hidden = Uuid::now_v7();
    for (entity, id, body) in [
        (
            "contribution_task",
            visible,
            json!({"repository_id":8,"issue_number":7,"snapshot":{"stage":"Blocked","stage_reason":"credentials"}}),
        ),
        (
            "standalone_review",
            standalone,
            json!({"repository_id":8,"pr_number":9,"stage":"review"}),
        ),
        (
            "contribution_task",
            hidden,
            json!({"repository_id":8,"issue_number":88,"state":{"stage":"Blocked","stage_reason":"private-reason"}}),
        ),
        (
            "contribution_task",
            Uuid::now_v7(),
            json!({"repository_id":88,"issue_number":1,"state":{"stage":"Blocked","stage_reason":"other-tenant"}}),
        ),
        (
            "turn",
            Uuid::now_v7(),
            json!({"task_id":visible,"status":"queued","submit_body_base64":"private prompt"}),
        ),
        (
            "turn",
            Uuid::now_v7(),
            json!({"task_id":visible,"status":"running"}),
        ),
        (
            "turn",
            Uuid::now_v7(),
            json!({"task_id":visible,"status":"running","finished":{"outcome":"succeeded"}}),
        ),
        (
            "turn",
            Uuid::now_v7(),
            json!({"task_id":visible,"status":"finished"}),
        ),
        (
            "turn",
            Uuid::now_v7(),
            json!({"standalone_task_id":standalone,"status":"unknown"}),
        ),
        (
            "turn",
            Uuid::now_v7(),
            json!({"task_id":hidden,"status":"queued"}),
        ),
    ] {
        sqlx::query("INSERT INTO backoffice.model_items(entity,entity_id,revision,body) VALUES ($1,$2,1,$3)")
            .bind(entity).bind(id.to_string()).bind(SqlJson(body)).execute(database.pool()).await.unwrap();
    }
    sqlx::query("INSERT INTO backoffice.model_events(id,event_type,data) VALUES ($1,'test','{}')")
        .bind(Uuid::now_v7())
        .execute(database.pool())
        .await
        .unwrap();
    let unavailable = Arc::new(AtomicBool::new(false));
    let config_reads = Arc::new(AtomicUsize::new(0));
    let auth_unavailable = unavailable.clone();
    let source_unavailable = Arc::new(AtomicBool::new(false));
    let control_source = source_unavailable.clone();
    let reads = config_reads.clone();
    let authority = Router::new()
        .route("/internal/v1/viewer", get(move |headers: HeaderMap, Query(query): Query<HashMap<String,String>>| {
            let unavailable = auth_unavailable.clone();
            async move {
                if unavailable.load(Ordering::SeqCst) { return StatusCode::SERVICE_UNAVAILABLE.into_response(); }
                assert_eq!(headers["authorization"], "Bearer read-service");
                let token = headers.get("x-concurro-viewer").and_then(|value| value.to_str().ok()).unwrap_or("");
                let tier = if token == "writer" && query["upstream"] == "acme/demo" && query["issue_number"] != "88" { "writer" }
                    else if token == "reader" || token == "writer" { "reader" } else { "none" };
                Json(json!({"user":{"id":42,"login":"octo"},"tier":tier,"checked_at":"2026-11-21T10:00:00Z"})).into_response()
            }
        }))
        .route("/v1/targets/acme/demo/configuration", get(move |headers: HeaderMap| {
            let unavailable = source_unavailable.clone();
            let reads = reads.clone();
            async move {
                assert_eq!(headers["authorization"], "Bearer read-task");
                reads.fetch_add(1, Ordering::SeqCst);
                if unavailable.load(Ordering::SeqCst) { return StatusCode::SERVICE_UNAVAILABLE.into_response(); }
                Json(json!({"mode":"autonomous","source":{"sha":"current-sha"},"roster":[],"profiles":[],"settings":{"max_model_calls":48}})).into_response()
            }
        }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let authority_url = format!("http://{}", listener.local_addr().unwrap());
    let authority_task = tokio::spawn(async move { axum::serve(listener, authority).await });
    let state = ApiState {
        database: database.clone(),
        client: reqwest::Client::new(),
        authority_url: authority_url.clone(),
        authority_bearer: "read-service".into(),
        task_api_url: authority_url,
        task_read_bearer: "read-task".into(),
        targets: Arc::new(RwLock::new(vec![
            Target {
                repository_id: 8,
                upstream_repository_id: None,
                upstream: "acme/demo".into(),
                fork: "acme/fork".into(),
                issue_repository: "acme/demo".into(),
            },
            Target {
                repository_id: 88,
                upstream_repository_id: None,
                upstream: "private/repo".into(),
                fork: "private/fork".into(),
                issue_repository: "private/issues".into(),
            },
        ])),
    };
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/api/v2", listener.local_addr().unwrap());
    let server = tokio::spawn(async move { axum::serve(listener, router(state)).await });
    let client = reqwest::Client::new();
    for endpoint in ["operator", "targets/acme/demo/configuration"] {
        assert_eq!(
            client
                .get(format!("{url}/{endpoint}"))
                .send()
                .await
                .unwrap()
                .status(),
            StatusCode::UNAUTHORIZED
        );
        assert_eq!(
            client
                .get(format!("{url}/{endpoint}"))
                .header("x-concurro-viewer", "reader")
                .send()
                .await
                .unwrap()
                .status(),
            StatusCode::FORBIDDEN
        );
    }
    assert_eq!(config_reads.load(Ordering::SeqCst), 0);
    let config: Value = client
        .get(format!("{url}/targets/acme/demo/configuration"))
        .header("x-concurro-viewer", "writer")
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(config["source"]["sha"], "current-sha");
    assert_eq!(config_reads.load(Ordering::SeqCst), 1);
    let loading: Value = client
        .get(format!("{url}/operator"))
        .header("x-concurro-viewer", "writer")
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(loading["projection"]["ready"], false);
    assert_eq!(loading["projection"]["buffered_events"], 1);
    assert!(loading["tasks"].is_null());
    assert!(loading["turns"].is_null());
    sqlx::query("UPDATE backoffice.model_state SET ready=true,revision=15 WHERE singleton")
        .execute(database.pool())
        .await
        .unwrap();
    let op: Value = client
        .get(format!("{url}/operator"))
        .header("x-concurro-viewer", "writer")
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(op["tasks"].as_array().unwrap().len(), 2);
    assert_eq!(op["stages"], json!({"blocked":1,"review":1}));
    assert_eq!(
        op["turns"],
        json!({"queued":1,"running":1,"unknown_outcome":1})
    );
    assert_eq!(op["projection"]["revision"], 15);
    for private in [
        "private-reason",
        "other-tenant",
        "private prompt",
        &hidden.to_string(),
    ] {
        assert!(!op.to_string().contains(private));
    }
    assert!(op.get("gateway").is_none());
    control_source.store(true, Ordering::SeqCst);
    let source_error = client
        .get(format!("{url}/targets/acme/demo/configuration"))
        .header("x-concurro-viewer", "writer")
        .send()
        .await
        .unwrap();
    assert_eq!(source_error.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(
        source_error.json::<Value>().await.unwrap()["error"],
        "read_unavailable"
    );
    unavailable.store(true, Ordering::SeqCst);
    for endpoint in ["operator", "targets/acme/demo/configuration"] {
        let response = client
            .get(format!("{url}/{endpoint}"))
            .header("x-concurro-viewer", "writer")
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(
            response.json::<Value>().await.unwrap()["error"],
            "read_unavailable"
        );
    }
    server.abort();
    authority_task.abort();
}

/// Terminal uncertainty has the same display after a baseline rebuild and live event replay.
#[tokio::test]
async fn turn_operations_match_baseline_and_terminal_event_shapes() {
    use concurro_backoffice_postgres as storage;

    let postgres = PostgresContainer::start().await.unwrap();
    let database = Database::connect(postgres.url(), 3).await.unwrap();
    model::migrate(&database).await.unwrap();
    let task = Uuid::now_v7();
    let unknown = Uuid::now_v7();
    let running = Uuid::now_v7();
    let snapshot = Uuid::now_v7();
    let mut work = database.begin().await.unwrap();
    for (ordinal, entity, id, body) in [
        (
            1,
            "contribution_task",
            task,
            json!({"task_id":task,"repository_id":8,"issue_number":7,"snapshot":{"stage":"Blocked"}}),
        ),
        (
            2,
            "turn",
            unknown,
            json!({"turn_id":unknown,"task_id":task,"status":"unknown",
            "outcome_reason":"admission_unknown","result":null,"finished":null,
            "admitted_at":"2026-11-21T10:00:00Z","runner_sessions":[]}),
        ),
        (
            3,
            "turn",
            running,
            json!({"turn_id":running,"task_id":task,"status":"running",
            "result":null,"finished":null,"admitted_at":"2026-11-21T10:01:00Z"}),
        ),
    ] {
        storage::stage_baseline_row(
            &mut work,
            snapshot,
            ordinal,
            entity,
            &id.to_string(),
            1,
            body,
        )
        .await
        .unwrap();
    }
    work.commit().await.unwrap();
    storage::finish_baseline(&database, snapshot, 0)
        .await
        .unwrap();
    let baseline = storage::operational_tasks(&database)
        .await
        .unwrap()
        .remove(0)
        .turns;
    assert_eq!(baseline.get("unknown_outcome"), Some(&1));
    assert_eq!(
        baseline.get("running"),
        Some(&1),
        "finished:null does not finish a turn"
    );
    // Replace the baseline's unknown attempt with the actual submitted/status/finished event shapes.
    sqlx::query("DELETE FROM backoffice.model_items WHERE entity='turn' AND entity_id=$1")
        .bind(unknown.to_string())
        .execute(database.pool())
        .await
        .unwrap();
    for (suffix, revision, data) in [
        (
            "submitted",
            None,
            json!({"turn_id":unknown,"attempt_id":unknown,"task_id":task,
            "grant_id":"org","stage":"intake","role":"triage","harness":"codex",
            "model":"gpt-test","effort":"low","profile":null,"session":null,
            "bundle_id":"bundle","limits":{},"spec":{"sha256":"a".repeat(64),"bytes":2,"media_type":"application/json"},
            "prompt":null,"submitted_at":"2026-11-21T10:00:00Z"}),
        ),
        (
            "status",
            Some(2),
            json!({"turn_id":unknown,"status":"finished","created_at":null,
            "started_at":null,"finished_at":null,"runner":null}),
        ),
        (
            "finished",
            None,
            json!({"turn_id":unknown,"outcome":"unknown","reason":"admission_unknown",
            "provider_requests":"unknown","response":null,"workspace":null,"transcript":null,
            "transcript_error":null,"elapsed_seconds":null,"provider_failure":null,"funding_ended":false}),
        ),
    ] {
        let mut work = database.begin().await.unwrap();
        storage::ingest_event_in_work(
            &mut work,
            &storage::TaskEvent {
                id: Uuid::now_v7(),
                event_type: format!("turn.{unknown}.{suffix}"),
                entity: revision.map(|_| format!("turn:{unknown}")),
                revision,
                data,
            },
        )
        .await
        .unwrap();
        work.commit().await.unwrap();
    }
    let streamed = storage::operational_tasks(&database)
        .await
        .unwrap()
        .remove(0)
        .turns;
    assert_eq!(streamed, baseline);
    // A known finished fact takes precedence even if an older active status arrives afterward.
    let mut work = database.begin().await.unwrap();
    storage::ingest_event_in_work(&mut work,&storage::TaskEvent {id:Uuid::now_v7(),
        event_type:format!("turn.{unknown}.status"),entity:Some(format!("turn:{unknown}")),revision:Some(3),
        data:json!({"turn_id":unknown,"status":"running","created_at":null,"started_at":null,"finished_at":null,"runner":null})}).await.unwrap();
    work.commit().await.unwrap();
    assert_eq!(
        storage::operational_tasks(&database)
            .await
            .unwrap()
            .remove(0)
            .turns,
        baseline
    );
}
