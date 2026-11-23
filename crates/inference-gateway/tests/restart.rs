//! A gateway process restart drops its old stream and accepts the next request.

use std::time::Duration;

use axum::{
    Json, Router,
    body::{Body, Bytes},
    response::IntoResponse,
    routing::post,
};
use chrono::Utc;
use futures_util::{StreamExt, stream};
use serde_json::{Value, json};
use tokio::process::Command;
use uuid::Uuid;

/// Restart the actual gateway binary while a provider stream is still open.
#[tokio::test]
async fn process_restart_drops_inflight_stream_and_readmits() {
    let session = Uuid::now_v7();
    let admission = Router::new()
        .route(
            "/internal/v1/requests",
            post(move |Json(request): Json<Value>| async move {
                Json(json!({
                    "request_id": request["request_id"], "session_id": session,
                    "turn_id": Uuid::nil(), "grant_id": "restart-org",
                    "credential": {"route": "restart_org"}, "models": ["gpt-test"],
                    "remaining_tokens": 1000,
                    "deadline": (Utc::now() + chrono::Duration::minutes(2)).to_rfc3339()
                }))
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
    let admission_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("admission listener");
    let admission_url = format!(
        "http://{}",
        admission_listener.local_addr().expect("admission address")
    );
    let admission_server = tokio::spawn(async move {
        axum::serve(admission_listener, admission)
            .await
            .expect("admission fixture")
    });

    let provider = Router::new().route(
        "/v1/responses",
        post(|Json(body): Json<Value>| async move {
            if body["hang"] == true {
                let first = stream::once(async {
                    Ok::<_, std::io::Error>(Bytes::from_static(
                        b"data: {\"type\":\"response.created\"}\n\n",
                    ))
                });
                let rest = stream::pending::<Result<Bytes, std::io::Error>>();
                let mut response = Body::from_stream(first.chain(rest)).into_response();
                response
                    .headers_mut()
                    .insert("content-type", "text/event-stream".parse().expect("media"));
                response
            } else {
                Json(json!({"id":"ok","usage":{"input_tokens":1,"output_tokens":1}}))
                    .into_response()
            }
        }),
    );
    let provider_listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("provider listener");
    let provider_url = format!(
        "http://{}/v1",
        provider_listener.local_addr().expect("provider address")
    );
    let provider_server = tokio::spawn(async move {
        axum::serve(provider_listener, provider)
            .await
            .expect("provider fixture")
    });

    let directory =
        std::env::temp_dir().join(format!("concurro-gateway-restart-{}", Uuid::now_v7()));
    std::fs::create_dir_all(&directory).expect("fixture directory");
    let token_file = directory.join("token");
    let bearer_file = directory.join("bearer");
    let config_file = directory.join("gateway.toml");
    std::fs::write(&token_file, "provider-secret\n").expect("provider token");
    std::fs::write(&bearer_file, "admission-test\n").expect("admission bearer");
    let address = {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("gateway port");
        let address = listener.local_addr().expect("gateway address");
        drop(listener);
        address
    };
    std::fs::write(&config_file, format!(
        "listen = \"{address}\"\nexchange_admission_url = {admission_url:?}\nadmission_bearer_file = {:?}\n\
         [routes.restart_org]\nupstream = {provider_url:?}\ntoken = {:?}\nauth = \"bearer\"\n",
        bearer_file.display().to_string(), token_file.display().to_string(),
    )).expect("gateway config");

    let mut gateway = Command::new(env!("CARGO_BIN_EXE_inference-gateway"))
        .env("CONCURRO_INFERENCE_GATEWAY_CONFIG", &config_file)
        .kill_on_drop(true)
        .spawn()
        .expect("gateway process");
    for _ in 0..100 {
        if tokio::net::TcpStream::connect(address).await.is_ok() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let url = format!("http://{address}/v1/responses");
    let mut hanging = reqwest::Client::new()
        .post(&url)
        .bearer_auth("session-key")
        .json(&json!({"model":"gpt-test","hang":true}))
        .send()
        .await
        .expect("first gateway request");
    assert_eq!(hanging.status(), 200);
    assert!(hanging.chunk().await.expect("first event").is_some());
    gateway.kill().await.expect("kill gateway");
    let old_stream = tokio::time::timeout(Duration::from_secs(3), hanging.chunk())
        .await
        .expect("old stream closed");
    assert!(old_stream.is_err() || old_stream.expect("chunk").is_none());

    let mut restarted = Command::new(env!("CARGO_BIN_EXE_inference-gateway"))
        .env("CONCURRO_INFERENCE_GATEWAY_CONFIG", &config_file)
        .kill_on_drop(true)
        .spawn()
        .expect("restart gateway");
    for _ in 0..100 {
        if tokio::net::TcpStream::connect(address).await.is_ok() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let response = reqwest::Client::new()
        .post(&url)
        .bearer_auth("session-key")
        .json(&json!({"model":"gpt-test"}))
        .send()
        .await
        .expect("readmitted request");
    assert_eq!(response.status(), 200);
    assert_eq!(
        response.json::<Value>().await.expect("provider body")["id"],
        "ok"
    );
    restarted.kill().await.expect("stop gateway");
    admission_server.abort();
    provider_server.abort();
    std::fs::remove_dir_all(directory).expect("remove fixture files");
}
