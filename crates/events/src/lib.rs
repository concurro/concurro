//! Transport-independent event handling decisions.

use std::time::Duration;

use concurro_contracts::Envelope;
use concurro_db::UnitOfWork;
use serde::{Deserialize, Serialize};

/// Position assigned by one incarnation of a JetStream stream.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct JournalPosition {
    /// Exact stream creation timestamp returned by JetStream.
    pub stream_created: String,
    /// Stream sequence in that incarnation.
    pub sequence: u64,
}

/// Consumer floor from which a producer should replay retained events.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ReplayRequest {
    /// Acknowledgement floor recorded before the loss.
    pub floor: JournalPosition,
    /// Stream incarnation receiving this replay attempt.
    pub target_created: String,
}

/// Part of a replay range that the producer can no longer prove complete.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Unrecoverable {
    /// Creation time of the oldest retained event, if any.
    pub before: Option<String>,
}

/// Result of re-queueing retained journal rows.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ReplayAnswer {
    /// Producer database time when all retained replay batches completed.
    pub requested_at: String,
    /// Latest original position considered for replay.
    pub replayed_through: Option<JournalPosition>,
    /// Missing journal history before retained rows.
    pub unrecoverable: Option<Unrecoverable>,
}

/// Result of comparing a durable consumer floor with its current stream.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ContinuityDecision {
    /// Record the floor immediately below the stream's first message.
    Initialize(JournalPosition),
    /// The current acknowledgement floor is continuous with stored progress.
    Advance(JournalPosition),
    /// An earlier floor may have messages the stream no longer retains.
    Lost(JournalPosition),
}

/// Detect a replaced stream or expiration ahead of a recorded floor.
pub fn check_continuity(
    recorded: Option<&JournalPosition>,
    stream_created: &str,
    first_sequence: u64,
    ack_floor: u64,
) -> ContinuityDecision {
    let Some(previous) = recorded else {
        return ContinuityDecision::Initialize(JournalPosition {
            stream_created: stream_created.to_owned(),
            sequence: first_sequence.saturating_sub(1),
        });
    };
    if previous.stream_created != stream_created
        || first_sequence > previous.sequence.saturating_add(1)
    {
        return ContinuityDecision::Lost(previous.clone());
    }
    ContinuityDecision::Advance(JournalPosition {
        stream_created: stream_created.to_owned(),
        sequence: previous.sequence.max(ack_floor),
    })
}

/// Coverage is current only after its own ack and every local effect is settled.
pub fn coverage_is_applied(
    coverage: &JournalPosition,
    floor: &JournalPosition,
    has_pending_effects: bool,
    has_open_loss: bool,
) -> bool {
    coverage.stream_created == floor.stream_created
        && floor.sequence >= coverage.sequence
        && !has_pending_effects
        && !has_open_loss
}

#[cfg(test)]
mod continuity_tests {
    use super::{ContinuityDecision, JournalPosition, check_continuity, coverage_is_applied};

    /// Replacement and expiration both report the previously durable floor.
    #[test]
    fn detects_stream_loss_without_losing_recorded_floor() {
        let prior = JournalPosition {
            stream_created: "old".to_owned(),
            sequence: 4,
        };
        assert_eq!(
            check_continuity(Some(&prior), "new", 1, 0),
            ContinuityDecision::Lost(prior.clone())
        );
        assert_eq!(
            check_continuity(Some(&prior), "old", 6, 4),
            ContinuityDecision::Lost(prior.clone())
        );
        assert_eq!(
            check_continuity(None, "old", 6, 0),
            ContinuityDecision::Initialize(JournalPosition {
                stream_created: "old".to_owned(),
                sequence: 5,
            })
        );
    }

    /// Parking, held work, or an open loss blocks a coverage acknowledgement.
    #[test]
    fn coverage_requires_every_local_effect() {
        let coverage = JournalPosition {
            stream_created: "stream".to_owned(),
            sequence: 7,
        };
        let floor = JournalPosition {
            stream_created: "stream".to_owned(),
            sequence: 8,
        };
        assert!(coverage_is_applied(&coverage, &floor, false, false));
        assert!(!coverage_is_applied(&coverage, &floor, true, false));
        assert!(!coverage_is_applied(&coverage, &floor, false, true));
        assert!(!coverage_is_applied(
            &coverage,
            &JournalPosition {
                stream_created: "recreated".to_owned(),
                sequence: 8,
            },
            false,
            false
        ));
    }
}

/// Count the bytes NATS sends for an event body and its message ID header.
pub fn publish_size(body_bytes: usize, message_id: &str) -> usize {
    body_bytes + "NATS/1.0\r\nNats-Msg-Id: \r\n\r\n".len() + message_id.len()
}

/// The decision after handling one event in a transaction.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Outcome {
    /// Commit the handler's writes and acknowledge the message.
    Done,
    /// Roll back and retry with backoff.
    Retry(String),
    /// Wait for a named prerequisite without using the attempt budget.
    Hold(String),
    /// Roll back and park the event for an operator.
    Reject(String),
}

/// Durable processing result mapped to a transport acknowledgement.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ProcessResult {
    /// Handler effects committed; acknowledge.
    Done,
    /// Already consumed, held, or a version this reader does not support; acknowledge.
    Skipped,
    /// Waiting for a prerequisite; acknowledge.
    Held,
    /// Stored for operator action; terminate delivery.
    Parked,
    /// Handler effects rolled back; request delivery after the delay.
    Retry(Duration),
}

/// Service domain behavior invoked by the shared consumer.
pub trait Handler<T>: Send + Sync {
    /// Write the effect of a decoded event through the caller's unit of work.
    fn handle(
        &self,
        uow: &mut UnitOfWork<'_>,
        event: Envelope<T>,
    ) -> impl Future<Output = Outcome> + Send;
}
