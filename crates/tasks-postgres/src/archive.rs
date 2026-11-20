//! Transactional transcript rows and original gzip upload intents.

use anyhow::{Result, ensure};
use concurro_db::{Database, UnitOfWork};
use concurro_tasks_diagnostics::ArchiveSnapshot;
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

/// Whether one exact retained session turn can start without losing journal history.
#[derive(Debug, PartialEq)]
pub enum RetainedEligibility {
    /// No prior journal is required for a new session.
    Fresh,
    /// The exact completed predecessor can seed the next turn.
    Continue { gzip_sha256: String },
    /// The same turn and request already completed; reuse its response.
    Replay { response: Value },
    /// The required revision is missing, corrupt or inconsistent.
    Unavailable(&'static str),
}

/// Check the exact session revision before funding a retained continuation.
pub async fn retained_eligibility(
    uow: &mut UnitOfWork<'_>,
    task_id: Uuid,
    scope: &str,
    ordinal: i64,
    request_digest: &str,
    predecessor_digest: Option<&str>,
    max_retained_bytes: Option<u64>,
    prompt: &Value,
) -> Result<RetainedEligibility> {
    ensure!(ordinal >= 0, "negative session ordinal");
    let rows: Vec<(
        i64,
        Option<String>,
        Option<String>,
        Option<Json<Value>>,
        Option<String>,
        bool,
        Option<String>,
        Option<i64>,
    )> = sqlx::query_as(
        "SELECT ar.session_turn,ar.gzip_sha256,ar.archive_error,ar.journal, \
             u.status,u.gzip_bytes IS NOT NULL,ar.tar_sha256,ar.retained_file_bytes \
             FROM tasks.turn_archives ar \
             JOIN tasks.attempts a ON a.id=ar.turn_id \
             LEFT JOIN tasks.archive_uploads u ON u.gzip_sha256=ar.gzip_sha256 \
             WHERE a.task_id=$1 AND ar.session_scope=$2 \
             AND ar.session_turn IN ($3,$4) \
             ORDER BY ar.session_turn DESC,ar.recorded_at DESC",
    )
    .bind(task_id)
    .bind(scope)
    .bind(ordinal)
    .bind(ordinal - 1)
    .fetch_all(uow.connection())
    .await?;
    let Some((
        prior_turn,
        gzip_sha,
        archive_error,
        journal,
        upload_status,
        pending_bytes,
        tar_sha,
        retained_bytes,
    )) = rows.first()
    else {
        return Ok(if ordinal == 0 {
            RetainedEligibility::Fresh
        } else {
            RetainedEligibility::Unavailable("retained_revision_missing")
        });
    };
    if rows.iter().filter(|row| row.0 == *prior_turn).count() != 1 {
        return Ok(RetainedEligibility::Unavailable(
            "retained_revision_ambiguous",
        ));
    }
    let Some(journal) = journal.as_ref().map(|value| &value.0) else {
        return Ok(RetainedEligibility::Unavailable("retained_journal_missing"));
    };
    if archive_error.is_some()
        || gzip_sha.is_none()
        || tar_sha.is_none()
        || retained_bytes.is_none()
        || !(*pending_bytes || upload_status.as_deref() == Some("verified"))
    {
        return Ok(RetainedEligibility::Unavailable(
            "retained_revision_unrestorable",
        ));
    }
    if max_retained_bytes
        .is_some_and(|limit| *retained_bytes.as_ref().expect("size checked") as u64 > limit)
    {
        return Ok(RetainedEligibility::Unavailable("retained_size_exceeded"));
    }
    if journal["scope"] != scope
        || journal["turn"] != *prior_turn
        || journal["status"] != "complete"
        || !journal["prompt"].is_object()
    {
        return Ok(RetainedEligibility::Unavailable(
            "retained_journal_mismatch",
        ));
    }
    if *prior_turn == ordinal {
        if journal["request"] != request_digest {
            return Ok(RetainedEligibility::Unavailable("retained_replay_mismatch"));
        }
        if journal.get("response").is_none_or(Value::is_null) {
            return Ok(RetainedEligibility::Unavailable(
                "retained_replay_missing_response",
            ));
        }
        return Ok(RetainedEligibility::Replay {
            response: journal["response"].clone(),
        });
    }
    if predecessor_digest != journal["request"].as_str() {
        return Ok(RetainedEligibility::Unavailable(
            "retained_predecessor_mismatch",
        ));
    }
    if journal["prompt"]["instructions"] != prompt["instructions"]
        || journal["prompt"]["response_schema"] != prompt["response_schema"]
    {
        return Ok(RetainedEligibility::Unavailable(
            "retained_instruction_mismatch",
        ));
    }
    Ok(RetainedEligibility::Continue {
        gzip_sha256: gzip_sha.as_ref().expect("restorable gzip checked").clone(),
    })
}

/// Record one terminal turn's source archive, search rows and upload in its result transaction.
pub(crate) async fn record_archive(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    result: &Value,
) -> Result<()> {
    let encoded = result
        .get("retained")
        .filter(|value| !value.is_null())
        .or_else(|| result.get("transcript").filter(|value| !value.is_null()));
    let (snapshot, decode_error) = match encoded {
        Some(Value::String(text)) => match ArchiveSnapshot::from_base64(text) {
            Ok(snapshot) => (Some(snapshot), None),
            Err(error) => (None, Some(error)),
        },
        Some(_) => (None, Some("invalid_base64")),
        None => (None, None),
    };
    if let Some(snapshot) = &snapshot {
        let inserted = sqlx::query(
            "INSERT INTO tasks.archive_uploads (gzip_sha256,gzip_bytes,byte_count,status) \
             VALUES ($1,$2,$3,'pending') ON CONFLICT (gzip_sha256) DO NOTHING",
        )
        .bind(&snapshot.gzip_sha256)
        .bind(&snapshot.gzip_bytes)
        .bind(i64::try_from(snapshot.gzip_bytes.len())?)
        .execute(uow.connection())
        .await?;
        if let Some(tar_sha) = &snapshot.tar_sha256 {
            for event in &snapshot.events {
                sqlx::query(
                    "INSERT INTO tasks.transcript_events \
                     (tar_sha256,member_ordinal,line_number,member_path,member_mtime,harness, \
                      event_kind,event_json,fallback_bytes,diagnostic) \
                     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT DO NOTHING",
                )
                .bind(tar_sha)
                .bind(i32::try_from(event.member_ordinal)?)
                .bind(i32::try_from(event.line_number)?)
                .bind(&event.member_path)
                .bind(i64::try_from(event.member_mtime)?)
                .bind(event.harness)
                .bind(event.kind.as_deref())
                .bind(event.json.as_ref().map(Json))
                .bind(event.fallback_bytes.as_deref())
                .bind(event.diagnostic)
                .execute(uow.connection())
                .await?;
                let mut start = 0;
                let mut part = 0;
                while start < event.search_text.len() {
                    let mut end = (start + 100_000).min(event.search_text.len());
                    while !event.search_text.is_char_boundary(end) {
                        end -= 1;
                    }
                    if end < event.search_text.len() {
                        if let Some(boundary) = event.search_text[start..end]
                            .rfind(char::is_whitespace)
                            .filter(|boundary| *boundary > 0)
                        {
                            end = start + boundary;
                        } else if let Some(boundary) =
                            event.search_text[end..].find(char::is_whitespace)
                        {
                            end += boundary;
                        } else {
                            end = event.search_text.len();
                        }
                    }
                    sqlx::query(
                        "INSERT INTO tasks.transcript_text_parts \
                         (tar_sha256,member_ordinal,line_number,part_index,search_text) \
                         VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
                    )
                    .bind(tar_sha)
                    .bind(i32::try_from(event.member_ordinal)?)
                    .bind(i32::try_from(event.line_number)?)
                    .bind(part)
                    .bind(&event.search_text[start..end])
                    .execute(uow.connection())
                    .await?;
                    start = end;
                    part += 1;
                }
            }
        }
        if inserted.rows_affected() == 1 {
            let _: i64 = sqlx::query_scalar(
                "SELECT (graphile_worker.add_job( \
                 identifier => 'upload_archive', payload => json_build_object('sha',$1::text), \
                 queue_name => 'archive-upload', max_attempts => 25, \
                 job_key => 'archive:' || $1::text || ':0')).id",
            )
            .bind(&snapshot.gzip_sha256)
            .fetch_one(uow.connection())
            .await?;
        }
    }
    let gzip_sha = snapshot.as_ref().map(|value| value.gzip_sha256.as_str());
    let tar_sha = snapshot
        .as_ref()
        .and_then(|value| value.tar_sha256.as_deref());
    let archive_error = decode_error.or_else(|| snapshot.as_ref().and_then(|value| value.error));
    let session_scope = snapshot
        .as_ref()
        .and_then(|value| value.journal.as_ref())
        .and_then(|value| value.get("scope"))
        .and_then(Value::as_str)
        .map(|scope| scope.replace('\0', "\\u0000"));
    let session_turn = snapshot
        .as_ref()
        .and_then(|value| value.journal.as_ref())
        .and_then(|value| value.get("turn"))
        .and_then(Value::as_i64)
        .filter(|turn| *turn >= 0);
    let inserted: Option<Uuid> = sqlx::query_scalar(
        "INSERT INTO tasks.turn_archives \
         (turn_id,repository_id,issue_number,gzip_sha256,tar_sha256,retained_file_bytes, \
          session_scope,session_turn,archive_error,journal) \
         SELECT $1,s.repository_id,s.issue_number,$2,$3,$4,$5,$6,$7,$8 \
         FROM tasks.attempts a JOIN tasks.task_state s ON s.task_id=a.task_id WHERE a.id=$1 \
         ON CONFLICT (turn_id) DO UPDATE SET turn_id=EXCLUDED.turn_id \
         WHERE tasks.turn_archives.repository_id=EXCLUDED.repository_id \
         AND tasks.turn_archives.issue_number=EXCLUDED.issue_number \
         AND tasks.turn_archives.session_scope IS NOT DISTINCT FROM EXCLUDED.session_scope \
         AND tasks.turn_archives.session_turn IS NOT DISTINCT FROM EXCLUDED.session_turn \
         AND tasks.turn_archives.gzip_sha256 IS NOT DISTINCT FROM EXCLUDED.gzip_sha256 \
         AND tasks.turn_archives.tar_sha256 IS NOT DISTINCT FROM EXCLUDED.tar_sha256 \
         AND tasks.turn_archives.retained_file_bytes IS NOT DISTINCT FROM EXCLUDED.retained_file_bytes \
         AND tasks.turn_archives.archive_error IS NOT DISTINCT FROM EXCLUDED.archive_error \
         RETURNING turn_id",
    )
    .bind(turn)
    .bind(gzip_sha)
    .bind(tar_sha)
    .bind(snapshot.as_ref().and_then(|value| value.retained_file_bytes).map(i64::try_from).transpose()?)
    .bind(session_scope.as_deref())
    .bind(session_turn)
    .bind(archive_error)
    .bind(snapshot.as_ref().and_then(|value| value.journal.as_ref()).map(|journal| {
        let mut projected = journal.clone();
        crate::effects::operational_json(&mut projected);
        Json(projected)
    }))
    .fetch_optional(uow.connection())
    .await?;
    ensure!(inserted == Some(turn), "turn archive changed on replay");
    Ok(())
}

/// Read original pending bytes and prior attempt count without holding an I/O lock.
pub async fn pending_archive(database: &Database, sha: &str) -> Result<Option<(Vec<u8>, i32)>> {
    let row: Option<(Option<Vec<u8>>, i32)> = sqlx::query_as(
        "SELECT gzip_bytes,upload_attempts FROM tasks.archive_uploads WHERE gzip_sha256=$1 \
         AND status IN ('pending','retrying')",
    )
    .bind(sha)
    .fetch_optional(database.pool())
    .await?;
    Ok(row.and_then(|(bytes, attempts)| bytes.map(|bytes| (bytes, attempts))))
}

/// Mark an upload verified only after the object was read back byte for byte.
pub async fn verify_archive(uow: &mut UnitOfWork<'_>, sha: &str) -> Result<bool> {
    let changed = sqlx::query(
        "UPDATE tasks.archive_uploads SET status='verified',gzip_bytes=NULL,last_error=NULL, \
         updated_at=now() WHERE gzip_sha256=$1 AND status IN ('pending','retrying')",
    )
    .bind(sha)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Count a failed upload and retain source bytes for operator recovery.
pub async fn fail_archive_upload(uow: &mut UnitOfWork<'_>, sha: &str, stage: &str) -> Result<bool> {
    let exhausted: Option<bool> = sqlx::query_scalar(
        "UPDATE tasks.archive_uploads SET upload_attempts=upload_attempts+1, \
         status=CASE WHEN upload_attempts+1>=25 THEN 'exhausted' ELSE 'retrying' END, \
         last_error=$2,updated_at=now() WHERE gzip_sha256=$1 \
         AND status IN ('pending','retrying') RETURNING status='exhausted'",
    )
    .bind(sha)
    .bind(stage)
    .fetch_optional(uow.connection())
    .await?;
    Ok(exhausted.unwrap_or(true))
}

/// Requeue an exhausted or stranded upload once, without racing an active job.
pub async fn retry_archive_upload(
    uow: &mut UnitOfWork<'_>,
    sha: &str,
    actor: &str,
) -> Result<bool> {
    ensure!(!actor.trim().is_empty(), "operator actor is empty");
    let eligible: Option<bool> = sqlx::query_scalar(
        "SELECT status IN ('pending','retrying','exhausted') AND gzip_bytes IS NOT NULL \
         FROM tasks.archive_uploads WHERE gzip_sha256=$1 FOR UPDATE",
    )
    .bind(sha)
    .fetch_optional(uow.connection())
    .await?;
    let active: bool = if eligible == Some(true) {
        sqlx::query_scalar(
            "SELECT EXISTS (SELECT 1 FROM graphile_worker.jobs j \
             WHERE j.key LIKE 'archive:' || $1::text || ':%' \
             AND (j.locked_at IS NOT NULL OR j.attempts < j.max_attempts))",
        )
        .bind(sha)
        .fetch_one(uow.connection())
        .await?
    } else {
        false
    };
    let generation: Option<i32> = if eligible == Some(true) && !active {
        sqlx::query_scalar(
            "UPDATE tasks.archive_uploads SET status='pending',last_error=NULL,upload_attempts=0, \
             retry_generation=retry_generation+1,updated_at=now() \
             WHERE gzip_sha256=$1 RETURNING retry_generation",
        )
        .bind(sha)
        .fetch_optional(uow.connection())
        .await?
    } else {
        None
    };
    if let Some(generation) = generation {
        let key = format!("archive:{sha}:{generation}");
        let _: i64 = sqlx::query_scalar(
            "SELECT (graphile_worker.add_job( \
             identifier => 'upload_archive',payload => json_build_object('sha',$1::text), \
             queue_name => 'archive-upload',max_attempts => 25,job_key => $2)).id",
        )
        .bind(sha)
        .bind(key)
        .fetch_one(uow.connection())
        .await?;
    }
    sqlx::query(
        "INSERT INTO tasks.operator_actions (actor,action,target,outcome) \
         VALUES ($1,'archives.retry',$2,$3)",
    )
    .bind(actor)
    .bind(sha)
    .bind(if generation.is_some() {
        "requeued"
    } else {
        "noop"
    })
    .execute(uow.connection())
    .await?;
    Ok(generation.is_some())
}
