//! Spike tests: a fake provider streams recorded-shape SSE events through the proxy.

use std::{
    net::SocketAddr,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use axum::{Router, body::Body, http::header, response::Response, routing::post};
use bytes::Bytes;
use futures_util::StreamExt;
use s0_passthrough::{MAX_EVENT, Observation, Proxy, router};
use tokio::{net::TcpListener, sync::mpsc};
use tokio_stream::wrappers::ReceiverStream;

const MESSAGES: &[&str] = &[
    r#"{"type":"message_start","message":{"usage":{"input_tokens":120,"output_tokens":1}}}"#,
    r#"{"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello"}}"#,
    r#"{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":42}}"#,
    r#"{"type":"message_stop"}"#,
];
const RESPONSES: &[&str] = &[
    r#"{"type":"response.created","response":{"id":"r1"}}"#,
    r#"{"type":"response.output_text.delta","delta":"Hi"}"#,
    r#"{"type":"response.completed","response":{"id":"r1","usage":{"input_tokens":77,"output_tokens":9}}}"#,
];
const ANTHROPIC_LIMIT: &[&str] = &[r#"{"type":"error","error":{"type":"rate_limit_error","message":"slow"}}"#];

fn sse(payload: &str) -> Bytes {
    Bytes::from(format!("event: x\ndata: {payload}\n\n"))
}

/// A fake provider that sends `events` 150 ms apart, recording when each was sent and
/// whether a send failed because the reader went away.
async fn provider(events: Vec<Bytes>) -> (SocketAddr, Arc<Mutex<Vec<Instant>>>, Arc<Mutex<Option<Instant>>>) {
    let sent = Arc::new(Mutex::new(Vec::new()));
    let gone = Arc::new(Mutex::new(None));
    let (s, g) = (sent.clone(), gone.clone());
    let app = Router::new().route(
        "/v1/{*rest}",
        post(move || {
            let (events, sent, gone) = (events.clone(), s.clone(), g.clone());
            async move {
                let (tx, rx) = mpsc::channel::<Result<Bytes, std::io::Error>>(1);
                tokio::spawn(async move {
                    for event in events {
                        if tx.send(Ok(event)).await.is_err() {
                            *gone.lock().unwrap() = Some(Instant::now());
                            return;
                        }
                        sent.lock().unwrap().push(Instant::now());
                        tokio::time::sleep(Duration::from_millis(150)).await;
                    }
                    // Keep probing so a closed reader is noticed even after the last event.
                    for _ in 0..40 {
                        if tx.send(Ok(Bytes::from_static(b": keepalive\n\n"))).await.is_err() {
                            *gone.lock().unwrap() = Some(Instant::now());
                            return;
                        }
                        tokio::time::sleep(Duration::from_millis(50)).await;
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
    (addr, sent, gone)
}

async fn proxy(upstream: SocketAddr, route: &'static str) -> (SocketAddr, Arc<Mutex<Vec<Observation>>>) {
    let finished = Arc::new(Mutex::new(Vec::new()));
    let state = Proxy {
        upstream: format!("http://{upstream}/v1/messages"),
        route,
        client: reqwest::Client::new(),
        finished: finished.clone(),
        authorizations: Arc::default(),
    };
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, router(state)).await.unwrap() });
    (addr, finished)
}

/// Read the proxied stream fully, timing each chunk.
async fn read_all(addr: SocketAddr) -> (Vec<u8>, Vec<Instant>) {
    let response = reqwest::Client::new()
        .post(format!("http://{addr}/v1/messages"))
        .body("{}")
        .send()
        .await
        .unwrap();
    let mut stream = response.bytes_stream();
    let (mut bytes, mut seen) = (Vec::new(), Vec::new());
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.unwrap();
        if !chunk.starts_with(b": keepalive") {
            seen.push(Instant::now());
            bytes.extend_from_slice(&chunk);
        } else {
            bytes.extend_from_slice(&chunk);
        }
    }
    (bytes, seen)
}

async fn settled(finished: &Arc<Mutex<Vec<Observation>>>) -> Observation {
    for _ in 0..50 {
        if let Some(observation) = finished.lock().unwrap().first().cloned() {
            return observation;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    panic!("no observation recorded");
}

#[tokio::test]
async fn messages_stream_passes_through_unbuffered_with_usage() {
    let events: Vec<Bytes> = MESSAGES.iter().map(|e| sse(e)).collect();
    let expected: Vec<u8> = events.iter().flat_map(|e| e.to_vec()).collect();
    let (upstream, sent, _) = provider(events).await;
    let (addr, finished) = proxy(upstream, "claude-subscription").await;
    let (bytes, seen) = read_all(addr).await;
    let body: Vec<u8> = bytes.split(|_| false).flatten().copied().collect();
    let without_keepalive = String::from_utf8(body).unwrap().replace(": keepalive\n\n", "");
    assert_eq!(without_keepalive.as_bytes(), expected.as_slice(), "byte-for-byte passthrough");
    let sent = sent.lock().unwrap().clone();
    // Each event reached the client before the provider sent the next one.
    for (index, received) in seen.iter().enumerate().take(sent.len() - 1) {
        assert!(*received < sent[index + 1], "event {index} was buffered");
    }
    let observation = settled(&finished).await;
    assert_eq!(observation.usage, Some((120, 42)));
    assert!(observation.completed && observation.category.is_none());
}

#[tokio::test]
async fn responses_stream_reports_usage() {
    let (upstream, _, _) = provider(RESPONSES.iter().map(|e| sse(e)).collect()).await;
    let (addr, finished) = proxy(upstream, "openai-api-key").await;
    read_all(addr).await;
    assert_eq!(settled(&finished).await.usage, Some((77, 9)));
}

#[tokio::test]
async fn provider_error_is_categorized() {
    let (upstream, _, _) = provider(ANTHROPIC_LIMIT.iter().map(|e| sse(e)).collect()).await;
    let (addr, finished) = proxy(upstream, "claude-subscription").await;
    read_all(addr).await;
    let observation = settled(&finished).await;
    assert_eq!(observation.category.as_deref(), Some("unavailable"));
    assert_eq!(observation.usage, None);
}

#[tokio::test]
async fn oversized_event_makes_usage_ambiguous() {
    let huge = format!(r#"{{"type":"content_block_delta","delta":{{"text":"{}"}}}}"#, "x".repeat(MAX_EVENT + 10));
    let mut events: Vec<Bytes> = vec![sse(MESSAGES[0]), Bytes::from(format!("data: {huge}"))];
    events.push(Bytes::from_static(b"\n\n"));
    events.extend(MESSAGES[2..].iter().map(|e| sse(e)));
    let (upstream, _, _) = provider(events).await;
    let (addr, finished) = proxy(upstream, "claude-subscription").await;
    read_all(addr).await;
    let observation = settled(&finished).await;
    assert!(observation.ambiguous);
    assert_eq!(observation.usage, None);
}

#[tokio::test]
async fn oversized_event_in_one_chunk_makes_usage_ambiguous() {
    let huge = format!(r#"{{"type":"content_block_delta","delta":{{"text":"{}"}}}}"#, "x".repeat(MAX_EVENT + 10));
    let mut events: Vec<Bytes> = vec![sse(MESSAGES[0]), sse(&huge)];
    events.extend(MESSAGES[2..].iter().map(|e| sse(e)));
    let (upstream, _, _) = provider(events).await;
    let (addr, finished) = proxy(upstream, "claude-subscription").await;
    read_all(addr).await;
    let observation = settled(&finished).await;
    assert!(observation.ambiguous);
    assert_eq!(observation.usage, None);
}

#[test]
fn an_oversized_complete_event_is_ambiguous_whatever_the_chunking() {
    use s0_passthrough::Tap;
    let huge = format!("data: {{\"type\":\"ping\",\"pad\":\"{}\"}}\n\n", "x".repeat(MAX_EVENT));
    let tail = [MESSAGES[2], MESSAGES[3]].map(|e| format!("data: {e}\n\n")).concat();
    // One chunk holding the whole oversized event, then the rest.
    let mut tap = Tap::default();
    tap.feed("claude-subscription", format!("data: {}\n\n", MESSAGES[0]).as_bytes());
    tap.feed("claude-subscription", huge.as_bytes());
    tap.feed("claude-subscription", tail.as_bytes());
    assert!(tap.observation.clone().finish().usage.is_none());
    // The same bytes split mid-event.
    let mut tap = Tap::default();
    tap.feed("claude-subscription", format!("data: {}\n\n", MESSAGES[0]).as_bytes());
    let (a, b) = huge.as_bytes().split_at(huge.len() / 2);
    tap.feed("claude-subscription", a);
    tap.feed("claude-subscription", b);
    tap.feed("claude-subscription", tail.as_bytes());
    assert!(tap.observation.clone().finish().usage.is_none());
}

#[tokio::test]
async fn client_disconnect_stops_the_upstream_request() {
    let (upstream, _, gone) = provider(MESSAGES.iter().map(|e| sse(e)).collect()).await;
    let (addr, finished) = proxy(upstream, "claude-subscription").await;
    let response = reqwest::Client::new()
        .post(format!("http://{addr}/v1/messages"))
        .body("{}")
        .send()
        .await
        .unwrap();
    let mut stream = response.bytes_stream();
    stream.next().await.unwrap().unwrap();
    let dropped = Instant::now();
    drop(stream);
    for _ in 0..100 {
        if gone.lock().unwrap().is_some() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let noticed = gone.lock().unwrap().expect("provider never saw the reader go away");
    assert!(noticed.duration_since(dropped) < Duration::from_secs(1), "upstream kept streaming");
    let observation = settled(&finished).await;
    assert_eq!(observation.usage, None, "an interrupted stream reports no usage");
    assert_eq!(observation.category.as_deref(), Some("unknown"));
}
