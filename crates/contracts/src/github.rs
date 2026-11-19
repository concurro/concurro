//! GitHub issue delivery contract.

use schemars::{JsonSchema, Schema, SchemaGenerator, json_schema};
use serde::{Deserialize, Deserializer, Serialize, Serializer, de::Error, ser::SerializeMap};
use serde_json::{Value, json};

use crate::{Contract, EventKind, Timestamp, valid_subject_token};

/// GitHub repository identity used for routing.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct Repository {
    /// GitHub repository ID.
    #[schemars(range(min = i64::MIN, max = i64::MAX))]
    pub id: i64,
    /// GitHub owner ID.
    #[schemars(range(min = i64::MIN, max = i64::MAX))]
    pub owner_id: i64,
    /// GitHub's owner and repository name.
    pub full_name: String,
}

/// The actor responsible for a delivery.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct Actor {
    /// GitHub actor ID.
    #[schemars(range(min = i64::MIN, max = i64::MAX))]
    pub id: i64,
    /// GitHub login.
    pub login: String,
    /// Actor category, open to future GitHub values.
    #[serde(rename = "type")]
    pub kind: ActorKind,
}

/// GitHub actor category, preserving unknown values.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ActorKind {
    /// A user account.
    User,
    /// An automation account.
    Bot,
    /// An organization account.
    Organization,
    /// A category added by GitHub later.
    Other(String),
}

impl Serialize for ActorKind {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self {
            Self::User => serializer.serialize_str("User"),
            Self::Bot => serializer.serialize_str("Bot"),
            Self::Organization => serializer.serialize_str("Organization"),
            Self::Other(value) => serializer.serialize_str(value),
        }
    }
}

impl<'de> Deserialize<'de> for ActorKind {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = String::deserialize(deserializer)?;
        Ok(match value.as_str() {
            "User" => Self::User,
            "Bot" => Self::Bot,
            "Organization" => Self::Organization,
            _ => Self::Other(value),
        })
    }
}

impl JsonSchema for ActorKind {
    fn schema_name() -> std::borrow::Cow<'static, str> {
        "ActorKind".into()
    }

    fn json_schema(_generator: &mut SchemaGenerator) -> Schema {
        json_schema!({"type": "string", "x-concurro-open": true})
    }
}

/// GitHub issue state, which is a closed wire enum.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum IssueState {
    /// Issue accepts new work.
    Open,
    /// Issue is closed.
    Closed,
}

/// A label attached to an issue.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct Label {
    /// Label name as GitHub supplied it.
    pub name: String,
}

/// Previous issue values supplied with an edit.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct IssueChanges {
    /// Previous title, if changed.
    pub title_from: Option<String>,
    /// Previous body, if changed.
    pub body_from: Option<String>,
}

/// The destination key of a transferred issue.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct NewIssue {
    /// Destination repository ID.
    #[schemars(range(min = i64::MIN, max = i64::MAX))]
    pub repository_id: i64,
    /// Destination issue number.
    #[schemars(range(min = i64::MIN, max = i64::MAX))]
    pub number: i64,
}

/// Issue fields needed by routing and lists.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct Issue {
    /// Repository-local issue number.
    #[schemars(range(min = i64::MIN, max = i64::MAX))]
    pub number: i64,
    /// Current issue title.
    pub title: String,
    /// Current issue body, when present.
    pub body: Option<String>,
    /// Current issue state.
    pub state: IssueState,
    /// GitHub's state explanation, when present.
    pub state_reason: Option<String>,
    /// Issue author.
    pub user: Actor,
    /// Current labels.
    pub labels: Vec<Label>,
    /// Current assignees.
    pub assignees: Vec<Actor>,
    /// Issue creation time.
    pub created_at: Timestamp,
    /// Latest issue update time.
    pub updated_at: Timestamp,
    /// Whether this issue is a pull request.
    pub is_pull_request: bool,
}

/// Action-specific issue fields.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "action", rename_all = "snake_case")]
pub enum KnownIssueAction {
    /// GitHub opened the issue.
    Opened,
    /// GitHub edited the issue.
    Edited {
        /// Previous values supplied by GitHub.
        changes: IssueChanges,
    },
    /// GitHub closed the issue.
    Closed,
    /// GitHub reopened the issue.
    Reopened,
    /// A label was attached.
    Labeled {
        /// Attached label.
        label: Label,
    },
    /// A label was removed.
    Unlabeled {
        /// Removed label.
        label: Label,
    },
    /// An assignee was added.
    Assigned {
        /// Added actor.
        assignee: Actor,
    },
    /// An assignee was removed.
    Unassigned {
        /// Removed actor.
        assignee: Actor,
    },
    /// The issue moved to another repository.
    Transferred {
        /// Destination issue key.
        new_issue: NewIssue,
    },
    /// GitHub deleted the issue.
    Deleted,
}

impl KnownIssueAction {
    /// The action's wire name.
    pub fn name(&self) -> &'static str {
        match self {
            Self::Opened => "opened",
            Self::Edited { .. } => "edited",
            Self::Closed => "closed",
            Self::Reopened => "reopened",
            Self::Labeled { .. } => "labeled",
            Self::Unlabeled { .. } => "unlabeled",
            Self::Assigned { .. } => "assigned",
            Self::Unassigned { .. } => "unassigned",
            Self::Transferred { .. } => "transferred",
            Self::Deleted => "deleted",
        }
    }
}

/// A known issue action or a future GitHub action carrying common fields.
#[derive(Clone, Debug, PartialEq)]
pub enum IssueAction {
    /// Action whose specific fields this version understands.
    Known(KnownIssueAction),
    /// Unknown action name; common issue fields still route and project.
    Other(String),
}

impl IssueAction {
    /// The action's wire name, including unknown GitHub values.
    pub fn name(&self) -> &str {
        match self {
            Self::Known(action) => action.name(),
            Self::Other(action) => action,
        }
    }
}

impl Serialize for IssueAction {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self {
            Self::Known(action) => action.serialize(serializer),
            Self::Other(action) => {
                let mut object = serializer.serialize_map(Some(1))?;
                object.serialize_entry("action", action)?;
                object.end()
            }
        }
    }
}

impl<'de> Deserialize<'de> for IssueAction {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = Value::deserialize(deserializer)?;
        let action = value
            .get("action")
            .and_then(Value::as_str)
            .ok_or_else(|| D::Error::custom("missing issue action"))?;
        if matches!(
            action,
            "opened"
                | "edited"
                | "closed"
                | "reopened"
                | "labeled"
                | "unlabeled"
                | "assigned"
                | "unassigned"
                | "transferred"
                | "deleted"
        ) {
            serde_json::from_value(value)
                .map(Self::Known)
                .map_err(D::Error::custom)
        } else {
            if !valid_subject_token(action) {
                return Err(D::Error::custom("invalid issue action token"));
            }
            Ok(Self::Other(action.to_owned()))
        }
    }
}

impl JsonSchema for IssueAction {
    fn schema_name() -> std::borrow::Cow<'static, str> {
        "IssueAction".into()
    }

    fn json_schema(generator: &mut SchemaGenerator) -> Schema {
        let known = generator.subschema_for::<KnownIssueAction>();
        json!({
            "x-concurro-open": true,
            "anyOf": [
                known.to_value(),
                {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "pattern": "^(?!(?:opened|edited|closed|reopened|labeled|unlabeled|assigned|unassigned|transferred|deleted)$)[^.\\s*>\\u0000-\\u001f\\u007f-\\u009f]+$(?![\\s\\S])"
                        }
                    },
                    "required": ["action"]
                }
            ]
        }).try_into().expect("object schema")
    }
}

/// One mapped GitHub issues delivery.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct GithubIssues {
    /// GitHub delivery GUID.
    pub delivery_id: String,
    /// Revision of ingress's mapping code.
    #[schemars(range(min = 0, max = u32::MAX))]
    pub mapping_revision: u32,
    /// Ingress receipt time.
    pub received_at: Timestamp,
    /// App installation ID.
    #[schemars(range(min = i64::MIN, max = i64::MAX))]
    pub installation_id: i64,
    /// Repository identity.
    pub repository: Repository,
    /// Actor responsible for the action.
    pub sender: Actor,
    /// Issue as GitHub supplied it.
    pub issue: Issue,
    /// Variant-specific fields, tagged by the top-level `action`.
    #[serde(flatten)]
    pub action: IssueAction,
}

impl Contract for GithubIssues {
    const FAMILY: &'static str = "github.issues";
    const VERSION: u32 = 1;
    const KIND: EventKind = EventKind::Fact;

    fn event_type(&self) -> String {
        format!("github.issues.{}", self.action.name())
    }

    fn subject(&self) -> String {
        self.event_type()
    }
}
