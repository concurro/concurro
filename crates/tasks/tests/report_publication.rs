//! Exact issue artifact readback after a lost report create response.

use std::{
    collections::BTreeMap,
    process::Command,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
};

use axum::{
    Json, Router,
    http::StatusCode,
    routing::{get, post},
};
use chrono::{Duration, Utc};
use concurro_db::Database;
use concurro_github_v1::AppClient;
use concurro_tasks::{PublicationContext, PublicationTarget, publish_task_report};
use concurro_tasks_core::{Actor, IssueKey, Stage, TaskState};
use concurro_tasks_postgres::{migrate, pending_report_artifact_readbacks};
use concurro_testkit::PostgresContainer;
use serde_json::{Value, json};
use sqlx::types::Json as SqlJson;
use tokio::sync::Mutex;
use uuid::Uuid;

/// A lost POST response converges through exact App artifact and version readback.
#[tokio::test]
async fn lost_report_artifact_response_creates_only_one_visible_report() {
    let items = Arc::new(Mutex::new(Vec::<Value>::new()));
    let readback_unavailable = Arc::new(AtomicBool::new(true));
    let creates = Arc::new(AtomicUsize::new(0));
    let app = Router::new()
        .route(
            "/repos/acme/demo/installation",
            get(|| async {
                Json(json!({"id":7,"repository_selection":"selected","account":{"login":"acme"}}))
            }),
        )
        .route(
            "/app/installations/7/access_tokens",
            post(|Json(body): Json<Value>| async move {
                Json(
                    json!({"token":"scoped-token","permissions":body["permissions"],
                    "expires_at":(Utc::now()+Duration::minutes(55)).to_rfc3339()}),
                )
            }),
        )
        .route(
            "/installation/repositories",
            get(|| async {
                Json(json!({"total_count":1,"repositories":[{"full_name":"acme/demo"}]}))
            }),
        )
        .route(
            "/repos/acme/demo/issues/12/artifacts",
            get({
                let items = items.clone();
                let unavailable = readback_unavailable.clone();
                move || {
                    let items = items.clone();
                    let unavailable = unavailable.clone();
                    async move {
                        if unavailable.load(Ordering::SeqCst) {
                            Json(json!([]))
                        } else {
                            Json(json!(items.lock().await.clone()))
                        }
                    }
                }
            })
            .post({
                let items = items.clone();
                let creates = creates.clone();
                move |Json(body): Json<Value>| {
                    let items = items.clone();
                    let creates = creates.clone();
                    async move {
                        assert_eq!(body["type"], "generic");
                        assert!(body["body"].as_str().is_some_and(|text| {
                            text.starts_with("<!-- concurro:task-report:")
                        }));
                        creates.fetch_add(1, Ordering::SeqCst);
                        items.lock().await.push(json!({
                            "id":99,"number":3,"name":body["name"],"body":body["body"],
                            "type":"generic","creator":{"login":"concurro[bot]"}
                        }));
                        (
                            StatusCode::BAD_GATEWAY,
                            Json(json!({"message":"response lost"})),
                        )
                    }
                }
            }),
        )
        .route(
            "/repos/acme/demo/issues/12/artifacts/3",
            get({
                let items = items.clone();
                move || {
                    let items = items.clone();
                    async move {
                        let artifact = items.lock().await[0].clone();
                        Json(json!({"artifact":{
                            "id":artifact["id"],"number":artifact["number"],
                            "name":artifact["name"],"body":artifact["body"],
                            "type":"generic",
                            "updated_by_actor":{"login":"concurro[bot]"}},
                            "versions":[{"version":1,"name":artifact["name"],
                                "body":artifact["body"],"actor":{"login":"concurro[bot]"}}]}))
                    }
                }
            }),
        );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let address = listener.local_addr().expect("address");
    let server = tokio::spawn(async move { axum::serve(listener, app).await });
    let key = Command::new("openssl")
        .args([
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
        ])
        .output()
        .expect("test App key");
    assert!(key.status.success());
    let context = PublicationContext {
        github: AppClient::new(
            reqwest::Client::new(),
            &format!("http://{address}"),
            42,
            &key.stdout,
        )
        .expect("GitHub App"),
        targets: std::collections::HashMap::from([(
            7,
            PublicationTarget {
                repository: "acme/demo".to_owned(),
                read_permissions: BTreeMap::from([("issues".to_owned(), "read".to_owned())]),
                write_permissions: BTreeMap::from([("issues".to_owned(), "write".to_owned())]),
                bot_login: "concurro[bot]".to_owned(),
            },
        )]),
    };
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    let issue = IssueKey {
        repository_id: 7,
        number: 12,
    };
    let task_id = Uuid::now_v7();
    let report_id = Uuid::now_v7();
    let state = TaskState {
        id: concurro_contracts::TaskId(task_id),
        issue,
        author: Actor {
            id: 7,
            login: "writer".to_owned(),
        },
        stage: Stage::Plan,
        revision: 1,
        stage_reason: None,
        retry_stage: None,
        pins: None,
        used_model_calls: 0,
        current_attempt: None,
    };
    sqlx::query("INSERT INTO tasks.issue_controls (repository_id,issue_number) VALUES ($1,$2)")
        .bind(issue.repository_id)
        .bind(issue.number)
        .execute(database.pool())
        .await
        .expect("issue control");
    sqlx::query("INSERT INTO tasks.task_state (repository_id,issue_number,task_id,revision,state) VALUES ($1,$2,$3,1,$4)")
        .bind(issue.repository_id).bind(issue.number).bind(task_id).bind(SqlJson(&state))
        .execute(database.pool()).await.expect("task state");
    let name = format!("concurro-task-report-{report_id}");
    let body = format!("<!-- concurro:task-report:{report_id} -->\n{{\"result\":\"ok\"}}");
    sqlx::query("INSERT INTO tasks.report_artifact_writes (report_id,task_id,repository_id,issue_number,name,body) VALUES ($1,$2,$3,$4,$5,$6)")
        .bind(report_id).bind(task_id).bind(issue.repository_id).bind(issue.number)
        .bind(name).bind(body).execute(database.pool()).await.expect("report intent");
    assert!(
        publish_task_report(&database, &context, report_id)
            .await
            .is_err()
    );
    assert_eq!(creates.load(Ordering::SeqCst), 1);
    assert_eq!(
        pending_report_artifact_readbacks(&database)
            .await
            .expect("pending"),
        vec![report_id]
    );
    readback_unavailable.store(false, Ordering::SeqCst);
    publish_task_report(&database, &context, report_id)
        .await
        .expect("exact artifact readback");
    publish_task_report(&database, &context, report_id)
        .await
        .expect("confirmed replay");
    assert_eq!(creates.load(Ordering::SeqCst), 1);
    let confirmed: (String, i64) = sqlx::query_as(
        "SELECT status,artifact_number FROM tasks.report_artifact_writes WHERE report_id=$1",
    )
    .bind(report_id)
    .fetch_one(database.pool())
    .await
    .expect("confirmed artifact");
    assert_eq!(confirmed, ("confirmed".to_owned(), 3));
    server.abort();
}
