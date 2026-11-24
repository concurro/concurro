//! Durable reviewer-account PR review publication and exact readback state.

use anyhow::{Result, ensure};
use concurro_db::{Database, UnitOfWork};
use serde_json::Value;
use sqlx::types::Json;
use uuid::Uuid;

use crate::read_model::append_standalone_view;
use crate::standalone::{
    enqueue_standalone_review_round, finish_cancelled_round, standalone_panel_complete,
};

/// Columns read by `standalone_review_publication`.
#[derive(sqlx::FromRow)]
struct StandalonePublicationRow {
    /// Persisted task id.
    task_id: Uuid,
    /// Persisted repository id.
    repository_id: i64,
    /// Persisted upstream.
    upstream: String,
    /// Persisted pr number.
    pr_number: i64,
    /// Persisted head.
    head: String,
    /// Persisted marker.
    marker: String,
    /// Persisted body.
    body: String,
    /// Persisted comments.
    comments: Json<Vec<Value>>,
    /// Persisted status.
    status: String,
    /// Persisted reviewer user id.
    reviewer_user_id: i64,
}

/// Columns read by `confirm_standalone_review_publication`.
type ConfirmStandaloneReviewPublicationRow =
    (Uuid, i32, String, bool, Json<Value>, Json<Vec<Value>>);

/// Columns read by `next_standalone_disposition`.
#[derive(sqlx::FromRow)]
struct StandaloneDispositionRow {
    /// Persisted finding id.
    finding_id: String,
    /// Persisted repository id.
    repository_id: i64,
    /// Persisted upstream.
    upstream: String,
    /// Persisted pr number.
    pr_number: i64,
    /// Persisted reviewer user id.
    reviewer_user_id: i64,
    /// Persisted root comment id.
    root_comment_id: i64,
    /// Persisted thread node id.
    thread_node_id: String,
    /// Persisted marker.
    marker: String,
    /// Persisted body.
    body: String,
    /// Persisted reply status.
    reply_status: String,
    /// Persisted resolve status.
    resolve_status: String,
}

/// One frozen native review owned by a standalone reviewer turn.
pub struct StandaloneReviewPublication {
    /// Exact model turn that produced the review.
    pub turn: Uuid,
    /// Standalone task identity.
    pub task_id: Uuid,
    /// Upstream repository identity.
    pub repository_id: i64,
    /// Upstream owner/repository.
    pub upstream: String,
    /// PR number.
    pub pr_number: i64,
    /// Pinned PR head.
    pub head: String,
    /// Unique marker at the start of the frozen body.
    pub marker: String,
    /// Exact native review body.
    pub body: String,
    /// Frozen inline comments and their owner finding IDs.
    pub comments: Vec<Value>,
    /// Current send/readback fence.
    pub status: String,
    /// Reviewer account ID pinned at enrollment.
    pub reviewer_user_id: i64,
}

/// Read a pending reviewer-account publication without changing its send fence.
pub async fn standalone_review_publication(
    database: &Database,
    turn: Uuid,
) -> Result<Option<StandaloneReviewPublication>> {
    let row: Option<StandalonePublicationRow> = sqlx::query_as(
        "SELECT p.task_id, t.repository_id, t.upstream, p.pr_number, p.head, p.marker, p.body, p.comments, p.status, p.reviewer_user_id \
         FROM tasks.standalone_review_publications p \
             JOIN tasks.standalone_review_tasks t ON t.id=p.task_id \
             WHERE p.turn_id=$1 AND p.status IN ('recorded','sent')",
    )
    .bind(turn)
    .fetch_optional(database.pool())
    .await?;
    Ok(row.map(
        |StandalonePublicationRow {
             task_id,
             repository_id,
             upstream,
             pr_number,
             head,
             marker,
             body,
             comments: Json(comments),
             status,
             reviewer_user_id,
         }| {
            StandaloneReviewPublication {
                turn,
                task_id,
                repository_id,
                upstream,
                pr_number,
                head,
                marker,
                body,
                comments,
                status,
                reviewer_user_id,
            }
        },
    ))
}

/// Fence all later retries to readback before the first reviewer-account POST.
pub async fn mark_standalone_review_sent(uow: &mut UnitOfWork<'_>, turn: Uuid) -> Result<bool> {
    let changed = sqlx::query(
        "UPDATE tasks.standalone_review_publications p SET status='sent' \
         FROM tasks.standalone_review_tasks t \
         WHERE p.turn_id=$1 AND p.task_id=t.id AND p.status='recorded' \
         AND t.stage IN ('review','publishing','cancelled','stopped')",
    )
    .bind(turn)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Release a fence only when GitHub definitively says the POST did not apply.
pub async fn retry_standalone_review_publication(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
) -> Result<bool> {
    let changed = sqlx::query(
        "UPDATE tasks.standalone_review_publications SET status='recorded' \
         WHERE turn_id=$1 AND status='sent'",
    )
    .bind(turn)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Retain unplaceable inline comments in the review body after a definite 422.
pub async fn fallback_standalone_review_comments(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
) -> Result<bool> {
    let changed = sqlx::query(
        "UPDATE tasks.standalone_review_publications \
         SET status='recorded',unplaced_comments=comments,comments='[]'::jsonb, \
             error='inline_placement_refused' \
         WHERE turn_id=$1 AND status='sent' AND jsonb_array_length(comments)>0",
    )
    .bind(turn)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Confirm one exact reviewer-account review and advance its pinned panel.
pub async fn confirm_standalone_review_publication(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    review_id: i64,
    threads: &[(String, String, i64)],
) -> Result<bool> {
    ensure!(review_id > 0, "GitHub review ID is invalid");
    let row: Option<ConfirmStandaloneReviewPublicationRow> = sqlx::query_as(
        "SELECT p.task_id,p.round,t.stage,r.cancellation_request_id IS NOT NULL,r.snapshot,p.comments \
         FROM tasks.standalone_review_publications p \
         JOIN tasks.standalone_review_tasks t ON t.id=p.task_id \
         JOIN tasks.standalone_review_rounds r ON r.task_id=p.task_id AND r.number=p.round \
         WHERE p.turn_id=$1 FOR UPDATE OF p,t,r",
    )
    .bind(turn)
    .fetch_optional(uow.connection())
    .await?;
    let Some((task_id, round, stage, cancelled, Json(snapshot), Json(comments))) = row else {
        return Ok(false);
    };
    ensure!(
        threads.len() == comments.len(),
        "standalone review thread readback is incomplete"
    );
    for ((id, node, root), comment) in threads.iter().zip(&comments) {
        ensure!(
            comment["finding_id"].as_str() == Some(id) && !node.is_empty() && *root > 0,
            "standalone review thread owner changed"
        );
    }
    let changed = sqlx::query(
        "UPDATE tasks.standalone_review_publications \
         SET status='confirmed',review_id=$2 WHERE turn_id=$1 AND status='sent'",
    )
    .bind(turn)
    .bind(review_id)
    .execute(uow.connection())
    .await?;
    if changed.rows_affected() == 0 {
        return Ok(false);
    }
    for (id, node, root) in threads {
        let updated = sqlx::query(
            "UPDATE tasks.standalone_review_findings SET thread_node_id=$3,root_comment_id=$4 \
             WHERE task_id=$1 AND id=$2 AND thread_node_id IS NULL",
        )
        .bind(task_id)
        .bind(id)
        .bind(node)
        .bind(root)
        .execute(uow.connection())
        .await?;
        ensure!(
            updated.rows_affected() == 1,
            "standalone finding thread owner changed"
        );
    }
    sqlx::query("UPDATE tasks.standalone_review_attempts SET status='published' WHERE attempt_id=$1 AND status='validated'")
        .bind(turn)
        .execute(uow.connection())
        .await?;
    if cancelled || stage == "cancelled" {
        finish_cancelled_round(uow, task_id, round).await?;
        append_standalone_view(uow, task_id).await?;
        return Ok(true);
    }
    if !matches!(stage.as_str(), "review" | "publishing") {
        return Ok(true);
    }
    if standalone_panel_complete(uow, task_id, round, &snapshot).await? {
        sqlx::query(
            "UPDATE tasks.standalone_review_tasks SET stage='complete' WHERE id=$1 AND round=$2",
        )
        .bind(task_id)
        .bind(round)
        .execute(uow.connection())
        .await?;
        sqlx::query("UPDATE tasks.standalone_review_rounds SET status='complete',completed_at=now() WHERE task_id=$1 AND number=$2")
            .bind(task_id)
            .bind(round)
            .execute(uow.connection())
            .await?;
    } else {
        enqueue_standalone_review_round(uow, task_id, round).await?;
    }
    append_standalone_view(uow, task_id).await?;
    Ok(true)
}

/// Retain a definite GitHub refusal and block further model admissions.
pub async fn fail_standalone_review_publication(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    reason: &str,
) -> Result<bool> {
    let row: Option<(Uuid, i32, String, bool)> = sqlx::query_as(
        "SELECT p.task_id,p.round,t.stage,r.cancellation_request_id IS NOT NULL \
         FROM tasks.standalone_review_publications p \
         JOIN tasks.standalone_review_tasks t ON t.id=p.task_id \
         JOIN tasks.standalone_review_rounds r ON r.task_id=p.task_id AND r.number=p.round \
         WHERE p.turn_id=$1 FOR UPDATE OF p,t,r",
    )
    .bind(turn)
    .fetch_optional(uow.connection())
    .await?;
    let Some((task_id, round, stage, cancelled)) = row else {
        return Ok(false);
    };
    let changed = sqlx::query(
        "UPDATE tasks.standalone_review_publications SET status='failed',error=$2 \
         WHERE turn_id=$1 AND status IN ('recorded','sent')",
    )
    .bind(turn)
    .bind(reason.replace('\0', "\\u0000"))
    .execute(uow.connection())
    .await?;
    if changed.rows_affected() == 0 {
        return Ok(false);
    }
    sqlx::query("UPDATE tasks.standalone_review_attempts SET status='failed',failure_reason=$2 WHERE attempt_id=$1")
        .bind(turn)
        .bind(reason.replace('\0', "\\u0000"))
        .execute(uow.connection())
        .await?;
    if cancelled || stage == "cancelled" {
        finish_cancelled_round(uow, task_id, round).await?;
    } else if stage != "stopped" {
        sqlx::query(
            "UPDATE tasks.standalone_review_tasks SET stage='blocked' WHERE id=$1 AND round=$2",
        )
        .bind(task_id)
        .bind(round)
        .execute(uow.connection())
        .await?;
        sqlx::query("UPDATE tasks.standalone_review_rounds SET status='blocked' WHERE task_id=$1 AND number=$2")
            .bind(task_id)
            .bind(round)
            .execute(uow.connection())
            .await?;
    }
    append_standalone_view(uow, task_id).await?;
    Ok(true)
}

/// Keep exact readback active after worker retries are exhausted.
pub async fn pending_standalone_review_readbacks(database: &Database) -> Result<Vec<Uuid>> {
    Ok(sqlx::query_scalar(
        "SELECT turn_id FROM tasks.standalone_review_publications \
         WHERE status IN ('recorded','sent') ORDER BY recorded_at,turn_id",
    )
    .fetch_all(database.pool())
    .await?)
}

/// One owner-bound finding disposition awaiting an account reply or App resolution.
pub struct StandaloneDisposition {
    /// Reviewer turn that accepted this disposition.
    pub turn: Uuid,
    /// Finding identity within its standalone task.
    pub finding_id: String,
    /// Numeric upstream repository identity.
    pub repository_id: i64,
    /// Upstream owner/repository.
    pub upstream: String,
    /// PR number.
    pub pr_number: i64,
    /// Configured reviewer account ID.
    pub reviewer_user_id: i64,
    /// Root REST comment ID owned by that reviewer.
    pub root_comment_id: i64,
    /// GraphQL thread node ID owned by that reviewer.
    pub thread_node_id: String,
    /// Exact reply marker.
    pub marker: String,
    /// Exact reply body.
    pub body: String,
    /// Reply send fence.
    pub reply_status: String,
    /// Conservative resolution fence.
    pub resolve_status: String,
}

/// Read the next ordered disposition only after its native review is confirmed.
pub async fn next_standalone_disposition(
    database: &Database,
    turn: Uuid,
) -> Result<Option<StandaloneDisposition>> {
    let row: Option<StandaloneDispositionRow> = sqlx::query_as(
        "SELECT d.finding_id, t.repository_id, t.upstream, t.pr_number, p.reviewer_user_id, f.root_comment_id, f.thread_node_id, d.marker, d.body, d.reply_status, d.resolve_status \
         FROM tasks.standalone_review_dispositions d \
         JOIN tasks.standalone_review_findings f ON f.task_id=d.task_id AND f.id=d.finding_id \
         JOIN tasks.standalone_review_tasks t ON t.id=d.task_id \
         JOIN tasks.standalone_review_publications p ON p.turn_id=d.turn_id \
         WHERE d.turn_id=$1 AND p.status='confirmed' \
         AND (d.reply_status IN ('recorded','sent') \
              OR (d.reply_status='confirmed' AND d.resolve_status IN ('recorded','intended'))) \
         ORDER BY f.sequence LIMIT 1",
    )
    .bind(turn)
    .fetch_optional(database.pool())
    .await?;
    Ok(row.map(
        |StandaloneDispositionRow {
             finding_id,
             repository_id,
             upstream,
             pr_number,
             reviewer_user_id,
             root_comment_id,
             thread_node_id,
             marker,
             body,
             reply_status,
             resolve_status,
         }| {
            StandaloneDisposition {
                turn,
                finding_id,
                repository_id,
                upstream,
                pr_number,
                reviewer_user_id,
                root_comment_id,
                thread_node_id,
                marker,
                body,
                reply_status,
                resolve_status,
            }
        },
    ))
}

/// Fence an account reply before its first POST.
pub async fn mark_standalone_disposition_reply_sent(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    finding: &str,
) -> Result<bool> {
    let changed = sqlx::query(
        "UPDATE tasks.standalone_review_dispositions SET reply_status='sent' \
         WHERE turn_id=$1 AND finding_id=$2 AND reply_status='recorded'",
    )
    .bind(turn)
    .bind(finding)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Release a reply fence only after a definite no-send outcome.
pub async fn retry_standalone_disposition_reply(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    finding: &str,
) -> Result<bool> {
    let changed = sqlx::query(
        "UPDATE tasks.standalone_review_dispositions SET reply_status='recorded' \
         WHERE turn_id=$1 AND finding_id=$2 AND reply_status='sent'",
    )
    .bind(turn)
    .bind(finding)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Confirm one exact reviewer-account reply found by readback.
pub async fn confirm_standalone_disposition_reply(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    finding: &str,
    reply_id: i64,
) -> Result<bool> {
    ensure!(reply_id > 0, "review reply ID is invalid");
    let changed = sqlx::query(
        "UPDATE tasks.standalone_review_dispositions \
         SET reply_status='confirmed',reply_id=$3 \
         WHERE turn_id=$1 AND finding_id=$2 AND reply_status IN ('recorded','sent')",
    )
    .bind(turn)
    .bind(finding)
    .bind(reply_id)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Retain a definite reviewer reply refusal.
pub async fn fail_standalone_disposition_reply(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    finding: &str,
    reason: &str,
) -> Result<bool> {
    let changed = sqlx::query(
        "UPDATE tasks.standalone_review_dispositions \
         SET reply_status='failed',error=$3 \
         WHERE turn_id=$1 AND finding_id=$2 AND reply_status IN ('recorded','sent')",
    )
    .bind(turn)
    .bind(finding)
    .bind(reason)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Record a resolution intent before the App mutation.
pub async fn intend_standalone_thread_resolution(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    finding: &str,
) -> Result<bool> {
    let changed = sqlx::query(
        "UPDATE tasks.standalone_review_dispositions SET resolve_status='intended' \
         WHERE turn_id=$1 AND finding_id=$2 AND reply_status='confirmed' \
         AND resolve_status='recorded'",
    )
    .bind(turn)
    .bind(finding)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Finish the one conservative resolution readback without retrying a human reopening.
pub async fn finish_standalone_thread_resolution(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    finding: &str,
    status: &str,
) -> Result<bool> {
    ensure!(
        matches!(status, "confirmed" | "manual" | "failed"),
        "invalid thread resolution outcome"
    );
    let changed = sqlx::query(
        "UPDATE tasks.standalone_review_dispositions SET resolve_status=$3 \
         WHERE turn_id=$1 AND finding_id=$2 AND resolve_status='intended'",
    )
    .bind(turn)
    .bind(finding)
    .bind(status)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Resume account replies and App resolutions after worker retries are spent.
pub async fn pending_standalone_dispositions(database: &Database) -> Result<Vec<Uuid>> {
    Ok(sqlx::query_scalar(
        "SELECT DISTINCT d.turn_id FROM tasks.standalone_review_dispositions d \
         JOIN tasks.standalone_review_publications p ON p.turn_id=d.turn_id \
         WHERE p.status='confirmed' AND (d.reply_status IN ('recorded','sent') \
         OR (d.reply_status='confirmed' AND d.resolve_status IN ('recorded','intended'))) \
         ORDER BY d.turn_id",
    )
    .fetch_all(database.pool())
    .await?)
}
