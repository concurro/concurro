//! Approved candidate publication against a local GitHub App API.

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
use concurro_events_postgres::{Outbox, migrate as migrate_events};
use concurro_github_v1::AppClient;
use concurro_tasks::{
    GitPublicationContext, GitPublicationTarget, publish_pr_review, run_task_gates,
    run_task_git_publication,
};
use concurro_tasks_core::{
    Actor, Admission, AttemptSpec, DesignReviewer, InitializationSnapshot, IssueKey, PinnedInputs,
    Requirements, Stage, TaskState, design_binding, task_branch, validate_pr_review,
};
use concurro_tasks_postgres::{
    admit_pr_review_batch, admit_pr_review_correction, apply_pr_review_result,
    implementation_inputs, migrate, pr_review_correction, reserve_ready_intent, task, task_detail,
};
use concurro_testkit::PostgresContainer;
use serde_json::{Value, json};
use sqlx::types::Json as SqlJson;
use tokio::sync::Mutex;
use uuid::Uuid;

/// Lost commit and PR create responses preserve one exact commit body and one draft PR.
#[tokio::test]
async fn approved_candidate_publishes_one_branch_commit_and_draft_pr() {
    let issue = IssueKey {
        repository_id: 8,
        number: 3,
    };
    let task_id = Uuid::now_v7();
    let planner_turn = Uuid::now_v7();
    let implementer_turn = Uuid::now_v7();
    let approval_request = Uuid::now_v7();
    let parent = "b".repeat(40);
    let tree_sha = "c".repeat(40);
    let candidate_tree = "d".repeat(40);
    let candidate_commit = "e".repeat(40);
    let second_commit = "f".repeat(40);
    let second_tree = "1".repeat(40);
    let branch = task_branch("acme/demo", issue.number).expect("task branch");
    let head = Arc::new(Mutex::new(None::<String>));
    let pulls = Arc::new(Mutex::new(Vec::<Value>::new()));
    let ref_creates = Arc::new(AtomicUsize::new(0));
    let ref_advances = Arc::new(AtomicUsize::new(0));
    let commit_creates = Arc::new(AtomicUsize::new(0));
    let commit_bodies = Arc::new(Mutex::new(Vec::<Value>::new()));
    let pr_creates = Arc::new(AtomicUsize::new(0));
    let pr_readback_unavailable = Arc::new(AtomicBool::new(true));
    let pr_files_ready = Arc::new(AtomicBool::new(false));
    let reviews = Arc::new(Mutex::new(Vec::<Value>::new()));
    let review_creates = Arc::new(AtomicUsize::new(0));
    let review_retry_once = Arc::new(AtomicBool::new(true));
    let review_readback_unavailable = Arc::new(AtomicBool::new(true));
    let thread_readback_unavailable = Arc::new(AtomicBool::new(true));
    let submit_readback_unavailable = Arc::new(AtomicBool::new(true));
    let native_threads = Arc::new(Mutex::new(Vec::<Value>::new()));
    let thread_creates = Arc::new(AtomicUsize::new(0));
    let review_submits = Arc::new(AtomicUsize::new(0));
    let ready_calls = Arc::new(AtomicUsize::new(0));
    let ready_graphql_refusal = Arc::new(AtomicBool::new(false));
    let gate_check_pending = Arc::new(AtomicBool::new(false));
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
            "/repos/acme/demo/git/ref/heads/{*branch}",
            get({
                let head = head.clone();
                move |Path(_branch): Path<String>| {
                    let head = head.clone();
                    async move {
                        match head.lock().await.clone() {
                            Some(sha) => (StatusCode::OK, Json(json!({"object":{"sha":sha}}))),
                            None => (StatusCode::NOT_FOUND, Json(json!({"message":"missing"}))),
                        }
                    }
                }
            }),
        )
        .route(
            "/repos/acme/demo/git/refs",
            post({
                let head = head.clone();
                let ref_creates = ref_creates.clone();
                let parent = parent.clone();
                let branch = branch.clone();
                move |Json(body): Json<Value>| {
                    let head = head.clone();
                    let ref_creates = ref_creates.clone();
                    let parent = parent.clone();
                    let branch = branch.clone();
                    async move {
                        assert_eq!(body["ref"], format!("refs/heads/{branch}"));
                        assert_eq!(body["sha"], parent);
                        ref_creates.fetch_add(1, Ordering::SeqCst);
                        *head.lock().await = Some(parent.clone());
                        (
                            StatusCode::BAD_GATEWAY,
                            Json(json!({"message":"branch create response lost"})),
                        )
                    }
                }
            }),
        )
        .route(
            "/repos/acme/demo/git/refs/heads/{*branch}",
            axum::routing::patch({
                let head = head.clone();
                let ref_advances = ref_advances.clone();
                let candidate_commit = candidate_commit.clone();
                let second_commit = second_commit.clone();
                let pulls = pulls.clone();
                move |Path(_branch): Path<String>, Json(body): Json<Value>| {
                    let head = head.clone();
                    let ref_advances = ref_advances.clone();
                    let candidate_commit = candidate_commit.clone();
                    let second_commit = second_commit.clone();
                    let pulls = pulls.clone();
                    async move {
                        assert!(body["sha"] == candidate_commit || body["sha"] == second_commit);
                        assert_eq!(body["force"], false);
                        ref_advances.fetch_add(1, Ordering::SeqCst);
                        let next = body["sha"].as_str().expect("new head").to_owned();
                        *head.lock().await = Some(next.clone());
                        if let Some(pr) = pulls.lock().await.first_mut() {
                            pr["head"]["sha"] = json!(next);
                        }
                        (
                            StatusCode::BAD_GATEWAY,
                            Json(json!({"message":"ref advance response lost"})),
                        )
                    }
                }
            }),
        )
        .route(
            "/repos/acme/demo/git/commits/{sha}",
            get({
                let parent = parent.clone();
                let tree_sha = tree_sha.clone();
                let candidate_commit = candidate_commit.clone();
                let candidate_tree = candidate_tree.clone();
                move |Path(sha): Path<String>| {
                    let parent = parent.clone();
                    let tree_sha = tree_sha.clone();
                    let candidate_commit = candidate_commit.clone();
                    let candidate_tree = candidate_tree.clone();
                    async move {
                        let tree = if sha == parent { tree_sha } else {
                            assert_eq!(sha, candidate_commit);
                            candidate_tree
                        };
                        Json(json!({"sha":sha,"tree":{"sha":tree}}))
                    }
                }
            }),
        )
        .route(
            "/users/concurro[bot]",
            get(|| async { Json(json!({"id":314,"login":"concurro[bot]"})) }),
        )
        .route(
            "/graphql",
            post({
                let pulls = pulls.clone();
                let ready_calls = ready_calls.clone();
                let ready_graphql_refusal = ready_graphql_refusal.clone();
                let reviews = reviews.clone();
                let review_creates = review_creates.clone();
                let review_retry_once = review_retry_once.clone();
                let native_threads = native_threads.clone();
                let thread_creates = thread_creates.clone();
                let thread_readback_unavailable = thread_readback_unavailable.clone();
                let review_submits = review_submits.clone();
                let candidate_commit = candidate_commit.clone();
                let second_commit = second_commit.clone();
                move |Json(body): Json<Value>| {
                    let pulls = pulls.clone();
                    let ready_calls = ready_calls.clone();
                    let ready_graphql_refusal = ready_graphql_refusal.clone();
                    let reviews = reviews.clone();
                    let review_creates = review_creates.clone();
                    let review_retry_once = review_retry_once.clone();
                    let native_threads = native_threads.clone();
                    let thread_creates = thread_creates.clone();
                    let thread_readback_unavailable = thread_readback_unavailable.clone();
                    let review_submits = review_submits.clone();
                    let candidate_commit = candidate_commit.clone();
                    let second_commit = second_commit.clone();
                    async move {
                        let query = body["query"].as_str().expect("GraphQL query");
                        let input = &body["variables"]["input"];
                        if query.contains("reviewThreads") {
                            let nodes = if thread_readback_unavailable.load(Ordering::SeqCst) {
                                vec![]
                            } else {
                                native_threads.lock().await.clone()
                            };
                            return (
                                StatusCode::OK,
                                Json(json!({"data":{"repository":{
                                "pullRequest":{"reviewThreads":{"nodes":nodes,
                                    "pageInfo":{"hasNextPage":false,"endCursor":null}}}}}})),
                            );
                        }
                        if query.contains("addPullRequestReviewThread(") {
                            assert_eq!(input["pullRequestReviewId"], "REVIEW_91");
                            assert_eq!(input["path"], "src/lib.rs");
                            assert_eq!(input["line"], 1);
                            assert_eq!(input["subjectType"], "LINE");
                            thread_creates.fetch_add(1, Ordering::SeqCst);
                            native_threads.lock().await.push(json!({
                                "id":"THREAD_1","path":"src/lib.rs","line":1,"startLine":null,
                                "isResolved":false,
                                "subjectType":"LINE","comments":{"nodes":[{
                                    "id":"COMMENT_1","body":input["body"],"author":{"login":"concurro[bot]"},
                                    "pullRequestReview":{"id":"REVIEW_91"}
                                }],"pageInfo":{"hasNextPage":false,"endCursor":null}}
                            }));
                            return (StatusCode::BAD_GATEWAY, Json(json!({"message":"thread response lost"})));
                        }
                        if query.contains("addPullRequestReviewThreadReply(") {
                            assert_eq!(input["pullRequestReviewId"], "REVIEW_92");
                            assert_eq!(input["pullRequestReviewThreadId"], "THREAD_1");
                            native_threads.lock().await[0]["comments"]["nodes"]
                                .as_array_mut().expect("comments").push(json!({
                                    "id":"COMMENT_2","body":input["body"],
                                    "author":{"login":"concurro[bot]"},
                                    "pullRequestReview":{"id":"REVIEW_92"}
                                }));
                            return (StatusCode::BAD_GATEWAY, Json(json!({"message":"reply response lost"})));
                        }
                        if query.contains("resolveReviewThread(") {
                            assert_eq!(input["threadId"], "THREAD_1");
                            native_threads.lock().await[0]["isResolved"] = json!(true);
                            native_threads.lock().await[0]["resolvedBy"] = json!({"login":"concurro[bot]"});
                            return (StatusCode::BAD_GATEWAY, Json(json!({"message":"resolve response lost"})));
                        }
                        if query.contains("node(id:") {
                            return (StatusCode::OK, Json(json!({"data":{"node":native_threads.lock().await[0]}})));
                        }
                        if query.contains("addPullRequestReview(") {
                            assert_eq!(input["pullRequestId"], "PR_19");
                            assert!(input["commitOID"] == candidate_commit || input["commitOID"] == second_commit);
                            let created = review_creates.fetch_add(1, Ordering::SeqCst);
                            if review_retry_once.swap(false, Ordering::SeqCst) {
                                return (StatusCode::TOO_MANY_REQUESTS, Json(json!({"message":"rate limited"})));
                            }
                            let id = 90 + created;
                            reviews.lock().await.push(json!({
                                "id":id,"node_id":format!("REVIEW_{id}"),"state":"PENDING",
                                "commit_id":input["commitOID"],"body":input["body"],
                                "user":{"id":314,"login":"concurro[bot]"}
                            }));
                            return (StatusCode::BAD_GATEWAY, Json(json!({"message":"pending review response lost"})));
                        }
                        if query.contains("submitPullRequestReview(") {
                            let id = input["pullRequestReviewId"].as_str().expect("review ID");
                            assert!(id == "REVIEW_91" || id == "REVIEW_92");
                            assert_eq!(input["event"], "COMMENT");
                            review_submits.fetch_add(1, Ordering::SeqCst);
                            let mut stored = reviews.lock().await;
                            let review = stored.iter_mut().find(|review| review["node_id"] == id).expect("pending review");
                            review["body"] = input["body"].clone();
                            review["state"] = json!("COMMENTED");
                            return (StatusCode::BAD_GATEWAY, Json(json!({"message":"submit response lost"})));
                        }
                        assert!(query.contains("markPullRequestReadyForReview"));
                        assert_eq!(body["variables"]["input"]["pullRequestId"], "PR_19");
                        ready_calls.fetch_add(1, Ordering::SeqCst);
                        if ready_graphql_refusal.load(Ordering::SeqCst) {
                            return (
                                StatusCode::OK,
                                Json(json!({"data":{"markPullRequestReadyForReview":null},
                                    "errors":[{"type":"FORBIDDEN","message":"denied"}]})),
                            );
                        }
                        pulls.lock().await[0]["draft"] = json!(false);
                        (
                            StatusCode::BAD_GATEWAY,
                            Json(json!({"message":"mark-ready response lost"})),
                        )
                    }
                }
            }),
        )
        .route(
            "/repos/acme/demo/git/trees/{sha}",
            get({
                let parent = parent.clone();
                let candidate_commit = candidate_commit.clone();
                move |Path(sha): Path<String>| {
                    let parent = parent.clone();
                    let candidate_commit = candidate_commit.clone();
                    async move {
                        assert!(sha == parent || sha == candidate_commit);
                        Json(json!({"truncated":false,"tree":[
                            {"path":"src/lib.rs","type":"blob","mode":"100644","sha":"a".repeat(40)}
                        ]}))
                    }
                }
            }),
        )
        .route(
            "/repos/acme/demo/git/trees",
            post({
                let candidate_tree = candidate_tree.clone();
                let tree_sha = tree_sha.clone();
                let second_tree = second_tree.clone();
                move |Json(body): Json<Value>| {
                    let candidate_tree = candidate_tree.clone();
                    let tree_sha = tree_sha.clone();
                    let second_tree = second_tree.clone();
                    async move {
                        assert!(body["base_tree"] == tree_sha || body["base_tree"] == candidate_tree);
                        assert_eq!(body["tree"][0]["path"], "src/lib.rs");
                        if body["base_tree"] == tree_sha {
                            assert_eq!(body["tree"][0]["content"], "fn changed() {}\n");
                            Json(json!({"sha":candidate_tree}))
                        } else {
                            assert_eq!(body["tree"][0]["content"], "fn changed() { assert!(true); }\n");
                            Json(json!({"sha":second_tree}))
                        }
                    }
                }
            }),
        )
        .route(
            "/repos/acme/demo/git/commits",
            post({
                let commit_creates = commit_creates.clone();
                let commit_bodies = commit_bodies.clone();
                let candidate_commit = candidate_commit.clone();
                let candidate_tree = candidate_tree.clone();
                let parent = parent.clone();
                let second_commit = second_commit.clone();
                let second_tree = second_tree.clone();
                move |Json(body): Json<Value>| {
                    let commit_creates = commit_creates.clone();
                    let commit_bodies = commit_bodies.clone();
                    let candidate_commit = candidate_commit.clone();
                    let candidate_tree = candidate_tree.clone();
                    let parent = parent.clone();
                    let second_commit = second_commit.clone();
                    let second_tree = second_tree.clone();
                    async move {
                        let first = body["parents"] == json!([parent]);
                        assert!(first || body["parents"] == json!([candidate_commit]));
                        assert_eq!(
                            body["tree"].as_str(),
                            Some(if first { candidate_tree.as_str() } else { second_tree.as_str() })
                        );
                        assert_eq!(body["author"], body["committer"]);
                        assert_eq!(
                            body["author"]["email"],
                            "314+concurro[bot]@users.noreply.github.com"
                        );
                        let date = body["author"]["date"].as_str().expect("commit date");
                        assert_eq!(date.len(), 20);
                        assert!(date.ends_with('Z'));
                        commit_bodies.lock().await.push(body);
                        let sent = commit_creates.fetch_add(1, Ordering::SeqCst);
                        if sent == 0 {
                            (
                                StatusCode::BAD_GATEWAY,
                                Json(json!({"message":"commit response lost"})),
                            )
                        } else {
                            (StatusCode::OK, Json(json!({"sha":if first { candidate_commit } else { second_commit }})))
                        }
                    }
                }
            }),
        )
        .route(
            "/repos/acme/demo/pulls",
            get({
                let pulls = pulls.clone();
                let pr_readback_unavailable = pr_readback_unavailable.clone();
                move || {
                    let pulls = pulls.clone();
                    let pr_readback_unavailable = pr_readback_unavailable.clone();
                    async move {
                        let current = pulls.lock().await.clone();
                        if !current.is_empty() && pr_readback_unavailable.load(Ordering::SeqCst) {
                            (
                                StatusCode::SERVICE_UNAVAILABLE,
                                Json(json!({"message":"readback unavailable"})),
                            )
                        } else {
                            (StatusCode::OK, Json(json!(current)))
                        }
                    }
                }
            })
            .post({
                let pulls = pulls.clone();
                let pr_creates = pr_creates.clone();
                let branch = branch.clone();
                let candidate_commit = candidate_commit.clone();
                let parent = parent.clone();
                move |Json(body): Json<Value>| {
                    let pulls = pulls.clone();
                    let pr_creates = pr_creates.clone();
                    let branch = branch.clone();
                    let candidate_commit = candidate_commit.clone();
                    let parent = parent.clone();
                    async move {
                        assert_eq!(body["head"], format!("acme:{branch}"));
                        assert_eq!(body["base"], "main");
                        assert_eq!(body["draft"], true);
                        let pr = json!({
                            "number":19,"state":"open","draft":true,
                            "user":{"login":"concurro[bot]"},
                            "head":{"sha":candidate_commit,"ref":branch,
                                "repo":{"full_name":"acme/demo"}},
                            "base":{"ref":"main","sha":parent},
                            "merge_commit_sha":null,"merged":false,"node_id":"PR_19",
                            "title":body["title"],"body":body["body"]
                        });
                        pulls.lock().await.push(pr);
                        pr_creates.fetch_add(1, Ordering::SeqCst);
                        (
                            StatusCode::BAD_GATEWAY,
                            Json(json!({"message":"lost response"})),
                        )
                    }
                }
            }),
        )
        .route(
            "/repos/acme/demo/pulls/19",
            get({
                let pulls = pulls.clone();
                move || {
                    let pulls = pulls.clone();
                    async move { Json(pulls.lock().await.first().cloned().unwrap_or(Value::Null)) }
                }
            })
            .patch({
                let pulls = pulls.clone();
                move |Json(body): Json<Value>| {
                    let pulls = pulls.clone();
                    async move {
                        let mut prs = pulls.lock().await;
                        prs[0]["title"] = body["title"].clone();
                        prs[0]["body"] = body["body"].clone();
                        (StatusCode::BAD_GATEWAY, Json(json!({"message":"PR edit response lost"})))
                    }
                }
            }),
        )
        .route(
            "/repos/acme/demo/pulls/19/files",
            get({
                let pr_files_ready = pr_files_ready.clone();
                move || {
                    let pr_files_ready = pr_files_ready.clone();
                    async move {
                        if pr_files_ready.load(Ordering::SeqCst) {
                            Json(json!([{"filename":"src/lib.rs","status":"modified"}]))
                        } else {
                            Json(json!([]))
                        }
                    }
                }
            }),
        )
        .route(
            "/repos/acme/demo/pulls/19/reviews",
            get({
                let reviews = reviews.clone();
                let unavailable = review_readback_unavailable.clone();
                let submit_unavailable = submit_readback_unavailable.clone();
                move || {
                    let reviews = reviews.clone();
                    let unavailable = unavailable.clone();
                    let submit_unavailable = submit_unavailable.clone();
                    async move {
                        if unavailable.load(Ordering::SeqCst)
                            || (submit_unavailable.load(Ordering::SeqCst)
                                && reviews.lock().await.iter().any(|review| review["state"] == "COMMENTED")) {
                            Json(json!([]))
                        } else {
                            Json(json!(reviews.lock().await.clone()))
                        }
                    }
                }
            })
            .post({
                let reviews = reviews.clone();
                let review_creates = review_creates.clone();
                let review_retry_once = review_retry_once.clone();
                let candidate_commit = candidate_commit.clone();
                move |Json(body): Json<Value>| {
                    let reviews = reviews.clone();
                    let review_creates = review_creates.clone();
                    let review_retry_once = review_retry_once.clone();
                    let candidate_commit = candidate_commit.clone();
                    async move {
                        assert_eq!(body["commit_id"], candidate_commit);
                        assert_eq!(body["event"], "COMMENT");
                        assert!(
                            body["body"]
                                .as_str()
                                .is_some_and(|text| text.starts_with("<!-- concurro:pr-review:"))
                        );
                        review_creates.fetch_add(1, Ordering::SeqCst);
                        if review_retry_once.swap(false, Ordering::SeqCst) {
                            return (
                                StatusCode::TOO_MANY_REQUESTS,
                                Json(json!({"message":"review rate limited"})),
                            );
                        }
                        reviews.lock().await.push(json!({
                            "id":91,"state":"COMMENTED","commit_id":candidate_commit,
                            "body":body["body"],
                            "user":{"id":314,"login":"concurro[bot]"}
                        }));
                        (
                            StatusCode::BAD_GATEWAY,
                            Json(json!({"message":"lost review response"})),
                        )
                    }
                }
            }),
        )
        .route(
            "/repos/acme/demo/commits/{sha}/check-runs",
            get({
                let candidate_commit = candidate_commit.clone();
                let gate_check_pending = gate_check_pending.clone();
                move |Path(sha): Path<String>| {
                    let candidate_commit = candidate_commit.clone();
                    let gate_check_pending = gate_check_pending.clone();
                    async move {
                        assert_eq!(sha, candidate_commit);
                        let pending = gate_check_pending.load(Ordering::SeqCst);
                        Json(json!({"check_runs":[{
                            "id":8,"name":"build","head_sha":sha,
                            "status":if pending { "in_progress" } else { "completed" },
                            "conclusion":if pending { Value::Null } else { json!("success") },
                            "check_suite":{"id":4},"app":{"id":7}
                        }]}))
                    }
                }
            }),
        )
        .route(
            "/repos/acme/demo/commits/{sha}/statuses",
            get(|| async { Json(json!([])) }),
        )
        .route(
            "/repos/acme/demo/commits/{sha}/check-suites",
            get({
                let candidate_commit = candidate_commit.clone();
                move |Path(sha): Path<String>| {
                    let candidate_commit = candidate_commit.clone();
                    async move {
                        assert_eq!(sha, candidate_commit);
                        Json(json!({"check_suites":[{
                            "id":4,"head_sha":sha,"status":"completed","conclusion":"success"
                        }]}))
                    }
                }
            }),
        )
        .route(
            "/repos/acme/demo/actions/runs",
            get({
                let candidate_commit = candidate_commit.clone();
                move || {
                    let candidate_commit = candidate_commit.clone();
                    async move {
                        Json(json!({"workflow_runs":[{
                            "id":10,"workflow_id":9,"event":"pull_request","head_branch":"topic",
                            "check_suite_id":4,"head_sha":candidate_commit,
                            "name":"CI","status":"completed","conclusion":"success"
                        }]}))
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
    .expect("GitHub client");

    let postgres = PostgresContainer::start().await.expect("Postgres");
    let database = Database::connect(postgres.url(), 3)
        .await
        .expect("database");
    migrate(&database).await.expect("tasks schema");
    migrate_events(&database).await.expect("events schema");
    let pins = PinnedInputs {
        config_sha: "a".repeat(40),
        base_sha: parent.clone(),
        max_model_calls: 10,
        max_fix_rounds: 2,
        max_plan_revisions: 2,
    };
    let configuration = json!({
        "repository":"acme/demo","base":"main","planner":"codex",
        "implementer":"codex",
        "qc":"codex","reviewers":["codex"],"max_review_repairs":1,
        "policy_documents":[],
        "models":{"codex":"gpt"},"paths":{"protected":[],"protected_extensions":[],
        "protected_names":[],"protected_name_prefixes":[],"protected_prefixes":[],
        "protected_files":[],"generated":[]}
    });
    let snapshot = InitializationSnapshot {
        pins: pins.clone(),
        configuration: configuration.clone(),
        requirements: Requirements {
            title: "Task".to_owned(),
            body: Some("Change function".to_owned()),
            discussion: vec![],
        },
        charter: r#"{"goals":{"G1":"Correctness"}}"#.to_owned(),
        instruction_texts: BTreeMap::new(),
        policy_documents: BTreeMap::new(),
        base_files: vec!["src/lib.rs".to_owned()],
        bundle_id: "bundle".to_owned(),
    };
    let state = TaskState {
        id: concurro_contracts::TaskId(task_id),
        issue,
        author: Actor {
            id: 7,
            login: "writer".to_owned(),
        },
        stage: Stage::Publish,
        revision: 7,
        stage_reason: None,
        retry_stage: None,
        pins: Some(pins),
        used_model_calls: 4,
        current_attempt: None,
    };
    let plan = json!({"summary":"Change function","headline":"Change the function","files":["src/lib.rs"],"responses":[]});
    let plan_id = "plan-id";
    let roster: Vec<DesignReviewer> =
        concurro_tasks_core::design_roster(&configuration).expect("roster");
    let binding = design_binding(
        &plan,
        plan_id,
        &snapshot,
        &roster,
        0,
        "cooperative",
        None,
        &[],
    )
    .expect("design binding");
    sqlx::query("INSERT INTO tasks.issue_controls (repository_id,issue_number) VALUES ($1,$2)")
        .bind(issue.repository_id)
        .bind(issue.number)
        .execute(database.pool())
        .await
        .expect("issue");
    sqlx::query("INSERT INTO tasks.task_state (repository_id,issue_number,task_id,revision,state) VALUES ($1,$2,$3,7,$4)")
        .bind(issue.repository_id).bind(issue.number).bind(task_id).bind(SqlJson(&state))
        .execute(database.pool()).await.expect("task");
    sqlx::query(
        "INSERT INTO tasks.initialization_inputs (task_id,task_revision,snapshot) VALUES ($1,1,$2)",
    )
    .bind(task_id)
    .bind(SqlJson(&snapshot))
    .execute(database.pool())
    .await
    .expect("snapshot");
    for (turn, operation) in [(planner_turn, "plan"), (implementer_turn, "develop")] {
        sqlx::query(
            "INSERT INTO tasks.attempts \
            (id,task_id,operation,expected_revision,staffing_group,fork,config_sha,submit_body, \
             initial_deadline,unbounded_running,status) \
            VALUES ($1,$2,$3,1,$4,'acme/demo',$5,$6,now(),false,'finished')",
        )
        .bind(turn)
        .bind(task_id)
        .bind(operation)
        .bind(format!("fixture:{turn}"))
        .bind("a".repeat(40))
        .bind(b"fixture".as_slice())
        .execute(database.pool())
        .await
        .expect("attempt");
    }
    sqlx::query("INSERT INTO tasks.plan_reports \
        (attempt_id,task_id,initialization_revision,raw_report,raw_report_bytes,accepted,current,plan_id) \
        VALUES ($1,$2,1,$3,$4,true,true,$5)")
        .bind(planner_turn).bind(task_id).bind(SqlJson(&plan))
        .bind(serde_json::to_vec(&plan).expect("plan bytes")).bind(plan_id)
        .execute(database.pool()).await.expect("plan");
    let batch = Uuid::now_v7();
    sqlx::query(
        "INSERT INTO tasks.plan_review_batches \
        (id,task_id,plan_id,generation,phase,binding,roster,status) \
        VALUES ($1,$2,$3,0,'cooperative',$4,$5,'settled')",
    )
    .bind(batch)
    .bind(task_id)
    .bind(plan_id)
    .bind(&binding)
    .bind(SqlJson(&roster))
    .execute(database.pool())
    .await
    .expect("review batch");
    for reviewer in &roster {
        sqlx::query(
            "INSERT INTO tasks.plan_review_seats \
            (batch_id,reviewer_id,identity,status,first_pass_eligible,report) \
            VALUES ($1,$2,$3,'accepted',true,$4)",
        )
        .bind(batch)
        .bind(&reviewer.id)
        .bind(SqlJson(reviewer))
        .bind(SqlJson(json!({"decision":"approve","risk":"low"})))
        .execute(database.pool())
        .await
        .expect("review seat");
    }
    sqlx::query("INSERT INTO tasks.requests \
        (id,repository_id,issue_number,channel,source,source_at,source_id,source_index,request_class,request,status) \
        VALUES ($1,$2,$3,'comment','approval',now(),1,0,1,$4,'applied')")
        .bind(approval_request).bind(issue.repository_id).bind(issue.number)
        .bind(SqlJson(json!({"kind":"approve"}))).execute(database.pool()).await.expect("approval request");
    sqlx::query(
        "INSERT INTO tasks.plan_approvals (request_id,task_id,plan_id,binding,actor) \
        VALUES ($1,$2,$3,$4,$5)",
    )
    .bind(approval_request)
    .bind(task_id)
    .bind(plan_id)
    .bind(&binding)
    .bind(SqlJson(json!({"id":7,"login":"writer"})))
    .execute(database.pool())
    .await
    .expect("approval");
    let candidate = json!({
        "plan_id":plan_id,"binding":binding,"parent":parent,
        "edits":[{"path":"src/lib.rs","content":"fn changed() {}\n"}],
        "description":{"lead":"Change the function.","problem":["Old behavior."],
            "changes":["New behavior."],"compatibility":[],"review_focus":[]},
        "commit":{"subject":"Change function","type":"fix","changes":["Update code"]}
    });
    sqlx::query(
        "INSERT INTO tasks.implementation_reports \
        (attempt_id,task_id,raw_result,status,candidate) VALUES ($1,$2,$3,'accepted',$4)",
    )
    .bind(implementer_turn)
    .bind(task_id)
    .bind(b"fixture".as_slice())
    .bind(SqlJson(&candidate))
    .execute(database.pool())
    .await
    .expect("candidate");
    let permissions = BTreeMap::from([("contents".to_owned(), "read".to_owned())]);
    let context = GitPublicationContext {
        github,
        targets: HashMap::from([(
            issue.repository_id,
            GitPublicationTarget {
                issue_repository: "acme/demo".to_owned(),
                fork: "acme/demo".to_owned(),
                upstream: "acme/demo".to_owned(),
                fork_read_permissions: permissions.clone(),
                fork_write_permissions: BTreeMap::from([(
                    "contents".to_owned(),
                    "write".to_owned(),
                )]),
                upstream_read_permissions: permissions,
                upstream_write_permissions: BTreeMap::from([(
                    "pull_requests".to_owned(),
                    "write".to_owned(),
                )]),
                bot_login: "concurro[bot]".to_owned(),
            },
        )]),
        outbox: Outbox::new(1024 * 1024, vec!["task.>".to_owned(), "turn.>".to_owned()]),
    };
    assert!(
        run_task_git_publication(&database, &context, issue)
            .await
            .is_err()
    );
    assert_eq!(commit_creates.load(Ordering::SeqCst), 1);
    assert_eq!(pr_creates.load(Ordering::SeqCst), 0);
    assert!(
        run_task_git_publication(&database, &context, issue)
            .await
            .is_err()
    );
    assert_eq!(
        task(&database, issue)
            .await
            .expect("task")
            .expect("pending PR")
            .stage,
        Stage::Publish
    );
    let pending = task_detail(&database, issue)
        .await
        .expect("pending detail")
        .expect("pending task detail");
    assert_eq!(pending["git_publication"]["intent"]["pr_create_sent"], true);
    assert_eq!(ref_creates.load(Ordering::SeqCst), 1);
    assert_eq!(ref_advances.load(Ordering::SeqCst), 1);
    assert_eq!(commit_creates.load(Ordering::SeqCst), 2);
    assert_eq!(pr_creates.load(Ordering::SeqCst), 1);
    let bodies = commit_bodies.lock().await;
    assert_eq!(bodies.len(), 2);
    assert_eq!(bodies[0], bodies[1]);
    drop(bodies);
    drop(database);
    pr_readback_unavailable.store(false, Ordering::SeqCst);
    let restarted = Database::connect(postgres.url(), 3)
        .await
        .expect("restart database");
    assert!(
        run_task_git_publication(&restarted, &context, issue)
            .await
            .is_err()
    );
    assert_eq!(
        task(&restarted, issue)
            .await
            .expect("task")
            .expect("pending PR files")
            .stage,
        Stage::Publish
    );
    pr_files_ready.store(true, Ordering::SeqCst);
    run_task_git_publication(&restarted, &context, issue)
        .await
        .expect("read back draft PR and files after restart");
    assert_eq!(
        task(&restarted, issue)
            .await
            .expect("task")
            .expect("published")
            .stage,
        Stage::Review
    );
    let detail = task_detail(&restarted, issue)
        .await
        .expect("detail")
        .expect("task detail");
    assert_eq!(detail["git_publication"]["intent"]["pr_number"], 19);
    assert_eq!(detail["pr_reviews"][0]["status"], "recorded");
    let review: (String, String, i64, Vec<String>) = sqlx::query_as(
        "SELECT b.head,b.plan_id,b.pr_number,ARRAY(SELECT jsonb_array_elements_text(b.changed_paths)) \
         FROM tasks.pr_review_batches b WHERE b.task_id=$1",
    )
    .bind(task_id)
    .fetch_one(restarted.pool())
    .await
    .expect("recorded PR review batch");
    assert_eq!(review.0, candidate_commit);
    assert_eq!(review.1, plan_id);
    assert_eq!(review.2, 19);
    assert_eq!(review.3, vec!["src/lib.rs"]);
    let seats: Vec<(String, String)> = sqlx::query_as(
        "SELECT reviewer_id,binding FROM tasks.pr_review_seats WHERE batch_id= \
         (SELECT id FROM tasks.pr_review_batches WHERE task_id=$1)",
    )
    .bind(task_id)
    .fetch_all(restarted.pool())
    .await
    .expect("reviewer seat");
    assert_eq!(seats.len(), 1);
    assert!(!seats[0].1.is_empty());
    run_task_git_publication(&restarted, &context, issue)
        .await
        .expect("settled replay");
    assert_eq!(ref_creates.load(Ordering::SeqCst), 1);
    assert_eq!(ref_advances.load(Ordering::SeqCst), 1);
    assert_eq!(commit_creates.load(Ordering::SeqCst), 2);
    assert_eq!(pr_creates.load(Ordering::SeqCst), 1);
    let notices: Vec<String> = sqlx::query_scalar(
        "SELECT body FROM tasks.publication_comments WHERE task_id=$1 AND kind='notice'",
    )
    .bind(task_id)
    .fetch_all(restarted.pool())
    .await
    .expect("branch notice");
    assert_eq!(notices.len(), 1);
    assert!(notices[0].contains("Implementation started"));
    assert!(notices[0].contains(&branch));
    let first_turn = Uuid::now_v7();
    let report = json!({
        "verdict":"changes_requested","summary":"One issue remains",
        "findings":[{"path":"src/lib.rs","start_line":1,"line":1,
            "detail":"The changed function needs another check.",
            "severity":"medium","title":"Check behavior"}],
        "dispositions":[],"design_issue":null
    });
    let review_batch: Uuid =
        sqlx::query_scalar("SELECT id FROM tasks.pr_review_batches WHERE task_id=$1")
            .bind(task_id)
            .fetch_one(restarted.pool())
            .await
            .expect("review batch ID");
    let review_state = task(&restarted, issue)
        .await
        .expect("review task")
        .expect("task");
    let submission = json!({
        "turn":first_turn,"expected":{},"models":["gpt"],"budget":1,"lease":1,
        "payload":{"request":{"session":null,"context":{
            "pr_review_binding":seats[0].1,
            "reviewer_identity":{"id":seats[0].0},
            "head":candidate_commit
        }}},
        "pin":null,"binding":{}
    });
    let attempt = AttemptSpec {
        id: first_turn,
        task: review_state.id,
        expected_revision: review_state.revision,
        staffing_group: format!("fixture:{first_turn}"),
        fork: "acme/demo".to_owned(),
        config_sha: "a".repeat(40),
        submit_body: serde_json::to_vec(&submission).expect("submission"),
        prompt_body: Some(b"{}".to_vec()),
        deadline: Utc::now() + Duration::minutes(30),
        unbounded_running: false,
    };
    let mut admission = restarted.begin().await.expect("review admission");
    assert_eq!(
        admit_pr_review_batch(
            &mut admission,
            &context.outbox,
            issue,
            review_batch,
            review_state.revision,
            &[(seats[0].0.clone(), attempt)],
        )
        .await
        .expect("admit reviewer roster"),
        Admission::Admitted,
    );
    admission.commit().await.expect("commit admission");
    sqlx::query(
        "UPDATE tasks.attempts SET status='finished',observation=$2,result=$3,result_raw=$4 \
         WHERE id=$1",
    )
    .bind(first_turn)
    .bind(SqlJson(json!({})))
    .bind(SqlJson(json!({"outcome":"ok","response":report})))
    .bind(serde_json::to_vec(&json!({"outcome":"ok","response":report})).expect("result"))
    .execute(restarted.pool())
    .await
    .expect("reviewer attempt");
    sqlx::query(
        "UPDATE tasks.pr_review_seats SET status='completed',report=$2,report_raw=$3 \
         WHERE attempt_id=$1",
    )
    .bind(first_turn)
    .bind(SqlJson(&report))
    .bind(serde_json::to_vec(&report).expect("report"))
    .execute(restarted.pool())
    .await
    .expect("completed reviewer seat");
    let mut invalid = restarted.begin().await.expect("invalid review transaction");
    assert!(
        apply_pr_review_result(
            &mut invalid,
            &context.outbox,
            first_turn,
            Err("review output omitted required evidence".to_owned()),
        )
        .await
        .expect("schedule private repair")
    );
    invalid.commit().await.expect("commit private repair");
    let correction = pr_review_correction(&restarted, review_batch, &seats[0].0, 1)
        .await
        .expect("read correction")
        .expect("private correction");
    assert!(!correction.retry);
    assert_eq!(correction.count, 1);
    assert_eq!(correction.batch.head, candidate_commit);
    let reviewer_turn = Uuid::now_v7();
    let correction_state = task(&restarted, issue)
        .await
        .expect("review state")
        .expect("task");
    let mut correction_submission = submission.clone();
    correction_submission["turn"] = json!(reviewer_turn);
    correction_submission["payload"]["request"]["context"]["review_validation_feedback"] =
        json!({"reason":correction.reason});
    let correction_attempt = AttemptSpec {
        id: reviewer_turn,
        task: correction_state.id,
        expected_revision: correction_state.revision,
        staffing_group: format!("fixture:{reviewer_turn}"),
        fork: "acme/demo".to_owned(),
        config_sha: "a".repeat(40),
        submit_body: serde_json::to_vec(&correction_submission).expect("correction submission"),
        prompt_body: Some(b"{}".to_vec()),
        deadline: Utc::now() + Duration::minutes(30),
        unbounded_running: false,
    };
    let sibling_turn = Uuid::now_v7();
    let mut sibling_submission = correction_submission.clone();
    sibling_submission["turn"] = json!(sibling_turn);
    sibling_submission["payload"]["request"]["context"]["pr_review_binding"] =
        json!("sibling-binding");
    sibling_submission["payload"]["request"]["context"]["reviewer_identity"]["id"] =
        json!("sibling");
    let sibling_attempt = AttemptSpec {
        id: sibling_turn,
        task: correction_state.id,
        expected_revision: correction_state.revision,
        staffing_group: format!("fixture:{sibling_turn}"),
        fork: "acme/demo".to_owned(),
        config_sha: "a".repeat(40),
        submit_body: serde_json::to_vec(&sibling_submission).expect("sibling submission"),
        prompt_body: Some(b"{}".to_vec()),
        deadline: Utc::now() + Duration::minutes(30),
        unbounded_running: false,
    };
    sqlx::query(
        "INSERT INTO tasks.pr_review_seats \
         (batch_id,reviewer_id,identity,binding,status,correction_count) \
         VALUES ($1,'sibling',$2,'sibling-binding','correcting',1)",
    )
    .bind(review_batch)
    .bind(SqlJson(json!({"id":"sibling"})))
    .execute(restarted.pool())
    .await
    .expect("second correcting seat");
    let mut work = restarted.begin().await.expect("correction admission");
    assert_eq!(
        admit_pr_review_correction(
            &mut work,
            &context.outbox,
            issue,
            review_batch,
            &seats[0].0,
            1,
            &correction_attempt,
        )
        .await
        .expect("admit correction"),
        Admission::Admitted
    );
    work.commit().await.expect("commit correction admission");
    let mut sibling_work = restarted
        .begin()
        .await
        .expect("sibling correction admission");
    assert_eq!(
        admit_pr_review_correction(
            &mut sibling_work,
            &context.outbox,
            issue,
            review_batch,
            "sibling",
            1,
            &sibling_attempt,
        )
        .await
        .expect("admit sibling after task revision advanced"),
        Admission::Admitted
    );
    sibling_work
        .commit()
        .await
        .expect("commit sibling correction");
    sqlx::query("DELETE FROM tasks.pr_review_seats WHERE batch_id=$1 AND reviewer_id='sibling'")
        .bind(review_batch)
        .execute(restarted.pool())
        .await
        .expect("remove synthetic sibling from single-reviewer fixture");
    sqlx::query(
        "UPDATE tasks.attempts SET status='finished',observation=$2,result=$3,result_raw=$4 WHERE id=$1",
    )
    .bind(reviewer_turn)
    .bind(SqlJson(json!({})))
    .bind(SqlJson(json!({"outcome":"ok","response":report})))
    .bind(serde_json::to_vec(&json!({"outcome":"ok","response":report})).expect("result"))
    .execute(restarted.pool())
    .await
    .expect("corrected attempt");
    sqlx::query(
        "UPDATE tasks.pr_review_seats SET status='completed',report=$2,report_raw=$3 WHERE attempt_id=$1",
    )
    .bind(reviewer_turn)
    .bind(SqlJson(&report))
    .bind(serde_json::to_vec(&report).expect("report"))
    .execute(restarted.pool())
    .await
    .expect("corrected seat");
    let mut work = restarted.begin().await.expect("review transaction");
    assert!(
        apply_pr_review_result(
            &mut work,
            &context.outbox,
            reviewer_turn,
            validate_pr_review(report, &[], &[]),
        )
        .await
        .expect("apply reviewer result")
    );
    work.commit().await.expect("commit review");
    let finding: (String, String, String) = sqlx::query_as(
        "SELECT id,reviewer_id,status FROM tasks.pr_review_findings WHERE task_id=$1",
    )
    .bind(task_id)
    .fetch_one(restarted.pool())
    .await
    .expect("owned finding");
    assert_eq!(finding.0, "F1");
    assert_eq!(finding.1, seats[0].0);
    assert_eq!(finding.2, "open");
    assert!(
        publish_pr_review(&restarted, &context, reviewer_turn)
            .await
            .is_err()
    );
    assert_eq!(review_creates.load(Ordering::SeqCst), 1);
    let refused_status: String =
        sqlx::query_scalar("SELECT status FROM tasks.pr_review_publications WHERE turn_id=$1")
            .bind(reviewer_turn)
            .fetch_one(restarted.pool())
            .await
            .expect("retryable review status");
    assert_eq!(refused_status, "sent");
    assert!(
        publish_pr_review(&restarted, &context, reviewer_turn)
            .await
            .is_err()
    );
    assert_eq!(review_creates.load(Ordering::SeqCst), 2);
    review_readback_unavailable.store(false, Ordering::SeqCst);
    assert!(
        publish_pr_review(&restarted, &context, reviewer_turn)
            .await
            .is_err()
    );
    assert_eq!(thread_creates.load(Ordering::SeqCst), 1);
    thread_readback_unavailable.store(false, Ordering::SeqCst);
    let thread_error = publish_pr_review(&restarted, &context, reviewer_turn)
        .await
        .expect_err("lost submit response");
    assert_eq!(review_submits.load(Ordering::SeqCst), 1, "{thread_error:#}");
    submit_readback_unavailable.store(false, Ordering::SeqCst);
    publish_pr_review(&restarted, &context, reviewer_turn)
        .await
        .expect("exact review and thread readback");
    publish_pr_review(&restarted, &context, reviewer_turn)
        .await
        .expect("confirmed review replay");
    assert_eq!(review_creates.load(Ordering::SeqCst), 2);
    assert_eq!(thread_creates.load(Ordering::SeqCst), 1);
    assert_eq!(review_submits.load(Ordering::SeqCst), 1);
    let confirmed: (String, i64) = sqlx::query_as(
        "SELECT status,review_id FROM tasks.pr_review_publications WHERE turn_id=$1",
    )
    .bind(reviewer_turn)
    .fetch_one(restarted.pool())
    .await
    .expect("confirmed native review");
    assert_eq!(confirmed, ("confirmed".to_owned(), 91));
    let detail = task_detail(&restarted, issue)
        .await
        .expect("review detail")
        .expect("task");
    assert_eq!(detail["review_findings"][0]["id"], "F1");
    assert_eq!(detail["review_publications"][0]["status"], "confirmed");
    let action_statuses: Vec<String> = sqlx::query_scalar(
        "SELECT status FROM tasks.pr_review_actions WHERE turn_id=$1 ORDER BY sequence",
    )
    .bind(reviewer_turn)
    .fetch_all(restarted.pool())
    .await
    .expect("native review actions");
    assert_eq!(action_statuses, vec!["confirmed"; 3]);
    assert_eq!(
        task(&restarted, issue)
            .await
            .expect("fix task")
            .expect("task")
            .stage,
        Stage::Implement
    );
    let fix_inputs = implementation_inputs(&restarted, task_id)
        .await
        .expect("fix inputs")
        .expect("approved fix inputs");
    assert_eq!(fix_inputs.parent, candidate_commit);
    assert_eq!(fix_inputs.branch.as_deref(), Some(branch.as_str()));
    assert_eq!(
        fix_inputs.prior_reviews[&seats[0].0]["verdict"],
        "changes_requested"
    );
    assert_eq!(fix_inputs.review_findings[0]["id"], "F1");
    let replay = restarted.begin().await.expect("replay transaction");
    let mut replay = replay;
    assert!(
        !apply_pr_review_result(
            &mut replay,
            &context.outbox,
            reviewer_turn,
            validate_pr_review(
                json!({"verdict":"approve","summary":"Done","findings":[],
            "dispositions":[],"design_issue":null}),
                &[],
                &[]
            ),
        )
        .await
        .expect("replay result")
    );
    replay.commit().await.expect("commit replay");
    sqlx::query("UPDATE tasks.pr_review_findings SET status='fixed' WHERE task_id=$1")
        .bind(task_id)
        .execute(restarted.pool())
        .await
        .expect("fixture resolved finding");
    sqlx::query("UPDATE tasks.pr_review_seats SET report=$2 WHERE attempt_id=$1")
        .bind(reviewer_turn)
        .bind(SqlJson(
            json!({"verdict":"approve","summary":"All clear","findings":[],
            "dispositions":[],"design_issue":null}),
        ))
        .execute(restarted.pool())
        .await
        .expect("fixture final approval");
    sqlx::query("UPDATE tasks.pr_review_batches SET status='settled' WHERE id=$1")
        .bind(review_batch)
        .execute(restarted.pool())
        .await
        .expect("fixture settled review");
    native_threads.lock().await[0]["isResolved"] = json!(true);
    let mut gate_state = task(&restarted, issue)
        .await
        .expect("gate task")
        .expect("task");
    gate_state.stage = Stage::Gates;
    gate_state.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(task_id)
        .bind(i64::try_from(gate_state.revision).expect("revision"))
        .bind(SqlJson(&gate_state))
        .execute(restarted.pool())
        .await
        .expect("fixture gates");
    let mut stale = restarted.begin().await.expect("stale ready observation");
    assert!(
        reserve_ready_intent(
            &mut stale,
            issue,
            gate_state.revision,
            19,
            &candidate_commit,
            &"a".repeat(40),
            Some(&"f".repeat(40)),
        )
        .await
        .expect("reserve stale base")
    );
    stale.commit().await.expect("stale ready intent");
    run_task_gates(&restarted, &context, issue)
        .await
        .expect("lost ready response recovered by PR readback");
    assert_eq!(ready_calls.load(Ordering::SeqCst), 1);
    let observed_base: String =
        sqlx::query_scalar("SELECT base_sha FROM tasks.pr_ready_intents WHERE task_id=$1")
            .bind(task_id)
            .fetch_one(restarted.pool())
            .await
            .expect("current ready base");
    assert_eq!(observed_base, "b".repeat(40));
    let detail = task_detail(&restarted, issue)
        .await
        .expect("ready detail")
        .expect("task");
    assert_eq!(detail["ready_intent"]["status"], "confirmed");
    assert_eq!(
        task(&restarted, issue)
            .await
            .expect("ready task")
            .expect("task")
            .stage,
        Stage::Ready
    );
    run_task_gates(&restarted, &context, issue)
        .await
        .expect("ready replay");
    assert_eq!(ready_calls.load(Ordering::SeqCst), 1);
    pulls.lock().await[0]["draft"] = json!(true);
    run_task_gates(&restarted, &context, issue)
        .await
        .expect("human returned PR to draft");
    let returned = task(&restarted, issue)
        .await
        .expect("returned task")
        .expect("task");
    assert_eq!(returned.stage, Stage::Gates);
    assert!(
        returned
            .stage_reason
            .as_deref()
            .is_some_and(|reason| reason.contains("human must mark"))
    );
    assert_eq!(ready_calls.load(Ordering::SeqCst), 1);
    sqlx::query("UPDATE tasks.pr_ready_intents SET ready_seen=false WHERE task_id=$1")
        .bind(task_id)
        .execute(restarted.pool())
        .await
        .expect("clear ready observation for human-ready fixture");
    pulls.lock().await[0]["draft"] = json!(false);
    gate_check_pending.store(true, Ordering::SeqCst);
    run_task_gates(&restarted, &context, issue)
        .await
        .expect("observe human-ready PR while CI is pending");
    let ready_seen: bool =
        sqlx::query_scalar("SELECT ready_seen FROM tasks.pr_ready_intents WHERE task_id=$1")
            .bind(task_id)
            .fetch_one(restarted.pool())
            .await
            .expect("human-ready observation");
    assert!(ready_seen);
    pulls.lock().await[0]["draft"] = json!(true);
    gate_check_pending.store(false, Ordering::SeqCst);
    run_task_gates(&restarted, &context, issue)
        .await
        .expect("human returned PR to draft after pending CI");
    assert_eq!(ready_calls.load(Ordering::SeqCst), 1);
    sqlx::query(
        "UPDATE tasks.pr_ready_intents SET status='recorded',ready_seen=false,error=NULL \
         WHERE task_id=$1",
    )
    .bind(task_id)
    .execute(restarted.pool())
    .await
    .expect("prepare GraphQL refusal fixture");
    ready_graphql_refusal.store(true, Ordering::SeqCst);
    run_task_gates(&restarted, &context, issue)
        .await
        .expect("GraphQL mark-ready refusal is recorded");
    assert_eq!(ready_calls.load(Ordering::SeqCst), 2);
    assert_eq!(
        task(&restarted, issue)
            .await
            .expect("task")
            .expect("blocked")
            .stage,
        Stage::Blocked
    );
    let ready_status: String =
        sqlx::query_scalar("SELECT status FROM tasks.pr_ready_intents WHERE task_id=$1")
            .bind(task_id)
            .fetch_one(restarted.pool())
            .await
            .expect("refused intent");
    assert_eq!(ready_status, "failed");
    let fix_turn = Uuid::now_v7();
    sqlx::query(
        "INSERT INTO tasks.attempts \
         (id,task_id,operation,expected_revision,staffing_group,fork,config_sha,submit_body, \
          initial_deadline,unbounded_running,status) \
         VALUES ($1,$2,'develop',1,$3,'acme/demo',$4,$5,now(),false,'finished')",
    )
    .bind(fix_turn)
    .bind(task_id)
    .bind(format!("fixture:{fix_turn}"))
    .bind("a".repeat(40))
    .bind(b"fixture".as_slice())
    .execute(restarted.pool())
    .await
    .expect("fix attempt");
    let fix_candidate = json!({
        "plan_id":plan_id,"binding":binding,"parent":candidate_commit,
        "edits":[{"path":"src/lib.rs","content":"fn changed() { assert!(true); }\n"}],
        "description":{"lead":"Change the function and verify the fix.",
            "problem":["Old behavior."],"changes":["New behavior.","Verified fix."],
            "compatibility":[],"review_focus":[]},
        "commit":{"subject":"Verify function","type":"fix","changes":["Add check"]}
    });
    sqlx::query(
        "INSERT INTO tasks.implementation_reports \
         (attempt_id,task_id,raw_result,status,candidate) VALUES ($1,$2,$3,'accepted',$4)",
    )
    .bind(fix_turn)
    .bind(task_id)
    .bind(b"fix fixture".as_slice())
    .bind(SqlJson(&fix_candidate))
    .execute(restarted.pool())
    .await
    .expect("accepted fix");
    let mut fix_state = task(&restarted, issue)
        .await
        .expect("task")
        .expect("fix state");
    fix_state.stage = Stage::Publish;
    fix_state.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(task_id)
        .bind(i64::try_from(fix_state.revision).expect("revision"))
        .bind(SqlJson(&fix_state))
        .execute(restarted.pool())
        .await
        .expect("publish fix");
    run_task_git_publication(&restarted, &context, issue)
        .await
        .expect("publish second commit on existing PR");
    assert_eq!(ref_creates.load(Ordering::SeqCst), 1);
    assert_eq!(ref_advances.load(Ordering::SeqCst), 2);
    assert_eq!(pr_creates.load(Ordering::SeqCst), 1);
    assert_eq!(pulls.lock().await[0]["head"]["sha"], second_commit);
    assert!(
        pulls.lock().await[0]["body"]
            .as_str()
            .is_some_and(|body| body.contains("Verified fix."))
    );
    let next_round: i64 = sqlx::query_scalar(
        "SELECT round FROM tasks.pr_review_batches WHERE publication_attempt=$1",
    )
    .bind(fix_turn)
    .fetch_one(restarted.pool())
    .await
    .expect("next review round");
    assert_eq!(next_round, 1);
    sqlx::query("UPDATE tasks.pr_review_batches SET status='settled' WHERE publication_attempt=$1")
        .bind(fix_turn)
        .execute(restarted.pool())
        .await
        .expect("settle fix review for description repair");
    let description_turn = Uuid::now_v7();
    sqlx::query(
        "INSERT INTO tasks.attempts \
         (id,task_id,operation,expected_revision,staffing_group,fork,config_sha,submit_body, \
          initial_deadline,unbounded_running,status) \
         VALUES ($1,$2,'develop',1,$3,'acme/demo',$4,$5,now(),false,'finished')",
    )
    .bind(description_turn)
    .bind(task_id)
    .bind(format!("fixture:{description_turn}"))
    .bind("a".repeat(40))
    .bind(b"fixture".as_slice())
    .execute(restarted.pool())
    .await
    .expect("description attempt");
    let description_candidate = json!({
        "plan_id":plan_id,"binding":binding,"parent":second_commit,
        "edits":[],"description":{"lead":"Change the function and verify the fix.",
            "problem":["Old behavior."],"changes":["New behavior.","Verified fix.",
            "Clarified validation."],"compatibility":[],"review_focus":[]},
        "validation":"Earlier validation remains applicable",
        "commit":{"subject":"Clarify validation","type":"docs","changes":["Clarify PR text"]}
    });
    sqlx::query(
        "INSERT INTO tasks.implementation_reports \
         (attempt_id,task_id,raw_result,status,candidate) VALUES ($1,$2,$3,'accepted',$4)",
    )
    .bind(description_turn)
    .bind(task_id)
    .bind(b"description fixture".as_slice())
    .bind(SqlJson(&description_candidate))
    .execute(restarted.pool())
    .await
    .expect("accepted description repair");
    let mut description_state = task(&restarted, issue)
        .await
        .expect("task")
        .expect("description state");
    description_state.stage = Stage::Publish;
    description_state.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(task_id)
        .bind(i64::try_from(description_state.revision).expect("revision"))
        .bind(SqlJson(&description_state))
        .execute(restarted.pool())
        .await
        .expect("publish description repair");
    run_task_git_publication(&restarted, &context, issue)
        .await
        .expect("publish description without a Git commit");
    assert_eq!(ref_advances.load(Ordering::SeqCst), 2);
    assert_eq!(pr_creates.load(Ordering::SeqCst), 1);
    assert_eq!(pulls.lock().await[0]["head"]["sha"], second_commit);
    assert!(
        pulls.lock().await[0]["body"]
            .as_str()
            .is_some_and(|body| body.contains("Clarified validation."))
    );
    let followup_turn = Uuid::now_v7();
    let followup_batch = Uuid::now_v7();
    let review_marker = format!("<!-- concurro:pr-review:{task_id}:{followup_turn} -->");
    let followup_body = format!("{review_marker}\n### Fixed finding F1");
    let reply_marker = format!("<!-- concurro:pr-disposition:{followup_turn}:F1 -->");
    let reply_body = format!("{reply_marker}\n**fixed**: Verified on the new head.");
    let mut followup_state = task(&restarted, issue)
        .await
        .expect("task")
        .expect("follow-up review state");
    followup_state.stage = Stage::Review;
    followup_state.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(task_id)
        .bind(i64::try_from(followup_state.revision).expect("revision"))
        .bind(SqlJson(&followup_state))
        .execute(restarted.pool())
        .await
        .expect("follow-up review stage");
    sqlx::query(
        "INSERT INTO tasks.attempts \
         (id,task_id,operation,expected_revision,staffing_group,fork,config_sha,submit_body, \
          initial_deadline,unbounded_running,status) \
         VALUES ($1,$2,'review',$3,$4,'acme/demo',$5,$6,now(),false,'finished')",
    )
    .bind(followup_turn)
    .bind(task_id)
    .bind(i64::try_from(followup_state.revision).expect("revision"))
    .bind(format!("fixture:{followup_turn}"))
    .bind("a".repeat(40))
    .bind(b"follow-up fixture".as_slice())
    .execute(restarted.pool())
    .await
    .expect("follow-up review attempt");
    sqlx::query(
        "INSERT INTO tasks.pr_review_batches \
         (id,task_id,publication_attempt,pr_number,head,branch,plan_id,round, \
          changed_paths,roster,status) \
         SELECT $1,task_id,$2,pr_number,$3,branch,plan_id, \
             (SELECT max(round)+1 FROM tasks.pr_review_batches WHERE task_id=$5), \
             changed_paths,roster,'admitted' \
         FROM tasks.pr_review_batches WHERE id=$4",
    )
    .bind(followup_batch)
    .bind(fix_turn)
    .bind(&second_commit)
    .bind(review_batch)
    .bind(task_id)
    .execute(restarted.pool())
    .await
    .expect("follow-up review batch");
    let followup_report = json!({
        "verdict":"approve","summary":"Fixed on the new head","findings":[],
        "dispositions":[{"id":"F1","status":"fixed","evidence":"Verified on the new head."}],
        "design_issue":null
    });
    sqlx::query(
        "INSERT INTO tasks.pr_review_seats \
         (batch_id,reviewer_id,identity,binding,attempt_id,status,report) \
         SELECT $1,reviewer_id,identity,binding,$2,'accepted',$3 \
         FROM tasks.pr_review_seats WHERE batch_id=$4",
    )
    .bind(followup_batch)
    .bind(followup_turn)
    .bind(SqlJson(&followup_report))
    .bind(review_batch)
    .execute(restarted.pool())
    .await
    .expect("follow-up reviewer seat");
    sqlx::query("UPDATE tasks.pr_review_findings SET status='fixed' WHERE task_id=$1 AND id='F1'")
        .bind(task_id)
        .execute(restarted.pool())
        .await
        .expect("owner fixed finding");
    native_threads.lock().await[0]["isResolved"] = json!(false);
    sqlx::query(
        "INSERT INTO tasks.pr_review_publications \
         (turn_id,task_id,batch_id,pr_number,head,marker,body) \
         VALUES ($1,$2,$3,19,$4,$5,$6)",
    )
    .bind(followup_turn)
    .bind(task_id)
    .bind(followup_batch)
    .bind(&second_commit)
    .bind(&review_marker)
    .bind(&followup_body)
    .execute(restarted.pool())
    .await
    .expect("follow-up native review");
    for (sequence, kind, finding, marker, body) in [
        (
            0,
            "create",
            None,
            review_marker.clone(),
            review_marker.clone(),
        ),
        (1, "reply", Some("F1"), reply_marker, reply_body),
        (
            2,
            "submit",
            None,
            review_marker.clone(),
            followup_body.clone(),
        ),
        (
            3,
            "resolve",
            Some("F1"),
            format!("resolve:{followup_turn}:F1"),
            String::new(),
        ),
    ] {
        sqlx::query(
            "INSERT INTO tasks.pr_review_actions \
             (turn_id,sequence,kind,finding_id,marker,body) \
             VALUES ($1,$2,$3,$4,$5,$6)",
        )
        .bind(followup_turn)
        .bind(sequence)
        .bind(kind)
        .bind(finding)
        .bind(marker)
        .bind(body)
        .execute(restarted.pool())
        .await
        .expect("follow-up native action");
    }
    publish_pr_review(&restarted, &context, followup_turn)
        .await
        .expect("owner reply and resolution after lost responses");
    assert_eq!(review_creates.load(Ordering::SeqCst), 3);
    assert_eq!(review_submits.load(Ordering::SeqCst), 2);
    assert_eq!(native_threads.lock().await[0]["isResolved"], true);
    let followup_actions: Vec<String> = sqlx::query_scalar(
        "SELECT status FROM tasks.pr_review_actions WHERE turn_id=$1 ORDER BY sequence",
    )
    .bind(followup_turn)
    .fetch_all(restarted.pool())
    .await
    .expect("follow-up action statuses");
    assert_eq!(followup_actions, vec!["confirmed"; 4]);
    let mut terminal_state = task(&restarted, issue)
        .await
        .expect("task")
        .expect("review state");
    terminal_state.stage = Stage::Review;
    terminal_state.revision += 1;
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(task_id)
        .bind(i64::try_from(terminal_state.revision).expect("revision"))
        .bind(SqlJson(&terminal_state))
        .execute(restarted.pool())
        .await
        .expect("terminal review");
    pulls.lock().await[0]["state"] = json!("closed");
    pulls.lock().await[0]["merged"] = json!(true);
    pulls.lock().await[0]["merge_commit_sha"] = json!("9".repeat(40));
    run_task_gates(&restarted, &context, issue)
        .await
        .expect("external merge observation");
    assert_eq!(
        task(&restarted, issue)
            .await
            .expect("task")
            .expect("merged task")
            .stage,
        Stage::ObservedMerged
    );
    let detail = task_detail(&restarted, issue)
        .await
        .expect("detail")
        .expect("task detail");
    assert_eq!(detail["terminal_pr"]["observed_head"], second_commit);
    assert_eq!(detail["terminal_pr"]["gates_verified"], false);
    server.abort();
}
