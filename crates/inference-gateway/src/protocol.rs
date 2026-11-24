//! Bounded provider observation while leaving successful response frames intact.

use axum::body::Bytes;
use concurro_contracts::UsageTokens;
use serde_json::{Value, json};

/// Largest single event or JSON error body inspected by the gateway.
pub const MAX_EVENT: usize = 8 * 1024 * 1024;

/// Trusted route family used only for structured error classification.
#[derive(Clone, Copy)]
pub enum RouteKind {
    /// OpenAI API key route.
    OpenAi,
    /// ChatGPT subscription route.
    ChatGpt,
    /// Anthropic Messages route.
    Anthropic,
}

impl RouteKind {
    /// Derive the response grammar from the frozen route and request path.
    pub fn for_request(chatgpt: bool, path: &str) -> Self {
        if path.ends_with("messages") {
            Self::Anthropic
        } else if chatgpt {
            Self::ChatGpt
        } else {
            Self::OpenAi
        }
    }
}

/// One provider response parsed incrementally without retaining its full stream.
pub struct Observation {
    route: RouteKind,
    streaming: bool,
    unknown_media: bool,
    pending: Vec<u8>,
    pub category: Option<&'static str>,
    pub usage: Option<UsageTokens>,
    pub completed: bool,
    pub ambiguous: bool,
    pub failed: bool,
    input: Option<UsageTokens>,
}

impl Observation {
    /// Start with the provider's declared media type.
    pub fn new(route: RouteKind, media: Option<&str>) -> Self {
        Self {
            route,
            streaming: media
                .is_some_and(|value| value.to_ascii_lowercase().contains("text/event-stream")),
            unknown_media: media.is_none_or(|value| value.trim().is_empty()),
            pending: Vec::new(),
            category: None,
            usage: None,
            completed: false,
            ambiguous: false,
            failed: false,
            input: None,
        }
    }

    /// Consume one provider chunk and return safe bytes ready for the downstream reader.
    pub fn feed(&mut self, bytes: &[u8]) -> Vec<Bytes> {
        if self.failed || self.ambiguous {
            return Vec::new();
        }
        if self.unknown_media {
            self.pending.extend_from_slice(bytes);
            if self.pending.len() < 6 {
                return Vec::new();
            }
            self.unknown_media = false;
            self.streaming = [b"data:".as_slice(), b"event:".as_slice(), b":".as_slice()]
                .iter()
                .any(|prefix| self.pending.starts_with(prefix));
            if self.streaming {
                let pending = std::mem::take(&mut self.pending);
                return self.feed(&pending);
            }
            if self.pending.len() > MAX_EVENT {
                self.ambiguous = true;
                self.pending.clear();
            }
            return Vec::new();
        }
        if !self.streaming {
            if self.pending.len().saturating_add(bytes.len()) > MAX_EVENT {
                self.ambiguous = true;
                self.pending.clear();
            } else {
                self.pending.extend_from_slice(bytes);
            }
            return Vec::new();
        }
        self.pending.extend_from_slice(bytes);
        let mut output = Vec::new();
        while let Some((end, width)) = self
            .pending
            .windows(2)
            .position(|part| part == b"\n\n")
            .map(|end| (end, 2))
            .or_else(|| {
                self.pending
                    .windows(4)
                    .position(|part| part == b"\r\n\r\n")
                    .map(|end| (end, 4))
            })
        {
            let event: Vec<u8> = self.pending.drain(..end + width).collect();
            if event.len() > MAX_EVENT {
                self.ambiguous = true;
                break;
            }
            let mut data = Vec::new();
            let mut error_label = false;
            for line in event.split(|byte| *byte == b'\n') {
                let line = line.strip_suffix(b"\r").unwrap_or(line);
                if let Some(value) = line.strip_prefix(b"data:") {
                    if !data.is_empty() {
                        data.push(b'\n');
                    }
                    data.extend_from_slice(value.trim_ascii());
                }
                if line
                    .strip_prefix(b"event:")
                    .is_some_and(|value| value.trim_ascii() == b"error")
                {
                    error_label = true;
                }
            }
            if !data.is_empty() && data != b"[DONE]" {
                match serde_json::from_slice::<Value>(&data) {
                    Ok(value) => self.accept(&value),
                    Err(_) => self.ambiguous = true,
                }
            }
            if error_label {
                self.category.get_or_insert("unknown");
                self.failed = true;
            }
            if self.ambiguous || self.failed {
                output.push(self.safe_error_frame());
                break;
            }
            output.push(Bytes::from(event));
        }
        if self.pending.len() > MAX_EVENT {
            self.ambiguous = true;
            self.pending.clear();
            output.push(self.safe_error_frame());
        }
        output
    }

    /// Accept only known terminal shapes and structured error fields.
    fn accept(&mut self, value: &Value) {
        let Some(object) = value.as_object() else {
            self.ambiguous = true;
            return;
        };
        let kind = object.get("type").and_then(Value::as_str);
        if matches!(kind, Some("error" | "response.failed"))
            || object.get("error").is_some_and(|error| !error.is_null())
        {
            self.category = Some(classify(self.route, value).unwrap_or("unknown"));
            self.failed = true;
            return;
        }
        let response = if kind == Some("response.completed") {
            value.get("response").unwrap_or(value)
        } else {
            value
        };
        if kind == Some("response.completed")
            || (object.get("object").and_then(Value::as_str) == Some("response")
                && object.get("status").and_then(Value::as_str) == Some("completed"))
        {
            self.completed = true;
            self.usage = response.get("usage").and_then(usage_fields);
        }
        if kind == Some("message")
            && object
                .get("stop_reason")
                .is_some_and(|reason| !reason.is_null())
        {
            self.completed = true;
            self.usage = object.get("usage").and_then(usage_fields);
        }
        if kind == Some("message_start") {
            self.input = value
                .pointer("/message/usage")
                .and_then(usage_fields_partial);
        }
        if kind == Some("message_delta") {
            let output = value
                .pointer("/usage/output_tokens")
                .and_then(Value::as_u64);
            self.usage = self
                .input
                .as_ref()
                .zip(output)
                .map(|(input, output_tokens)| UsageTokens {
                    input_tokens: input.input_tokens,
                    output_tokens,
                    cache_read_input_tokens: input.cache_read_input_tokens,
                    cache_creation_input_tokens: input.cache_creation_input_tokens,
                });
        }
        if kind == Some("message_stop") {
            self.completed = true;
        }
    }

    /// Complete a JSON response or close an incomplete stream conservatively.
    pub fn finish(mut self) -> (Self, Option<Bytes>) {
        let mut body = None;
        if !self.streaming && !self.ambiguous {
            match serde_json::from_slice::<Value>(&self.pending) {
                Ok(value) => {
                    self.accept(&value);
                    body = Some(if self.failed {
                        self.safe_error_json()
                    } else {
                        Bytes::from(std::mem::take(&mut self.pending))
                    });
                }
                Err(_) => self.ambiguous = true,
            }
        }
        if self.ambiguous || !self.completed {
            self.category.get_or_insert("unknown");
            self.usage = None;
        }
        (self, body)
    }

    /// Return an error frame with no provider-supplied message text.
    fn safe_error_frame(&self) -> Bytes {
        let value = json!({"error":{"type":self.category.unwrap_or("unknown"),"message":"provider request failed"}});
        Bytes::from(format!("event: error\ndata: {value}\n\n"))
    }

    /// Return an error JSON body with only the classified type.
    fn safe_error_json(&self) -> Bytes {
        Bytes::from(json!({"error":{"type":self.category.unwrap_or("unknown"),"message":"provider request failed"}}).to_string())
    }
}

/// Read required totals and optional cache counters without zero-filling missing counts.
fn usage_fields(value: &Value) -> Option<UsageTokens> {
    let mut usage = usage_fields_partial(value)?;
    usage.output_tokens = value.get("output_tokens")?.as_u64()?;
    Some(usage)
}

/// Read input and cache fields from an Anthropic start event.
fn usage_fields_partial(value: &Value) -> Option<UsageTokens> {
    Some(UsageTokens {
        input_tokens: value.get("input_tokens")?.as_u64()?,
        output_tokens: 0,
        cache_read_input_tokens: value
            .get("input_tokens_details")
            .and_then(|details| details.get("cached_tokens"))
            .and_then(Value::as_u64)
            .or_else(|| value.get("cache_read_input_tokens").and_then(Value::as_u64)),
        cache_creation_input_tokens: value
            .get("cache_creation_input_tokens")
            .and_then(Value::as_u64),
    })
}

/// Classify only documented structured codes for this trusted route family.
pub fn classify(route: RouteKind, value: &Value) -> Option<&'static str> {
    let error = if value.get("type").and_then(Value::as_str) == Some("response.failed") {
        value.pointer("/response/error")?
    } else {
        value.get("error")?
    };
    let code = error.get("code").and_then(Value::as_str);
    let kind = error.get("type").and_then(Value::as_str);
    match route {
        RouteKind::OpenAi => match code {
            Some("invalid_api_key") => Some("auth"),
            Some(
                "insufficient_quota"
                | "credit_balance_exhausted"
                | "organization_spend_limit_exceeded"
                | "project_spend_limit_exceeded"
                | "organization_usage_limit_exceeded",
            ) => Some("quota_exhausted"),
            Some("rate_limit_exceeded" | "slow_down") => Some("rate_limited"),
            Some("model_not_found") => Some("model_denied"),
            _ if kind == Some("insufficient_quota") => Some("quota_exhausted"),
            _ => None,
        },
        RouteKind::ChatGpt => match code {
            Some("token_expired") => Some("auth"),
            _ => None,
        },
        RouteKind::Anthropic => match kind {
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
    }
}

#[cfg(test)]
mod tests {
    use super::{Observation, RouteKind};

    /// Many small frames may exceed the single-frame cap in aggregate.
    #[test]
    fn long_responses_stream_keeps_terminal_usage() {
        let mut observation = Observation::new(RouteKind::OpenAi, Some("text/event-stream"));
        let frame = b"data: {\"type\":\"response.output_text.delta\",\"delta\":\"part\"}\n\n";
        for _ in 0..150_000 {
            assert_eq!(observation.feed(frame).len(), 1);
        }
        observation.feed(b"data: {\"type\":\"response.completed\",\"response\":{\"usage\":{\"input_tokens\":8,\"output_tokens\":3,\"input_tokens_details\":{\"cached_tokens\":2}}}}\n\n");
        let (observation, _) = observation.finish();
        assert!(!observation.ambiguous);
        assert_eq!(
            observation
                .usage
                .expect("terminal usage")
                .cache_read_input_tokens,
            Some(2)
        );
    }

    /// Structured failures are sanitized, and malformed or oversized frames cannot lend later usage.
    #[test]
    fn failed_and_malformed_frames_do_not_report_success() {
        let mut failed = Observation::new(RouteKind::OpenAi, Some("text/event-stream"));
        let bytes = failed.feed(b"data: {\"type\":\"response.failed\",\"response\":{\"error\":{\"code\":\"insufficient_quota\",\"message\":\"secret text\"}}}\n\n");
        assert_eq!(failed.category, Some("quota_exhausted"));
        assert!(
            !std::str::from_utf8(&bytes[0])
                .expect("safe frame")
                .contains("secret text")
        );
        let mut malformed = Observation::new(RouteKind::Anthropic, Some("text/event-stream"));
        malformed.feed(b"data: {bad}\n\n");
        malformed.feed(b"data: {\"type\":\"message_stop\"}\n\n");
        let (malformed, _) = malformed.finish();
        assert!(malformed.ambiguous);
        assert!(malformed.usage.is_none());
        let mut large = Observation::new(RouteKind::Anthropic, Some("text/event-stream"));
        large.feed(&vec![b'x'; 8 * 1024 * 1024 + 1]);
        assert!(large.ambiguous);
    }

    /// Anthropic start cache counters survive the delta and stop frames.
    #[test]
    fn messages_start_cache_usage_is_preserved() {
        let mut observation = Observation::new(RouteKind::Anthropic, Some("text/event-stream"));
        observation.feed(b"data: {\"type\":\"message_start\",\"message\":{\"usage\":{\"input_tokens\":7,\"cache_read_input_tokens\":3}}}\n\n");
        observation.feed(b"data: {\"type\":\"message_delta\",\"usage\":{\"output_tokens\":4}}\n\n");
        observation.feed(b"data: {\"type\":\"message_stop\"}\n\n");
        let (observation, _) = observation.finish();
        let usage = observation.usage.expect("usage");
        assert_eq!(
            (
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_input_tokens
            ),
            (7, 4, Some(3))
        );
    }

    /// A successful HTTP status cannot override a structured failed JSON body.
    #[test]
    fn nonstream_failed_response_is_sanitized() {
        let mut observation = Observation::new(RouteKind::OpenAi, Some("application/json"));
        assert!(observation.feed(br#"{"type":"response.failed","response":{"error":{"code":"invalid_api_key","message":"private provider text"}}}"#).is_empty());
        let (observation, body) = observation.finish();
        assert_eq!(observation.category, Some("auth"));
        assert!(observation.usage.is_none());
        assert!(
            !std::str::from_utf8(&body.expect("sanitized body"))
                .expect("UTF-8")
                .contains("private provider text")
        );
    }
}
