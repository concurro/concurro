//! Fetch the producer baseline and stage it through Postgres storage.

use anyhow::{Context, Result, ensure};
use concurro_db::Database;
use reqwest::Client;
use serde::Deserialize;
use serde_json::Value;
use uuid::Uuid;

pub use concurro_backoffice_postgres::{
    TaskEvent, configured_targets, ingest_event_in_work, migrate,
};
use concurro_backoffice_postgres::{begin_baseline, finish_baseline, stage_baseline_row};

/// One task-service baseline row.
#[derive(Deserialize)]
struct SourceRow {
    ordinal: i64,
    entity: String,
    entity_id: String,
    revision: i64,
    body: Value,
}

/// One page of a producer's frozen source snapshot.
#[derive(Deserialize)]
struct SourcePage {
    snapshot_id: Uuid,
    rows: Vec<SourceRow>,
    next_cursor: Option<String>,
}

/// Replace the read model from stable baseline pages, then apply every buffered event.
pub async fn rebuild(
    database: &Database,
    client: &Client,
    task_api_url: &str,
    bearer: &str,
) -> Result<Uuid> {
    let overlap_floor = begin_baseline(database).await?;
    let mut cursor = None::<String>;
    let mut snapshot = None::<Uuid>;
    let mut previous = 0;
    loop {
        let mut request = client
            .get(format!(
                "{}/v1/baseline",
                task_api_url.trim_end_matches('/')
            ))
            .bearer_auth(bearer);
        if let Some(cursor) = cursor.as_deref() {
            request = request.query(&[("cursor", cursor)]);
        }
        let page: SourcePage = request.send().await?.error_for_status()?.json().await?;
        if let Some(id) = snapshot {
            ensure!(
                page.snapshot_id == id,
                "baseline snapshot changed between pages"
            );
        } else {
            snapshot = Some(page.snapshot_id);
        }
        let mut work = database.begin().await?;
        for row in page.rows {
            ensure!(row.ordinal > previous, "baseline row order changed");
            previous = row.ordinal;
            stage_baseline_row(
                &mut work,
                page.snapshot_id,
                row.ordinal,
                &row.entity,
                &row.entity_id,
                row.revision,
                row.body,
            )
            .await?;
        }
        work.commit().await?;
        cursor = page.next_cursor;
        if cursor.is_none() {
            break;
        }
    }
    let snapshot = snapshot.context("baseline returned no page")?;
    finish_baseline(database, snapshot, overlap_floor).await?;
    Ok(snapshot)
}

/// Store one event in a dedicated transaction for baseline overlap tests.
#[cfg(test)]
pub async fn ingest_event(database: &Database, event: &TaskEvent) -> Result<()> {
    let mut work = database.begin().await?;
    ingest_event_in_work(&mut work, event).await?;
    work.commit().await?;
    Ok(())
}

#[cfg(test)]
#[path = "model_tests.rs"]
mod tests;
