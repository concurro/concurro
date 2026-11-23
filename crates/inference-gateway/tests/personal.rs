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
    for (record, secret) in [(&bad, "bad-key"), (&good, "good-key")] {
        accounts
            .create(
                &format!("personal/{record}"),
                &BTreeMap::from([("secret".to_owned(), SecretString::from(secret.to_owned()))]),
            )
            .await
            .expect("immutable personal version");
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
            _ => (StatusCode::FORBIDDEN, Json(json!({"error":{"type":"unknown"}}))),
        }
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
        personal_routes: Arc::new(HashMap::from([(
            "anthropic-api-key".to_owned(),
            PersonalRoute {
                upstream,
                anthropic_beta: None,
            },
        )])),
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
    }
    gateway_server.abort();
    provider_server.abort();
    admission_server.abort();
}
