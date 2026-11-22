//! Python-oracle parity through the Rust browser router and a fake owner vault.

use std::{
    process::Command,
    sync::{Arc, Mutex},
};

use axum::{
    Json, Router,
    body::{Body, to_bytes},
    extract::State,
    http::{Request, StatusCode},
    response::{IntoResponse, Response},
    routing::any,
};
use concurro_accounts::{
    AccountState, AuthorityConfig, Credentials, browser_router,
    github::{Access, User},
    http_client,
    logout::Logout,
    oauth::OAuth,
    sessions::Sessions,
    targets::Targets,
};
use concurro_db::Database;
use concurro_github_v1::AppClient;
use reqwest::Client;
use serde_json::{Value, json};
use sqlx::postgres::PgPoolOptions;

/// Current expected fake response and one observed vault request.
#[derive(Default)]
struct VaultFixture {
    /// Response returned to the next owner call.
    reply: Option<Value>,
    /// Last request received by the fake vault.
    observed: Option<Value>,
}

/// Record the exact owner bearer and JSON forwarded to the fake vault.
async fn vault(State(state): State<Arc<Mutex<VaultFixture>>>, request: Request<Body>) -> Response {
    let method = request.method().as_str().to_owned();
    let path = request.uri().path().to_owned();
    let query = request
        .uri()
        .query()
        .unwrap_or("")
        .split('&')
        .filter_map(|pair| {
            pair.split_once('=')
                .map(|(key, value)| (key.to_owned(), Value::String(value.to_owned())))
        })
        .collect::<serde_json::Map<_, _>>();
    let authorization = request
        .headers()
        .get("authorization")
        .and_then(|value| value.to_str().ok())
        .map(str::to_owned);
    let body = to_bytes(request.into_body(), 1_000_000).await.unwrap();
    let body = if body.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&body).unwrap()
    };
    let mut fixture = state.lock().unwrap();
    fixture.observed = Some(json!({"method":method,"path":path,"query":query,
        "authorization":authorization,"body":body}));
    let reply = fixture.reply.clone().unwrap();
    let status = StatusCode::from_u16(reply["status"].as_u64().unwrap() as u16).unwrap();
    (status, Json(reply["body"].clone())).into_response()
}

/// Bind one local HTTP router for the browser or fake vault.
async fn serve(router: Router) -> (String, tokio::task::JoinHandle<Result<(), std::io::Error>>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let task = tokio::spawn(async move { axum::serve(listener, router).await });
    (url, task)
}

/// Make a browser session with the same shared HTTP client as production.
fn state(vault_url: String, sessions: Sessions) -> AccountState {
    let client = http_client().unwrap();
    let key = Command::new("openssl")
        .args(["genrsa", "2048"])
        .output()
        .unwrap()
        .stdout;
    let app = AppClient::new(client.clone(), &vault_url, 42, &key).unwrap();
    let oauth = OAuth::new(
        client.clone(),
        "client".to_owned(),
        "secret".to_owned(),
        "http://localhost/auth/callback".to_owned(),
        "http://localhost/oauth/authorize".to_owned(),
        "http://localhost/oauth/token".to_owned(),
        vault_url.clone(),
    );
    let database = Database::from_pool(
        PgPoolOptions::new()
            .connect_lazy("postgres://concurro:unused@127.0.0.1:1/concurro")
            .unwrap(),
    );
    let logout = Logout::new(database, &[0; 32], oauth.clone()).unwrap();
    let targets = Targets::new(client.clone(), vault_url.clone());
    let credentials = Credentials::new(
        client,
        Some(vault_url),
        Some("relay.invalid:7443".to_owned()),
        Some("https://example.invalid/relay".to_owned()),
    )
    .unwrap();
    AccountState::new(
        sessions,
        oauth,
        Access::new(app),
        targets,
        logout,
        AuthorityConfig {
            bearer: "internal".to_owned(),
            base_path: String::new(),
            secure_cookies: false,
        },
    )
    .with_credentials(credentials)
}

/// Derive the existing frontend route and body for one recorded Python call.
fn browser_call(case: &Value) -> (String, Option<Value>) {
    let request = &case["vault_request"];
    let path = request["path"].as_str().unwrap();
    let mut body = request["body"].clone();
    let browser_path = match case["name"].as_str().unwrap() {
        "list" | "add" => "/api/me/credentials".to_owned(),
        "relay_status" | "relay_status_unavailable" => "/api/me/relays".to_owned(),
        "relay_add" | "relay_add_two_routes" => "/api/me/credentials/relay".to_owned(),
        "relay_pending" => "/api/me/credentials/relay/pending".to_owned(),
        "relay_replace_device" => {
            for entry in body["entries"].as_array_mut().unwrap() {
                let id = entry.as_object_mut().unwrap().remove("entry_id").unwrap();
                entry["id"] = id;
            }
            "/api/me/credentials/relay/device".to_owned()
        }
        "remove" => format!("/api/me{}?revision=4", path),
        _ => format!("/api/me{path}"),
    };
    if case["name"] == "add" {
        body.as_object_mut().unwrap().remove("route");
        body["type"] = json!("openai_api");
    }
    (browser_path, (!body.is_null()).then_some(body))
}

/// Compare one browser result and exact vault wire request to the Python fixture.
#[tokio::test]
async fn python_credential_parity() {
    let fixture: Value = serde_json::from_str(include_str!("credentials_python.json")).unwrap();
    let vault_state = Arc::new(Mutex::new(VaultFixture::default()));
    let (vault_url, vault_task) = serve(
        Router::new()
            .fallback(any(vault))
            .with_state(vault_state.clone()),
    )
    .await;
    let sessions = Sessions::new();
    let session = sessions.create(
        "fixture-user-token".to_owned(),
        User {
            id: 7,
            login: "owner".to_owned(),
            name: None,
            avatar_url: None,
        },
        3600,
    );
    let (browser_url, browser_task) =
        serve(browser_router(state(vault_url, sessions.clone()))).await;
    let client = Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .unwrap();
    for case in fixture["cases"].as_array().unwrap() {
        vault_state.lock().unwrap().reply = Some(case["vault_response"].clone());
        vault_state.lock().unwrap().observed = None;
        let method = reqwest::Method::from_bytes(
            case["vault_request"]["method"].as_str().unwrap().as_bytes(),
        )
        .unwrap();
        let (path, body) = browser_call(case);
        let mut request = client
            .request(method.clone(), format!("{browser_url}{path}"))
            .header("cookie", format!("concurro_session={}", session.id));
        if method != reqwest::Method::GET {
            request = request.header("x-concurro-request", "1");
        }
        if let Some(body) = body {
            request = request.json(&body);
        }
        let response = request.send().await.unwrap();
        let status = response.status();
        let actual: Value = response.json().await.unwrap();
        let expected = &case["browser_response"];
        let expected_body = if path == "/api/me/relays" {
            let mut body = expected["body"].clone();
            body["setup"] = fixture["relay_setup"].clone();
            body
        } else {
            expected["body"].clone()
        };
        assert_eq!(
            status.as_u16(),
            expected["status"].as_u64().unwrap() as u16,
            "{}",
            case["name"]
        );
        assert_eq!(actual, expected_body, "{}", case["name"]);
        let observed = vault_state.lock().unwrap().observed.clone().unwrap();
        let mut expected_request = case["vault_request"].clone();
        expected_request.as_object_mut().unwrap().remove("timeout");
        assert_eq!(observed, expected_request, "{}", case["name"]);
        if case["name"] == "error_401" {
            assert!(sessions.get(Some(&session.id)).is_none());
        }
    }
    browser_task.abort();
    vault_task.abort();
}

/// Require owner session and CSRF before parsing, and suppress secret-bearing malformation.
#[tokio::test]
async fn browser_authorization_and_malformed_body() {
    let vault_state = Arc::new(Mutex::new(VaultFixture::default()));
    let (vault_url, vault_task) = serve(
        Router::new()
            .fallback(any(vault))
            .with_state(vault_state.clone()),
    )
    .await;
    let sessions = Sessions::new();
    let session = sessions.create(
        "fixture-user-token".to_owned(),
        User {
            id: 7,
            login: "owner".to_owned(),
            name: None,
            avatar_url: None,
        },
        3600,
    );
    let (browser_url, browser_task) = serve(browser_router(state(vault_url, sessions))).await;
    let client = Client::new();
    let writes = [
        ("POST", "/api/me/credentials"),
        ("PATCH", "/api/me/credentials/entry"),
        ("PUT", "/api/me/credentials/entry/secret"),
        ("PUT", "/api/me/credentials/order/openai"),
        ("DELETE", "/api/me/credentials/entry?revision=1"),
        ("POST", "/api/me/credentials/relay/pending"),
        ("POST", "/api/me/credentials/relay"),
        ("PUT", "/api/me/credentials/relay/device"),
    ];
    for (method, path) in writes {
        let url = format!("{browser_url}{path}");
        let method = reqwest::Method::from_bytes(method.as_bytes()).unwrap();
        let secret = "do-not-echo-synthetic-secret";
        let response = client
            .request(method.clone(), &url)
            .header("x-concurro-request", "1")
            .body(secret)
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED, "{path}");
        let response = client
            .request(method.clone(), &url)
            .header("cookie", format!("concurro_session={}", session.id))
            .body(secret)
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::FORBIDDEN, "{path}");
        if path.contains("?revision=") {
            continue;
        }
        let response = client
            .request(method, &url)
            .header("cookie", format!("concurro_session={}", session.id))
            .header("x-concurro-request", "1")
            .body(secret)
            .send()
            .await
            .unwrap();
        assert_eq!(
            response.status(),
            StatusCode::UNPROCESSABLE_ENTITY,
            "{path}"
        );
        assert_eq!(
            response.text().await.unwrap(),
            r#"{"detail":"malformed request"}"#
        );
        assert!(vault_state.lock().unwrap().observed.is_none(), "{path}");
    }
    let cookie = format!("concurro_session={}", session.id);
    let response = client
        .delete(format!("{browser_url}/api/me/credentials/entry"))
        .header("cookie", &cookie)
        .header("x-concurro-request", "1")
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::GONE);
    assert_eq!(
        response.json::<Value>().await.unwrap(),
        json!({"detail":
        "Credentials are now managed as a list. Reload the page to continue."})
    );
    let response = client
        .post(format!("{browser_url}/api/me/credentials"))
        .header("cookie", &cookie)
        .header("x-concurro-request", "1")
        .json(
            &json!({"type":"openai_api","name":"Credits","secret":"synthetic-secret",
            "mutation_id":"mmmmmmmmmmmmmmmm","roles":["unknown"]}),
        )
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(
        response.json::<Value>().await.unwrap(),
        json!({"detail":"unknown role filter"})
    );
    assert!(vault_state.lock().unwrap().observed.is_none());
    browser_task.abort();
    vault_task.abort();
}

/// Preserve vault refusal messages and distinguish an unreachable owner vault.
#[tokio::test]
async fn vault_refusal_and_outage() {
    let vault_state = Arc::new(Mutex::new(VaultFixture {
        reply: Some(json!({"status":404,"body":{"error":"unknown or expired device code"}})),
        observed: None,
    }));
    let (vault_url, vault_task) = serve(
        Router::new()
            .fallback(any(vault))
            .with_state(vault_state.clone()),
    )
    .await;
    let sessions = Sessions::new();
    let session = sessions.create(
        "fixture-user-token".to_owned(),
        User {
            id: 7,
            login: "owner".to_owned(),
            name: None,
            avatar_url: None,
        },
        3600,
    );
    let (browser_url, browser_task) =
        serve(browser_router(state(vault_url.clone(), sessions))).await;
    let client = Client::new();
    let response = client
        .post(format!("{browser_url}/api/me/credentials/relay/pending"))
        .header("cookie", format!("concurro_session={}", session.id))
        .header("x-concurro-request", "1")
        .json(&json!({"code":"unknown"}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
    assert_eq!(
        response.json::<Value>().await.unwrap(),
        json!({"detail":"unknown or expired device code"})
    );
    vault_task.abort();
    let _ = vault_task.await;
    let response = client
        .get(format!("{browser_url}/api/me/credentials"))
        .header("cookie", format!("concurro_session={}", session.id))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(
        response.json::<Value>().await.unwrap(),
        json!({"detail":"the credentials vault is unavailable"})
    );
    let response = client
        .get(format!("{browser_url}/api/me/relays"))
        .header("cookie", format!("concurro_session={}", session.id))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let value = response.json::<Value>().await.unwrap();
    assert_eq!(value["service"], "unavailable");
    assert_eq!(value["relays"], json!([]));
    browser_task.abort();
}
