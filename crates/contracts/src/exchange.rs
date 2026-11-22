//! Exchange v2 HTTP messages and durable bus events.

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

use crate::{Contract, ContractError, EventKind, RequestActor, TaskId, Timestamp};

/// Immutable spending limits attached to one grant.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct GrantLimits {
    /// Maximum provider tokens in a session.
    pub tokens: i64,
    /// Maximum session lease in seconds.
    pub lease_seconds: i64,
}

/// Versioned funding route; personal routes identify their exact credential version.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct GrantCredential {
    /// Provider route name.
    pub route: String,
    /// Browser credential entry for contributed funding.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub entry_id: Option<String>,
    /// Secret record for contributed funding.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub record_id: Option<String>,
    /// Immutable secret version for contributed funding.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub version: Option<i64>,
    /// Custody kind, `secret_store` or `relay`, for contributed funding.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub custody: Option<String>,
    /// Relay identity when custody is relay.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub relay_id: Option<String>,
}

/// Immutable authorization to spend one payer's selected route.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct Grant {
    /// Stable grant identity.
    pub grant_id: String,
    /// `task_org` or `task_contributed`.
    pub purpose: String,
    /// Contributing user identity, absent for org funding.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub payer: Option<RequestActor>,
    /// Exact route or credential version.
    pub credential: GrantCredential,
    /// Models permitted under this grant.
    pub models: Vec<String>,
    /// Upper bounds for each session.
    pub limits: GrantLimits,
}

/// GitHub OIDC identity expected from the dispatched runner.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ExpectedClaims {
    /// Numeric repository identity.
    pub repository_id: String,
    /// Numeric repository owner identity.
    pub repository_owner_id: String,
    /// Launcher workflow reference.
    pub job_workflow_ref: String,
    /// GitHub App actor that dispatches the job.
    pub actor_id: String,
    /// Optional workflow content identity.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub job_workflow_sha: Option<String>,
}

/// Engine repository and immutable commit named by a runner claim.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct RunnerEngine {
    /// Engine source repository.
    pub repository: String,
    /// Engine commit to execute.
    pub sha: String,
}

/// Runner checkout and task binding, excluding funding fields.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct TurnBinding {
    /// Issue repository name.
    pub repository: String,
    /// Issue number.
    pub issue: i64,
    /// Agent role and provider.
    pub role: String,
    /// Engine source and commit.
    pub engine: RunnerEngine,
    /// Runner fork repository.
    pub fork: String,
    /// Upstream repository.
    pub upstream: String,
    /// Frozen configuration commit.
    pub config_sha: String,
    /// Frozen upstream base commit.
    pub base: String,
    /// Additional existing binding data required by the engine.
    #[serde(flatten)]
    pub extra: serde_json::Map<String, Value>,
}

/// A content-addressed runner artifact.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct BlobRef {
    /// Lowercase SHA-256 digest.
    pub sha256: String,
    /// Original byte count.
    pub size: u64,
}

/// Turn input delivered to the v2 runner.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct TurnPayloadV2 {
    /// Frozen model request.
    pub request: Value,
    /// Prior-turn feedback.
    pub feedback: Value,
    /// Frozen checkout and workspace specification.
    pub spec: Value,
    /// Engine timeout in seconds.
    pub timeout: f64,
    /// Retained archive to restore, when continuing.
    pub retained: Option<BlobRef>,
}

/// Runner result with blob references rather than embedded bytes.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct TurnResultV2 {
    /// Engine outcome.
    pub outcome: String,
    /// Engine response content.
    pub response: Value,
    /// Optional termination diagnostic.
    #[serde(default)]
    pub termination_reason: Option<String>,
    /// Elapsed execution time.
    #[serde(default)]
    pub elapsed_seconds: Option<f64>,
    /// Workspace change JSON blob.
    pub workspace: Option<BlobRef>,
    /// Transcript archive blob.
    pub transcript: Option<BlobRef>,
    /// Retained archive blob.
    pub retained: Option<BlobRef>,
}

/// Runner's first authenticated claim response.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct RunnerClaimReply {
    /// Engine source and pinned commit.
    pub engine: RunnerEngine,
    /// Derived bearer for this session and run.
    pub runner_token: String,
    /// Session deadline.
    pub deadline: Timestamp,
}

/// Runner's engine-key handshake request.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct RunnerKeyRequest {
    /// Checked-out engine commit.
    pub engine_sha: String,
}

/// Runner's derived inference key and frozen execution binding.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct RunnerKeyReply {
    /// Derived key accepted by the inference gateway.
    pub session_key: String,
    /// Gateway endpoint for provider traffic.
    pub inference_base_url: String,
    /// Session models.
    pub models: Vec<String>,
    /// Session deadline.
    pub deadline: Timestamp,
    /// Frozen task and checkout binding.
    pub binding: TurnBinding,
}

/// Next runner turn after the session has bound its engine.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct RunnerNextTurn {
    /// Exchange turn identity.
    #[schemars(with = "TaskId")]
    pub turn_id: Uuid,
    /// Frozen engine input.
    pub payload: TurnPayloadV2,
    /// Session deadline.
    pub deadline: Timestamp,
}

/// Exact relay claim payload accepted by the S1 hosted verifier.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ExchangeSessionClaim {
    /// Unique claim identity.
    pub jti: String,
    /// Always `concurro-exchange`.
    pub iss: String,
    /// Exchange session ID.
    pub sub: String,
    /// Issue time as integral Unix seconds.
    pub iat: i64,
    /// Expiry as integral Unix seconds.
    pub exp: i64,
    /// Approved relay ID.
    pub relay_id: String,
    /// Approved numeric owner ID.
    pub user_id: u64,
    /// Approved entry ID.
    pub entry_id: String,
    /// Approved immutable credential record.
    pub record_id: String,
    /// Approved version.
    pub version: u64,
    /// Approved route.
    pub route: String,
    /// Allowed provider models.
    pub models: Vec<String>,
    /// Maximum admitted calls.
    pub calls: u64,
    /// Token allowance.
    pub tokens: u64,
    /// Repository shown to the Sponsor.
    pub repository: String,
    /// Issue number shown to the Sponsor.
    pub issue: u64,
}

/// Complete body of a v2 operator submission.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct TurnSubmissionV2 {
    /// Attempt identity, equal to the URL turn ID.
    #[schemars(with = "TaskId")]
    pub attempt_id: Uuid,
    /// Owning task identity.
    #[schemars(with = "TaskId")]
    pub task_id: Uuid,
    /// Immutable funding grant.
    pub grant: Grant,
    /// Runner checkout binding.
    pub binding: TurnBinding,
    /// Digest of staffing inputs.
    pub staffing_group: String,
    /// Expected job identity.
    pub expected_claims: ExpectedClaims,
    /// Models allowed in this turn.
    pub models: Vec<String>,
    /// Token budget, within the grant.
    pub budget: i64,
    /// Session lease in seconds, within the grant.
    pub lease_seconds: i64,
    /// Runner input.
    pub payload: TurnPayloadV2,
}

/// New runner session that the operator must dispatch.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct Dispatch {
    /// Session to launch.
    #[schemars(with = "TaskId")]
    pub session_id: Uuid,
}

/// One durable turn observation returned by the operator API.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct TurnObservation {
    /// Exchange turn identity.
    #[schemars(with = "TaskId")]
    pub turn_id: Uuid,
    /// Monotonic exchange lifecycle revision for readback fencing.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub revision: Option<u64>,
    /// Queued, running, finished, lost or cancelled.
    pub status: String,
    /// Current or last session.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    #[schemars(with = "Option<TaskId>")]
    pub session_id: Option<Uuid>,
    /// Bound GitHub Actions run.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub run_id: Option<String>,
    /// Session requiring an external dispatch.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub dispatch: Option<Dispatch>,
    /// Latest runner refusal.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub refusal: Option<String>,
    /// Admission time.
    pub created: Timestamp,
    /// Runner start time.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub started: Option<Timestamp>,
    /// Terminal time.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub finished: Option<Timestamp>,
    /// Terminal runner result.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub result: Option<TurnResultV2>,
    /// Trusted provider failure.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_failure: Option<ProviderFailure>,
    /// Recorded request evidence.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_requests: Option<ProviderRequests>,
    /// Settled request totals, available with terminal request evidence.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub usage: Option<TurnUsage>,
}

/// All sessions allocated or used by a turn or grant.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct RetiredSessions {
    /// Complete, stable set of session IDs.
    #[schemars(with = "Vec<TaskId>")]
    pub sessions: Vec<Uuid>,
}

/// Provider request evidence, independent of the turn result.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum ProviderRequests {
    /// At least one request reached the provider or verifier.
    Some,
    /// All recorded requests finished before dispatch.
    None,
    /// No request was observed dispatched, and at least one report was lost.
    Unknown,
}

/// Trusted provider failure used for credential fallback.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct ProviderFailure {
    /// Failure category.
    pub category: String,
    /// Model involved.
    pub model: String,
    /// Optional retry delay in seconds.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retry_after: Option<f64>,
    /// Request that reported the failure.
    #[schemars(with = "TaskId")]
    pub request_id: Uuid,
    /// Funding grant.
    pub grant_id: String,
    /// Personal credential record, when applicable.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub record_id: Option<String>,
    /// Personal credential version, when applicable.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub version: Option<i64>,
}

/// Usage totals in one completed turn.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct TurnUsage {
    /// Recorded request count.
    pub requests: u64,
    /// Input tokens with known usage.
    pub input_tokens: u64,
    /// Output tokens with known usage.
    pub output_tokens: u64,
    /// Requests with unavailable usage.
    pub unavailable: u64,
}

/// Operator lifecycle snapshot, appended with its state transaction.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct ExchangeTurnStatus {
    /// Turn whose state changed.
    #[schemars(with = "TaskId")]
    pub turn_id: Uuid,
    /// Queued, running, finished, lost or cancelled.
    pub status: String,
    /// Current or last session.
    #[schemars(with = "Option<TaskId>")]
    pub session_id: Option<Uuid>,
    /// Bound GitHub Actions run.
    pub run_id: Option<String>,
    /// New session to dispatch.
    pub dispatch: Option<Dispatch>,
    /// Latest refusal.
    pub refusal: Option<String>,
    /// Trusted provider failure.
    pub provider_failure: Option<ProviderFailure>,
    /// Admission time.
    pub created: Timestamp,
    /// Runner start time.
    pub started: Option<Timestamp>,
    /// Terminal time.
    pub finished: Option<Timestamp>,
}

impl Contract for ExchangeTurnStatus {
    const FAMILY: &'static str = "exchange.turn.status";
    const VERSION: u32 = 1;
    const KIND: EventKind = EventKind::Snapshot;

    fn event_type(&self) -> String {
        format!("exchange.turn.{}.status", self.turn_id)
    }
    fn subject(&self) -> String {
        self.event_type()
    }
    fn validate_metadata(
        &self,
        entity: Option<&str>,
        revision: Option<u64>,
    ) -> Result<(), ContractError> {
        if entity != Some(format!("turn:{}", self.turn_id).as_str())
            || !revision.is_some_and(|value| value > 0)
        {
            return Err(ContractError::InvalidMetadata("exchange turn status"));
        }
        Ok(())
    }
}

/// Terminal turn fact emitted once runner results are settled.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct ExchangeTurnFinished {
    /// Exchange turn identity.
    #[schemars(with = "TaskId")]
    pub turn_id: Uuid,
    /// Terminal operator snapshot retained with this fact beyond live-row pruning.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub observation: Option<TurnObservation>,
    /// Task attempt identity.
    #[schemars(with = "TaskId")]
    pub attempt_id: Uuid,
    /// Terminal status.
    pub status: String,
    /// Runner result, if any.
    pub result: Option<TurnResultV2>,
    /// Trusted provider failure.
    pub provider_failure: Option<ProviderFailure>,
    /// Provider request evidence.
    pub provider_requests: ProviderRequests,
    /// Request usage totals.
    pub usage: TurnUsage,
}

impl Contract for ExchangeTurnFinished {
    const FAMILY: &'static str = "exchange.turn.finished";
    const VERSION: u32 = 1;
    const KIND: EventKind = EventKind::Fact;
    fn event_type(&self) -> String {
        format!("exchange.turn.{}.finished", self.turn_id)
    }
    fn subject(&self) -> String {
        self.event_type()
    }
}

/// Token accounting reported after one inference request.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct UsageTokens {
    /// Provider input tokens.
    pub input_tokens: u64,
    /// Provider output tokens.
    pub output_tokens: u64,
    /// Cache-read input tokens.
    #[serde(default)]
    pub cache_read_input_tokens: Option<u64>,
    /// Cache-created input tokens.
    #[serde(default)]
    pub cache_creation_input_tokens: Option<u64>,
}

/// One inference request's immutable accounting fact.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct InferenceUsage {
    /// Gateway request identity.
    #[schemars(with = "TaskId")]
    pub request_id: Uuid,
    /// Runner session.
    #[schemars(with = "TaskId")]
    pub session_id: Uuid,
    /// Running turn.
    #[schemars(with = "TaskId")]
    pub turn_id: Uuid,
    /// Funding grant.
    pub grant_id: String,
    /// Personal credential record, if used.
    pub record_id: Option<String>,
    /// Personal credential version, if used.
    pub version: Option<i64>,
    /// Provider model.
    pub model: String,
    /// Result category.
    pub category: String,
    /// Known token usage.
    pub usage: Option<UsageTokens>,
    /// Reason usage could not be counted.
    pub usage_unavailable: Option<String>,
    /// Admission time.
    pub started: Timestamp,
    /// Settlement time.
    pub finished: Timestamp,
}

impl Contract for InferenceUsage {
    const FAMILY: &'static str = "inference.usage";
    const VERSION: u32 = 1;
    const KIND: EventKind = EventKind::Fact;
    fn event_type(&self) -> String {
        Self::FAMILY.to_owned()
    }
    fn subject(&self) -> String {
        self.event_type()
    }
}

/// Gateway's request for session admission.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct AdmissionRequest {
    /// Derived session key.
    pub key: String,
    /// Requested model.
    pub model: String,
    /// Caller-generated stable request identity.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    #[schemars(with = "Option<TaskId>")]
    pub request_id: Option<Uuid>,
    /// Model-list probe that records no request.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub probe: Option<bool>,
}

/// An admitted request and its exact funding route.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct Admission {
    /// Accepted request identity.
    #[schemars(with = "TaskId")]
    pub request_id: Uuid,
    /// Live runner session.
    #[schemars(with = "TaskId")]
    pub session_id: Uuid,
    /// Running turn.
    #[schemars(with = "TaskId")]
    pub turn_id: Uuid,
    /// Funding grant.
    pub grant_id: String,
    /// Exact route or credential version.
    pub credential: GrantCredential,
    /// Models allowed by the session.
    pub models: Vec<String>,
    /// Remaining known token allowance.
    pub remaining_tokens: i64,
    /// Session deadline.
    pub deadline: Timestamp,
    /// Signed relay claim when custody is relay.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub claim: Option<String>,
}

/// Allowed models returned by an admission probe without recording a request.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct ProbeResponse {
    /// Session models visible to the gateway.
    pub models: Vec<String>,
}

/// Gateway's final report for one admitted request.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct FinishRequest {
    /// Whether a request reached the provider or verifier.
    pub dispatched: bool,
    /// Outcome category.
    pub category: String,
    /// Parsed provider usage, if known.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub usage: Option<UsageTokens>,
    /// Reason known usage is absent.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub usage_unavailable: Option<String>,
    /// Retry delay in seconds.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retry_after: Option<f64>,
}

/// Sessions the gateway needs to check for stream cancellation.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct SessionStatusRequest {
    /// Session identities to inspect.
    #[schemars(with = "Vec<TaskId>")]
    pub session_ids: Vec<Uuid>,
}

/// Live subset of a session-status request.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct SessionStatusResponse {
    /// Sessions still admitting requests.
    #[schemars(with = "Vec<TaskId>")]
    pub live: Vec<Uuid>,
}
