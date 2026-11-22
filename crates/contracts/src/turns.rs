//! Diagnostic turn facts and status snapshots shared across the task stream.

use crate::{Contract, ContractError, EventKind, TaskId, Timestamp};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// Content-addressed artifact kept by the task application.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct TurnArtifact {
    /// SHA-256 of the original artifact bytes.
    pub sha256: String,
    /// Original byte count.
    pub bytes: u64,
    /// MIME type used when retrieving the artifact.
    pub media_type: String,
}

/// Funding and immutable inputs recorded when one paid attempt is admitted.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct TurnSubmitted {
    /// Exchange turn identity.
    #[schemars(with = "TaskId")]
    pub turn_id: Uuid,
    /// Task attempt identity, equal to the turn in M1.
    #[schemars(with = "TaskId")]
    pub attempt_id: Uuid,
    /// Task charged for the attempt.
    pub task_id: TaskId,
    /// Funding grant identity, `org` for M1 organization funding.
    pub grant_id: String,
    /// Task stage at admission.
    pub stage: String,
    /// Agent role at admission.
    pub role: String,
    /// Provider harness.
    pub harness: String,
    /// Model selected for the paid attempt.
    pub model: String,
    /// Requested reasoning effort.
    pub effort: Option<String>,
    /// Optional agent profile.
    pub profile: Option<String>,
    /// Retained session identity when this is a continuation.
    pub session: Option<serde_json::Value>,
    /// Frozen instruction bundle identity.
    pub bundle_id: String,
    /// Budget and deadline limits supplied to the exchange.
    pub limits: serde_json::Value,
    /// Redacted immutable runner specification.
    pub spec: TurnArtifact,
    /// Reconstructed prompt, if available.
    pub prompt: Option<TurnArtifact>,
    /// Admission time.
    pub submitted_at: Timestamp,
}

impl Contract for TurnSubmitted {
    const FAMILY: &'static str = "turn.submitted";
    const VERSION: u32 = 1;
    const KIND: EventKind = EventKind::Fact;

    fn event_type(&self) -> String {
        format!("turn.{}.submitted", self.turn_id)
    }

    fn subject(&self) -> String {
        self.event_type()
    }
}

/// Runner identity currently associated with one turn observation.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct TurnRunner {
    /// Exchange runner session.
    #[schemars(with = "TaskId")]
    pub session_id: Uuid,
    /// GitHub Actions run, when observed.
    pub run_id: Option<String>,
    /// Actions rerun attempt, when observed.
    pub run_attempt: Option<u32>,
}

/// Latest observed lifecycle state of one turn.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct TurnStatus {
    /// Turn whose state changed.
    #[schemars(with = "TaskId")]
    pub turn_id: Uuid,
    /// Queued, running, finished or cancelled.
    pub status: String,
    /// Exchange admission time.
    pub created_at: Option<Timestamp>,
    /// Runner start time.
    pub started_at: Option<Timestamp>,
    /// Terminal time.
    pub finished_at: Option<Timestamp>,
    /// Session and run associated with this observation.
    pub runner: Option<TurnRunner>,
}

impl Contract for TurnStatus {
    const FAMILY: &'static str = "turn.status";
    const VERSION: u32 = 1;
    const KIND: EventKind = EventKind::Snapshot;

    fn event_type(&self) -> String {
        format!("turn.{}.status", self.turn_id)
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
            || !revision.is_some_and(|revision| revision >= 1)
        {
            return Err(ContractError::InvalidMetadata("turn status"));
        }
        Ok(())
    }
}

/// A transcript bundle and its compressed size.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct TurnTranscript {
    /// Original gzip content digest.
    pub blob: String,
    /// Original compressed byte count.
    pub bytes: u64,
}

/// Immutable outcome of one turn, recorded with its result archive.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct TurnFinished {
    /// Turn that ended.
    #[schemars(with = "TaskId")]
    pub turn_id: Uuid,
    /// Succeeded, failed or unknown.
    pub outcome: String,
    /// Open diagnostic reason for the outcome.
    pub reason: Option<String>,
    /// None, some or unknown paid provider requests.
    pub provider_requests: String,
    /// Exchange token and call totals when supplied by the Rust exchange.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub usage: Option<crate::TurnUsage>,
    /// Original model response artifact, when one was retained.
    pub response: Option<TurnArtifact>,
    /// Original workspace change artifact, when supplied.
    pub workspace: Option<TurnArtifact>,
    /// Readable original transcript bundle.
    pub transcript: Option<TurnTranscript>,
    /// Archive diagnostic if transcript decoding failed.
    pub transcript_error: Option<String>,
    /// Exchange elapsed time.
    pub elapsed_seconds: Option<f64>,
    /// Structured provider failure, when supplied.
    pub provider_failure: Option<serde_json::Value>,
    /// Whether funding ended with this turn.
    pub funding_ended: bool,
}

impl Contract for TurnFinished {
    const FAMILY: &'static str = "turn.finished";
    const VERSION: u32 = 1;
    const KIND: EventKind = EventKind::Fact;

    fn event_type(&self) -> String {
        format!("turn.{}.finished", self.turn_id)
    }

    fn subject(&self) -> String {
        self.event_type()
    }
}
