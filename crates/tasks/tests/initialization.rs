//! Pinned GitHub source capture before the intake attempt is admitted.

use std::{
    collections::{BTreeMap, HashMap},
    io::Write,
    process::Command,
    process::Stdio,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::Duration as StdDuration,
};

use axum::{
    Json, Router,
    extract::{Path, Query},
    http::{HeaderMap, StatusCode},
    routing::{get, post},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use chrono::{DateTime, Duration, Utc};
use concurro_db::Database;
use concurro_events_postgres::{Outbox, migrate as migrate_events};
use concurro_exchange_v1::{Operator, TurnObservation};
use concurro_github_v1::AppClient;
use concurro_tasks::{
    ExchangeContext, ExchangeEffectJob, InitializationContext, InitializationTarget, IntakeContext,
    IntakeTransport, SponsorshipTransitionJob, TaskInitializeJob, TaskIntakeJob,
    read_initialization, read_requirements,
};
use concurro_tasks_core::{
    Actor, Authority, CommentRevision, IssueKey, LaterCommand, Provider, Request, RequestKind,
    SourceOrder, Stage,
};
use concurro_tasks_postgres::{
    apply_request, funding_slot, migrate, pin_start_config, record_comment_revision,
    record_observation, record_request, task,
};
use concurro_testkit::PostgresContainer;
use concurro_vault_v1::MetadataClient;
use graphile_worker::WorkerOptions;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::types::Json as SqlJson;
use uuid::Uuid;

#[tokio::test]
async fn source_snapshot_reads_fork_config_and_upstream_policy_at_distinct_commits() {
    verify_initialization(false, false, false, false).await;
}

#[tokio::test]
async fn withdrawal_before_initial_slots_prevents_personal_charge() {
    verify_initialization(true, false, false, false).await;
}

#[tokio::test]
async fn removing_sponsor_comment_after_enrollment_cuts_off_its_grant() {
    verify_initialization(false, true, false, false).await;
}

#[tokio::test]
async fn trusted_provider_failure_uses_next_sponsor_credential() {
    verify_initialization(false, false, true, false).await;
}

#[tokio::test]
async fn unknown_provider_outcome_does_not_start_credential_fallback() {
    verify_initialization(false, false, false, true).await;
}

/// Exercise one contributed enrollment with withdrawal before or after its first turn.
async fn verify_initialization(
    early_withdraw: bool,
    remove_source: bool,
    fallback: bool,
    unknown_outcome: bool,
) {
    let fork_sha = "a".repeat(40);
    let base_sha = "b".repeat(40);
    let moved_base = Arc::new(AtomicBool::new(false));
    let base_reads = Arc::new(AtomicUsize::new(0));
    let tree_reads = Arc::new(AtomicUsize::new(0));
    let vault_reads = Arc::new(AtomicUsize::new(0));
    let sponsorship_writes = Arc::new(AtomicUsize::new(0));
    let openai_ack = Arc::new(AtomicBool::new(false));
    let submitted = Arc::new(Mutex::new(Vec::<u8>::new()));
    let app = Router::new()
        .route("/exchange/operator/turns", post({
            let submitted = submitted.clone();
            let openai_ack = openai_ack.clone();
            move |headers: HeaderMap, body: axum::body::Bytes| {
                let submitted = submitted.clone();
                let openai_ack = openai_ack.clone();
                async move {
                    assert_eq!(headers["authorization"], "Bearer operator-only");
                    assert!(openai_ack.load(Ordering::SeqCst));
                    let value: Value = serde_json::from_slice(&body).expect("submitted turn JSON");
                    *submitted.lock().expect("submitted body") = body.to_vec();
                    Json(json!({"turn":value["turn"],"status":"queued","created":1.0,
                        "started":null,"finished":null,"dispatch":null,"runner":null,
                        "result":null,"provider_failure":null,"refusal":null}))
                }
            }
        }))
        .route("/exchange/operator/sponsorship", post({
            let sponsorship_writes = sponsorship_writes.clone();
            let openai_ack = openai_ack.clone();
            move |headers: HeaderMap, Json(body): Json<Value>| {
                let sponsorship_writes = sponsorship_writes.clone();
                let openai_ack = openai_ack.clone();
                async move {
                    assert_eq!(headers["authorization"], "Bearer operator-only");
                    sponsorship_writes.fetch_add(1, Ordering::SeqCst);
                    if body["provider"] == "openai" {
                        openai_ack.store(true, Ordering::SeqCst);
                    }
                    Json(json!({"transitions":[{
                        "transition":body["transition"], "epoch":body["slot"]["epoch"]
                    }]}))
                }
            }
        }))
        .route("/metadata/2", get({
            let vault_reads = vault_reads.clone();
            move |headers: HeaderMap, Query(query): Query<BTreeMap<String, String>>| {
                let vault_reads = vault_reads.clone();
                async move {
                    assert_eq!(headers["authorization"], "Bearer metadata-only");
                    assert!(query.is_empty() || query.contains_key("at"));
                    vault_reads.fetch_add(1, Ordering::SeqCst);
                    Json(json!({"history_complete":true,"credentials":[{
                        "record_id":"e".repeat(64),"user_id":"2","route":"openai-api-key",
                        "version":1,"status":"valid","models":["gpt-test"],
                        "created":"2026-11-19T09:00:00Z","updated":"2026-11-19T09:00:00Z",
                        "kind":"api_key","expiry_known":false,"expires_at":null,
                        "entry_id":"entry-openai","provider":"openai","name":"primary",
                        "enabled":true,"roles":["triage"],
                        "profiles":[{"target":"acme/demo","profile":"codex"}],
                        "position":0,"removed":false
                    },{
                        "record_id":"f".repeat(64),"user_id":"2","route":"openai-api-key",
                        "version":2,"status":"valid","models":["gpt-test"],
                        "created":"2026-11-19T09:01:00Z","updated":"2026-11-19T09:01:00Z",
                        "kind":"api_key","expiry_known":false,"expires_at":null,
                        "entry_id":"entry-secondary","provider":"openai","name":"secondary",
                        "enabled":true,"roles":["triage"],
                        "profiles":[{"target":"acme/demo","profile":"codex"}],
                        "position":1,"removed":false
                    }],"lists":{"openai":{"revision":1,"entries":["entry-openai","entry-secondary"]}}}))
                }
            }
        }))
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
        .route("/users/writer", get(|| async {
            Json(json!({"id": 2, "login": "writer"}))
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
                        ".concurro/config.yaml" => Some((fork_sha.as_str(), "repository: acme/demo\nbase: main\nbot_login: concurro[bot]\nfunding_mode: contributed\nmax_model_calls: 3\nmax_fix_rounds: 2\nmax_plan_revisions: 1\nmax_context_bytes: 350000\nturn_timeout_seconds: null\nmax_retained_bytes: null\npolicy_documents: [README.md]\nqc: codex\nplanner: codex\nimplementer: codex\nreviewers: [codex]\nmodels: {codex: gpt-test}\nmcp: {servers: {tool: {transport: stdio, command: tool, args: [\"--root={workspace}\"], env: {RA_LOG: error}}}}\npaths: {protected: [], protected_extensions: [], protected_names: [], protected_name_prefixes: [], protected_files: [], generated: []}\n")),
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
                {"id": 3, "body": "/concurro sponsor openai",
                    "created_at":"2026-11-19T10:00:00Z", "updated_at":"2026-11-19T10:00:00Z",
                    "user": {"id": 2, "login": "writer"}},
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
    let vault = MetadataClient::new(
        reqwest::Client::new(),
        format!("http://{address}"),
        "metadata-only",
    );
    let exchange = Operator::new(
        reqwest::Client::new(),
        format!("http://{address}"),
        "operator-only",
    );
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
        engine_sha: "e".repeat(40),
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
    let sponsor_at = DateTime::parse_from_rfc3339("2026-11-19T10:00:00Z")
        .expect("sponsor time")
        .to_utc();
    let sponsor_order = SourceOrder {
        at: sponsor_at,
        source_id: 3,
        source: format!(
            "3:{}:{}",
            sponsor_at.to_rfc3339(),
            hex::encode(Sha256::digest(b"/concurro sponsor openai"))
        ),
        index: 0,
    };
    let sponsor = Request {
        id: Uuid::now_v7(),
        channel: "comment".to_owned(),
        reply: "issue_comment:3".to_owned(),
        comment_id: Some(3),
        issue: request.issue,
        issue_author: request.issue_author.clone(),
        actor: request.actor.clone(),
        kind: RequestKind::Later(LaterCommand::Sponsor {
            provider: Some("openai".to_owned()),
        }),
        order: sponsor_order.clone(),
    };
    let mut consent = database.begin().await.expect("consent receipt");
    record_comment_revision(
        &mut consent,
        &outbox,
        &CommentRevision {
            issue: request.issue,
            comment_id: 3,
            order: sponsor_order,
            deleted: false,
            request: Some(sponsor.clone()),
        },
    )
    .await
    .expect("sponsor receipt");
    consent.commit().await.expect("consent receipt commit");
    let mut consent = database.begin().await.expect("consent decision");
    apply_request(
        &mut consent,
        &outbox,
        sponsor.id,
        sponsor.issue,
        Authority {
            writer: true,
            issue_open: true,
        },
    )
    .await
    .expect("sponsor decision")
    .expect("sponsor selected");
    consent.commit().await.expect("consent decision commit");
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
    let withdraw = Request {
        id: Uuid::now_v7(),
        channel: "comment".to_owned(),
        reply: "issue_comment:7".to_owned(),
        comment_id: Some(7),
        issue: request.issue,
        issue_author: request.issue_author.clone(),
        actor: request.actor.clone(),
        kind: RequestKind::Later(LaterCommand::Withdraw {
            provider: "openai".to_owned(),
        }),
        order: SourceOrder {
            at: Utc::now(),
            source_id: 7,
            source: "comment:7".to_owned(),
            index: 0,
        },
    };
    if early_withdraw {
        apply_withdraw(&database, &outbox, &withdraw).await;
    }
    let worker = WorkerOptions::default()
        .pg_pool(database.pool().clone())
        .schema("graphile_worker")
        .define_job::<TaskInitializeJob>()
        .define_job::<TaskIntakeJob>()
        .define_job::<SponsorshipTransitionJob>()
        .define_job::<ExchangeEffectJob>()
        .add_extension(ExchangeContext {
            operator: exchange,
            rust: None,
            outbox: outbox.clone(),
        })
        .add_extension(InitializationContext {
            github: github.clone(),
            targets: HashMap::from([(8, target.clone())]),
            outbox: outbox.clone(),
            vault: Some(vault.clone()),
        })
        .add_extension(IntakeContext {
            initialization: InitializationContext {
                github,
                targets: HashMap::from([(8, target)]),
                outbox,
                vault: Some(vault),
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
    if early_withdraw {
        tokio::time::timeout(StdDuration::from_secs(15), async {
            loop {
                let acknowledged: i64 = sqlx::query_scalar(
                    "SELECT count(*) FROM tasks.sponsorship_transitions WHERE status='acknowledged'",
                )
                .fetch_one(database.pool())
                .await
                .expect("transition status");
                if acknowledged == 3 {
                    break;
                }
                tokio::time::sleep(StdDuration::from_millis(50)).await;
            }
        })
        .await
        .expect("early withdrawal acknowledged");
        let attempted: i64 = sqlx::query_scalar("SELECT count(*) FROM tasks.attempts")
            .fetch_one(database.pool())
            .await
            .expect("attempt count");
        assert_eq!(attempted, 0);
        let state = tokio::time::timeout(StdDuration::from_secs(15), async {
            loop {
                let state = task(&database, request.issue)
                    .await
                    .expect("task")
                    .expect("initialized task");
                if state.stage == Stage::Blocked {
                    break state;
                }
                tokio::time::sleep(StdDuration::from_millis(50)).await;
            }
        })
        .await
        .expect("withdrawn task visibly blocked");
        assert_eq!(state.used_model_calls, 0);
        assert_eq!(state.retry_stage, Some(Stage::Intake));
        assert_eq!(
            state.stage_reason.as_deref(),
            Some("provider has no active sponsor")
        );
        let funding = funding_slot(&database, state.id.0, Provider::Openai)
            .await
            .expect("funding slot")
            .expect("boundary");
        assert!(funding.acknowledged);
        assert_eq!(funding.slot.expect("slot")["status"], "withdrawn");
        assert_eq!(sponsorship_writes.load(Ordering::SeqCst), 3);
        running.abort();
        server.abort();
        return;
    }
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
    tokio::time::timeout(StdDuration::from_secs(15), async {
        loop {
            let acknowledged: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM tasks.sponsorship_transitions WHERE status='acknowledged'",
            )
            .fetch_one(database.pool())
            .await
            .expect("transition status");
            if acknowledged == 2 {
                break;
            }
            tokio::time::sleep(StdDuration::from_millis(50)).await;
        }
    })
    .await
    .expect("initial sponsorship transitions acknowledged");
    assert_eq!(sponsorship_writes.load(Ordering::SeqCst), 2);
    assert!(vault_reads.load(Ordering::SeqCst) > 0);
    let (initial, availability, funding): (Value, Value, String) = sqlx::query_as(
        "SELECT initial,availability,default_funding FROM tasks.sponsorship_initializations",
    )
    .fetch_one(database.pool())
    .await
    .expect("frozen boundary");
    assert_eq!(funding, "contributed");
    assert_eq!(availability["openai"], true);
    assert_eq!(
        initial["projection"]["providers"]["openai"]["slot"]["Active"]["sponsor"]["id"],
        2
    );
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
    let SqlJson(grant): SqlJson<Value> =
        sqlx::query_scalar("SELECT row_to_json(g) FROM tasks.funding_grants g WHERE attempt_id=$1")
            .bind(
                submission["turn"]
                    .as_str()
                    .expect("turn ID")
                    .parse::<Uuid>()
                    .expect("UUID"),
            )
            .fetch_one(database.pool())
            .await
            .expect("durable funding grant");
    assert_eq!(grant["purpose"], "task_contributed");
    assert_eq!(grant["status"], "issued");
    assert_eq!(grant["reservation_status"], "charged");
    assert_eq!(grant["payer_id"], 2);
    assert_eq!(grant["provider"], "openai");
    assert_eq!(grant["slot_grant_id"], submission["binding"]["grant_id"]);
    assert_eq!(grant["slot_epoch"], submission["binding"]["epoch"]);
    assert_eq!(grant["credential_record_id"], "e".repeat(64));
    assert_eq!(grant["credential_version"], 1);
    assert_eq!(grant["models"], json!(["gpt-test"]));
    assert_eq!(grant["limits"]["calls"], 1);
    tokio::time::timeout(StdDuration::from_secs(15), async {
        loop {
            if !submitted.lock().expect("submitted body").is_empty() {
                break;
            }
            tokio::time::sleep(StdDuration::from_millis(50)).await;
        }
    })
    .await
    .expect("Exchange submission");
    assert_eq!(*submitted.lock().expect("submitted body"), body);
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
    if fallback {
        let failure: TurnObservation = serde_json::from_value(json!({
            "turn":turn,"status":"running","created":1.0,"started":2.0,
            "provider_failure":{
                "turn":turn,"model":"gpt-test","binding":submission["binding"],
                "category":"auth","request":"provider-request-1",
                "retry_after":null
            }
        }))
        .expect("trusted provider failure");
        let mut work = database.begin().await.expect("provider failure");
        record_observation(&mut work, &observed_outbox, &failure, None)
            .await
            .expect("record provider failure");
        work.commit().await.expect("failure commit");
        tokio::time::timeout(StdDuration::from_secs(15), async {
            loop {
                let attempts: i64 =
                    sqlx::query_scalar("SELECT count(*) FROM tasks.attempts WHERE task_id=$1")
                        .bind(applied.task.as_ref().expect("task").id.0)
                        .fetch_one(database.pool())
                        .await
                        .expect("attempt count");
                if attempts == 2 {
                    break;
                }
                tokio::time::sleep(StdDuration::from_millis(50)).await;
            }
        })
        .await
        .expect("replacement credential attempt");
        let second: Vec<u8> = sqlx::query_scalar(
            "SELECT submit_body FROM tasks.attempts WHERE task_id=$1 AND id<>$2",
        )
        .bind(applied.task.as_ref().expect("task").id.0)
        .bind(turn)
        .fetch_one(database.pool())
        .await
        .expect("replacement body");
        let replacement: Value = serde_json::from_slice(&second).expect("replacement JSON");
        assert_eq!(replacement["binding"]["credential"], "f".repeat(64));
        assert_eq!(replacement["binding"]["credential_version"], 2);
        assert_eq!(replacement["payload"]["request"]["admission"]["call"], 2);
        let category: String =
            sqlx::query_scalar("SELECT category FROM tasks.funding_exclusions WHERE attempt_id=$1")
                .bind(turn)
                .fetch_one(database.pool())
                .await
                .expect("exact failed version");
        assert_eq!(category, "auth");
        let used = task(&database, request.issue)
            .await
            .expect("task")
            .expect("enrolled")
            .used_model_calls;
        assert_eq!(used, 2);
        running.abort();
        server.abort();
        return;
    }
    if unknown_outcome {
        let unknown: TurnObservation = serde_json::from_value(json!({
            "turn":turn,"status":"lost","created":1.0,"started":2.0,
            "provider_failure":{
                "turn":turn,"model":"gpt-test","binding":submission["binding"],
                "category":"auth","request":"provider-request-1",
                "retry_after":null
            }
        }))
        .expect("unknown outcome");
        let mut work = database.begin().await.expect("unknown observation");
        record_observation(&mut work, &observed_outbox, &unknown, None)
            .await
            .expect("record unknown");
        work.commit().await.expect("unknown commit");
        tokio::time::sleep(StdDuration::from_millis(200)).await;
        let attempts: i64 =
            sqlx::query_scalar("SELECT count(*) FROM tasks.attempts WHERE task_id=$1")
                .bind(applied.task.as_ref().expect("task").id.0)
                .fetch_one(database.pool())
                .await
                .expect("attempt count");
        assert_eq!(
            attempts, 1,
            "unknown outcome must not start a new paid turn"
        );
        let (status, excluded): (String, bool) = sqlx::query_as(
            "SELECT status,EXISTS(SELECT 1 FROM tasks.funding_exclusions WHERE attempt_id=$1) \
             FROM tasks.attempts WHERE id=$1",
        )
        .bind(turn)
        .fetch_one(database.pool())
        .await
        .expect("unknown attempt state");
        assert_eq!((status.as_str(), excluded), ("unknown", false));
        running.abort();
        server.abort();
        return;
    }
    if remove_source {
        let mut removal = database.begin().await.expect("source removal");
        record_comment_revision(
            &mut removal,
            &observed_outbox,
            &CommentRevision {
                issue: request.issue,
                comment_id: 3,
                order: SourceOrder {
                    at: Utc::now(),
                    source_id: 3,
                    source: "comment:3:deleted".to_owned(),
                    index: 0,
                },
                deleted: true,
                request: None,
            },
        )
        .await
        .expect("remove sponsor source");
        removal.commit().await.expect("source removal commit");
    } else {
        apply_withdraw(&database, &observed_outbox, &withdraw).await;
    }
    tokio::time::timeout(StdDuration::from_secs(15), async {
        loop {
            let acknowledged: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM tasks.sponsorship_transitions WHERE status='acknowledged'",
            )
            .fetch_one(database.pool())
            .await
            .expect("transition status");
            if acknowledged == 3 {
                break;
            }
            tokio::time::sleep(StdDuration::from_millis(50)).await;
        }
    })
    .await
    .expect("withdrawal transition acknowledged");
    let charged: i64 =
        sqlx::query_scalar("SELECT (state->>'used_model_calls')::bigint FROM tasks.task_state")
            .fetch_one(database.pool())
            .await
            .expect("charged calls");
    assert_eq!(charged, 1);
    let grant_status: (String, String) = sqlx::query_as(
        "SELECT status,reservation_status FROM tasks.funding_grants WHERE attempt_id=$1",
    )
    .bind(turn)
    .fetch_one(database.pool())
    .await
    .expect("revoked attempt grant");
    assert_eq!(grant_status, ("revoked".to_owned(), "charged".to_owned()));
    let funding = funding_slot(
        &database,
        task(&database, request.issue)
            .await
            .expect("task")
            .expect("enrolled")
            .id
            .0,
        Provider::Openai,
    )
    .await
    .expect("withdrawn funding slot")
    .expect("frozen funding boundary");
    assert!(funding.consent_current);
    assert!(funding.acknowledged);
    assert_eq!(funding.slot.expect("slot")["status"], "withdrawn");
    let stranger = Request {
        id: Uuid::now_v7(),
        reply: "issue_comment:8".to_owned(),
        comment_id: Some(8),
        actor: Actor {
            id: 99,
            login: "stranger".to_owned(),
        },
        kind: RequestKind::Later(LaterCommand::Sponsor {
            provider: Some("openai".to_owned()),
        }),
        order: SourceOrder {
            at: Utc::now() + Duration::seconds(1),
            source_id: 8,
            source: "comment:8".to_owned(),
            index: 0,
        },
        ..withdraw.clone()
    };
    let mut receipt = database.begin().await.expect("unverified receipt");
    record_comment_revision(
        &mut receipt,
        &observed_outbox,
        &CommentRevision {
            issue: request.issue,
            comment_id: 8,
            order: stranger.order.clone(),
            deleted: false,
            request: Some(stranger),
        },
    )
    .await
    .expect("record unverified receipt");
    receipt.commit().await.expect("receipt commit");
    let enrolled = task(&database, request.issue)
        .await
        .expect("task")
        .expect("enrolled");
    assert!(
        funding_slot(&database, enrolled.id.0, Provider::Anthropic)
            .await
            .expect("other provider")
            .expect("frozen boundary")
            .consent_current
    );
    assert!(
        funding_slot(&database, enrolled.id.0, Provider::Openai)
            .await
            .expect("original provider")
            .expect("frozen boundary")
            .consent_current
    );
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
    let transitions: Vec<Vec<u8>> =
        sqlx::query_scalar("SELECT body FROM tasks.sponsorship_transitions ORDER BY sequence")
            .fetch_all(database.pool())
            .await
            .expect("stored sponsorship transitions");
    let transitions = transitions
        .iter()
        .map(|body| serde_json::from_slice::<Value>(body).expect("transition JSON"))
        .collect::<Vec<_>>();
    oracle
        .stdin
        .take()
        .expect("oracle stdin")
        .write_all(
            &serde_json::to_vec(&json!({"snapshot": stored, "submission": submission,
                "transitions": transitions}))
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
    running.abort();
    server.abort();
}

/// Persist one self-withdrawal with no current writer or open-issue authority.
async fn apply_withdraw(database: &Database, outbox: &Outbox, withdraw: &Request) {
    let mut receipt = database.begin().await.expect("withdraw receipt");
    record_request(&mut receipt, withdraw)
        .await
        .expect("record withdraw");
    let (_, decision) = apply_request(
        &mut receipt,
        outbox,
        withdraw.id,
        withdraw.issue,
        Authority {
            writer: false,
            issue_open: false,
        },
    )
    .await
    .expect("apply withdraw")
    .expect("withdraw selected");
    assert_eq!(decision.status, concurro_tasks_core::RequestStatus::Applied);
    receipt.commit().await.expect("withdraw commit");
}
