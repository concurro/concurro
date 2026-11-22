//! S0 spike: stream a provider response through unchanged while tapping its events for
//! usage and error categories, as `gateway/provider_protocol.py` does.

use std::sync::{Arc, Mutex};

use axum::{
    Router,
    body::Body,
    extract::State,
    http::{HeaderMap, StatusCode, header},
    response::Response,
    routing::post,
};
use bytes::Bytes;
use futures_util::StreamExt;
use serde_json::Value;

/// Largest single SSE event the tap parses; a bigger one makes usage ambiguous.
pub const MAX_EVENT: usize = 8 * 1024 * 1024;

/// What the tap learned from one response.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Observation {
    pub category: Option<String>,
    pub usage: Option<(u64, u64)>,
    pub completed: bool,
    pub ambiguous: bool,
    input_tokens: Option<u64>,
}

impl Observation {
    /// Inspect one decoded JSON event (port of `Observation.accept`).
    pub fn accept(&mut self, route: &str, value: &Value) {
        let Some(object) = value.as_object() else {
            self.ambiguous = true;
            return;
        };
        let kind = object.get("type").and_then(Value::as_str);
        if matches!(kind, Some("error" | "response.failed")) || object.get("error").is_some_and(|e| !e.is_null()) {
            self.category = Some(category(route, value).unwrap_or("unknown").to_owned());
            return;
        }
        let response = if kind == Some("response.completed") { value.get("response").unwrap_or(value) } else { value };
        if kind == Some("response.completed")
            || (object.get("object").and_then(Value::as_str) == Some("response")
                && object.get("status").and_then(Value::as_str) == Some("completed"))
        {
            self.completed = true;
            self.usage = tokens(response.get("usage"));
        }
        if kind == Some("message") && object.get("stop_reason").is_some_and(|v| !v.is_null()) {
            self.completed = true;
            self.usage = tokens(object.get("usage"));
        }
        if kind == Some("message_start") {
            self.input_tokens = value.pointer("/message/usage/input_tokens").and_then(Value::as_u64);
        }
        if kind == Some("message_delta") {
            let output = value.pointer("/usage/output_tokens").and_then(Value::as_u64);
            self.usage = self.input_tokens.zip(output);
        }
        if kind == Some("message_stop") {
            self.completed = true;
        }
    }

    /// Leave truncated, unsupported and incomplete usage unknown (port of `finish`).
    pub fn finish(mut self) -> Self {
        if self.ambiguous || !self.completed {
            self.category.get_or_insert_with(|| "unknown".to_owned());
            self.usage = None;
        }
        self
    }
}

fn tokens(value: Option<&Value>) -> Option<(u64, u64)> {
    let value = value?;
    Some((value.get("input_tokens")?.as_u64()?, value.get("output_tokens")?.as_u64()?))
}

fn category(route: &str, value: &Value) -> Option<&'static str> {
    let error = if value.get("type").and_then(Value::as_str) == Some("response.failed") {
        value.pointer("/response/error")?
    } else {
        value.get("error")?
    };
    let code = error.get("code").and_then(Value::as_str);
    let kind = error.get("type").and_then(Value::as_str);
    match route {
        "openai-api-key" => match code {
            Some("invalid_api_key") => Some("auth"),
            Some("insufficient_quota" | "credit_balance_exhausted") => Some("quota_exhausted"),
            Some("rate_limit_exceeded" | "slow_down") => Some("rate_limited"),
            Some("model_not_found") => Some("model_denied"),
            _ => None,
        },
        "anthropic-api-key" | "claude-subscription" => match kind {
            Some("authentication_error") => Some("auth"),
            Some("billing_error") => Some("quota_exhausted"),
            Some("permission_error" | "not_found_error") => Some("model_denied"),
            Some("rate_limit_error")
                if error.pointer("/details/error_code").and_then(Value::as_str)
                    == Some("enforced_spend_limit_reached") =>
            {
                Some("quota_exhausted")
            }
            Some("rate_limit_error") => Some("unavailable"),
            _ => None,
        },
        _ => None,
    }
}

/// Splits a byte stream into SSE events and feeds each `data:` payload to the observation.
#[derive(Default)]
pub struct Tap {
    pending: Vec<u8>,
    pub observation: Observation,
}

impl Tap {
    pub fn feed(&mut self, route: &str, chunk: &[u8]) {
        if self.observation.ambiguous {
            return;
        }
        self.pending.extend_from_slice(chunk);
        while let Some(end) = find(&self.pending, b"\n\n") {
            let event: Vec<u8> = self.pending.drain(..end + 2).collect();
            // A complete event can arrive in one chunk, so its own size is checked too.
            if event.len() > MAX_EVENT {
                self.observation.ambiguous = true;
                self.pending.clear();
                return;
            }
            for line in event.split(|b| *b == b'\n') {
                if let Some(data) = line.strip_prefix(b"data:") {
                    match serde_json::from_slice::<Value>(data.trim_ascii()) {
                        Ok(value) => self.observation.accept(route, &value),
                        Err(_) => self.observation.ambiguous = true,
                    }
                }
            }
        }
        if self.pending.len() > MAX_EVENT {
            self.observation.ambiguous = true;
            self.pending.clear();
        }
    }
}

fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}

/// Spike proxy state: one upstream, one route, and where finished observations go.
#[derive(Clone)]
pub struct Proxy {
    pub upstream: String,
    pub route: &'static str,
    pub client: reqwest::Client,
    pub finished: Arc<Mutex<Vec<Observation>>>,
    /// Session keys as received, to check a bridge passes them through intact.
    pub authorizations: Arc<Mutex<Vec<String>>>,
}

/// Forward one POST unchanged, streaming the response back as it arrives.
async fn forward(State(proxy): State<Proxy>, headers: HeaderMap, body: Bytes) -> Result<Response, StatusCode> {
    if let Some(key) = headers.get(header::AUTHORIZATION).and_then(|v| v.to_str().ok()) {
        proxy.authorizations.lock().unwrap().push(key.to_owned());
    }
    let mut request = proxy.client.post(&proxy.upstream).body(body).header(header::ACCEPT_ENCODING, "identity");
    if let Some(kind) = headers.get(header::CONTENT_TYPE) {
        request = request.header(header::CONTENT_TYPE, kind);
    }
    let upstream = request.send().await.map_err(|_| StatusCode::BAD_GATEWAY)?;
    let status = upstream.status();
    let media = upstream.headers().get(header::CONTENT_TYPE).cloned();
    let route = proxy.route;
    let finished = proxy.finished.clone();
    // The tap lives in the stream; when the client goes away the stream is dropped, the
    // upstream response with it, and the observation is recorded as incomplete.
    struct Recorder {
        tap: Tap,
        finished: Arc<Mutex<Vec<Observation>>>,
    }
    impl Drop for Recorder {
        fn drop(&mut self) {
            let observation = std::mem::take(&mut self.tap.observation).finish();
            self.finished.lock().unwrap().push(observation);
        }
    }
    let mut recorder = Recorder { tap: Tap::default(), finished };
    let stream = upstream.bytes_stream().map(move |chunk| {
        if let Ok(bytes) = &chunk {
            recorder.tap.feed(route, bytes);
        }
        chunk
    });
    let mut response = Response::builder().status(status.as_u16());
    if let Some(media) = media {
        response = response.header(header::CONTENT_TYPE, media);
    }
    response.body(Body::from_stream(stream)).map_err(|_| StatusCode::BAD_GATEWAY)
}

pub fn router(proxy: Proxy) -> Router {
    Router::new().route("/v1/{*rest}", post(forward)).with_state(proxy)
}
