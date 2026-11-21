//! Pinned GitHub source capture before the intake attempt is admitted.

use std::{
    collections::{BTreeMap, HashMap},
    io::Write,
    process::Command,
    process::Stdio,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::Duration as StdDuration,
};

use axum::{
    Json, Router,
    extract::{Path, Query},
    http::StatusCode,
    routing::{get, post},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use chrono::{Duration, Utc};
use concurro_db::Database;
use concurro_events_postgres::{Outbox, migrate as migrate_events};
use concurro_exchange_v1::TurnObservation;
use concurro_github_v1::AppClient;
use concurro_tasks::{
    InitializationContext, InitializationTarget, IntakeContext, IntakeTransport, TaskInitializeJob,
    TaskIntakeJob, read_initialization, read_requirements,
};
use concurro_tasks_core::{Actor, Authority, IssueKey, Request, RequestKind, SourceOrder, Stage};
use concurro_tasks_postgres::{
    apply_request, migrate, pin_start_config, record_observation, record_request, task,
};
use concurro_testkit::PostgresContainer;
use graphile_worker::WorkerOptions;
use serde_json::{Value, json};
use uuid::Uuid;

#[tokio::test]
async fn source_snapshot_reads_fork_config_and_upstream_policy_at_distinct_commits() {
    let fork_sha = "a".repeat(40);
    let base_sha = "b".repeat(40);
    let moved_base = Arc::new(AtomicBool::new(false));
    let base_reads = Arc::new(AtomicUsize::new(0));
    let tree_reads = Arc::new(AtomicUsize::new(0));
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
        .route("/repos/acme/demo", get(|| async {
            Json(json!({"id": 8, "owner": {"id": 9}, "default_branch": "release"}))
        }))
        .route("/users/concurro[bot]", get(|| async {
            Json(json!({"id": 10, "login": "concurro[bot]"}))
        }))
        .route("/repos/acme/demo/git/ref/heads/release", get({
            let fork_sha = fork_sha.clone();
            move || async move { Json(json!({"object": {"sha": fork_sha}})) }
        }))
        .route("/repos/acme/demo/git/ref/heads/main", get({
            let base_sha = base_sha.clone();
            let moved_base = moved_base.clone();
            let base_reads = base_reads.clone();
            move || {
                let base_sha = base_sha.clone();
                let moved_base = moved_base.clone();
                let base_reads = base_reads.clone();
                async move {
                    base_reads.fetch_add(1, Ordering::SeqCst);
                    let sha = if moved_base.load(Ordering::SeqCst) {
                        "c".repeat(40)
                    } else {
                        base_sha
                    };
                    Json(json!({"object": {"sha": sha}}))
                }
            }
        }))
        .route("/repos/acme/demo/contents/{*path}", get({
            let fork_sha = fork_sha.clone();
            let base_sha = base_sha.clone();
            move |Path(path): Path<String>, Query(query): Query<BTreeMap<String, String>>| {
                let fork_sha = fork_sha.clone();
                let base_sha = base_sha.clone();
                async move {
                    let content = match path.as_str() {
                        ".concurro/config.yaml" => Some((fork_sha.as_str(), "repository: acme/demo\nbase: main\nbot_login: concurro[bot]\nmax_model_calls: 3\nmax_fix_rounds: 2\nmax_plan_revisions: 1\nmax_context_bytes: 350000\nturn_timeout_seconds: null\nmax_retained_bytes: null\npolicy_documents: [README.md]\nqc: codex\nmodels: {codex: gpt-test}\nmcp: {servers: {tool: {transport: stdio, command: tool, args: [\"--root={workspace}\"], env: {RA_LOG: error}}}}\npaths: {protected: [], protected_extensions: [], protected_names: [], protected_name_prefixes: [], protected_prefixes: [], protected_files: [], generated: []}\n")),
                        ".concurro/charter.json" => Some((fork_sha.as_str(), "{\"version\":1,\"id\":\"test\",\"goals\":{\"g\":\"v\"},\"non_goals\":[\"n\"],\"constraints\":[\"c\"],\"acceptance\":[\"a\"]}")),
                        ".concurro/instructions/shared.md" => Some((fork_sha.as_str(), "Target guidance")),
                        "README.md" => Some((base_sha.as_str(), "Upstream policy")),
                        ".github/instructions/rules.md" => Some((base_sha.as_str(), "Scoped rules")),
                        _ => None,
                    };
                    match content {
                        Some((sha, text)) if query.get("ref").map(String::as_str) == Some(sha) =>
                            (StatusCode::OK, Json(json!({"type": "file", "encoding": "base64",
                                "path": path, "size": text.len(), "content": STANDARD.encode(text)}))),
                        _ => (StatusCode::NOT_FOUND, Json(json!({"message": "not found"}))),
                    }
                }
            }
        }))
        .route("/repos/acme/demo/git/trees/{sha}", get({
            let base_sha = base_sha.clone();
            let tree_reads = tree_reads.clone();
            move |Path(sha): Path<String>| {
                let base_sha = base_sha.clone();
                let tree_reads = tree_reads.clone();
                async move {
                    tree_reads.fetch_add(1, Ordering::SeqCst);
                    assert_eq!(sha, base_sha);
                    Json(json!({"truncated": false, "tree": [
                        {"path": "README.md", "type": "blob"},
                        {"path": "src/lib.rs", "type": "blob"},
                        {"path": ".github/instructions/rules.md", "type": "blob"},
                        {"path": "src", "type": "tree"}
                    ]}))
                }
            }
        }))
        .route("/repos/acme/demo/issues/3", get(|| async {
            Json(json!({"state": "open", "title": "Task title", "body": "Issue body",
                "user": {"id": 1, "login": "author"}}))
        }))
        .route("/repos/acme/demo/issues/3/comments", get(|| async {
            Json(json!([
                {"id": 1, "body": "Author detail", "user": {"id": 1, "login": "author"}},
                {"id": 2, "body": "Outsider", "user": {"id": 3, "login": "outsider"}},
                {"id": 3, "body": "Bot notice", "user": {"id": 4, "login": "concurro[bot]"}},
                {"id": 4, "body": "Writer detail", "user": {"id": 2, "login": "writer"}},
                {"id": 5, "body": "/concurro start", "user": {"id": 2, "login": "writer"}},
                {"id": 6, "body": "Too late", "user": {"id": 1, "login": "author"}}
            ]))
        }))
        .route("/repos/acme/demo/collaborators/{login}/permission", get(|Path(login): Path<String>| async move {
            if login == "writer" {
                (StatusCode::OK, Json(json!({"permission": "write"})))
            } else {
                (StatusCode::NOT_FOUND, Json(json!({"message": "not found"})))
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
    let github = AppClient::new(
        reqwest::Client::new(),
        &format!("http://{address}"),
        42,
        &key.stdout,
    )
    .expect("App client");
    let permissions = BTreeMap::from([
        ("contents".to_owned(), "read".to_owned()),
        ("issues".to_owned(), "read".to_owned()),
    ]);
    let target = InitializationTarget {
        issue_repository: "acme/demo".to_owned(),
        fork: "acme/demo".to_owned(),
        upstream: "acme/demo".to_owned(),
        issue_permissions: permissions.clone(),
        fork_permissions: permissions.clone(),
        upstream_permissions: permissions,
        bot_login: "concurro[bot]".to_owned(),
        publisher_logins: vec![],
        engine_sha: "engine".to_owned(),
    };
    let request = Request {
        id: Uuid::now_v7(),
        channel: "comment".to_owned(),
        reply: "issue_comment:5".to_owned(),
        comment_id: Some(5),
        issue: IssueKey {
            repository_id: 8,
            number: 3,
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
    let snapshot = read_initialization(&github, &target, &request, &fork_sha)
        .await
        .expect("source snapshot");
    assert_eq!(snapshot.pins.config_sha, fork_sha);
    assert_eq!(snapshot.pins.base_sha, base_sha);
    assert_eq!(snapshot.pins.max_model_calls, 3);
    assert_eq!(snapshot.requirements.discussion.len(), 2);
    assert_eq!(snapshot.requirements.discussion[0].body, "Author detail");
    assert_eq!(snapshot.requirements.discussion[1].body, "Writer detail");
    assert_eq!(snapshot.policy_documents["README.md"], "Upstream policy");
    assert_eq!(
        snapshot.policy_documents[".github/instructions/rules.md"],
        "Scoped rules"
    );
    assert_eq!(
        snapshot.instruction_texts[".concurro/instructions/shared.md"],
        "Target guidance"
    );
    assert_eq!(snapshot.base_files.len(), 3);
    let reads_before_answer = base_reads.load(Ordering::SeqCst);
    let trees_before_answer = tree_reads.load(Ordering::SeqCst);
    moved_base.store(true, Ordering::SeqCst);
    let (answered, _) = read_requirements(&github, &target, &request)
        .await
        .expect("read clarified requirements after base moved");
    assert_eq!(answered, snapshot.requirements);
    assert_eq!(base_reads.load(Ordering::SeqCst), reads_before_answer);
    assert_eq!(tree_reads.load(Ordering::SeqCst), trees_before_answer);
    moved_base.store(false, Ordering::SeqCst);

    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    migrate_events(&database).await.expect("event schema");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "request.>".to_owned(),
            "task.>".to_owned(),
            "turn.>".to_owned(),
        ],
    );
    let observed_outbox = outbox.clone();
    let mut start = database.begin().await.expect("start transaction");
    record_request(&mut start, &request)
        .await
        .expect("record start");
    let (_, applied) = apply_request(
        &mut start,
        &outbox,
        request.id,
        request.issue,
        Authority {
            writer: true,
            issue_open: true,
        },
    )
    .await
    .expect("apply start")
    .expect("start selected");
    pin_start_config(
        &mut start,
        request.id,
        applied.task.as_ref().expect("task"),
        &fork_sha,
    )
    .await
    .expect("pin start configuration");
    start.commit().await.expect("start commit");
    let worker = WorkerOptions::default()
        .pg_pool(database.pool().clone())
        .schema("graphile_worker")
        .define_job::<TaskInitializeJob>()
        .define_job::<TaskIntakeJob>()
        .add_extension(InitializationContext {
            github: github.clone(),
            targets: HashMap::from([(8, target.clone())]),
            outbox: outbox.clone(),
        })
        .add_extension(IntakeContext {
            initialization: InitializationContext {
                github,
                targets: HashMap::from([(8, target)]),
                outbox,
            },
            transport: IntakeTransport {
                gateway: json!({"url": "https://gateway.example", "headers": {"x-test": "value"}}),
                workflow_ref: "acme/demo/.github/workflows/agent.yml@refs/heads/main".to_owned(),
                models: vec!["gpt-*".to_owned()],
                budget: 1000,
                lease: 21600,
            },
        })
        .init()
        .await
        .expect("initialization worker");
    let running = tokio::spawn(async move { worker.run().await });
    tokio::time::timeout(StdDuration::from_secs(15), async {
        loop {
            if task(&database, request.issue)
                .await
                .expect("task")
                .is_some_and(|state| {
                    state.stage == Stage::Intake && state.current_attempt.is_some()
                })
            {
                break;
            }
            tokio::time::sleep(StdDuration::from_millis(50)).await;
        }
    })
    .await
    .expect("initialization completed");
    let stored: Value = sqlx::query_scalar("SELECT snapshot FROM tasks.initialization_inputs")
        .fetch_one(database.pool())
        .await
        .expect("stored source snapshot");
    assert_eq!(stored["pins"]["base_sha"], base_sha);
    assert_eq!(
        stored["requirements"]["discussion"]
            .as_array()
            .expect("discussion")
            .len(),
        2
    );
    let body: Vec<u8> = sqlx::query_scalar("SELECT submit_body FROM tasks.attempts")
        .fetch_one(database.pool())
        .await
        .expect("durable intake submission");
    let submission: Value = serde_json::from_slice(&body).expect("submission JSON");
    let mut oracle = Command::new("python3")
        .arg(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/intake_oracle.py"
        ))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("start Python payload oracle");
    oracle
        .stdin
        .take()
        .expect("oracle stdin")
        .write_all(
            &serde_json::to_vec(&json!({"snapshot": stored, "submission": submission}))
                .expect("oracle input"),
        )
        .expect("send oracle input");
    let compared = oracle
        .wait_with_output()
        .expect("Python payload comparison");
    assert!(
        compared.status.success(),
        "{}",
        String::from_utf8_lossy(&compared.stderr)
    );
    assert_eq!(submission["expected"]["repository_id"], "8");
    assert_eq!(submission["expected"]["actor_id"], "10");
    assert_eq!(submission["binding"]["config_sha"], fork_sha);
    assert_eq!(submission["binding"]["base"], base_sha);
    assert_eq!(submission["payload"]["spec"]["refs"]["main"], base_sha);
    assert!(submission["payload"]["timeout"].is_null());
    assert_eq!(
        submission["payload"]["request"]["context"]["editable_files"],
        json!(["README.md", "src/lib.rs", ".github/instructions/rules.md"])
    );
    let turn: Uuid = serde_json::from_value(submission["turn"].clone()).expect("turn ID");
    let running_turn: TurnObservation = serde_json::from_value(json!({
        "turn": turn, "status": "running", "created": 1.0, "started": 2.0,
    }))
    .expect("running turn");
    let mut observation = database.begin().await.expect("running observation");
    record_observation(&mut observation, &observed_outbox, &running_turn, None)
        .await
        .expect("record running");
    observation.commit().await.expect("running commit");
    let deadline: Option<chrono::DateTime<Utc>> =
        sqlx::query_scalar("SELECT deadline FROM tasks.attempts WHERE id=$1")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("deadline");
    assert!(deadline.is_none());
    running.abort();
    server.abort();
}
