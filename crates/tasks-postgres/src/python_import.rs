//! Restartable inventory and transcript import from a stopped Python deployment.

use std::{collections::HashMap, fs, path::Path};

use anyhow::{Context, Result, ensure};
use concurro_db::Database;
use concurro_tasks_diagnostics::ArchiveSnapshot;
use serde_json::{Value, json};
use sqlx::types::Json;

use crate::archive::store_archive_snapshot;

/// List this deployment's original archives still awaiting object-store verification.
pub async fn pending_python_import_archives(
    database: &Database,
    deployment: &str,
) -> Result<Vec<String>> {
    let rows: Vec<(String,)> = sqlx::query_as(
        "SELECT DISTINCT u.gzip_sha256 FROM tasks.archive_uploads u \
         JOIN tasks.python_import_sources s ON s.gzip_sha256=u.gzip_sha256 \
         WHERE s.deployment=$1 AND u.status IN ('pending','retrying')",
    )
    .bind(deployment)
    .fetch_all(database.pool())
    .await?;
    Ok(rows.into_iter().map(|(sha,)| sha).collect())
}

/// Verify and import every original file, turn record and retained session in one manifest.
pub async fn import_python_manifest(database: &Database, manifest: &Value) -> Result<Value> {
    let deployment = manifest["deployment"]
        .as_str()
        .filter(|value| !value.is_empty())
        .context("import deployment is missing")?;
    manifest["diagnostics_dir"]
        .as_str()
        .context("diagnostics directory is missing")?;
    let files = manifest["files"]
        .as_array()
        .context("file inventory is missing")?;
    let turns = manifest["turns"]
        .as_array()
        .context("turn inventory is missing")?;
    let mut known = HashMap::new();
    let mut uploaded = 0usize;
    let mut failed = 0usize;
    let mut fallbacks = 0usize;
    for file in files {
        let source_path = file["source_path"]
            .as_str()
            .context("source path is missing")?;
        let path = file["path"].as_str().context("file path is missing")?;
        let kind = file["kind"].as_str().context("file kind is missing")?;
        ensure!(
            matches!(kind, "diagnostics" | "retained"),
            "invalid file kind"
        );
        let bytes = fs::read(path);
        let (snapshot, status, diagnostic) = match bytes {
            Ok(bytes) => {
                let snapshot = ArchiveSnapshot::from_bytes(bytes);
                let expected_name = Path::new(path).file_name().and_then(|name| name.to_str());
                let invalid_name =
                    kind == "diagnostics" && expected_name != Some(snapshot.gzip_sha256.as_str());
                let scope_mismatch = kind == "retained"
                    && snapshot.journal.as_ref().is_some_and(|journal| {
                        journal["status"] == "complete"
                            && source_path
                                .rsplit('/')
                                .next()
                                .and_then(|name| name.strip_suffix(".tar.gz"))
                                .is_some_and(|scope| journal["scope"] != scope)
                    });
                let diagnostic = if invalid_name {
                    Some("filename_digest_mismatch")
                } else if scope_mismatch {
                    Some("journal_scope_mismatch")
                } else {
                    snapshot.error
                };
                let status = if diagnostic.is_none() {
                    "verified"
                } else {
                    "invalid"
                };
                (Some(snapshot), status, diagnostic)
            }
            Err(_) => (None, "missing", Some("source_file_missing")),
        };
        if status != "verified" {
            failed += 1;
        }
        let mut work = database.begin().await?;
        if let Some(snapshot) = snapshot.as_ref().filter(|_| status == "verified") {
            store_archive_snapshot(&mut work, snapshot).await?;
            uploaded += 1;
            fallbacks += snapshot
                .events
                .iter()
                .filter(|event| event.diagnostic.is_some())
                .count();
        }
        let digest = snapshot.as_ref().map(|value| value.gzip_sha256.as_str());
        let tar = snapshot
            .as_ref()
            .and_then(|value| value.tar_sha256.as_deref());
        let bytes = snapshot
            .as_ref()
            .map(|value| i64::try_from(value.gzip_bytes.len()))
            .transpose()?;
        let accepted: Option<String> = sqlx::query_scalar(
            "INSERT INTO tasks.python_import_sources \
             (deployment,source_path,kind,gzip_sha256,tar_sha256,byte_count,status,diagnostic) \
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (deployment,source_path) \
             DO UPDATE SET gzip_sha256=EXCLUDED.gzip_sha256,tar_sha256=EXCLUDED.tar_sha256,byte_count=EXCLUDED.byte_count, \
                status=EXCLUDED.status,diagnostic=EXCLUDED.diagnostic,recorded_at=now() \
             WHERE tasks.python_import_sources.gzip_sha256 IS NOT DISTINCT FROM EXCLUDED.gzip_sha256 \
                OR tasks.python_import_sources.gzip_sha256 IS NULL \
             RETURNING source_path",
        )
        .bind(deployment)
        .bind(source_path)
        .bind(kind)
        .bind(digest)
        .bind(tar)
        .bind(bytes)
        .bind(status)
        .bind(diagnostic)
        .fetch_optional(work.connection())
        .await?;
        ensure!(
            accepted.is_some(),
            "source file changed after import: {source_path}"
        );
        if kind == "retained" {
            let relative = source_path
                .strip_prefix("retained/")
                .context("retained path is invalid")?;
            let (repository_dir, filename) = relative
                .rsplit_once('/')
                .context("retained path has no repository")?;
            let scope = filename
                .strip_suffix(".tar.gz")
                .context("retained path has no scope")?;
            let repository = repository_dir.replace("__", "/");
            let completed = snapshot.as_ref().and_then(|value| {
                let journal = value.journal.as_ref()?;
                (journal["status"] == "complete" && journal["scope"] == scope)
                    .then(|| journal["turn"].as_i64().filter(|value| *value >= 0))
                    .flatten()
            });
            sqlx::query(
                "INSERT INTO tasks.python_import_sessions \
                 (deployment,repository,session_scope,source_path,gzip_sha256,completed_turn,status,diagnostic) \
                 VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (deployment,repository,session_scope) \
                 DO UPDATE SET gzip_sha256=EXCLUDED.gzip_sha256,completed_turn=EXCLUDED.completed_turn, \
                    status=EXCLUDED.status,diagnostic=EXCLUDED.diagnostic \
                 WHERE tasks.python_import_sessions.source_path=EXCLUDED.source_path",
            )
            .bind(deployment)
            .bind(repository)
            .bind(scope)
            .bind(source_path)
            .bind(digest)
            .bind(completed)
            .bind(status)
            .bind(diagnostic)
            .execute(work.connection())
            .await?;
        }
        work.commit().await?;
        if kind == "diagnostics" {
            known.insert(
                source_path.to_owned(),
                (
                    digest.map(str::to_owned),
                    tar.map(str::to_owned),
                    bytes,
                    status,
                    snapshot.as_ref().and_then(|value| value.journal.clone()),
                ),
            );
        }
    }
    let mut incomplete_turns = 0usize;
    for turn in turns {
        let source_id = turn["id"].as_str().context("turn ID is missing")?;
        let repository = turn["repository"]
            .as_str()
            .context("turn repository is missing")?;
        let issue = turn["issue"].as_i64().context("turn issue is missing")?;
        let stage = turn["stage"].as_str().context("turn stage is missing")?;
        let status = turn["status"].as_str().context("turn status is missing")?;
        let requested_turn = turn["session_turn"].as_i64();
        let scope = turn["session_scope"].as_str();
        let transcript = turn["transcript"].as_str();
        let valid_transcript = transcript
            .filter(|sha| sha.len() == 64 && sha.bytes().all(|byte| byte.is_ascii_hexdigit()));
        let source_path = valid_transcript.map(|sha| format!("diagnostics/{}/{}", &sha[..2], sha));
        let file = source_path.as_deref().and_then(|path| known.get(path));
        let expected_size = turn["transcript_bytes"].as_i64();
        let digest_matches =
            transcript
                .zip(file)
                .is_some_and(|(expected, (actual, _, size, state, journal))| {
                    actual.as_deref() == Some(expected)
                        && *state == "verified"
                        && expected_size.is_none_or(|expected| *size == Some(expected))
                        && journal.as_ref().is_none_or(|journal| {
                            journal["status"] != "complete"
                                || scope.is_none_or(|scope| journal["scope"] == scope)
                        })
                });
        let import_status = if transcript.is_none() {
            "no_archive"
        } else if digest_matches {
            "verified"
        } else {
            incomplete_turns += 1;
            "incomplete"
        };
        let diagnostic =
            (import_status == "incomplete").then_some("archive_missing_or_inconsistent");
        let runner = turn["runner"]
            .as_str()
            .and_then(|value| serde_json::from_str::<Value>(value).ok());
        let staffing = turn["runner_sessions"]
            .as_str()
            .and_then(|value| serde_json::from_str::<Value>(value).ok())
            .filter(Value::is_array)
            .unwrap_or_else(|| json!([]));
        let completed_turn: Option<i64> = if digest_matches {
            file.and_then(|(_, _, _, _, journal)| journal.as_ref())
                .and_then(|journal| {
                    (journal["status"] == "complete"
                        && scope.is_some_and(|scope| journal["scope"] == scope))
                    .then(|| journal["turn"].as_i64().filter(|value| *value >= 0))
                    .flatten()
                })
        } else {
            None
        };
        let mut work = database.begin().await?;
        let accepted: Option<String> = sqlx::query_scalar(
            "INSERT INTO tasks.python_import_turns \
             (deployment,source_id,repository,issue_number,generation,stage,status, \
              session_scope,requested_turn,completed_turn,serving_session,serving_run, \
              staffing,source_record,gzip_sha256,tar_sha256,import_status,diagnostic) \
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18) \
             ON CONFLICT (deployment,source_id) DO UPDATE SET \
                completed_turn=EXCLUDED.completed_turn,gzip_sha256=EXCLUDED.gzip_sha256, \
                tar_sha256=EXCLUDED.tar_sha256, \
                import_status=EXCLUDED.import_status,diagnostic=EXCLUDED.diagnostic \
             WHERE tasks.python_import_turns.source_record=EXCLUDED.source_record \
             RETURNING source_id",
        )
        .bind(deployment)
        .bind(source_id)
        .bind(repository)
        .bind(issue)
        .bind(turn["generation"].as_i64())
        .bind(stage)
        .bind(status)
        .bind(scope)
        .bind(requested_turn)
        .bind(completed_turn)
        .bind(runner.as_ref().and_then(|value| value["session"].as_str()))
        .bind(runner.as_ref().and_then(|value| value["run_id"].as_str()))
        .bind(Json(&staffing))
        .bind(Json(turn))
        .bind(transcript.filter(|_| digest_matches))
        .bind(
            file.and_then(|(_, tar, _, _, _)| tar.as_deref())
                .filter(|_| digest_matches),
        )
        .bind(import_status)
        .bind(diagnostic)
        .fetch_optional(work.connection())
        .await?;
        ensure!(
            accepted.is_some(),
            "source turn changed after import: {source_id}"
        );
        work.commit().await?;
    }
    Ok(
        json!({"deployment":deployment,"files":files.len(),"turns":turns.len(),
        "archives_queued":uploaded,"invalid_files":failed,"incomplete_turns":incomplete_turns,
        "fallback_lines":fallbacks}),
    )
}
