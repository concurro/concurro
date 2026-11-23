//! Versioned wire contracts shared by Concurro services.

mod credentials;
mod exchange;
mod github;
mod github_events;
mod identifiers;
mod ingress;
mod schema;
mod tasks;
mod turns;

pub use credentials::{CredentialChange, CredentialChanged, RevokedVersion};
pub use exchange::{
    Admission, AdmissionRequest, BlobRef, Dispatch, ExchangeSessionClaim, ExchangeTurnFinished,
    ExchangeTurnStatus, ExpectedClaims, FinishRequest, Grant, GrantCredential, GrantLimits,
    InferenceUsage, ProbeResponse, ProviderFailure, ProviderRequests, RetiredSessions,
    RunnerClaimReply, RunnerEngine, RunnerKeyReply, RunnerKeyRequest, RunnerNextTurn,
    SessionStatusRequest, SessionStatusResponse, TurnBinding, TurnObservation, TurnPayloadV2,
    TurnResultV2, TurnSubmissionV2, TurnUsage, UsageTokens,
};
pub use github::{
    Actor, ActorKind, GithubIssues, Issue, IssueAction, IssueChanges, IssueState, KnownIssueAction,
    Label, NewIssue, Repository,
};
pub use github_events::{
    CheckRunPayload, CheckSuitePayload, CommentIssue, GithubComment, GithubEvent, GithubPayload,
    GithubPullRequest, GithubReview, GithubReviewComment, GithubReviewThread, IssueCommentPayload,
    NumberedPullRequest, PullBase, PullHead, PullRequestPayload, PullRequestReviewCommentPayload,
    PullRequestReviewPayload, PullRequestReviewThreadPayload, PushPayload, RequestedTeam,
    ReviewPullRequest, StatusPayload, WorkflowRunPayload,
};
pub use identifiers::{EventId, RequestId, TaskId, Timestamp};
pub use ingress::{CoverageAdvanced, CoverageGap};
pub use tasks::{
    CommandClaims, CommandContent, CommandSubmission, ConfigLauncher, ConfigTarget, ConfigTargets,
    ReadModelRecord, RequestActor, RequestCommand, RequestOutcome, RequestOutcomeStatus,
    RetainedRequestOutcome, TaskBudget, TaskBudgets, TaskIssue, TaskNotice, TaskReport,
    TaskSnapshot,
};
pub use turns::{
    TurnArtifact, TurnFinished, TurnRunner, TurnStatus, TurnSubmitted, TurnTranscript,
};

use schemars::{JsonSchema, generate::SchemaSettings};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use uuid::Uuid;

/// A NATS subject token with no separator, wildcard or whitespace.
pub fn valid_subject_token(token: &str) -> bool {
    !token.is_empty()
        && !token
            .chars()
            .any(|ch| ch == '.' || ch == '*' || ch == '>' || ch.is_control() || ch.is_whitespace())
}

/// Header fields read before decoding a contract's data.
#[derive(Clone, Debug, Eq, PartialEq, Deserialize)]
pub struct EnvelopeHeader {
    /// Concrete event type.
    #[serde(rename = "type")]
    pub event_type: String,
    /// Contract major version; may exceed a version this binary reads.
    pub version: u64,
}

/// A contract or envelope violation while decoding a bus message.
#[derive(Debug, thiserror::Error)]
pub enum ContractError {
    /// The JSON cannot be decoded into the expected wire structure.
    #[error("invalid event JSON: {0}")]
    InvalidJson(#[from] serde_json::Error),
    /// A newer or older contract version reached this reader.
    #[error("unsupported contract version {found}; expected {expected}")]
    UnsupportedVersion {
        /// Version this reader understands.
        expected: u32,
        /// Version on the message.
        found: u64,
    },
    /// The envelope type disagrees with its payload action.
    #[error("event type {found} does not match payload type {expected}")]
    TypeMismatch {
        /// Type derived from the payload.
        expected: String,
        /// Type in the envelope.
        found: String,
    },
    /// The envelope kind disagrees with its contract.
    #[error("event kind {found:?} does not match contract kind {expected:?}")]
    KindMismatch {
        /// Contract kind.
        expected: EventKind,
        /// Envelope kind.
        found: EventKind,
    },
    /// Entity or revision metadata does not match fact/snapshot semantics.
    #[error("{0} metadata is invalid")]
    InvalidMetadata(&'static str),
    /// The contract produced a subject NATS cannot publish.
    #[error("invalid event subject {0}")]
    InvalidSubject(String),
}

/// Read type and version without deserializing a contract's data.
pub fn peek_header(raw: &[u8]) -> Result<EnvelopeHeader, ContractError> {
    Ok(serde_json::from_slice(raw)?)
}

/// Read a valid event ID without requiring its payload to decode.
pub fn peek_event_id(raw: &[u8]) -> Option<EventId> {
    let value: serde_json::Value = serde_json::from_slice(raw).ok()?;
    let id = value.get("id")?.as_str()?;
    Uuid::parse_str(id).ok().map(EventId)
}

/// Decode one supported version and check its envelope against the payload.
pub fn decode<T: Contract + DeserializeOwned>(raw: &[u8]) -> Result<Envelope<T>, ContractError> {
    let header = peek_header(raw)?;
    if header.version != u64::from(T::VERSION) {
        return Err(ContractError::UnsupportedVersion {
            expected: T::VERSION,
            found: header.version,
        });
    }
    let envelope: Envelope<T> = serde_json::from_slice(raw)?;
    envelope.validate()?;
    Ok(envelope)
}

/// The application rule for an event on the bus.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum EventKind {
    /// An independently applicable occurrence.
    Fact,
    /// The complete state at one entity revision.
    Snapshot,
}

/// A versioned event and its typed payload.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct Envelope<T> {
    /// Stable event identifier across redeliveries and schema versions.
    pub id: EventId,
    /// Concrete event type, such as `github.issues.labeled`.
    #[serde(rename = "type")]
    pub event_type: String,
    /// Contract major version.
    #[schemars(range(min = 0, max = u32::MAX))]
    pub version: u32,
    /// Fact or full state snapshot.
    pub kind: EventKind,
    /// Service that committed this event.
    pub producer: String,
    /// Producer transaction time.
    pub occurred_at: Timestamp,
    /// Snapshot entity, absent for facts.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub entity: Option<String>,
    /// Snapshot revision, absent for facts.
    #[serde(skip_serializing_if = "Option::is_none")]
    #[schemars(range(min = 0, max = u64::MAX))]
    pub revision: Option<u64>,
    /// Source request or event, when known.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub causation: Option<String>,
    /// Contract payload.
    pub data: T,
}

/// Wire metadata supplied by a contract payload.
pub trait Contract {
    /// Contract family, shared by its action variants.
    const FAMILY: &'static str;
    /// Contract major version.
    const VERSION: u32;
    /// Event application kind.
    const KIND: EventKind;

    /// Concrete event type.
    fn event_type(&self) -> String;
    /// NATS subject for this event.
    fn subject(&self) -> String;

    /// Check payload-specific envelope metadata after the shared kind rules.
    fn validate_metadata(
        &self,
        _entity: Option<&str>,
        _revision: Option<u64>,
    ) -> Result<(), ContractError> {
        Ok(())
    }
}

impl<T: Contract> Envelope<T> {
    /// Wrap a payload using its own type, version, kind and subject.
    pub fn fact(id: EventId, producer: impl Into<String>, occurred_at: Timestamp, data: T) -> Self {
        Self {
            id,
            event_type: data.event_type(),
            version: T::VERSION,
            kind: T::KIND,
            producer: producer.into(),
            occurred_at,
            entity: None,
            revision: None,
            causation: None,
            data,
        }
    }

    /// Wrap a complete state snapshot at one entity revision.
    pub fn snapshot(
        id: EventId,
        producer: impl Into<String>,
        occurred_at: Timestamp,
        entity: String,
        revision: u64,
        data: T,
    ) -> Self {
        Self {
            id,
            event_type: data.event_type(),
            version: T::VERSION,
            kind: T::KIND,
            producer: producer.into(),
            occurred_at,
            entity: Some(entity),
            revision: Some(revision),
            causation: None,
            data,
        }
    }

    /// Validate that metadata matches the payload's contract.
    pub fn validate(&self) -> Result<(), ContractError> {
        if self.version != T::VERSION {
            return Err(ContractError::UnsupportedVersion {
                expected: T::VERSION,
                found: u64::from(self.version),
            });
        }
        if self.kind != T::KIND {
            return Err(ContractError::KindMismatch {
                expected: T::KIND,
                found: self.kind,
            });
        }
        let expected = self.data.event_type();
        if self.event_type != expected {
            return Err(ContractError::TypeMismatch {
                expected,
                found: self.event_type.clone(),
            });
        }
        match self.kind {
            EventKind::Fact if self.entity.is_some() || self.revision.is_some() => {
                return Err(ContractError::InvalidMetadata("fact"));
            }
            EventKind::Snapshot
                if self.entity.as_deref().is_none_or(str::is_empty) || self.revision.is_none() =>
            {
                return Err(ContractError::InvalidMetadata("snapshot"));
            }
            _ => {}
        }
        self.data
            .validate_metadata(self.entity.as_deref(), self.revision)?;
        let subject = self.subject();
        if !subject.split('.').all(valid_subject_token) {
            return Err(ContractError::InvalidSubject(subject));
        }
        Ok(())
    }

    /// NATS subject derived from the payload.
    pub fn subject(&self) -> String {
        self.data.subject()
    }
}

/// One entry in the schema generation registry.
#[derive(Clone, Copy, Debug)]
pub struct ContractRegistration {
    /// Contract family name.
    pub family: &'static str,
    /// Contract major version.
    pub version: u32,
    /// Schema after contract-specific wire adjustments.
    pub schema: fn(SchemaSettings) -> Result<serde_json::Value, serde_json::Error>,
    /// Contract-specific schema adjustments.
    pub prepare_schema: fn(&mut serde_json::Value),
    /// Checked fixture decode and serialization.
    pub round_trip: fn(&[u8]) -> Result<serde_json::Value, ContractError>,
    /// Checked Python decoder exported by the package.
    pub python_decoder: &'static str,
}

/// Build the schema for one registered wire type.
fn generate_schema<T: JsonSchema>(
    settings: SchemaSettings,
) -> Result<serde_json::Value, serde_json::Error> {
    serde_json::to_value(
        settings
            .into_generator()
            .into_root_schema_for::<Envelope<T>>(),
    )
}

/// Decode and serialize a registered contract fixture.
fn round_trip<T: Contract + DeserializeOwned + Serialize>(
    raw: &[u8],
) -> Result<serde_json::Value, ContractError> {
    Ok(serde_json::to_value(decode::<T>(raw)?)?)
}

/// Register one typed GitHub family with its schema and decoder.
const fn github_registration<P>(python_decoder: &'static str) -> ContractRegistration
where
    P: GithubPayload + JsonSchema + Serialize + DeserializeOwned,
{
    ContractRegistration {
        family: P::FAMILY,
        version: 1,
        schema: generate_schema::<GithubEvent<P>>,
        prepare_schema: schema::prepare_github_event::<P>,
        round_trip: round_trip::<GithubEvent<P>>,
        python_decoder,
    }
}

/// Contract families emitted by the schema generator.
pub const REGISTRY: &[ContractRegistration] = &[
    ContractRegistration {
        family: ExchangeTurnStatus::FAMILY,
        version: ExchangeTurnStatus::VERSION,
        schema: generate_schema::<ExchangeTurnStatus>,
        prepare_schema: schema::prepare_exchange_turn_status,
        round_trip: round_trip::<ExchangeTurnStatus>,
        python_decoder: "decode_exchange_turn_status",
    },
    ContractRegistration {
        family: ExchangeTurnFinished::FAMILY,
        version: ExchangeTurnFinished::VERSION,
        schema: generate_schema::<ExchangeTurnFinished>,
        prepare_schema: schema::prepare_exchange_turn_finished,
        round_trip: round_trip::<ExchangeTurnFinished>,
        python_decoder: "decode_exchange_turn_finished",
    },
    ContractRegistration {
        family: InferenceUsage::FAMILY,
        version: InferenceUsage::VERSION,
        schema: generate_schema::<InferenceUsage>,
        prepare_schema: schema::prepare_inference_usage,
        round_trip: round_trip::<InferenceUsage>,
        python_decoder: "decode_inference_usage",
    },
    ContractRegistration {
        family: ConfigTargets::FAMILY,
        version: ConfigTargets::VERSION,
        schema: generate_schema::<ConfigTargets>,
        prepare_schema: schema::prepare_config_targets,
        round_trip: round_trip::<ConfigTargets>,
        python_decoder: "decode_config_targets",
    },
    ContractRegistration {
        family: GithubIssues::FAMILY,
        version: GithubIssues::VERSION,
        schema: generate_schema::<GithubIssues>,
        prepare_schema: schema::prepare_github_issues,
        round_trip: round_trip::<GithubIssues>,
        python_decoder: "decode_github_issues",
    },
    github_registration::<IssueCommentPayload>("decode_github_issue_comment"),
    github_registration::<PullRequestPayload>("decode_github_pull_request"),
    github_registration::<PullRequestReviewPayload>("decode_github_pull_request_review"),
    github_registration::<PullRequestReviewCommentPayload>(
        "decode_github_pull_request_review_comment",
    ),
    github_registration::<PullRequestReviewThreadPayload>(
        "decode_github_pull_request_review_thread",
    ),
    github_registration::<PushPayload>("decode_github_push"),
    github_registration::<StatusPayload>("decode_github_status"),
    github_registration::<CheckRunPayload>("decode_github_check_run"),
    github_registration::<CheckSuitePayload>("decode_github_check_suite"),
    github_registration::<WorkflowRunPayload>("decode_github_workflow_run"),
    ContractRegistration {
        family: CoverageAdvanced::FAMILY,
        version: CoverageAdvanced::VERSION,
        schema: generate_schema::<CoverageAdvanced>,
        prepare_schema: schema::prepare_coverage_advanced,
        round_trip: round_trip::<CoverageAdvanced>,
        python_decoder: "decode_coverage_advanced",
    },
    ContractRegistration {
        family: CoverageGap::FAMILY,
        version: CoverageGap::VERSION,
        schema: generate_schema::<CoverageGap>,
        prepare_schema: schema::prepare_coverage_gap,
        round_trip: round_trip::<CoverageGap>,
        python_decoder: "decode_coverage_gap",
    },
    ContractRegistration {
        family: RequestOutcome::FAMILY,
        version: RequestOutcome::VERSION,
        schema: generate_schema::<RequestOutcome>,
        prepare_schema: schema::prepare_request_outcome,
        round_trip: round_trip::<RequestOutcome>,
        python_decoder: "decode_request_outcome",
    },
    ContractRegistration {
        family: TaskSnapshot::FAMILY,
        version: TaskSnapshot::VERSION,
        schema: generate_schema::<TaskSnapshot>,
        prepare_schema: schema::prepare_task_snapshot,
        round_trip: round_trip::<TaskSnapshot>,
        python_decoder: "decode_task_snapshot",
    },
    ContractRegistration {
        family: ReadModelRecord::FAMILY,
        version: ReadModelRecord::VERSION,
        schema: generate_schema::<ReadModelRecord>,
        prepare_schema: schema::prepare_read_model_record,
        round_trip: round_trip::<ReadModelRecord>,
        python_decoder: "decode_read_model_record",
    },
    ContractRegistration {
        family: TaskNotice::FAMILY,
        version: TaskNotice::VERSION,
        schema: generate_schema::<TaskNotice>,
        prepare_schema: schema::prepare_task_notice,
        round_trip: round_trip::<TaskNotice>,
        python_decoder: "decode_task_notice",
    },
    ContractRegistration {
        family: TaskReport::FAMILY,
        version: TaskReport::VERSION,
        schema: generate_schema::<TaskReport>,
        prepare_schema: schema::prepare_task_report,
        round_trip: round_trip::<TaskReport>,
        python_decoder: "decode_task_report",
    },
    ContractRegistration {
        family: TurnSubmitted::FAMILY,
        version: TurnSubmitted::VERSION,
        schema: generate_schema::<TurnSubmitted>,
        prepare_schema: schema::prepare_turn_submitted,
        round_trip: round_trip::<TurnSubmitted>,
        python_decoder: "decode_turn_submitted",
    },
    ContractRegistration {
        family: TurnStatus::FAMILY,
        version: TurnStatus::VERSION,
        schema: generate_schema::<TurnStatus>,
        prepare_schema: schema::prepare_turn_status,
        round_trip: round_trip::<TurnStatus>,
        python_decoder: "decode_turn_status",
    },
    ContractRegistration {
        family: CredentialChanged::FAMILY,
        version: CredentialChanged::VERSION,
        schema: generate_schema::<CredentialChanged>,
        prepare_schema: schema::prepare_credential_changed,
        round_trip: round_trip::<CredentialChanged>,
        python_decoder: "decode_credential_changed",
    },
    ContractRegistration {
        family: TurnFinished::FAMILY,
        version: TurnFinished::VERSION,
        schema: generate_schema::<TurnFinished>,
        prepare_schema: schema::prepare_turn_finished,
        round_trip: round_trip::<TurnFinished>,
        python_decoder: "decode_turn_finished",
    },
];
