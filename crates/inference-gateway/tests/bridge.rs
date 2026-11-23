//! Exercise the S1 Codex WebSocket bridge through the real Rust gateway router.
//! Set `CONCURRO_RESPONSES_BRIDGE_BIN` to the pinned S1 binary for this integration check.

use std::{
    collections::HashMap,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use axum::{Json, Router, body::Body, http::HeaderMap, response::IntoResponse, routing::post};
use chrono::Utc;
use concurro_contracts::AdmissionRequest;
use concurro_exchange_client::AdmissionClient;
use concurro_inference_gateway::{GatewayState, OrgRoute, router};
use concurro_secrets::{SecretRef, Secrets};
use futures_util::{SinkExt, StreamExt, stream};
use serde_json::{Value, json};
use tokio::{
    process::Command,
    sync::{Mutex, Notify},
};
use tokio_tungstenite::tungstenite::{Message, client::IntoClientRequest};
use uuid::Uuid;

/// A Codex create and cancel travel through S1 bridge, gateway admission and provider.
#[tokio::test]
#[ignore = "requires the pinned S1 responses-bridge binary"]
async fn codex_websocket_uses_rust_gateway_and_cancels_provider() {
    let binary = std::env::var("CONCURRO_RESPONSES_BRIDGE_BIN")
        .expect("set CONCURRO_RESPONSES_BRIDGE_BIN to the pinned S1 binary");
    let session = Uuid::now_v7();
    let admissions = Arc::new(AtomicUsize::new(0));
    let admission_calls = admissions.clone();
    let admission = Router::new()
        .route(
            "/internal/v1/requests",
            post(move |Json(request): Json<Value>| {
                let admission_calls = admission_calls.clone();
                async move {
                    admission_calls.fetch_add(1, Ordering::SeqCst);
                    Json(json!({
                        "request_id": request["request_id"], "session_id": session,
                        "turn_id": Uuid::nil(), "grant_id":"bridge-org",
                        "credential":{"route":"chatgpt_org"}, "models":["gpt-test"],
                        "remaining_tokens":1000,
                        "deadline":(Utc::now()+chrono::Duration::minutes(2)).to_rfc3339()
                    }))
                }
            }),
        )
        .route(
            "/internal/v1/sessions/status",
            post(move || async move { Json(json!({"live":[session]})) }),
        )
        .route(
            "/internal/v1/requests/{id}/finish",
            post(|| async { Json(json!({})) }),
        );
    let admission_listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let admission_url = format!("http://{}", admission_listener.local_addr().unwrap());
    let admission_server =
        tokio::spawn(async move { axum::serve(admission_listener, admission).await.unwrap() });
    AdmissionClient::new(
        reqwest::Client::new(),
        admission_url.clone(),
        "admission-test",
    )
    .request(&AdmissionRequest {
        key: "bridge-session-key".to_owned(),
        model: "gpt-test".to_owned(),
        request_id: Some(Uuid::now_v7()),
        probe: None,
    })
    .await
    .expect("fixture admission is valid");

    let seen = Arc::new(Mutex::new(Vec::<(HeaderMap, Value)>::new()));
    let cancelled = Arc::new(Notify::new());
    let provider_seen = seen.clone();
    let provider_cancelled = cancelled.clone();
    let provider = Router::new().route("/v1/responses", post(move |headers: HeaderMap, Json(body): Json<Value>| {
        let seen = provider_seen.clone();
        let cancelled = provider_cancelled.clone();
        async move {
            seen.lock().await.push((headers, body.clone()));
            let first = axum::body::Bytes::from_static(
                b"data: {\"type\":\"response.created\",\"response\":{\"id\":\"r1\"}}\n\n",
            );
            let remaining = if body["cancel"] == true {
                let (sender, receiver) = tokio::sync::mpsc::channel::<Result<axum::body::Bytes, std::io::Error>>(1);
                tokio::spawn(async move {
                    let _ = sender.send(Ok(first)).await;
                    sender.closed().await;
                    cancelled.notify_one();
                });
                Body::from_stream(stream::unfold(receiver, |mut receiver| async move {
                    receiver.recv().await.map(|chunk| (chunk, receiver))
                }))
            } else {
                let terminal = stream::once(async {
                    Ok::<_, std::io::Error>(axum::body::Bytes::from_static(
                        b"data: {\"type\":\"response.completed\",\"response\":{\"id\":\"r1\",\"usage\":{\"input_tokens\":2,\"output_tokens\":1}}}\n\n",
                    ))
                });
                Body::from_stream(stream::once(async { Ok::<_, std::io::Error>(first) }).chain(terminal))
            };
            let mut response = remaining.into_response();
            response.headers_mut().insert("content-type", "text/event-stream".parse().unwrap());
            response
        }
    }));
    let provider_listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let upstream = format!("http://{}/v1", provider_listener.local_addr().unwrap());
    let provider_server =
        tokio::spawn(async move { axum::serve(provider_listener, provider).await.unwrap() });

    let token_file = std::env::temp_dir().join(format!("concurro-bridge-token-{}", Uuid::now_v7()));
    let account_file =
        std::env::temp_dir().join(format!("concurro-bridge-account-{}", Uuid::now_v7()));
    std::fs::write(&token_file, "bridge-token\n").unwrap();
    std::fs::write(&account_file, "bridge-account\n").unwrap();
    let gateway = router(GatewayState {
        admission: AdmissionClient::new(reqwest::Client::new(), admission_url, "admission-test"),
        provider: reqwest::Client::new(),
        routes: Arc::new(HashMap::from([(
            "chatgpt_org".to_owned(),
            OrgRoute {
                upstream,
                token: SecretRef::File(token_file.clone()),
                auth: "bearer".to_owned(),
                account: Some(SecretRef::File(account_file.clone())),
                anthropic_beta: None,
            },
        )])),
        personal_routes: Arc::new(HashMap::new()),
        relay: None,
        secrets: Arc::new(Secrets::new(None, Duration::from_secs(60))),
    });
    let gateway_listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let gateway_url = format!("http://{}", gateway_listener.local_addr().unwrap());
    let gateway_server =
        tokio::spawn(async move { axum::serve(gateway_listener, gateway).await.unwrap() });

    let free = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let bridge = free.local_addr().unwrap();
    drop(free);
    let mut child = Command::new(binary)
        .args(["--listen", &bridge.to_string(), "--gateway", &gateway_url])
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    for _ in 0..100 {
        if tokio::net::TcpStream::connect(bridge).await.is_ok() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let mut request = format!("ws://{bridge}/v1/responses")
        .into_client_request()
        .unwrap();
    request.headers_mut().insert(
        "authorization",
        "Bearer bridge-session-key".parse().unwrap(),
    );
    let (mut socket, _) = tokio_tungstenite::connect_async(request.clone())
        .await
        .unwrap();
    socket
        .send(Message::Text(
            json!({"type":"response.create","model":"gpt-test","input":[]})
                .to_string()
                .into(),
        ))
        .await
        .unwrap();
    for expected in ["response.created", "response.completed"] {
        let message = tokio::time::timeout(Duration::from_secs(5), socket.next())
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        let Message::Text(text) = message else {
            panic!("expected provider event")
        };
        assert_eq!(
            serde_json::from_str::<Value>(&text).unwrap()["type"],
            expected
        );
    }
    let (headers, body) = seen.lock().await[0].clone();
    assert_eq!(headers["authorization"], "Bearer bridge-token");
    assert_eq!(headers["chatgpt-account-id"], "bridge-account");
    assert_eq!(body["stream"], true);
    assert!(body.get("type").is_none());
    socket.close(None).await.unwrap();

    let (mut socket, _) = tokio_tungstenite::connect_async(request).await.unwrap();
    socket
        .send(Message::Text(
            json!({"type":"response.create","model":"gpt-test","input":[],"cancel":true})
                .to_string()
                .into(),
        ))
        .await
        .unwrap();
    tokio::time::timeout(Duration::from_secs(5), async {
        while seen.lock().await.len() < 2 {
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .unwrap_or_else(|_| {
        panic!(
            "second bridge request reached provider; admissions={}",
            admissions.load(Ordering::SeqCst)
        )
    });
    socket
        .send(Message::Text(
            json!({"type":"response.cancel"}).to_string().into(),
        ))
        .await
        .unwrap();
    // A cancelled bridge request closes its gateway response without a terminal event.
    tokio::time::timeout(Duration::from_secs(5), cancelled.notified())
        .await
        .expect("provider stream closed after WebSocket cancel");
    child.kill().await.unwrap();
    admission_server.abort();
    provider_server.abort();
    gateway_server.abort();
    std::fs::remove_file(token_file).unwrap();
    std::fs::remove_file(account_file).unwrap();
}
