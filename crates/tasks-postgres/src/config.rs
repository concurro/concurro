//! Revisioned target identities from the task service's settings file.

use chrono::Utc;
use concurro_contracts::{ConfigTarget, ConfigTargets, Envelope, EventId, Timestamp};
use concurro_db::Database;
use concurro_events_postgres::Outbox;
use serde_json::Value;
use sqlx::types::Json;

/// Persist the current target list, advancing only when its identity changes.
pub async fn sync_config_targets(
    database: &Database,
    outbox: &Outbox,
    targets: &ConfigTargets,
) -> anyhow::Result<i64> {
    let body: Value = serde_json::to_value(&targets.targets)?;
    let mut work = database.begin().await?;
    let changed: Option<i64> = sqlx::query_scalar(
        "INSERT INTO tasks.config_targets (singleton,revision,body) VALUES (true,1,$1) \
         ON CONFLICT (singleton) DO UPDATE SET revision=tasks.config_targets.revision+1, \
         body=EXCLUDED.body,updated_at=now() \
         WHERE tasks.config_targets.body IS DISTINCT FROM EXCLUDED.body \
         RETURNING revision",
    )
    .bind(Json(&body))
    .fetch_optional(work.connection())
    .await?;
    let revision = if let Some(revision) = changed {
        let event = Envelope::snapshot(
            EventId::mint(),
            "tasks",
            Timestamp(Utc::now()),
            "config:targets".to_owned(),
            revision as u64,
            targets.clone(),
        );
        outbox.append(&mut work, &event).await?;
        revision
    } else {
        sqlx::query_scalar("SELECT revision FROM tasks.config_targets WHERE singleton")
            .fetch_one(work.connection())
            .await?
    };
    work.commit().await?;
    Ok(revision)
}

/// Read the current revision and target array from the same durable row.
pub async fn current_config_targets(
    database: &Database,
) -> anyhow::Result<Option<(i64, Vec<ConfigTarget>)>> {
    let row: Option<(i64, Json<Vec<ConfigTarget>>)> =
        sqlx::query_as("SELECT revision,body FROM tasks.config_targets WHERE singleton")
            .fetch_optional(database.pool())
            .await?;
    Ok(row.map(|(revision, Json(targets))| (revision, targets)))
}
