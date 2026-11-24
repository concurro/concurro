//! Transactional transcript rows and original gzip upload intents.

use anyhow::{Context, Result, ensure};
use base64::Engine;
use concurro_db::{Database, UnitOfWork};
use concurro_tasks_diagnostics::ArchiveSnapshot;
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

use concurro_exchange_v1::TurnSubmission;
use concurro_tasks_core::{AttemptSpec, python_digest};
use sha2::{Digest, Sha256};

use crate::execution_owner::lock_execution_owner;

/// Columns read by `retained_preparation`.
type RetainedPreparationPriorRow = (i64, String, String, Option<String>, Option<Vec<u8>>);

/// Columns read by `retained_eligibility`.
#[derive(sqlx::FromRow)]
struct RetainedArchiveRow {
    /// Persisted session turn.
    session_turn: i64,
    /// Persisted gzip sha256.
    gzip_sha256: Option<String>,
    /// Persisted archive error.
    archive_error: Option<String>,
    /// Persisted journal.
    journal: Option<Json<Value>>,
    /// Persisted upload status.
    upload_status: Option<String>,
    /// Persisted has pending bytes.
    has_pending_bytes: bool,
    /// Persisted tar sha256.
    tar_sha256: Option<String>,
    /// Persisted retained file bytes.
    retained_file_bytes: Option<i64>,
}

/// Inputs shared by `retained_eligibility`.
pub struct RetainedInput<'a> {
    /// Stable retained-session identity for this task and model role.
    pub scope: &'a str,
    /// Zero-based turn number in the retained session.
    pub ordinal: i64,
    /// Canonical digest of the exact model-facing request.
    pub request_digest: &'a str,
    /// Digest of the required completed predecessor, if this is a continuation.
    pub predecessor_digest: Option<&'a str>,
    /// Configured limit on the predecessor journal, when present.
    pub max_retained_bytes: Option<u64>,
    /// Exact instructions and schema used to verify journal compatibility.
    pub prompt: &'a Value,
}

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

/// Exact predecessor and source gzip needed before a paid continuation.
pub struct RetainedPreparation {
    /// Next session turn ordinal.
    pub turn: i64,
    /// Prior immutable prompt digest, when continuing.
    pub predecessor_digest: Option<String>,
    /// Original gzip digest of the required prior turn.
    pub gzip_sha256: Option<String>,
    /// Original gzip bytes still pending in PostgreSQL.
    pub pending_bytes: Option<Vec<u8>>,
}

/// Transactional outcome of validating one frozen runner session request.
pub(crate) enum RetainedCheck {
    /// The operation has no journal, as for intake or fresh review.
    Unscoped,
    /// A new or continuing turn may be funded.
    Eligible {
        /// Exact role journal scope.
        scope: String,
        /// Zero-based session turn.
        turn: i64,
        /// Canonical immutable prompt digest.
        digest: String,
    },
    /// The exact cached response already belongs to an earlier attempt.
    Replay,
    /// The required predecessor cannot safely seed a new call.
    Refused(&'static str),
}

/// Validate journal continuity and the supplied original gzip inside admission.
pub(crate) async fn check_retained_attempt(
    uow: &mut UnitOfWork<'_>,
    spec: &AttemptSpec,
) -> Result<RetainedCheck> {
    let Some(prompt_body) = spec.prompt_body.as_deref() else {
        return Ok(RetainedCheck::Unscoped);
    };
    let submission: TurnSubmission = serde_json::from_slice(&spec.submit_body)?;
    let session = &submission.payload["request"]["session"];
    if session.is_null() {
        return Ok(RetainedCheck::Unscoped);
    }
    let Some(scope) = session["scope"].as_str() else {
        return Ok(RetainedCheck::Refused("retained_scope_invalid"));
    };
    let Some(turn) = session["turn"].as_i64().filter(|turn| *turn >= 0) else {
        return Ok(RetainedCheck::Refused("retained_turn_invalid"));
    };
    let prompt: Value = serde_json::from_slice(prompt_body)?;
    let digest = python_digest(&prompt)?;
    let max_bytes = submission.payload["request"]["max_retained_bytes"].as_u64();
    let eligibility = retained_eligibility(
        uow,
        spec.task.0,
        RetainedInput {
            scope,
            ordinal: turn,
            request_digest: &digest,
            predecessor_digest: session["predecessor_digest"].as_str(),
            max_retained_bytes: max_bytes,
            prompt: &prompt,
        },
    )
    .await?;
    match eligibility {
        RetainedEligibility::Fresh => {
            if !submission.payload["retained"].is_null() {
                return Ok(RetainedCheck::Refused("unexpected_retained_bytes"));
            }
        }
        RetainedEligibility::Continue { gzip_sha256 } => {
            if session["gzip_sha256"] != gzip_sha256 {
                return Ok(RetainedCheck::Refused("retained_digest_changed"));
            }
            let Some(encoded) = submission.payload["retained"].as_str() else {
                return Ok(RetainedCheck::Refused("retained_bytes_missing"));
            };
            let Ok(bytes) = base64::engine::general_purpose::STANDARD.decode(encoded) else {
                return Ok(RetainedCheck::Refused("retained_bytes_invalid"));
            };
            if hex::encode(Sha256::digest(bytes)) != gzip_sha256 {
                return Ok(RetainedCheck::Refused("retained_digest_mismatch"));
            }
        }
        RetainedEligibility::Replay { .. } => return Ok(RetainedCheck::Replay),
        RetainedEligibility::Unavailable(reason) => return Ok(RetainedCheck::Refused(reason)),
    }
    Ok(RetainedCheck::Eligible {
        scope: scope.to_owned(),
        turn,
        digest,
    })
}

/// Select the latest attempt in one role scope; never fall back past it.
pub async fn retained_preparation(
    database: &Database,
    task: Uuid,
    scope: &str,
) -> Result<RetainedPreparation> {
    let prior: Option<RetainedPreparationPriorRow> = sqlx::query_as(
        "SELECT a.retained_turn,a.retained_request,a.status,ar.gzip_sha256,u.gzip_bytes \
         FROM tasks.attempts a \
         LEFT JOIN tasks.turn_archives ar ON ar.turn_id=a.id \
         LEFT JOIN tasks.archive_uploads u ON u.gzip_sha256=ar.gzip_sha256 \
         WHERE a.task_id=$1 AND a.retained_scope=$2 \
         ORDER BY a.retained_turn DESC,a.id DESC LIMIT 1",
    )
    .bind(task)
    .bind(scope)
    .fetch_optional(database.pool())
    .await?;
    let Some((turn, predecessor_digest, status, gzip_sha256, pending_bytes)) = prior else {
        return Ok(RetainedPreparation {
            turn: 0,
            predecessor_digest: None,
            gzip_sha256: None,
            pending_bytes: None,
        });
    };
    ensure!(status == "finished", "prior retained turn did not finish");
    Ok(RetainedPreparation {
        turn: turn + 1,
        predecessor_digest: Some(predecessor_digest),
        gzip_sha256,
        pending_bytes,
    })
}

/// Check the exact session revision before funding a retained continuation.
pub async fn retained_eligibility(
    uow: &mut UnitOfWork<'_>,
    task_id: Uuid,
    inputs: RetainedInput<'_>,
) -> Result<RetainedEligibility> {
    let RetainedInput {
        scope,
        ordinal,
        request_digest,
        predecessor_digest,
        max_retained_bytes,
        prompt,
    } = inputs;
    ensure!(ordinal >= 0, "negative session ordinal");
    let rows: Vec<RetainedArchiveRow> = sqlx::query_as(
        "SELECT ar.session_turn, ar.gzip_sha256 AS gzip_sha256, ar.archive_error, ar.journal, u.status AS upload_status, u.gzip_bytes IS NOT NULL AS has_pending_bytes, ar.tar_sha256 AS tar_sha256, ar.retained_file_bytes \
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
    let Some(RetainedArchiveRow {
        session_turn: prior_turn,
        gzip_sha256: gzip_sha,
        archive_error,
        journal,
        upload_status,
        has_pending_bytes: pending_bytes,
        tar_sha256: tar_sha,
        retained_file_bytes: retained_bytes,
    }) = rows.first()
    else {
        return Ok(if ordinal == 0 {
            RetainedEligibility::Fresh
        } else {
            RetainedEligibility::Unavailable("retained_revision_missing")
        });
    };
    if rows
        .iter()
        .filter(|row| row.session_turn == *prior_turn)
        .count()
        != 1
    {
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

/// Store one original gzip and its searchable transcript through the shared upload path.
pub async fn store_archive_snapshot(
    uow: &mut UnitOfWork<'_>,
    snapshot: &ArchiveSnapshot,
) -> Result<()> {
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
    Ok(())
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
        store_archive_snapshot(uow, snapshot).await?;
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
    let (repository_id, issue_number) = lock_execution_owner(uow, turn)
        .await?
        .context("archive turn has no execution owner")?
        .conversation();
    let inserted: Option<Uuid> = sqlx::query_scalar(
        "INSERT INTO tasks.turn_archives \
         (turn_id,repository_id,issue_number,gzip_sha256,tar_sha256,retained_file_bytes, \
          session_scope,session_turn,archive_error,journal) \
         VALUES ($1,$9,$10,$2,$3,$4,$5,$6,$7,$8) \
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
    .bind(repository_id)
    .bind(issue_number)
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
