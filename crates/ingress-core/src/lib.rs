//! GitHub delivery classification and pure event mapping.

mod issues;
mod mapped;
mod recovery;

use chrono::{DateTime, Utc};
use concurro_contracts::{Actor, Repository};
use serde::Deserialize;
use serde_json::Value;

pub use issues::map_issues;
pub use mapped::{MappedEvent, map_other};
pub use recovery::{DeliveryAttempt, DeliveryPage, RecoveryDecision, decide_recovery};

/// Revision of the GitHub delivery-to-event mapping.
pub const MAPPING_REVISION: u32 = 1;

/// GitHub object carrying a numeric ID.
#[derive(Deserialize)]
pub(crate) struct RawId {
    /// Numeric ID.
    pub id: i64,
}

/// Repository fields needed by all GitHub facts.
#[derive(Deserialize)]
pub(crate) struct RawRepository {
    /// Numeric repository ID.
    pub id: i64,
    /// Owner identity.
    pub owner: RawId,
    /// Owner and repository name.
    pub full_name: String,
}

impl From<RawRepository> for Repository {
    fn from(raw: RawRepository) -> Self {
        Self {
            id: raw.id,
            owner_id: raw.owner.id,
            full_name: raw.full_name,
        }
    }
}

/// Input common to all modeled GitHub webhook families.
#[derive(Deserialize)]
pub(crate) struct RawCommon {
    /// Open action name, absent for push and status.
    pub action: Option<String>,
    /// App installation identity.
    pub installation: RawId,
    /// Repository identity.
    pub repository: RawRepository,
    /// Acting user or bot.
    pub sender: Actor,
}

/// Receipt fields retained even when a delivery cannot map.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DeliveryMetadata {
    /// GitHub event family from the request header.
    pub event: String,
    /// GitHub action supplied by the payload.
    pub action: Option<String>,
    /// App installation that sent the delivery.
    pub installation_id: Option<i64>,
    /// GitHub repository ID when present.
    pub repository_id: Option<i64>,
}

/// Classification made without any storage or transport access.
#[derive(Clone, Debug, PartialEq)]
pub enum Mapping {
    /// Installation is outside the configured set.
    OtherInstallation(DeliveryMetadata),
    /// The event family has no mapper yet.
    Unmodeled(DeliveryMetadata),
    /// A field required by this mapper is absent or mistyped.
    Unmappable(DeliveryMetadata, String),
    /// A fully mapped event ready for atomic receipt and outbox admission.
    Published(DeliveryMetadata, Box<MappedEvent>),
}

/// Classify and map one signed delivery for an accepted installation.
pub fn map_delivery(
    delivery_id: &str,
    event: &str,
    raw: &[u8],
    received_at: DateTime<Utc>,
    installations: &[i64],
) -> Mapping {
    let value: Value = match serde_json::from_slice(raw) {
        Ok(value) => value,
        Err(error) => {
            return Mapping::Unmappable(
                DeliveryMetadata {
                    event: event.to_owned(),
                    action: None,
                    installation_id: None,
                    repository_id: None,
                },
                error.to_string(),
            );
        }
    };
    let metadata = DeliveryMetadata {
        event: event.to_owned(),
        action: value
            .get("action")
            .and_then(Value::as_str)
            .map(str::to_owned),
        installation_id: value.pointer("/installation/id").and_then(Value::as_i64),
        repository_id: value.pointer("/repository/id").and_then(Value::as_i64),
    };
    if let Some(installation_id) = metadata.installation_id
        && !installations.contains(&installation_id)
    {
        return Mapping::OtherInstallation(metadata);
    }
    let mapped = if event == "issues" {
        Some(issues::map_issues(delivery_id, &value, received_at).map(MappedEvent::Issues))
    } else {
        map_other(delivery_id, event, &value, received_at)
    };
    match mapped {
        None => Mapping::Unmodeled(metadata),
        Some(Ok(mapped)) => Mapping::Published(metadata, Box::new(mapped)),
        Some(Err(error)) => Mapping::Unmappable(metadata, error),
    }
}
