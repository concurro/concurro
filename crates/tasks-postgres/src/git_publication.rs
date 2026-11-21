//! Durable intent for one approved candidate's fork branch and draft pull request.

use anyhow::{Result, ensure};
use chrono::{SecondsFormat, Utc};
use concurro_db::{Database, UnitOfWork};
use concurro_events_postgres::Outbox;
use concurro_tasks_core::{InitializationSnapshot, IssueKey, Stage, TaskState};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

use crate::{
    append_task_snapshot, lock_issue_for_turn, publication::task_comment_id, queue_task_comment,
};

/// GitHub identities and sent-write fences for one approved candidate.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct GitPublicationIntent {
    /// Exact task-owned branch name.
    pub branch: String,
    /// Approved commit initially pointed to by the reserved branch.
    pub parent: String,
    /// Frozen author and committer date for deterministic Git object retries.
    pub commit_date: String,
    /// Public App bot ID used for deterministic Git author and committer identity.
    #[serde(default)]
    pub bot_user_id: Option<i64>,
    /// Exact publisher login used with the pinned bot ID for Git identity.
    #[serde(default)]
    pub bot_login: Option<String>,
    /// Whether the branch create was confirmed by exact ref readback.
    pub branch_created: bool,
    /// Exact immutable candidate commit once its create response is known.
    pub commit_sha: Option<String>,
    /// Whether the branch ref was read back at the candidate commit.
    pub ref_advanced: bool,
    /// Frozen draft PR title before the first create request.
    pub pr_title: Option<String>,
    /// Frozen draft PR body before the first create request.
    pub pr_body: Option<String>,
    /// A PR create may have applied; readback is required before any further write.
    pub pr_create_sent: bool,
    /// A PR description update may have applied; only readback is permitted afterward.
    #[serde(default)]
    pub pr_update_sent: bool,
    /// Verified App-authored upstream PR number.
    pub pr_number: Option<i64>,
}

/// Source and durable intent needed to reconcile one publication job.
pub struct GitPublicationInput {
    /// Current task state and issue identity.
    pub state: TaskState,
    /// Exact accepted implementer turn.
    pub turn: Uuid,
    /// Pinned configuration and base source identity.
    pub snapshot: InitializationSnapshot,
    /// Source-validated native candidate.
    pub candidate: Value,
    /// Current approved plan for the PR title and description.
    pub plan: Value,
    /// Saved publication intent, if reservation has started.
    pub intent: Option<(Uuid, i64, GitPublicationIntent)>,
    /// Prior settled PR number, head, branch and frozen description for a fix publication.
    pub prior_pr: Option<(i64, String, String, Option<String>, Option<String>)>,
}

/// Read only the latest candidate under a still-current approved plan.
pub async fn git_publication_input(
    database: &Database,
    issue: IssueKey,
) -> Result<Option<GitPublicationInput>> {
    let row: Option<(
        Json<TaskState>,
        Uuid,
        Json<InitializationSnapshot>,
        Json<Value>,
        Vec<u8>,
    )> = sqlx::query_as(
        "SELECT t.state,r.attempt_id,i.snapshot,r.candidate,p.raw_report_bytes \
         FROM tasks.task_state t JOIN tasks.initialization_inputs i ON i.task_id=t.task_id \
         JOIN tasks.implementation_reports r ON r.task_id=t.task_id AND r.status='accepted' \
         JOIN tasks.plan_reports p ON p.task_id=t.task_id AND p.accepted AND p.current \
             AND p.plan_id=r.candidate->>'plan_id' \
         JOIN tasks.plan_approvals a ON a.task_id=t.task_id \
             AND a.plan_id=r.candidate->>'plan_id' \
             AND a.binding=r.candidate->>'binding' \
             AND a.request_id=(SELECT request_id FROM tasks.plan_approvals \
                 WHERE task_id=t.task_id ORDER BY recorded_at DESC,request_id DESC LIMIT 1) \
         WHERE t.repository_id=$1 AND t.issue_number=$2 \
         ORDER BY r.recorded_at DESC,r.attempt_id DESC LIMIT 1",
    )
    .bind(issue.repository_id)
    .bind(issue.number)
    .fetch_optional(database.pool())
    .await?;
    let Some((Json(state), turn, Json(snapshot), Json(candidate), plan)) = row else {
        return Ok(None);
    };
    if state.stage != Stage::Publish {
        return Ok(None);
    }
    let intent: Option<(Uuid, i64, Json<GitPublicationIntent>)> = sqlx::query_as(
        "SELECT attempt_id,revision,intent FROM tasks.git_publications \
         WHERE task_id=$1 AND attempt_id=$2",
    )
    .bind(state.id.0)
    .bind(turn)
    .fetch_optional(database.pool())
    .await?;
    let prior_pr: Option<(i64, String, String, Option<String>, Option<String>)> = sqlx::query_as(
        "SELECT b.pr_number,b.head,b.branch,p.intent->>'pr_title',p.intent->>'pr_body' \
         FROM tasks.pr_review_batches b JOIN tasks.git_publications p \
             ON p.attempt_id=b.publication_attempt AND p.task_id=b.task_id \
         WHERE b.task_id=$1 AND b.status='settled' ORDER BY b.round DESC,b.id DESC LIMIT 1",
    )
    .bind(state.id.0)
    .fetch_optional(database.pool())
    .await?;
    Ok(Some(GitPublicationInput {
        state,
        turn,
        snapshot,
        candidate,
        plan: serde_json::from_slice(&plan)?,
        intent: intent.map(|(owner, revision, Json(intent))| (owner, revision, intent)),
        prior_pr,
    }))
}

/// Record a branch reservation before its first GitHub create request.
pub async fn reserve_git_publication(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    branch: &str,
    parent: &str,
) -> Result<bool> {
    let Some(state) = lock_issue_for_turn(uow, turn).await? else {
        return Ok(false);
    };
    if state.stage != Stage::Publish {
        return Ok(false);
    }
    let accepted: Option<(String, String)> = sqlx::query_as(
        "SELECT a.plan_id,a.binding FROM tasks.implementation_reports r \
         JOIN tasks.plan_reports p ON p.task_id=r.task_id AND p.accepted AND p.current \
             AND p.plan_id=r.candidate->>'plan_id' \
         JOIN tasks.plan_approvals a ON a.task_id=r.task_id \
             AND a.plan_id=r.candidate->>'plan_id' \
             AND a.binding=r.candidate->>'binding' \
             AND a.request_id=(SELECT request_id FROM tasks.plan_approvals \
                 WHERE task_id=r.task_id ORDER BY recorded_at DESC,request_id DESC LIMIT 1) \
         WHERE r.attempt_id=$1 AND r.task_id=$2 AND r.status='accepted' \
             AND r.candidate->>'parent'=$3",
    )
    .bind(turn)
    .bind(state.id.0)
    .bind(parent)
    .fetch_optional(uow.connection())
    .await?;
    if accepted.is_none() || accepted != crate::design::approval_proof(uow, state.id.0).await? {
        return Ok(false);
    }
    let intent = GitPublicationIntent {
        branch: branch.to_owned(),
        parent: parent.to_owned(),
        commit_date: Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true),
        bot_user_id: None,
        bot_login: None,
        branch_created: false,
        commit_sha: None,
        ref_advanced: false,
        pr_title: None,
        pr_body: None,
        pr_create_sent: false,
        pr_update_sent: false,
        pr_number: None,
    };
    sqlx::query(
        "INSERT INTO tasks.git_publications (task_id,attempt_id,intent) VALUES ($1,$2,$3) \
         ON CONFLICT (task_id,attempt_id) DO NOTHING",
    )
    .bind(state.id.0)
    .bind(turn)
    .bind(Json(&intent))
    .execute(uow.connection())
    .await?;
    let (saved_turn, Json(saved)): (Uuid, Json<GitPublicationIntent>) = sqlx::query_as(
        "SELECT attempt_id,intent FROM tasks.git_publications \
            WHERE task_id=$1 AND attempt_id=$2",
    )
    .bind(state.id.0)
    .bind(turn)
    .fetch_one(uow.connection())
    .await?;
    ensure!(
        saved_turn == turn && saved.branch == branch && saved.parent == parent,
        "publication intent belongs to another candidate"
    );
    Ok(true)
}

/// Compare and save one exact intent revision after a GitHub readback.
pub async fn save_git_publication(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    revision: i64,
    intent: &GitPublicationIntent,
) -> Result<bool> {
    let Some(state) = lock_issue_for_turn(uow, turn).await? else {
        return Ok(false);
    };
    if state.stage != Stage::Publish {
        return Ok(false);
    }
    let saved = sqlx::query(
        "UPDATE tasks.git_publications SET revision=revision+1,intent=$4 \
         WHERE task_id=$1 AND attempt_id=$2 AND revision=$3",
    )
    .bind(state.id.0)
    .bind(turn)
    .bind(revision)
    .bind(Json(intent))
    .execute(uow.connection())
    .await?;
    Ok(saved.rows_affected() == 1)
}

/// Queue the once-per-branch implementation notice only after its ref is confirmed.
pub async fn queue_implementation_started(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    fork: &str,
    branch: &str,
) -> Result<bool> {
    let Some(state) = lock_issue_for_turn(uow, turn).await? else {
        return Ok(false);
    };
    if state.stage != Stage::Publish {
        return Ok(false);
    }
    let intent: Option<Json<GitPublicationIntent>> = sqlx::query_scalar(
        "SELECT intent FROM tasks.git_publications WHERE task_id=$1 AND attempt_id=$2",
    )
    .bind(state.id.0)
    .bind(turn)
    .fetch_optional(uow.connection())
    .await?;
    let Some(Json(intent)) = intent else {
        return Ok(false);
    };
    ensure!(
        intent.branch_created && intent.branch == branch,
        "implementation branch notice is not backed by a confirmed ref"
    );
    let body = format!(
        "Implementation started\n\nWork branch: [{branch}](https://github.com/{fork}/tree/{branch})"
    );
    let source_key = format!("implementation:{branch}");
    let existing: Option<i32> = sqlx::query_scalar(
        "SELECT 1 FROM tasks.publication_comments WHERE id=$1 AND task_id=$2 AND kind='notice'",
    )
    .bind(task_comment_id(state.id.0, &source_key))
    .bind(state.id.0)
    .fetch_optional(uow.connection())
    .await?;
    if existing.is_some() {
        return Ok(true);
    }
    queue_task_comment(uow, &state, "notice", &source_key, &body).await?;
    Ok(true)
}

/// Advance a verified draft PR to the review stage exactly once.
pub async fn complete_git_publication(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    turn: Uuid,
    pr_number: i64,
    changed_paths: &[String],
) -> Result<bool> {
    let Some(state) = lock_issue_for_turn(uow, turn).await? else {
        return Ok(false);
    };
    if state.stage != Stage::Publish {
        return Ok(false);
    }
    let intent: Option<Json<GitPublicationIntent>> = sqlx::query_scalar(
        "SELECT intent FROM tasks.git_publications WHERE task_id=$1 AND attempt_id=$2",
    )
    .bind(state.id.0)
    .bind(turn)
    .fetch_optional(uow.connection())
    .await?;
    let Some(Json(intent)) = intent else {
        return Ok(false);
    };
    ensure!(
        intent.ref_advanced && intent.pr_number == Some(pr_number),
        "draft PR does not match the advanced publication intent"
    );
    let next = TaskState {
        stage: Stage::Review,
        stage_reason: Some(format!("Draft pull request #{pr_number} awaits review")),
        revision: state.revision + 1,
        ..state.clone()
    };
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(state.id.0)
        .bind(i64::try_from(next.revision)?)
        .bind(Json(&next))
        .execute(uow.connection())
        .await?;
    crate::pr_review::record_pr_review_batch(uow, &next, turn, pr_number, &intent, changed_paths)
        .await?;
    append_task_snapshot(uow, outbox, &next).await?;
    Ok(true)
}

/// Retain a permanent publication conflict in the visible task state.
pub async fn block_git_publication(
    uow: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    turn: Uuid,
    reason: &str,
) -> Result<bool> {
    let Some(state) = lock_issue_for_turn(uow, turn).await? else {
        return Ok(false);
    };
    if state.stage != Stage::Publish {
        return Ok(false);
    }
    let next = TaskState {
        stage: Stage::Blocked,
        retry_stage: Some(Stage::Publish),
        stage_reason: Some(reason.to_owned()),
        revision: state.revision + 1,
        ..state.clone()
    };
    sqlx::query("UPDATE tasks.task_state SET revision=$2,state=$3 WHERE task_id=$1")
        .bind(state.id.0)
        .bind(i64::try_from(next.revision)?)
        .bind(Json(&next))
        .execute(uow.connection())
        .await?;
    append_task_snapshot(uow, outbox, &next).await?;
    Ok(true)
}
