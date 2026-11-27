//! Personal OpenBao versions through real exchange admission and a fake provider.

use std::{
    collections::{BTreeMap, HashMap},
    path::PathBuf,
    sync::Arc,
    time::Duration,
};

use axum::{
    Json, Router,
    http::{HeaderMap, StatusCode},
    routing::post,
};
use concurro_db::Database;
use concurro_events_postgres::{Outbox, migrate as migrate_events};
use concurro_exchange::{AdmissionState, admission_router};
use concurro_exchange_postgres::{Store, migrate as migrate_exchange};
use concurro_inference_gateway::{GatewayState, PersonalRoute, router};
use concurro_secrets::{OpenBao, OpenBaoSettings, SecretString, Secrets};
use concurro_testkit::{OpenBaoContainer, PostgresContainer};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use uuid::Uuid;

/// Sign a test service into the configured OpenBao AppRole policy.
async fn service(bao: &OpenBaoContainer, admin: &str, role: &str) -> OpenBao {
    let http = reqwest::Client::new();
    let role_id: Value = http
        .get(format!("{}/v1/auth/approle/role/{role}/role-id", bao.url()))
        .header("X-Vault-Token", admin)
        .send()
        .await
        .expect("role ID response")
        .json()
        .await
        .expect("role ID");
    let secret_id: Value = http
        .post(format!(
            "{}/v1/auth/approle/role/{role}/secret-id",
            bao.url()
        ))
        .header("X-Vault-Token", admin)
        .json(&json!({}))
        .send()
        .await
        .expect("secret ID response")
        .json()
        .await
        .expect("secret ID");
    let file = std::env::temp_dir().join(format!("concurro-s4d-{role}-{}", Uuid::now_v7()));
    std::fs::write(
        &file,
        secret_id["data"]["secret_id"].as_str().expect("secret ID"),
    )
    .expect("secret ID file");
    let client = OpenBao::connect(OpenBaoSettings {
        url: bao.url().to_owned(),
        role_id: role_id["data"]["role_id"]
            .as_str()
            .expect("role ID")
            .to_owned(),
        secret_id_file: file.clone(),
        mount: "concurro".to_owned(),
        refresh_seconds: 0,
    })
    .await
    .expect("service AppRole");
    std::fs::remove_file(file).expect("remove test secret ID");
    client
}

/// A failed personal version is attributed to its grant, then another exact version succeeds.
#[tokio::test]
async fn personal_failure_then_replacement_uses_exact_openbao_version() {
    let bao = OpenBaoContainer::start(
        &PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../deploy/openbao/openbao.hcl"),
        &[
            ("INITIAL_ADMIN_PASSWORD", "admin-pass"),
            ("INITIAL_ADMIN_RECOVERY_PASSWORD", "recovery-pass"),
            ("INITIAL_OPERATOR_PASSWORD", "operator-pass"),
        ],
    )
    .await
    .expect("OpenBao");
    let http = reqwest::Client::new();
    let admin: Value = http
        .post(format!("{}/v1/auth/userpass/login/admin", bao.url()))
        .json(&json!({"password":"admin-pass"}))
        .send()
        .await
        .expect("admin login")
        .json()
        .await
        .expect("admin token");
    let admin = admin["auth"]["client_token"].as_str().expect("admin token");
    let accounts = service(&bao, admin, "accounts").await;
    let gateway_bao = service(&bao, admin, "gateway").await;
    let bad = "a".repeat(64);
    let good = "b".repeat(64);
    let openai = "c".repeat(64);
    let claude = "d".repeat(64);
    let chatgpt = "e".repeat(64);
    let google = "f".repeat(64);
    for (record, secret) in [(&bad, "bad-key"), (&good, "good-key")] {
        accounts
            .create(
                &format!("personal/{record}"),
                &BTreeMap::from([("secret".to_owned(), SecretString::from(secret.to_owned()))]),
            )
            .await
            .expect("immutable personal version");
    }
    for (record, secret, account) in [
        (&openai, "openai-key", None),
        (&google, "gemini-key", None),
        (&claude, "claude-oauth", None),
        (
            &chatgpt,
            "chatgpt-token",
            Some(r#"{"chatgpt_account_id":"paired-account"}"#),
        ),
    ] {
        let mut fields =
            BTreeMap::from([("secret".to_owned(), SecretString::from(secret.to_owned()))]);
        if let Some(account) = account {
            fields.insert("account".to_owned(), SecretString::from(account.to_owned()));
        }
        accounts
            .create(&format!("personal/{record}"), &fields)
            .await
            .expect("route version");
    }
    let postgres = PostgresContainer::start().await.expect("PostgreSQL");
    let database = Database::connect(postgres.url(), 8)
        .await
        .expect("database");
    migrate_events(&database).await.expect("events");
    migrate_exchange(&database).await.expect("exchange");
    let store = Store::new(
        database.clone(),
        Outbox::new(
            1_048_576,
            vec!["exchange.turn.>".to_owned(), "inference.usage".to_owned()],
        ),
    );
    let admission_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("admission listener");
    let admission_url = format!(
        "http://{}",
        admission_listener.local_addr().expect("address")
    );
    let admission_server = tokio::spawn({
        let store = store.clone();
        async move {
            axum::serve(
                admission_listener,
                admission_router(AdmissionState {
                    store,
                    bearer: "admission-test".to_owned(),
                }),
            )
            .await
            .expect("admission server");
        }
    });
    let provider = Router::new().route("/v1/messages", post(|headers: HeaderMap, Json(_): Json<Value>| async move {
        match headers.get("x-api-key").and_then(|value| value.to_str().ok()) {
            Some("bad-key") => (StatusCode::UNAUTHORIZED, Json(json!({
                "type":"error", "error":{"type":"authentication_error", "message":"bad credential"}
            }))),
            Some("good-key") => (StatusCode::OK, Json(json!({
                "type":"message", "stop_reason":"end_turn", "usage":{"input_tokens":3,"output_tokens":2}
            }))),
            _ if headers.get("authorization").is_some_and(|value| value == "Bearer claude-oauth")
                && headers.get("anthropic-beta").is_some_and(|value| value == "oauth-2025-04-20") =>
                (StatusCode::OK, Json(json!({"type":"message","stop_reason":"end_turn","usage":{"input_tokens":1,"output_tokens":1}}))),
            _ => (StatusCode::FORBIDDEN, Json(json!({"error":{"type":"unknown"}}))),
        }
    })).route("/v1/responses", post(|headers: HeaderMap, Json(_): Json<Value>| async move {
        let authorization = headers.get("authorization").and_then(|value| value.to_str().ok());
        let account = headers.get("chatgpt-account-id").and_then(|value| value.to_str().ok());
        let valid = (authorization == Some("Bearer openai-key") && account.is_none())
            || (authorization == Some("Bearer chatgpt-token") && account == Some("paired-account"));
        if valid && headers.get("x-api-key").is_none() {
            (StatusCode::OK, Json(json!({"status":"completed","usage":{"input_tokens":1,"output_tokens":1}})))
        } else {
            (StatusCode::FORBIDDEN, Json(json!({"error":{"type":"unknown"}})))
        }
    }));
    let provider = provider.route("/v1beta/models/claude-test:generateContent", post(|headers: HeaderMap, Json(_): Json<Value>| async move {
        assert_eq!(headers["x-goog-api-key"], "gemini-key");
        assert!(headers.get("authorization").is_none());
        assert!(headers.get("x-goog-user-project").is_none());
        Json(json!({"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"ok"}]}}],"usageMetadata":{"promptTokenCount":3,"candidatesTokenCount":2,"totalTokenCount":5}}))
    }));
    let provider_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("provider listener");
    let upstream = format!(
        "http://{}/v1",
        provider_listener.local_addr().expect("provider address")
    );
    let provider_server = tokio::spawn(async move {
        axum::serve(provider_listener, provider)
            .await
            .expect("provider");
    });
    let gateway = router(GatewayState {
        admission: concurro_exchange_client::AdmissionClient::new(
            reqwest::Client::new(),
            admission_url,
            "admission-test",
        ),
        provider: reqwest::Client::new(),
        routes: Arc::new(HashMap::new()),
        personal_routes: Arc::new(HashMap::from([
            (
                "gemini-api-key".to_owned(),
                PersonalRoute {
                    upstream: upstream.trim_end_matches("/v1").to_owned(),
                    anthropic_beta: None,
                },
            ),
            (
                "anthropic-api-key".to_owned(),
                PersonalRoute {
                    upstream: upstream.clone(),
                    anthropic_beta: None,
                },
            ),
            (
                "openai-api-key".to_owned(),
                PersonalRoute {
                    upstream: upstream.clone(),
                    anthropic_beta: None,
                },
            ),
            (
                "chatgpt-subscription".to_owned(),
                PersonalRoute {
                    upstream: upstream.clone(),
                    anthropic_beta: None,
                },
            ),
            (
                "claude-subscription".to_owned(),
                PersonalRoute {
                    upstream,
                    anthropic_beta: Some("oauth-2025-04-20".to_owned()),
                },
            ),
        ])),
        relay: None,
        secrets: Arc::new(Secrets::new(Some(gateway_bao), Duration::from_secs(0))),
    });
    let gateway_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("gateway listener");
    let gateway_url = format!(
        "http://{}",
        gateway_listener.local_addr().expect("gateway address")
    );
    let gateway_server = tokio::spawn(async move {
        axum::serve(gateway_listener, gateway)
            .await
            .expect("gateway");
    });
    let mut fixture: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/exchange.http.v1/operator_submit.json"
    ))
    .expect("v2 fixture");
    for (record, key, expect) in [
        (&bad, "bad-session", StatusCode::UNAUTHORIZED),
        (&good, "good-session", StatusCode::OK),
    ] {
        let turn = Uuid::now_v7();
        fixture["attempt_id"] = json!(turn);
        fixture["grant"]["grant_id"] = json!(turn.to_string());
        fixture["grant"]["purpose"] = json!("task_contributed");
        fixture["grant"]["payer"] = json!({"id":35576205,"login":"payer"});
        fixture["grant"]["credential"] = json!({"route":"anthropic-api-key",
            "entry_id":"entry","record_id":record,"version":1,"custody":"secret_store"});
        let session = store
            .submit(turn, &fixture)
            .await
            .expect("contributed grant")
            .dispatch
            .expect("dispatch")
            .session_id;
        sqlx::query("UPDATE exchange.sessions SET session_key_hash=$2,bound_at=now(),last_seen=now() WHERE session_id=$1")
            .bind(session).bind(hex::encode(Sha256::digest(key.as_bytes())))
            .execute(database.pool()).await.expect("bind key");
        sqlx::query("UPDATE exchange.turns SET status='running' WHERE turn_id=$1")
            .bind(turn)
            .execute(database.pool())
            .await
            .expect("running");
        let response = http
            .post(format!("{gateway_url}/v1/messages"))
            .bearer_auth(key)
            .json(&json!({"model":"claude-test","messages":[]}))
            .send()
            .await
            .expect("inference");
        assert_eq!(response.status(), expect);
        let _ = response.bytes().await.expect("body");
        for _ in 0..40 {
            let category: Option<String> = sqlx::query_scalar(
                "SELECT category FROM exchange.inference_requests WHERE turn_id=$1 AND finished IS NOT NULL"
            ).bind(turn).fetch_optional(database.pool()).await.expect("request accounting");
            if category.is_some() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        let (category, failure): (String, Option<Value>) = sqlx::query_as(
            "SELECT r.category,t.provider_failure FROM exchange.inference_requests r \
             JOIN exchange.turns t ON t.turn_id=r.turn_id WHERE r.turn_id=$1",
        )
        .bind(turn)
        .fetch_one(database.pool())
        .await
        .expect("settled request");
        if record == &bad {
            assert_eq!(category, "auth");
            let failure = failure.expect("exact failure");
            assert_eq!(failure["grant_id"], turn.to_string());
            assert_eq!(failure["record_id"], *record);
            assert_eq!(failure["version"], 1);
            store.cancel(turn).await.expect("retire failed version");
        } else {
            assert_eq!(category, "ok");
            assert!(failure.is_none());
        }
        store.cancel(turn).await.expect("release route session");
    }
    for (record, route, path, key) in [
        (&openai, "openai-api-key", "responses", "openai-session"),
        (
            &chatgpt,
            "chatgpt-subscription",
            "responses",
            "chatgpt-session",
        ),
        (&claude, "claude-subscription", "messages", "claude-session"),
        (
            &google,
            "gemini-api-key",
            "v1beta/models/claude-test:generateContent",
            "gemini-session",
        ),
    ] {
        let turn = Uuid::now_v7();
        fixture["attempt_id"] = json!(turn);
        fixture["grant"]["grant_id"] = json!(turn.to_string());
        fixture["grant"]["credential"] = json!({"route":route,
            "entry_id":"entry","record_id":record,"version":1,"custody":"secret_store"});
        let session = store
            .submit(turn, &fixture)
            .await
            .expect("route grant")
            .dispatch
            .expect("route dispatch")
            .session_id;
        sqlx::query("UPDATE exchange.sessions SET session_key_hash=$2,bound_at=now(),last_seen=now() WHERE session_id=$1")
            .bind(session).bind(hex::encode(Sha256::digest(key.as_bytes())))
            .execute(database.pool()).await.expect("route session bind");
        sqlx::query("UPDATE exchange.turns SET status='running' WHERE turn_id=$1")
            .bind(turn)
            .execute(database.pool())
            .await
            .expect("route running");
        let response = http
            .post(format!(
                "{gateway_url}/{}",
                if route == "gemini-api-key" {
                    path.to_owned()
                } else {
                    format!("v1/{path}")
                }
            ))
            .bearer_auth(key)
            .header("x-api-key", "forged")
            .header("chatgpt-account-id", "forged")
            .header("x-goog-api-key", "forged")
            .header("x-goog-user-project", "forged")
            .json(&json!({"model":"claude-test","messages":[]}))
            .send()
            .await
            .expect("route request");
        assert_eq!(response.status(), StatusCode::OK, "{route}");
        let _ = response.bytes().await.expect("route response");
        store.cancel(turn).await.expect("release route");
    }
    gateway_server.abort();
    provider_server.abort();
    admission_server.abort();
}
