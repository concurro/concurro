//! Stopped-Python source boundary and Exchange identity floors.

use anyhow::{Result, ensure};
use chrono::{DateTime, Utc};
use concurro_db::{Database, UnitOfWork};
use serde::Deserialize;
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

/// One issue's last Python generation and provider epochs.
#[derive(Clone, Copy, Debug, Deserialize, PartialEq)]
pub struct CutoverFloor {
    /// Last generation acknowledged by Exchange before Rust activation.
    pub generation: i64,
    /// Last OpenAI slot epoch, or minus one when no slot existed.
    pub openai_epoch: i64,
    /// Last Anthropic slot epoch, or minus one when no slot existed.
    pub anthropic_epoch: i64,
}

/// An issue's imported Exchange identity floor.
#[derive(Deserialize)]
struct IssueFloor {
    repository_id: i64,
    issue_number: i64,
    #[serde(flatten)]
    floor: CutoverFloor,
}

/// One stopped Python target and the last ingress sequence it acknowledged.
#[derive(Deserialize)]
struct TargetBoundary {
    repository_id: i64,
    activation_at: DateTime<Utc>,
    source_incarnation: String,
    source_sequence: i64,
}

/// A historical PR reserved against a new Rust owner and seeded into the index.
#[derive(Deserialize)]
struct HistoricalPr {
    upstream: String,
    pr_number: i64,
    document: Value,
    #[serde(default)]
    owned_contribution: bool,
}

/// A complete, restartable manifest from stopped Python and Exchange state.
#[derive(Deserialize)]
struct CutoverManifest {
    #[serde(default)]
    targets: Vec<TargetBoundary>,
    #[serde(default)]
    floors: Vec<IssueFloor>,
    #[serde(default)]
    pull_requests: Vec<HistoricalPr>,
}

/// Import the verified stopped-controller boundary without starting live work.
pub async fn import_cutover_manifest(database: &Database, manifest: &Value) -> Result<Value> {
    let source: CutoverManifest = serde_json::from_value(manifest.clone())?;
    let mut work = database.begin().await?;
    for target in &source.targets {
        ensure!(
            target.repository_id > 0
                && target.source_sequence >= 0
                && !target.source_incarnation.is_empty(),
            "invalid cutover target"
        );
        sqlx::query(
            "INSERT INTO tasks.cutover_targets \
             (repository_id,activation_at,source_incarnation,source_sequence) \
             VALUES ($1,$2,$3,$4) ON CONFLICT (repository_id) DO NOTHING",
        )
        .bind(target.repository_id)
        .bind(target.activation_at)
        .bind(&target.source_incarnation)
        .bind(target.source_sequence)
        .execute(work.connection())
        .await?;
        let existing: (DateTime<Utc>, String, i64) = sqlx::query_as(
            "SELECT activation_at,source_incarnation,source_sequence \
             FROM tasks.cutover_targets WHERE repository_id=$1",
        )
        .bind(target.repository_id)
        .fetch_one(work.connection())
        .await?;
        ensure!(
            existing
                == (
                    target.activation_at,
                    target.source_incarnation.clone(),
                    target.source_sequence
                ),
            "cutover target changed on replay"
        );
    }
    for item in &source.floors {
        ensure!(
            item.repository_id > 0
                && item.issue_number > 0
                && item.floor.generation >= 0
                && item.floor.openai_epoch >= -1
                && item.floor.anthropic_epoch >= -1,
            "invalid cutover floor"
        );
        sqlx::query(
            "INSERT INTO tasks.cutover_floors \
             (repository_id,issue_number,generation,openai_epoch,anthropic_epoch) \
             VALUES ($1,$2,$3,$4,$5) ON CONFLICT (repository_id,issue_number) DO NOTHING",
        )
        .bind(item.repository_id)
        .bind(item.issue_number)
        .bind(item.floor.generation)
        .bind(item.floor.openai_epoch)
        .bind(item.floor.anthropic_epoch)
        .execute(work.connection())
        .await?;
        let existing: (i64, i64, i64) = sqlx::query_as(
            "SELECT generation,openai_epoch,anthropic_epoch FROM tasks.cutover_floors \
             WHERE repository_id=$1 AND issue_number=$2",
        )
        .bind(item.repository_id)
        .bind(item.issue_number)
        .fetch_one(work.connection())
        .await?;
        ensure!(
            existing
                == (
                    item.floor.generation,
                    item.floor.openai_epoch,
                    item.floor.anthropic_epoch
                ),
            "cutover floor changed on replay"
        );
    }
    for pr in &source.pull_requests {
        ensure!(
            pr.pr_number > 0 && pr.upstream.split_once('/').is_some(),
            "invalid historical PR identity"
        );
        let upstream = pr.upstream.to_ascii_lowercase();
        sqlx::query(
            "INSERT INTO tasks.cutover_pr_index (upstream,pr_number,document) \
             VALUES ($1,$2,$3) ON CONFLICT (upstream,pr_number) DO NOTHING",
        )
        .bind(&upstream)
        .bind(pr.pr_number)
        .bind(Json(&pr.document))
        .execute(work.connection())
        .await?;
        let existing: Json<Value> = sqlx::query_scalar(
            "SELECT document FROM tasks.cutover_pr_index WHERE upstream=$1 AND pr_number=$2",
        )
        .bind(&upstream)
        .bind(pr.pr_number)
        .fetch_one(work.connection())
        .await?;
        ensure!(existing.0 == pr.document, "historical PR changed on replay");
        if !pr.owned_contribution {
            continue;
        }
        let owner_id = Uuid::new_v5(
            &Uuid::NAMESPACE_OID,
            format!("python-contribution-pr:{upstream}:{}", pr.pr_number).as_bytes(),
        );
        sqlx::query(
            "INSERT INTO tasks.pr_ownership (upstream,pr_number,owner_kind,owner_id) \
             VALUES ($1,$2,'python',$3) ON CONFLICT (upstream,pr_number) DO NOTHING",
        )
        .bind(&upstream)
        .bind(pr.pr_number)
        .bind(owner_id)
        .execute(work.connection())
        .await?;
        let owner: String = sqlx::query_scalar(
            "SELECT owner_kind FROM tasks.pr_ownership WHERE upstream=$1 AND pr_number=$2",
        )
        .bind(&upstream)
        .bind(pr.pr_number)
        .fetch_one(work.connection())
        .await?;
        ensure!(
            owner == "python",
            "historical PR already belongs to a Rust task"
        );
    }
    work.commit().await?;
    Ok(serde_json::json!({
        "targets":source.targets.len(),
        "floors":source.floors.len(),
        "pull_requests":source.pull_requests.len()
    }))
}

/// Return a Python floor while holding the issue's current transaction.
pub async fn cutover_floor(
    work: &mut UnitOfWork<'_>,
    repository_id: i64,
    issue_number: i64,
) -> Result<Option<CutoverFloor>, sqlx::Error> {
    let row: Option<(i64, i64, i64)> = sqlx::query_as(
        "SELECT generation,openai_epoch,anthropic_epoch FROM tasks.cutover_floors \
         WHERE repository_id=$1 AND issue_number=$2",
    )
    .bind(repository_id)
    .bind(issue_number)
    .fetch_optional(work.connection())
    .await?;
    Ok(
        row.map(|(generation, openai_epoch, anthropic_epoch)| CutoverFloor {
            generation,
            openai_epoch,
            anthropic_epoch,
        }),
    )
}

/// Admit only sources at or after a stopped Python target's activation time.
pub async fn source_after_activation(
    work: &mut UnitOfWork<'_>,
    repository_id: i64,
    source_at: DateTime<Utc>,
) -> Result<bool, sqlx::Error> {
    let activation: Option<DateTime<Utc>> = sqlx::query_scalar(
        "SELECT activation_at FROM tasks.cutover_targets WHERE repository_id=$1",
    )
    .bind(repository_id)
    .fetch_optional(work.connection())
    .await?;
    Ok(activation.is_none_or(|activation| source_at >= activation))
}
