//! Browser commands against the real task request router and PostgreSQL.

use std::{
    collections::{BTreeMap, HashMap},
    process::Command,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
};

use axum::{
    Json, Router,
    body::to_bytes,
    extract::State,
    http::{Request, StatusCode},
    response::{IntoResponse, Response},
    routing::any,
};
use chrono::{Duration, Utc};
use concurro_accounts::{
    AccountState, AuthorityConfig, browser_router,
    commands::Commands,
    github::{Access, User},
    http_client,
    logout::Logout,
    oauth::OAuth,
    sessions::Sessions,
    targets::Targets,
};
use concurro_contracts::{
    CommandContent, CommandSubmission, ConfigLauncher, ConfigTarget, ConfigTargets, RequestActor,
    TaskIssue,
};
use concurro_db::Database;
use concurro_events_postgres::{Outbox, migrate as migrate_events};
use concurro_github_v1::AppClient;
use concurro_tasks::{
    DecisionContext, DecisionTarget,
    api::{TaskApi, router as task_router, verify_proof},
};
use concurro_tasks_postgres::{migrate as migrate_tasks, sync_config_targets};
use concurro_testkit::PostgresContainer;
use serde_json::{Value, json};
use sqlx::types::Json as SqlJson;
use uuid::Uuid;

/// Fake user and App reads, retaining GitHub's bearer distinction.
async fn github(request: Request<axum::body::Body>) -> Response {
    if request.headers().get("user-agent").is_none() {
        return StatusCode::BAD_REQUEST.into_response();
    }
    let path = request.uri().path().to_owned();
    let auth = request
        .headers()
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_owned();
    if let Some(role) = auth.strip_prefix("Bearer user-") {
        if role == "revoked" {
            return StatusCode::UNAUTHORIZED.into_response();
        }
        if let Some(repo) = path.strip_prefix("/repos/acme/") {
            if let Some(name) = repo.strip_suffix("/issues/7") {
                return if name == "issues" && role != "outsider" {
                    Json(json!({"number":7})).into_response()
                } else {
                    StatusCode::NOT_FOUND.into_response()
                };
            }
            if !repo.contains('/') && (role == "writer" || role == "reader") {
                return Json(json!({"id":match repo { "issues" => 8, "upstream" => 9, "fork" => 10, _ => 0 }})).into_response();
            }
        }
        return StatusCode::NOT_FOUND.into_response();
    }
    if path.ends_with("/installation") {
        return Json(json!({"id":7,"repository_selection":"selected","account":{"login":"acme"}}))
            .into_response();
    }
    if path == "/app/installations/7/access_tokens" {
        let bytes = to_bytes(request.into_body(), 4096).await.unwrap();
        let body: Value = serde_json::from_slice(&bytes).unwrap();
        let repository = body["repositories"][0].as_str().unwrap();
        return Json(
            json!({"token":format!("installation-read-{repository}"),"permissions":body["permissions"],
            "expires_at":(Utc::now()+Duration::minutes(55)).to_rfc3339()}),
        )
        .into_response();
    }
    if path == "/installation/repositories" {
        let repository = auth.strip_prefix("Bearer installation-read-").unwrap_or("");
        return Json(
            json!({"total_count":1,"repositories":[{"full_name":format!("acme/{repository}")}]}),
        )
        .into_response();
    }
    if path.ends_with("/collaborators/writer/permission") {
        return Json(json!({"permission":"write"})).into_response();
    }
    if path.ends_with("/collaborators/reader/permission") {
        return StatusCode::NOT_FOUND.into_response();
    }
    if path == "/repos/acme/issues/issues/7" {
        return Json(json!({"user":{"id":99,"login":"author"}})).into_response();
    }
    if path == "/repos/acme/upstream/issues/9" {
        return Json(json!({"user":{"id":99,"login":"author"},"pull_request":{}})).into_response();
    }
    StatusCode::NOT_FOUND.into_response()
}

/// Forward to the real task router, optionally losing its response after admission.
struct Proxy {
    client: reqwest::Client,
    task_url: String,
    drop_response: AtomicBool,
    proofs: Mutex<HashMap<Uuid, String>>,
}

/// Model a delivery proxy that can fail after the task application records the action.
async fn proxy(State(proxy): State<Arc<Proxy>>, request: Request<axum::body::Body>) -> Response {
    let action = request.headers().get("idempotency-key").cloned();
    let bytes = to_bytes(request.into_body(), 64 * 1024).await.unwrap();
    if let (Some(action), Ok(body)) = (&action, serde_json::from_slice::<Value>(&bytes))
        && let (Ok(action), Some(proof)) = (
            Uuid::parse_str(action.to_str().unwrap_or("")),
            body["proof"].as_str(),
        )
    {
        proxy
            .proofs
            .lock()
            .unwrap()
            .insert(action, proof.to_owned());
    }
    let mut forwarded = proxy
        .client
        .post(format!("{}/v1/requests", proxy.task_url))
        .body(bytes);
    if let Some(action) = action {
        forwarded = forwarded.header("idempotency-key", action);
    }
    forwarded = forwarded.header("content-type", "application/json");
    let response = forwarded.send().await.unwrap();
    if proxy.drop_response.load(Ordering::SeqCst) {
        return (StatusCode::ACCEPTED, "").into_response();
    }
    let status = response.status();
    let body = response.bytes().await.unwrap();
    (status, body).into_response()
}

/// Bind one local router and retain its server task.
async fn serve(router: Router) -> (String, tokio::task::JoinHandle<Result<(), std::io::Error>>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let task = tokio::spawn(async move { axum::serve(listener, router).await });
    (url, task)
}

/// Post an existing browser command shape with a chosen action ID.
async fn post(
    client: &reqwest::Client,
    browser: &str,
    session: &str,
    repository_and_number: &str,
    body: Value,
    action: Uuid,
    csrf: bool,
) -> reqwest::Response {
    let mut request = client
        .post(format!(
            "{browser}/api/tasks/{repository_and_number}/commands"
        ))
        .header("cookie", format!("concurro_session={session}"))
        .header("Idempotency-Key", action.to_string())
        .json(&body);
    if csrf {
        request = request.header("x-concurro-request", "1");
    }
    request.send().await.unwrap()
}

/// Exercise command authority, proof binding, durable replay and transport errors.
#[tokio::test]
async fn browser_commands_record_only_authorized_actor_and_replay_exactly() {
    let postgres = PostgresContainer::start().await.unwrap();
    let database = Database::connect(postgres.url(), 8).await.unwrap();
    migrate_events(&database).await.unwrap();
    migrate_tasks(&database).await.unwrap();
    let config = ConfigTargets {
        targets: vec![ConfigTarget {
            repository_id: 8,
            upstream_repository_id: Some(9),
            upstream: "acme/upstream".into(),
            fork: "acme/fork".into(),
            issue_repository: "acme/issues".into(),
            reviewer_user_id: Some(42),
            launcher: ConfigLauncher {
                workflow: "executor.yml".into(),
                r#ref: "refs/heads/main".into(),
            },
        }],
    };
    sync_config_targets(
        &database,
        &Outbox::new(1024, vec!["config.>".into()]),
        &config,
    )
    .await
    .unwrap();
    let key = Command::new("openssl")
        .args([
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
        ])
        .output()
        .unwrap();
    assert!(key.status.success());
    let public = Command::new("openssl")
        .args(["pkey", "-pubout"])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let mut public = public;
    use std::io::Write;
    public.stdin.take().unwrap().write_all(&key.stdout).unwrap();
    let public = public.wait_with_output().unwrap();
    assert!(public.status.success());
    let (github_url, github_task) = serve(Router::new().fallback(any(github))).await;
    let client = http_client().unwrap();
    let app = AppClient::new(client.clone(), &github_url, 42, &key.stdout).unwrap();
    let permissions = BTreeMap::from([("contents".to_owned(), "read".to_owned())]);
    let issue_target = DecisionTarget {
        repository: "acme/issues".into(),
        read_permissions: permissions.clone(),
        fork: "acme/fork".into(),
        fork_permissions: permissions.clone(),
        initialization: None,
        publication: None,
    };
    let pr_target = DecisionTarget {
        repository: "acme/upstream".into(),
        read_permissions: permissions,
        fork: "acme/fork".into(),
        fork_permissions: BTreeMap::new(),
        initialization: None,
        publication: None,
    };
    let task_api = TaskApi {
        database: database.clone(),
        decisions: DecisionContext {
            github: app.clone(),
            targets: HashMap::from([(8, issue_target)]),
            outbox: Outbox::new(1024, vec![]),
            rust_exchange: false,
        },
        standalone_targets: HashMap::from([(9, pr_target)]),
        proof_public_key: Arc::new(public.stdout.clone()),
        read_bearer: Arc::new("task-read".into()),
        delivery_bearer: None,
    };
    let (task_url, task_server) = serve(task_router(task_api)).await;
    let proxy_state = Arc::new(Proxy {
        client: client.clone(),
        task_url: task_url.clone(),
        drop_response: AtomicBool::new(false),
        proofs: Mutex::new(HashMap::new()),
    });
    let (proxy_url, proxy_server) = serve(
        Router::new()
            .fallback(any(proxy))
            .with_state(proxy_state.clone()),
    )
    .await;
    let sessions = Sessions::new();
    let oauth = OAuth::new(
        client.clone(),
        "client".into(),
        "secret".into(),
        "http://example.test/auth/callback".into(),
        format!("{github_url}/authorize"),
        format!("{github_url}/token"),
        github_url.clone(),
    );
    let logout = Logout::new(database.clone(), &[7; 32], oauth.clone()).unwrap();
    let signer = Commands::new(client.clone(), proxy_url.clone(), &key.stdout)
        .unwrap()
        .with_proof_seconds(3);
    let targets = Targets::new(client.clone(), task_url.clone());
    targets.refresh().await.unwrap();
    let state = AccountState::new(
        sessions.clone(),
        oauth,
        Access::new(app),
        targets,
        logout,
        AuthorityConfig {
            bearer: "read-service".into(),
            base_path: "".into(),
            secure_cookies: false,
        },
    )
    .with_commands(signer.clone());
    let (browser, browser_server) = serve(browser_router(state)).await;
    let writer = sessions.create(
        "user-writer".into(),
        User {
            id: 42,
            login: "writer".into(),
            name: None,
            avatar_url: None,
        },
        3600,
    );
    let reader = sessions.create(
        "user-reader".into(),
        User {
            id: 43,
            login: "reader".into(),
            name: None,
            avatar_url: None,
        },
        3600,
    );
    let outsider = sessions.create(
        "user-outsider".into(),
        User {
            id: 44,
            login: "outsider".into(),
            name: None,
            avatar_url: None,
        },
        3600,
    );
    let stop = json!({"command":"stop","feedback":"please stop"});
    let action = Uuid::now_v7();
    assert_eq!(
        post(
            &client,
            &browser,
            &writer.id,
            "acme/issues/7",
            stop.clone(),
            action,
            false
        )
        .await
        .status(),
        StatusCode::FORBIDDEN
    );
    assert_eq!(
        post(
            &client,
            &browser,
            &reader.id,
            "acme/issues/7",
            stop.clone(),
            Uuid::now_v7(),
            true
        )
        .await
        .status(),
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        post(
            &client,
            &browser,
            &outsider.id,
            "acme/issues/7",
            stop.clone(),
            Uuid::now_v7(),
            true
        )
        .await
        .status(),
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        post(
            &client,
            &browser,
            &outsider.id,
            "acme/issues/7",
            json!({"command":"withdraw","argument":"openai"}),
            Uuid::now_v7(),
            true
        )
        .await
        .status(),
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        sqlx::query_scalar::<_, i64>("SELECT count(*) FROM tasks.browser_actions")
            .fetch_one(database.pool())
            .await
            .unwrap(),
        0
    );
    let result = post(
        &client,
        &browser,
        &writer.id,
        "acme/issues/7",
        stop.clone(),
        action,
        true,
    )
    .await;
    assert_eq!(result.status(), StatusCode::OK);
    let first: Value = result.json().await.unwrap();
    assert_eq!(first["request_id"], action.to_string());
    let row: SqlJson<Value> = sqlx::query_scalar("SELECT request FROM tasks.requests WHERE id=$1")
        .bind(action)
        .fetch_one(database.pool())
        .await
        .unwrap();
    assert_eq!(row.0["actor"]["id"], 42);
    assert_eq!(row.0["actor"]["login"], "writer");
    let before: i64 = sqlx::query_scalar("SELECT count(*) FROM tasks.browser_actions")
        .fetch_one(database.pool())
        .await
        .unwrap();
    assert_eq!(before, 1);
    for body in [
        json!({"command":"other"}),
        json!({"command":"funding","argument":"bad org"}),
        json!({"command":"stop","argument":"unexpected"}),
        json!({"command":"withdraw"}),
        json!({"command":"resume","argument":"--description-only"}),
    ] {
        assert_eq!(
            post(
                &client,
                &browser,
                &writer.id,
                "acme/issues/7",
                body,
                Uuid::now_v7(),
                true
            )
            .await
            .status(),
            StatusCode::UNPROCESSABLE_ENTITY
        );
    }
    for (session, csrf, status, detail) in [
        (None, true, StatusCode::UNAUTHORIZED, "sign in with GitHub"),
        (
            Some(writer.id.as_str()),
            false,
            StatusCode::FORBIDDEN,
            "missing request header",
        ),
        (
            Some(writer.id.as_str()),
            true,
            StatusCode::UNPROCESSABLE_ENTITY,
            "invalid command body",
        ),
    ] {
        let mut request = client
            .post(format!("{browser}/api/tasks/acme/issues/7/commands"))
            .header("content-type", "application/json")
            .header("idempotency-key", Uuid::now_v7().to_string())
            .body("{bad");
        if let Some(session) = session {
            request = request.header("cookie", format!("concurro_session={session}"));
        }
        if csrf {
            request = request.header("x-concurro-request", "1");
        }
        let response = request.send().await.unwrap();
        assert_eq!(response.status(), status);
        assert_eq!(response.json::<Value>().await.unwrap()["detail"], detail);
    }
    assert_eq!(
        client
            .post(format!("{browser}/api/tasks/acme/issues/7/commands"))
            .header("cookie", format!("concurro_session={}", writer.id))
            .header("x-concurro-request", "1")
            .json(&stop)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::BAD_REQUEST
    );
    assert_eq!(
        client
            .post(format!("{browser}/api/repos/acme/issues/issues/7/commands"))
            .header("cookie", format!("concurro_session={}", writer.id))
            .header("idempotency-key", Uuid::now_v7().to_string())
            .json(&stop)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::FORBIDDEN
    );
    for credential in [&writer.viewer_token, "read-service", "task-read"] {
        assert_eq!(
            client
                .post(format!("{browser}/api/tasks/acme/issues/7/commands"))
                .header("cookie", format!("concurro_session={credential}"))
                .header("x-concurro-request", "1")
                .header("idempotency-key", Uuid::now_v7().to_string())
                .json(&stop)
                .send()
                .await
                .unwrap()
                .status(),
            StatusCode::UNAUTHORIZED
        );
        let submission = CommandSubmission {
            content: CommandContent {
                issue: TaskIssue {
                    repository_id: 8,
                    number: 7,
                },
                kind: "stop".into(),
                argument: json!({}),
                detail: json!({}),
                source_at: Utc::now(),
            },
            proof: credential.to_owned(),
        };
        for bearer in [None, Some(credential)] {
            let mut request = client
                .post(format!("{task_url}/v1/requests"))
                .header("idempotency-key", Uuid::now_v7().to_string())
                .json(&submission);
            if let Some(bearer) = bearer {
                request = request.bearer_auth(bearer);
            }
            let response = request.send().await.unwrap();
            assert_eq!(response.status(), StatusCode::FORBIDDEN);
            assert_eq!(
                response.json::<Value>().await.unwrap()["error"],
                "proof_invalid"
            );
        }
    }
    assert_eq!(
        sqlx::query_scalar::<_, i64>("SELECT count(*) FROM tasks.browser_actions")
            .fetch_one(database.pool())
            .await
            .unwrap(),
        before
    );
    let withdraw_action = Uuid::now_v7();
    let withdraw = post(
        &client,
        &browser,
        &reader.id,
        "acme/issues/7",
        json!({"command":"withdraw","argument":"openai"}),
        withdraw_action,
        true,
    )
    .await;
    assert_eq!(withdraw.status(), StatusCode::OK);
    let withdrawn: SqlJson<Value> =
        sqlx::query_scalar("SELECT request FROM tasks.requests WHERE id=$1")
            .bind(withdraw_action)
            .fetch_one(database.pool())
            .await
            .unwrap();
    assert_eq!(withdrawn.0["actor"]["id"], 43);
    assert_eq!(
        withdrawn.0["kind"]["Later"]["Withdraw"]["provider"],
        "openai"
    );
    let pr = post(
        &client,
        &browser,
        &writer.id,
        "acme/upstream/9",
        json!({"command":"stop"}),
        Uuid::now_v7(),
        true,
    )
    .await;
    assert_eq!(pr.status(), StatusCode::OK);
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM tasks.browser_standalone_actions")
        .fetch_one(database.pool())
        .await
        .unwrap();
    assert_eq!(count, 1);
    let review_actor: SqlJson<Value> = sqlx::query_scalar(
        "SELECT c.actor FROM tasks.standalone_review_commands c \
         JOIN tasks.browser_standalone_actions a ON a.command_id=c.id",
    )
    .fetch_one(database.pool())
    .await
    .unwrap();
    assert_eq!(review_actor.0["id"], 42);
    assert_eq!(review_actor.0["login"], "writer");
    proxy_state.drop_response.store(true, Ordering::SeqCst);
    let dropped_action = Uuid::now_v7();
    let dropped = post(
        &client,
        &browser,
        &writer.id,
        "acme/issues/7",
        json!({"command":"invalidate"}),
        dropped_action,
        true,
    )
    .await;
    assert_eq!(dropped.status(), StatusCode::BAD_GATEWAY);
    assert_eq!(
        dropped.json::<Value>().await.unwrap()["detail"],
        "the command result is unknown; retry the same action ID"
    );
    assert_eq!(
        sqlx::query_scalar::<_, i64>(
            "SELECT count(*) FROM tasks.browser_actions WHERE action_id=$1"
        )
        .bind(dropped_action)
        .fetch_one(database.pool())
        .await
        .unwrap(),
        1
    );
    proxy_state.drop_response.store(false, Ordering::SeqCst);
    let admitted_id: Uuid =
        sqlx::query_scalar("SELECT request_id FROM tasks.browser_actions WHERE action_id=$1")
            .bind(dropped_action)
            .fetch_one(database.pool())
            .await
            .unwrap();
    let retry = post(
        &client,
        &browser,
        &writer.id,
        "acme/issues/7",
        json!({"command":"invalidate"}),
        dropped_action,
        true,
    )
    .await;
    assert_eq!(retry.status(), StatusCode::OK);
    assert_eq!(
        retry.json::<Value>().await.unwrap()["request_id"],
        admitted_id.to_string()
    );
    tokio::time::sleep(std::time::Duration::from_secs(4)).await;
    let original_proof = proxy_state
        .proofs
        .lock()
        .unwrap()
        .get(&action)
        .unwrap()
        .clone();
    let original: SqlJson<Value> =
        sqlx::query_scalar("SELECT content FROM tasks.browser_actions WHERE action_id=$1")
            .bind(action)
            .fetch_one(database.pool())
            .await
            .unwrap();
    let original: CommandContent = serde_json::from_value(original.0).unwrap();
    assert!(verify_proof(&public.stdout, action, &original, &original_proof).is_err());
    let repeated = post(
        &client,
        &browser,
        &writer.id,
        "acme/issues/7",
        stop,
        action,
        true,
    )
    .await;
    assert_eq!(repeated.status(), StatusCode::OK);
    assert_eq!(
        repeated.json::<Value>().await.unwrap()["request_id"],
        first["request_id"]
    );
    let conflict = post(
        &client,
        &browser,
        &writer.id,
        "acme/issues/7",
        json!({"command":"stop","feedback":"changed"}),
        action,
        true,
    )
    .await;
    assert_eq!(conflict.status(), StatusCode::CONFLICT);
    assert_eq!(
        conflict.json::<Value>().await.unwrap()["detail"],
        "idempotency_conflict"
    );
    let repos_route = client
        .post(format!("{browser}/api/repos/acme/issues/issues/7/commands"))
        .header("cookie", format!("concurro_session={}", writer.id))
        .header("x-concurro-request", "1")
        .header("idempotency-key", Uuid::now_v7().to_string())
        .json(&json!({"command":"retry","argument":"2"}))
        .send()
        .await
        .unwrap();
    assert_eq!(repos_route.status(), StatusCode::OK);
    for (body, kind, argument) in [
        (
            json!({"command":"funding","argument":"openai org"}),
            "funding",
            json!({"provider":"openai","mode":"org"}),
        ),
        (json!({"command":"sponsor"}), "sponsor", json!({})),
        (
            json!({"command":"approve","argument":"plan-7"}),
            "approve",
            json!({"plan_id":"plan-7"}),
        ),
        (
            json!({"command":"resume"}),
            "resume",
            json!({"description_only":false}),
        ),
    ] {
        let action = Uuid::now_v7();
        assert_eq!(
            post(
                &client,
                &browser,
                &writer.id,
                "acme/issues/7",
                body,
                action,
                true
            )
            .await
            .status(),
            StatusCode::OK
        );
        let recorded: SqlJson<Value> =
            sqlx::query_scalar("SELECT content FROM tasks.browser_actions WHERE action_id=$1")
                .bind(action)
                .fetch_one(database.pool())
                .await
                .unwrap();
        assert_eq!(recorded.0["kind"], kind);
        assert_eq!(recorded.0["argument"], argument);
    }
    let count: i64 =
        sqlx::query_scalar("SELECT count(*) FROM tasks.browser_actions WHERE action_id=$1")
            .bind(action)
            .fetch_one(database.pool())
            .await
            .unwrap();
    assert_eq!(count, 1);
    assert_ne!(
        proxy_state.proofs.lock().unwrap().get(&action).unwrap(),
        &original_proof
    );
    let content = CommandContent {
        issue: TaskIssue {
            repository_id: 8,
            number: 7,
        },
        kind: "stop".into(),
        argument: json!({}),
        detail: json!({"feedback":"bound"}),
        source_at: Utc::now(),
    };
    let proof_action = Uuid::now_v7();
    let proof = signer
        .sign(
            RequestActor {
                id: 42,
                login: "writer".into(),
            },
            proof_action,
            content.clone(),
        )
        .unwrap();
    assert!(verify_proof(&public.stdout, proof_action, &content, &proof.proof).is_ok());
    assert!(verify_proof(&public.stdout, Uuid::now_v7(), &content, &proof.proof).is_err());
    assert!(
        verify_proof(
            &public.stdout,
            proof_action,
            &CommandContent {
                issue: TaskIssue {
                    repository_id: 8,
                    number: 8
                },
                ..content.clone()
            },
            &proof.proof
        )
        .is_err()
    );
    let mut injected = serde_json::to_value(&proof).unwrap();
    injected["actor"] = json!({"id":44,"login":"outsider"});
    assert_ne!(
        client
            .post(format!("{task_url}/v1/requests"))
            .header("idempotency-key", proof_action.to_string())
            .json(&injected)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::ACCEPTED
    );
    assert_eq!(
        client
            .post(format!("{task_url}/v1/requests"))
            .header("idempotency-key", Uuid::now_v7().to_string())
            .json(&proof)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::FORBIDDEN
    );
    assert!(
        verify_proof(
            &public.stdout,
            proof_action,
            &CommandContent {
                argument: json!({"provider":"openai"}),
                ..content.clone()
            },
            &proof.proof
        )
        .is_err()
    );
    assert!(
        verify_proof(
            &public.stdout,
            proof_action,
            &CommandContent {
                kind: "withdraw".into(),
                ..content.clone()
            },
            &proof.proof
        )
        .is_err()
    );
    assert!(
        verify_proof(
            &public.stdout,
            proof_action,
            &CommandContent {
                detail: json!({"feedback":"changed"}),
                ..content.clone()
            },
            &proof.proof
        )
        .is_err()
    );
    assert!(
        verify_proof(
            &public.stdout,
            proof_action,
            &CommandContent {
                source_at: Utc::now() + Duration::seconds(1),
                ..content
            },
            &proof.proof
        )
        .is_err()
    );
    proxy_server.abort();
    tokio::task::yield_now().await;
    let unsent_action = Uuid::now_v7();
    let unsent = post(
        &client,
        &browser,
        &writer.id,
        "acme/issues/7",
        json!({"command":"stop"}),
        unsent_action,
        true,
    )
    .await;
    assert_eq!(unsent.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(
        unsent.json::<Value>().await.unwrap()["detail"],
        "Concurro is unavailable; nothing was recorded"
    );
    assert_eq!(
        sqlx::query_scalar::<_, i64>(
            "SELECT count(*) FROM tasks.browser_actions WHERE action_id=$1"
        )
        .bind(unsent_action)
        .fetch_one(database.pool())
        .await
        .unwrap(),
        0
    );
    browser_server.abort();
    task_server.abort();
    github_task.abort();
}
