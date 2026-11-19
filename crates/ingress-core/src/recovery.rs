//! Pure App-delivery recovery decisions across all listed pages.

use std::collections::{HashMap, HashSet};

use serde::{Deserialize, Serialize};

/// One GitHub App webhook delivery attempt.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct DeliveryAttempt {
    /// GitHub attempt ID used by the redelivery endpoint.
    pub id: i64,
    /// GUID shared by every attempt for one delivery.
    pub guid: String,
    /// App installation the delivery belongs to.
    pub installation_id: Option<i64>,
    /// GitHub delivery status.
    pub status: Option<String>,
    /// HTTP response code when GitHub reached the endpoint.
    pub status_code: Option<i64>,
}

/// One page from the Python service's App delivery API.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct DeliveryPage {
    /// Delivery attempts in GitHub's order.
    pub deliveries: Vec<DeliveryAttempt>,
    /// Cursor for the following page.
    pub next_cursor: Option<String>,
}

/// Redelivery requests and whether coverage remains unresolved.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RecoveryDecision {
    /// Latest failed attempt ID for each unresolved GUID.
    pub redeliver: Vec<i64>,
    /// Number of GUIDs awaiting redelivery or still in progress.
    pub unresolved: usize,
    /// GUIDs that still have no receipt or successful attempt.
    pub unresolved_guids: Vec<String>,
    /// GUIDs with an attempt still in progress.
    pub pending_guids: Vec<String>,
}

/// Decide only after all pages and local receipts are known.
pub fn decide_recovery(
    attempts: &[DeliveryAttempt],
    received: &HashSet<String>,
    installations: &[i64],
) -> RecoveryDecision {
    let mut groups: HashMap<&str, Vec<&DeliveryAttempt>> = HashMap::new();
    for attempt in attempts {
        if attempt
            .installation_id
            .is_some_and(|id| installations.contains(&id))
        {
            groups.entry(&attempt.guid).or_default().push(attempt);
        }
    }
    let mut decision = RecoveryDecision {
        redeliver: Vec::new(),
        unresolved: 0,
        unresolved_guids: Vec::new(),
        pending_guids: Vec::new(),
    };
    for (guid, group) in groups {
        if received.contains(guid)
            || group.iter().any(|attempt| {
                attempt
                    .status
                    .as_deref()
                    .is_some_and(|status| status.eq_ignore_ascii_case("ok"))
                    || attempt
                        .status_code
                        .is_some_and(|code| (200..400).contains(&code))
            })
        {
            continue;
        }
        decision.unresolved += 1;
        decision.unresolved_guids.push(guid.to_owned());
        let pending = group.iter().any(|attempt| {
            attempt.status.as_deref().is_some_and(|status| {
                matches!(
                    status.to_ascii_lowercase().as_str(),
                    "pending" | "scheduled" | "queued" | "delivering" | "in progress"
                )
            }) || attempt.status.is_none() && attempt.status_code.unwrap_or(0) == 0
        });
        if pending {
            decision.pending_guids.push(guid.to_owned());
            continue;
        }
        let all_failed = group.iter().all(|attempt| {
            attempt
                .status_code
                .is_some_and(|code| (400..600).contains(&code))
                || attempt.status.as_deref().is_some_and(|status| {
                    let lower = status.to_ascii_lowercase();
                    ["fail", "error", "invalid", "timed out", "timeout"]
                        .iter()
                        .any(|word| lower.contains(word))
                })
        });
        if all_failed && let Some(latest) = group.iter().max_by_key(|attempt| attempt.id) {
            decision.redeliver.push(latest.id);
        }
    }
    decision.redeliver.sort_unstable();
    decision.unresolved_guids.sort();
    decision.pending_guids.sort();
    decision
}

#[cfg(test)]
mod tests {
    use super::{DeliveryAttempt, decide_recovery};
    use std::collections::HashSet;

    /// Success on a later page resolves failure; only wholly failed GUIDs retry.
    #[test]
    fn full_listing_precedes_redelivery() {
        let attempts = vec![
            DeliveryAttempt {
                id: 1,
                guid: "recovered".to_owned(),
                installation_id: Some(42),
                status: Some("failed".to_owned()),
                status_code: Some(503),
            },
            DeliveryAttempt {
                id: 2,
                guid: "failed".to_owned(),
                installation_id: Some(42),
                status: Some("timeout".to_owned()),
                status_code: Some(0),
            },
            DeliveryAttempt {
                id: 3,
                guid: "recovered".to_owned(),
                installation_id: Some(42),
                status: Some("OK".to_owned()),
                status_code: Some(202),
            },
            DeliveryAttempt {
                id: 4,
                guid: "pending".to_owned(),
                installation_id: Some(42),
                status: Some("scheduled".to_owned()),
                status_code: None,
            },
            DeliveryAttempt {
                id: 5,
                guid: "received".to_owned(),
                installation_id: Some(42),
                status: Some("failed".to_owned()),
                status_code: Some(503),
            },
        ];
        let received = HashSet::from(["received".to_owned()]);
        let decision = decide_recovery(&attempts, &received, &[42]);
        assert_eq!(decision.redeliver, vec![2]);
        assert_eq!(decision.unresolved, 2);
    }
}
