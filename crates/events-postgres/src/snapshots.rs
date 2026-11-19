//! Snapshot revision admission in a consumer's unit of work.

use anyhow::{Context, Result};
use concurro_contracts::{Contract, Envelope, EventKind};
use concurro_db::UnitOfWork;

/// Record a newer snapshot revision and return whether its state should apply.
pub async fn apply_snapshot<T: Contract>(
    uow: &mut UnitOfWork<'_>,
    consumer: &str,
    envelope: &Envelope<T>,
) -> Result<bool> {
    envelope.validate()?;
    anyhow::ensure!(
        envelope.kind == EventKind::Snapshot,
        "event is not a snapshot"
    );
    let entity = envelope.entity.as_deref().context("snapshot entity")?;
    let revision = i64::try_from(envelope.revision.context("snapshot revision")?)
        .context("snapshot revision exceeds PostgreSQL bigint")?;
    let accepted: Option<i64> = sqlx::query_scalar(
        "INSERT INTO events.revisions (consumer, event_type, entity, revision) \
         VALUES ($1, $2, $3, $4) \
         ON CONFLICT (consumer, event_type, entity) DO UPDATE \
         SET revision = EXCLUDED.revision \
         WHERE events.revisions.revision < EXCLUDED.revision \
         RETURNING revision",
    )
    .bind(consumer)
    .bind(&envelope.event_type)
    .bind(entity)
    .bind(revision)
    .fetch_optional(uow.connection())
    .await?;
    Ok(accepted.is_some())
}
