//! Browser authority and read API integration over real routers and PostgreSQL.

use std::{
    collections::HashMap,
    process::Command,
    sync::{
        Arc, Mutex, RwLock,
        atomic::{AtomicBool, AtomicI64, AtomicUsize, Ordering},
    },
};

use axum::{
    Json, Router,
    body::to_bytes,
    extract::State,
    http::{Method, Request, StatusCode},
    response::{IntoResponse, Response},
    routing::any,
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use chrono::{Duration, Utc};
use concurro_accounts::{
    AccountState, AuthorityConfig, browser_router, github::Access, http_client, internal_router,
    logout::Logout, oauth::OAuth, sessions::Sessions, targets::Targets,
};
use concurro_accounts_postgres as accounts_storage;
use concurro_backoffice_api::{
    Target,
    api::{ApiState, router as read_router},
    model,
};
use concurro_db::Database;
use concurro_github_v1::AppClient;
use concurro_testkit::PostgresContainer;
use graphile_worker::WorkerOptions;
use reqwest::{Client, Url};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::types::Json as SqlJson;
use uuid::Uuid;

/// Mutable fake dependencies for the local authority flow.
#[derive(Default)]
struct Fake {
    /// Challenge expected from the browser's last authorization redirect.
    challenge: Mutex<Option<String>>,
    /// Fail GitHub token and resource reads.
    github_down: AtomicBool,
    /// Fail the task application's target snapshot.
    targets_down: AtomicBool,
    /// Fail remote OAuth token revocation.
    revocation_down: AtomicBool,
    /// Count DELETE revocation attempts.
    revoke_attempts: AtomicUsize,
}

/// Serve OAuth, GitHub REST and task snapshot responses locally.
async fn fake(State(fake): State<Arc<Fake>>, request: Request<axum::body::Body>) -> Response {
    if request
        .headers()
        .get("user-agent")
        .and_then(|value| value.to_str().ok())
        .is_none()
    {
        return StatusCode::BAD_REQUEST.into_response();
    }
    let path = request.uri().path().to_owned();
    let method = request.method().clone();
    let auth = request
        .headers()
        .get("authorization")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .to_owned();
    if path == "/v1/targets" {
        return if fake.targets_down.load(Ordering::SeqCst) {
            StatusCode::SERVICE_UNAVAILABLE.into_response()
        } else {
            Json(json!({"revision":3,"targets":[{"repository_id":8,
                "upstream_repository_id":null,"upstream":"acme/upstream",
                "fork":"acme/fork","issue_repository":"acme/issues",
                "reviewer_user_id":null,"launcher":{"workflow":"executor.yml",
                "ref":"refs/heads/main"}}]}))
            .into_response()
        };
    }
    if path == "/v1/history" {
        return Json(json!({"plans":[]})).into_response();
    }
    if path == "/login/oauth/access_token" {
        if fake.github_down.load(Ordering::SeqCst) {
            return StatusCode::SERVICE_UNAVAILABLE.into_response();
        }
        if request
            .headers()
            .get("accept")
            .and_then(|value| value.to_str().ok())
            != Some("application/json")
        {
            return StatusCode::NOT_ACCEPTABLE.into_response();
        }
        let bytes = to_bytes(request.into_body(), 4096).await.unwrap();
        let body = String::from_utf8(bytes.to_vec()).unwrap();
        let fields: HashMap<_, _> = body
            .split('&')
            .filter_map(|part| part.split_once('='))
            .collect();
        if fields.get("client_id") != Some(&"client")
            || fields.get("client_secret") != Some(&"secret")
        {
            return StatusCode::UNAUTHORIZED.into_response();
        }
        let verifier = fields.get("code_verifier").copied().unwrap_or("");
        let challenge = URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()));
        if fake.challenge.lock().unwrap().as_deref() != Some(challenge.as_str()) {
            return StatusCode::BAD_REQUEST.into_response();
        }
        let code = fields.get("code").copied().unwrap_or("unknown");
        if code == "refused" {
            return Json(json!({"error":"bad_verification_code"})).into_response();
        }
        return Json(json!({"access_token":format!("user-{code}"),
            "token_type":"bearer","expires_in":3600}))
        .into_response();
    }
    if path == "/applications/client/token" && method == Method::DELETE {
        fake.revoke_attempts.fetch_add(1, Ordering::SeqCst);
        return if fake.revocation_down.load(Ordering::SeqCst) {
            StatusCode::SERVICE_UNAVAILABLE.into_response()
        } else {
            StatusCode::NO_CONTENT.into_response()
        };
    }
    if fake.github_down.load(Ordering::SeqCst) {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    }
    let role = auth.strip_prefix("Bearer user-").unwrap_or("");
    if path == "/user" {
        return if role.is_empty() {
            StatusCode::UNAUTHORIZED.into_response()
        } else {
            Json(
                json!({"id":if role == "writer" {42} else if role == "reader" {43} else {44},
            "login":role,"name":null,"avatar_url":null}),
            )
            .into_response()
        };
    }
    if auth.starts_with("Bearer user-") {
        if role == "revoked" {
            return StatusCode::UNAUTHORIZED.into_response();
        }
        if role == "writer" && matches!(path.as_str(), "/repos/acme/upstream" | "/repos/acme/fork")
        {
            return Json(json!({"full_name":&path[7..]})).into_response();
        }
        if path == "/repos/acme/issues/issues/7" && role != "outsider" {
            return Json(json!({"number":7})).into_response();
        }
        return StatusCode::NOT_FOUND.into_response();
    }
    if path == "/repos/acme/upstream/installation" {
        return Json(json!({"id":7,"repository_selection":"selected",
            "account":{"login":"acme"}}))
        .into_response();
    }
    if path == "/app/installations/7/access_tokens" {
        return Json(
            json!({"token":"installation-read","permissions":{"contents":"read"},
            "expires_at":(Utc::now()+Duration::minutes(55)).to_rfc3339()}),
        )
        .into_response();
    }
    if path == "/installation/repositories" {
        return Json(json!({"total_count":1,"repositories":[{"full_name":"acme/upstream"}]}))
            .into_response();
    }
    if path == "/repos/acme/upstream/collaborators/writer/permission" {
        return Json(json!({"permission":"write"})).into_response();
    }
    StatusCode::NOT_FOUND.into_response()
}

/// Bind a test router to an ephemeral local port.
async fn serve(router: Router) -> (String, tokio::task::JoinHandle<Result<(), std::io::Error>>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let task = tokio::spawn(async move { axum::serve(listener, router).await });
    (url, task)
}

/// Extract one cookie's value from a response.
fn cookie(response: &reqwest::Response, name: &str) -> String {
    response
        .headers()
        .get_all("set-cookie")
        .iter()
        .filter_map(|value| value.to_str().ok())
        .find_map(|value| value.strip_prefix(&format!("{name}=")))
        .and_then(|value| value.split(';').next())
        .unwrap_or_else(|| panic!("missing {name} cookie"))
        .to_owned()
}

/// Decode the message shown by the browser's sign-in page.
fn signin_message(response: &reqwest::Response) -> String {
    let location = response.headers()["location"].to_str().unwrap();
    let url = Url::parse(&format!("http://example.test{location}")).unwrap();
    url.query_pairs()
        .find(|(key, _)| key == "error")
        .unwrap()
        .1
        .into_owned()
}

/// Complete a PKCE login and return independent session and viewer credentials.
async fn sign_in(client: &Client, browser: &str, fake: &Fake, role: &str) -> (String, String) {
    let response = client
        .get(format!("{browser}/auth/login?next=%2Ftasks"))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::FOUND);
    let login_cookie = cookie(&response, "concurro_login");
    let location = Url::parse(
        response
            .headers()
            .get("location")
            .unwrap()
            .to_str()
            .unwrap(),
    )
    .unwrap();
    let query: HashMap<_, _> = location
        .query_pairs()
        .map(|(key, value)| (key.to_string(), value.to_string()))
        .collect();
    assert_eq!(query["code_challenge_method"], "S256");
    *fake.challenge.lock().unwrap() = Some(query["code_challenge"].clone());
    let callback = client
        .get(format!("{browser}/auth/callback"))
        .query(&[("code", role), ("state", query["state"].as_str())])
        .header("cookie", format!("concurro_login={login_cookie}"))
        .send()
        .await
        .unwrap();
    assert_eq!(callback.status(), StatusCode::FOUND);
    assert_eq!(callback.headers()["location"], "/tasks");
    let replay = client
        .get(format!("{browser}/auth/callback"))
        .query(&[("code", role), ("state", query["state"].as_str())])
        .header("cookie", format!("concurro_login={login_cookie}"))
        .send()
        .await
        .unwrap();
    assert_eq!(
        signin_message(&replay),
        "sign-in expired or was started in another browser; please sign in again"
    );
    let guarded = client
        .get(format!("{browser}/auth/login?next=%2F%5Cevil.test"))
        .send()
        .await
        .unwrap();
    let guarded_cookie = cookie(&guarded, "concurro_login");
    let guarded_url = Url::parse(guarded.headers()["location"].to_str().unwrap()).unwrap();
    let guarded_query: HashMap<_, _> = guarded_url
        .query_pairs()
        .map(|(key, value)| (key.to_string(), value.to_string()))
        .collect();
    *fake.challenge.lock().unwrap() = Some(guarded_query["code_challenge"].clone());
    let guarded_callback = client
        .get(format!("{browser}/auth/callback"))
        .query(&[
            ("code", "writer"),
            ("state", guarded_query["state"].as_str()),
        ])
        .header("cookie", format!("concurro_login={guarded_cookie}"))
        .send()
        .await
        .unwrap();
    assert_eq!(guarded_callback.headers()["location"], "/");
    (
        cookie(&callback, "concurro_session"),
        cookie(&callback, "concurro_viewer"),
    )
}

#[tokio::test]
async fn browser_and_read_authority_are_scoped_and_logout_is_durable() {
    let _ = tracing_subscriber::fmt::try_init();
    let postgres = PostgresContainer::start().await.unwrap();
    let database = Database::connect(postgres.url(), 8).await.unwrap();
    accounts_storage::migrate(&database).await.unwrap();
    model::migrate(&database).await.unwrap();
    sqlx::query("UPDATE backoffice.model_state SET ready=true WHERE singleton")
        .execute(database.pool())
        .await
        .unwrap();
    let task = Uuid::now_v7();
    for (entity, id, body) in [
        (
            "contribution_task",
            task.to_string(),
            json!({"task_id":task,"repository_id":8,
            "issue_number":7,"state":{"stage":"Ready","stage_reason":null}}),
        ),
        (
            "turn",
            Uuid::now_v7().to_string(),
            json!({"task_id":task,"submit_body_base64":"private prompt"}),
        ),
        (
            "consent",
            "mine".to_owned(),
            json!({"repository_id":8,"issue_number":7,
            "fact":{"actor":{"id":43}}}),
        ),
        (
            "consent",
            "theirs".to_owned(),
            json!({"repository_id":8,"issue_number":7,
            "fact":{"actor":{"id":99}}}),
        ),
    ] {
        sqlx::query("INSERT INTO backoffice.model_items (entity,entity_id,revision,body) VALUES ($1,$2,1,$3)")
            .bind(entity).bind(id).bind(SqlJson(body)).execute(database.pool()).await.unwrap();
    }
    let fake_state = Arc::new(Fake::default());
    let (fake_url, fake_task) = serve(
        Router::new()
            .fallback(any(fake))
            .with_state(fake_state.clone()),
    )
    .await;
    let client = http_client().unwrap();
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
    let app = AppClient::new(client.clone(), &fake_url, 42, &key.stdout).unwrap();
    let oauth = OAuth::new(
        client.clone(),
        "client".into(),
        "secret".into(),
        "http://example.test/auth/callback".into(),
        format!("{fake_url}/authorize"),
        format!("{fake_url}/login/oauth/access_token"),
        fake_url.clone(),
    );
    let clock = Arc::new(AtomicI64::new(Utc::now().timestamp()));
    let clock_value = clock.clone();
    let sessions = Sessions::with_clock(Arc::new(move || clock_value.load(Ordering::SeqCst)));
    let targets = Targets::with_refresh_interval(
        client.clone(),
        fake_url.clone(),
        std::time::Duration::from_millis(10),
    );
    fake_state.targets_down.store(true, Ordering::SeqCst);
    assert!(targets.refresh().await.is_err());
    fake_state.targets_down.store(false, Ordering::SeqCst);
    targets.refresh().await.unwrap();
    assert_eq!(targets.revision().await, Some(3));
    let logout = Logout::new(database.clone(), &[7; 32], oauth.clone()).unwrap();
    let cold_state = AccountState::new(
        sessions.clone(),
        oauth.clone(),
        Access::new(app.clone()),
        Targets::new(client.clone(), fake_url.clone()),
        logout.clone(),
        AuthorityConfig {
            bearer: "read-service".into(),
            base_path: "".into(),
            secure_cookies: false,
        },
    );
    let state = AccountState::new(
        sessions.clone(),
        oauth.clone(),
        Access::new(app),
        targets,
        logout.clone(),
        AuthorityConfig {
            bearer: "read-service".into(),
            base_path: "".into(),
            secure_cookies: false,
        },
    );
    let (browser, browser_task) = serve(browser_router(state.clone())).await;
    let (internal, internal_task) = serve(internal_router(state)).await;
    let (cold_internal, cold_task) = serve(internal_router(cold_state)).await;
    let (read, read_task) = serve(read_router(ApiState {
        database: database.clone(),
        client: client.clone(),
        authority_url: internal.clone(),
        authority_bearer: "read-service".into(),
        task_api_url: fake_url.clone(),
        task_read_bearer: "read-task".into(),
        targets: Arc::new(RwLock::new(vec![Target {
            repository_id: 8,
            upstream_repository_id: None,
            upstream: "acme/upstream".into(),
            fork: "acme/fork".into(),
            issue_repository: "acme/issues".into(),
        }])),
    }))
    .await;
    assert_eq!(
        client
            .get(format!("{browser}/internal/v1/viewer"))
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::NOT_FOUND
    );
    let redirect = client
        .get(format!("{browser}/auth/login?next=https%3A%2F%2Fevil.test"))
        .send()
        .await
        .unwrap();
    assert_eq!(redirect.status(), StatusCode::FOUND);
    let bad_login = cookie(&redirect, "concurro_login");
    let bad_callback = client
        .get(format!("{browser}/auth/callback?code=writer&state=wrong"))
        .header("cookie", format!("concurro_login={bad_login}"))
        .send()
        .await
        .unwrap();
    assert_eq!(
        signin_message(&bad_callback),
        "sign-in expired or was started in another browser; please sign in again"
    );
    let replay = client
        .get(format!("{browser}/auth/callback?code=writer&state=wrong"))
        .header("cookie", format!("concurro_login={bad_login}"))
        .send()
        .await
        .unwrap();
    assert_eq!(
        signin_message(&replay),
        "sign-in expired or was started in another browser; please sign in again"
    );
    let missing_cookie = client
        .get(format!("{browser}/auth/callback?code=writer&state=missing"))
        .send()
        .await
        .unwrap();
    assert_eq!(
        signin_message(&missing_cookie),
        "sign-in expired or was started in another browser; please sign in again"
    );
    let expiring_login = client
        .get(format!("{browser}/auth/login"))
        .send()
        .await
        .unwrap();
    let expiring_cookie = cookie(&expiring_login, "concurro_login");
    let expiring_url = Url::parse(expiring_login.headers()["location"].to_str().unwrap()).unwrap();
    let expiring_query: HashMap<_, _> = expiring_url
        .query_pairs()
        .map(|(key, value)| (key.to_string(), value.to_string()))
        .collect();
    clock.fetch_add(601, Ordering::SeqCst);
    let expired_login = client
        .get(format!("{browser}/auth/callback"))
        .query(&[
            ("code", "writer"),
            ("state", expiring_query["state"].as_str()),
        ])
        .header("cookie", format!("concurro_login={expiring_cookie}"))
        .send()
        .await
        .unwrap();
    assert_eq!(
        signin_message(&expired_login),
        "sign-in expired or was started in another browser; please sign in again"
    );
    clock.fetch_sub(601, Ordering::SeqCst);
    let refused_login = client
        .get(format!("{browser}/auth/login"))
        .send()
        .await
        .unwrap();
    let refused_cookie = cookie(&refused_login, "concurro_login");
    let refused_url = Url::parse(refused_login.headers()["location"].to_str().unwrap()).unwrap();
    let refused_query: HashMap<_, _> = refused_url
        .query_pairs()
        .map(|(key, value)| (key.to_string(), value.to_string()))
        .collect();
    *fake_state.challenge.lock().unwrap() = Some(refused_query["code_challenge"].clone());
    let refused = client
        .get(format!("{browser}/auth/callback"))
        .query(&[
            ("code", "refused"),
            ("state", refused_query["state"].as_str()),
        ])
        .header("cookie", format!("concurro_login={refused_cookie}"))
        .send()
        .await
        .unwrap();
    assert_eq!(
        signin_message(&refused),
        "GitHub refused the sign-in; please sign in again"
    );
    assert!(
        !refused
            .headers()
            .get_all("set-cookie")
            .iter()
            .filter_map(|value| value.to_str().ok())
            .any(|value| value.starts_with("concurro_session="))
    );
    let (writer_session, writer_viewer) = sign_in(&client, &browser, &fake_state, "writer").await;
    let me: Value = client
        .get(format!("{browser}/api/me"))
        .header("cookie", format!("concurro_session={writer_session}"))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(me["name"], "writer");
    assert_eq!(me["avatarUrl"], "");
    let (reader_session, reader_viewer) = sign_in(&client, &browser, &fake_state, "reader").await;
    let (_, outsider_viewer) = sign_in(&client, &browser, &fake_state, "outsider").await;
    let issue = format!(
        "{internal}/internal/v1/viewer?upstream=acme%2Fupstream&issue_repository=acme%2Fissues&issue_number=7"
    );
    tokio::time::sleep(std::time::Duration::from_millis(15)).await;
    fake_state.targets_down.store(true, Ordering::SeqCst);
    assert_eq!(
        client
            .get(issue.replace(&internal, &cold_internal))
            .bearer_auth("read-service")
            .header("x-concurro-viewer", &writer_viewer)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::SERVICE_UNAVAILABLE
    );
    assert_eq!(
        client
            .get(&issue)
            .bearer_auth("read-service")
            .header("x-concurro-viewer", &writer_viewer)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    assert_eq!(client.get(format!("{internal}/internal/v1/viewer?upstream=other%2Frepo&issue_repository=acme%2Fissues&issue_number=7"))
        .bearer_auth("read-service").header("x-concurro-viewer",&writer_viewer)
        .send().await.unwrap().status(),StatusCode::NOT_FOUND);
    fake_state.targets_down.store(false, Ordering::SeqCst);
    assert_eq!(
        client
            .get(&issue)
            .header("x-concurro-viewer", &writer_viewer)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        client
            .get(&issue)
            .bearer_auth("wrong")
            .header("x-concurro-viewer", &writer_viewer)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        client
            .get(&issue)
            .bearer_auth("read-service")
            .header("x-concurro-viewer", "unknown")
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(client.get(format!("{internal}/internal/v1/viewer?upstream=other%2Frepo&issue_repository=acme%2Fissues&issue_number=7"))
        .bearer_auth("read-service").header("x-concurro-viewer",&writer_viewer).send().await.unwrap().status(),StatusCode::NOT_FOUND);
    for (viewer, expected) in [
        (&writer_viewer, "writer"),
        (&reader_viewer, "reader"),
        (&outsider_viewer, "none"),
    ] {
        let body: Value = client
            .get(&issue)
            .bearer_auth("read-service")
            .header("x-concurro-viewer", viewer)
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        assert_eq!(body["tier"], expected, "{body}");
    }
    fake_state.github_down.store(true, Ordering::SeqCst);
    assert_eq!(
        client
            .get(issue.replace("issue_number=7", "issue_number=8"))
            .bearer_auth("read-service")
            .header("x-concurro-viewer", &reader_viewer)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::SERVICE_UNAVAILABLE
    );
    let unavailable_login = client
        .get(format!("{browser}/auth/login"))
        .send()
        .await
        .unwrap();
    let unavailable_cookie = cookie(&unavailable_login, "concurro_login");
    let unavailable_url =
        Url::parse(unavailable_login.headers()["location"].to_str().unwrap()).unwrap();
    let unavailable_query: HashMap<_, _> = unavailable_url
        .query_pairs()
        .map(|(key, value)| (key.to_string(), value.to_string()))
        .collect();
    let unavailable_callback = client
        .get(format!("{browser}/auth/callback"))
        .query(&[
            ("code", "reader"),
            ("state", unavailable_query["state"].as_str()),
        ])
        .header("cookie", format!("concurro_login={unavailable_cookie}"))
        .send()
        .await
        .unwrap();
    assert_eq!(
        signin_message(&unavailable_callback),
        "GitHub sign-in is unavailable; please sign in again"
    );
    fake_state.github_down.store(false, Ordering::SeqCst);
    let (revoked_session, revoked_viewer) =
        sign_in(&client, &browser, &fake_state, "revoked").await;
    assert_eq!(
        client
            .get(&issue)
            .bearer_auth("read-service")
            .header("x-concurro-viewer", &revoked_viewer)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        client
            .get(format!("{browser}/api/me"))
            .header("cookie", format!("concurro_session={revoked_session}"))
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    let revoked_read: Value = client
        .get(format!("{read}/api/v2/tasks/{task}"))
        .header("x-concurro-viewer", &revoked_viewer)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(revoked_read["error"], "viewer_expired");
    assert_eq!(
        client
            .get(&issue)
            .bearer_auth("read-service")
            .header("x-concurro-viewer", &revoked_viewer)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    let task_url = format!("{read}/api/v2/tasks/{task}");
    let writer: Value = client
        .get(&task_url)
        .header("x-concurro-viewer", &writer_viewer)
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(writer.to_string().contains("private prompt"));
    let reader: Value = client
        .get(&task_url)
        .header("x-concurro-viewer", &reader_viewer)
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(!reader.to_string().contains("private prompt"));
    let own_consent = reader["own_consent"].as_array().expect("own consent array");
    assert_eq!(own_consent.len(), 1, "{reader}");
    assert_eq!(own_consent[0]["fact"]["actor"]["id"], 43, "{reader}");
    assert_eq!(
        client
            .get(&task_url)
            .header("x-concurro-viewer", &outsider_viewer)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        client
            .get(format!("{read}/api/v2/targets"))
            .header("x-concurro-viewer", &writer_viewer)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    fake_state.revocation_down.store(true, Ordering::SeqCst);
    let logged_out = client
        .post(format!("{browser}/auth/logout"))
        .header("x-concurro-request", "1")
        .header(
            "cookie",
            format!("concurro_session={writer_session}; concurro_viewer={writer_viewer}"),
        )
        .send()
        .await
        .unwrap();
    assert_eq!(logged_out.status(), StatusCode::OK);
    let cleared: Vec<_> = logged_out
        .headers()
        .get_all("set-cookie")
        .iter()
        .filter_map(|value| value.to_str().ok())
        .collect();
    assert!(
        cleared
            .iter()
            .any(|value| value.starts_with("concurro_session=;") && value.contains("Max-Age=0"))
    );
    assert!(
        cleared
            .iter()
            .any(|value| value.starts_with("concurro_viewer=;") && value.contains("Max-Age=0"))
    );
    let receipt = cookie(&logged_out, "concurro_logout");
    let body: Value = logged_out.json().await.unwrap();
    assert_eq!(body["remote"], "pending");
    let pending: Value = client
        .get(format!("{browser}/auth/logout-status"))
        .header("cookie", format!("concurro_logout={receipt}"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(pending["remote"], "pending");
    assert_eq!(
        client
            .get(format!("{browser}/api/me"))
            .header("cookie", format!("concurro_session={writer_session}"))
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(fake_state.revoke_attempts.load(Ordering::SeqCst), 1);
    let sealed: Vec<u8> =
        sqlx::query_scalar("SELECT sealed FROM accounts.logout_jobs WHERE receipt_id=$1")
            .bind(Uuid::parse_str(&receipt).unwrap())
            .fetch_one(database.pool())
            .await
            .unwrap();
    assert!(!String::from_utf8_lossy(&sealed).contains("user-writer"));
    assert_eq!(
        client
            .get(&issue)
            .bearer_auth("read-service")
            .header("x-concurro-viewer", &writer_viewer)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        client
            .get(format!("{read}/api/v2/targets"))
            .header("x-concurro-viewer", &writer_viewer)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        client
            .get(&task_url)
            .header("x-concurro-viewer", &writer_viewer)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    let wrong_key = Logout::new(database.clone(), &[8; 32], oauth.clone()).unwrap();
    assert_eq!(
        wrong_key
            .status(Some(Uuid::parse_str(&receipt).unwrap()))
            .await,
        "unknown"
    );
    fake_state.revocation_down.store(false, Ordering::SeqCst);
    let restarted_logout = Logout::new(database.clone(), &[7; 32], oauth.clone()).unwrap();
    let worker = WorkerOptions::default()
        .pg_pool(database.pool().clone())
        .schema("graphile_worker")
        .define_job::<concurro_accounts::logout::RevokeTokenJob>()
        .add_extension(restarted_logout.clone())
        .init()
        .await
        .unwrap();
    let worker_task = tokio::spawn(async move { worker.run().await });
    tokio::time::timeout(std::time::Duration::from_secs(10), async {
        loop {
            if restarted_logout
                .status(Some(Uuid::parse_str(&receipt).unwrap()))
                .await
                == "revoked"
            {
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        }
    })
    .await
    .expect("worker completed revocation after restart");
    worker_task.abort();
    let job = accounts_storage::revocation_job(&database, Uuid::parse_str(&receipt).unwrap())
        .await
        .unwrap()
        .unwrap();
    assert!(job.sealed.is_none());
    assert_eq!(
        logout
            .status(Some(Uuid::parse_str(&receipt).unwrap()))
            .await,
        "revoked"
    );
    let complete: Value = client
        .get(format!("{browser}/auth/logout-status"))
        .header("cookie", format!("concurro_logout={receipt}"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(complete["remote"], "revoked");
    let (expiring_session, expiring_viewer) =
        sign_in(&client, &browser, &fake_state, "reader").await;
    clock.fetch_add(3601, Ordering::SeqCst);
    assert_eq!(
        client
            .get(&issue)
            .bearer_auth("read-service")
            .header("x-concurro-viewer", &expiring_viewer)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    assert_eq!(
        client
            .get(format!("{browser}/api/me"))
            .header("cookie", format!("concurro_session={expiring_session}"))
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNAUTHORIZED
    );
    let (db_session, db_viewer) = sign_in(&client, &browser, &fake_state, "reader").await;
    let (down_session, down_viewer) = sign_in(&client, &browser, &fake_state, "outsider").await;
    database.pool().close().await;
    let database_down = client
        .post(format!("{browser}/auth/logout"))
        .header("x-concurro-request", "1")
        .header(
            "cookie",
            format!("concurro_session={db_session}; concurro_viewer={db_viewer}"),
        )
        .send()
        .await
        .unwrap();
    assert!(
        !database_down
            .headers()
            .get_all("set-cookie")
            .iter()
            .filter_map(|value| value.to_str().ok())
            .any(|value| value.starts_with("concurro_logout="))
    );
    let database_down_body: Value = database_down.json().await.unwrap();
    assert_eq!(database_down_body["remote"], "revoked");
    let unreadable: Value = client
        .get(format!("{browser}/auth/logout-status"))
        .header("cookie", format!("concurro_logout={receipt}"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(unreadable["remote"], "unavailable");
    fake_state.revocation_down.store(true, Ordering::SeqCst);
    let unavailable = client
        .post(format!("{browser}/auth/logout"))
        .header("x-concurro-request", "1")
        .header(
            "cookie",
            format!("concurro_session={down_session}; concurro_viewer={down_viewer}"),
        )
        .send()
        .await
        .unwrap();
    let unavailable_body: Value = unavailable.json().await.unwrap();
    assert_eq!(unavailable_body["remote"], "unavailable");
    let _ = reader_session;
    cold_task.abort();
    browser_task.abort();
    internal_task.abort();
    read_task.abort();
    fake_task.abort();
}
