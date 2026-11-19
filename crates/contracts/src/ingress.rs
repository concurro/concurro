//! Producer coverage facts used by every recovering consumer.

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use crate::{Contract, EventKind, Timestamp};

/// The producer has published everything through a delivery time.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct CoverageAdvanced {
    /// Latest completely covered GitHub delivery time.
    pub covered_until: Timestamp,
}

impl Contract for CoverageAdvanced {
    const FAMILY: &'static str = "ingress.coverage.advanced";
    const VERSION: u32 = 1;
    const KIND: EventKind = EventKind::Fact;

    fn event_type(&self) -> String {
        Self::FAMILY.to_owned()
    }

    fn subject(&self) -> String {
        Self::FAMILY.to_owned()
    }
}

/// An interval for which delivery history cannot establish completeness.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, JsonSchema)]
pub struct CoverageGap {
    /// Beginning of the incomplete interval.
    pub from: Timestamp,
    /// End of the incomplete interval.
    pub to: Timestamp,
    /// Accepted App installation that must reconcile.
    pub installation_id: i64,
}

impl Contract for CoverageGap {
    const FAMILY: &'static str = "ingress.coverage.gap";
    const VERSION: u32 = 1;
    const KIND: EventKind = EventKind::Fact;

    fn event_type(&self) -> String {
        Self::FAMILY.to_owned()
    }

    fn subject(&self) -> String {
        Self::FAMILY.to_owned()
    }
}
