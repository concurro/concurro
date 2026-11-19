//! Minimal input fields for GitHub issues deliveries.

use chrono::{DateTime, Utc};
use concurro_contracts::{
    Actor, Envelope, EventId, GithubIssues, Issue, IssueAction, IssueChanges, IssueState,
    KnownIssueAction, Label, NewIssue, Repository, Timestamp,
};
use serde::Deserialize;
use serde_json::Value;

use crate::{MAPPING_REVISION, RawId, RawRepository};

/// Fields used from the issue snapshot.
#[derive(Deserialize)]
struct RawIssue {
    /// Repository-local issue number.
    number: i64,
    /// Current issue title.
    title: String,
    /// Current issue body.
    body: Option<String>,
    /// Current issue state.
    state: IssueState,
    /// GitHub's state explanation.
    state_reason: Option<String>,
    /// Issue author.
    user: Actor,
    /// Current labels.
    labels: Vec<Label>,
    /// Current assignees.
    assignees: Vec<Actor>,
    /// Creation time.
    created_at: Timestamp,
    /// Update time.
    updated_at: Timestamp,
    /// Present when GitHub represents a pull request as an issue.
    pull_request: Option<Value>,
}

/// Previous value shape on edited deliveries.
#[derive(Deserialize)]
struct RawPrevious {
    /// Value before the edit.
    from: Option<String>,
}

/// GitHub's edited issue change shape.
#[derive(Deserialize)]
struct RawChanges {
    /// Previous title, if supplied.
    title: Option<RawPrevious>,
    /// Previous body, if supplied.
    body: Option<RawPrevious>,
    /// Destination issue after a transfer.
    new_issue: Option<RawTransferredIssue>,
    /// Destination repository after a transfer.
    new_repository: Option<RawId>,
}

/// Destination issue after a transfer.
#[derive(Deserialize)]
struct RawTransferredIssue {
    /// Destination issue number.
    number: i64,
}

/// Minimal issues delivery input.
#[derive(Deserialize)]
struct RawIssues {
    /// GitHub action name.
    action: String,
    /// App installation.
    installation: RawId,
    /// Repository identity.
    repository: RawRepository,
    /// Actor responsible for this action.
    sender: Actor,
    /// Issue snapshot.
    issue: RawIssue,
    /// Label for label actions.
    label: Option<Label>,
    /// Assignee for assignment actions.
    assignee: Option<Actor>,
    /// Changes on an edit.
    changes: Option<RawChanges>,
}

/// Map only the fields the issue contract and Python routing consume.
pub fn map_issues(
    delivery_id: &str,
    value: &Value,
    received_at: DateTime<Utc>,
) -> Result<Envelope<GithubIssues>, String> {
    let raw: RawIssues =
        serde_json::from_value(value.clone()).map_err(|error| error.to_string())?;
    let action = match raw.action.as_str() {
        "opened" => IssueAction::Known(KnownIssueAction::Opened),
        "closed" => IssueAction::Known(KnownIssueAction::Closed),
        "reopened" => IssueAction::Known(KnownIssueAction::Reopened),
        "deleted" => IssueAction::Known(KnownIssueAction::Deleted),
        "edited" => {
            let changes = raw.changes.ok_or("edited issue is missing changes")?;
            IssueAction::Known(KnownIssueAction::Edited {
                changes: IssueChanges {
                    title_from: changes.title.and_then(|value| value.from),
                    body_from: changes.body.and_then(|value| value.from),
                },
            })
        }
        "labeled" | "unlabeled" => {
            let label = raw.label.ok_or("label action is missing label")?;
            IssueAction::Known(if raw.action == "labeled" {
                KnownIssueAction::Labeled { label }
            } else {
                KnownIssueAction::Unlabeled { label }
            })
        }
        "assigned" | "unassigned" => {
            let assignee = raw
                .assignee
                .ok_or("assignment action is missing assignee")?;
            IssueAction::Known(if raw.action == "assigned" {
                KnownIssueAction::Assigned { assignee }
            } else {
                KnownIssueAction::Unassigned { assignee }
            })
        }
        "transferred" => {
            let changes = raw.changes.ok_or("transfer is missing changes")?;
            let destination = changes
                .new_issue
                .ok_or("transfer is missing changes.new_issue")?;
            let repository = changes
                .new_repository
                .ok_or("transfer is missing changes.new_repository")?;
            IssueAction::Known(KnownIssueAction::Transferred {
                new_issue: NewIssue {
                    repository_id: repository.id,
                    number: destination.number,
                },
            })
        }
        other => IssueAction::Other(other.to_owned()),
    };
    let data = GithubIssues {
        delivery_id: delivery_id.to_owned(),
        mapping_revision: MAPPING_REVISION,
        received_at: Timestamp(received_at),
        installation_id: raw.installation.id,
        repository: Repository {
            id: raw.repository.id,
            owner_id: raw.repository.owner.id,
            full_name: raw.repository.full_name,
        },
        sender: raw.sender,
        issue: Issue {
            number: raw.issue.number,
            title: raw.issue.title,
            body: raw.issue.body,
            state: raw.issue.state,
            state_reason: raw.issue.state_reason,
            user: raw.issue.user,
            labels: raw.issue.labels,
            assignees: raw.issue.assignees,
            created_at: raw.issue.created_at,
            updated_at: raw.issue.updated_at,
            is_pull_request: raw.issue.pull_request.is_some(),
        },
        action,
    };
    let event = Envelope::fact(
        EventId::github(delivery_id, MAPPING_REVISION),
        "ingress",
        Timestamp(received_at),
        data,
    );
    event.validate().map_err(|error| error.to_string())?;
    Ok(event)
}
