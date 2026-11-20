//! Postgres persistence for event delivery and consumer state.

mod admin;
mod consumer;
mod forwarder;
mod journal;
mod outbox;
mod snapshots;

pub use admin::{
    HeldDetail, HeldSummary, ParkedDetail, ParkedSummary, consumer_status, discard_parked,
    list_held, list_parked, replay_parked, run_admin, show_held, show_parked,
};
pub use consumer::{process_delivery, process_released, release_held};
pub use forwarder::{ForwarderLease, OutboxRecord, UnpublishedStatus};
pub use journal::{
    capture_barrier, consumer_coverage_applied, observe_consumer, replay_retained,
    resolve_consumer_loss, take_ready_barrier,
};
pub use outbox::{Outbox, OutboxError};
pub use snapshots::apply_snapshot;

use concurro_db::Database;
use sqlx::migrate::Migrator;

static MIGRATIONS: Migrator = sqlx::migrate!("./migrations");

/// Install this library's tables in its own Postgres schema.
pub async fn migrate(database: &Database) -> anyhow::Result<()> {
    database.migrate_in_schema("events", &MIGRATIONS).await
}
