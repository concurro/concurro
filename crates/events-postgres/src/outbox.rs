//! Atomic event append operations for a producing service.

use concurro_contracts::{Contract, ContractError, Envelope};
use concurro_db::UnitOfWork;
use concurro_events::publish_size;
use concurro_jsonb::jsonb_representable;
use serde::Serialize;

/// An outbox admission or database failure.
#[derive(Debug, thiserror::Error)]
pub enum OutboxError {
    /// Envelope metadata or subject is invalid.
    #[error(transparent)]
    Contract(#[from] ContractError),
    /// The event subject falls outside this producer's stream.
    #[error("subject {0} is outside the stream")]
    SubjectOutsideStream(String),
    /// The serialized event and its NATS header exceed the configured cap.
    #[error("event is {size} bytes; cap is {max} bytes")]
    MessageTooLarge {
        /// Total publish byte count.
        size: usize,
        /// Configured message cap.
        max: usize,
    },
    /// The event was already appended at this version.
    #[error("event and version already exist in outbox")]
    Duplicate,
    /// The version cannot fit in the database column.
    #[error("event version exceeds the database integer range")]
    VersionOutOfRange,
    /// JSON encoding failed.
    #[error(transparent)]
    Serialize(#[from] serde_json::Error),
    /// PostgreSQL rejected the write.
    #[error(transparent)]
    Database(#[from] sqlx::Error),
}

/// Admission settings for one service's outbox stream.
#[derive(Clone, Debug)]
pub struct Outbox {
    max_message_bytes: usize,
    subjects: Vec<String>,
}

impl Outbox {
    /// Bind one outbox to the stream's subjects and payload cap.
    pub fn new(max_message_bytes: usize, subjects: Vec<String>) -> Self {
        Self {
            max_message_bytes,
            subjects,
        }
    }

    /// Append an event in the caller's state transaction and wake the forwarder on commit.
    pub async fn append<T: Contract + Serialize>(
        &self,
        uow: &mut UnitOfWork<'_>,
        event: &Envelope<T>,
    ) -> Result<i64, OutboxError> {
        event.validate()?;
        let subject = event.subject();
        if !self
            .subjects
            .iter()
            .any(|filter| matches_subject(filter, &subject))
        {
            return Err(OutboxError::SubjectOutsideStream(subject));
        }
        let message =
            String::from_utf8(serde_json::to_vec(event)?).expect("serde_json emits UTF-8");
        let size = publish_size(message.len(), &format!("{}:{}", event.id.0, event.version));
        if size > self.max_message_bytes {
            return Err(OutboxError::MessageTooLarge {
                size,
                max: self.max_message_bytes,
            });
        }
        let version = i32::try_from(event.version).map_err(|_| OutboxError::VersionOutOfRange)?;
        // Wire bytes remain authoritative when PostgreSQL cannot represent the JSON value.
        let projection: serde_json::Value = serde_json::from_str(&message)?;
        let parsed = jsonb_representable(&projection).then_some(message.as_str());
        let sequence = sqlx::query_scalar::<_, i64>(
            "INSERT INTO events.outbox (id, subject, type, version, message, wire_bytes) VALUES ($1, $2, $3, $4, $5::jsonb, $6) ON CONFLICT (id, version) DO NOTHING RETURNING sequence",
        )
        .bind(event.id.0)
        .bind(subject)
        .bind(&event.event_type)
        .bind(version)
        .bind(parsed)
        .bind(message.as_bytes())
        .fetch_optional(uow.connection())
        .await?
        .ok_or(OutboxError::Duplicate)?;
        sqlx::query("SELECT pg_notify('concurro_outbox', '')")
            .execute(uow.connection())
            .await?;
        Ok(sequence)
    }
}

/// Match a subject to NATS `*` and terminal `>` wildcard tokens.
fn matches_subject(filter: &str, subject: &str) -> bool {
    let mut tokens = subject.split('.');
    for pattern in filter.split('.') {
        if pattern == ">" {
            return tokens.next().is_some();
        }
        let Some(token) = tokens.next() else {
            return false;
        };
        if pattern != "*" && pattern != token {
            return false;
        }
    }
    tokens.next().is_none()
}

#[cfg(test)]
mod tests {
    use super::matches_subject;

    #[test]
    fn nats_subject_wildcards() {
        assert!(matches_subject("github.>", "github.issues.labeled"));
        assert!(matches_subject("github.*.labeled", "github.issues.labeled"));
        assert!(!matches_subject("github.>", "github"));
        assert!(!matches_subject("github.*.labeled", "github.issues.opened"));
    }
}
