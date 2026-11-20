//! Task request outcomes published from the task application's decisions.

use crate::{Contract, ContractError, EventKind, RequestId, TaskId, Timestamp};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

/// Stable issue identity used by task and request records.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct TaskIssue {
    /// GitHub repository ID.
    pub repository_id: i64,
    /// Repository-local issue number.
    pub number: i64,
}

/// Authenticated GitHub identity responsible for a request.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct RequestActor {
    /// GitHub numeric account ID.
    pub id: i64,
    /// GitHub login at source capture time.
    pub login: String,
}

/// Final disposition of a request in the task decision queue.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum RequestOutcomeStatus {
    /// The request applied its allowed decision.
    Applied,
    /// The request could not be applied.
    Refused,
    /// A newer source revision or applied control made the request obsolete.
    Superseded,
}

/// Command kind and its exact argument shape on request outcome facts.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "kind", content = "argument", rename_all = "snake_case")]
pub enum RequestCommand {
    /// Start a task.
    Start,
    /// Stop a task.
    Stop,
    /// Approve one plan identity.
    Approve {
        /// Exact approved plan identity.
        plan_id: String,
    },
    /// Extend the plan-revision budget.
    Extend {
        /// Added plan revisions.
        plan_revisions: u16,
    },
    /// Resume with an optional description-only scope.
    Resume {
        /// Limit the resumed work to the PR description.
        description_only: bool,
    },
    /// Offer sponsorship for one or any provider.
    Sponsor {
        /// Provider when the offer is scoped.
        #[serde(skip_serializing_if = "Option::is_none")]
        provider: Option<String>,
    },
    /// Withdraw consent for one provider.
    Withdraw {
        /// Provider losing consent.
        provider: String,
    },
    /// Select a funding provider and mode.
    Funding {
        /// Funding provider.
        provider: String,
        /// Organization or personal mode.
        mode: String,
    },
    /// Retry a permitted operation.
    Retry,
    /// Invalidate the current task decision.
    Invalidate,
}

/// One durable request decision for publication and read models.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct RequestOutcome {
    /// Stable request ID.
    pub request_id: RequestId,
    /// Task, when one already exists or was created.
    pub task_id: Option<TaskId>,
    /// GitHub issue addressed by the request.
    pub issue: TaskIssue,
    /// Command kind and typed argument.
    #[serde(flatten)]
    pub command: RequestCommand,
    /// Verified request actor.
    pub actor: RequestActor,
    /// Channel that authenticated the source.
    pub channel: String,
    /// Stable channel source revision.
    pub source: String,
    /// When the actor made this source revision.
    pub source_at: Timestamp,
    /// Final status that also selects the event type.
    pub status: RequestOutcomeStatus,
    /// Refusal or supersession reason.
    pub reason: Option<String>,
    /// Channel route for an outcome reply.
    pub reply: String,
}

impl Contract for RequestOutcome {
    const FAMILY: &'static str = "request.tasks";
    const VERSION: u32 = 1;
    const KIND: EventKind = EventKind::Fact;

    fn event_type(&self) -> String {
        let status = match self.status {
            RequestOutcomeStatus::Applied => "applied",
            RequestOutcomeStatus::Refused => "refused",
            RequestOutcomeStatus::Superseded => "superseded",
        };
        format!("{}.{status}", Self::FAMILY)
    }

    fn subject(&self) -> String {
        self.event_type()
    }
}

/// One task budget counter and its configured limit.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct TaskBudget {
    /// Charged use so far.
    pub used: u64,
    /// Frozen maximum for this task.
    pub max: u64,
}

/// Budget counters shown with a task state snapshot.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct TaskBudgets {
    /// Charged model calls.
    pub model_calls: TaskBudget,
    /// Fix rounds, introduced in M2.
    pub fix_rounds: TaskBudget,
    /// Plan revisions, introduced in M2.
    pub plan_revisions: TaskBudget,
}

/// Full M1 task state snapshot, revised after each durable state change.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct TaskSnapshot {
    /// Stable task identity.
    pub task_id: TaskId,
    /// GitHub issue owning the task.
    pub issue: TaskIssue,
    /// Contribution in M1; later phases add standalone reviews.
    pub kind: String,
    /// Author of the issue, distinct from any request actor.
    pub author: RequestActor,
    /// Whether pinned inputs have completed initialization.
    pub initialized: bool,
    /// Manual M1 task mode.
    pub mode: String,
    /// Public stage after initialization.
    pub stage: Option<String>,
    /// Stage's attention or release reason, when present.
    pub stage_reason: Option<String>,
    /// Enrollment detail introduced in M3.
    pub enrollment: Option<serde_json::Value>,
    /// Plan detail introduced in M2.
    pub plan: Option<serde_json::Value>,
    /// Charged and configured task limits.
    pub budgets: TaskBudgets,
    /// Whether the current stage needs human attention.
    pub attention: bool,
    /// Pull request detail introduced in M2.
    pub pull_request: Option<serde_json::Value>,
}

impl Contract for TaskSnapshot {
    const FAMILY: &'static str = "task";
    const VERSION: u32 = 1;
    const KIND: EventKind = EventKind::Snapshot;

    fn event_type(&self) -> String {
        format!("task.{}.state", self.task_id.0)
    }

    fn subject(&self) -> String {
        self.event_type()
    }

    fn validate_metadata(
        &self,
        entity: Option<&str>,
        revision: Option<u64>,
    ) -> Result<(), ContractError> {
        if entity != Some(format!("task:{}", self.task_id.0).as_str())
            || !revision.is_some_and(|revision| revision >= 1)
        {
            return Err(ContractError::InvalidMetadata("task snapshot"));
        }
        Ok(())
    }
}

/// One task notice at a stable identity and revision.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct TaskNotice {
    /// Task whose notice changed.
    pub task_id: TaskId,
    /// Stable notice identity.
    #[schemars(with = "TaskId")]
    pub notice_id: uuid::Uuid,
    /// Notice category.
    pub kind: String,
    /// Open, resolved or superseded.
    pub state: String,
    /// Category-specific notice fields.
    pub data: serde_json::Value,
}

impl Contract for TaskNotice {
    const FAMILY: &'static str = "task.notice";
    const VERSION: u32 = 1;
    const KIND: EventKind = EventKind::Snapshot;

    fn event_type(&self) -> String {
        format!("task.{}.notice", self.task_id.0)
    }

    fn subject(&self) -> String {
        self.event_type()
    }

    fn validate_metadata(
        &self,
        entity: Option<&str>,
        revision: Option<u64>,
    ) -> Result<(), ContractError> {
        if entity != Some(format!("task:{}:notice:{}", self.task_id.0, self.notice_id).as_str())
            || !revision.is_some_and(|revision| revision >= 1)
        {
            return Err(ContractError::InvalidMetadata("task notice"));
        }
        Ok(())
    }
}

/// One report body at a stable task-scoped identity.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct TaskReport {
    /// Task whose report was recorded.
    pub task_id: TaskId,
    /// Stable report identity.
    #[schemars(with = "TaskId")]
    pub report_id: uuid::Uuid,
    /// Intake in M1; later phases add other report kinds.
    pub kind: String,
    /// Operation step that produced the report.
    pub step: String,
    /// Complete report body.
    pub body: serde_json::Value,
    /// SHA-256 of the encoded body bytes.
    pub digest: String,
}

impl Contract for TaskReport {
    const FAMILY: &'static str = "task.report";
    const VERSION: u32 = 1;
    const KIND: EventKind = EventKind::Snapshot;

    fn event_type(&self) -> String {
        format!("task.{}.report", self.task_id.0)
    }

    fn subject(&self) -> String {
        self.event_type()
    }

    fn validate_metadata(
        &self,
        entity: Option<&str>,
        revision: Option<u64>,
    ) -> Result<(), ContractError> {
        if entity != Some(format!("task:{}:report:{}", self.task_id.0, self.report_id).as_str())
            || !revision.is_some_and(|revision| revision >= 1)
        {
            return Err(ContractError::InvalidMetadata("task report"));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use crate::{ContractError, Envelope};

    use super::TaskSnapshot;

    #[test]
    fn task_snapshot_requires_its_entity_and_positive_revision() {
        let mut event: Envelope<TaskSnapshot> = serde_json::from_str(include_str!(
            "../../../contracts/fixtures/task.v1/valid/initializing.json"
        ))
        .expect("task fixture");
        event.validate().expect("valid snapshot");
        event.entity = Some("task:other".to_owned());
        std::assert_matches!(
            event.validate(),
            Err(ContractError::InvalidMetadata("task snapshot"))
        );
        event.entity = Some(format!("task:{}", event.data.task_id.0));
        event.revision = Some(0);
        std::assert_matches!(
            event.validate(),
            Err(ContractError::InvalidMetadata("task snapshot"))
        );
    }
}
