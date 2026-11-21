//! Read-only projections used by viewer-scoped HTTP handlers.

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use concurro_db::Database;
use serde::Serialize;
use serde_json::Value;
use sqlx::{Error, types::Json};

/// One stored projection row with its source revision.
pub struct ModelRow {
    /// Source family.
    pub entity: String,
    /// Stable source identity.
    pub entity_id: String,
    /// Last applied source revision.
    pub revision: i64,
    /// Complete projected body.
    pub body: Value,
}

/// Read projection readiness and its browser change revision.
pub async fn model_status(database: &Database) -> Result<(bool, i64), Error> {
    sqlx::query_as("SELECT ready,revision FROM backoffice.model_state WHERE singleton")
        .fetch_one(database.pool())
        .await
}

/// List current contribution and standalone task rows in stable source order.
pub async fn task_rows(database: &Database) -> Result<Vec<ModelRow>, Error> {
    let rows: Vec<(String, String, i64, Json<Value>)> = sqlx::query_as(
        "SELECT entity,entity_id,revision,body FROM backoffice.model_items \
         WHERE entity IN ('contribution_task','standalone_review') \
         ORDER BY entity,entity_id",
    )
    .fetch_all(database.pool())
    .await?;
    Ok(rows
        .into_iter()
        .map(|(entity, entity_id, revision, Json(body))| ModelRow {
            entity,
            entity_id,
            revision,
            body,
        })
        .collect())
}

/// Read all consent records before filtering by the current viewer and target.
pub async fn sponsorship_rows(database: &Database) -> Result<Vec<(String, Value)>, Error> {
    let rows: Vec<(String, Json<Value>)> = sqlx::query_as(
        "SELECT entity,body FROM backoffice.model_items \
         WHERE entity IN ('consent','consent_removal','standalone_funding', \
             'standalone_funding_removal','consent_state') \
         ORDER BY entity,entity_id",
    )
    .fetch_all(database.pool())
    .await?;
    Ok(rows
        .into_iter()
        .map(|(entity, Json(body))| (entity, body))
        .collect())
}

/// Read a task projection by stable ID.
pub async fn task_row(database: &Database, id: &str) -> Result<Option<ModelRow>, Error> {
    let row: Option<(String, String, i64, Json<Value>)> = sqlx::query_as(
        "SELECT entity,entity_id,revision,body FROM backoffice.model_items \
         WHERE entity_id=$1 AND entity IN ('contribution_task','standalone_review')",
    )
    .bind(id)
    .fetch_optional(database.pool())
    .await?;
    Ok(
        row.map(|(entity, entity_id, revision, Json(body))| ModelRow {
            entity,
            entity_id,
            revision,
            body,
        }),
    )
}

/// Read all records attached to one task and its source issue.
pub async fn task_records(
    database: &Database,
    task: &str,
    repository_id: i64,
    number: i64,
    repository: &str,
) -> Result<Vec<(String, Value)>, Error> {
    let rows: Vec<(String, Json<Value>)> = sqlx::query_as(
        "SELECT entity,body FROM backoffice.model_items WHERE \
         (body->>'task_id'=$1 OR body->>'standalone_task_id'=$1) \
         OR (body->>'repository_id'=$2 AND \
             (body->>'issue_number'=$3 OR body->>'pr_number'=$3)) \
         OR (entity='target_index' AND body->>'repository_id'=$2 \
             AND body->>'item_id'=$3) \
         OR (entity='imported_turn' AND body->>'repository'=$4 AND body->>'issue_number'=$3)",
    )
    .bind(task)
    .bind(repository_id.to_string())
    .bind(number.to_string())
    .bind(repository)
    .fetch_all(database.pool())
    .await?;
    Ok(rows
        .into_iter()
        .map(|(entity, Json(body))| (entity, body))
        .collect())
}

/// Find an enrolled task by its source issue identity.
pub async fn issue_task(
    database: &Database,
    repository_id: i64,
    number: i64,
) -> Result<Option<ModelRow>, Error> {
    let row: Option<(String, String, i64, Json<Value>)> = sqlx::query_as(
        "SELECT entity,entity_id,revision,body FROM backoffice.model_items \
         WHERE entity IN ('contribution_task','standalone_review') \
         AND body->>'repository_id'=$1 AND body->>'issue_number'=$2 LIMIT 1",
    )
    .bind(repository_id.to_string())
    .bind(number.to_string())
    .fetch_optional(database.pool())
    .await?;
    Ok(
        row.map(|(entity, entity_id, revision, Json(body))| ModelRow {
            entity,
            entity_id,
            revision,
            body,
        }),
    )
}

/// Read consent history for one source issue.
pub async fn issue_consent(
    database: &Database,
    repository_id: i64,
    number: i64,
) -> Result<Vec<(String, Value)>, Error> {
    let rows: Vec<(String, Json<Value>)> = sqlx::query_as(
        "SELECT entity,body FROM backoffice.model_items \
         WHERE entity IN ('consent','consent_removal','consent_state') \
         AND body->>'repository_id'=$1 AND body->>'issue_number'=$2 \
         ORDER BY entity,entity_id",
    )
    .bind(repository_id.to_string())
    .bind(number.to_string())
    .fetch_all(database.pool())
    .await?;
    Ok(rows
        .into_iter()
        .map(|(entity, Json(body))| (entity, body))
        .collect())
}

/// Read imported turn summaries for an issue without exposing archive bodies.
pub async fn imported_turns(
    database: &Database,
    repository: &str,
    number: i64,
) -> Result<Vec<Value>, Error> {
    let rows: Vec<(Json<Value>,)> = sqlx::query_as(
        "SELECT body FROM backoffice.model_items WHERE entity='imported_turn' \
         AND body->>'repository'=$1 AND body->>'issue_number'=$2 ORDER BY entity_id",
    )
    .bind(repository)
    .bind(number.to_string())
    .fetch_all(database.pool())
    .await?;
    Ok(rows.into_iter().map(|(Json(body),)| body).collect())
}

/// Read one imported turn before its fresh writer entitlement check.
pub async fn imported_turn(database: &Database, id: &str) -> Result<Option<Value>, Error> {
    let row: Option<(Json<Value>,)> = sqlx::query_as(
        "SELECT body FROM backoffice.model_items WHERE entity='imported_turn' AND entity_id=$1",
    )
    .bind(id)
    .fetch_optional(database.pool())
    .await?;
    Ok(row.map(|(Json(body),)| body))
}

/// Check that the requested diagnostic turn belongs to this task.
pub async fn turn_owned(database: &Database, task: &str, turn: &str) -> Result<bool, Error> {
    sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM backoffice.model_items WHERE entity='turn' \
         AND entity_id=$1 AND (body->>'task_id'=$2 OR body->>'standalone_task_id'=$2))",
    )
    .bind(turn)
    .bind(task)
    .fetch_one(database.pool())
    .await
}

/// Generic projection state; buffered events describe this database, not broker backlog.
#[derive(Serialize)]
pub struct ProjectionHealth {
    /// A complete source baseline has been installed.
    pub ready: bool,
    /// Browser change revision.
    pub revision: i64,
    /// Received events still awaiting projection application.
    pub buffered_events: i64,
    /// Latest received event time, absent before the first event.
    pub last_event_at: Option<DateTime<Utc>>,
}

/// Read generic projection health, including the loading state.
pub async fn projection_health(database: &Database) -> Result<ProjectionHealth, Error> {
    let (ready, revision, buffered_events, last_event_at) = sqlx::query_as(
        "SELECT ready,revision,(SELECT count(*) FROM backoffice.model_events WHERE NOT applied), \
         (SELECT max(received_at) FROM backoffice.model_events) \
         FROM backoffice.model_state WHERE singleton",
    )
    .fetch_one(database.pool())
    .await?;
    Ok(ProjectionHealth {
        ready,
        revision,
        buffered_events,
        last_event_at,
    })
}

/// A task and its projected turn-state counts, filtered for viewer access by the API.
pub struct OperationalTask {
    /// Task identity and state used by the existing entitlement path.
    pub task: ModelRow,
    /// Projected unfinished states and unknown terminal outcomes.
    pub turns: BTreeMap<String, i64>,
}

/// Read task operations without loading prompts, results or provider credentials.
pub async fn operational_tasks(database: &Database) -> Result<Vec<OperationalTask>, Error> {
    /// Storage tuple for one task and its aggregated unfinished turns.
    type Row = (
        String,
        String,
        i64,
        Json<Value>,
        Json<BTreeMap<String, i64>>,
    );
    let rows: Vec<Row> = sqlx::query_as(
        "SELECT t.entity,t.entity_id,t.revision,t.body, \
         COALESCE((SELECT jsonb_object_agg(status,n) FROM ( \
           SELECT status,count(*) n FROM ( \
             SELECT CASE \
               WHEN jsonb_typeof(a.body->'finished')='object' THEN \
                 CASE WHEN a.body#>>'{finished,outcome}'='unknown' THEN 'unknown_outcome' END \
               WHEN a.body->>'status'='unknown' THEN 'unknown_outcome' \
               WHEN a.body->>'status'='finished' AND \
                 a.body#>>'{result,outcome}' IN ('uncertain','lost','timed_out') THEN 'unknown_outcome' \
               WHEN COALESCE(a.body->>'status','pending') IN ('pending','ready','sending','queued','running') \
                 THEN COALESCE(a.body->>'status','pending') \
             END status FROM backoffice.model_items a WHERE a.entity='turn' \
             AND (a.body->>'task_id'=t.entity_id OR a.body->>'standalone_task_id'=t.entity_id) \
           ) observations WHERE status IS NOT NULL GROUP BY status \
         ) counts),'{}'::jsonb) \
         FROM backoffice.model_items t WHERE t.entity IN ('contribution_task','standalone_review') \
         ORDER BY t.entity,t.entity_id",
    ).fetch_all(database.pool()).await?;
    Ok(rows
        .into_iter()
        .map(
            |(entity, entity_id, revision, Json(body), Json(turns))| OperationalTask {
                task: ModelRow {
                    entity,
                    entity_id,
                    revision,
                    body,
                },
                turns,
            },
        )
        .collect())
}
