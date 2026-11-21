//! Managed issue labels against a scoped App and durable task desire.

use std::{
    collections::{BTreeMap, HashMap, HashSet},
    process::Command,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
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
use concurro_events_postgres::{Outbox, migrate as migrate_events};
use concurro_github_v1::AppClient;
use concurro_tasks::{PublicationContext, PublicationTarget, publish_stage_label};
use concurro_tasks_core::{Actor, Authority, IssueKey, Request, RequestKind, SourceOrder};
use concurro_tasks_postgres::{
    apply_request, begin_stage_label_write, finish_stage_label_write, migrate, pending_effects,
    record_request, record_stage_label_desire, stage_label_surface, task, task_detail,
};
use concurro_testkit::PostgresContainer;
use serde_json::{Value, json};
use tokio::sync::{Mutex, Notify};
use uuid::Uuid;

/// Reconcile a stage label while preserving human labels and recording every send.
#[tokio::test]
async fn stage_label_create_add_remove_and_readback_are_durable() {
    let issue_labels = Arc::new(Mutex::new(vec![
        "human".to_owned(),
        "concurro:stage:plan".to_owned(),
    ]));
    let repository_labels = Arc::new(Mutex::new(HashSet::<String>::new()));
    let hold_remove = Arc::new(AtomicBool::new(false));
    let remove_started = Arc::new(Notify::new());
    let remove_release = Arc::new(Notify::new());
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
                assert_eq!(body["repositories"], json!(["demo"]));
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
            "/repos/acme/demo/labels/{label}",
            get({
                let repository_labels = Arc::clone(&repository_labels);
                move |Path(label): Path<String>| {
                    let repository_labels = Arc::clone(&repository_labels);
                    async move {
                        if repository_labels.lock().await.contains(&label) {
                            (StatusCode::OK, Json(json!({"name":label})))
                        } else {
                            (StatusCode::NOT_FOUND, Json(json!({"message":"missing"})))
                        }
                    }
                }
            }),
        )
        .route(
            "/repos/acme/demo/labels",
            post({
                let repository_labels = Arc::clone(&repository_labels);
                move |Json(body): Json<Value>| {
                    let repository_labels = Arc::clone(&repository_labels);
                    async move {
                        let label = body["name"].as_str().expect("label name");
                        assert!(label.starts_with("concurro:stage:"));
                        repository_labels.lock().await.insert(label.to_owned());
                        Json(body)
                    }
                }
            }),
        )
        .route(
            "/repos/acme/demo/issues/12/labels",
            get({
                let issue_labels = Arc::clone(&issue_labels);
                move || {
                    let issue_labels = Arc::clone(&issue_labels);
                    async move {
                        Json(json!(
                            issue_labels
                                .lock()
                                .await
                                .iter()
                                .map(|name| json!({"name":name}))
                                .collect::<Vec<_>>()
                        ))
                    }
                }
            })
            .post({
                let issue_labels = Arc::clone(&issue_labels);
                move |Json(body): Json<Value>| {
                    let issue_labels = Arc::clone(&issue_labels);
                    async move {
                        let label = body["labels"][0].as_str().expect("added label");
                        issue_labels.lock().await.push(label.to_owned());
                        Json(json!([]))
                    }
                }
            }),
        )
        .route(
            "/repos/acme/demo/issues/12/labels/{label}",
            axum::routing::delete({
                let issue_labels = Arc::clone(&issue_labels);
                let hold_remove = Arc::clone(&hold_remove);
                let remove_started = Arc::clone(&remove_started);
                let remove_release = Arc::clone(&remove_release);
                move |Path(label): Path<String>| {
                    let issue_labels = Arc::clone(&issue_labels);
                    let hold_remove = Arc::clone(&hold_remove);
                    let remove_started = Arc::clone(&remove_started);
                    let remove_release = Arc::clone(&remove_release);
                    async move {
                        if label == "concurro:stage:intake" && hold_remove.load(Ordering::SeqCst) {
                            remove_started.notify_one();
                            remove_release.notified().await;
                        }
                        issue_labels.lock().await.retain(|name| name != &label);
                        StatusCode::NO_CONTENT
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
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    migrate_events(&database).await.expect("events schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "task.>".to_owned(),
            "request.>".to_owned(),
            "turn.>".to_owned(),
        ],
    );
    let request = Request {
        id: Uuid::now_v7(),
        channel: "comment".to_owned(),
        reply: "issue_comment:5".to_owned(),
        comment_id: Some(5),
        issue: IssueKey {
            repository_id: 8,
            number: 12,
        },
        issue_author: Actor {
            id: 1,
            login: "author".to_owned(),
        },
        actor: Actor {
            id: 2,
            login: "writer".to_owned(),
        },
        kind: RequestKind::Start,
        order: SourceOrder {
            at: Utc::now(),
            source_id: 5,
            source: "comment:5".to_owned(),
            index: 0,
        },
    };
    let mut decision = database.begin().await.expect("decision");
    record_request(&mut decision, &request)
        .await
        .expect("request");
    apply_request(
        &mut decision,
        &outbox,
        request.id,
        request.issue,
        Authority {
            writer: true,
            issue_open: true,
        },
    )
    .await
    .expect("start");
    decision.commit().await.expect("start commit");
    let state = task(&database, request.issue)
        .await
        .expect("task")
        .expect("started");
    let initial: (Option<String>, bool) =
        sqlx::query_as("SELECT desired,watch FROM tasks.stage_label_surfaces WHERE task_id=$1")
            .bind(state.id.0)
            .fetch_one(database.pool())
            .await
            .expect("initial label desire");
    assert_eq!(initial, (None, true));
    sqlx::query(
        "UPDATE tasks.stage_label_surfaces SET revision=revision+1, \
         desired='concurro:stage:intake' WHERE task_id=$1",
    )
    .bind(state.id.0)
    .execute(database.pool())
    .await
    .expect("intake desire");
    let publication = PublicationContext {
        github,
        targets: HashMap::from([(
            8,
            PublicationTarget {
                repository: "acme/demo".to_owned(),
                read_permissions: BTreeMap::from([("issues".to_owned(), "read".to_owned())]),
                write_permissions: BTreeMap::from([("issues".to_owned(), "write".to_owned())]),
                bot_login: "concurro[bot]".to_owned(),
            },
        )]),
        standalone_targets: HashMap::new(),
    };
    for _ in 0..4 {
        publish_stage_label(&database, &publication, state.id.0)
            .await
            .expect("reconcile stage label");
    }
    assert_eq!(
        issue_labels.lock().await.as_slice(),
        ["human", "concurro:stage:intake"]
    );
    let watched: bool =
        sqlx::query_scalar("SELECT watch FROM tasks.stage_label_surfaces WHERE task_id=$1")
            .bind(state.id.0)
            .fetch_one(database.pool())
            .await
            .expect("observed label");
    assert!(!watched);
    let writes: Vec<(String, String)> = sqlx::query_as(
        "SELECT action,status FROM tasks.stage_label_writes WHERE task_id=$1 ORDER BY id",
    )
    .bind(state.id.0)
    .fetch_all(database.pool())
    .await
    .expect("physical writes");
    assert_eq!(
        writes,
        vec![
            ("create".to_owned(), "written".to_owned()),
            ("add".to_owned(), "written".to_owned()),
            ("remove".to_owned(), "written".to_owned()),
        ]
    );
    repository_labels
        .lock()
        .await
        .insert("concurro:stage:stopped".to_owned());
    sqlx::query(
        "UPDATE tasks.stage_label_surfaces SET revision=revision+1, \
         desired='concurro:stage:stopped',watch=true WHERE task_id=$1",
    )
    .bind(state.id.0)
    .execute(database.pool())
    .await
    .expect("stopped desire");
    publish_stage_label(&database, &publication, state.id.0)
        .await
        .expect("attach stopped label");
    hold_remove.store(true, Ordering::SeqCst);
    let late_database = database.clone();
    let late_publication = publication.clone();
    let late_task = state.id.0;
    let late_remove = tokio::spawn(async move {
        publish_stage_label(&late_database, &late_publication, late_task).await
    });
    tokio::time::timeout(std::time::Duration::from_secs(5), remove_started.notified())
        .await
        .expect("late removal started");
    sqlx::query(
        "UPDATE tasks.stage_label_surfaces SET revision=revision+1, \
         desired='concurro:stage:intake',watch=true WHERE task_id=$1",
    )
    .bind(state.id.0)
    .execute(database.pool())
    .await
    .expect("intake restored");
    publish_stage_label(&database, &publication, state.id.0)
        .await
        .expect("remove stopped label");
    publish_stage_label(&database, &publication, state.id.0)
        .await
        .expect("matching label read");
    let watched: bool =
        sqlx::query_scalar("SELECT watch FROM tasks.stage_label_surfaces WHERE task_id=$1")
            .bind(state.id.0)
            .fetch_one(database.pool())
            .await
            .expect("uncertain watch");
    assert!(watched);
    let pending = pending_effects(&database).await.expect("uncertain writes");
    assert!(pending.iter().any(|write| {
        write["kind"] == "stage_label"
            && write["status"] == "sending"
            && write["label"] == "concurro:stage:intake"
    }));
    let detail = task_detail(&database, request.issue)
        .await
        .expect("task detail")
        .expect("task");
    assert_eq!(detail["stage_label"]["watch"], true);
    remove_release.notify_one();
    late_remove
        .await
        .expect("late remove join")
        .expect("late remove result");
    assert_eq!(issue_labels.lock().await.as_slice(), ["human"]);
    publish_stage_label(&database, &publication, state.id.0)
        .await
        .expect("repair desired intake label");
    publish_stage_label(&database, &publication, state.id.0)
        .await
        .expect("verify repaired label");
    assert_eq!(
        issue_labels.lock().await.as_slice(),
        ["human", "concurro:stage:intake"]
    );
    let watched: bool =
        sqlx::query_scalar("SELECT watch FROM tasks.stage_label_surfaces WHERE task_id=$1")
            .bind(state.id.0)
            .fetch_one(database.pool())
            .await
            .expect("settled watch");
    assert!(!watched);

    let revision: i64 =
        sqlx::query_scalar("SELECT revision FROM tasks.stage_label_surfaces WHERE task_id=$1")
            .bind(state.id.0)
            .fetch_one(database.pool())
            .await
            .expect("current revision");
    let old_write: i64 = sqlx::query_scalar(
        "INSERT INTO tasks.stage_label_writes (task_id,revision,action,label,status) \
         VALUES ($1,$2,'add','concurro:stage:intake','sending') RETURNING id",
    )
    .bind(state.id.0)
    .bind(revision)
    .fetch_one(database.pool())
    .await
    .expect("old uncertain add");
    let mut stopped = state.clone();
    stopped.revision = u64::try_from(revision + 1).expect("revision");
    let mut desire = database.begin().await.expect("desire transaction");
    record_stage_label_desire(&mut desire, &stopped, Some("stopped"))
        .await
        .expect("new desire");
    desire.commit().await.expect("desire commit");
    let old_status: String =
        sqlx::query_scalar("SELECT status FROM tasks.stage_label_writes WHERE id=$1")
            .bind(old_write)
            .fetch_one(database.pool())
            .await
            .expect("old status");
    assert_eq!(old_status, "obsolete_uncertain");
    let surface = stage_label_surface(&database, state.id.0)
        .await
        .expect("surface")
        .expect("watched surface");
    let mut refused = database.begin().await.expect("write transaction");
    let refused_write =
        begin_stage_label_write(&mut refused, &surface, "add", "concurro:stage:stopped")
            .await
            .expect("begin current write")
            .expect("current write");
    refused.commit().await.expect("begin commit");
    let mut settled = database.begin().await.expect("settle transaction");
    finish_stage_label_write(
        &mut settled,
        refused_write,
        state.id.0,
        "failed",
        Some("github_client_refusal"),
        None,
    )
    .await
    .expect("definite refusal");
    settled.commit().await.expect("settle commit");
    let (watch, error): (bool, Option<String>) =
        sqlx::query_as("SELECT watch,last_error FROM tasks.stage_label_surfaces WHERE task_id=$1")
            .bind(state.id.0)
            .fetch_one(database.pool())
            .await
            .expect("watch after refusal");
    assert!(watch);
    assert_eq!(error.as_deref(), Some("github_client_refusal"));
    hold_remove.store(false, Ordering::SeqCst);
    issue_labels.lock().await.retain(|label| label == "human");
    publish_stage_label(&database, &publication, state.id.0)
        .await
        .expect("attach current label");
    issue_labels
        .lock()
        .await
        .push("concurro:stage:intake".to_owned());
    publish_stage_label(&database, &publication, state.id.0)
        .await
        .expect("repair late old add");
    assert_eq!(
        issue_labels.lock().await.as_slice(),
        ["human", "concurro:stage:stopped"]
    );
    let mut accounted = database.begin().await.expect("old write transaction");
    finish_stage_label_write(&mut accounted, old_write, state.id.0, "written", None, None)
        .await
        .expect("late old response");
    accounted.commit().await.expect("old write commit");
    publish_stage_label(&database, &publication, state.id.0)
        .await
        .expect("final readback");
    let watch: bool =
        sqlx::query_scalar("SELECT watch FROM tasks.stage_label_surfaces WHERE task_id=$1")
            .bind(state.id.0)
            .fetch_one(database.pool())
            .await
            .expect("settled watch");
    assert!(!watch);
    server.abort();
}
