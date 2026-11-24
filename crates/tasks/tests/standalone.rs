//! Authenticated standalone review request through timeline and authority boundaries.

use concurro_tasks_postgres::StandaloneReviewAuthority;
use std::{
    collections::{BTreeMap, HashMap},
    fs::{self, OpenOptions},
    os::unix::fs::OpenOptionsExt,
    process::Command,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
};

use axum::{
    Json, Router,
    routing::{get, post},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use chrono::{Duration, Utc};
use concurro_contracts::{GithubEvent, PullRequestPayload};
use concurro_db::Database;
use concurro_events::ProcessResult;
use concurro_events_postgres::{Outbox, migrate as migrate_events, process_delivery};
use concurro_exchange_v1::{Operator, TurnObservation};
use concurro_github_v1::{AppClient, user_grant::UserGrant};
use concurro_tasks::{
    InitializationContext, InitializationTarget, IntakeContext, IntakeTransport,
    PublicationContext, PublicationTarget, StandaloneReviewContext, StandaloneReviewHandler,
    StandaloneReviewTarget, observe_standalone_funding_policy, publish_request_reply,
    publish_standalone_dispositions, publish_standalone_review, publish_task_report,
    run_sponsorship_transition, run_standalone_review_decision, run_standalone_review_result,
    run_standalone_review_round, scan_standalone_review_wake, sync_standalone_review_check,
};
use concurro_tasks_core::{Actor, IssueKey, LaterCommand, RequestKind};
use concurro_tasks_postgres::{
    StandaloneReviewRequest, apply_standalone_review_request, list_tasks, migrate, pending_effects,
    pending_standalone_review_wakes, record_observation, record_standalone_review_request,
    standalone_review_task, task_detail, watched_standalone_review_prs,
};
use concurro_testkit::PostgresContainer;
use concurro_vault_v1::MetadataClient;
use serde_json::{Value, json};
use uuid::Uuid;

/// One requested reviewer opens one round from the timeline event, with a frozen PR snapshot.
#[tokio::test]
async fn review_request_delivery_enrolls_one_source_ordered_standalone_round() {
    verify_standalone_review(false).await;
}

/// A verified personal payer funds the same pinned standalone reviewer path.
#[tokio::test]
async fn contributed_standalone_review_uses_accepted_sponsor_slot() {
    verify_standalone_review(true).await;
}

/// Exercise one source-ordered standalone round under the selected funding mode.
async fn verify_standalone_review(contributed: bool) {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    migrate_events(&database).await.expect("event schema");
    let config = format!(
        "repository: acme/demo\nbase: main\nbot_login: concurro[bot]\nfunding_mode: {}\nmax_model_calls: 3\nmax_fix_rounds: 2\nmax_plan_revisions: 1\nmax_context_bytes: 350000\npolicy_documents: [README.md]\nqc: codex\nplanner: codex\nimplementer: codex\nreviewers: [codex]\nmodels: {{codex: gpt-test}}\npaths: {{protected: [], protected_extensions: [], protected_names: [], protected_name_prefixes: [], protected_files: [], generated: []}}\n",
        if contributed { "contributed" } else { "org" }
    );
    let reviews = Arc::new(tokio::sync::Mutex::new(Vec::<Value>::new()));
    let review_comments = Arc::new(tokio::sync::Mutex::new(Vec::<Value>::new()));
    let replies = Arc::new(tokio::sync::Mutex::new(Vec::<Value>::new()));
    let reply_posts = Arc::new(AtomicUsize::new(0));
    let resolved = Arc::new(AtomicBool::new(false));
    let checks = Arc::new(tokio::sync::Mutex::new(Vec::<Value>::new()));
    let check_reads = Arc::new(AtomicUsize::new(0));
    let posts = Arc::new(AtomicUsize::new(0));
    let artifacts = Arc::new(tokio::sync::Mutex::new(Vec::<Value>::new()));
    let artifact_list = artifacts.clone();
    let artifact_send = artifacts.clone();
    let artifact_detail = artifacts.clone();
    let command_comments = Arc::new(tokio::sync::Mutex::new(Vec::<Value>::new()));
    let command_comment_list = command_comments.clone();
    let command_comment_send = command_comments.clone();
    let review_list = reviews.clone();
    let review_send = reviews.clone();
    let comment_send = review_comments.clone();
    let comment_list = review_comments.clone();
    let sent_count = posts.clone();
    let pr_open = Arc::new(AtomicBool::new(true));
    let pr_state = pr_open.clone();
    let writer_access = Arc::new(AtomicBool::new(true));
    let writer_permission = writer_access.clone();
    let graphql_resolved = resolved.clone();
    let reply_list = replies.clone();
    let reply_send = replies.clone();
    let reply_count = reply_posts.clone();
    let check_list = checks.clone();
    let check_read_count = check_reads.clone();
    let check_send = checks.clone();
    let check_update = checks.clone();
    let app = Router::new()
        .route("/graphql", post(move |Json(body): Json<Value>| {
            let resolved = graphql_resolved.clone();
            async move {
                let query = body["query"].as_str().unwrap_or("");
                if query.contains("resolveReviewThread") {
                    resolved.store(true, Ordering::SeqCst);
                    return Json(json!({"data":{"resolveReviewThread":{"thread":{
                        "id":"PRRT_500","isResolved":true}}}}));
                }
                if query.contains("node(id:") {
                    let active = resolved.load(Ordering::SeqCst);
                    return Json(json!({"data":{"node":{"id":"PRRT_500",
                        "isResolved":active,"resolvedBy":if active {json!({"login":"concurro[bot]"})} else {Value::Null}}}}));
                }
                Json(json!({"data":{"repository":{"pullRequest":{"reviewThreads":{
                    "nodes":[{"id":"PRRT_500","comments":{"nodes":[{"id":"PRRC_500"}]}}],
                    "pageInfo":{"hasNextPage":false,"endCursor":null}
                }}}}}))
            }
        }))
        .route("/repos/acme/demo/pulls/4/comments", get(move || {
            let replies = reply_list.clone();
            async move { Json(json!(*replies.lock().await)) }
        }))
        .route("/repos/acme/demo/pulls/4/comments/500/replies", post(move |Json(body): Json<Value>| {
            let replies = reply_send.clone();
            let posts = reply_count.clone();
            async move {
                posts.fetch_add(1, Ordering::SeqCst);
                let reply = json!({"id":600,"body":body["body"],
                    "in_reply_to_id":500,"user":{"id":99,"login":"reviewer"}});
                replies.lock().await.push(reply.clone());
                Json(reply)
            }
        }))
        .route("/repos/acme/demo/commits/{sha}/check-runs", get(move || {
            let checks=check_list.clone();
            let reads=check_read_count.clone();
            async move {
                reads.fetch_add(1, Ordering::SeqCst);
                Json(json!({"check_runs":*checks.lock().await}))
            }
        }))
        .route("/repos/acme/demo/check-runs", post(move |Json(body): Json<Value>| {
            let checks=check_send.clone();
            async move {
                let mut runs=checks.lock().await;
                let mut created=body;
                created["id"]=json!(700);
                runs.push(created.clone());
                Json(created)
            }
        }))
        .route("/repos/acme/demo/check-runs/{id}", axum::routing::patch(move |Json(body): Json<Value>| {
            let checks=check_update.clone();
            async move {
                let mut runs=checks.lock().await;
                for run in runs.iter_mut() {
                    if run["id"]==700 {
                        for (key,value) in body.as_object().expect("check update") {
                            run[key]=value.clone();
                        }
                        return Json(run.clone());
                    }
                }
                Json(Value::Null)
            }
        }))
        .route("/metadata/7", get(|| async { Json(json!({
            "history_complete":true,
            "credentials":[{"record_id":"f".repeat(64),"user_id":"7",
                "route":"openai-api-key","version":1,"status":"valid",
                "models":null,"created":"2026-11-19T10:00:00Z",
                "updated":"2026-11-19T10:00:00Z","kind":"api_key",
                "expiry_known":false,"expires_at":null,"entry_id":"entry-a",
                "provider":"openai","name":"mine","enabled":true,
                "roles":["pr_reviewers"],
                "profiles":[{"target":"acme/demo","profile":"codex"}],
                "position":0,"removed":false}],
            "lists":{"openai":{"revision":1,"entries":["entry-a"]}}
        })) }))
        .route("/exchange/operator/sponsorship", post(|Json(body): Json<Value>| async move {
            Json(json!({"transitions":[{"transition":body["transition"],
                "epoch":body["slot"]["epoch"]}]}))
        }))
        .route("/repos/acme/demo/issues/4/artifacts", get(move || {
            let artifacts=artifact_list.clone();
            async move { Json(json!(*artifacts.lock().await)) }
        }).post(move |Json(body): Json<Value>| {
            let artifacts=artifact_send.clone();
            async move {
                let artifact=json!({"id":900,"number":1,"type":body["type"],
                    "name":body["name"],"body":body["body"],
                    "creator":{"login":"concurro[bot]"},
                    "updated_by_actor":{"login":"concurro[bot]"}});
                artifacts.lock().await.push(artifact.clone());
                Json(artifact)
            }
        }))
        .route("/repos/acme/demo/issues/4/artifacts/1", get(move || {
            let artifacts=artifact_detail.clone();
            async move {
                let artifact=artifacts.lock().await[0].clone();
                Json(json!({"artifact":artifact,"versions":[{"version":1,
                    "name":artifact["name"],"body":artifact["body"],
                    "actor":{"login":"concurro[bot]"}}]}))
            }
        }))
        .route("/repos/acme/demo/issues/4/comments", get(move || {
            let comments=command_comment_list.clone();
            async move {Json(json!(*comments.lock().await))}
        }).post(move |Json(body): Json<Value>| {
            let comments=command_comment_send.clone();
            async move {
                let now=Utc::now().to_rfc3339();
                let comment=json!({"id":1000+comments.lock().await.len(),
                    "body":body["body"],
                    "user":{"id":10,"login":"concurro[bot]","type":"Bot"},
                    "author_association":"MEMBER","created_at":now,"updated_at":now,
                    "html_url":"https://github.example/acme/demo/issues/4#issuecomment-1000"});
                comments.lock().await.push(comment.clone());
                Json(comment)
            }
        }))
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
            "/user/7",
            get(|| async { Json(json!({"id":7,"login":"writer"})) }),
        )
        .route(
            "/users/writer",
            get(|| async { Json(json!({"id":7,"login":"writer"})) }),
        )
        .route(
            "/repos/acme/demo/collaborators/writer/permission",
            get(move || {
                let writer = writer_permission.clone();
                async move {
                    Json(json!({"permission":if writer.load(Ordering::SeqCst) {
                        "write"
                    } else {
                        "read"
                    }}))
                }
            }),
        )
        .route(
            "/repos/acme/demo/issues/4/events",
            get(|| async {
                Json(json!([
                    {"id":122,"event":"review_requested",
                        "created_at":"2026-11-20T09:59:00Z",
                        "actor":{"id":7,"login":"writer"},
                        "requested_reviewer":{"id":99,"login":"reviewer"}},
                    {"id":123,"event":"review_requested",
                        "created_at":"2026-11-20T10:00:00Z",
                        "actor":{"id":7,"login":"writer"},
                        "requested_reviewer":{"id":99,"login":"reviewer"}}
                ]))
            }),
        )
        .route(
            "/repos/acme/demo/pulls/4",
            get(move || {
                let open = pr_state.load(Ordering::SeqCst);
                async move {
                Json(
                    json!({"id":44,"node_id":"P_44","number":4,"state":if open {"open"} else {"closed"},"merged":false,
                "requested_reviewers":[{"id":99,"login":"reviewer"}],
                "head":{"sha":"a".repeat(40)},
                "base":{"ref":"main","sha":"b".repeat(40)},
                "title":"External change","body":"Description",
                "user":{"login":"contributor"}}),
                )
                }
            }),
        )
        .route(
            "/repos/acme/demo/pulls/4/reviews",
            get(move || {
                let reviews = review_list.clone();
                async move { Json(json!(*reviews.lock().await)) }
            })
            .post(move |Json(body): Json<Value>| {
                let reviews = review_send.clone();
                let comments = comment_send.clone();
                let posts = sent_count.clone();
                async move {
                    posts.fetch_add(1, Ordering::SeqCst);
                    let review = json!({
                        "id":300,"body":body["body"],"commit_id":body["commit_id"],
                        "state":"COMMENTED","user":{"id":99,"login":"reviewer"}
                    });
                    reviews.lock().await.push(review.clone());
                    let submitted = body["comments"].as_array().cloned().unwrap_or_default();
                    *comments.lock().await = submitted.into_iter().enumerate().map(|(index, entry)| {
                        json!({
                            "id":500+index,
                            "node_id":format!("PRRC_{}",500+index),
                            "body":entry["body"],"path":entry["path"],
                            "line":entry["line"],"commit_id":body["commit_id"],
                            "user":{"id":99,"login":"reviewer"}
                        })
                    }).collect();
                    Json(review)
                }
            }),
        )
        .route(
            "/repos/acme/demo/pulls/4/reviews/300/comments",
            get(move || {
                let comments = comment_list.clone();
                async move { Json(json!(*comments.lock().await)) }
            }),
        )
        .route(
            "/repos/acme/demo",
            get(|| async { Json(json!({"id":8,"owner":{"id":9},"default_branch":"main"})) }),
        )
        .route(
            "/users/concurro[bot]",
            get(|| async { Json(json!({"id":10,"login":"concurro[bot]"})) }),
        )
        .route(
            "/repos/acme/demo/git/ref/heads/main",
            get(|| async { Json(json!({"object":{"sha":"c".repeat(40)}})) }),
        )
        .route(
            "/repos/acme/demo/contents/.concurro/config.yaml",
            get(move || { let config=config.clone(); async move {
                Json(
                    json!({"type":"file","encoding":"base64","path":".concurro/config.yaml",
                "size":config.len(),"content":STANDARD.encode(config)}),
                )
            }}),
        )
        .route(
            "/repos/acme/demo/compare/{compare}",
            get(|| async { Json(json!({"merge_base_commit":{"sha":"d".repeat(40)}})) }),
        )
        .route(
            "/repos/acme/demo/contents/.concurro/charter.json",
            get(|| async {
                let charter = "{\"version\":1,\"id\":\"test\",\"goals\":{\"g\":\"v\"},\"non_goals\":[\"n\"],\"constraints\":[\"c\"],\"acceptance\":[\"a\"]}";
                Json(json!({"type":"file","encoding":"base64",
                    "path":".concurro/charter.json","size":charter.len(),
                    "content":STANDARD.encode(charter)}))
            }),
        )
        .route(
            "/repos/acme/demo/contents/README.md",
            get(|| async {
                let policy = "# Review policy\n";
                Json(json!({"type":"file","encoding":"base64",
                    "path":"README.md","size":policy.len(),
                    "content":STANDARD.encode(policy)}))
            }),
        )
        .route(
            "/repos/acme/demo/git/trees/{sha}",
            get(|| async {
                Json(json!({"truncated":false,"tree":[
                    {"path":"README.md","type":"blob","sha":"f".repeat(40),
                        "size":16,"mode":"100644"},
                    {"path":"src/lib.rs","type":"blob","sha":"e".repeat(40),
                        "mode":"100644"}
                ]}))
            }),
        )
        .route(
            "/repos/acme/demo/git/blobs/{sha}",
            get(|| async {
                let source = "fn value() {}\n";
                Json(json!({"sha":"e".repeat(40),"encoding":"base64",
                    "content":STANDARD.encode(source)}))
            }),
        )
        .route(
            "/repos/acme/demo/pulls/4/files",
            get(|| async { Json(json!([{"filename":"src/lib.rs","status":"modified"}])) }),
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
    let grant_path = std::env::temp_dir().join(format!("concurro-review-grant-{}", Uuid::now_v7()));
    let mut grant_file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&grant_path)
        .expect("grant file");
    use std::io::Write;
    grant_file
        .write_all(
            serde_json::to_string(
                &json!({"token":"reviewer-token","actor":{"id":99,"login":"reviewer"}}),
            )
            .expect("grant JSON")
            .as_bytes(),
        )
        .expect("grant write");
    drop(grant_file);
    let grant = UserGrant::open(
        reqwest::Client::new(),
        &format!("http://{address}"),
        &grant_path,
        "test-client",
        &grant_path,
        99,
    )
    .expect("reviewer grant");
    let context = StandaloneReviewContext {
        github: AppClient::new(
            reqwest::Client::new(),
            &format!("http://{address}"),
            42,
            &key.stdout,
        )
        .expect("App identity"),
        targets: HashMap::from([(
            8,
            StandaloneReviewTarget {
                source: InitializationTarget {
                    issue_repository: "acme/demo".to_owned(),
                    fork: "acme/demo".to_owned(),
                    upstream: "acme/demo".to_owned(),
                    issue_permissions: BTreeMap::new(),
                    fork_permissions: BTreeMap::from([("contents".to_owned(), "read".to_owned())]),
                    upstream_permissions: BTreeMap::from([(
                        "pull_requests".to_owned(),
                        "read".to_owned(),
                    )]),
                    bot_login: "concurro[bot]".to_owned(),
                    publisher_logins: vec![],
                    engine_sha: "e".repeat(40),
                },
                runner_repository_id: 8,
                reviewer_id: 99,
                upstream_write_permissions: BTreeMap::from([(
                    "pull_requests".to_owned(),
                    "write".to_owned(),
                )]),
                grant: Some(grant),
            },
        )]),
        outbox: Outbox::new(1024 * 1024, vec!["request.>".to_owned()]),
        rust_exchange: false,
    };
    observe_standalone_funding_policy(&database, &context, 8, &"c".repeat(40))
        .await
        .expect("current-head funding policy");
    let mut delivery: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/github.pull_request.v1/valid/representative.json"
    ))
    .expect("delivery fixture");
    delivery["id"] = json!(Uuid::now_v7());
    delivery["data"]["delivery_id"] = json!("standalone-request");
    delivery["data"]["repository"]["full_name"] = json!("acme/demo");
    delivery["data"]["pull_request"]["number"] = json!(4);
    delivery["data"]["pull_request"]["updated_at"] = json!("2026-11-20T10:00:00Z");
    delivery["data"]["requested_reviewer"]["id"] = json!(99);
    delivery["data"]["sender"]["id"] = json!(7);
    delivery["data"]["sender"]["login"] = json!("writer");
    assert_eq!(
        process_delivery::<GithubEvent<PullRequestPayload>, _>(
            &database,
            "standalone-intake",
            3,
            &serde_json::to_vec(&delivery).expect("wire"),
            "standalone-request",
            &StandaloneReviewHandler::new(context.clone()),
        )
        .await
        .expect("delivery"),
        ProcessResult::Done
    );
    let wakes = pending_standalone_review_wakes(&database)
        .await
        .expect("pending wake");
    assert_eq!(wakes.len(), 1);
    scan_standalone_review_wake(&database, &context, &wakes[0])
        .await
        .expect("timeline scan");
    let funding_command = Uuid::now_v7();
    sqlx::query(
        "INSERT INTO tasks.standalone_review_commands \
        (id,repository_id,upstream,pr_number,comment_id,source,source_at,actor, \
         comment_author_id,kind,body,deleted) \
        VALUES ($1,8,'acme/demo',4,120,'120:funding', \
         '2026-11-20T09:58:00Z',$2,7,$3,$4,false)",
    )
    .bind(funding_command)
    .bind(sqlx::types::Json(json!({"id":7,"login":"writer"})))
    .bind(sqlx::types::Json(RequestKind::Later(
        LaterCommand::Funding {
            provider: "openai".to_owned(),
            mode: (if contributed { "personal" } else { "org" }).to_owned(),
        },
    )))
    .bind(if contributed {
        "/concurro funding openai personal"
    } else {
        "/concurro funding openai org"
    })
    .execute(database.pool())
    .await
    .expect("source-ordered funding command");
    run_standalone_review_decision(&database, &context, 8, "acme/demo", 4)
        .await
        .expect("review decision");
    let task = standalone_review_task(&database, "acme/demo", 4)
        .await
        .expect("task")
        .expect("standalone task");
    assert_eq!(task["stage"], "queued");
    assert_eq!(task["round"], 1);
    let command_status: String =
        sqlx::query_scalar("SELECT status FROM tasks.standalone_review_commands WHERE id=$1")
            .bind(funding_command)
            .fetch_one(database.pool())
            .await
            .expect("command decision");
    assert_eq!(command_status, "applied");
    let facts: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM tasks.standalone_review_funding_facts WHERE command_id=$1",
    )
    .bind(funding_command)
    .fetch_one(database.pool())
    .await
    .expect("funding fact");
    assert_eq!(facts, 1);
    let sources: Vec<(i64, String)> = sqlx::query_as(
        "SELECT source_id,status FROM tasks.standalone_review_requests \
         WHERE upstream='acme/demo' AND pr_number=4 ORDER BY source_id",
    )
    .fetch_all(database.pool())
    .await
    .expect("source dispositions");
    assert_eq!(
        sources,
        vec![(122, "superseded".to_owned()), (123, "applied".to_owned())]
    );
    let snapshot: Value = sqlx::query_scalar::<_, sqlx::types::Json<Value>>(
        "SELECT snapshot FROM tasks.standalone_review_rounds WHERE number=1",
    )
    .fetch_one(database.pool())
    .await
    .expect("frozen round")
    .0;
    assert_eq!(snapshot["head"], "a".repeat(40));
    assert_eq!(snapshot["merge_base"], "d".repeat(40));
    assert_eq!(snapshot["changed_paths"], json!(["src/lib.rs"]));
    assert_eq!(snapshot["panel"].as_array().expect("panel").len(), 1);
    assert!(
        pending_standalone_review_wakes(&database)
            .await
            .expect("settled wake")
            .is_empty()
    );
    let task_id: Uuid = serde_json::from_value(task["id"].clone()).expect("task ID");
    sync_standalone_review_check(&database, &context, task_id)
        .await
        .expect("queued activity check");
    assert_eq!(checks.lock().await[0]["status"], "queued");
    let outbox = Outbox::new(
        1024 * 1024,
        vec![
            "task.>".to_owned(),
            "turn.>".to_owned(),
            "request.>".to_owned(),
        ],
    );
    let intake = IntakeContext {
        initialization: InitializationContext {
            github: context.github.clone(),
            targets: HashMap::from([(8, context.targets[&8].source.clone())]),
            outbox: outbox.clone(),
            vault: contributed.then(|| {
                MetadataClient::new(
                    reqwest::Client::new(),
                    format!("http://{address}/"),
                    "metadata-only",
                )
            }),
            rust_exchange: false,
        },
        transport: IntakeTransport {
            gateway: json!({"url":"http://gateway.invalid"}),
            workflow_ref: "acme/demo/.github/workflows/review.yml@refs/heads/main".to_owned(),
            models: vec!["gpt-test".to_owned()],
            budget: 1000,
            lease: 1200,
        },
    };
    pr_open.store(false, Ordering::SeqCst);
    run_standalone_review_round(&database, &context, &intake, task_id, 1)
        .await
        .expect("closed PR pause");
    assert_eq!(
        standalone_review_task(&database, "acme/demo", 4)
            .await
            .expect("closed task")
            .expect("task")["stage"],
        "closed"
    );
    let before: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM tasks.standalone_review_attempts WHERE task_id=$1",
    )
    .bind(task_id)
    .fetch_one(database.pool())
    .await
    .expect("no closed admission");
    assert_eq!(before, 0);
    pr_open.store(true, Ordering::SeqCst);
    if contributed {
        let pending = run_standalone_review_round(&database, &context, &intake, task_id, 1)
            .await
            .expect_err("sponsorship must be acknowledged before charge");
        assert!(
            pending.to_string().contains("transition is pending"),
            "{pending:#}"
        );
        let exchange = Operator::new(
            reqwest::Client::new(),
            format!("http://{address}"),
            "operator-only",
        );
        let transitions: Vec<String> = sqlx::query_scalar(
            "SELECT transition FROM tasks.sponsorship_transitions \
             WHERE standalone_task_id=$1 ORDER BY sequence",
        )
        .bind(task_id)
        .fetch_all(database.pool())
        .await
        .expect("frozen transitions");
        for transition in transitions {
            run_sponsorship_transition(&database, &exchange, &transition)
                .await
                .expect("Exchange sponsorship receipt");
        }
    }
    run_standalone_review_round(&database, &context, &intake, task_id, 1)
        .await
        .expect("funded reviewer turn");
    let turn: Uuid = sqlx::query_scalar(
        "SELECT attempt_id FROM tasks.standalone_review_attempts WHERE task_id=$1",
    )
    .bind(task_id)
    .fetch_one(database.pool())
    .await
    .expect("admitted reviewer turn");
    let grant_kind: String = sqlx::query_scalar(
        "SELECT purpose FROM tasks.standalone_review_funding_grants WHERE attempt_id=$1",
    )
    .bind(turn)
    .fetch_one(database.pool())
    .await
    .expect("funding grant");
    assert_eq!(grant_kind, if contributed { "contributed" } else { "org" });
    if contributed {
        let (payer, epoch, grant): (i64, i64, String) = sqlx::query_as(
            "SELECT payer_id,slot_epoch,slot_grant_id FROM tasks.standalone_review_funding_grants \
             WHERE attempt_id=$1",
        )
        .bind(turn)
        .fetch_one(database.pool())
        .await
        .expect("exact payer");
        let body: Vec<u8> =
            sqlx::query_scalar("SELECT submit_body FROM tasks.attempts WHERE id=$1")
                .bind(turn)
                .fetch_one(database.pool())
                .await
                .expect("frozen submission");
        let submission: concurro_exchange_v1::TurnSubmission =
            serde_json::from_slice(&body).expect("submission");
        assert_eq!(payer, 7);
        assert_eq!(submission.binding["sponsor"], "7");
        assert_eq!(submission.binding["epoch"], epoch);
        assert_eq!(submission.binding["grant_id"], grant);
    }
    assert_eq!(
        standalone_review_task(&database, "acme/demo", 4)
            .await
            .expect("charged task")
            .expect("task")["used_model_calls"],
        1
    );
    sqlx::query("UPDATE tasks.attempts SET status='running' WHERE id=$1")
        .bind(turn)
        .execute(database.pool())
        .await
        .expect("runner started");
    let observation: TurnObservation = serde_json::from_value(json!({
        "turn":turn,"status":"done","created":1.0,"finished":2.0,
        "result":{"outcome":"ok","response":{
            "verdict":"changes_requested","summary":"One line needs attention",
            "findings":[{"path":"src/lib.rs","start_line":null,"line":1,
                "detail":"Handle the empty input"}],
            "dispositions":[],"design_issue":null
        }}
    }))
    .expect("model result");
    let mut work = database.begin().await.expect("result transaction");
    assert!(
        record_observation(&mut work, &outbox, &observation, None)
            .await
            .expect("model result retention")
    );
    work.commit().await.expect("result commit");
    run_standalone_review_result(&database, &context, turn)
        .await
        .expect("source-aware review validation");
    let publication = PublicationContext {
        github: context.github.clone(),
        targets: HashMap::new(),
        standalone_targets: HashMap::from([(
            8,
            PublicationTarget {
                repository: "acme/demo".to_owned(),
                read_permissions: BTreeMap::from([("issues".to_owned(), "read".to_owned())]),
                write_permissions: BTreeMap::from([("issues".to_owned(), "write".to_owned())]),
                bot_login: "concurro[bot]".to_owned(),
            },
        )]),
    };
    let reply_id: Uuid = sqlx::query_scalar(
        "SELECT id FROM tasks.publication_comments WHERE standalone_command_id=$1",
    )
    .bind(funding_command)
    .fetch_one(database.pool())
    .await
    .expect("command reply");
    publish_request_reply(&database, &publication, reply_id)
        .await
        .expect("App command reply");
    publish_request_reply(&database, &publication, reply_id)
        .await
        .expect("reply replay");
    assert_eq!(command_comments.lock().await.len(), 1);
    publish_task_report(&database, &publication, turn)
        .await
        .expect("report artifact");
    publish_task_report(&database, &publication, turn)
        .await
        .expect("artifact replay");
    assert_eq!(artifacts.lock().await.len(), 1);
    let artifact_status: String =
        sqlx::query_scalar("SELECT status FROM tasks.report_artifact_writes WHERE report_id=$1")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("artifact status");
    assert_eq!(artifact_status, "confirmed");
    let pending = pending_effects(&database)
        .await
        .expect("operator pending view");
    assert!(
        pending
            .iter()
            .any(|item| item["kind"] == "standalone_review_publication"
                && item["turn_id"] == json!(turn))
    );
    assert!(
        pending
            .iter()
            .any(|item| item["kind"] == "submit" && item["owner_kind"] == "standalone_pr_review")
    );
    publish_standalone_review(&database, &context, turn)
        .await
        .expect("reviewer-account publication");
    assert_eq!(posts.load(Ordering::SeqCst), 1);
    assert_eq!(reviews.lock().await.len(), 1);
    assert_eq!(review_comments.lock().await.len(), 1);
    let thread: String = sqlx::query_scalar(
        "SELECT thread_node_id FROM tasks.standalone_review_findings WHERE task_id=$1 AND id='F1'",
    )
    .bind(task_id)
    .fetch_one(database.pool())
    .await
    .expect("native finding thread");
    assert_eq!(thread, "PRRT_500");
    let published = standalone_review_task(&database, "acme/demo", 4)
        .await
        .expect("settled task")
        .expect("task");
    assert_eq!(published["stage"], "complete");
    sync_standalone_review_check(&database, &context, task_id)
        .await
        .expect("finished activity check");
    assert_eq!(checks.lock().await[0]["status"], "completed");
    assert_eq!(checks.lock().await[0]["conclusion"], "neutral");
    sqlx::query(
        "UPDATE tasks.standalone_review_checks \
         SET recheck_until=now()-interval '1 second' WHERE task_id=$1",
    )
    .bind(task_id)
    .execute(database.pool())
    .await
    .expect("finish duplicate readback window");
    let reads = check_reads.load(Ordering::SeqCst);
    sync_standalone_review_check(&database, &context, task_id)
        .await
        .expect("idle completed check");
    assert_eq!(check_reads.load(Ordering::SeqCst), reads);
    assert!(
        !watched_standalone_review_prs(&database)
            .await
            .expect("idle PR watch")
            .iter()
            .any(|(watched, _)| *watched == task_id)
    );
    let publication: (String, i64) = sqlx::query_as(
        "SELECT status,review_id FROM tasks.standalone_review_publications WHERE turn_id=$1",
    )
    .bind(turn)
    .fetch_one(database.pool())
    .await
    .expect("native review receipt");
    assert_eq!(publication, ("confirmed".to_owned(), 300));
    sqlx::query(
        "UPDATE tasks.standalone_review_findings SET status='fixed' WHERE task_id=$1 AND id='F1'",
    )
    .bind(task_id)
    .execute(database.pool())
    .await
    .expect("fixed finding");
    sqlx::query(
        "INSERT INTO tasks.standalone_review_dispositions \
        (turn_id,task_id,finding_id,marker,body,resolve_status) \
        VALUES ($1,$2,'F1','<!-- disposition -->','<!-- disposition --> fixed','recorded')",
    )
    .bind(turn)
    .bind(task_id)
    .execute(database.pool())
    .await
    .expect("disposition");
    publish_standalone_dispositions(&database, &context, turn)
        .await
        .expect("reviewer reply and App resolution");
    publish_standalone_dispositions(&database, &context, turn)
        .await
        .expect("idempotent disposition readback");
    assert_eq!(reply_posts.load(Ordering::SeqCst), 1);
    assert!(resolved.load(Ordering::SeqCst));
    let disposition: (String, String, i64) = sqlx::query_as(
        "SELECT reply_status,resolve_status,reply_id FROM tasks.standalone_review_dispositions \
         WHERE turn_id=$1 AND finding_id='F1'",
    )
    .bind(turn)
    .fetch_one(database.pool())
    .await
    .expect("settled disposition");
    assert_eq!(
        disposition,
        ("confirmed".to_owned(), "confirmed".to_owned(), 600)
    );
    let listing = list_tasks(&database).await.expect("operator listing");
    assert_eq!(listing.len(), 1);
    assert_eq!(listing[0]["kind"], "standalone_pr_review");
    let detail = task_detail(
        &database,
        IssueKey {
            repository_id: 8,
            number: 4,
        },
    )
    .await
    .expect("operator detail")
    .expect("standalone detail");
    assert_eq!(detail["rounds"][0]["status"], "complete");
    assert_eq!(detail["findings"][0]["thread_node_id"], "PRRT_500");
    assert_eq!(detail["review_publications"][0]["status"], "confirmed");
    let later = Uuid::now_v7();
    sqlx::query(
        "INSERT INTO tasks.standalone_review_commands \
        (id,repository_id,upstream,pr_number,comment_id,source,source_at,actor, \
         comment_author_id,kind,body,deleted) \
        VALUES ($1,8,'acme/demo',4,125,'125:funding', \
         '2026-11-20T10:02:00Z',$2,7,$3,'/concurro funding openai personal',false)",
    )
    .bind(later)
    .bind(sqlx::types::Json(json!({"id":7,"login":"writer"})))
    .bind(sqlx::types::Json(RequestKind::Later(
        LaterCommand::Funding {
            provider: "openai".to_owned(),
            mode: "personal".to_owned(),
        },
    )))
    .execute(database.pool())
    .await
    .expect("later funding command");
    run_standalone_review_decision(&database, &context, 8, "acme/demo", 4)
        .await
        .expect("later funding decision");
    let later_status: String =
        sqlx::query_scalar("SELECT status FROM tasks.standalone_review_commands WHERE id=$1")
            .bind(later)
            .fetch_one(database.pool())
            .await
            .expect("later command status");
    assert_eq!(later_status, "applied");
    let later_facts: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM tasks.standalone_review_funding_facts WHERE command_id=$1",
    )
    .bind(later)
    .fetch_one(database.pool())
    .await
    .expect("later funding facts");
    assert_eq!(later_facts, 1);
    let transitions: Vec<(i64, String)> = sqlx::query_as(
        "SELECT sequence,status FROM tasks.sponsorship_transitions \
         WHERE standalone_task_id=$1 AND provider='openai' ORDER BY sequence",
    )
    .bind(task_id)
    .fetch_all(database.pool())
    .await
    .expect("transition rows");
    assert_eq!(transitions.len(), 2, "transition rows: {transitions:?}");
    let slot_body: Vec<u8> = sqlx::query_scalar(
        "SELECT body FROM tasks.sponsorship_transitions \
         WHERE standalone_task_id=$1 AND provider='openai' ORDER BY sequence DESC LIMIT 1",
    )
    .bind(task_id)
    .fetch_one(database.pool())
    .await
    .expect("later funding transition");
    let slot: Value = serde_json::from_slice(&slot_body).expect("funding JSON");
    assert_eq!(slot["reason"], "funding");
    assert_eq!(slot["slot"]["funding"], "contributed");
    assert_eq!(slot["slot"]["sponsor_id"], "7");
    let removal = Uuid::now_v7();
    sqlx::query(
        "INSERT INTO tasks.standalone_review_commands \
        (id,repository_id,upstream,pr_number,comment_id,source,source_at,actor, \
         comment_author_id,kind,body,deleted) \
        VALUES ($1,8,'acme/demo',4,125,'125:removed', \
         '2026-11-20T10:03:00Z',$2,7,NULL,'removed command',false)",
    )
    .bind(removal)
    .bind(sqlx::types::Json(json!({"id":7,"login":"writer"})))
    .execute(database.pool())
    .await
    .expect("edited command");
    run_standalone_review_decision(&database, &context, 8, "acme/demo", 4)
        .await
        .expect("removed funding decision");
    let withdrawn: Value = serde_json::from_slice(
        &sqlx::query_scalar::<_, Vec<u8>>(
            "SELECT body FROM tasks.sponsorship_transitions \
         WHERE standalone_task_id=$1 AND provider='openai' ORDER BY sequence DESC LIMIT 1",
        )
        .bind(task_id)
        .fetch_one(database.pool())
        .await
        .expect("withdrawal transition"),
    )
    .expect("withdrawal JSON");
    assert_eq!(withdrawn["reason"], "funding");
    assert_eq!(withdrawn["slot"]["status"], "withdrawn");
    assert_eq!(
        withdrawn["slot"]["funding"],
        if contributed { "contributed" } else { "org" }
    );
    sqlx::query("UPDATE tasks.standalone_review_tasks SET stage='blocked' WHERE id=$1")
        .bind(task_id)
        .execute(database.pool())
        .await
        .expect("paused task");
    sqlx::query(
        "UPDATE tasks.standalone_review_rounds SET status='blocked' \
        WHERE task_id=$1 AND number=1",
    )
    .bind(task_id)
    .execute(database.pool())
    .await
    .expect("paused round");
    let resume = Uuid::now_v7();
    sqlx::query(
        "INSERT INTO tasks.standalone_review_commands \
        (id,repository_id,upstream,pr_number,comment_id,source,source_at,actor, \
         comment_author_id,kind,body,deleted) \
        VALUES ($1,8,'acme/demo',4,126,'126:resume', \
         '2026-11-20T10:04:00Z',$2,7,$3,'/concurro resume',false)",
    )
    .bind(resume)
    .bind(sqlx::types::Json(json!({"id":7,"login":"writer"})))
    .bind(sqlx::types::Json(RequestKind::Later(
        LaterCommand::Resume {
            description_only: false,
        },
    )))
    .execute(database.pool())
    .await
    .expect("resume command");
    run_standalone_review_decision(&database, &context, 8, "acme/demo", 4)
        .await
        .expect("resume adoption");
    let adoption: Value = sqlx::query_scalar::<_, sqlx::types::Json<Value>>(
        "SELECT adopted_snapshot FROM tasks.standalone_review_adoptions WHERE command_id=$1",
    )
    .bind(resume)
    .fetch_one(database.pool())
    .await
    .expect("adoption receipt")
    .0;
    assert_eq!(adoption["head"], "a".repeat(40));
    assert_eq!(adoption["config_sha"], "c".repeat(40));
    assert_eq!(
        standalone_review_task(&database, "acme/demo", 4)
            .await
            .expect("resumed task")
            .expect("task")["stage"],
        "review"
    );
    for (source_id, verb, kind, expected) in [
        (127, "stop", RequestKind::Stop, "stopped"),
        (
            128,
            "retry",
            RequestKind::Later(LaterCommand::Retry {
                plan_revisions: None,
            }),
            "review",
        ),
    ] {
        let command_id = Uuid::now_v7();
        sqlx::query(
            "INSERT INTO tasks.standalone_review_commands \
            (id,repository_id,upstream,pr_number,comment_id,source,source_at,actor, \
             comment_author_id,kind,body,deleted) \
            VALUES ($1,8,'acme/demo',4,$2,$3,$4::timestamptz,$5,7,$6,$7,false)",
        )
        .bind(command_id)
        .bind(source_id)
        .bind(format!("{source_id}:{verb}"))
        .bind(format!("2026-11-20T10:{:02}:00Z", source_id - 122))
        .bind(sqlx::types::Json(json!({"id":7,"login":"writer"})))
        .bind(sqlx::types::Json(kind))
        .bind(format!("/concurro {verb}"))
        .execute(database.pool())
        .await
        .expect("control command");
        run_standalone_review_decision(&database, &context, 8, "acme/demo", 4)
            .await
            .expect("control decision");
        assert_eq!(
            standalone_review_task(&database, "acme/demo", 4)
                .await
                .expect("control task")
                .expect("task")["stage"],
            expected
        );
    }
    let detail = task_detail(
        &database,
        IssueKey {
            repository_id: 8,
            number: 4,
        },
    )
    .await
    .expect("updated operator detail")
    .expect("standalone task");
    assert_eq!(detail["adoptions"].as_array().expect("adoptions").len(), 1);
    assert!(detail["commands"].as_array().expect("commands").len() >= 5);
    for (source, at, kind, body) in [
        (
            "129:org",
            "2026-11-20T10:07:00Z",
            Some(RequestKind::Later(LaterCommand::Funding {
                provider: "openai".to_owned(),
                mode: "org".to_owned(),
            })),
            "/concurro funding openai org",
        ),
        ("129:removed", "2026-11-20T10:08:00Z", None, "removed"),
    ] {
        sqlx::query(
            "INSERT INTO tasks.standalone_review_commands \
            (id,repository_id,upstream,pr_number,comment_id,source,source_at,actor, \
             comment_author_id,kind,body,deleted) \
            VALUES ($1,8,'acme/demo',4,129,$2,$3::timestamptz,$4,7,$5,$6,false)",
        )
        .bind(Uuid::now_v7())
        .bind(source)
        .bind(at)
        .bind(sqlx::types::Json(json!({"id":7,"login":"writer"})))
        .bind(kind.map(sqlx::types::Json))
        .bind(body)
        .execute(database.pool())
        .await
        .expect("org funding revision");
        run_standalone_review_decision(&database, &context, 8, "acme/demo", 4)
            .await
            .expect("org funding decision");
    }
    let body: Vec<u8> = sqlx::query_scalar(
        "SELECT body FROM tasks.sponsorship_transitions \
         WHERE standalone_task_id=$1 AND provider='openai' ORDER BY sequence DESC LIMIT 1",
    )
    .bind(task_id)
    .fetch_one(database.pool())
    .await
    .expect("reset funding transition");
    let reset: Value = serde_json::from_slice(&body).expect("funding reset JSON");
    assert_eq!(
        reset["slot"]["funding"],
        if contributed { "contributed" } else { "org" }
    );
    assert_eq!(reset["slot"]["status"], "withdrawn");
    assert!(reset["slot"]["funding_order"].is_null());
    let exchange = Operator::new(
        reqwest::Client::new(),
        format!("http://{address}"),
        "operator-only",
    );
    let transitions: Vec<String> = sqlx::query_scalar(
        "SELECT transition FROM tasks.sponsorship_transitions \
         WHERE standalone_task_id=$1 AND status='recorded' ORDER BY sequence",
    )
    .bind(task_id)
    .fetch_all(database.pool())
    .await
    .expect("pending funding writes");
    for transition in transitions {
        run_sponsorship_transition(&database, &exchange, &transition)
            .await
            .expect("ordered Exchange funding receipt");
    }
    let pending: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM tasks.sponsorship_transitions \
         WHERE standalone_task_id=$1 AND status<>'acknowledged'",
    )
    .bind(task_id)
    .fetch_one(database.pool())
    .await
    .expect("settled funding writes");
    assert_eq!(pending, 0);
    writer_access.store(false, Ordering::SeqCst);
    let withdrawal = Uuid::now_v7();
    sqlx::query(
        "INSERT INTO tasks.standalone_review_commands \
         (id,repository_id,upstream,pr_number,comment_id,source,source_at,actor, \
          comment_author_id,kind,body,deleted) \
         VALUES ($1,8,'acme/demo',4,130,'130:withdraw', \
         '2026-11-20T10:09:00Z',$2,7,$3,'/concurro withdraw openai',false)",
    )
    .bind(withdrawal)
    .bind(sqlx::types::Json(json!({"id":7,"login":"writer"})))
    .bind(sqlx::types::Json(RequestKind::Later(
        LaterCommand::Withdraw {
            provider: "openai".to_owned(),
        },
    )))
    .execute(database.pool())
    .await
    .expect("former writer withdrawal");
    run_standalone_review_decision(&database, &context, 8, "acme/demo", 4)
        .await
        .expect("self withdrawal decision");
    let withdrawal_status: String =
        sqlx::query_scalar("SELECT status FROM tasks.standalone_review_commands WHERE id=$1")
            .bind(withdrawal)
            .fetch_one(database.pool())
            .await
            .expect("self withdrawal status");
    assert_eq!(withdrawal_status, "applied");
    server.abort();
    fs::remove_file(grant_path).expect("remove grant");
}

/// A reviewer user token publishes one exact native review and advances its round once.
#[tokio::test]
async fn reviewer_account_publication_confirms_exact_native_review_once() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 2)
        .await
        .expect("database");
    migrate(&database).await.expect("task schema");
    let reviews = Arc::new(tokio::sync::Mutex::new(Vec::<Value>::new()));
    let posts = Arc::new(AtomicUsize::new(0));
    let review_list = reviews.clone();
    let review_send = reviews.clone();
    let sent_count = posts.clone();
    let app = Router::new()
        .route(
            "/repos/acme/demo/pulls/4",
            get(|| async { Json(json!({"state":"open","head":{"sha":"a".repeat(40)}})) }),
        )
        .route(
            "/repos/acme/demo/pulls/4/reviews",
            get(move || {
                let reviews = review_list.clone();
                async move { Json(json!(*reviews.lock().await)) }
            })
            .post(move |Json(body): Json<Value>| {
                let reviews = review_send.clone();
                let posts = sent_count.clone();
                async move {
                    posts.fetch_add(1, Ordering::SeqCst);
                    let review = json!({
                        "id":300,"body":body["body"],"commit_id":body["commit_id"],
                        "state":"COMMENTED","user":{"id":99,"login":"reviewer"}
                    });
                    reviews.lock().await.push(review.clone());
                    Json(review)
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
    let grant_path = std::env::temp_dir().join(format!("concurro-review-grant-{}", Uuid::now_v7()));
    let mut grant_file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&grant_path)
        .expect("grant file");
    use std::io::Write;
    grant_file
        .write_all(
            serde_json::to_string(
                &json!({"token":"reviewer-token","actor":{"id":99,"login":"reviewer"}}),
            )
            .expect("grant JSON")
            .as_bytes(),
        )
        .expect("grant write");
    drop(grant_file);
    let grant = UserGrant::open(
        reqwest::Client::new(),
        &format!("http://{address}"),
        &grant_path,
        "test-client",
        &grant_path,
        99,
    )
    .expect("reviewer grant");
    let context = StandaloneReviewContext {
        github: AppClient::new(
            reqwest::Client::new(),
            &format!("http://{address}"),
            42,
            &key.stdout,
        )
        .expect("App identity"),
        targets: HashMap::from([(
            8,
            StandaloneReviewTarget {
                source: InitializationTarget {
                    issue_repository: "acme/demo".to_owned(),
                    fork: "acme/demo".to_owned(),
                    upstream: "acme/demo".to_owned(),
                    issue_permissions: BTreeMap::new(),
                    fork_permissions: BTreeMap::new(),
                    upstream_permissions: BTreeMap::new(),
                    bot_login: "concurro[bot]".to_owned(),
                    publisher_logins: vec![],
                    engine_sha: "e".repeat(40),
                },
                runner_repository_id: 8,
                reviewer_id: 99,
                upstream_write_permissions: BTreeMap::from([(
                    "pull_requests".to_owned(),
                    "write".to_owned(),
                )]),
                grant: Some(grant),
            },
        )]),
        outbox: Outbox::new(1024 * 1024, vec!["request.>".to_owned()]),
        rust_exchange: false,
    };
    let requested = StandaloneReviewRequest {
        id: Uuid::now_v7(),
        repository_id: 8,
        upstream: "acme/demo".to_owned(),
        pr_number: 4,
        source_id: 20,
        source_at: Utc::now(),
        actor: Actor {
            id: 7,
            login: "writer".to_owned(),
        },
        action: "review_requested".to_owned(),
    };
    let mut work = database.begin().await.expect("request transaction");
    record_standalone_review_request(&mut work, &requested)
        .await
        .expect("request");
    apply_standalone_review_request(
&mut work,
requested.id,
StandaloneReviewAuthority { upstream: &requested.upstream, pr_number: requested.pr_number, reviewer_id: 99, runner_repository_id: 8, actor_writer: true, pr_open: true, reviewer_requested: true, source_defect: None, snapshot: Some(&json!({"head":"a".repeat(40),"panel":[{"id":"reviewer","provider":"openai","model":"test","roles":["pr_review"]}]})) },
)
    .await
    .expect("round");
    work.commit().await.expect("request commit");
    let task: Uuid = sqlx::query_scalar(
        "SELECT id FROM tasks.standalone_review_tasks WHERE upstream='acme/demo' AND pr_number=4",
    )
    .fetch_one(database.pool())
    .await
    .expect("task");
    let turn = Uuid::now_v7();
    sqlx::query(
        "INSERT INTO tasks.attempts \
         (id,standalone_task_id,operation,expected_revision,staffing_group,fork,config_sha, \
          submit_body,initial_deadline,deadline,unbounded_running,status) \
         VALUES ($1,$2,'standalone_review',1,'standalone-test','acme/demo',$3, \
                 $4,now()+interval '1 hour',now()+interval '1 hour',false,'finished')",
    )
    .bind(turn)
    .bind(task)
    .bind("a".repeat(40))
    .bind(b"{}".as_slice())
    .execute(database.pool())
    .await
    .expect("attempt");
    sqlx::query(
        "INSERT INTO tasks.standalone_review_attempts \
         (attempt_id,task_id,round,reviewer_id,status) VALUES ($1,$2,1,'reviewer','validated')",
    )
    .bind(turn)
    .bind(task)
    .execute(database.pool())
    .await
    .expect("review seat");
    sqlx::query("UPDATE tasks.standalone_review_tasks SET stage='review' WHERE id=$1")
        .bind(task)
        .execute(database.pool())
        .await
        .expect("active round");
    let marker = format!("<!-- concurro:pr-review:{task}:{turn} -->");
    let body = format!("{marker}\n**Review**\n\nNo issues found.");
    sqlx::query(
        "INSERT INTO tasks.standalone_review_publications \
         (turn_id,task_id,round,reviewer_id,pr_number,head,marker,body,reviewer_user_id) \
         VALUES ($1,$2,1,'reviewer',4,$3,$4,$5,99)",
    )
    .bind(turn)
    .bind(task)
    .bind("a".repeat(40))
    .bind(&marker)
    .bind(&body)
    .execute(database.pool())
    .await
    .expect("publication");
    publish_standalone_review(&database, &context, turn)
        .await
        .expect("native publication");
    publish_standalone_review(&database, &context, turn)
        .await
        .expect("idempotent readback");
    assert_eq!(posts.load(Ordering::SeqCst), 1);
    assert_eq!(reviews.lock().await[0]["body"], body);
    let state = standalone_review_task(&database, "acme/demo", 4)
        .await
        .expect("task state")
        .expect("task");
    assert_eq!(state["stage"], "complete");
    let confirmed: (String, i64) = sqlx::query_as(
        "SELECT status,review_id FROM tasks.standalone_review_publications WHERE turn_id=$1",
    )
    .bind(turn)
    .fetch_one(database.pool())
    .await
    .expect("publication result");
    assert_eq!(confirmed, ("confirmed".to_owned(), 300));
    server.abort();
    fs::remove_file(grant_path).expect("remove grant");
}
