//! Real exchange admission through the org gateway to a fake provider.

use std::{collections::HashMap, sync::Arc, time::Duration};

use axum::{
    Json, Router,
    body::{Body, Bytes},
    http::{HeaderMap, StatusCode},
    response::IntoResponse,
    routing::post,
};
use concurro_db::Database;
use concurro_events_postgres::{Outbox, migrate as migrate_events};
use concurro_exchange::{AdmissionState, admission_router};
use concurro_exchange_postgres::{Store, migrate as migrate_exchange};
use concurro_inference_gateway::{GatewayState, OrgRoute, router};
use concurro_secrets::{SecretRef, Secrets};
use concurro_testkit::PostgresContainer;
use futures_util::{StreamExt, stream};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio::io::{AsyncReadExt, copy_bidirectional};
use tokio::sync::{Mutex, Notify};
use uuid::Uuid;

/// Forward org headers, retain provider bytes, and settle one usage fact.
#[tokio::test]
async fn org_route_uses_frozen_credential_and_reports_usage() {
    let postgres = PostgresContainer::start().await.expect("PostgreSQL");
    let database = Database::connect(postgres.url(), 8)
        .await
        .expect("database");
    migrate_events(&database).await.expect("outbox");
    migrate_exchange(&database).await.expect("exchange");
    let store = Store::new(
        database.clone(),
        Outbox::new(
            1_048_576,
            vec!["exchange.turn.>".to_owned(), "inference.usage".to_owned()],
        ),
    );
    let control = store.clone();
    let body: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/exchange.http.v1/operator_submit.json"
    ))
    .expect("fixture");
    let turn: Uuid = body["attempt_id"]
        .as_str()
        .expect("turn")
        .parse()
        .expect("UUID");
    let session = store
        .submit(turn, &body)
        .await
        .expect("submit")
        .dispatch
        .expect("dispatch")
        .session_id;
    sqlx::query("UPDATE exchange.sessions SET session_key_hash=$2,runner_token_hash=$3,bound_at=now(),last_seen=now() WHERE session_id=$1")
        .bind(session).bind(hex::encode(Sha256::digest(b"org-session-key")))
        .bind(hex::encode(Sha256::digest(b"runner-token")))
        .execute(database.pool()).await.expect("bind session");
    sqlx::query("UPDATE exchange.turns SET status='running',started=now() WHERE turn_id=$1")
        .bind(turn)
        .execute(database.pool())
        .await
        .expect("running turn");
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("admission listener");
    let exchange_address = listener.local_addr().expect("admission address");
    let admission_server = tokio::spawn(async move {
        axum::serve(
            listener,
            admission_router(AdmissionState {
                store,
                bearer: "admission-test".to_owned(),
            }),
        )
        .await
        .expect("admission server");
    });
    // The switch owns established sockets too, so dropping it proves an outage even
    // when the gateway reused a keep-alive connection to the exchange listener.
    let switch = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("admission switch");
    let admission_url = format!("http://{}", switch.local_addr().expect("switch address"));
    let switch_connections = Arc::new(Mutex::new(Vec::new()));
    let connections = switch_connections.clone();
    let switch_server = tokio::spawn(async move {
        loop {
            let Ok((mut gateway, _)) = switch.accept().await else {
                break;
            };
            let Ok(mut exchange) = tokio::net::TcpStream::connect(exchange_address).await else {
                continue;
            };
            let connection = tokio::spawn(async move {
                let _ = copy_bidirectional(&mut gateway, &mut exchange).await;
            });
            connections.lock().await.push(connection);
        }
    });
    let received = Arc::new(Mutex::new(None::<HeaderMap>));
    let captured = received.clone();
    let response_captured = received.clone();
    let rotation_calls = Arc::new(Mutex::new(Vec::<String>::new()));
    let provider_rotations = rotation_calls.clone();
    let stall_seen = Arc::new(Notify::new());
    let stalled_provider = stall_seen.clone();
    let provider = Router::new().route(
        "/v1/messages",
        post(move |headers: HeaderMap, Json(body): Json<Value>| {
            let captured = captured.clone();
            let stalled_provider = stalled_provider.clone();
            let provider_rotations = provider_rotations.clone();
            async move {
                let supplied_key = headers.get("x-api-key").and_then(|value| value.to_str().ok()).unwrap_or_default().to_owned();
                *captured.lock().await = Some(headers);
                if body["rotate"] == true {
                    provider_rotations.lock().await.push(supplied_key.clone());
                    if supplied_key != "org-secret-rotated" {
                        return (StatusCode::UNAUTHORIZED, Json(json!({"error":{"type":"authentication_error","message":"private provider text"}}))).into_response();
                    }
                    return Json(json!({"type":"message","stop_reason":"end_turn","usage":{"input_tokens":2,"output_tokens":1}})).into_response();
                }
                if body["stall"] == true {
                    stalled_provider.notify_one();
                    tokio::time::sleep(Duration::from_secs(60)).await;
                    Json(json!({"type":"message","stop_reason":"end_turn","usage":{"input_tokens":1,"output_tokens":1}})).into_response()
                } else if body["flood"] == true {
                    let events = (0..10_000).map(|_| Ok::<Bytes, std::io::Error>(Bytes::from_static(b"data: {\"type\":\"content_block_delta\"}\n\n")));
                    let mut response = Body::from_stream(stream::iter(events)).into_response();
                    response.headers_mut().insert("content-type", "text/event-stream".parse().expect("media"));
                    response
                } else if body["hang"] == true {
                    let first = stream::once(async {
                        Ok::<Bytes, std::io::Error>(Bytes::from_static(
                            b"data: {\"type\":\"message_start\"}\n\n",
                        ))
                    });
                    let endless = stream::pending::<Result<Bytes, std::io::Error>>();
                    let mut response = Body::from_stream(first.chain(endless)).into_response();
                    response.headers_mut().insert("content-type", "text/event-stream".parse().expect("media"));
                    response
                } else {
                    let chunks = stream::iter([
                        Ok::<Bytes, std::io::Error>(Bytes::from_static(b"{\"type\":\"message\",\"stop_reason\":\"end_turn\",")),
                        Ok(Bytes::from_static(b"\"usage\":{\"input_tokens\":13,\"output_tokens\":5}}")),
                    ]);
                    let mut response = Body::from_stream(chunks).into_response();
                    response.headers_mut().insert("content-type", "application/json".parse().expect("JSON media"));
                    response.headers_mut().insert("x-provider-request-id", "provider-1".parse().expect("header"));
                    response
                }
            }
        }),
    ).route("/v1/responses", post(move |headers: HeaderMap, Json(body): Json<Value>| {
        let response_captured = response_captured.clone();
        async move {
            *response_captured.lock().await = Some(headers);
            if body["fail"] == true {
                let mut response = Body::from("data: {\"type\":\"response.failed\",\"response\":{\"error\":{\"code\":\"insufficient_quota\",\"message\":\"provider private text\"}}}\n\n").into_response();
                response.headers_mut().insert("content-type", "text/event-stream".parse().expect("media"));
                response
            } else {
                let event = Bytes::from_static(b"data: {\"type\":\"response.output_text.delta\",\"delta\":\"part\"}\n\n");
                let pieces = stream::iter((0..150_000).map(move |_| Ok::<Bytes, std::io::Error>(event.clone())));
                let terminal = stream::once(async { Ok::<Bytes, std::io::Error>(Bytes::from_static(b"data: {\"type\":\"response.completed\",\"response\":{\"usage\":{\"input_tokens\":9,\"output_tokens\":4}}}\n\n")) });
                let mut response = Body::from_stream(pieces.chain(terminal)).into_response();
                response.headers_mut().insert("content-type", "text/event-stream".parse().expect("media"));
                response.headers_mut().insert("x-request-id", "response-1".parse().expect("metadata"));
                response
            }
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
            .expect("fake provider");
    });
    let dropper_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("dropper listener");
    let dropper_upstream = format!(
        "http://{}/v1",
        dropper_listener.local_addr().expect("dropper address")
    );
    let dropper_server = tokio::spawn(async move {
        let (mut socket, _) = dropper_listener
            .accept()
            .await
            .expect("provider connection");
        let mut received = [0_u8; 4096];
        let _ = socket
            .read(&mut received)
            .await
            .expect("provider received request");
        // The body may have reached the provider before its response vanished.
    });
    let key_file = std::env::temp_dir().join(format!("concurro-org-gateway-{}", Uuid::now_v7()));
    std::fs::write(&key_file, "org-secret-test\n").expect("token file");
    let oauth_file =
        std::env::temp_dir().join(format!("concurro-oauth-gateway-{}", Uuid::now_v7()));
    std::fs::write(&oauth_file, "org-oauth-test\n").expect("OAuth token file");
    let openai_file =
        std::env::temp_dir().join(format!("concurro-openai-gateway-{}", Uuid::now_v7()));
    std::fs::write(&openai_file, "org-openai-test\n").expect("OpenAI token file");
    let route = OrgRoute {
        upstream: upstream.clone(),
        token: SecretRef::File(key_file.clone()),
        auth: "x-api-key".to_owned(),
        account: None,
        anthropic_beta: None,
    };
    let gateway = router(GatewayState {
        admission: concurro_exchange_client::AdmissionClient::new(
            reqwest::Client::new(),
            admission_url,
            "admission-test",
        ),
        provider: reqwest::Client::new(),
        routes: Arc::new(HashMap::from([
            ("claude_org".to_owned(), route),
            (
                "claude_oauth".to_owned(),
                OrgRoute {
                    upstream: upstream.clone(),
                    token: SecretRef::File(oauth_file.clone()),
                    auth: "bearer".to_owned(),
                    account: None,
                    anthropic_beta: Some("oauth-2025-04-20".to_owned()),
                },
            ),
            (
                "openai_org".to_owned(),
                OrgRoute {
                    upstream: upstream.clone(),
                    token: SecretRef::File(openai_file.clone()),
                    auth: "bearer".to_owned(),
                    account: None,
                    anthropic_beta: None,
                },
            ),
            (
                "dropper_org".to_owned(),
                OrgRoute {
                    upstream: dropper_upstream,
                    token: SecretRef::File(openai_file.clone()),
                    auth: "bearer".to_owned(),
                    account: None,
                    anthropic_beta: None,
                },
            ),
        ])),
        personal_routes: Arc::new(HashMap::new()),
        relay: None,
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
            .expect("gateway server");
    });
    let response = reqwest::Client::new()
        .post(format!("{gateway_url}/v1/messages"))
        .bearer_auth("org-session-key")
        .header("x-concurro-plan", "forged")
        .header("x-api-key", "forged")
        .header("anthropic-version", "2023-06-01")
        .header("originator", "codex_cli_rs")
        .header("connection", "x-drop-this")
        .header("x-drop-this", "not-forwarded")
        .json(&json!({"model":"claude-test","messages":[]}))
        .send()
        .await
        .expect("gateway request");
    assert_eq!(response.status(), 200);
    assert_eq!(
        response
            .headers()
            .get("x-provider-request-id")
            .expect("provider response metadata"),
        "provider-1"
    );
    assert_eq!(
        response.json::<Value>().await.expect("provider response")["usage"]["output_tokens"],
        5
    );
    let headers = received
        .lock()
        .await
        .take()
        .expect("provider received request");
    assert_eq!(
        headers.get("x-api-key").expect("org key"),
        "org-secret-test"
    );
    assert!(headers.get("x-concurro-plan").is_none());
    assert_eq!(
        headers.get("originator").expect("native metadata"),
        "codex_cli_rs"
    );
    assert_eq!(
        headers.get("anthropic-version").expect("Messages version"),
        "2023-06-01"
    );
    assert!(headers.get("x-drop-this").is_none());
    for _ in 0..40 {
        let count: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM events.outbox WHERE subject='inference.usage'",
        )
        .fetch_one(database.pool())
        .await
        .expect("usage count");
        if count == 1 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let counted: (i64, i64) = sqlx::query_as(
        "SELECT (usage->>'input_tokens')::bigint,(usage->>'output_tokens')::bigint \
         FROM exchange.inference_requests WHERE turn_id=$1",
    )
    .bind(turn)
    .fetch_one(database.pool())
    .await
    .expect("settled request");
    assert_eq!(counted, (13, 5));
    std::fs::write(&key_file, "org-secret-rotated\n").expect("operator rotated token");
    let rotated = reqwest::Client::new()
        .post(format!("{gateway_url}/v1/messages"))
        .bearer_auth("org-session-key")
        .json(&json!({"model":"claude-test","rotate":true}))
        .send()
        .await
        .expect("rotated request");
    assert_eq!(rotated.status(), StatusCode::OK);
    assert_eq!(
        rotated.json::<Value>().await.expect("rotated body")["usage"]["output_tokens"],
        1
    );
    assert_eq!(
        &*rotation_calls.lock().await,
        &["org-secret-test", "org-secret-rotated"]
    );
    let admitted: i64 =
        sqlx::query_scalar("SELECT count(*) FROM exchange.inference_requests WHERE turn_id=$1")
            .bind(turn)
            .fetch_one(database.pool())
            .await
            .expect("single rotation admission");
    assert_eq!(
        admitted, 2,
        "rotation retry created another chargeable request"
    );
    let mut hanging = reqwest::Client::new()
        .post(format!("{gateway_url}/v1/messages"))
        .bearer_auth("org-session-key")
        .json(&json!({"model":"claude-test","hang":true}))
        .send()
        .await
        .expect("open stream");
    assert_eq!(hanging.status(), 200);
    assert!(hanging.chunk().await.expect("first frame").is_some());
    let flood = reqwest::Client::new()
        .post(format!("{gateway_url}/v1/messages"))
        .bearer_auth("org-session-key")
        .json(&json!({"model":"claude-test","flood":true}))
        .send()
        .await
        .expect("backpressured stream");
    assert_eq!(flood.status(), 200);
    let gateway_disconnected = gateway_url.clone();
    let disconnected = tokio::spawn(async move {
        reqwest::Client::new()
            .post(format!("{gateway_disconnected}/v1/messages"))
            .bearer_auth("org-session-key")
            .json(&json!({"model":"claude-test","stall":true}))
            .send()
            .await
    });
    tokio::time::timeout(Duration::from_secs(3), stall_seen.notified())
        .await
        .expect("provider saw disconnectable request");
    let disconnected_request: Uuid = sqlx::query_scalar("SELECT request_id FROM exchange.inference_requests WHERE turn_id=$1 ORDER BY started DESC, request_id DESC LIMIT 1")
        .bind(turn).fetch_one(database.pool()).await.expect("disconnectable request id");
    disconnected.abort();
    let _ = disconnected.await;
    for _ in 0..40 {
        let unknown: Option<String> = sqlx::query_scalar("SELECT category FROM exchange.inference_requests WHERE request_id=$1 AND finished IS NOT NULL")
            .bind(disconnected_request).fetch_optional(database.pool()).await.expect("disconnect accounting");
        if unknown.is_some() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let disconnected_unknown: Option<String> = sqlx::query_scalar("SELECT category FROM exchange.inference_requests WHERE request_id=$1 AND finished IS NOT NULL")
        .bind(disconnected_request).fetch_optional(database.pool()).await.expect("settled disconnect");
    assert_eq!(
        disconnected_unknown.as_deref(),
        Some("unknown"),
        "stalled-header disconnect was not settled"
    );
    let gateway_stalled = gateway_url.clone();
    let stalled = tokio::spawn(async move {
        reqwest::Client::new()
            .post(format!("{gateway_stalled}/v1/messages"))
            .bearer_auth("org-session-key")
            .json(&json!({"model":"claude-test","stall":true}))
            .send()
            .await
            .expect("stalled request")
    });
    tokio::time::timeout(Duration::from_secs(3), stall_seen.notified())
        .await
        .expect("provider saw stalled request");
    control.revoke("org-1").await.expect("revoke grant");
    let ended = tokio::time::timeout(Duration::from_secs(13), hanging.chunk())
        .await
        .expect("stream closed after revoke")
        .expect("stream read");
    assert!(ended.is_none(), "revoked stream was not closed");
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(13), stalled)
            .await
            .expect("stalled headers cancelled")
            .expect("stalled join")
            .status(),
        503
    );
    for _ in 0..40 {
        let finished: i64 = sqlx::query_scalar("SELECT count(*) FROM exchange.inference_requests WHERE turn_id=$1 AND finished IS NOT NULL")
            .bind(turn).fetch_one(database.pool()).await.expect("finished count");
        if finished == 5 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let settled: i64 = sqlx::query_scalar("SELECT count(*) FROM exchange.inference_requests WHERE turn_id=$1 AND finished IS NOT NULL")
        .bind(turn).fetch_one(database.pool()).await.expect("settled requests");
    assert_eq!(settled, 5, "backpressured stream remained unsettled");
    drop(flood);
    let mut oauth_body: Value = serde_json::from_str(include_str!(
        "../../../contracts/fixtures/exchange.http.v1/operator_submit.json"
    ))
    .expect("OAuth grant fixture");
    let oauth_turn = Uuid::now_v7();
    oauth_body["attempt_id"] = json!(oauth_turn);
    oauth_body["grant"]["grant_id"] = json!(oauth_turn.to_string());
    oauth_body["grant"]["credential"]["route"] = json!("claude_oauth");
    let oauth_session = control
        .submit(oauth_turn, &oauth_body)
        .await
        .expect("OAuth grant submit")
        .dispatch
        .expect("OAuth dispatch")
        .session_id;
    sqlx::query(
        "UPDATE exchange.sessions SET session_key_hash=$2,runner_token_hash=$3, \
         bound_at=now(),last_seen=now() WHERE session_id=$1",
    )
    .bind(oauth_session)
    .bind(hex::encode(Sha256::digest(b"oauth-session-key")))
    .bind(hex::encode(Sha256::digest(b"oauth-runner-token")))
    .execute(database.pool())
    .await
    .expect("OAuth session bind");
    sqlx::query("UPDATE exchange.turns SET status='running',started=now() WHERE turn_id=$1")
        .bind(oauth_turn)
        .execute(database.pool())
        .await
        .expect("OAuth turn running");
    let response = reqwest::Client::new()
        .post(format!("{gateway_url}/v1/messages"))
        .bearer_auth("oauth-session-key")
        .header("x-api-key", "forged")
        .header("anthropic-beta", "feature-one")
        .json(&json!({"model":"claude-test","messages":[]}))
        .send()
        .await
        .expect("OAuth gateway request");
    assert_eq!(response.status(), 200);
    let _: Value = response.json().await.expect("OAuth provider response");
    let headers = received
        .lock()
        .await
        .take()
        .expect("OAuth provider headers");
    assert_eq!(
        headers.get("authorization").expect("OAuth bearer"),
        "Bearer org-oauth-test"
    );
    assert!(headers.get("x-api-key").is_none());
    assert_eq!(
        headers.get("anthropic-beta").expect("OAuth beta"),
        "feature-one,oauth-2025-04-20"
    );
    control
        .cancel(oauth_turn)
        .await
        .expect("release OAuth session");
    let openai_turn = Uuid::now_v7();
    let mut openai_body = oauth_body.clone();
    openai_body["attempt_id"] = json!(openai_turn);
    openai_body["grant"]["grant_id"] = json!(openai_turn.to_string());
    openai_body["grant"]["credential"]["route"] = json!("openai_org");
    let openai_session = control
        .submit(openai_turn, &openai_body)
        .await
        .expect("OpenAI grant submit")
        .dispatch
        .expect("OpenAI dispatch")
        .session_id;
    sqlx::query("UPDATE exchange.sessions SET session_key_hash=$2,runner_token_hash=$3,bound_at=now(),last_seen=now() WHERE session_id=$1")
        .bind(openai_session).bind(hex::encode(Sha256::digest(b"openai-session-key")))
        .bind(hex::encode(Sha256::digest(b"openai-runner-token")))
        .execute(database.pool()).await.expect("OpenAI session bind");
    sqlx::query("UPDATE exchange.turns SET status='running',started=now() WHERE turn_id=$1")
        .bind(openai_turn)
        .execute(database.pool())
        .await
        .expect("OpenAI turn running");
    let streamed = reqwest::Client::new()
        .post(format!("{gateway_url}/v1/responses"))
        .bearer_auth("openai-session-key")
        .header("openai-beta", "responses-2025-01")
        .header("originator", "codex_cli_rs")
        .header("session_id", "native-session")
        .header("version", "0.1.1")
        .json(&json!({"model":"claude-test","stream":true}))
        .send()
        .await
        .expect("Responses route");
    assert_eq!(streamed.status(), 200);
    assert_eq!(
        streamed
            .headers()
            .get("x-request-id")
            .expect("response metadata"),
        "response-1"
    );
    let length = streamed.bytes().await.expect("Responses stream").len();
    assert!(
        length > 8 * 1024 * 1024,
        "ordinary aggregate stream must exceed one-event limit"
    );
    let headers = received
        .lock()
        .await
        .take()
        .expect("OpenAI provider headers");
    assert_eq!(
        headers.get("authorization").expect("server bearer"),
        "Bearer org-openai-test"
    );
    for field in ["openai-beta", "originator", "session_id", "version"] {
        assert!(headers.get(field).is_some(), "native {field} omitted");
    }
    for _ in 0..40 {
        let usage: Option<i64> = sqlx::query_scalar("SELECT (usage->>'input_tokens')::bigint FROM exchange.inference_requests WHERE turn_id=$1")
            .bind(openai_turn).fetch_optional(database.pool()).await.expect("Responses usage").flatten();
        if usage == Some(9) {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let usage: Option<i64> = sqlx::query_scalar(
        "SELECT (usage->>'input_tokens')::bigint FROM exchange.inference_requests WHERE turn_id=$1",
    )
    .bind(openai_turn)
    .fetch_optional(database.pool())
    .await
    .expect("settled Responses usage")
    .flatten();
    assert_eq!(usage, Some(9));
    let failure = reqwest::Client::new()
        .post(format!("{gateway_url}/v1/responses"))
        .bearer_auth("openai-session-key")
        .json(&json!({"model":"claude-test","stream":true,"fail":true}))
        .send()
        .await
        .expect("HTTP 200 failure");
    let text = failure.text().await.expect("sanitized event");
    assert!(text.contains("quota_exhausted"));
    assert!(!text.contains("provider private text"));
    for _ in 0..40 {
        let category: Option<String> = sqlx::query_scalar("SELECT category FROM exchange.inference_requests WHERE turn_id=$1 AND category='quota_exhausted'")
            .bind(openai_turn).fetch_optional(database.pool()).await.expect("structured failure");
        if category.is_some() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let category: Option<String> = sqlx::query_scalar("SELECT category FROM exchange.inference_requests WHERE turn_id=$1 AND category='quota_exhausted'")
        .bind(openai_turn).fetch_optional(database.pool()).await.expect("settled structured failure");
    assert_eq!(category.as_deref(), Some("quota_exhausted"));
    control
        .cancel(openai_turn)
        .await
        .expect("release OpenAI session");
    let drop_turn = Uuid::now_v7();
    let mut drop_body = openai_body.clone();
    drop_body["attempt_id"] = json!(drop_turn);
    drop_body["grant"]["grant_id"] = json!(drop_turn.to_string());
    drop_body["grant"]["credential"]["route"] = json!("dropper_org");
    let drop_session = control
        .submit(drop_turn, &drop_body)
        .await
        .expect("dropper grant submit")
        .dispatch
        .expect("dropper dispatch")
        .session_id;
    sqlx::query("UPDATE exchange.sessions SET session_key_hash=$2,runner_token_hash=$3,bound_at=now(),last_seen=now() WHERE session_id=$1")
        .bind(drop_session).bind(hex::encode(Sha256::digest(b"dropper-session-key")))
        .bind(hex::encode(Sha256::digest(b"dropper-runner-token")))
        .execute(database.pool()).await.expect("dropper session bind");
    sqlx::query("UPDATE exchange.turns SET status='running',started=now() WHERE turn_id=$1")
        .bind(drop_turn)
        .execute(database.pool())
        .await
        .expect("dropper turn running");
    let uncertain = reqwest::Client::new()
        .post(format!("{gateway_url}/v1/responses"))
        .bearer_auth("dropper-session-key")
        .json(&json!({"model":"claude-test"}))
        .send()
        .await
        .expect("gateway transport response");
    assert_eq!(uncertain.status(), 502);
    for _ in 0..40 {
        let dispatched: Option<bool> = sqlx::query_scalar(
            "SELECT dispatched FROM exchange.inference_requests WHERE turn_id=$1",
        )
        .bind(drop_turn)
        .fetch_optional(database.pool())
        .await
        .expect("dispatch evidence")
        .flatten();
        if dispatched.is_some() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    let dispatched: Option<bool> =
        sqlx::query_scalar("SELECT dispatched FROM exchange.inference_requests WHERE turn_id=$1")
            .bind(drop_turn)
            .fetch_optional(database.pool())
            .await
            .expect("settled transport evidence")
            .flatten();
    assert_eq!(
        dispatched,
        Some(true),
        "post-send loss cannot claim definite non-dispatch"
    );
    control
        .cancel(drop_turn)
        .await
        .expect("release dropper session");
    let deadline_turn = Uuid::now_v7();
    let mut deadline_body = openai_body.clone();
    deadline_body["attempt_id"] = json!(deadline_turn);
    deadline_body["grant"]["grant_id"] = json!(deadline_turn.to_string());
    let deadline_session = control
        .submit(deadline_turn, &deadline_body)
        .await
        .expect("deadline grant submit")
        .dispatch
        .expect("deadline dispatch")
        .session_id;
    sqlx::query("UPDATE exchange.sessions SET session_key_hash=$2,bound_at=now(),last_seen=now(),deadline=now()+interval '2 seconds' WHERE session_id=$1")
        .bind(deadline_session).bind(hex::encode(Sha256::digest(b"deadline-session-key")))
        .execute(database.pool()).await.expect("short deadline bind");
    sqlx::query("UPDATE exchange.turns SET status='running',started=now() WHERE turn_id=$1")
        .bind(deadline_turn)
        .execute(database.pool())
        .await
        .expect("deadline turn running");
    let mut deadline_stream = reqwest::Client::new()
        .post(format!("{gateway_url}/v1/messages"))
        .bearer_auth("deadline-session-key")
        .json(&json!({"model":"claude-test","hang":true}))
        .send()
        .await
        .expect("open deadline stream");
    assert_eq!(deadline_stream.status(), StatusCode::OK);
    assert!(
        deadline_stream
            .chunk()
            .await
            .expect("first deadline event")
            .is_some()
    );
    let ended = tokio::time::timeout(Duration::from_secs(5), deadline_stream.chunk())
        .await
        .expect("deadline closed stream")
        .expect("stream read");
    assert!(ended.is_none(), "deadline left provider stream open");
    control
        .cancel(deadline_turn)
        .await
        .expect("release deadline session");
    let outage_turn = Uuid::now_v7();
    let mut outage_body = openai_body.clone();
    outage_body["attempt_id"] = json!(outage_turn);
    outage_body["grant"]["grant_id"] = json!(outage_turn.to_string());
    let outage_session = control
        .submit(outage_turn, &outage_body)
        .await
        .expect("outage grant submit")
        .dispatch
        .expect("outage dispatch")
        .session_id;
    sqlx::query("UPDATE exchange.sessions SET session_key_hash=$2,bound_at=now(),last_seen=now() WHERE session_id=$1")
        .bind(outage_session).bind(hex::encode(Sha256::digest(b"outage-session-key")))
        .execute(database.pool()).await.expect("outage session bind");
    sqlx::query("UPDATE exchange.turns SET status='running',started=now() WHERE turn_id=$1")
        .bind(outage_turn)
        .execute(database.pool())
        .await
        .expect("outage turn running");
    let mut outage_stream = reqwest::Client::new()
        .post(format!("{gateway_url}/v1/messages"))
        .bearer_auth("outage-session-key")
        .json(&json!({"model":"claude-test","hang":true}))
        .send()
        .await
        .expect("open outage stream");
    assert_eq!(outage_stream.status(), StatusCode::OK);
    assert!(
        outage_stream
            .chunk()
            .await
            .expect("first outage event")
            .is_some()
    );
    switch_server.abort();
    let _ = switch_server.await;
    for connection in switch_connections.lock().await.drain(..) {
        connection.abort();
    }
    let after_outage = reqwest::Client::new()
        .post(format!("{gateway_url}/v1/messages"))
        .bearer_auth("outage-session-key")
        .json(&json!({"model":"claude-test"}))
        .send()
        .await
        .expect("new request during exchange outage");
    assert_eq!(after_outage.status(), StatusCode::SERVICE_UNAVAILABLE);
    let ended = tokio::time::timeout(Duration::from_secs(40), outage_stream.chunk())
        .await
        .expect("outage closed stream within bound")
        .expect("stream read");
    assert!(ended.is_none(), "outage left provider stream open");
    std::fs::remove_file(key_file).expect("remove token file");
    std::fs::remove_file(oauth_file).expect("remove OAuth file");
    std::fs::remove_file(openai_file).expect("remove OpenAI file");
    gateway_server.abort();
    admission_server.abort();
    provider_server.abort();
    dropper_server.abort();
}
