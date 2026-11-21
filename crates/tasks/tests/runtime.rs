//! The task executable drives typed comments through its real worker wiring.

use std::{
    collections::{HashMap, HashSet},
    fs,
    io::Write,
    process::Command,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

use async_nats::jetstream::{self, stream};
use axum::{
    Json, Router,
    body::Bytes,
    extract::State,
    http::{Method, StatusCode, Uri},
    response::{IntoResponse, Response},
    routing::any,
};
use base64::{Engine, engine::general_purpose::STANDARD};
use chrono::{Duration as ChronoDuration, Utc};
use concurro_contracts::{GithubEvent, IssueCommentPayload, decode};
use concurro_db::Database;
use concurro_events_nats::ensure_stream;
use concurro_events_postgres::migrate as migrate_events;
use concurro_tasks_core::{IssueKey, Stage};
use concurro_tasks_postgres::{migrate, task};
use concurro_testkit::{NatsContainer, PostgresContainer};
use flate2::{Compression, write::GzEncoder};
use serde_json::{Value, json};
use tar::{Builder, Header};
use tokio::{
    process::Child,
    sync::{Mutex, Notify},
};
use uuid::Uuid;

/// Mutable responses and externally visible writes of the local GitHub fixture.
#[derive(Default)]
struct GithubFixture {
    comments: Vec<Value>,
    artifacts: Vec<Value>,
    next_comment: i64,
    refs: HashMap<String, String>,
    pulls: Vec<Value>,
    reviews: Vec<Value>,
    hold_review: Arc<AtomicBool>,
    review_entered: Arc<Notify>,
    release_review: Arc<Notify>,
    ready_calls: usize,
    dispatches: Vec<Uuid>,
    labels: HashSet<String>,
    hold_intake_notice: Arc<AtomicBool>,
    intake_notice_entered: Arc<Notify>,
    release_intake_notice: Arc<Notify>,
}

/// Answer the App calls made by decision, initialization and publication jobs.
async fn github_reply(
    State(state): State<Arc<Mutex<GithubFixture>>>,
    method: Method,
    uri: Uri,
    body: Bytes,
) -> (StatusCode, Json<Value>) {
    let path = uri.path();
    let request: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
    let mut fixture = state.lock().await;
    let ok = |value| (StatusCode::OK, Json(value));
    let missing = || (StatusCode::NOT_FOUND, Json(json!({"message":"not found"})));
    if path == "/repos/acme/demo/installation" {
        return ok(json!({"id":7,"repository_selection":"selected","account":{"login":"acme"}}));
    }
    if path == "/app/installations/7/access_tokens" {
        return ok(
            json!({"token":"scoped-token","permissions":request["permissions"],
            "expires_at":(Utc::now()+ChronoDuration::minutes(55)).to_rfc3339()}),
        );
    }
    if path == "/installation/repositories" {
        return ok(json!({"total_count":1,"repositories":[{"full_name":"acme/demo"}]}));
    }
    if path == "/repos/acme/demo" {
        return ok(json!({"id":8,"owner":{"id":9},"default_branch":"release"}));
    }
    if path == "/users/concurro[bot]" {
        return ok(json!({"id":10,"login":"concurro[bot]"}));
    }
    if path == "/repos/acme/demo/git/ref/heads/release" {
        return ok(json!({"object":{"sha":"a".repeat(40)}}));
    }
    if path == "/repos/acme/demo/git/ref/heads/main" {
        return ok(json!({"object":{"sha":"b".repeat(40)}}));
    }
    if let Some(branch) = path.strip_prefix("/repos/acme/demo/git/ref/heads/") {
        return fixture
            .refs
            .get(branch)
            .map_or_else(missing, |sha| ok(json!({"object":{"sha":sha}})));
    }
    if path == "/repos/acme/demo/git/refs" && method == Method::POST {
        let branch = request["ref"]
            .as_str()
            .unwrap_or_default()
            .trim_start_matches("refs/heads/");
        fixture.refs.insert(
            branch.to_owned(),
            request["sha"].as_str().unwrap_or_default().to_owned(),
        );
        return (StatusCode::CREATED, Json(json!({"ref":request["ref"]})));
    }
    if let Some(branch) = path.strip_prefix("/repos/acme/demo/git/refs/heads/")
        && method == Method::PATCH
    {
        let sha = request["sha"].as_str().unwrap_or_default().to_owned();
        fixture.refs.insert(branch.to_owned(), sha.clone());
        if let Some(pr) = fixture.pulls.first_mut() {
            pr["head"]["sha"] = json!(sha);
        }
        return ok(json!({"object":{"sha":sha}}));
    }
    if let Some(branch) = path.strip_prefix("/repos/acme/demo/git/refs/heads/")
        && method == Method::DELETE
    {
        fixture.refs.remove(branch);
        return ok(json!({}));
    }
    if path == "/repos/acme/demo/actions/workflows/concurro-executor.yml/dispatches" {
        let session = request["inputs"]["session"]
            .as_str()
            .and_then(|value| Uuid::parse_str(value).ok())
            .expect("dispatch session");
        fixture.dispatches.push(session);
        return ok(json!({}));
    }
    if path == "/repos/acme/demo/actions/workflows/concurro-executor.yml/runs" {
        let runs = fixture
            .dispatches
            .iter()
            .enumerate()
            .map(|(index, session)| {
                json!({
                    "id":index+100,"head_branch":format!("concurro/runs/{session}"),
                    "head_sha":"a".repeat(40),"event":"workflow_dispatch"
                })
            })
            .collect::<Vec<_>>();
        return ok(json!({"workflow_runs":runs}));
    }
    if let Some(file) = path.strip_prefix("/repos/acme/demo/contents/") {
        let content = match file {
            ".concurro/config.yaml" => Some(
                "repository: acme/demo\nbase: main\nbot_login: concurro[bot]\nmax_model_calls: 10\nmax_fix_rounds: 2\nmax_plan_revisions: 1\nmax_context_bytes: 350000\nturn_timeout_seconds: 120\nmax_retained_bytes: null\npolicy_documents: [README.md]\nplanner: codex\nimplementer: codex\nqc: codex\nreviewers: [codex]\nmodels: {codex: gpt-test}\npaths: {protected: [], protected_extensions: [], protected_names: [], protected_name_prefixes: [], protected_prefixes: [], protected_files: [], generated: []}\n",
            ),
            ".concurro/charter.json" => Some(
                "{\"version\":1,\"id\":\"test\",\"goals\":{\"G-1\":\"Goal\"},\"non_goals\":[\"none\"],\"constraints\":[\"stay in scope\"],\"acceptance\":[\"reviewed\"]}",
            ),
            "README.md" => Some("Upstream policy"),
            "src/lib.rs" => Some("fn example() {}\n"),
            _ => None,
        };
        return content.map_or_else(missing, |text| {
            ok(json!({
                "type":"file","encoding":"base64","path":file,
                "size":text.len(),"content":STANDARD.encode(text)
            }))
        });
    }
    if let Some(sha) = path.strip_prefix("/repos/acme/demo/git/blobs/") {
        let content = if sha == "4".repeat(40) {
            "fn changed() {}\n"
        } else {
            "fn example() {}\n"
        };
        return ok(json!({"sha":sha,"encoding":"base64","content":STANDARD.encode(content)}));
    }
    if path.starts_with("/repos/acme/demo/git/commits/") {
        let sha = path.rsplit('/').next().unwrap_or_default();
        let parents = if sha == "f".repeat(40) {
            json!([{"sha":"b".repeat(40)},{"sha":"e".repeat(40)}])
        } else {
            json!([])
        };
        return ok(json!({"sha":sha,"tree":{"sha":"c".repeat(40)},"parents":parents}));
    }
    if path == "/repos/acme/demo/git/trees" && method == Method::POST {
        return ok(json!({"sha":"d".repeat(40)}));
    }
    if path == "/repos/acme/demo/git/commits" && method == Method::POST {
        return ok(json!({"sha":"e".repeat(40)}));
    }
    if let Some(sha) = path.strip_prefix("/repos/acme/demo/git/trees/") {
        let source = if sha == "e".repeat(40) {
            "4".repeat(40)
        } else {
            "3".repeat(40)
        };
        return ok(json!({"truncated":false,"tree":[
            {"path":"README.md","type":"blob","mode":"100644","sha":"2".repeat(40)},
            {"path":"src/lib.rs","type":"blob","mode":"100644","sha":source}
        ]}));
    }
    if path == "/repos/acme/demo/pulls" {
        if method == Method::GET {
            return ok(json!(fixture.pulls));
        }
        let branch = request["head"]
            .as_str()
            .unwrap_or_default()
            .split_once(':')
            .map_or("", |(_, branch)| branch);
        let pr = json!({
            "number":19,"state":"open","draft":true,"title":request["title"],
            "body":request["body"],"user":{"id":10,"login":"concurro[bot]"},
            "head":{"sha":"e".repeat(40),"ref":branch,"repo":{"full_name":"acme/demo"}},
            "base":{"ref":"main","sha":"b".repeat(40)},
            "merge_commit_sha":"f".repeat(40),"merged":false,"node_id":"PR_19",
        });
        fixture.pulls.push(pr.clone());
        return (StatusCode::CREATED, Json(pr));
    }
    if path == "/repos/acme/demo/pulls/19" {
        return fixture.pulls.first().cloned().map_or_else(missing, ok);
    }
    if path == "/repos/acme/demo/pulls/19/files" {
        return ok(json!([{"filename":"src/lib.rs","status":"modified"}]));
    }
    if path == "/repos/acme/demo/pulls/19/reviews" {
        if method == Method::GET {
            return ok(json!(fixture.reviews));
        }
        let review = json!({"id":91,"state":"COMMENTED","commit_id":request["commit_id"],
            "body":request["body"],"user":{"id":10,"login":"concurro[bot]"}});
        fixture.reviews.push(review.clone());
        let hold_review = fixture.hold_review.swap(false, Ordering::SeqCst);
        let entered = fixture.review_entered.clone();
        let release = fixture.release_review.clone();
        drop(fixture);
        if hold_review {
            entered.notify_one();
            release.notified().await;
        }
        return ok(review);
    }
    if path == "/graphql" {
        let query = request["query"].as_str().unwrap_or_default();
        if query.contains("reviewThreads") {
            return ok(
                json!({"data":{"repository":{"pullRequest":{"reviewThreads":{
                    "nodes":[],"pageInfo":{"hasNextPage":false,"endCursor":null}
                }}}}}),
            );
        }
        if query.contains("markPullRequestReadyForReview") {
            fixture.ready_calls += 1;
            fixture.pulls[0]["draft"] = json!(false);
            return ok(
                json!({"data":{"markPullRequestReadyForReview":{"pullRequest":{
                    "id":"PR_19","isDraft":false
                }}}}),
            );
        }
    }
    if let Some(sha) = path.strip_prefix("/repos/acme/demo/commits/") {
        if sha.ends_with("/check-runs") {
            let head = sha.trim_end_matches("/check-runs");
            return ok(json!({"check_runs":[{"id":8,"name":"build","head_sha":head,
                "status":"completed","conclusion":"success","check_suite":{"id":4},"app":{"id":7}}]}));
        }
        if sha.ends_with("/check-suites") {
            let head = sha.trim_end_matches("/check-suites");
            return ok(json!({"check_suites":[{"id":4,"head_sha":head,
                "status":"completed","conclusion":"success"}]}));
        }
        if sha.ends_with("/statuses") {
            return ok(json!([]));
        }
        if sha.ends_with("/status") {
            return ok(json!({"state":"success","statuses":[]}));
        }
    }
    if path == "/repos/acme/demo/actions/runs" {
        let head = uri
            .query()
            .unwrap_or_default()
            .split('&')
            .find_map(|part| part.strip_prefix("head_sha="))
            .unwrap_or_default();
        return ok(json!({"workflow_runs":[{"id":10,"workflow_id":9,
            "event":"pull_request","head_branch":"topic","check_suite_id":4,
            "head_sha":head,"name":"CI","status":"completed",
            "conclusion":"success"}]}));
    }
    if path == "/repos/acme/demo/issues/3" {
        return ok(
            json!({"state":"open","title":"Task title","body":"Issue body",
            "user":{"id":10,"login":"writer"}}),
        );
    }
    if path == "/repos/acme/demo/collaborators/writer/permission" {
        return ok(json!({"permission":"write"}));
    }
    if path == "/repos/acme/demo/issues/3/comments" {
        if method == Method::GET {
            let mut comments = vec![json!({"id":5,"body":"/concurro start",
                "user":{"id":10,"login":"writer","type":"User"},"author_association":"OWNER",
                "created_at":"2026-11-19T10:00:00Z","updated_at":"2026-11-19T10:00:00Z",
                "html_url":"https://example.test/acme/demo/issues/3#issuecomment-5"})];
            comments.extend(fixture.comments.iter().cloned());
            return ok(json!(comments));
        }
        fixture.next_comment += 1;
        let id = fixture.next_comment + 1000;
        let comment = json!({"id":id,"body":request["body"],
            "html_url":format!("https://example.test/acme/demo/issues/3#issuecomment-{id}"),
            "user":{"id":10,"login":"concurro[bot]","type":"Bot"},"author_association":"COLLABORATOR",
            "created_at":"2026-11-19T10:01:00Z","updated_at":"2026-11-19T10:01:00Z"});
        fixture.comments.push(comment.clone());
        let hold_notice = request["body"]
            .as_str()
            .is_some_and(|body| body.contains("Intake approved with concerns"))
            && fixture.hold_intake_notice.swap(false, Ordering::SeqCst);
        let entered = fixture.intake_notice_entered.clone();
        let release = fixture.release_intake_notice.clone();
        drop(fixture);
        if hold_notice {
            entered.notify_one();
            release.notified().await;
        }
        return (StatusCode::CREATED, Json(comment));
    }
    if path == "/repos/acme/demo/issues/3/artifacts" {
        if method == Method::GET {
            return ok(json!(fixture.artifacts));
        }
        let number = i64::try_from(fixture.artifacts.len()).expect("artifact count") + 1;
        let artifact = json!({
            "id":number,"number":number,"name":request["name"],
            "body":request["body"],"type":request["type"],
            "creator":{"login":"concurro[bot]"},
            "updated_by_actor":{"login":"concurro[bot]"},
        });
        fixture.artifacts.push(artifact.clone());
        return (StatusCode::CREATED, Json(artifact));
    }
    if let Some(number) = path.strip_prefix("/repos/acme/demo/issues/3/artifacts/") {
        return fixture
            .artifacts
            .iter()
            .find(|artifact| artifact["number"].to_string() == number)
            .map_or_else(missing, |artifact| {
                ok(json!({
                    "artifact":artifact,
                    "versions":[{"version":1,"name":artifact["name"],
                        "body":artifact["body"],"actor":{"login":"concurro[bot]"}}]
                }))
            });
    }
    if let Some(id) = path.strip_prefix("/repos/acme/demo/issues/comments/") {
        if let Some(comment) = fixture
            .comments
            .iter_mut()
            .find(|comment| comment["id"].to_string() == id)
        {
            if method == Method::PATCH {
                comment["body"] = request["body"].clone();
            }
            return ok(comment.clone());
        }
        return missing();
    }
    if path == "/repos/acme/demo/issues/3/labels" {
        if method == Method::GET {
            return ok(json!(
                fixture
                    .labels
                    .iter()
                    .map(|name| json!({"name":name}))
                    .collect::<Vec<_>>()
            ));
        }
        if let Some(labels) = request["labels"].as_array() {
            for label in labels {
                if let Some(name) = label.as_str() {
                    fixture.labels.insert(name.to_owned());
                }
            }
        }
        return ok(json!([]));
    }
    if path == "/repos/acme/demo/labels" && method == Method::POST {
        let name = request["name"].as_str().unwrap_or_default();
        return (StatusCode::CREATED, Json(json!({"name":name})));
    }
    if let Some(name) = path.strip_prefix("/repos/acme/demo/labels/") {
        return ok(json!({"name":name}));
    }
    if let Some(name) = path.strip_prefix("/repos/acme/demo/issues/3/labels/")
        && method == Method::DELETE
    {
        fixture.labels.remove(name);
        return ok(json!({}));
    }
    eprintln!("unhandled GitHub fixture request: {method} {path}");
    missing()
}

/// Return stage-specific results only after the corresponding runner dispatch.
async fn exchange_reply(
    State((
        github,
        turns,
        session,
        archive,
        hold_submit,
        submit_entered,
        release_submit,
        hold_poll,
        poll_entered,
        release_poll,
    )): State<(
        Arc<Mutex<GithubFixture>>,
        Arc<Mutex<HashMap<Uuid, (String, Uuid)>>>,
        Uuid,
        String,
        Arc<AtomicBool>,
        Arc<Notify>,
        Arc<Notify>,
        Arc<AtomicBool>,
        Arc<Notify>,
        Arc<Notify>,
    )>,
    method: Method,
    uri: Uri,
    body: Bytes,
) -> (StatusCode, Json<Value>) {
    if uri.path() == "/exchange/operator/turns" && method == Method::POST {
        let submitted: Value = serde_json::from_slice(&body).expect("turn body");
        let id = Uuid::parse_str(submitted["turn"].as_str().expect("turn")).expect("turn id");
        let stage = submitted["payload"]["request"]["stage"]
            .as_str()
            .expect("request stage")
            .to_owned();
        let dispatch = if stage == "intake" {
            session
        } else {
            Uuid::now_v7()
        };
        let mut stored = turns.lock().await;
        let dispatch = stored.entry(id).or_insert((stage.clone(), dispatch)).1;
        drop(stored);
        if stage == "intake" && hold_submit.swap(false, Ordering::SeqCst) {
            submit_entered.notify_one();
            release_submit.notified().await;
        }
        return (
            StatusCode::OK,
            Json(json!({"turn":id,"status":"queued","created":1.0,"dispatch":dispatch})),
        );
    }
    let Some(id) = uri
        .path()
        .rsplit('/')
        .next()
        .and_then(|id| Uuid::parse_str(id).ok())
    else {
        return (StatusCode::NOT_FOUND, Json(json!({"error":"unknown turn"})));
    };
    let Some((stage, dispatch)) = turns.lock().await.get(&id).cloned() else {
        return (StatusCode::NOT_FOUND, Json(json!({"error":"unknown turn"})));
    };
    if method == Method::DELETE {
        return (StatusCode::OK, Json(json!({"sessions":[dispatch]})));
    }
    if !github.lock().await.dispatches.contains(&dispatch) {
        return (
            StatusCode::OK,
            Json(json!({"turn":id,"status":"queued","created":1.0,"dispatch":dispatch})),
        );
    }
    if stage == "intake" && hold_poll.swap(false, Ordering::SeqCst) {
        poll_entered.notify_one();
        release_poll.notified().await;
    }
    let response = match stage.as_str() {
        "intake" => json!({
            "decision":"proceed","rationale":"Fits","risk":"low",
            "questions":[],"concerns":["Review scope"],
            "scope":{"goal_ids":["G-1"],"fit":"Matches goal","conflicts":[]}
        }),
        "plan" => json!({
            "summary":"Implement the change","questions":[],"files":["src/lib.rs"],
            "steps":["Edit code"],"tests":["Run suite"],"responses":[],
            "publication":{"changes":["Change behavior"],"review_focus":["Correctness"]},
            "scope":{"paths":["src/"]}
        }),
        "plan_qc" => json!({
            "decision":"approve","rationale":"The plan covers the request.","risk":"low",
            "questions":[],"concerns":[],
            "scope":{"goal_ids":["G-1"],"fit":"Matches goal","conflicts":[]},
            "sources":[{"id":"S1","path":"src/lib.rs","line":1,"quote":"fn example()"}],
            "assessments":[
                {"criterion":"R1","status":"satisfied","sources":["S1"],
                    "reason":"The planned edit addresses the title.","counterexample":"None applies."},
                {"criterion":"R2","status":"satisfied","sources":["S1"],
                    "reason":"The planned edit addresses the body.","counterexample":"None applies."}
            ],
            "objection_updates":[]
        }),
        "develop" => json!({
            "summary":"Changed the behavior.","validation":"Run suite passed.",
            "design_issue":null,
            "description":{"lead":"Update task behavior.","problem":["Old behavior needs change."],
                "changes":["Update src/lib.rs."],"compatibility":[],"review_focus":[]},
            "commit":{"subject":"Update behavior","type":"feat","changes":["Update source."]}
        }),
        "review" => json!({"verdict":"approve","summary":"Looks good.",
            "findings":[],"dispositions":[],"design_issue":null}),
        other => panic!("unexpected exchange stage: {other}"),
    };
    let workspace = if stage == "develop" {
        json!({"edits":[{"path":"src/lib.rs","content":"fn changed() {}\n"}],"removed":[]})
    } else {
        Value::Null
    };
    (
        StatusCode::OK,
        Json(
            json!({"turn":id,"status":"done","created":1.0,"finished":2.0,
            "runner":{"session":dispatch,"run_id":"run-1"},
            "result":{"outcome":"ok","response":response,"workspace":workspace,"retained":archive}}),
        ),
    )
}

/// Keep exact S3 bytes for the runtime upload and readback jobs.
async fn archive_reply(
    State(objects): State<Arc<Mutex<HashMap<String, Vec<u8>>>>>,
    method: Method,
    uri: Uri,
    body: Bytes,
) -> Response {
    let key = uri.path().to_owned();
    if method == Method::PUT {
        objects.lock().await.insert(key, body.to_vec());
        return (StatusCode::OK, [("etag", "\"stored\"")]).into_response();
    }
    if method == Method::GET {
        if let Some(bytes) = objects.lock().await.get(&key).cloned() {
            return (StatusCode::OK, [("etag", "\"stored\"")], bytes).into_response();
        }
    }
    StatusCode::NOT_FOUND.into_response()
}

/// Launch the actual task binary against one retained database and broker.
fn launch(config: &std::path::Path, log: &std::path::Path) -> Child {
    let stderr = fs::File::create(log).expect("task log");
    let stdout = stderr.try_clone().expect("task stdout");
    tokio::process::Command::new(env!("CARGO_BIN_EXE_concurro-tasks"))
        .env("CONCURRO_TASKS_CONFIG", config)
        .stdout(stdout)
        .stderr(stderr)
        .kill_on_drop(true)
        .spawn()
        .expect("task process")
}

/// Reach Ready from Start across intake and PR-review write restarts.
#[tokio::test]
async fn typed_start_reaches_ready_through_task_restarts() {
    let postgres = PostgresContainer::start().await.expect("Postgres");
    let nats = NatsContainer::start().await.expect("NATS");
    let database = Database::connect(postgres.url(), 4)
        .await
        .expect("database");
    migrate_events(&database).await.expect("event schema");
    migrate(&database).await.expect("task schema");
    let intake_notice_entered = Arc::new(Notify::new());
    let release_intake_notice = Arc::new(Notify::new());
    let review_entered = Arc::new(Notify::new());
    let release_review = Arc::new(Notify::new());
    let github = Arc::new(Mutex::new(GithubFixture {
        hold_review: Arc::new(AtomicBool::new(true)),
        review_entered: review_entered.clone(),
        release_review: release_review.clone(),
        hold_intake_notice: Arc::new(AtomicBool::new(true)),
        intake_notice_entered: intake_notice_entered.clone(),
        release_intake_notice: release_intake_notice.clone(),
        ..GithubFixture::default()
    }));
    let turns = Arc::new(Mutex::new(HashMap::new()));
    let session = Uuid::now_v7();
    let hold_submit = Arc::new(AtomicBool::new(true));
    let submitted = Arc::new(Notify::new());
    let release_submit = Arc::new(Notify::new());
    let hold_poll = Arc::new(AtomicBool::new(true));
    let poll_entered = Arc::new(Notify::new());
    let release_poll = Arc::new(Notify::new());
    let mut tar = Builder::new(Vec::new());
    let line = b"{\"type\":\"message\",\"text\":\"archive proof\"}\n";
    let mut header = Header::new_gnu();
    header.set_size(line.len() as u64);
    header.set_mode(0o644);
    header.set_cksum();
    tar.append_data(&mut header, "codex/turn.jsonl", line.as_slice())
        .expect("transcript member");
    let mut gzip = GzEncoder::new(Vec::new(), Compression::default());
    gzip.write_all(&tar.into_inner().expect("tar bytes"))
        .expect("gzip write");
    let archive_bytes = gzip.finish().expect("gzip bytes");
    let archive = STANDARD.encode(&archive_bytes);
    let objects = Arc::new(Mutex::new(HashMap::<String, Vec<u8>>::new()));
    let github_app = Router::new()
        .fallback(any(github_reply))
        .with_state(github.clone());
    let exchange_app = Router::new().fallback(any(exchange_reply)).with_state((
        github.clone(),
        turns.clone(),
        session,
        archive,
        hold_submit,
        submitted.clone(),
        release_submit.clone(),
        hold_poll,
        poll_entered.clone(),
        release_poll.clone(),
    ));
    let archive_app = Router::new()
        .fallback(any(archive_reply))
        .with_state(objects.clone());
    let github_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("github bind");
    let github_address = github_listener.local_addr().expect("github address");
    let github_server = tokio::spawn(async move { axum::serve(github_listener, github_app).await });
    let exchange_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("exchange bind");
    let exchange_address = exchange_listener.local_addr().expect("exchange address");
    let exchange_server =
        tokio::spawn(async move { axum::serve(exchange_listener, exchange_app).await });
    let archive_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("archive bind");
    let archive_address = archive_listener.local_addr().expect("archive address");
    let archive_server =
        tokio::spawn(async move { axum::serve(archive_listener, archive_app).await });
    let key = Command::new("openssl")
        .args([
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
        ])
        .output()
        .expect("App key");
    assert!(key.status.success());
    let dir = std::env::temp_dir().join(format!("concurro-runtime-{}", Uuid::now_v7()));
    fs::create_dir(&dir).expect("fixture directory");
    fs::write(dir.join("database"), postgres.url()).expect("database URL");
    fs::write(dir.join("key.pem"), key.stdout).expect("App key");
    fs::write(dir.join("token"), "operator-secret").expect("exchange token");
    fs::write(
        dir.join("archive.json"),
        r#"{"access_key_id":"local","secret_access_key":"local"}"#,
    )
    .expect("archive credentials");
    let config = format!(
        r#"
database_url_file = "{}"
nats_url = "{}"
github_api_base = "http://{}"
github_app_id = 42
github_private_key_file = "{}"
exchange_url = "http://{}"
exchange_token_file = "{}"
archive_endpoint = "http://{}"
archive_bucket = "archive"
archive_prefix = "turns"
archive_region = "us-east-1"
archive_credentials_file = "{}"
max_message_bytes = 1048576
published_retention_seconds = 3600
forwarder_poll_ms = 20
consumer_name = "runtime-test"
consumer_max_attempts = 3
consumer_poll_ms = 20

[intake]
gateway = {{}}
workflow_ref = "acme/demo/.github/workflows/concurro-executor.yml@refs/heads/release"
models = ["gpt-test"]
budget = 100
lease = 3600

[[targets]]
repository_id = 8
issue_repository = "acme/demo"
fork = "acme/demo"
upstream = "acme/demo"
bot_login = "concurro[bot]"
publisher_logins = []
engine_sha = "engine"
issue_permissions = {{ issues = "read", contents = "read" }}
issue_write_permissions = {{ issues = "write" }}
fork_permissions = {{ contents = "read", actions = "read" }}
fork_write_permissions = {{ contents = "write", actions = "write", workflows = "write" }}
upstream_permissions = {{ contents = "read" }}
upstream_write_permissions = {{ pull_requests = "write" }}
"#,
        dir.join("database").display(),
        nats.url(),
        github_address,
        dir.join("key.pem").display(),
        exchange_address,
        dir.join("token").display(),
        archive_address,
        dir.join("archive.json").display()
    );
    fs::write(dir.join("tasks.toml"), config).expect("task config");
    let client = async_nats::connect(nats.url()).await.expect("NATS client");
    let stream = jetstream::new(client);
    ensure_stream(
        &stream,
        stream::Config {
            name: "GITHUB".to_owned(),
            subjects: vec!["github.>".to_owned()],
            ..Default::default()
        },
    )
    .await
    .expect("ingress stream");
    let mut event: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/github.issue_comment.v1/valid/representative.json"
    ))
    .expect("comment fixture");
    event["id"] = json!(Uuid::now_v7());
    event["data"]["repository"]["full_name"] = json!("acme/demo");
    event["data"]["comment"]["id"] = json!(5);
    event["data"]["comment"]["body"] = json!("/concurro start");
    event["data"]["issue"]["user"] = json!({"id":10,"login":"writer","type":"User"});
    let raw = serde_json::to_vec(&event).expect("typed event");
    decode::<GithubEvent<IssueCommentPayload>>(&raw).expect("contract");
    stream
        .publish("github.issue_comment.created", raw.clone().into())
        .await
        .expect("publish")
        .await
        .expect("publish ack");
    let mut process = launch(&dir.join("tasks.toml"), &dir.join("tasks.log"));
    tokio::time::timeout(Duration::from_secs(15), submitted.notified())
        .await
        .expect("submit reached exchange");
    let sending: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM tasks.effects WHERE kind='submit' AND status='sending'",
    )
    .fetch_one(database.pool())
    .await
    .expect("submit state");
    assert_eq!(sending, 1);
    process.kill().await.expect("kill during submit");
    process.wait().await.expect("submit process exit");
    release_submit.notify_one();
    let mut process = launch(
        &dir.join("tasks.toml"),
        &dir.join("tasks-submit-restarted.log"),
    );
    tokio::time::timeout(Duration::from_secs(50), poll_entered.notified())
        .await
        .expect("poll reached exchange after submit recovery");
    let live: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM tasks.attempts WHERE status IN ('queued','running')",
    )
    .fetch_one(database.pool())
    .await
    .expect("live poll state");
    assert_eq!(live, 1);
    process.kill().await.expect("kill during poll");
    process.wait().await.expect("poll process exit");
    release_poll.notify_one();
    let mut process = launch(
        &dir.join("tasks.toml"),
        &dir.join("tasks-poll-restarted.log"),
    );
    tokio::time::timeout(Duration::from_secs(50), intake_notice_entered.notified())
        .await
        .expect("intake notice reached GitHub after poll recovery");
    let sending: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM tasks.publication_comments WHERE kind='notice' AND status='sending'",
    )
    .fetch_one(database.pool())
    .await
    .expect("notice state");
    assert_eq!(sending, 1);
    process.kill().await.expect("kill during notice write");
    process.wait().await.expect("notice process exit");
    release_intake_notice.notify_one();
    let mut process = launch(
        &dir.join("tasks.toml"),
        &dir.join("tasks-notice-restarted.log"),
    );
    let completed = tokio::time::timeout(Duration::from_secs(60), async {
        loop {
            if let Some(exit) = process.try_wait().expect("task status") {
                panic!(
                    "task process exited {exit}: {}",
                    fs::read_to_string(dir.join("tasks-notice-restarted.log")).expect("task log")
                );
            }
            let state = task(
                &database,
                IssueKey {
                    repository_id: 8,
                    number: 3,
                },
            )
            .await
            .expect("task");
            if state
                .as_ref()
                .is_some_and(|state| state.stage == Stage::Plan)
            {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    })
    .await;
    if completed.is_err() {
        let requests: Vec<(String, String)> = sqlx::query_as(
            "SELECT status,COALESCE(reason,'') FROM tasks.requests ORDER BY source_at",
        )
        .fetch_all(database.pool())
        .await
        .expect("request diagnostics");
        let jobs: Vec<(String, String)> = sqlx::query_as(
            "SELECT task_identifier,last_error FROM graphile_worker.jobs WHERE last_error IS NOT NULL",
        ).fetch_all(database.pool()).await.expect("job diagnostics");
        let current = task(
            &database,
            IssueKey {
                repository_id: 8,
                number: 3,
            },
        )
        .await
        .expect("task diagnostics");
        panic!(
            "runtime timeout: requests={requests:?}, jobs={jobs:?}, task={current:?}, log={}",
            fs::read_to_string(dir.join("tasks-notice-restarted.log")).expect("task log")
        );
    }
    process.kill().await.expect("stop task process");
    process.wait().await.expect("process exit");
    let mut process = launch(&dir.join("tasks.toml"), &dir.join("tasks-restarted.log"));
    stream
        .publish("github.issue_comment.created", raw.into())
        .await
        .expect("redeliver start")
        .await
        .expect("redelivery ack");
    tokio::time::timeout(Duration::from_secs(30), async {
        loop {
            if let Some(exit) = process.try_wait().expect("restarted status") {
                panic!("restarted task exited {exit}");
            }
            let retire: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM tasks.effects WHERE kind='retire' AND status='settled'",
            )
            .fetch_one(database.pool())
            .await
            .expect("retirement state");
            let archive_verified: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM tasks.archive_uploads WHERE status='verified'",
            )
            .fetch_one(database.pool())
            .await
            .expect("archive state");
            let notices_written: i64 = sqlx::query_scalar(
                "SELECT count(*) FROM tasks.publication_comments WHERE kind='notice' AND status='written'"
            ).fetch_one(database.pool()).await.expect("notice state");
            if retire == 1 && archive_verified >= 1 && notices_written >= 2 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    })
    .await
    .expect("restarted effects settled");
    process.kill().await.expect("stop restarted process");
    process.wait().await.expect("restarted exit");
    let (attempts, finished): (i64, i64) = sqlx::query_as(
        "SELECT count(*),count(*) FILTER (WHERE status='finished') FROM tasks.attempts \
         WHERE id IN (SELECT attempt_id FROM tasks.intake_reports)",
    )
    .fetch_one(database.pool())
    .await
    .expect("attempts");
    assert_eq!((attempts, finished), (1, 1));
    assert_eq!(
        github
            .lock()
            .await
            .dispatches
            .iter()
            .filter(|id| **id == session)
            .count(),
        1
    );
    let comments = github.lock().await.comments.clone();
    let managed: Vec<(String, String)> =
        sqlx::query_as("SELECT kind,marker FROM tasks.publication_comments WHERE status='written' ORDER BY kind,marker")
            .fetch_all(database.pool())
            .await
            .expect("managed comments");
    assert_eq!(
        managed
            .iter()
            .filter(|(kind, _)| kind == "dashboard")
            .count(),
        1
    );
    assert!(managed.iter().filter(|(kind, _)| kind == "notice").count() >= 2);
    for (_, marker) in managed {
        assert_eq!(
            comments
                .iter()
                .filter(|comment| comment["body"]
                    .as_str()
                    .is_some_and(|body| body.contains(&marker)))
                .count(),
            1,
            "one GitHub comment per marker"
        );
    }
    let report_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM tasks.intake_reports WHERE accepted")
            .fetch_one(database.pool())
            .await
            .expect("report");
    assert_eq!(report_count, 1);
    let fact_count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM events.outbox WHERE type LIKE 'turn.%.finished'")
            .fetch_one(database.pool())
            .await
            .expect("finished fact");
    assert!(fact_count >= 1);
    let stored = objects.lock().await;
    assert!(stored.values().any(|bytes| bytes == &archive_bytes));
    drop(stored);
    let mut process = launch(&dir.join("tasks.toml"), &dir.join("tasks-composed.log"));
    tokio::time::timeout(Duration::from_secs(90), async {
        loop {
            let current = task(
                &database,
                IssueKey {
                    repository_id: 8,
                    number: 3,
                },
            )
            .await
            .expect("task")
            .expect("started task");
            if current.stage == Stage::Planned {
                break;
            }
            if let Some(exit) = process.try_wait().expect("task status") {
                panic!("composed task exited {exit}");
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    })
    .await
    .unwrap_or_else(|_| {
        panic!(
            "planning timed out: {}",
            fs::read_to_string(dir.join("tasks-composed.log")).expect("task log")
        )
    });
    let plan_id: String = sqlx::query_scalar(
        "SELECT plan_id FROM tasks.plan_reports WHERE accepted AND current LIMIT 1",
    )
    .fetch_one(database.pool())
    .await
    .expect("reviewed plan ID");
    let approval_body = format!("/concurro approve {plan_id}");
    github.lock().await.comments.push(json!({
        "id":6,"body":approval_body,
        "user":{"id":10,"login":"writer","type":"User"},"author_association":"OWNER",
        "created_at":"2026-11-19T10:02:00Z","updated_at":"2026-11-19T10:02:00Z",
        "html_url":"https://example.test/acme/demo/issues/3#issuecomment-6"
    }));
    event["id"] = json!(Uuid::now_v7());
    event["data"]["delivery_id"] = json!("approval-delivery");
    event["data"]["comment"]["id"] = json!(6);
    event["data"]["comment"]["body"] = json!(approval_body);
    event["data"]["comment"]["created_at"] = json!("2026-11-19T10:02:00Z");
    event["data"]["comment"]["updated_at"] = json!("2026-11-19T10:02:00Z");
    event["data"]["received_at"] = json!("2026-11-19T10:03:00Z");
    event["occurred_at"] = json!("2026-11-19T10:03:00Z");
    let approval = serde_json::to_vec(&event).expect("approval event");
    decode::<GithubEvent<IssueCommentPayload>>(&approval).expect("approval contract");
    stream
        .publish("github.issue_comment.created", approval.into())
        .await
        .expect("publish approval")
        .await
        .expect("approval ack");
    tokio::time::timeout(Duration::from_secs(100), review_entered.notified())
        .await
        .unwrap_or_else(|_| {
            panic!(
                "review write not reached: {}",
                fs::read_to_string(dir.join("tasks-composed.log")).expect("task log")
            )
        });
    process.kill().await.expect("kill during review write");
    process.wait().await.expect("review process exit");
    release_review.notify_one();
    let mut process = launch(
        &dir.join("tasks.toml"),
        &dir.join("tasks-review-restarted.log"),
    );
    let ready = tokio::time::timeout(Duration::from_secs(120), async {
        loop {
            let current = task(
                &database,
                IssueKey {
                    repository_id: 8,
                    number: 3,
                },
            )
            .await
            .expect("task")
            .expect("started task");
            if matches!(
                current.stage,
                Stage::Ready | Stage::Blocked | Stage::Escalated
            ) {
                break;
            }
            if let Some(exit) = process.try_wait().expect("task status") {
                panic!("composed task exited {exit}");
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    })
    .await;
    if ready.is_err() {
        let jobs: Vec<(String, String)> = sqlx::query_as(
            "SELECT task_identifier,last_error FROM graphile_worker.jobs WHERE last_error IS NOT NULL"
        ).fetch_all(database.pool()).await.expect("job diagnostics");
        let current = task(
            &database,
            IssueKey {
                repository_id: 8,
                number: 3,
            },
        )
        .await
        .expect("task diagnostics");
        panic!(
            "composed runtime timeout: task={current:?}, jobs={jobs:?}, turns={:?}, log={}",
            *turns.lock().await,
            fs::read_to_string(dir.join("tasks-review-restarted.log")).expect("task log")
        );
    }
    let current = task(
        &database,
        IssueKey {
            repository_id: 8,
            number: 3,
        },
    )
    .await
    .expect("task diagnostics")
    .expect("task");
    if current.stage != Stage::Ready {
        let reports: Vec<(String, Option<String>)> = sqlx::query_as(
            "SELECT status,validation_error FROM tasks.implementation_reports ORDER BY recorded_at",
        )
        .fetch_all(database.pool())
        .await
        .expect("implementation reports");
        panic!(
            "composed task stopped: stage={:?}, reason={:?}, reports={reports:?}, turns={:?}, log={}",
            current.stage,
            current.stage_reason,
            *turns.lock().await,
            fs::read_to_string(dir.join("tasks-review-restarted.log")).expect("task log")
        );
    }
    process.kill().await.expect("stop composed task");
    process.wait().await.expect("composed process exit");
    let fixture = github.lock().await;
    assert_eq!(fixture.pulls.len(), 1);
    assert_eq!(fixture.reviews.len(), 1);
    assert_eq!(fixture.ready_calls, 1);
    let stages: Vec<String> = sqlx::query_scalar(
        "SELECT DISTINCT message->'data'->>'stage' FROM events.outbox \
         WHERE type LIKE 'task.%.state' AND message->'data'->>'stage' IS NOT NULL",
    )
    .fetch_all(database.pool())
    .await
    .expect("task stage history");
    for expected in [
        "intake",
        "plan",
        "plan_qc",
        "planned",
        "implement",
        "publish",
        "review",
        "gates",
        "ready",
    ] {
        assert!(
            stages.iter().any(|stage| stage == expected),
            "missing {expected} stage"
        );
    }
    for (table, sql) in [
        (
            "plan_reports",
            "SELECT count(*) FROM tasks.plan_reports WHERE accepted",
        ),
        (
            "plan_review_batches",
            "SELECT count(*) FROM tasks.plan_review_batches WHERE status='settled'",
        ),
        (
            "plan_approvals",
            "SELECT count(*) FROM tasks.plan_approvals",
        ),
        (
            "implementation_reports",
            "SELECT count(*) FROM tasks.implementation_reports WHERE status='accepted'",
        ),
        (
            "git_publications",
            "SELECT count(*) FROM tasks.git_publications",
        ),
        (
            "pr_review_batches",
            "SELECT count(*) FROM tasks.pr_review_batches WHERE status='settled'",
        ),
        (
            "pr_review_publications",
            "SELECT count(*) FROM tasks.pr_review_publications WHERE status='confirmed'",
        ),
        (
            "pr_ready_intents",
            "SELECT count(*) FROM tasks.pr_ready_intents WHERE status='confirmed'",
        ),
    ] {
        let count: i64 = sqlx::query_scalar(sql)
            .fetch_one(database.pool())
            .await
            .expect("stage record");
        assert_eq!(count, 1, "one {table} record");
    }
    github_server.abort();
    exchange_server.abort();
    archive_server.abort();
}
