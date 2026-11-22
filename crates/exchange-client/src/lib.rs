//! Typed HTTP clients for exchange operator and admission APIs.

use std::{fmt, time::Duration};

use concurro_contracts::{
    Admission, AdmissionRequest, FinishRequest, ProbeResponse, RetiredSessions,
    SessionStatusRequest, SessionStatusResponse, TurnObservation, TurnSubmissionV2,
};
use reqwest::{Client, Method, RequestBuilder, header::CONTENT_TYPE};
use serde::{Serialize, de::DeserializeOwned};
use uuid::Uuid;

/// A bounded exchange refusal or transport failure without credentials or request bodies.
#[derive(Debug, thiserror::Error)]
pub enum ClientError {
    /// The endpoint could not be reached or its body was interrupted.
    #[error("exchange unavailable")]
    Unavailable,
    /// The endpoint returned an invalid success body.
    #[error("invalid exchange response")]
    InvalidResponse,
    /// The endpoint refused a request with a stable public code.
    #[error("exchange {status}: {code}")]
    Refused {
        /// Refusal HTTP status.
        status: u16,
        /// Bounded public refusal code.
        code: String,
    },
}

/// One bearer-authenticated private exchange listener.
#[derive(Clone)]
struct Endpoint {
    /// Reusable HTTP transport.
    client: Client,
    /// Listener URL without a trailing separator.
    base: String,
    /// Private caller credential.
    bearer: String,
    /// Per-call response deadline.
    timeout: Duration,
}

impl fmt::Debug for Endpoint {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.debug_struct("Endpoint").finish_non_exhaustive()
    }
}

impl Endpoint {
    /// Bind a private listener and its bearer without exposing either in debug output.
    fn new(
        client: Client,
        base: impl Into<String>,
        bearer: impl Into<String>,
        timeout: Duration,
    ) -> Self {
        Self {
            client,
            base: base.into().trim_end_matches('/').to_owned(),
            bearer: bearer.into(),
            timeout,
        }
    }

    /// Send a typed request and retain only safe refusal metadata.
    async fn send<B: Serialize + ?Sized>(
        &self,
        method: Method,
        path: &str,
        body: Option<&B>,
    ) -> Result<Vec<u8>, ClientError> {
        let mut request = self
            .client
            .request(method, format!("{}{path}", self.base))
            .bearer_auth(&self.bearer)
            .timeout(self.timeout);
        if let Some(body) = body {
            request = request.json(body);
        }
        self.execute(request).await
    }

    /// Send the exact persisted JSON bytes of an operator submission.
    async fn send_bytes(
        &self,
        method: Method,
        path: &str,
        body: &[u8],
    ) -> Result<Vec<u8>, ClientError> {
        let request = self
            .client
            .request(method, format!("{}{path}", self.base))
            .bearer_auth(&self.bearer)
            .timeout(self.timeout)
            .header(CONTENT_TYPE, "application/json")
            .body(body.to_vec());
        self.execute(request).await
    }

    /// Apply one safe error mapping to typed and byte-preserving requests.
    async fn execute(&self, request: RequestBuilder) -> Result<Vec<u8>, ClientError> {
        let response = request.send().await.map_err(|_| ClientError::Unavailable)?;
        let status = response.status();
        let bytes = response
            .bytes()
            .await
            .map_err(|_| ClientError::Unavailable)?;
        if !status.is_success() {
            let code = serde_json::from_slice::<serde_json::Value>(&bytes)
                .ok()
                .and_then(|value| value.get("error")?.as_str().map(str::to_owned))
                .filter(|value| {
                    value.len() <= 80
                        && value
                            .chars()
                            .all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
                })
                .unwrap_or_default();
            return Err(ClientError::Refused {
                status: status.as_u16(),
                code,
            });
        }
        Ok(bytes.to_vec())
    }

    /// Decode a success body into its shared contract type.
    async fn json<T: DeserializeOwned, B: Serialize + ?Sized>(
        &self,
        method: Method,
        path: &str,
        body: Option<&B>,
    ) -> Result<T, ClientError> {
        serde_json::from_slice(&self.send(method, path, body).await?)
            .map_err(|_| ClientError::InvalidResponse)
    }
}

/// Task application's authenticated exchange operator client.
#[derive(Clone, Debug)]
pub struct OperatorClient(Endpoint);

impl OperatorClient {
    /// Bind to the private operator listener.
    pub fn new(client: Client, base: impl Into<String>, bearer: impl Into<String>) -> Self {
        Self(Endpoint::new(client, base, bearer, Duration::from_secs(80)))
    }

    /// Submit a turn and recover its current observation on identical replay.
    pub async fn submit(
        &self,
        turn: Uuid,
        body: &TurnSubmissionV2,
    ) -> Result<TurnObservation, ClientError> {
        let bytes = serde_json::to_vec(body).map_err(|_| ClientError::InvalidResponse)?;
        self.submit_bytes(turn, &bytes).await
    }

    /// Send one previously persisted submit body without changing its bytes.
    pub async fn submit_bytes(
        &self,
        turn: Uuid,
        body: &[u8],
    ) -> Result<TurnObservation, ClientError> {
        let bytes = self
            .0
            .send_bytes(Method::PUT, &format!("/v1/turns/{turn}"), body)
            .await?;
        let observation: TurnObservation =
            serde_json::from_slice(&bytes).map_err(|_| ClientError::InvalidResponse)?;
        if observation.turn_id != turn {
            return Err(ClientError::InvalidResponse);
        }
        Ok(observation)
    }

    /// Read a turn for reconciliation.
    pub async fn observe(&self, turn: Uuid) -> Result<TurnObservation, ClientError> {
        let observation: TurnObservation = self
            .0
            .json::<_, ()>(Method::GET, &format!("/v1/turns/{turn}"), None)
            .await?;
        if observation.turn_id != turn {
            return Err(ClientError::InvalidResponse);
        }
        Ok(observation)
    }

    /// Retire a turn and obtain its stable complete session set.
    pub async fn cancel(&self, turn: Uuid) -> Result<RetiredSessions, ClientError> {
        self.0
            .json::<_, ()>(Method::DELETE, &format!("/v1/turns/{turn}"), None)
            .await
    }

    /// Fence a grant, including one with no turns yet.
    pub async fn revoke(&self, grant: &str) -> Result<RetiredSessions, ClientError> {
        let mut url = reqwest::Url::parse(&format!("{}/v1/grants", self.0.base))
            .map_err(|_| ClientError::Unavailable)?;
        url.path_segments_mut()
            .map_err(|_| ClientError::Unavailable)?
            .push(grant)
            .push("revoke");
        let request = self
            .0
            .client
            .request(Method::POST, url)
            .bearer_auth(&self.0.bearer)
            .timeout(self.0.timeout);
        serde_json::from_slice(&self.0.execute(request).await?)
            .map_err(|_| ClientError::InvalidResponse)
    }
}

/// Gateway's authenticated exchange admission client.
#[derive(Clone, Debug)]
pub struct AdmissionClient(Endpoint);

impl AdmissionClient {
    /// Bind to the private admission listener.
    pub fn new(client: Client, base: impl Into<String>, bearer: impl Into<String>) -> Self {
        Self(Endpoint::new(client, base, bearer, Duration::from_secs(5)))
    }

    /// Admit one inference request using its derived session key.
    pub async fn request(&self, body: &AdmissionRequest) -> Result<Admission, ClientError> {
        self.0
            .json(Method::POST, "/internal/v1/requests", Some(body))
            .await
    }

    /// Probe allowed models without recording a provider request.
    pub async fn probe(&self, key: &str) -> Result<ProbeResponse, ClientError> {
        let request = AdmissionRequest {
            key: key.to_owned(),
            model: String::new(),
            request_id: None,
            probe: Some(true),
        };
        self.0
            .json(Method::POST, "/internal/v1/requests", Some(&request))
            .await
    }

    /// Finish an admitted provider request idempotently.
    pub async fn finish(&self, request: Uuid, body: &FinishRequest) -> Result<(), ClientError> {
        self.0
            .send(
                Method::POST,
                &format!("/internal/v1/requests/{request}/finish"),
                Some(body),
            )
            .await?;
        Ok(())
    }

    /// Return the live subset of requested sessions.
    pub async fn sessions_status(
        &self,
        body: &SessionStatusRequest,
    ) -> Result<SessionStatusResponse, ClientError> {
        self.0
            .json(Method::POST, "/internal/v1/sessions/status", Some(body))
            .await
    }
}
