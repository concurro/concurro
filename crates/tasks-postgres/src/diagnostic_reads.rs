//! Turn-scoped diagnostic reads for the private task API.

use base64::{Engine, engine::general_purpose::STANDARD};
use concurro_db::Database;
use serde::Serialize;
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

/// Columns read by `transcript_for_tar`.
#[derive(sqlx::FromRow)]
struct TranscriptEventRow {
    /// Persisted position.
    position: i64,
    /// Persisted member path.
    member_path: String,
    /// Persisted line number.
    line_number: i32,
    /// Persisted harness.
    harness: String,
    /// Persisted event kind.
    event_kind: Option<String>,
    /// Persisted event json.
    event_json: Option<Json<Value>>,
    /// Persisted fallback bytes.
    fallback_bytes: Option<Vec<u8>>,
    /// Persisted diagnostic.
    diagnostic: Option<String>,
}

/// One preserved transcript line in its source order.
#[derive(Serialize)]
pub struct TranscriptLine {
    /// One-based position within the turn's archive.
    pub position: i64,
    /// Original archive member path.
    pub member_path: String,
    /// Original line number in that member.
    pub line_number: i32,
    /// Harness family recorded for the line.
    pub harness: String,
    /// Original event kind when present.
    pub kind: Option<String>,
    /// Queryable JSON value when PostgreSQL can represent it.
    pub json: Option<Value>,
    /// Exact source bytes for a malformed or unrepresentable line.
    pub fallback_base64: Option<String>,
    /// Fixed fallback diagnostic when present.
    pub diagnostic: Option<String>,
}

/// Read one immutable transcript page for a recorded turn.
pub async fn transcript_page(
    database: &Database,
    turn: Uuid,
    after: i64,
    limit: i64,
) -> Result<Option<Vec<TranscriptLine>>, sqlx::Error> {
    let archive: Option<Option<String>> =
        sqlx::query_scalar("SELECT tar_sha256 FROM tasks.turn_archives WHERE turn_id=$1")
            .bind(turn)
            .fetch_optional(database.pool())
            .await?;
    let Some(Some(tar)) = archive else {
        return Ok(None);
    };
    Ok(Some(
        transcript_for_tar(database, &tar, after, limit).await?,
    ))
}

/// Read a verified Python turn's original transcript through its source identity.
pub async fn imported_transcript_page(
    database: &Database,
    deployment: &str,
    source_id: &str,
    after: i64,
    limit: i64,
) -> Result<Option<Vec<TranscriptLine>>, sqlx::Error> {
    let tar: Option<String> = sqlx::query_scalar(
        "SELECT t.tar_sha256 FROM tasks.python_import_turns t \
         JOIN tasks.archive_uploads u ON u.gzip_sha256=t.gzip_sha256 \
         WHERE t.deployment=$1 AND t.source_id=$2 AND t.import_status='verified' \
         AND u.status='verified'",
    )
    .bind(deployment)
    .bind(source_id)
    .fetch_optional(database.pool())
    .await?
    .flatten();
    let Some(tar) = tar else { return Ok(None) };
    Ok(Some(
        transcript_for_tar(database, &tar, after, limit).await?,
    ))
}

/// Page shared immutable transcript rows by their uncompressed tar digest.
async fn transcript_for_tar(
    database: &Database,
    tar: &str,
    after: i64,
    limit: i64,
) -> Result<Vec<TranscriptLine>, sqlx::Error> {
    let rows: Vec<TranscriptEventRow> = sqlx::query_as(
        "SELECT position, member_path, line_number, harness, event_kind, event_json, fallback_bytes, diagnostic \
         FROM ( \
             SELECT row_number() OVER (ORDER BY member_mtime,member_path COLLATE \"C\", \
                 member_ordinal,line_number) position, \
                 member_path,line_number,harness,event_kind,event_json,fallback_bytes,diagnostic \
             FROM tasks.transcript_events WHERE tar_sha256=$1) lines \
         WHERE position>$2 ORDER BY position LIMIT $3",
    )
    .bind(tar)
    .bind(after)
    .bind(limit)
    .fetch_all(database.pool())
    .await?;
    Ok(rows
        .into_iter()
        .map(
            |TranscriptEventRow {
                 position,
                 member_path,
                 line_number,
                 harness,
                 event_kind: kind,
                 event_json: json,
                 fallback_bytes: fallback,
                 diagnostic,
             }| {
                TranscriptLine {
                    position,
                    member_path,
                    line_number,
                    harness,
                    kind,
                    json: json.map(|Json(value)| value),
                    fallback_base64: fallback.map(|bytes| STANDARD.encode(bytes)),
                    diagnostic,
                }
            },
        )
        .collect())
}

/// Fetch an artifact only through its recorded turn link.
pub async fn turn_artifact(
    database: &Database,
    turn: Uuid,
    digest: &str,
) -> Result<Option<(String, Vec<u8>)>, sqlx::Error> {
    sqlx::query_as(
        "SELECT a.media_type,a.body FROM tasks.turn_artifacts a \
         JOIN tasks.turn_artifact_links l ON l.sha256=a.sha256 \
         WHERE l.turn_id=$1 AND a.sha256=$2",
    )
    .bind(turn)
    .bind(digest)
    .fetch_optional(database.pool())
    .await
}
