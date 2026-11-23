//! A comment request and its exchange effects through durable boundaries.

use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, AtomicUsize, Ordering},
};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    process::Command,
};

use axum::{
    Json, Router,
    body::Bytes,
    http::{HeaderMap, StatusCode},
    routing::{get, post},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use chrono::{DateTime, Duration, Utc};
use concurro_contracts::{
    GithubEvent, GithubIssues, IssueCommentPayload, ReadModelRecord, TaskNotice, TaskReport,
    TurnFinished, TurnStatus, TurnSubmitted, decode,
};
use concurro_db::Database;
use concurro_events::ProcessResult;
use concurro_events_postgres::{Outbox, migrate as migrate_events, process_delivery};
use concurro_exchange_v1::Operator;
use concurro_github_v1::AppClient;
use concurro_tasks::{
    CommentHandler, DecisionContext, DecisionTarget, ExchangeContext, ExchangeEffectJob,
    ExchangePollJob, InitializationTarget, LabelScanJob, TaskDecisionJob, run_exchange_effect,
    run_exchange_poll, run_task_decision, scan_label_wake,
};
use concurro_tasks_core::{
    Actor, Admission, AttemptSpec, Authority, InitializationSnapshot, IssueKey, LaterCommand,
    PinnedInputs, Request, RequestKind, Requirements, SourceOrder, Stage,
};
use concurro_tasks_postgres::{
    admit_intake, apply_request, claim_effect, enqueue_label_scan, initialize_task, migrate,
    next_pending, pending_effects, pending_label_wakes, record_request, task,
};
use concurro_testkit::PostgresContainer;
use graphile_worker::WorkerOptions;
use serde_json::{Value, json};
use uuid::Uuid;

/// Supply exact source inputs for pipeline transaction tests.
fn snapshot(pins: PinnedInputs) -> InitializationSnapshot {
    InitializationSnapshot {
        pins,
        configuration: json!({"repository": "acme/fork"}),
        requirements: Requirements {
            title: "Task".to_owned(),
            body: Some("Description".to_owned()),
            discussion: vec![],
        },
        charter: "{\"version\":1,\"goals\":{\"G-1\":\"Goal\"}}".to_owned(),
        instruction_texts: BTreeMap::new(),
        policy_documents: BTreeMap::new(),
        base_files: vec!["src/lib.rs".to_owned()],
        bundle_id: "bundle-identity".to_owned(),
    }
}

#[tokio::test]
async fn label_scan_records_timeline_sources_once_across_repeated_wakes() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    migrate_events(&database).await.expect("events schema");
    let final_event_visible = Arc::new(AtomicBool::new(false));
    let app = Router::new()
        .route("/repos/acme/demo/installation", get(|| async {
            Json(json!({"id": 7, "repository_selection": "selected", "account": {"login": "acme"}}))
        }))
        .route("/app/installations/7/access_tokens", post(|Json(body): Json<Value>| async move {
            Json(json!({"token": "scoped-token", "permissions": body["permissions"],
                "expires_at": (Utc::now() + Duration::minutes(55)).to_rfc3339()}))
        }))
        .route("/installation/repositories", get(|| async {
            Json(json!({"total_count": 1, "repositories": [{"full_name": "acme/demo"}]}))
        }))
        .route("/users/writer", get(|| async {
            Json(json!({"id": 7, "login": "writer"}))
        }))
        .route("/users/other", get(|| async {
            Json(json!({"id": 8, "login": "other"}))
        }))
        .route("/repos/acme/demo/collaborators/{login}/permission", get(|| async {
            Json(json!({"permission": "write"}))
        }))
        .route("/repos/acme/demo", get(|| async {
            Json(json!({"default_branch": "main"}))
        }))
        .route("/repos/acme/demo/git/ref/heads/main", get(|| async {
            Json(json!({"object": {"sha": "a".repeat(40)}}))
        }))
        .route("/repos/acme/demo/contents/.concurro/config.yaml", get(|| async {
            let config = "repository: acme/demo\nbase: main\nbot_login: concurro[bot]\nautonomous: false\nmax_model_calls: 3\nmax_fix_rounds: 2\nmax_plan_revisions: 1\nmax_context_bytes: 350000\npolicy_documents: [README.md]\nqc: codex\nplanner: codex\nimplementer: codex\nreviewers: [codex]\nmodels: {codex: gpt-test}\npaths: {protected: [], protected_extensions: [], protected_names: [], protected_name_prefixes: [], protected_files: [], generated: []}\n";
            Json(json!({"type": "file", "encoding": "base64", "path": ".concurro/config.yaml",
                "size": config.len(), "content": STANDARD.encode(config)}))
        }))
        .route("/repos/acme/demo/issues/3", get(|| async {
            Json(json!({"number": 3, "state": "open", "user": {"id": 5, "login": "author"},
                "labels": [{"name": "concurro"}]}))
        }))
        .route("/repos/acme/demo/issues/3/events", get({
            let final_event_visible = final_event_visible.clone();
            move |axum::extract::Query(query): axum::extract::Query<HashMap<String, String>>| {
                let final_event_visible = final_event_visible.clone();
                async move {
            if query.get("page").is_some_and(|page| page != "1") {
                return Json(json!([]));
            }
            let mut events = json!([
                {"id": 101, "event": "labeled", "created_at": "2026-11-20T10:00:00Z",
                    "actor": {"id": 7, "login": "writer"}, "label": {"name": "concurro"}},
                {"id": 102, "event": "unlabeled", "created_at": "2026-11-20T10:00:01Z",
                    "actor": {"id": 8, "login": "other"}, "label": {"name": "concurro"}}
            ]);
            if final_event_visible.load(Ordering::SeqCst) {
                events.as_array_mut().expect("events").push(json!({
                    "id": 103, "event": "labeled", "created_at": "2026-11-20T10:00:02Z",
                    "actor": {"id": 7, "login": "writer"}, "label": {"name": "concurro"}
                }));
            }
            Json(events)
                }
            }
        }));
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
    let context = DecisionContext {
        github: AppClient::new(
            reqwest::Client::new(),
            &format!("http://{address}"),
            42,
            &key.stdout,
        )
        .expect("App identity"),
        targets: HashMap::from([(
            8,
            DecisionTarget {
                repository: "acme/demo".to_owned(),
                read_permissions: BTreeMap::from([("issues".to_owned(), "read".to_owned())]),
                fork: "acme/demo".to_owned(),
                fork_permissions: BTreeMap::new(),
                initialization: Some(InitializationTarget {
                    issue_repository: "acme/demo".to_owned(),
                    fork: "acme/demo".to_owned(),
                    upstream: "acme/demo".to_owned(),
                    issue_permissions: BTreeMap::new(),
                    fork_permissions: BTreeMap::new(),
                    upstream_permissions: BTreeMap::new(),
                    bot_login: "concurro[bot]".to_owned(),
                    publisher_logins: vec![],
                    engine_sha: "b".repeat(40),
                }),
                publication: None,
            },
        )]),
        outbox: Outbox::new(1024 * 1024, vec!["request.>".to_owned()]),
        rust_exchange: false,
    };
    let wake = LabelScanJob {
        delivery_id: "label-delivery".to_owned(),
        repository_id: 8,
        issue_number: 3,
        action: "labeled".to_owned(),
        actor_id: 7,
        updated_at: DateTime::parse_from_rfc3339("2026-11-20T10:00:02Z")
            .expect("time")
            .with_timezone(&Utc),
    };
    let mut recorded = database.begin().await.expect("record label delivery");
    enqueue_label_scan(
        &mut recorded,
        IssueKey {
            repository_id: 8,
            number: 3,
        },
        &wake.delivery_id,
        &wake.action,
        wake.actor_id,
        wake.updated_at,
    )
    .await
    .expect("enqueue scan");
    recorded.commit().await.expect("commit label wake");
    assert!(scan_label_wake(&database, &context, &wake).await.is_err());
    let (attempts, observed): (i32, bool) = sqlx::query_as(
        "SELECT scan_attempts,observed_at IS NOT NULL FROM tasks.label_wakes WHERE delivery_id=$1",
    )
    .bind(&wake.delivery_id)
    .fetch_one(database.pool())
    .await
    .expect("deferred wake");
    assert_eq!((attempts, observed), (1, false));
    assert!(
        pending_label_wakes(&database)
            .await
            .expect("backoff")
            .is_empty()
    );
    final_event_visible.store(true, Ordering::SeqCst);
    scan_label_wake(&database, &context, &wake)
        .await
        .expect("event appeared");
    scan_label_wake(&database, &context, &wake)
        .await
        .expect("repeated scan");
    assert!(
        pending_label_wakes(&database)
            .await
            .expect("settled wakes")
            .is_empty()
    );
    let sources: Vec<(String,)> = sqlx::query_as(
        "SELECT source FROM tasks.requests WHERE channel='label' ORDER BY source_id",
    )
    .fetch_all(database.pool())
    .await
    .expect("label requests");
    assert_eq!(sources.len(), 3);
    assert_eq!(
        sources
            .iter()
            .map(|(source,)| source.as_str())
            .collect::<Vec<_>>(),
        ["issue_label:101", "issue_label:102", "issue_label:103"]
    );
    let first = next_pending(
        &database,
        IssueKey {
            repository_id: 8,
            number: 3,
        },
    )
    .await
    .expect("pending")
    .expect("first request");
    assert_eq!(first.order.source_id, 101);
    run_task_decision(
        &database,
        &context,
        IssueKey {
            repository_id: 8,
            number: 3,
        },
    )
    .await
    .expect("label decisions");
    let dispositions: Vec<(i64, String, Option<String>)> = sqlx::query_as(
        "SELECT source_id,status,reason FROM tasks.requests WHERE channel='label' ORDER BY source_id",
    )
    .fetch_all(database.pool())
    .await
    .expect("label dispositions");
    assert_eq!(
        dispositions,
        vec![
            (
                101,
                "refused".to_owned(),
                Some("label_not_current".to_owned())
            ),
            (102, "applied".to_owned(), None),
            (
                103,
                "refused".to_owned(),
                Some("autonomous_disabled".to_owned())
            ),
        ]
    );
    assert!(
        task(
            &database,
            IssueKey {
                repository_id: 8,
                number: 3,
            }
        )
        .await
        .expect("no autonomous task")
        .is_none()
    );

    let invalid = LabelScanJob {
        delivery_id: "unavailable-target".to_owned(),
        repository_id: 999,
        ..wake
    };
    let mut recorded = database.begin().await.expect("record failed scan");
    enqueue_label_scan(
        &mut recorded,
        IssueKey {
            repository_id: invalid.repository_id,
            number: invalid.issue_number,
        },
        &invalid.delivery_id,
        &invalid.action,
        invalid.actor_id,
        invalid.updated_at,
    )
    .await
    .expect("enqueue failed scan");
    recorded.commit().await.expect("commit failed scan");
    assert!(
        scan_label_wake(&database, &context, &invalid)
            .await
            .is_err()
    );
    sqlx::query("UPDATE tasks.label_wakes SET scan_attempts=47 WHERE delivery_id=$1")
        .bind(&invalid.delivery_id)
        .execute(database.pool())
        .await
        .expect("prepare exhausted scan");
    assert!(
        scan_label_wake(&database, &context, &invalid)
            .await
            .is_err()
    );
    let exhausted = pending_effects(&database)
        .await
        .expect("operator diagnostics")
        .into_iter()
        .find(|effect| effect["delivery_id"] == invalid.delivery_id)
        .expect("exhausted wake visible");
    assert_eq!(exhausted["scan_attempts"], 48);
    assert_eq!(exhausted["resolution"], "reconciliation_required");
    assert!(
        pending_label_wakes(&database)
            .await
            .expect("exhausted wake not retried")
            .is_empty()
    );
    server.abort();
}

#[tokio::test]
async fn decision_jobs_recheck_github_before_applying_each_request() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 4)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks");
    migrate_events(&database).await.expect("events");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "request.>".to_owned(),
            "task.>".to_owned(),
            "turn.>".to_owned(),
        ],
    );
    let handler = CommentHandler::new([8], outbox.clone()).with_owned_accounts(HashMap::from([(
        8,
        HashSet::from(["concurro[bot]".to_owned()]),
    )]));
    let open = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let app = Router::new()
        .route(
            "/repos/acme/demo/installation",
            get(|| async {
                Json(json!({"id": 7, "repository_selection": "selected", "account": {"login": "acme"}}))
            }),
        )
        .route(
            "/app/installations/7/access_tokens",
            post(|Json(body): Json<Value>| async move {
                Json(json!({"token": "scoped-token", "permissions": body["permissions"],
                    "expires_at": (Utc::now() + Duration::minutes(55)).to_rfc3339()}))
            }),
        )
        .route(
            "/installation/repositories",
            get(|| async {
                Json(json!({"total_count": 1, "repositories": [{"full_name": "acme/demo"}]}))
            }),
        )
        .route(
            "/repos/acme/demo/issues/3",
            get({
                let open = Arc::clone(&open);
                move || {
                    let open = Arc::clone(&open);
                    async move {
                        Json(json!({"state": if open.load(Ordering::SeqCst) { "open" } else { "closed" }}))
                    }
                }
            }),
        )
        .route(
            "/repos/acme/demo/collaborators/writer/permission",
            get(|| async { Json(json!({"permission": "write"})) }),
        )
        .route(
            "/repos/acme/demo",
            get(|| async { Json(json!({"default_branch": "main"})) }),
        )
        .route(
            "/repos/acme/demo/git/ref/heads/main",
            get(|| async { Json(json!({"object": {"sha": "a".repeat(40)}})) }),
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
    let context = DecisionContext {
        github: AppClient::new(
            reqwest::Client::new(),
            &format!("http://{address}"),
            42,
            &key.stdout,
        )
        .expect("App identity"),
        targets: HashMap::from([(
            8,
            DecisionTarget {
                repository: "acme/demo".to_owned(),
                read_permissions: BTreeMap::from([("issues".to_owned(), "read".to_owned())]),
                fork: "acme/demo".to_owned(),
                fork_permissions: BTreeMap::from([("contents".to_owned(), "read".to_owned())]),
                initialization: None,
                publication: None,
            },
        )]),
        outbox,
        rust_exchange: false,
    };
    let worker = WorkerOptions::default()
        .pg_pool(database.pool().clone())
        .schema("graphile_worker")
        .define_job::<TaskDecisionJob>()
        .add_extension(context)
        .init()
        .await
        .expect("decision worker");
    let running = tokio::spawn(async move { worker.run().await });
    let mut event: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/github.issue_comment.v1/valid/representative.json"
    ))
    .expect("fixture");
    for (index, comment_id) in [(0, 11), (1, 12)] {
        event["id"] = json!(Uuid::now_v7());
        event["data"]["delivery_id"] = json!(format!("decision-{index}"));
        event["data"]["comment"]["id"] = json!(comment_id);
        event["data"]["comment"]["body"] = json!("/concurro start");
        event["data"]["comment"]["created_at"] = json!(format!("2026-11-19T10:0{index}:00Z"));
        event["data"]["comment"]["updated_at"] = json!(format!("2026-11-19T10:0{index}:00Z"));
        assert_eq!(
            process_delivery::<GithubEvent<IssueCommentPayload>, _>(
                &database,
                "task-comments",
                3,
                &serde_json::to_vec(&event).expect("wire"),
                &format!("decision-{index}"),
                &handler,
            )
            .await
            .expect("delivery"),
            ProcessResult::Done
        );
        tokio::time::timeout(std::time::Duration::from_secs(15), async {
            loop {
                let status: Option<String> =
                    sqlx::query_scalar("SELECT status FROM tasks.requests WHERE comment_id=$1")
                        .bind(comment_id)
                        .fetch_optional(database.pool())
                        .await
                        .expect("request status");
                if status.as_deref() == Some(if index == 0 { "refused" } else { "applied" }) {
                    break;
                }
                tokio::time::sleep(std::time::Duration::from_millis(50)).await;
            }
        })
        .await
        .expect("decision completed");
        if index == 0 {
            assert!(
                task(
                    &database,
                    IssueKey {
                        repository_id: 8,
                        number: 3
                    }
                )
                .await
                .expect("task")
                .is_none()
            );
            open.store(true, Ordering::SeqCst);
        }
    }
    assert_eq!(
        task(
            &database,
            IssueKey {
                repository_id: 8,
                number: 3
            }
        )
        .await
        .expect("task")
        .expect("started")
        .stage,
        Stage::Initializing
    );
    let pinned: String = sqlx::query_scalar("SELECT config_sha FROM tasks.start_pins")
        .fetch_one(database.pool())
        .await
        .expect("applied start pin");
    assert_eq!(pinned, "a".repeat(40));
    open.store(false, Ordering::SeqCst);
    let mut closed: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/github.issues.v1/valid/closed.json"
    ))
    .expect("closed issue fixture");
    closed["id"] = json!(Uuid::now_v7());
    closed["data"]["delivery_id"] = json!("decision-closed");
    closed["data"]["repository"]["id"] = json!(8);
    closed["data"]["repository"]["full_name"] = json!("acme/demo");
    closed["data"]["issue"]["number"] = json!(3);
    closed["data"]["issue"]["updated_at"] = json!("2026-11-19T10:03:00Z");
    assert_eq!(
        process_delivery::<GithubIssues, _>(
            &database,
            "task-closures",
            3,
            &serde_json::to_vec(&closed).expect("closed wire"),
            "decision-closed",
            &handler,
        )
        .await
        .expect("closed delivery"),
        ProcessResult::Done
    );
    tokio::time::timeout(std::time::Duration::from_secs(15), async {
        loop {
            if task(
                &database,
                IssueKey {
                    repository_id: 8,
                    number: 3,
                },
            )
            .await
            .expect("task")
            .is_some_and(|value| value.stage == Stage::Stopped)
            {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
    })
    .await
    .expect("closed issue stopped task");
    let mut own = event.clone();
    own["id"] = json!(Uuid::now_v7());
    own["data"]["delivery_id"] = json!("owned-bot-comment");
    own["data"]["comment"]["id"] = json!(25);
    own["data"]["comment"]["body"] = json!("/concurro start");
    own["data"]["comment"]["user"] = json!({
        "id": 10, "login": "concurro[bot]", "type": "Bot",
    });
    own["data"]["sender"] = own["data"]["comment"]["user"].clone();
    process_delivery::<GithubEvent<IssueCommentPayload>, _>(
        &database,
        "task-comments",
        3,
        &serde_json::to_vec(&own).expect("own comment wire"),
        "owned-bot-comment",
        &handler,
    )
    .await
    .expect("own comment delivery");
    let own_requests: i64 =
        sqlx::query_scalar("SELECT count(*) FROM tasks.requests WHERE comment_id=25")
            .fetch_one(database.pool())
            .await
            .expect("owned request count");
    assert_eq!(own_requests, 0);
    running.abort();
    server.abort();
}

/// A control arriving during authorization cannot strand the older decision job.
#[tokio::test]
async fn control_arrival_during_authority_read_drains_both_requests() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 4)
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
    let issue = IssueKey {
        repository_id: 8,
        number: 3,
    };
    let start = Request {
        id: Uuid::now_v7(),
        channel: "comment".to_owned(),
        reply: "issue_comment:11".to_owned(),
        comment_id: Some(11),
        issue,
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
            at: DateTime::<Utc>::from_timestamp(10, 0).unwrap(),
            source_id: 11,
            source: "comment:11".to_owned(),
            index: 0,
        },
    };
    let stop = Request {
        id: Uuid::now_v7(),
        reply: "issue_comment:12".to_owned(),
        comment_id: Some(12),
        kind: RequestKind::Stop,
        order: SourceOrder {
            at: DateTime::<Utc>::from_timestamp(20, 0).unwrap(),
            source_id: 12,
            source: "comment:12".to_owned(),
            index: 0,
        },
        ..start.clone()
    };
    let unverified = Request {
        id: Uuid::now_v7(),
        reply: "issue_comment:10".to_owned(),
        comment_id: Some(10),
        actor: Actor {
            id: 999,
            login: "vanished".to_owned(),
        },
        kind: RequestKind::Later(LaterCommand::Sponsor {
            provider: Some("openai".to_owned()),
        }),
        order: SourceOrder {
            at: DateTime::<Utc>::from_timestamp(5, 0).unwrap(),
            source_id: 10,
            source: "comment:10".to_owned(),
            index: 0,
        },
        ..start.clone()
    };
    let injected = Arc::new(AtomicBool::new(false));
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
            "/repos/acme/demo/issues/3",
            get(|| async { Json(json!({"state":"open"})) }),
        )
        .route(
            "/repos/acme/demo/collaborators/writer/permission",
            get({
                let database = database.clone();
                let injected = injected.clone();
                move || {
                    let database = database.clone();
                    let injected = injected.clone();
                    let stop = stop.clone();
                    async move {
                        if !injected.swap(true, Ordering::SeqCst) {
                            let mut work = database.begin().await.expect("arriving stop");
                            record_request(&mut work, &stop).await.expect("record stop");
                            work.commit().await.expect("stop receipt");
                        }
                        Json(json!({"permission":"write"}))
                    }
                }
            }),
        )
        .route(
            "/repos/acme/demo",
            get(|| async { Json(json!({"default_branch":"main"})) }),
        )
        .route(
            "/repos/acme/demo/git/ref/heads/main",
            get(|| async { Json(json!({"object":{"sha":"a".repeat(40)}})) }),
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
    let context = DecisionContext {
        github: AppClient::new(
            reqwest::Client::new(),
            &format!("http://{address}"),
            42,
            &key.stdout,
        )
        .expect("App client"),
        targets: HashMap::from([(
            8,
            DecisionTarget {
                repository: "acme/demo".to_owned(),
                read_permissions: BTreeMap::from([("issues".to_owned(), "read".to_owned())]),
                fork: "acme/demo".to_owned(),
                fork_permissions: BTreeMap::from([("contents".to_owned(), "read".to_owned())]),
                initialization: None,
                publication: None,
            },
        )]),
        outbox: outbox.clone(),
        rust_exchange: false,
    };
    let mut receipt = database.begin().await.expect("start receipt");
    record_request(&mut receipt, &unverified)
        .await
        .expect("record unverified sponsor");
    record_request(&mut receipt, &start)
        .await
        .expect("record start");
    receipt.commit().await.expect("commit start");
    let worker = WorkerOptions::default()
        .pg_pool(database.pool().clone())
        .schema("graphile_worker")
        .define_job::<TaskDecisionJob>()
        .add_extension(context)
        .init()
        .await
        .expect("decision worker");
    let running = tokio::spawn(async move { worker.run().await });
    tokio::time::timeout(std::time::Duration::from_secs(15), async {
        loop {
            let pending: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM tasks.requests WHERE repository_id=8 AND issue_number=3 AND status='pending'",
            ).fetch_one(database.pool()).await.expect("pending count");
            if pending == 0 && injected.load(Ordering::SeqCst) { break; }
            tokio::time::sleep(std::time::Duration::from_millis(30)).await;
        }
    }).await.expect("both decisions completed");
    let dispositions: Vec<(i64, String)> = sqlx::query_as(
        "SELECT comment_id,status FROM tasks.requests WHERE repository_id=8 AND issue_number=3 ORDER BY comment_id",
    ).fetch_all(database.pool()).await.expect("request dispositions");
    assert_eq!(
        dispositions,
        [
            (10, "refused".to_owned()),
            (11, "superseded".to_owned()),
            (12, "applied".to_owned())
        ]
    );
    let replies: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM tasks.publication_comments WHERE repository_id=8 AND issue_number=3",
    )
    .fetch_one(database.pool())
    .await
    .expect("reply intents");
    assert_eq!(replies, 3);
    running.abort();
    server.abort();
}

#[tokio::test]
async fn comment_to_attempt_and_stop_to_retirement_survive_durable_boundaries() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    migrate_events(&database).await.expect("event schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "request.>".to_owned(),
            "task.>".to_owned(),
            "turn.>".to_owned(),
        ],
    );
    let handler = CommentHandler::new([8], outbox.clone());
    let mut start: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/github.issue_comment.v1/valid/representative.json"
    ))
    .expect("fixture");
    start["data"]["comment"]["body"] = json!("/concurro start");
    let start_bytes = serde_json::to_vec(&start).expect("start wire");
    assert_eq!(
        process_delivery::<GithubEvent<IssueCommentPayload>, _>(
            &database,
            "task-comments",
            3,
            &start_bytes,
            "start",
            &handler,
        )
        .await
        .expect("start delivery"),
        ProcessResult::Done
    );
    assert_eq!(
        process_delivery::<GithubEvent<IssueCommentPayload>, _>(
            &database,
            "task-comments",
            3,
            &start_bytes,
            "start-redelivered",
            &handler,
        )
        .await
        .expect("redelivery"),
        ProcessResult::Skipped
    );
    let issue = IssueKey {
        repository_id: 8,
        number: 3,
    };
    let authority = Authority {
        writer: true,
        issue_open: true,
    };
    let pending = next_pending(&database, issue)
        .await
        .expect("pending")
        .expect("start request");
    let mut apply = database.begin().await.expect("apply start");
    apply_request(&mut apply, &outbox, pending.id, issue, authority)
        .await
        .expect("apply")
        .expect("selected");
    apply.commit().await.expect("start commit");
    let state = task(&database, issue)
        .await
        .expect("task")
        .expect("started");
    assert_eq!(state.stage, Stage::Initializing);
    let mut initialize = database.begin().await.expect("initialize");
    initialize_task(
        &mut initialize,
        &outbox,
        issue,
        state.revision,
        &snapshot(PinnedInputs {
            config_sha: "a".repeat(40),
            base_sha: "b".repeat(40),
            max_model_calls: 2,
            max_fix_rounds: 1,
            max_plan_revisions: 1,
        }),
    )
    .await
    .expect("pin")
    .expect("current task");
    initialize.commit().await.expect("pin commit");

    let turn = Uuid::now_v7();
    let session = Uuid::now_v7();
    let body = format!("{{ \"turn\": \"{turn}\", \"payload\": {{}} }}").into_bytes();
    let attempt = AttemptSpec {
        id: turn,
        task: state.id,
        expected_revision: state.revision + 1,
        staffing_group: "intake".to_owned(),
        fork: "acme/fork".to_owned(),
        config_sha: "a".repeat(40),
        submit_body: body.clone(),
        prompt_body: None,
        deadline: Utc::now() + Duration::minutes(30),
        unbounded_running: false,
    };
    let mut admit = database.begin().await.expect("admit");
    assert_eq!(
        admit_intake(&mut admit, &outbox, issue, &attempt)
            .await
            .expect("admission"),
        Admission::Admitted
    );
    admit.commit().await.expect("admission commit");

    let received = Arc::new(Mutex::new(Vec::new()));
    let polls = Arc::new(AtomicUsize::new(0));
    let app = Router::new()
        .route(
            "/exchange/operator/turns",
            post({
                let received = Arc::clone(&received);
                move |headers: HeaderMap, bytes: Bytes| {
                    let received = Arc::clone(&received);
                    async move {
                        assert_eq!(headers["authorization"], "Bearer operator-secret");
                        *received.lock().expect("capture") = bytes.to_vec();
                        Json(json!({"turn": turn, "status": "queued", "created": 1.0,
                            "dispatch": session}))
                    }
                }
            }),
        )
        .route(
            "/exchange/operator/turns/{turn}",
            get({
                let polls = Arc::clone(&polls);
                move || {
                    let polls = Arc::clone(&polls);
                    async move {
                        if polls.fetch_add(1, Ordering::SeqCst) == 0 {
                            Json(json!({"turn": turn, "status": "queued", "created": 1.0}))
                        } else {
                            Json(json!({"turn": turn, "status": "done", "created": 1.0,
                                "finished": 2.0, "runner": {"session": session, "run_id": "run-1"},
                                "result": {"outcome": "ok", "response": {
                                    "decision": "proceed", "rationale": "Fits\0the charter", "risk": "low",
                                    "questions": [], "concerns": [],
                                    "scope": {"goal_ids": ["G-1"], "fit": "Matches goal", "conflicts": []}
                                }}}))
                        }
                    }
                }
            })
            .delete(move || async move { Json(json!({"sessions": [session]})) }),
        );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let address = listener.local_addr().expect("address");
    let server = tokio::spawn(async move { axum::serve(listener, app).await });
    let exchange = Operator::new(
        reqwest::Client::new(),
        format!("http://{address}"),
        "operator-secret",
    );
    let submit_effect: i64 =
        sqlx::query_scalar("SELECT id FROM tasks.effects WHERE turn_id=$1 AND kind='submit'")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("queued submit");
    let worker = WorkerOptions::default()
        .pg_pool(database.pool().clone())
        .schema("graphile_worker")
        .define_job::<ExchangeEffectJob>()
        .define_job::<ExchangePollJob>()
        .add_extension(ExchangeContext {
            operator: exchange.clone(),
            rust: None,
            outbox: outbox.clone(),
        })
        .init()
        .await
        .expect("effect worker");
    let running = tokio::spawn(async move { worker.run().await });
    tokio::time::timeout(std::time::Duration::from_secs(15), async {
        loop {
            let status: String = sqlx::query_scalar("SELECT status FROM tasks.effects WHERE id=$1")
                .bind(submit_effect)
                .fetch_one(database.pool())
                .await
                .expect("submit status");
            if status == "settled" {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
    })
    .await
    .expect("queued submit completed");
    assert_eq!(*received.lock().expect("received"), body);
    let response: sqlx::types::Json<Value> =
        sqlx::query_scalar("SELECT response FROM tasks.effects WHERE turn_id=$1 AND kind='submit'")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("submit response");
    assert_eq!(response["dispatch"], json!(session));
    let dispatched_session: Uuid =
        sqlx::query_scalar("SELECT session_id FROM tasks.attempt_sessions WHERE turn_id=$1")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("dispatch session");
    assert_eq!(dispatched_session, session);
    tokio::time::timeout(std::time::Duration::from_secs(45), async {
        loop {
            let status: String =
                sqlx::query_scalar("SELECT status FROM tasks.attempts WHERE id=$1")
                    .bind(turn)
                    .fetch_one(database.pool())
                    .await
                    .expect("attempt status");
            if status == "finished" {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
    })
    .await
    .expect("queued poll completed");
    let result: sqlx::types::Json<Value> =
        sqlx::query_scalar("SELECT result FROM tasks.attempts WHERE id=$1")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("stored result");
    assert_eq!(result["outcome"], "ok");
    assert_eq!(result["response"]["decision"], "proceed");
    let poll_generation: i64 =
        sqlx::query_scalar("SELECT poll_generation FROM tasks.attempts WHERE id=$1")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("poll generation");
    assert_eq!(poll_generation, 2);
    run_exchange_poll(&database, &exchange, &outbox, turn, 1)
        .await
        .expect("stale poll is inert");
    assert_eq!(polls.load(Ordering::SeqCst), 2);

    let mut stop = start;
    stop["id"] = json!(Uuid::now_v7());
    stop["data"]["delivery_id"] = json!("stop-delivery");
    stop["data"]["comment"]["id"] = json!(12);
    stop["data"]["comment"]["body"] = json!("/concurro stop");
    stop["data"]["comment"]["created_at"] = json!("2026-11-19T10:02:00Z");
    stop["data"]["comment"]["updated_at"] = json!("2026-11-19T10:02:00Z");
    let stop_bytes = serde_json::to_vec(&stop).expect("stop wire");
    assert_eq!(
        process_delivery::<GithubEvent<IssueCommentPayload>, _>(
            &database,
            "task-comments",
            3,
            &stop_bytes,
            "stop",
            &handler,
        )
        .await
        .expect("stop delivery"),
        ProcessResult::Done
    );
    let pending = next_pending(&database, issue)
        .await
        .expect("pending")
        .expect("stop request");
    assert_eq!(
        task(&database, issue).await.expect("task").unwrap().stage,
        Stage::Plan
    );
    let mut apply = database.begin().await.expect("apply stop");
    apply_request(&mut apply, &outbox, pending.id, issue, authority)
        .await
        .expect("apply")
        .expect("selected");
    apply.commit().await.expect("stop commit");
    assert_eq!(
        task(&database, issue).await.expect("task").unwrap().stage,
        Stage::Stopped
    );
    let retire_effect: i64 =
        sqlx::query_scalar("SELECT id FROM tasks.effects WHERE turn_id=$1 AND kind='retire'")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("queued retirement");
    tokio::time::timeout(std::time::Duration::from_secs(15), async {
        loop {
            let status: String = sqlx::query_scalar("SELECT status FROM tasks.effects WHERE id=$1")
                .bind(retire_effect)
                .fetch_one(database.pool())
                .await
                .expect("retirement status");
            if status == "settled" {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        }
    })
    .await
    .expect("queued retirement completed");
    run_exchange_effect(&database, &exchange, &outbox, retire_effect)
        .await
        .expect("settled replay");
    let sessions: Vec<Uuid> =
        sqlx::query_scalar("SELECT session_id FROM tasks.attempt_sessions WHERE turn_id=$1")
            .bind(turn)
            .fetch_all(database.pool())
            .await
            .expect("retired sessions");
    assert_eq!(sessions, [session]);
    let group_count: i64 = sqlx::query_scalar("SELECT count(*) FROM tasks.staffing_groups")
        .fetch_one(database.pool())
        .await
        .expect("group reservations");
    assert_eq!(group_count, 0);
    let event_types: Vec<String> = sqlx::query_scalar(
        "SELECT type FROM events.outbox WHERE type<>'view.record' ORDER BY sequence",
    )
    .fetch_all(database.pool())
    .await
    .expect("outbox");
    let snapshot_type = format!("task.{}.state", state.id.0);
    let notice_type = format!("task.{}.notice", state.id.0);
    let report_type = format!("task.{}.report", state.id.0);
    let submitted_type = format!("turn.{turn}.submitted");
    let status_type = format!("turn.{turn}.status");
    let finished_type = format!("turn.{turn}.finished");
    assert_eq!(
        event_types,
        [
            snapshot_type.as_str(),
            "request.tasks.applied",
            snapshot_type.as_str(),
            notice_type.as_str(),
            submitted_type.as_str(),
            snapshot_type.as_str(),
            status_type.as_str(),
            status_type.as_str(),
            status_type.as_str(),
            report_type.as_str(),
            snapshot_type.as_str(),
            finished_type.as_str(),
            snapshot_type.as_str(),
            "request.tasks.applied",
        ]
    );
    let wire: Vec<(String, Vec<u8>)> =
        sqlx::query_as("SELECT type,wire_bytes FROM events.outbox ORDER BY sequence")
            .fetch_all(database.pool())
            .await
            .expect("contract wire bytes");
    for (event_type, bytes) in wire {
        if event_type == "view.record" {
            let record = decode::<ReadModelRecord>(&bytes).expect("read model contract");
            assert_eq!(record.data.entity, "report");
        } else if event_type == notice_type {
            decode::<TaskNotice>(&bytes).expect("notice contract");
        } else if event_type == report_type {
            let report = decode::<TaskReport>(&bytes).expect("report contract");
            assert_eq!(report.data.body["rationale"], "Fits\0the charter");
            let raw: Vec<u8> = sqlx::query_scalar(
                "SELECT raw_report_bytes FROM tasks.intake_reports WHERE attempt_id=$1",
            )
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("original report");
            assert_eq!(
                serde_json::from_slice::<Value>(&raw).expect("raw report")["rationale"],
                "Fits\0the charter"
            );
        } else if event_type == submitted_type {
            let submitted = decode::<TurnSubmitted>(&bytes).expect("submitted contract");
            assert_eq!(submitted.data.turn_id, turn);
            assert!(submitted.data.spec.bytes > 0);
        } else if event_type == status_type {
            decode::<TurnStatus>(&bytes).expect("status contract");
        } else if event_type == finished_type {
            let finished = decode::<TurnFinished>(&bytes).expect("finished contract");
            assert_eq!(finished.data.turn_id, turn);
        }
    }
    running.abort();
    server.abort();
}

#[tokio::test]
async fn command_removal_and_repeated_edit_do_not_resurrect_a_start() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    migrate_events(&database).await.expect("event schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "request.>".to_owned(),
            "task.>".to_owned(),
            "turn.>".to_owned(),
        ],
    );
    let handler = CommentHandler::new([8], outbox.clone());
    let mut event: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/github.issue_comment.v1/valid/representative.json"
    ))
    .expect("fixture");
    let issue = IssueKey {
        repository_id: 8,
        number: 3,
    };
    let authority = Authority {
        writer: true,
        issue_open: true,
    };
    for (index, comment_id, body, action) in [
        (0, 11, "/concurro start", "created"),
        (1, 11, "ordinary text", "edited"),
        (2, 11, "/concurro start", "edited"),
        (3, 12, "/concurro stop", "created"),
        (4, 11, "ordinary text", "edited"),
        (5, 11, "/concurro start", "edited"),
    ] {
        event["id"] = json!(Uuid::now_v7());
        event["type"] = json!(format!("github.issue_comment.{action}"));
        event["data"]["action"] = json!(action);
        event["data"]["delivery_id"] = json!(format!("edit-sequence-{index}"));
        event["data"]["comment"]["id"] = json!(comment_id);
        event["data"]["comment"]["body"] = json!(body);
        event["data"]["comment"]["updated_at"] = json!(format!("2026-11-19T10:{index:02}:00Z"));
        event["data"]["comment"]["created_at"] = if comment_id == 11 {
            json!("2026-11-19T10:00:00Z")
        } else {
            json!("2026-11-19T10:03:00Z")
        };
        let wire = serde_json::to_vec(&event).expect("wire");
        assert_eq!(
            process_delivery::<GithubEvent<IssueCommentPayload>, _>(
                &database,
                "task-comments",
                3,
                &wire,
                &format!("delivery-{index}"),
                &handler,
            )
            .await
            .expect("delivery"),
            ProcessResult::Done
        );
        if index == 1 {
            let status: String = sqlx::query_scalar(
                "SELECT status FROM tasks.requests WHERE comment_id=11 ORDER BY source_at LIMIT 1",
            )
            .fetch_one(database.pool())
            .await
            .expect("removed command");
            assert_eq!(status, "superseded");
            assert!(task(&database, issue).await.expect("task").is_none());
        }
        if index == 2 || index == 3 || index == 5 {
            let pending = next_pending(&database, issue)
                .await
                .expect("pending")
                .expect("command request");
            let mut apply = database.begin().await.expect("apply");
            let (_, decision) = apply_request(&mut apply, &outbox, pending.id, issue, authority)
                .await
                .expect("decision")
                .expect("selected");
            apply.commit().await.expect("apply commit");
            if index == 5 {
                assert_eq!(
                    (decision.status, decision.reason),
                    (
                        concurro_tasks_core::RequestStatus::Refused,
                        Some("already_applied")
                    )
                );
            }
        }
    }
    assert_eq!(
        task(&database, issue).await.expect("task").unwrap().stage,
        Stage::Stopped
    );
    let statuses: Vec<String> = sqlx::query_scalar(
        "SELECT status FROM tasks.requests WHERE comment_id=11 ORDER BY source_at",
    )
    .fetch_all(database.pool())
    .await
    .expect("comment history");
    assert_eq!(statuses, ["superseded", "applied", "refused"]);
    let revision_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM tasks.comment_revisions WHERE comment_id=11")
            .fetch_one(database.pool())
            .await
            .expect("revision history");
    assert_eq!(revision_count, 5);
}

#[tokio::test]
async fn deleted_and_ambiguous_comments_never_recreate_pending_commands() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks");
    migrate_events(&database).await.expect("events");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "request.>".to_owned(),
            "task.>".to_owned(),
            "turn.>".to_owned(),
        ],
    );
    let handler = CommentHandler::new([8], outbox);
    let mut event: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/github.issue_comment.v1/valid/representative.json"
    ))
    .expect("fixture");
    let issue = IssueKey {
        repository_id: 8,
        number: 3,
    };
    for (index, comment_id, action, body, at, bot) in [
        (0, 11, "deleted", "/concurro start", "10:02", true),
        (1, 11, "created", "/concurro start", "10:00", false),
        (2, 11, "edited", "/concurro start", "10:01", false),
        (3, 12, "created", "/concurro start", "10:03", false),
        (4, 12, "edited", "ordinary text", "10:03", false),
        (5, 12, "created", "/concurro start", "10:03", false),
        (6, 13, "edited", "ordinary text", "10:04", false),
        (7, 13, "created", "/concurro start", "10:04", false),
    ] {
        event["id"] = json!(Uuid::now_v7());
        event["type"] = json!(format!("github.issue_comment.{action}"));
        event["data"]["action"] = json!(action);
        event["data"]["delivery_id"] = json!(format!("reconciled-{index}"));
        event["data"]["sender"]["type"] = json!(if bot { "Bot" } else { "User" });
        event["data"]["comment"]["id"] = json!(comment_id);
        event["data"]["comment"]["body"] = json!(body);
        event["data"]["comment"]["created_at"] = json!(if comment_id == 12 {
            "2026-11-19T10:03:00Z"
        } else {
            "2026-11-19T10:00:00Z"
        });
        event["data"]["comment"]["updated_at"] = json!(format!("2026-11-19T{at}:00Z"));
        assert_eq!(
            process_delivery::<GithubEvent<IssueCommentPayload>, _>(
                &database,
                "task-comments",
                3,
                &serde_json::to_vec(&event).expect("wire"),
                &format!("reconciled-{index}"),
                &handler,
            )
            .await
            .expect("delivery"),
            ProcessResult::Done
        );
    }
    assert!(
        next_pending(&database, issue)
            .await
            .expect("pending")
            .is_none()
    );
    let deleted: bool = sqlx::query_scalar(
        "SELECT bool_or(deleted) FROM tasks.comment_revisions WHERE comment_id=11",
    )
    .fetch_one(database.pool())
    .await
    .expect("deletion tombstone");
    assert!(deleted);
    let revision_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM tasks.comment_revisions WHERE comment_id=12")
            .fetch_one(database.pool())
            .await
            .expect("logical revisions");
    assert_eq!(revision_count, 2);
    let reason: String =
        sqlx::query_scalar("SELECT reason FROM tasks.requests WHERE comment_id=12 LIMIT 1")
            .fetch_one(database.pool())
            .await
            .expect("ambiguous command");
    assert_eq!(reason, "ambiguous_comment_revision");
    let reverse_status: String =
        sqlx::query_scalar("SELECT status FROM tasks.requests WHERE comment_id=13")
            .fetch_one(database.pool())
            .await
            .expect("reverse-order ambiguity");
    assert_eq!(reverse_status, "superseded");
}

/// Exercise a refused submit through the same claimed effect path as the worker.
async fn rejected_submit(status: u16, reason: &'static str, permanent: bool, crash_fourth: bool) {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    migrate_events(&database).await.expect("event schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "request.>".to_owned(),
            "task.>".to_owned(),
            "turn.>".to_owned(),
        ],
    );
    let issue = IssueKey {
        repository_id: 8,
        number: 3,
    };
    let actor = Actor {
        id: 10,
        login: "writer".to_owned(),
    };
    let request = Request {
        id: Uuid::now_v7(),
        channel: "comment".to_owned(),
        reply: "issue_comment:11".to_owned(),
        comment_id: Some(11),
        issue,
        issue_author: actor.clone(),
        actor,
        kind: RequestKind::Start,
        order: SourceOrder {
            at: DateTime::<Utc>::from_timestamp(10, 0).expect("time"),
            source_id: 11,
            source: "11:start".to_owned(),
            index: 0,
        },
    };
    let authority = Authority {
        writer: true,
        issue_open: true,
    };
    let mut start = database.begin().await.expect("start");
    record_request(&mut start, &request)
        .await
        .expect("record request");
    apply_request(&mut start, &outbox, request.id, issue, authority)
        .await
        .expect("apply request");
    start.commit().await.expect("start commit");
    let state = task(&database, issue).await.expect("task").expect("state");
    let mut pin = database.begin().await.expect("pin");
    initialize_task(
        &mut pin,
        &outbox,
        issue,
        state.revision,
        &snapshot(PinnedInputs {
            config_sha: "a".repeat(40),
            base_sha: "b".repeat(40),
            max_model_calls: 2,
            max_fix_rounds: 1,
            max_plan_revisions: 1,
        }),
    )
    .await
    .expect("initialize");
    pin.commit().await.expect("pin commit");
    let turn = Uuid::now_v7();
    let spec = AttemptSpec {
        id: turn,
        task: state.id,
        expected_revision: state.revision + 1,
        staffing_group: "intake".to_owned(),
        fork: "acme/fork".to_owned(),
        config_sha: "a".repeat(40),
        submit_body: format!("{{\"turn\":\"{turn}\"}}").into_bytes(),
        prompt_body: None,
        deadline: Utc::now() + Duration::minutes(30),
        unbounded_running: false,
    };
    let mut admit = database.begin().await.expect("admit");
    assert_eq!(
        admit_intake(&mut admit, &outbox, issue, &spec)
            .await
            .expect("admit"),
        Admission::Admitted
    );
    admit.commit().await.expect("admit commit");
    let posts = Arc::new(AtomicUsize::new(0));
    let app = Router::new()
        .route(
            "/exchange/operator/turns",
            post({
                let posts = Arc::clone(&posts);
                move || {
                    let posts = Arc::clone(&posts);
                    async move {
                        posts.fetch_add(1, Ordering::SeqCst);
                        (
                            StatusCode::from_u16(status).expect("HTTP status"),
                            Json(json!({"error": reason})),
                        )
                    }
                }
            }),
        )
        .route(
            "/exchange/operator/turns/{turn}",
            axum::routing::delete(|| async { Json(json!({"sessions": []})) }),
        );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let address = listener.local_addr().expect("address");
    let server = tokio::spawn(async move { axum::serve(listener, app).await });
    let exchange = Operator::new(
        reqwest::Client::new(),
        format!("http://{address}"),
        "operator-secret",
    );
    let effect_id: i64 =
        sqlx::query_scalar("SELECT id FROM tasks.effects WHERE turn_id=$1 AND kind='submit'")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("queued submit");
    let outcome = run_exchange_effect(&database, &exchange, &outbox, effect_id).await;
    assert!(outcome.is_ok());
    let effect: (String, Option<sqlx::types::Json<Value>>) =
        sqlx::query_as("SELECT status,response FROM tasks.effects WHERE id=$1")
            .bind(effect_id)
            .fetch_one(database.pool())
            .await
            .expect("effect outcome");
    let attempt_status: String =
        sqlx::query_scalar("SELECT status FROM tasks.attempts WHERE id=$1")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("attempt outcome");
    let retirement_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM tasks.effects WHERE turn_id=$1 AND kind='retire'")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("retirement effect");
    if permanent {
        assert_eq!(effect.0, "settled");
        assert_eq!(effect.1.expect("refusal")["error"], reason);
        assert_eq!(attempt_status, "unknown");
        assert_eq!(retirement_count, 1);
        let retire_effect: i64 =
            sqlx::query_scalar("SELECT id FROM tasks.effects WHERE turn_id=$1 AND kind='retire'")
                .bind(turn)
                .fetch_one(database.pool())
                .await
                .expect("retirement effect");
        run_exchange_effect(&database, &exchange, &outbox, retire_effect)
            .await
            .expect("acknowledged retirement");
        let group: Option<Uuid> =
            sqlx::query_scalar("SELECT turn_id FROM tasks.staffing_groups WHERE turn_id=$1")
                .bind(turn)
                .fetch_optional(database.pool())
                .await
                .expect("group released after retirement");
        assert!(group.is_none());
    } else {
        assert_eq!(effect.0, "recorded");
        assert!(effect.1.is_none());
        assert_eq!(attempt_status, "sending");
        assert_eq!(retirement_count, 0);
        let delay: f64 = sqlx::query_scalar(
            "SELECT EXTRACT(EPOCH FROM run_at-now())::double precision \
             FROM graphile_worker.jobs WHERE key=$1",
        )
        .bind(format!("exchange-effect:{effect_id}:1"))
        .fetch_one(database.pool())
        .await
        .expect("scheduled submit retry");
        assert!((10.0..=16.0).contains(&delay));
        run_exchange_effect(&database, &exchange, &outbox, effect_id)
            .await
            .expect("early replay deferred");
        let sends: i32 =
            sqlx::query_scalar("SELECT submit_attempts FROM tasks.effects WHERE id=$1")
                .bind(effect_id)
                .fetch_one(database.pool())
                .await
                .expect("deferred sends");
        assert_eq!(sends, 1);
        for retry in 2..=if crash_fourth { 3 } else { 4 } {
            sqlx::query(
                "UPDATE tasks.effects SET updated_at=now()-interval '16 seconds' WHERE id=$1",
            )
            .bind(effect_id)
            .execute(database.pool())
            .await
            .expect("retry due");
            run_exchange_effect(&database, &exchange, &outbox, effect_id)
                .await
                .expect("scheduled retry");
            let attempts: i32 =
                sqlx::query_scalar("SELECT submit_attempts FROM tasks.effects WHERE id=$1")
                    .bind(effect_id)
                    .fetch_one(database.pool())
                    .await
                    .expect("attempt count");
            assert_eq!(attempts, retry);
        }
        if crash_fourth {
            sqlx::query(
                "UPDATE tasks.effects SET updated_at=now()-interval '16 seconds' WHERE id=$1",
            )
            .bind(effect_id)
            .execute(database.pool())
            .await
            .expect("fourth claim due");
            let mut interrupted = database.begin().await.expect("interrupted fourth send");
            let fourth = claim_effect(&mut interrupted, effect_id)
                .await
                .expect("claim")
                .expect("fourth submit");
            assert_eq!(fourth.submit_attempts, 4);
            assert!(!fourth.recovered_exhaustion);
            interrupted.commit().await.expect("claim commit");
            sqlx::query(
                "UPDATE tasks.effects SET updated_at=now()-interval '16 seconds' WHERE id=$1",
            )
            .bind(effect_id)
            .execute(database.pool())
            .await
            .expect("orphaned claim due");
            run_exchange_effect(&database, &exchange, &outbox, effect_id)
                .await
                .expect("orphaned fourth send settled");
        }
        let outcome: (String, String) = sqlx::query_as(
            "SELECT e.status,a.status FROM tasks.effects e JOIN tasks.attempts a ON a.id=e.turn_id WHERE e.id=$1",
        )
        .bind(effect_id)
        .fetch_one(database.pool())
        .await
        .expect("exhausted outcome");
        assert_eq!(outcome, ("settled".to_owned(), "unknown".to_owned()));
        let retirement_count: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM tasks.effects WHERE turn_id=$1 AND kind='retire'",
        )
        .bind(turn)
        .fetch_one(database.pool())
        .await
        .expect("retirement count");
        assert_eq!(retirement_count, 1);
        assert_eq!(
            posts.load(Ordering::SeqCst),
            if crash_fourth { 3 } else { 4 }
        );
    }
    server.abort();
}

#[tokio::test]
async fn submit_refusals_distinguish_turn_tombstones_conflicts_and_credentials() {
    rejected_submit(410, "turn_expired", true, false).await;
    rejected_submit(409, "turn_conflict", true, false).await;
    rejected_submit(400, "invalid_submission", true, false).await;
    rejected_submit(422, "invalid_payload", true, false).await;
    rejected_submit(410, "credential_revoked", true, false).await;
    rejected_submit(503, "busy", false, false).await;
    rejected_submit(408, "timeout", false, true).await;
}
