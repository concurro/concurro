//! Ordered native review writes, each fenced before GitHub can receive it.

use anyhow::{Result, ensure};
use concurro_db::{Database, UnitOfWork};
use concurro_tasks_core::{PrReviewReport, Stage, TaskState, markdown_text};
use uuid::Uuid;

use crate::lock_issue_for_turn;

/// One frozen pending-review mutation or its exact readback target.
pub struct PrReviewAction {
    /// Order within the one native review.
    pub sequence: i32,
    /// Create, thread, reply, submit, or resolve.
    pub kind: String,
    /// Finding whose thread or disposition this action owns.
    pub finding_id: Option<String>,
    /// Review path for a new file or line thread.
    pub path: Option<String>,
    /// First right-side line of a range.
    pub start_line: Option<i64>,
    /// Last right-side line of a range.
    pub line: Option<i64>,
    /// Inline, file, or body after a definite placement refusal.
    pub placement: Option<String>,
    /// Unique visible marker used to find a lost response.
    pub marker: String,
    /// Frozen body supplied to GitHub.
    pub body: String,
    /// Recorded can send; sent requires readback; confirmed is complete.
    pub status: String,
    /// Confirmed GraphQL node identity, when the action creates one.
    pub result_node_id: Option<String>,
    /// Existing finding thread used by replies and resolutions.
    pub thread_node_id: Option<String>,
}

/// Freeze the pending review, finding threads, owner replies and resolutions.
pub(crate) async fn record_pr_review_actions(
    uow: &mut UnitOfWork<'_>,
    task: &TaskState,
    turn: Uuid,
    marker: &str,
    body: &str,
    report: &PrReviewReport,
    finding_ids: &[String],
) -> Result<()> {
    let owns_threads = report.findings.iter().any(|finding| finding.path.is_some());
    let prior_threads: Vec<(String, Option<String>)> = sqlx::query_as(
        "SELECT id,thread_node_id FROM tasks.pr_review_findings WHERE task_id=$1 \
         AND id=ANY($2) ORDER BY sequence",
    )
    .bind(task.id.0)
    .bind(
        report
            .dispositions
            .iter()
            .map(|item| item.id.clone())
            .collect::<Vec<_>>(),
    )
    .fetch_all(uow.connection())
    .await?;
    if !owns_threads && prior_threads.iter().all(|(_, thread)| thread.is_none()) {
        return Ok(());
    }
    let mut sequence = 0;
    sqlx::query(
        "INSERT INTO tasks.pr_review_actions \
         (turn_id,sequence,kind,marker,body) VALUES ($1,$2,'create',$3,$3)",
    )
    .bind(turn)
    .bind(sequence)
    .bind(marker)
    .execute(uow.connection())
    .await?;
    for (id, finding) in finding_ids.iter().zip(&report.findings) {
        let Some(path) = &finding.path else { continue };
        sequence += 1;
        let opening = format!("<!-- concurro:pr-finding:{}:{id} -->", task.id.0);
        let title = finding.title.as_deref().unwrap_or("Finding");
        let comment = format!(
            "{opening}\n**{} · {id}**\n\n{}",
            markdown_text(title, 160),
            markdown_text(&finding.detail, 55_000),
        );
        sqlx::query(
            "INSERT INTO tasks.pr_review_actions \
             (turn_id,sequence,kind,finding_id,path,start_line,line,placement,marker,body) \
             VALUES ($1,$2,'thread',$3,$4,$5,$6,$7,$8,$9)",
        )
        .bind(turn)
        .bind(sequence)
        .bind(id)
        .bind(path)
        .bind(finding.start_line.map(i64::try_from).transpose()?)
        .bind(finding.line.map(i64::try_from).transpose()?)
        .bind(if finding.line.is_some() {
            "inline"
        } else {
            "file"
        })
        .bind(opening)
        .bind(comment)
        .execute(uow.connection())
        .await?;
    }
    let mut resolutions = Vec::new();
    for disposition in &report.dispositions {
        if !prior_threads
            .iter()
            .any(|(id, thread)| id == &disposition.id && thread.is_some())
        {
            continue;
        }
        sequence += 1;
        let reply_marker = format!("<!-- concurro:pr-disposition:{turn}:{} -->", disposition.id);
        let reply = format!(
            "{reply_marker}\n**{}**: {}",
            markdown_text(&disposition.status, 30),
            markdown_text(&disposition.evidence, 55_000),
        );
        sqlx::query(
            "INSERT INTO tasks.pr_review_actions \
             (turn_id,sequence,kind,finding_id,marker,body) \
             VALUES ($1,$2,'reply',$3,$4,$5)",
        )
        .bind(turn)
        .bind(sequence)
        .bind(&disposition.id)
        .bind(reply_marker)
        .bind(reply)
        .execute(uow.connection())
        .await?;
        if matches!(disposition.status.as_str(), "fixed" | "obsolete") {
            resolutions.push(disposition.id.as_str());
        }
    }
    sequence += 1;
    sqlx::query(
        "INSERT INTO tasks.pr_review_actions \
         (turn_id,sequence,kind,marker,body) VALUES ($1,$2,'submit',$3,$4)",
    )
    .bind(turn)
    .bind(sequence)
    .bind(marker)
    .bind(body)
    .execute(uow.connection())
    .await?;
    for id in resolutions {
        sequence += 1;
        sqlx::query(
            "INSERT INTO tasks.pr_review_actions \
             (turn_id,sequence,kind,finding_id,marker,body) \
             VALUES ($1,$2,'resolve',$3,$4,'')",
        )
        .bind(turn)
        .bind(sequence)
        .bind(id)
        .bind(format!("resolve:{turn}:{id}"))
        .execute(uow.connection())
        .await?;
    }
    Ok(())
}

/// Read the next native mutation and its current owner thread.
pub async fn next_pr_review_action(
    database: &Database,
    turn: Uuid,
) -> Result<Option<PrReviewAction>> {
    let row: Option<(
        i32,
        String,
        Option<String>,
        Option<String>,
        Option<i64>,
        Option<i64>,
        Option<String>,
        String,
        String,
        String,
        Option<String>,
        Option<String>,
    )> = sqlx::query_as(
        "SELECT a.sequence,a.kind,a.finding_id,a.path,a.start_line,a.line,a.placement, \
         a.marker,a.body,a.status,a.result_node_id,f.thread_node_id \
         FROM tasks.pr_review_actions a JOIN tasks.pr_review_publications p ON p.turn_id=a.turn_id \
         LEFT JOIN tasks.pr_review_findings f ON f.task_id=p.task_id AND f.id=a.finding_id \
         WHERE a.turn_id=$1 AND a.status<>'confirmed' AND p.status IN ('recorded','sent') \
         ORDER BY a.sequence LIMIT 1",
    )
    .bind(turn)
    .fetch_optional(database.pool())
    .await?;
    Ok(row.map(
        |(
            sequence,
            kind,
            finding_id,
            path,
            start_line,
            line,
            placement,
            marker,
            body,
            status,
            result_node_id,
            thread_node_id,
        )| PrReviewAction {
            sequence,
            kind,
            finding_id,
            path,
            start_line,
            line,
            placement,
            marker,
            body,
            status,
            result_node_id,
            thread_node_id,
        },
    ))
}

/// Claim exactly one unsent GraphQL action under the task issue lock.
pub async fn mark_pr_review_action_sent(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    sequence: i32,
) -> Result<bool> {
    let Some(state) = lock_issue_for_turn(uow, turn).await? else {
        return Ok(false);
    };
    if state.stage != Stage::Review {
        return Ok(false);
    }
    let kind: Option<String> = sqlx::query_scalar(
        "SELECT kind FROM tasks.pr_review_actions WHERE turn_id=$1 AND sequence=$2",
    )
    .bind(turn)
    .bind(sequence)
    .fetch_optional(uow.connection())
    .await?;
    if kind.as_deref() == Some("create") {
        let occupied: bool = sqlx::query_scalar(
            "SELECT EXISTS (SELECT 1 FROM tasks.pr_review_publications p \
             JOIN tasks.pr_review_publications other ON other.task_id=p.task_id \
                 AND other.pr_number=p.pr_number AND other.bot_user_id=p.bot_user_id \
                 AND other.turn_id<>p.turn_id \
             JOIN tasks.pr_review_actions created ON created.turn_id=other.turn_id \
                 AND created.kind='create' AND created.status IN ('sent','confirmed') \
             JOIN tasks.pr_review_actions submitted ON submitted.turn_id=other.turn_id \
                 AND submitted.kind='submit' AND submitted.status<>'confirmed' \
             WHERE p.turn_id=$1)",
        )
        .bind(turn)
        .fetch_one(uow.connection())
        .await?;
        if occupied {
            return Ok(false);
        }
    }
    let changed = sqlx::query(
        "UPDATE tasks.pr_review_actions SET status='sent' WHERE turn_id=$1 \
         AND sequence=$2 AND status='recorded'",
    )
    .bind(turn)
    .bind(sequence)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Record exact readback of one GitHub action and its created node.
pub async fn confirm_pr_review_action(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    sequence: i32,
    node: Option<&str>,
) -> Result<bool> {
    let Some(state) = lock_issue_for_turn(uow, turn).await? else {
        return Ok(false);
    };
    let row: Option<(String, Option<String>)> = sqlx::query_as(
        "UPDATE tasks.pr_review_actions SET status='confirmed',result_node_id=$3 \
         WHERE turn_id=$1 AND sequence=$2 AND status='sent' \
         RETURNING kind,finding_id",
    )
    .bind(turn)
    .bind(sequence)
    .bind(node)
    .fetch_optional(uow.connection())
    .await?;
    let Some((kind, finding_id)) = row else {
        return Ok(false);
    };
    if kind == "create" {
        let node = node.ok_or_else(|| anyhow::anyhow!("pending review node is missing"))?;
        sqlx::query("UPDATE tasks.pr_review_publications SET review_node_id=$2 WHERE turn_id=$1")
            .bind(turn)
            .bind(node)
            .execute(uow.connection())
            .await?;
    } else if kind == "thread" {
        if let Some(node) = node {
            let id = finding_id.ok_or_else(|| anyhow::anyhow!("thread finding is missing"))?;
            sqlx::query(
                "UPDATE tasks.pr_review_findings SET thread_node_id=$3 WHERE task_id=$1 AND id=$2",
            )
            .bind(state.id.0)
            .bind(id)
            .bind(node)
            .execute(uow.connection())
            .await?;
        }
    }
    Ok(true)
}

/// Release a definitively refused thread to file placement, then to the review body.
pub async fn fallback_pr_review_thread(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    sequence: i32,
) -> Result<bool> {
    let Some(_) = lock_issue_for_turn(uow, turn).await? else {
        return Ok(false);
    };
    let placement: Option<String> = sqlx::query_scalar(
        "SELECT placement FROM tasks.pr_review_actions WHERE turn_id=$1 AND sequence=$2 \
         AND kind='thread' AND status='sent' FOR UPDATE",
    )
    .bind(turn)
    .bind(sequence)
    .fetch_optional(uow.connection())
    .await?;
    match placement.as_deref() {
        Some("inline") => {
            sqlx::query("UPDATE tasks.pr_review_actions SET placement='file',line=NULL,start_line=NULL,status='recorded' WHERE turn_id=$1 AND sequence=$2")
                .bind(turn).bind(sequence).execute(uow.connection()).await?;
            Ok(true)
        }
        Some("file") => {
            sqlx::query("UPDATE tasks.pr_review_actions SET placement='body',status='confirmed' WHERE turn_id=$1 AND sequence=$2")
                .bind(turn).bind(sequence).execute(uow.connection()).await?;
            Ok(true)
        }
        _ => Ok(false),
    }
}

/// Reopen an action only when GitHub definitively rejected its mutation.
pub async fn retry_pr_review_action(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    sequence: i32,
) -> Result<bool> {
    let changed = sqlx::query(
        "UPDATE tasks.pr_review_actions SET status='recorded' WHERE turn_id=$1 \
         AND sequence=$2 AND status='sent'",
    )
    .bind(turn)
    .bind(sequence)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Preserve a definite refusal until the writer explicitly retries the review batch.
pub async fn fail_pr_review_action(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
    sequence: i32,
) -> Result<bool> {
    let changed = sqlx::query(
        "UPDATE tasks.pr_review_actions SET status='failed' WHERE turn_id=$1 \
         AND sequence=$2 AND status='sent'",
    )
    .bind(turn)
    .bind(sequence)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Prove the complete native write chain before settling reviewer votes.
pub async fn pr_review_actions_complete(uow: &mut UnitOfWork<'_>, turn: Uuid) -> Result<bool> {
    let (count, complete): (i64, bool) = sqlx::query_as(
        "SELECT count(*),COALESCE(bool_and(status='confirmed'),false) \
         FROM tasks.pr_review_actions WHERE turn_id=$1",
    )
    .bind(turn)
    .fetch_one(uow.connection())
    .await?;
    ensure!(count > 0, "native review action chain is missing");
    Ok(complete)
}
