//! Durable identities and uncertain-create fences for standalone review checks.

use chrono::{DateTime, Utc};
use concurro_db::{Database, UnitOfWork};
use uuid::Uuid;

use crate::StandaloneReviewRound;

/// Columns read by `standalone_review_check`.
#[derive(sqlx::FromRow)]
struct StandaloneCheckRow {
    /// Persisted head.
    head: String,
    /// Persisted external id.
    external_id: String,
    /// Persisted run id.
    run_id: Option<i64>,
    /// Persisted create state.
    create_state: String,
    /// Persisted create misses.
    create_misses: i32,
    /// Persisted recheck until.
    recheck_until: Option<DateTime<Utc>>,
    /// Persisted sent digest.
    sent_digest: Option<String>,
    /// Persisted completed.
    completed: bool,
}

/// One App-owned check run correlated to a frozen standalone round.
pub struct StandaloneReviewCheck {
    /// Standalone task identity.
    pub task_id: Uuid,
    /// Frozen round number.
    pub round: i32,
    /// Display attempt, incremented when a completed check is restarted.
    pub attempt: i32,
    /// Pinned PR head.
    pub head: String,
    /// Correlation value, never an idempotency key.
    pub external_id: String,
    /// Adopted GitHub run identity.
    pub run_id: Option<i64>,
    /// Create fence.
    pub create_state: String,
    /// Readbacks that missed an uncertain create.
    pub create_misses: i32,
    /// Period for detecting a late duplicate run.
    pub recheck_until: Option<DateTime<Utc>>,
    /// Last desired state confirmed by a successful update.
    pub sent_digest: Option<String>,
    /// Whether the last desired state was terminal.
    pub completed: bool,
}

/// Create or read the current round's check without losing a prior send fence.
pub async fn standalone_review_check(
    database: &Database,
    round: &StandaloneReviewRound,
) -> anyhow::Result<StandaloneReviewCheck> {
    let mut work = database.begin().await?;
    let latest: Option<(i32, bool)> = sqlx::query_as(
        "SELECT attempt,completed FROM tasks.standalone_review_checks \
         WHERE task_id=$1 AND round=$2 ORDER BY attempt DESC LIMIT 1 FOR UPDATE",
    )
    .bind(round.task_id)
    .bind(round.number)
    .fetch_optional(work.connection())
    .await?;
    let attempt = match latest {
        Some((previous, true))
            if matches!(round.stage.as_str(), "queued" | "review" | "publishing") =>
        {
            previous + 1
        }
        Some((previous, _)) => previous,
        None => 1,
    };
    let head = round.snapshot["head"]
        .as_str()
        .ok_or_else(|| anyhow::anyhow!("standalone review check head missing"))?;
    let external_id = format!(
        "concurro:{}#{}:{}:{}",
        round.upstream, round.pr_number, round.number, attempt
    );
    sqlx::query(
        "INSERT INTO tasks.standalone_review_checks \
         (task_id,round,attempt,head,external_id) VALUES ($1,$2,$3,$4,$5) \
         ON CONFLICT DO NOTHING",
    )
    .bind(round.task_id)
    .bind(round.number)
    .bind(attempt)
    .bind(head)
    .bind(&external_id)
    .execute(work.connection())
    .await?;
    let row: StandaloneCheckRow = sqlx::query_as(
        "SELECT head, external_id, run_id, create_state, create_misses, recheck_until, sent_digest, completed \
         FROM tasks.standalone_review_checks \
         WHERE task_id=$1 AND round=$2 AND attempt=$3",
    )
    .bind(round.task_id)
    .bind(round.number)
    .bind(attempt)
    .fetch_one(work.connection())
    .await?;
    work.commit().await?;
    anyhow::ensure!(
        row.head == head && row.external_id == external_id,
        "standalone check correlation changed"
    );
    Ok(StandaloneReviewCheck {
        task_id: round.task_id,
        round: round.number,
        attempt,
        head: row.head,
        external_id: row.external_id,
        run_id: row.run_id,
        create_state: row.create_state,
        create_misses: row.create_misses,
        recheck_until: row.recheck_until,
        sent_digest: row.sent_digest,
        completed: row.completed,
    })
}

/// Fence a new check creation before GitHub receives it.
pub async fn mark_standalone_check_sent(
    uow: &mut UnitOfWork<'_>,
    check: &StandaloneReviewCheck,
) -> Result<bool, sqlx::Error> {
    let changed = sqlx::query(
        "UPDATE tasks.standalone_review_checks SET create_state='sent',create_misses=0, \
         recheck_until=COALESCE(recheck_until,now()+interval '1 hour') \
         WHERE task_id=$1 AND round=$2 AND attempt=$3 AND run_id IS NULL \
         AND (create_state='recorded' OR (create_state='sent' AND create_misses>=2))",
    )
    .bind(check.task_id)
    .bind(check.round)
    .bind(check.attempt)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Count one later readback that missed an uncertain create.
pub async fn miss_standalone_check_create(
    uow: &mut UnitOfWork<'_>,
    check: &StandaloneReviewCheck,
) -> Result<i32, sqlx::Error> {
    let misses: Option<i32> = sqlx::query_scalar(
        "UPDATE tasks.standalone_review_checks SET create_misses=create_misses+1 \
         WHERE task_id=$1 AND round=$2 AND attempt=$3 AND create_state='sent' \
         AND run_id IS NULL RETURNING create_misses",
    )
    .bind(check.task_id)
    .bind(check.round)
    .bind(check.attempt)
    .fetch_optional(uow.connection())
    .await?;
    Ok(misses.unwrap_or_default())
}

/// Adopt the newest matching App check run found on the pinned head.
pub async fn adopt_standalone_check_run(
    uow: &mut UnitOfWork<'_>,
    check: &StandaloneReviewCheck,
    run_id: i64,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE tasks.standalone_review_checks SET run_id=$4,create_state='confirmed', \
         sent_digest=CASE WHEN run_id=$4 THEN sent_digest ELSE NULL END,error=NULL \
         WHERE task_id=$1 AND round=$2 AND attempt=$3",
    )
    .bind(check.task_id)
    .bind(check.round)
    .bind(check.attempt)
    .bind(run_id)
    .execute(uow.connection())
    .await?;
    Ok(())
}

/// Record the check state after GitHub accepted its exact desired update.
pub async fn settle_standalone_check(
    uow: &mut UnitOfWork<'_>,
    check: &StandaloneReviewCheck,
    digest: &str,
    completed: bool,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE tasks.standalone_review_checks SET sent_digest=$4,completed=$5,error=NULL \
         WHERE task_id=$1 AND round=$2 AND attempt=$3",
    )
    .bind(check.task_id)
    .bind(check.round)
    .bind(check.attempt)
    .bind(digest)
    .bind(completed)
    .execute(uow.connection())
    .await?;
    Ok(())
}

/// Keep a failed display write visible without blocking the review round.
pub async fn fail_standalone_check(
    database: &Database,
    check: &StandaloneReviewCheck,
    error: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE tasks.standalone_review_checks SET error=left($4,500) \
         WHERE task_id=$1 AND round=$2 AND attempt=$3",
    )
    .bind(check.task_id)
    .bind(check.round)
    .bind(check.attempt)
    .bind(error)
    .execute(database.pool())
    .await?;
    Ok(())
}

/// Find replaced check correlations that may still show unfinished duplicate runs.
pub async fn prior_standalone_checks(
    database: &Database,
    task_id: Uuid,
    round: i32,
    attempt: i32,
) -> Result<Vec<(i32, i32, String, String)>, sqlx::Error> {
    sqlx::query_as(
        "SELECT round,attempt,head,external_id FROM tasks.standalone_review_checks \
         WHERE task_id=$1 AND (round,attempt)<($2,$3) \
         AND (NOT completed OR recheck_until>now()) ORDER BY round,attempt",
    )
    .bind(task_id)
    .bind(round)
    .bind(attempt)
    .fetch_all(database.pool())
    .await
}

/// Count confirmed reviewer reports and collect their native review links.
pub async fn standalone_check_progress(
    database: &Database,
    task_id: Uuid,
    round: i32,
) -> Result<(i64, Vec<i64>), sqlx::Error> {
    let ids: Vec<i64> = sqlx::query_scalar(
        "SELECT p.review_id FROM tasks.standalone_review_publications p \
         WHERE p.task_id=$1 AND p.round=$2 AND p.status='confirmed' \
         ORDER BY p.recorded_at,p.turn_id",
    )
    .bind(task_id)
    .bind(round)
    .fetch_all(database.pool())
    .await?;
    Ok((ids.len() as i64, ids))
}

/// Mark a replaced run retired while retaining its late-visibility window.
pub async fn retire_standalone_check(
    database: &Database,
    task_id: Uuid,
    round: i32,
    attempt: i32,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE tasks.standalone_review_checks SET completed=true, \
         recheck_until=COALESCE(recheck_until,now()+interval '1 hour') \
         WHERE task_id=$1 AND round=$2 AND attempt=$3",
    )
    .bind(task_id)
    .bind(round)
    .bind(attempt)
    .execute(database.pool())
    .await?;
    Ok(())
}
