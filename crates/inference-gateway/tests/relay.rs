//! Relay dispatch preserves the exchange claim and provider request identity.

use std::{
    collections::HashMap,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use axum::{
    Json, Router,
    http::{HeaderMap, StatusCode},
    routing::post,
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use chrono::Utc;
use concurro_db::Database;
use concurro_events_postgres::{Outbox, migrate as migrate_events};
use concurro_exchange::{AdmissionState, admission_router};
use concurro_exchange_postgres::{RelaySigner, Store, migrate as migrate_exchange};
use concurro_inference_gateway::{GatewayState, RelayDispatch, router};
use concurro_relay_hosted_api::{
    claim::VerificationKeys,
    dispatch::{CLAIM_HEADER, REQUEST_HEADER},
};
use concurro_secrets::{SecretRef, Secrets};
use concurro_testkit::PostgresContainer;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use uuid::Uuid;

/// A verifier's reused-ID refusal is forwarded once with the same signed authority.
#[tokio::test]
async fn relay_dispatch_does_not_retry_request_reused_with_a_new_id() {
    let postgres = PostgresContainer::start().await.expect("PostgreSQL");
    let database = Database::connect(postgres.url(), 8)
        .await
        .expect("database");
    migrate_events(&database).await.expect("events");
    migrate_exchange(&database).await.expect("exchange");
    let private = URL_SAFE_NO_PAD
        .decode("MC4CAQAwBQYDK2VwBCIEIJ1hsZ3v_VpguoRK9JLsLMREScVpezJpGXA7rAMcrn9g")
        .expect("PKCS8 fixture");
    let store = Store::new(
        database.clone(),
        Outbox::new(
            1_048_576,
            vec!["exchange.turn.>".to_owned(), "inference.usage".to_owned()],
        ),
    )
    .with_relay(RelaySigner::new("fixture".to_owned(), private, 1000).expect("signer"));
    let mut body: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/exchange.http.v1/operator_submit.json"
    ))
    .expect("fixture");
    let turn = Uuid::now_v7();
    body["attempt_id"] = json!(turn);
    body["grant"]["grant_id"] = json!(turn.to_string());
    body["grant"]["purpose"] = json!("task_contributed");
    body["grant"]["payer"] = json!({"id":35576205,"login":"payer"});
    body["grant"]["credential"] = json!({"route":"claude-subscription","entry_id":"entry",
        "record_id":"record","version":1,"custody":"relay","relay_id":"relay-1"});
    let session = store
        .submit(turn, &body)
        .await
        .expect("submit")
        .dispatch
        .expect("dispatch")
        .session_id;
    sqlx::query("UPDATE exchange.sessions SET session_key_hash=$2,bound_at=now(),last_seen=now() WHERE session_id=$1")
        .bind(session).bind(hex::encode(Sha256::digest(b"relay-session")))
        .execute(database.pool()).await.expect("session key");
    sqlx::query("UPDATE exchange.turns SET status='running' WHERE turn_id=$1")
        .bind(turn)
        .execute(database.pool())
        .await
        .expect("running");
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("admission listener");
    let admission_url = format!("http://{}", listener.local_addr().expect("address"));
    let admission_server = tokio::spawn({
        let store = store.clone();
        async move {
            axum::serve(
                listener,
                admission_router(AdmissionState {
                    store,
                    bearer: "admission-test".to_owned(),
                }),
            )
            .await
            .expect("admission server");
        }
    });
    let keys = Arc::new(VerificationKeys::from_jwks(br#"{"keys":[{"kid":"fixture","kty":"OKP","crv":"Ed25519","x":"11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"}]}"#)
        .expect("S1 keys"));
    let calls = Arc::new(AtomicUsize::new(0));
    let verifier = Router::new().route(
        "/dispatch/v2/messages",
        post({
            let calls = calls.clone();
            move |headers: HeaderMap, Json(body): Json<Value>| {
                let calls = calls.clone();
                let keys = keys.clone();
                async move {
                    calls.fetch_add(1, Ordering::SeqCst);
                    assert_eq!(
                        headers.get("authorization").expect("verifier credential"),
                        "Bearer inference-test"
                    );
                    let claim = keys
                        .verify(
                            headers
                                .get(CLAIM_HEADER)
                                .expect("claim")
                                .to_str()
                                .expect("JWS"),
                            Utc::now().timestamp(),
                        )
                        .expect("S1 claim verifier");
                    assert_eq!(claim.sub, session.to_string());
                    assert!(
                        Uuid::parse_str(
                            headers
                                .get(REQUEST_HEADER)
                                .expect("request ID")
                                .to_str()
                                .expect("UUID")
                        )
                        .is_ok()
                    );
                    match body["case"].as_str() {
                        Some("claim_refused") => (
                            StatusCode::FORBIDDEN,
                            Json(json!({"error":{"code":"claim_refused"}})),
                        ),
                        Some("unavailable") => (
                            StatusCode::SERVICE_UNAVAILABLE,
                            Json(json!({"error":{"code":"unavailable"}})),
                        ),
                        Some("success") => (
                            StatusCode::OK,
                            Json(json!({
                                "type":"message","stop_reason":"end_turn",
                                "usage":{"input_tokens":3,"output_tokens":2}
                            })),
                        ),
                        _ => (
                            StatusCode::CONFLICT,
                            Json(
                                json!({"error":{"code":"request_reused","message":"already used"}}),
                            ),
                        ),
                    }
                }
            }
        }),
    );
    let verifier_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("verifier listener");
    let verifier_url = format!(
        "http://{}",
        verifier_listener.local_addr().expect("verifier address")
    );
    let verifier_server = tokio::spawn(async move {
        axum::serve(verifier_listener, verifier)
            .await
            .expect("verifier");
    });
    let token_file =
        std::env::temp_dir().join(format!("concurro-s4d-inference-{}", Uuid::now_v7()));
    std::fs::write(&token_file, "inference-test\n").expect("verifier credential file");
    let gateway = router(GatewayState {
        admission: concurro_exchange_client::AdmissionClient::new(
            reqwest::Client::new(),
            admission_url,
            "admission-test",
        ),
        provider: reqwest::Client::new(),
        routes: Arc::new(HashMap::new()),
        personal_routes: Arc::new(HashMap::new()),
        relay: Some(RelayDispatch {
            verifier: verifier_url,
            credential: SecretRef::File(token_file.clone()),
        }),
        secrets: Arc::new(Secrets::new(None, Duration::from_secs(60))),
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
    for (index, case, path, status, category, dispatched) in [
        (
            1_i64,
            "request_reused",
            "/v1/messages",
            StatusCode::CONFLICT,
            "unknown",
            true,
        ),
        (
            2,
            "claim_refused",
            "/v1/messages",
            StatusCode::FORBIDDEN,
            "unknown",
            true,
        ),
        (
            3,
            "unsupported",
            "/v1/messages/count_tokens",
            StatusCode::UNPROCESSABLE_ENTITY,
            "unknown",
            false,
        ),
        (4, "success", "/v1/messages", StatusCode::OK, "ok", true),
        (
            5,
            "unavailable",
            "/v1/messages",
            StatusCode::SERVICE_UNAVAILABLE,
            "unavailable",
            true,
        ),
    ] {
        let response = reqwest::Client::new()
            .post(format!("{gateway_url}{path}"))
            .bearer_auth("relay-session")
            .json(&json!({"model":"claude-test","messages":[],"case":case}))
            .send()
            .await
            .expect("relay dispatch");
        assert_eq!(response.status(), status, "{case}");
        let _ = response.bytes().await.expect("complete relay response");
        tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                let finished: i64 = sqlx::query_scalar(
                    "SELECT count(*) FROM exchange.inference_requests WHERE turn_id=$1 AND finished IS NOT NULL"
                ).bind(turn).fetch_one(database.pool()).await.expect("settled request count");
                if finished >= index { break; }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        }).await.expect("relay finish report");
        let recorded: (String, bool) = sqlx::query_as(
            "SELECT category,dispatched FROM exchange.inference_requests WHERE turn_id=$1 ORDER BY finished DESC LIMIT 1"
        ).bind(turn).fetch_one(database.pool()).await.expect("settled relay request");
        assert_eq!(recorded, (category.to_owned(), dispatched), "{case}");
        if case == "success" {
            let usage: (i64, i64) = sqlx::query_as(
                "SELECT (usage->>'input_tokens')::bigint,(usage->>'output_tokens')::bigint \
                 FROM exchange.inference_requests WHERE turn_id=$1 ORDER BY finished DESC LIMIT 1",
            )
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("relay usage");
            assert_eq!(usage, (3, 2));
        }
    }
    assert_eq!(calls.load(Ordering::SeqCst), 4);
    let requests: i64 =
        sqlx::query_scalar("SELECT count(*) FROM exchange.inference_requests WHERE turn_id=$1")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("request count");
    assert_eq!(requests, 5);
    let failure: Value =
        sqlx::query_scalar("SELECT provider_failure FROM exchange.turns WHERE turn_id=$1")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("relay failure");
    assert_eq!(failure["category"], "unavailable");
    gateway_server.abort();
    verifier_server.abort();
    admission_server.abort();
    std::fs::remove_file(token_file).expect("remove verifier credential");
}
