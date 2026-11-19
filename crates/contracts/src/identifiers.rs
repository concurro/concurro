//! Stable identifiers used by event wire types.

use std::sync::OnceLock;

use chrono::{DateTime, Datelike, Utc};
use regex::Regex;
use schemars::{JsonSchema, Schema, SchemaGenerator, json_schema};
use serde::{Deserialize, Deserializer, Serialize, de::Error as _};
use uuid::Uuid;

/// RFC 3339 wire spelling shared with the generated schema and Python validator.
pub const TIMESTAMP_PATTERN: &str = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-5][0-9](?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$(?![\s\S])";

/// A bus event identifier.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
#[serde(transparent)]
pub struct EventId(pub Uuid);

impl EventId {
    /// Mint a new fact identifier.
    pub fn mint() -> Self {
        Self(Uuid::now_v7())
    }

    /// Derive the stable ID of one GitHub delivery mapping revision.
    pub fn github(delivery_id: &str, mapping_revision: u32) -> Self {
        let name = format!("{delivery_id}:{mapping_revision}");
        Self(Uuid::new_v5(&Uuid::NAMESPACE_OID, name.as_bytes()))
    }
}

/// An orchestrator task identifier.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
#[serde(transparent)]
pub struct TaskId(pub Uuid);

impl TaskId {
    /// Mint a task identifier when an issue first becomes owned.
    pub fn mint() -> Self {
        Self(Uuid::now_v7())
    }
}

/// Represent a UUID as a format-checked JSON string.
fn uuid_schema() -> Schema {
    json_schema!({"type": "string", "format": "uuid"})
}

impl JsonSchema for EventId {
    fn schema_name() -> std::borrow::Cow<'static, str> {
        "EventId".into()
    }

    fn json_schema(_generator: &mut SchemaGenerator) -> Schema {
        uuid_schema()
    }
}

impl JsonSchema for TaskId {
    fn schema_name() -> std::borrow::Cow<'static, str> {
        "TaskId".into()
    }

    fn json_schema(_generator: &mut SchemaGenerator) -> Schema {
        uuid_schema()
    }
}

/// An RFC 3339 UTC timestamp on the wire.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(transparent)]
pub struct Timestamp(pub DateTime<Utc>);

impl<'de> Deserialize<'de> for Timestamp {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        static PATTERN: OnceLock<Regex> = OnceLock::new();
        let value = String::deserialize(deserializer)?;
        let grammar = PATTERN.get_or_init(|| {
            Regex::new(&TIMESTAMP_PATTERN.replace("$(?![\\s\\S])", "\\z"))
                .expect("timestamp pattern")
        });
        if !grammar.is_match(&value) {
            return Err(D::Error::custom("invalid RFC 3339 timestamp spelling"));
        }
        let parsed = DateTime::parse_from_rfc3339(&value).map_err(D::Error::custom)?;
        let utc = parsed.with_timezone(&Utc);
        if parsed.year() < 1 || !(1..=9999).contains(&utc.year()) {
            return Err(D::Error::custom(
                "timestamp is outside the supported UTC calendar",
            ));
        }
        Ok(Self(utc))
    }
}

impl JsonSchema for Timestamp {
    fn schema_name() -> std::borrow::Cow<'static, str> {
        "Timestamp".into()
    }

    fn json_schema(_generator: &mut SchemaGenerator) -> Schema {
        json_schema!({"type": "string", "format": "date-time", "pattern": TIMESTAMP_PATTERN})
    }
}

#[cfg(test)]
mod tests {
    use super::Timestamp;

    /// Keep Rust timestamp acceptance aligned with the Python reference decoder.
    #[test]
    fn shared_timestamp_calendar() {
        let corpus: serde_json::Value =
            serde_json::from_str(include_str!("../../../contracts/fixtures/timestamps.json"))
                .expect("timestamp corpus");
        for value in corpus["valid"].as_array().expect("valid cases") {
            let encoded = serde_json::to_string(value).expect("encoded timestamp");
            serde_json::from_str::<Timestamp>(&encoded).expect("valid timestamp");
        }
        for value in corpus["invalid"].as_array().expect("invalid cases") {
            let encoded = serde_json::to_string(value).expect("encoded timestamp");
            assert!(
                serde_json::from_str::<Timestamp>(&encoded).is_err(),
                "{value}"
            );
        }
    }
}
