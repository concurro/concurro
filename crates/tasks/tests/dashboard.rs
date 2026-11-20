//! Dashboard create and edit recovery against a scoped App and PostgreSQL.

use std::{
    collections::{BTreeMap, HashMap},
    process::Command,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
};

use axum::{Json, Router, extract::Path, http::StatusCode, routing::get};
use chrono::{Duration, Utc};
use concurro_db::Database;
use concurro_events_postgres::{Outbox, migrate as migrate_events};
use concurro_github_v1::AppClient;
use concurro_tasks::{PublicationContext, PublicationTarget, publish_dashboard};
use concurro_tasks_core::{Actor, Authority, IssueKey, Request, RequestKind, SourceOrder, Stage};
use concurro_tasks_postgres::{
    apply_request, begin_dashboard_edit, dashboard_surface, finish_dashboard_edit, migrate,
    record_dashboard_desire, record_request, task, task_detail,
};
use concurro_testkit::PostgresContainer;
use serde_json::{Value, json};
use tokio::sync::Mutex;
use uuid::Uuid;

/// Recover an uncertain create and edit only the one managed comment for each revision.
#[tokio::test]
async fn dashboard_create_readback_and_latest_revision_edit_survive_restart() {
    let comments = Arc::new(Mutex::new(Vec::<Value>::new()));
    let posts = Arc::new(AtomicUsize::new(0));
    let patches = Arc::new(AtomicUsize::new(0));
    let app = Router::new()
        .route(
            "/repos/acme/demo/installation",
            get(|| async {
                Json(json!({"id":7,"repository_selection":"selected","account":{"login":"acme"}}))
            }),
        )
        .route(
            "/app/installations/7/access_tokens",
            axum::routing::post(|Json(body): Json<Value>| async move {
                Json(json!({"token":"scoped-token","permissions":body["permissions"],
                    "expires_at":(Utc::now()+Duration::minutes(55)).to_rfc3339()}))
            }),
        )
        .route(
            "/installation/repositories",
            get(|| async {
                Json(json!({"total_count":1,"repositories":[{"full_name":"acme/demo"}]}))
            }),
        )
        .route(
            "/repos/acme/demo/issues/12/comments",
            get({
                let comments = Arc::clone(&comments);
                move || {
                    let comments = Arc::clone(&comments);
                    async move { Json(json!(comments.lock().await.clone())) }
                }
            })
            .post({
                let comments = Arc::clone(&comments);
                let posts = Arc::clone(&posts);
                move |Json(body): Json<Value>| {
                    let comments = Arc::clone(&comments);
                    let posts = Arc::clone(&posts);
                    async move {
                        let comment = json!({"id":101,"body":body["body"],
                            "user":{"id":10,"login":"concurro[bot]","type":"Bot"},
                            "author_association":"MEMBER",
                            "created_at":Utc::now().to_rfc3339(),
                            "updated_at":Utc::now().to_rfc3339(),
                            "html_url":"https://github.example/acme/demo/issues/12#issuecomment-101"});
                        comments.lock().await.push(comment.clone());
                        if posts.fetch_add(1, Ordering::SeqCst) == 0 {
                            (StatusCode::BAD_GATEWAY, Json(json!({"message":"lost response"})))
                        } else {
                            (StatusCode::OK, Json(comment))
                        }
                    }
                }
            }),
        )
        .route(
            "/repos/acme/demo/issues/comments/{id}",
            get({
                let comments = Arc::clone(&comments);
                move |Path(id): Path<i64>| {
                    let comments = Arc::clone(&comments);
                    async move {
                        assert_eq!(id, 101);
                        Json(comments.lock().await[0].clone())
                    }
                }
            })
            .patch({
                let comments = Arc::clone(&comments);
                let patches = Arc::clone(&patches);
                move |Path(id): Path<i64>, Json(body): Json<Value>| {
                    let comments = Arc::clone(&comments);
                    let patches = Arc::clone(&patches);
                    async move {
                        assert_eq!(id, 101);
                        patches.fetch_add(1, Ordering::SeqCst);
                        let mut comments = comments.lock().await;
                        comments[0]["body"] = body["body"].clone();
                        Json(comments[0].clone())
                    }
                }
            }),
        );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind App");
    let address = listener.local_addr().expect("App address");
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
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    migrate_events(&database).await.expect("events schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "request.>".to_owned(),
            "task.>".to_owned(),
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
    let mut work = database.begin().await.expect("request transaction");
    record_request(&mut work, &request)
        .await
        .expect("record request");
    apply_request(
        &mut work,
        &outbox,
        request.id,
        request.issue,
        Authority {
            writer: true,
            issue_open: true,
        },
    )
    .await
    .expect("apply request");
    work.commit().await.expect("commit request");
    let state = task(&database, request.issue)
        .await
        .expect("task read")
        .expect("task");
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
    };
    assert!(
        publish_dashboard(&database, &publication, state.id.0)
            .await
            .is_err()
    );
    assert_eq!(posts.load(Ordering::SeqCst), 1);
    assert_eq!(comments.lock().await.len(), 1);
    publish_dashboard(&database, &publication, state.id.0)
        .await
        .expect("readback after restart");
    assert_eq!(posts.load(Ordering::SeqCst), 1);
    let surface = dashboard_surface(&database, state.id.0)
        .await
        .expect("surface")
        .expect("watched");
    assert_eq!(surface.comment_id, Some(101));
    let old_body = surface.desired_body.clone();
    let mut work = database.begin().await.expect("old edit transaction");
    let old_write = begin_dashboard_edit(&mut work, &surface)
        .await
        .expect("record old edit")
        .expect("old edit claim");
    work.commit().await.expect("commit old edit");

    let next = concurro_tasks_core::TaskState {
        revision: state.revision + 1,
        stage: Stage::Blocked,
        stage_reason: Some("Need owner | review\nbefore continuing".to_owned()),
        ..state
    };
    let mut work = database.begin().await.expect("revision transaction");
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(next.id.0)
        .bind(i64::try_from(next.revision).expect("revision"))
        .bind(sqlx::types::Json(&next))
        .execute(work.connection())
        .await
        .expect("update task");
    record_dashboard_desire(&mut work, &next)
        .await
        .expect("record dashboard desire");
    work.commit().await.expect("commit revision");
    publish_dashboard(&database, &publication, next.id.0)
        .await
        .expect("edit dashboard");
    assert_eq!(posts.load(Ordering::SeqCst), 1);
    assert_eq!(patches.load(Ordering::SeqCst), 1);
    publish_dashboard(&database, &publication, next.id.0)
        .await
        .expect("idempotent restart");
    assert_eq!(patches.load(Ordering::SeqCst), 1);
    comments.lock().await[0]["body"] = json!(old_body);
    let mut work = database.begin().await.expect("late edit transaction");
    finish_dashboard_edit(&mut work, next.id.0, old_write, "written", None, None)
        .await
        .expect("late old edit");
    work.commit().await.expect("commit late edit");
    publish_dashboard(&database, &publication, next.id.0)
        .await
        .expect("restore latest dashboard");
    assert_eq!(patches.load(Ordering::SeqCst), 2);
    assert!(
        comments.lock().await[0]["body"]
            .as_str()
            .expect("body")
            .contains("Blocked")
    );
    let detail = task_detail(&database, request.issue)
        .await
        .expect("detail")
        .expect("task");
    assert_eq!(detail["dashboard"]["comment_id"], 101);
    assert_eq!(detail["dashboard"]["writes"][0]["status"], "written");
    assert_eq!(detail["dashboard"]["writes"][1]["status"], "written");
    server.abort();
}
