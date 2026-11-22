//! Credential facts the accounts service publishes.

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use crate::{Contract, EventId, EventKind, Timestamp};

/// One committed change to an owner's credential entries or provider lists.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct CredentialChanged {
    /// GitHub owner ID.
    #[schemars(range(min = 1))]
    pub user_id: i64,
    /// The accounts service's change sequence, increasing across all owners.
    #[schemars(range(min = 1))]
    pub sequence: i64,
    /// The changed entry; absent for a reorder.
    pub entry_id: Option<String>,
    /// `anthropic` or `openai`.
    pub provider: String,
    /// What changed.
    pub change: CredentialChange,
    /// The provider list revision after the change.
    #[schemars(range(min = 0))]
    pub list_revision: i64,
    /// Versions this change retired.
    pub revoked: Vec<RevokedVersion>,
    /// When the change committed.
    pub at: Timestamp,
}

/// The kind of credential change.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum CredentialChange {
    /// An entry was added, or a relay approved.
    Added,
    /// An entry's secret or relay device was replaced.
    Replaced,
    /// An entry's name, filters or enabled state changed.
    Edited,
    /// A provider list was reordered.
    Reordered,
    /// An entry was removed.
    Removed,
    /// A version became unusable, as when it expired.
    Unusable,
}

/// One credential version a change retired.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct RevokedVersion {
    /// Opaque version identity, `sha256("{user}/{route}/{version}")` in hex.
    pub record_id: String,
    /// Version number.
    #[schemars(range(min = 1))]
    pub version: i64,
    /// Credential route.
    pub route: String,
    /// `vault` (the accounts secret store) or `relay`.
    pub custody: String,
}

impl CredentialChanged {
    /// The fact's stable ID, derived from `credential:<sequence>`.
    pub fn event_id(&self) -> EventId {
        EventId::named(&format!("credential:{}", self.sequence))
    }
}

impl Contract for CredentialChanged {
    const FAMILY: &'static str = "credential.changed";
    const VERSION: u32 = 1;
    const KIND: EventKind = EventKind::Fact;

    fn event_type(&self) -> String {
        format!("credential.{}.changed", self.user_id)
    }

    fn subject(&self) -> String {
        self.event_type()
    }
}
