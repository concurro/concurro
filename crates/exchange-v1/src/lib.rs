//! Operator client for the Python exchange during the Rust task migration.

use std::{fmt, time::Duration};

use reqwest::{Client, Method, StatusCode, header::CONTENT_TYPE};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::Value;
use uuid::Uuid;

/// A submitted turn's durable body, serialized once before its first send.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TurnSubmission {
    /// Attempt ID, also used as the exchange turn ID.
    pub turn: Uuid,
    /// OIDC claims expected from the runner job.
    pub expected: Value,
    /// Models the issued session can call.
    pub models: Vec<String>,
    /// Per-session token budget.
    pub budget: i64,
    /// Session lease in seconds.
    pub lease: i64,
    /// The runner's v1 payload.
    pub payload: Value,
    /// Existing session holding a retained continuation, if any.
    pub pin: Option<Uuid>,
    /// Task, role and funding admission binding.
    pub binding: Value,
}

/// A runner session observed on a turn.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Runner {
    /// Exchange session ID.
    pub session: Uuid,
    /// Bound Actions run and attempt, when claimed.
    pub run_id: Option<String>,
}

/// The exchange's durable turn observation and optional dispatch.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct TurnObservation {
    /// Attempt and turn ID.
    pub turn: Uuid,
    /// Exchange status (`queued`, `running`, `done`, `lost` or `cancelled`).
    pub status: String,
    /// Exchange creation time, in Unix seconds.
    pub created: f64,
    /// Runner start time, if one was observed.
    pub started: Option<f64>,
    /// Terminal time, if known.
    pub finished: Option<f64>,
    /// Session to dispatch; it can appear in a submit reply or a poll.
    pub dispatch: Option<Uuid>,
    /// Session currently serving or last serving the turn.
    pub runner: Option<Runner>,
    /// Completed runner result, kept when the turn is retired.
    pub result: Option<Value>,
    /// Provider refusal or budget failure, when reported.
    pub provider_failure: Option<Value>,
    /// Runner refusal reported during queueing.
    pub refusal: Option<String>,
    /// Exact response bytes, retained outside the operational JSON projection.
    #[serde(skip)]
    pub raw_body: Option<Vec<u8>>,
}

/// Acknowledgement of every session associated with a retired turn.
#[derive(Clone, Debug, Serialize, Deserialize, Eq, PartialEq)]
pub struct Retirement {
    /// All allocated or serving session IDs, including on replay after pruning.
    pub sessions: Vec<Uuid>,
}

/// Acknowledgement of one durable provider-slot transition.
#[derive(Clone, Debug, Deserialize, Serialize, Eq, PartialEq)]
pub struct SponsorshipTransitionAck {
    /// Exact idempotency identity supplied by the caller.
    pub transition: String,
    /// Epoch accepted for this provider slot.
    pub epoch: i64,
}

/// Atomic acknowledgement of one or two sponsorship transitions.
#[derive(Clone, Debug, Deserialize, Serialize, Eq, PartialEq)]
pub struct SponsorshipAck {
    /// Receipts in the same order as the submitted transitions.
    pub transitions: Vec<SponsorshipTransitionAck>,
}

/// A failed exchange operation without operator credentials or submitted content.
#[derive(Debug, thiserror::Error)]
pub enum ExchangeError {
    /// The exchange or its response body could not be reached.
    #[error("exchange unavailable")]
    Unavailable,
    /// The exchange returned a response outside the expected JSON contract.
    #[error("invalid exchange response")]
    InvalidResponse,
    /// The exchange refused this operation.
    #[error("exchange {status}: {reason}")]
    Refused {
        /// HTTP status from the exchange.
        status: u16,
        /// Bounded, nonsecret refusal text supplied by the exchange.
        reason: String,
    },
    /// A transition was rejected only because its task generation is stale.
    #[error("exchange 409: stale sponsorship generation")]
    StaleSponsorshipGeneration {
        /// The one transition whose generation must be advanced and rehashed.
        transition: String,
    },
}

/// Authenticated operator calls to the Python exchange.
#[derive(Clone)]
pub struct Operator {
    client: Client,
    base: String,
    token: String,
}

impl fmt::Debug for Operator {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.debug_struct("Operator").finish_non_exhaustive()
    }
}

impl Operator {
    /// Bind a reusable HTTP client to the exchange edge route and operator token.
    pub fn new(client: Client, base: impl Into<String>, token: impl Into<String>) -> Self {
        Self {
            client,
            base: base.into().trim_end_matches('/').to_owned(),
            token: token.into(),
        }
    }

    /// Send the exact body previously persisted with an attempt.
    pub async fn submit(&self, body: &[u8]) -> Result<TurnObservation, ExchangeError> {
        let (mut observation, raw): (TurnObservation, Vec<u8>) =
            self.call(Method::POST, "turns", Some(body)).await?;
        observation.raw_body = Some(raw);
        Ok(observation)
    }

    /// Observe a turn, allowing the exchange at most the requested long-poll duration.
    pub async fn poll(&self, turn: Uuid, wait: Duration) -> Result<TurnObservation, ExchangeError> {
        let (mut observation, raw): (TurnObservation, Vec<u8>) = self
            .call(
                Method::GET,
                &format!("turns/{turn}?wait={}", wait.as_secs()),
                None,
            )
            .await?;
        observation.raw_body = Some(raw);
        (observation.turn == turn)
            .then_some(observation)
            .ok_or(ExchangeError::InvalidResponse)
    }

    /// Retire a turn and receive its complete, replayable session acknowledgement.
    pub async fn retire(&self, turn: Uuid) -> Result<Retirement, ExchangeError> {
        self.call(Method::DELETE, &format!("turns/{turn}"), None)
            .await
            .map(|(retirement, _)| retirement)
    }

    /// Submit the exact persisted provider transitions and verify every receipt.
    pub async fn sponsorship(&self, body: &[u8]) -> Result<SponsorshipAck, ExchangeError> {
        let value: Value =
            serde_json::from_slice(body).map_err(|_| ExchangeError::InvalidResponse)?;
        let requests = value.get("transitions").and_then(Value::as_array);
        let submitted = requests.map_or_else(|| vec![&value], |requests| requests.iter().collect());
        if submitted.is_empty() || submitted.len() > 2 {
            return Err(ExchangeError::InvalidResponse);
        }
        let identities = submitted
            .iter()
            .map(|transition| {
                Some((
                    transition.get("transition")?.as_str()?,
                    transition.get("slot")?.get("epoch")?.as_i64()?,
                ))
            })
            .collect::<Option<Vec<_>>>()
            .ok_or(ExchangeError::InvalidResponse)?;
        let (receipt, _): (SponsorshipAck, _) =
            self.call(Method::POST, "sponsorship", Some(body)).await?;
        if receipt.transitions.len() != identities.len()
            || !receipt
                .transitions
                .iter()
                .zip(identities)
                .all(|(ack, (transition, epoch))| {
                    ack.transition == transition && ack.epoch == epoch
                })
        {
            return Err(ExchangeError::InvalidResponse);
        }
        Ok(receipt)
    }

    /// Call one operator route without exposing the token or request body in errors.
    async fn call<T: DeserializeOwned>(
        &self,
        method: Method,
        route: &str,
        body: Option<&[u8]>,
    ) -> Result<(T, Vec<u8>), ExchangeError> {
        let url = format!("{}/exchange/operator/{route}", self.base);
        let mut request = self
            .client
            .request(method, url)
            .bearer_auth(&self.token)
            .timeout(Duration::from_secs(80));
        if let Some(body) = body {
            request = request
                .header(CONTENT_TYPE, "application/json")
                .body(body.to_vec());
        }
        let response = request
            .send()
            .await
            .map_err(|_| ExchangeError::Unavailable)?;
        let status = response.status();
        let bytes = response
            .bytes()
            .await
            .map_err(|_| ExchangeError::Unavailable)?;
        if !status.is_success() {
            let detail = serde_json::from_slice::<Value>(&bytes).ok();
            if route == "sponsorship"
                && status == StatusCode::CONFLICT
                && detail
                    .as_ref()
                    .is_some_and(|value| value["code"] == "stale_sponsorship_generation")
                && let Some(transition) = detail
                    .as_ref()
                    .and_then(|value| value["transition"].as_str())
                && transition.len() == 64
                && transition
                    .bytes()
                    .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
            {
                return Err(ExchangeError::StaleSponsorshipGeneration {
                    transition: transition.to_owned(),
                });
            }
            let reason = detail
                .and_then(|value| value.get("error")?.as_str().map(str::to_owned))
                .unwrap_or_default();
            return Err(ExchangeError::Refused {
                status: status.as_u16(),
                reason: reason.chars().take(200).collect(),
            });
        }
        let parsed = serde_json::from_slice(&bytes).map_err(|_| ExchangeError::InvalidResponse)?;
        Ok((parsed, bytes.to_vec()))
    }
}

#[cfg(test)]
mod tests {
    use std::{
        sync::{Arc, Mutex},
        time::Duration,
    };

    use axum::{
        Json, Router,
        body::Bytes,
        http::{HeaderMap, StatusCode},
        routing::{get, post},
    };
    use serde_json::json;
    use uuid::Uuid;

    use super::{ExchangeError, Operator, Retirement, SponsorshipAck, SponsorshipTransitionAck};

    #[tokio::test]
    async fn operator_preserves_submit_bytes_and_reads_retirement_ack() {
        let turn = Uuid::now_v7();
        let session = Uuid::now_v7();
        let submitted = Arc::new(Mutex::new(Vec::new()));
        let observed = format!(
            " {{ \"turn\": \"{turn}\", \"status\": \"done\", \"created\": 1.0, \
             \"finished\": 2.0, \"result\": {{\"outcome\":\"ok\",\"response\":{{\"rationale\":\"model\\u0000text\"}}}} }}\n"
        );
        let app = Router::new()
            .route("/exchange/operator/turns", post({
                let submitted = Arc::clone(&submitted);
                move |headers: HeaderMap, body: Bytes| {
                    let submitted = Arc::clone(&submitted);
                    async move {
                        assert_eq!(headers["authorization"], "Bearer operator-secret");
                        *submitted.lock().expect("capture") = body.to_vec();
                        Json(json!({"turn": turn, "status": "queued", "created": 1.0, "dispatch": session}))
                    }
                }
            }))
            .route("/exchange/operator/turns/{turn}", get({
                let observed = observed.clone();
                move || {
                    let observed = observed.clone();
                    async move { observed }
                }
            }).delete(move || async move {
                Json(json!({"sessions": [session]}))
            }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let address = listener.local_addr().expect("address");
        let server = tokio::spawn(async move { axum::serve(listener, app).await });
        let operator = Operator::new(
            reqwest::Client::new(),
            format!("http://{address}/"),
            "operator-secret",
        );
        let body = format!("{{ \"turn\": \"{turn}\", \"payload\": {{}} }}");
        let queued = operator.submit(body.as_bytes()).await.expect("submit");
        assert_eq!(queued.dispatch, Some(session));
        assert_eq!(*submitted.lock().expect("capture"), body.as_bytes());
        let finished = operator
            .poll(turn, Duration::from_secs(45))
            .await
            .expect("poll");
        assert_eq!(
            finished.result,
            Some(json!({"outcome": "ok", "response":{"rationale":"model\0text"}}))
        );
        assert_eq!(finished.raw_body.as_deref(), Some(observed.as_bytes()));
        assert_eq!(
            operator.retire(turn).await.expect("retire"),
            Retirement {
                sessions: vec![session]
            }
        );
        server.abort();
    }

    #[tokio::test]
    async fn refusal_does_not_expose_token_or_body() {
        let app = Router::new().route(
            "/exchange/operator/turns",
            post(|| async { (StatusCode::GONE, Json(json!({"error": "turn_expired"}))) }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let address = listener.local_addr().expect("address");
        let server = tokio::spawn(async move { axum::serve(listener, app).await });
        let operator = Operator::new(
            reqwest::Client::new(),
            format!("http://{address}"),
            "secret-token",
        );
        let error = operator
            .submit(b"sensitive-body")
            .await
            .expect_err("refusal");
        std::assert_matches!(&error, ExchangeError::Refused { status: 410, .. });
        assert_eq!(error.to_string(), "exchange 410: turn_expired");
        server.abort();
    }

    /// Send an immutable two-provider body and verify the returned transition identities.
    #[tokio::test]
    async fn sponsorship_preserves_bytes_and_checks_atomic_receipts() {
        let first = "a".repeat(64);
        let second = "b".repeat(64);
        let submitted = Arc::new(Mutex::new(Vec::new()));
        let app = Router::new().route(
            "/exchange/operator/sponsorship",
            post({
                let submitted = Arc::clone(&submitted);
                let first = first.clone();
                let second = second.clone();
                move |headers: HeaderMap, body: Bytes| {
                    let submitted = Arc::clone(&submitted);
                    let first = first.clone();
                    let second = second.clone();
                    async move {
                        assert_eq!(headers["authorization"], "Bearer operator-secret");
                        *submitted.lock().expect("capture") = body.to_vec();
                        Json(json!({"transitions": [
                            {"transition": first, "epoch": 1},
                            {"transition": second, "epoch": 0}
                        ]}))
                    }
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let address = listener.local_addr().expect("address");
        let server = tokio::spawn(async move { axum::serve(listener, app).await });
        let operator = Operator::new(
            reqwest::Client::new(),
            format!("http://{address}"),
            "operator-secret",
        );
        let body = format!(
            "{{ \"transitions\": [{{\"transition\":\"{first}\",\"slot\":{{\"epoch\":1}}}}, \
             {{\"transition\":\"{second}\",\"slot\":{{\"epoch\":0}}}}] }}"
        );
        assert_eq!(
            operator
                .sponsorship(body.as_bytes())
                .await
                .expect("accepted"),
            SponsorshipAck {
                transitions: vec![
                    SponsorshipTransitionAck {
                        transition: first,
                        epoch: 1
                    },
                    SponsorshipTransitionAck {
                        transition: second,
                        epoch: 0
                    },
                ]
            }
        );
        assert_eq!(*submitted.lock().expect("capture"), body.as_bytes());
        server.abort();
    }

    /// Preserve the exact rejected transition without exposing the remote error body.
    #[tokio::test]
    async fn sponsorship_stale_generation_is_identified_without_secret_text() {
        let transition = "c".repeat(64);
        let app = Router::new().route(
            "/exchange/operator/sponsorship",
            post({
                let transition = transition.clone();
                move || {
                    let transition = transition.clone();
                    async move {
                        (
                            StatusCode::CONFLICT,
                            Json(json!({
                                "error": "secret-body", "code": "stale_sponsorship_generation",
                                "transition": transition
                            })),
                        )
                    }
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let address = listener.local_addr().expect("address");
        let server = tokio::spawn(async move { axum::serve(listener, app).await });
        let operator = Operator::new(
            reqwest::Client::new(),
            format!("http://{address}"),
            "operator-secret",
        );
        let body = format!("{{\"transition\":\"{transition}\",\"slot\":{{\"epoch\":1}}}}");
        let error = operator
            .sponsorship(body.as_bytes())
            .await
            .expect_err("stale generation");
        std::assert_matches!(
            &error,
            ExchangeError::StaleSponsorshipGeneration { transition: id } if id == &transition
        );
        assert!(!format!("{error:?} {error}").contains("secret-body"));
        server.abort();
    }
}
