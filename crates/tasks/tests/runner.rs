//! Runner ref and dispatch publication against a scoped App fixture.

use std::{
    collections::{BTreeMap, HashMap},
    process::Command,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
};

use axum::{
    Json, Router,
    extract::Path,
    http::StatusCode,
    routing::{get, post},
};
use chrono::{Duration, Utc};
use concurro_db::Database;
use concurro_github_v1::AppClient;
use concurro_tasks::{RunnerContext, RunnerTarget, publish_runner_session};
use concurro_tasks_core::IssueKey;
use concurro_tasks_postgres::{
    finish_ref_write, migrate, record_runner_session, retire_runner_sessions, runner_session,
    task_detail,
};
use concurro_testkit::PostgresContainer;
use serde_json::{Value, json};
use tokio::sync::Mutex;
use uuid::Uuid;

#[tokio::test]
async fn runner_ref_is_pinned_and_dispatch_is_sent_once() {
    let session = Uuid::now_v7();
    let refs = Arc::new(Mutex::new(HashMap::<String, String>::new()));
    let dispatches = Arc::new(AtomicUsize::new(0));
    let visible = Arc::new(AtomicBool::new(false));
    let app = Router::new()
        .route(
            "/repos/acme/fork/installation",
            get(|| async {
                Json(json!({"id":7,"repository_selection":"selected","account":{"login":"acme"}}))
            }),
        )
        .route(
            "/app/installations/7/access_tokens",
            post(|Json(body): Json<Value>| async move {
                assert_eq!(body["repositories"], json!(["fork"]));
                Json(
                    json!({"token":"scoped-token","permissions":body["permissions"],
                "expires_at":(Utc::now()+Duration::minutes(55)).to_rfc3339()}),
                )
            }),
        )
        .route(
            "/installation/repositories",
            get(|| async {
                Json(json!({"total_count":1,"repositories":[{"full_name":"acme/fork"}]}))
            }),
        )
        .route(
            "/repos/acme/fork",
            get(|| async { Json(json!({"full_name":"acme/fork"})) }),
        )
        .route(
            "/repos/acme/fork/git/ref/heads/concurro/runs/{session}",
            get({
                let refs = Arc::clone(&refs);
                move |Path(session): Path<String>| {
                    let refs = Arc::clone(&refs);
                    async move {
                        match refs.lock().await.get(&session) {
                            Some(sha) => (StatusCode::OK, Json(json!({"object":{"sha":sha}}))),
                            None => (StatusCode::NOT_FOUND, Json(json!({"message":"missing"}))),
                        }
                    }
                }
            }),
        )
        .route(
            "/repos/acme/fork/git/refs",
            post({
                let refs = Arc::clone(&refs);
                move |Json(body): Json<Value>| {
                    let refs = Arc::clone(&refs);
                    async move {
                        let session = body["ref"]
                            .as_str()
                            .expect("ref")
                            .strip_prefix("refs/heads/concurro/runs/")
                            .expect("runner ref");
                        let sha = body["sha"].as_str().expect("sha");
                        refs.lock().await.insert(session.to_owned(), sha.to_owned());
                        (StatusCode::CREATED, Json(json!({"ref":body["ref"]})))
                    }
                }
            }),
        )
        .route(
            "/repos/acme/fork/git/refs/heads/concurro/runs/{session}",
            axum::routing::delete({
                let refs = Arc::clone(&refs);
                move |Path(session): Path<String>| {
                    let refs = Arc::clone(&refs);
                    async move {
                        refs.lock().await.remove(&session);
                        StatusCode::NO_CONTENT
                    }
                }
            }),
        )
        .route(
            "/repos/acme/fork/actions/workflows/concurro-executor.yml/dispatches",
            post({
                let dispatches = Arc::clone(&dispatches);
                move |Json(body): Json<Value>| {
                    let dispatches = Arc::clone(&dispatches);
                    async move {
                        assert_eq!(
                            body["inputs"]["session"].as_str(),
                            body["ref"]
                                .as_str()
                                .and_then(|text| text.strip_prefix("concurro/runs/"))
                        );
                        assert_eq!(body["inputs"]["task-label"], "acme/project#12");
                        assert_eq!(body["inputs"]["operation-label"], "Task assessment");
                        assert_eq!(body["inputs"]["agent-label"], "gpt-6 · low effort");
                        dispatches.fetch_add(1, Ordering::SeqCst);
                        StatusCode::NO_CONTENT
                    }
                }
            }),
        )
        .route(
            "/repos/acme/fork/actions/workflows/concurro-executor.yml/runs",
            get({
                let visible = Arc::clone(&visible);
                move || {
                    let visible = Arc::clone(&visible);
                    async move {
                        Json(json!({"workflow_runs":if visible.load(Ordering::SeqCst) {
                        vec![json!({"id":123,"head_branch":format!("concurro/runs/{session}"),
                            "head_sha":"a".repeat(40),"event":"workflow_dispatch"})]
                    } else { Vec::new() }}))
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
    let github = AppClient::new(
        reqwest::Client::new(),
        &format!("http://{address}"),
        42,
        &key.stdout,
    )
    .expect("App client");
    let read_permissions = BTreeMap::from([
        ("contents".to_owned(), "read".to_owned()),
        ("actions".to_owned(), "read".to_owned()),
    ]);
    let write_permissions = BTreeMap::from([
        ("contents".to_owned(), "write".to_owned()),
        ("actions".to_owned(), "write".to_owned()),
        ("workflows".to_owned(), "write".to_owned()),
    ]);
    let context = RunnerContext {
        github,
        targets: HashMap::from([(
            8,
            RunnerTarget {
                fork: "acme/fork".to_owned(),
                read_permissions,
                write_permissions,
            },
        )]),
    };
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    let task = Uuid::now_v7();
    let turn = Uuid::now_v7();
    let submit_body = serde_json::to_vec(&json!({
        "turn":turn,"expected":{},"models":["gpt-6"],"budget":100,"lease":60,
        "payload":{"request":{"stage":"intake","provider":"codex","model":"gpt-6","effort":"low"}},
        "pin":null,"binding":{"repository":"acme/project","issue":12}
    }))
    .expect("submit body");
    sqlx::query("INSERT INTO tasks.issue_controls (repository_id,issue_number) VALUES (8,12)")
        .execute(database.pool())
        .await
        .expect("issue");
    sqlx::query(
        "INSERT INTO tasks.task_state \
        (repository_id,issue_number,task_id,revision,state) VALUES (8,12,$1,1,'{}'::jsonb)",
    )
    .bind(task)
    .execute(database.pool())
    .await
    .expect("task");
    sqlx::query("INSERT INTO tasks.attempts \
        (id,task_id,operation,expected_revision,staffing_group,fork,config_sha,submit_body, \
         initial_deadline,unbounded_running,status) \
         VALUES ($1,$2,'intake',1,'group','acme/fork',$3,$4,now()+interval '1 hour',false,'queued')")
        .bind(turn).bind(task).bind("a".repeat(40)).bind(submit_body)
        .execute(database.pool()).await.expect("attempt");
    let mut work = database.begin().await.expect("session transaction");
    record_runner_session(&mut work, turn, session)
        .await
        .expect("session intent");
    work.commit().await.expect("session commit");
    publish_runner_session(&database, &context, session)
        .await
        .expect("create ref");
    assert_eq!(
        refs.lock().await.get(&session.to_string()),
        Some(&"a".repeat(40))
    );
    publish_runner_session(&database, &context, session)
        .await
        .expect("dispatch");
    assert_eq!(dispatches.load(Ordering::SeqCst), 1);
    publish_runner_session(&database, &context, session)
        .await
        .expect("uncertain readback");
    assert_eq!(dispatches.load(Ordering::SeqCst), 1);
    let recorded = runner_session(&database, session)
        .await
        .expect("session")
        .expect("row");
    assert_eq!(recorded.dispatch_status, "sent");
    visible.store(true, Ordering::SeqCst);
    publish_runner_session(&database, &context, session)
        .await
        .expect("adopt run");
    let recorded = runner_session(&database, session)
        .await
        .expect("session")
        .expect("row");
    assert_eq!(recorded.dispatch_status, "observed");
    let old_write: i64 = sqlx::query_scalar(
        "INSERT INTO tasks.runner_ref_writes (session_id,revision,action,status) \
         VALUES ($1,1,'create','sending') RETURNING id",
    )
    .bind(session)
    .fetch_one(database.pool())
    .await
    .expect("old uncertain create");
    let conflict_session = Uuid::now_v7();
    let mut work = database.begin().await.expect("conflict transaction");
    record_runner_session(&mut work, turn, conflict_session)
        .await
        .expect("second session");
    work.commit().await.expect("conflict session commit");
    assert!(
        !runner_session(&database, session)
            .await
            .expect("superseded session")
            .expect("old row")
            .desired_present
    );
    refs.lock()
        .await
        .insert(conflict_session.to_string(), "b".repeat(40));
    publish_runner_session(&database, &context, conflict_session)
        .await
        .expect("report conflicting SHA");
    let conflicting = runner_session(&database, conflict_session)
        .await
        .expect("conflict session")
        .expect("row");
    assert_eq!(conflicting.dispatch_status, "failed");
    assert_eq!(dispatches.load(Ordering::SeqCst), 1);
    let detail = task_detail(
        &database,
        IssueKey {
            repository_id: 8,
            number: 12,
        },
    )
    .await
    .expect("detail")
    .expect("task");
    assert!(
        detail["runners"]
            .as_array()
            .expect("runners")
            .iter()
            .any(|runner| runner["config_sha"] == "a".repeat(40))
    );
    let mut work = database.begin().await.expect("retire transaction");
    retire_runner_sessions(&mut work, turn)
        .await
        .expect("retire");
    work.commit().await.expect("retire commit");
    let old_status: String =
        sqlx::query_scalar("SELECT status FROM tasks.runner_ref_writes WHERE id=$1")
            .bind(old_write)
            .fetch_one(database.pool())
            .await
            .expect("old ref status");
    assert_eq!(old_status, "obsolete_uncertain");
    publish_runner_session(&database, &context, session)
        .await
        .expect("delete ref");
    assert!(!refs.lock().await.contains_key(&session.to_string()));
    publish_runner_session(&database, &context, session)
        .await
        .expect("confirm absence");
    assert!(
        runner_session(&database, session)
            .await
            .expect("watch session")
            .expect("row")
            .ref_watch
    );
    refs.lock()
        .await
        .insert(session.to_string(), "a".repeat(40));
    publish_runner_session(&database, &context, session)
        .await
        .expect("repair late create");
    assert!(!refs.lock().await.contains_key(&session.to_string()));
    let mut work = database.begin().await.expect("late response transaction");
    finish_ref_write(&mut work, session, old_write, "written", None, None)
        .await
        .expect("account for late create");
    work.commit().await.expect("late response commit");
    publish_runner_session(&database, &context, session)
        .await
        .expect("settle ref watch");
    assert!(
        !runner_session(&database, session)
            .await
            .expect("watch session")
            .expect("row")
            .ref_watch
    );
    assert_eq!(dispatches.load(Ordering::SeqCst), 1);
    server.abort();
}
