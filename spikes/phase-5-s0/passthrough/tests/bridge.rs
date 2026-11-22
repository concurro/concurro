//! Spike: `responses-bridge` (concurro-relay) in front of the spike proxy. Codex's
//! WebSocket turns must reach the proxy as streaming `POST /v1/responses` carrying the
//! session key, and come back as WebSocket events. Set `BRIDGE_BIN` to the built binary;
//! the test is skipped without it.

use std::{
    net::SocketAddr,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use axum::{Router, body::Body, http::header, response::Response, routing::post};
use bytes::Bytes;
use futures_util::{SinkExt, StreamExt};
use s0_passthrough::{Proxy, router};
use tokio::{net::TcpListener, process::Command, sync::mpsc};
use tokio_stream::wrappers::ReceiverStream;
use tokio_tungstenite::tungstenite::{Message, client::IntoClientRequest};

const TURN: &[&str] = &[
    r#"{"type":"response.created","response":{"id":"r1"}}"#,
    r#"{"type":"response.output_text.delta","delta":"Hi"}"#,
    r#"{"type":"response.completed","response":{"id":"r1","usage":{"input_tokens":5,"output_tokens":2}}}"#,
];

async fn provider() -> (SocketAddr, Arc<Mutex<Option<Instant>>>) {
    let gone = Arc::new(Mutex::new(None));
    let g = gone.clone();
    let app = Router::new().route(
        "/v1/{*rest}",
        post(move || {
            let gone = g.clone();
            async move {
                let (tx, rx) = mpsc::channel::<Result<Bytes, std::io::Error>>(1);
                tokio::spawn(async move {
                    for event in TURN {
                        let event = Bytes::from(format!("data: {event}\n\n"));
                        if tx.send(Ok(event)).await.is_err() {
                            *gone.lock().unwrap() = Some(Instant::now());
                            return;
                        }
                        tokio::time::sleep(Duration::from_millis(300)).await;
                    }
                });
                Response::builder()
                    .header(header::CONTENT_TYPE, "text/event-stream")
                    .body(Body::from_stream(ReceiverStream::new(rx)))
                    .unwrap()
            }
        }),
    );
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    (addr, gone)
}

struct Stack {
    bridge: SocketAddr,
    authorizations: Arc<Mutex<Vec<String>>>,
    gone: Arc<Mutex<Option<Instant>>>,
    _child: tokio::process::Child,
}

async fn stack() -> Option<Stack> {
    let bin = std::env::var("BRIDGE_BIN").ok()?;
    let (upstream, gone) = provider().await;
    let authorizations = Arc::new(Mutex::new(Vec::new()));
    let state = Proxy {
        upstream: format!("http://{upstream}/v1/responses"),
        route: "chatgpt-subscription",
        client: reqwest::Client::new(),
        finished: Arc::default(),
        authorizations: authorizations.clone(),
    };
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let proxy = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, router(state)).await.unwrap() });
    let free = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let bridge = free.local_addr().unwrap();
    drop(free);
    let child = Command::new(bin)
        .args(["--listen", &bridge.to_string(), "--gateway", &format!("http://{proxy}")])
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    for _ in 0..100 {
        if tokio::net::TcpStream::connect(bridge).await.is_ok() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    Some(Stack { bridge, authorizations, gone, _child: child })
}

async fn codex(stack: &Stack) -> tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>> {
    let mut request = format!("ws://{}/v1/responses", stack.bridge).into_client_request().unwrap();
    request.headers_mut().insert("authorization", "Bearer sk-concurro-spike".parse().unwrap());
    tokio_tungstenite::connect_async(request).await.unwrap().0
}

async fn next_type(socket: &mut (impl StreamExt<Item = Result<Message, tokio_tungstenite::tungstenite::Error>> + Unpin)) -> String {
    loop {
        let message = tokio::time::timeout(Duration::from_secs(5), socket.next()).await.unwrap().unwrap().unwrap();
        if let Message::Text(text) = message {
            let value: serde_json::Value = serde_json::from_str(&text).unwrap();
            return value["type"].as_str().unwrap().to_owned();
        }
    }
}

const CREATE: &str = r#"{"type":"response.create","model":"gpt-5-codex","input":[{"type":"message","role":"user","content":"hi"}]}"#;

#[tokio::test]
async fn a_websocket_turn_streams_through_with_the_session_key() {
    let Some(stack) = stack().await else { return eprintln!("BRIDGE_BIN unset; skipped") };
    let mut socket = codex(&stack).await;
    socket.send(Message::Text(CREATE.into())).await.unwrap();
    let types = [next_type(&mut socket).await, next_type(&mut socket).await, next_type(&mut socket).await];
    assert_eq!(types, ["response.created", "response.output_text.delta", "response.completed"]);
    assert_eq!(*stack.authorizations.lock().unwrap(), ["Bearer sk-concurro-spike"]);
}

#[tokio::test]
async fn a_queued_second_turn_runs_after_the_first() {
    let Some(stack) = stack().await else { return eprintln!("BRIDGE_BIN unset; skipped") };
    let mut socket = codex(&stack).await;
    socket.send(Message::Text(CREATE.into())).await.unwrap();
    socket.send(Message::Text(CREATE.into())).await.unwrap();
    let mut completed = 0;
    while completed < 2 {
        if next_type(&mut socket).await == "response.completed" {
            completed += 1;
        }
    }
    assert_eq!(stack.authorizations.lock().unwrap().len(), 2);
}

#[tokio::test]
async fn a_cancel_stops_the_upstream_request() {
    let Some(stack) = stack().await else { return eprintln!("BRIDGE_BIN unset; skipped") };
    let mut socket = codex(&stack).await;
    socket.send(Message::Text(CREATE.into())).await.unwrap();
    assert_eq!(next_type(&mut socket).await, "response.created");
    socket.send(Message::Text(r#"{"type":"response.cancel"}"#.into())).await.unwrap();
    let cancelled = Instant::now();
    for _ in 0..100 {
        if stack.gone.lock().unwrap().is_some() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let noticed = stack.gone.lock().unwrap().expect("provider kept streaming after cancel");
    assert!(noticed.duration_since(cancelled) < Duration::from_secs(1));
}
