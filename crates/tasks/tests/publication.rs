//! Durable request replies against a GitHub App API and PostgreSQL boundary.

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
    http::StatusCode,
    routing::{get, post},
};
use chrono::{Duration, Utc};
use concurro_db::Database;
use concurro_events_postgres::{Outbox, migrate as migrate_events};
use concurro_github_v1::AppClient;
use concurro_tasks::{PublicationContext, PublicationTarget, publish_request_reply};
use concurro_tasks_core::{Actor, Authority, IssueKey, Request, RequestKind, SourceOrder};
use concurro_tasks_postgres::{
    CommentResolution, apply_request, due_comment_readbacks, migrate, pending_effects,
    record_request, resolve_comment,
};
use concurro_testkit::PostgresContainer;
use serde_json::{Value, json};
use tokio::sync::Mutex;
use uuid::Uuid;

#[tokio::test]
async fn uncertain_reply_create_is_adopted_without_a_second_post() {
    let comments = Arc::new(Mutex::new(Vec::<Value>::new()));
    let posts = Arc::new(AtomicUsize::new(0));
    let post_status = Arc::new(AtomicUsize::new(0));
    let post_rate_limited = Arc::new(AtomicBool::new(true));
    let minted = Arc::new(AtomicUsize::new(0));
    let readback_status = Arc::new(AtomicUsize::new(0));
    let mint_fail = Arc::new(AtomicUsize::new(0));
    let app = Router::new()
        .route(
            "/repos/acme/demo/installation",
            get(|| async {
                Json(json!({"id":7,"repository_selection":"selected","account":{"login":"acme"}}))
            }),
        )
        .route(
            "/app/installations/7/access_tokens",
            post({
                let mint_fail = mint_fail.clone();
                let minted = minted.clone();
                move |Json(body): Json<Value>| {
                    let mint_fail = mint_fail.clone();
                    let minted = minted.clone();
                    async move {
                        assert_eq!(body["repositories"], json!(["demo"]));
                        assert!(
                            body["permissions"] == json!({"issues":"write"})
                                || body["permissions"] == json!({"issues":"read"})
                        );
                        if mint_fail.load(Ordering::SeqCst) != 0 {
                            (
                                StatusCode::SERVICE_UNAVAILABLE,
                                Json(json!({"message":"mint unavailable"})),
                            )
                        } else {
                            minted.fetch_add(1, Ordering::SeqCst);
                            (
                                StatusCode::OK,
                                Json(
                                    json!({"token":"scoped-token","permissions":body["permissions"],
                                "expires_at":(Utc::now()+Duration::minutes(55)).to_rfc3339()}),
                                ),
                            )
                        }
                    }
                }
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
                let comments = comments.clone();
                let readback_status = readback_status.clone();
                move || {
                    let comments = comments.clone();
                    let readback_status = readback_status.clone();
                    async move {
                        match readback_status.load(Ordering::SeqCst) {
                            403 => (
                                StatusCode::FORBIDDEN,
                                Json(json!({"message":"rate limited"})),
                            ),
                            404 => (StatusCode::NOT_FOUND, Json(json!({"message":"not found"}))),
                            _ => (StatusCode::OK, Json(json!(comments.lock().await.clone()))),
                        }
                    }
                }
            })
            .post({
                let comments = comments.clone();
                let posts = posts.clone();
                let post_status = post_status.clone();
                let post_rate_limited = post_rate_limited.clone();
                move |Json(body): Json<Value>| {
                    let comments = comments.clone();
                    let posts = posts.clone();
                    let post_status = post_status.clone();
                    let post_rate_limited = post_rate_limited.clone();
                    async move {
                        let count = posts.fetch_add(1, Ordering::SeqCst);
                        let refused = post_status.load(Ordering::SeqCst);
                        if refused != 0 {
                            return (
                                StatusCode::from_u16(refused as u16).expect("refusal status"),
                                Json(json!({"message": if refused == 403 && post_rate_limited.load(Ordering::SeqCst) { "secondary rate limit" } else { "rejected" }})),
                            );
                        }
                        let now = Utc::now().to_rfc3339();
                        comments
                            .lock()
                            .await
                            .push(json!({"id":101,"body":body["body"],
                        "user":{"id":10,"login":"concurro[bot]","type":"Bot"},
                        "author_association":"MEMBER","created_at":now,"updated_at":now,
                        "html_url":"https://github.example/acme/demo/issues/12#issuecomment-101"}));
                        if count == 0 {
                            (
                                StatusCode::BAD_GATEWAY,
                                Json(json!({"message":"lost response"})),
                            )
                        } else {
                            (
                                StatusCode::OK,
                                Json(comments.lock().await.last().cloned().unwrap()),
                            )
                        }
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
    let mut decision = database.begin().await.expect("decision");
    record_request(&mut decision, &request)
        .await
        .expect("record request");
    apply_request(
        &mut decision,
        &outbox,
        request.id,
        request.issue,
        Authority {
            writer: false,
            issue_open: true,
        },
    )
    .await
    .expect("refuse request")
    .expect("decision");
    decision.commit().await.expect("commit decision");
    let (write_id, status): (Uuid, String) =
        sqlx::query_as("SELECT id,status FROM tasks.publication_comments WHERE request_id=$1")
            .bind(request.id)
            .fetch_one(database.pool())
            .await
            .expect("reply intent");
    assert_eq!(status, "recorded");
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
    mint_fail.store(1, Ordering::SeqCst);
    assert!(
        publish_request_reply(&database, &publication, write_id)
            .await
            .is_err()
    );
    let preflight_status: String =
        sqlx::query_scalar("SELECT status FROM tasks.publication_comments WHERE id=$1")
            .bind(write_id)
            .fetch_one(database.pool())
            .await
            .expect("preflight state");
    assert_eq!(preflight_status, "recorded");
    assert_eq!(posts.load(Ordering::SeqCst), 0);
    mint_fail.store(0, Ordering::SeqCst);
    assert!(
        publish_request_reply(&database, &publication, write_id)
            .await
            .is_err()
    );
    assert_eq!(posts.load(Ordering::SeqCst), 1);
    for status in [403, 404] {
        readback_status.store(status, Ordering::SeqCst);
        assert!(
            publish_request_reply(&database, &publication, write_id)
                .await
                .is_err()
        );
        let stored: String =
            sqlx::query_scalar("SELECT status FROM tasks.publication_comments WHERE id=$1")
                .bind(write_id)
                .fetch_one(database.pool())
                .await
                .expect("uncertain state");
        assert_eq!(stored, "sending");
        assert_eq!(posts.load(Ordering::SeqCst), 1);
    }
    readback_status.store(0, Ordering::SeqCst);
    let pending = pending_effects(&database).await.expect("pending writes");
    assert_eq!(pending.len(), 1);
    assert_eq!(pending[0]["resolution"], "readback_or_operator");
    let mut operator = database.begin().await.expect("operator transaction");
    assert!(
        resolve_comment(
            &mut operator,
            write_id,
            "host-user",
            CommentResolution::Failed
        )
        .await
        .expect("resolve failed")
    );
    operator.commit().await.expect("operator commit");
    let action: String =
        sqlx::query_scalar("SELECT action FROM tasks.operator_actions WHERE target=$1")
            .bind(write_id.to_string())
            .fetch_one(database.pool())
            .await
            .expect("operator audit");
    assert_eq!(action, "writes.resolve.failed");
    sqlx::query(
        "UPDATE tasks.publication_comments SET updated_at=now()-interval '31 seconds' WHERE id=$1",
    )
    .bind(write_id)
    .execute(database.pool())
    .await
    .expect("age uncertain create");
    assert_eq!(
        due_comment_readbacks(&database)
            .await
            .expect("readback scan"),
        vec![write_id]
    );
    publish_request_reply(&database, &publication, write_id)
        .await
        .expect("readback");
    publish_request_reply(&database, &publication, write_id)
        .await
        .expect("settled replay");
    assert_eq!(posts.load(Ordering::SeqCst), 1);
    let (status, comment_id): (String, i64) =
        sqlx::query_as("SELECT status,comment_id FROM tasks.publication_comments WHERE id=$1")
            .bind(write_id)
            .fetch_one(database.pool())
            .await
            .expect("settled reply");
    assert_eq!((status.as_str(), comment_id), ("written", 101));
    assert!(
        pending_effects(&database)
            .await
            .expect("settled effects")
            .is_empty()
    );
    let mut retry_write = None;
    for (offset, resolution) in [
        (
            1,
            CommentResolution::Written {
                comment_id: 202,
                url: "https://github.example/acme/demo/issues/12#issuecomment-202",
            },
        ),
        (2, CommentResolution::Resend),
    ] {
        let later = Request {
            id: Uuid::now_v7(),
            reply: format!("issue_comment:{}", 5 + offset),
            comment_id: Some(5 + offset),
            order: SourceOrder {
                at: Utc::now(),
                source_id: 5 + offset,
                source: format!("comment:{}", 5 + offset),
                index: 0,
            },
            ..request.clone()
        };
        let mut work = database.begin().await.expect("later request");
        record_request(&mut work, &later)
            .await
            .expect("record later");
        apply_request(
            &mut work,
            &outbox,
            later.id,
            later.issue,
            Authority {
                writer: false,
                issue_open: true,
            },
        )
        .await
        .expect("refuse later");
        work.commit().await.expect("later commit");
        let later_write: Uuid =
            sqlx::query_scalar("SELECT id FROM tasks.publication_comments WHERE request_id=$1")
                .bind(later.id)
                .fetch_one(database.pool())
                .await
                .expect("later write");
        let mut work = database.begin().await.expect("operator resolution");
        assert!(
            resolve_comment(&mut work, later_write, "host-user", resolution)
                .await
                .expect("resolve later")
        );
        work.commit().await.expect("resolution commit");
        let (status, url): (String, Option<String>) =
            sqlx::query_as("SELECT status,comment_url FROM tasks.publication_comments WHERE id=$1")
                .bind(later_write)
                .fetch_one(database.pool())
                .await
                .expect("resolution row");
        if offset == 1 {
            assert_eq!(status, "written");
            assert_eq!(
                url.as_deref(),
                Some("https://github.example/acme/demo/issues/12#issuecomment-202")
            );
        } else {
            assert_eq!(status, "recorded");
            assert!(url.is_none());
            retry_write = Some(later_write);
        }
    }
    let retry_write = retry_write.expect("operator resend");
    post_status.store(403, Ordering::SeqCst);
    post_rate_limited.store(false, Ordering::SeqCst);
    publish_request_reply(&database, &publication, retry_write)
        .await
        .expect("plain forbidden refusal");
    let refused: String =
        sqlx::query_scalar("SELECT status FROM tasks.publication_comments WHERE id=$1")
            .bind(retry_write)
            .fetch_one(database.pool())
            .await
            .expect("forbidden refusal");
    assert_eq!(refused, "failed");
    let mut manual = database.begin().await.expect("manual resend");
    assert!(
        resolve_comment(
            &mut manual,
            retry_write,
            "host-user",
            CommentResolution::Resend
        )
        .await
        .expect("operator override")
    );
    manual.commit().await.expect("override commit");
    post_rate_limited.store(true, Ordering::SeqCst);
    for refused in [401, 429, 403] {
        post_status.store(refused, Ordering::SeqCst);
        publish_request_reply(&database, &publication, retry_write)
            .await
            .expect("definitive refusal deferred");
        let (status, failure, waiting): (String, String, bool) = sqlx::query_as(
            "SELECT status,failure,retry_at>now() FROM tasks.publication_comments WHERE id=$1",
        )
        .bind(retry_write)
        .fetch_one(database.pool())
        .await
        .expect("deferred reply");
        assert_eq!(status, "recorded");
        assert_eq!(failure, format!("http_{refused}"));
        assert!(waiting);
        assert!(
            publish_request_reply(&database, &publication, retry_write)
                .await
                .is_ok()
        );
        sqlx::query(
            "UPDATE tasks.publication_comments SET retry_at=now()-interval '1 second' WHERE id=$1",
        )
        .bind(retry_write)
        .execute(database.pool())
        .await
        .expect("advance retry clock");
        assert_eq!(
            due_comment_readbacks(&database)
                .await
                .expect("deferred scan"),
            vec![retry_write]
        );
    }
    assert!(minted.load(Ordering::SeqCst) >= 2);
    post_status.store(0, Ordering::SeqCst);
    publish_request_reply(&database, &publication, retry_write)
        .await
        .expect("retried reply accepted");
    let final_status: String =
        sqlx::query_scalar("SELECT status FROM tasks.publication_comments WHERE id=$1")
            .bind(retry_write)
            .fetch_one(database.pool())
            .await
            .expect("accepted reply");
    assert_eq!(final_status, "written");
    server.abort();
}
