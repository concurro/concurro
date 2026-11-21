//! Durable issue-comment replies and uncertain-create recovery.

use concurro_db::{Database, UnitOfWork};
use concurro_tasks_core::{IssueKey, Request, RequestStatus, TaskState, request_reply_body};
use sha2::{Digest, Sha256};
use uuid::Uuid;

/// One claimed App comment write, fenced from older worker observations.
pub struct CommentWrite {
    /// Stable logical write identity.
    pub id: Uuid,
    /// GitHub issue receiving the reply.
    pub issue: IssueKey,
    /// Exact comment body with its unique recovery marker.
    pub body: String,
    /// Marker used to adopt a create whose HTTP reply was lost.
    pub marker: String,
    /// Worker claim token required to settle the write.
    pub claim_token: Uuid,
    /// A prior send may have landed; only readback is permitted.
    pub readback_only: bool,
}

/// Read a queued comment's repository and state before preparing a send token.
pub async fn comment_scope(
    database: &Database,
    id: Uuid,
) -> Result<Option<(i64, String, bool)>, sqlx::Error> {
    sqlx::query_as(
        "SELECT repository_id,status,standalone_command_id IS NOT NULL \
        FROM tasks.publication_comments WHERE id=$1",
    )
    .bind(id)
    .fetch_optional(database.pool())
    .await
}

/// Host operator's explicit disposition for an uncertain comment create.
pub enum CommentResolution<'a> {
    /// Adopt the observed GitHub comment and its public URL.
    Written { comment_id: i64, url: &'a str },
    /// Stop retries while still watching for a late marked App comment.
    Failed,
    /// Override uncertainty and allow another POST, which can duplicate a late create.
    Resend,
}

/// Queue a durable reply job using its stable logical identity.
async fn enqueue_comment_job(
    uow: &mut UnitOfWork<'_>,
    id: Uuid,
    issue: IssueKey,
) -> anyhow::Result<()> {
    let queue = format!("publication:issue:{}:{}", issue.repository_id, issue.number);
    let key = format!("publication-comment:{id}");
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'publish_request_reply', payload => json_build_object('id',$1::text), \
         queue_name => $2, max_attempts => 25, job_key => $3)).id",
    )
    .bind(id)
    .bind(queue)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Queue one immutable task comment through the same uncertain-create path as replies.
pub async fn queue_task_comment(
    uow: &mut UnitOfWork<'_>,
    state: &TaskState,
    kind: &str,
    source_key: &str,
    body: &str,
) -> anyhow::Result<Uuid> {
    let id = task_comment_id(state.id.0, source_key);
    let marker = if kind == "dashboard" {
        "<!-- concurro:v1 -->".to_owned()
    } else {
        format!(
            "<!-- concurro:event:v1:{} -->",
            hex::encode(Sha256::digest(id.as_bytes()))
        )
    };
    let body = if body.starts_with(&marker) {
        body.to_owned()
    } else {
        format!("{marker}\n{body}")
    };
    let saved: Option<Uuid> = sqlx::query_scalar(
        "INSERT INTO tasks.publication_comments \
         (id,task_id,kind,source_revision,repository_id,issue_number,marker,body,status) \
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'recorded') ON CONFLICT (id) DO UPDATE \
         SET id=EXCLUDED.id WHERE tasks.publication_comments.task_id=EXCLUDED.task_id \
         AND tasks.publication_comments.kind=EXCLUDED.kind \
         AND tasks.publication_comments.source_revision=EXCLUDED.source_revision \
         AND tasks.publication_comments.marker=EXCLUDED.marker \
         AND tasks.publication_comments.body=EXCLUDED.body RETURNING id",
    )
    .bind(id)
    .bind(state.id.0)
    .bind(kind)
    .bind(i64::try_from(state.revision)?)
    .bind(state.issue.repository_id)
    .bind(state.issue.number)
    .bind(marker)
    .bind(body)
    .fetch_optional(uow.connection())
    .await?;
    anyhow::ensure!(saved == Some(id), "task comment changed on replay");
    enqueue_comment_job(uow, id, state.issue).await?;
    Ok(id)
}

/// Derive the stable identity of one task comment from its source key.
pub(crate) fn task_comment_id(task: Uuid, source_key: &str) -> Uuid {
    Uuid::new_v5(
        &Uuid::NAMESPACE_OID,
        format!("task-comment:{task}:{source_key}").as_bytes(),
    )
}

/// Record and queue one reply in the request's final disposition transaction.
pub(crate) async fn queue_request_reply(
    uow: &mut UnitOfWork<'_>,
    request: &Request,
    status: RequestStatus,
    reason: Option<&str>,
) -> anyhow::Result<()> {
    if request.channel != "comment" {
        return Ok(());
    }
    let id = Uuid::new_v5(
        &Uuid::NAMESPACE_OID,
        format!("publication-comment:{}", request.id).as_bytes(),
    );
    let marker = format!("<!-- concurro:reply:v1:{} -->", request.id);
    let body = request_reply_body(request, status, reason);
    let saved: Option<Uuid> = sqlx::query_scalar(
        "INSERT INTO tasks.publication_comments (id,request_id,repository_id,issue_number,marker,body,status) \
         VALUES ($1,$2,$3,$4,$5,$6,'recorded') ON CONFLICT (request_id) DO UPDATE \
         SET id=tasks.publication_comments.id \
         WHERE tasks.publication_comments.marker=EXCLUDED.marker \
         AND tasks.publication_comments.body=EXCLUDED.body RETURNING id",
    )
    .bind(id)
    .bind(request.id)
    .bind(request.issue.repository_id)
    .bind(request.issue.number)
    .bind(marker)
    .bind(body)
    .fetch_optional(uow.connection())
    .await?;
    anyhow::ensure!(saved == Some(id), "request reply changed on replay");
    enqueue_comment_job(uow, id, request.issue).await?;
    Ok(())
}

/// Claim a pending reply; uncertain creates can only be recovered by readback.
pub async fn claim_comment(
    uow: &mut UnitOfWork<'_>,
    id: Uuid,
) -> Result<Option<CommentWrite>, sqlx::Error> {
    let row: Option<(i64, i64, String, String, String, Option<String>, bool)> = sqlx::query_as(
        "SELECT repository_id,issue_number,body,marker,status,failure, \
         retry_at IS NOT NULL AND retry_at>now() \
         FROM tasks.publication_comments WHERE id=$1 FOR UPDATE",
    )
    .bind(id)
    .fetch_optional(uow.connection())
    .await?;
    let Some((repository_id, number, body, marker, status, failure, waiting)) = row else {
        return Ok(None);
    };
    if waiting
        || status == "written"
        || status == "failed" && failure.as_deref() != Some("operator_failed")
    {
        return Ok(None);
    }
    let claim_token = Uuid::now_v7();
    sqlx::query(
        "UPDATE tasks.publication_comments SET status=CASE WHEN status='failed' THEN 'failed' ELSE 'sending' END, \
         claim_token=$2,retry_at=NULL,updated_at=now() \
         WHERE id=$1",
    )
    .bind(id)
    .bind(claim_token)
    .execute(uow.connection())
    .await?;
    Ok(Some(CommentWrite {
        id,
        issue: IssueKey {
            repository_id,
            number,
        },
        body,
        marker,
        claim_token,
        readback_only: status != "recorded",
    }))
}

/// Settle a reply only from the worker that observed the matching App comment.
pub async fn settle_comment(
    uow: &mut UnitOfWork<'_>,
    write: &CommentWrite,
    comment_id: i64,
) -> Result<bool, sqlx::Error> {
    let changed = sqlx::query(
        "UPDATE tasks.publication_comments SET status='written',comment_id=$3,updated_at=now() \
         WHERE id=$1 AND claim_token=$2 AND status IN ('sending','failed')",
    )
    .bind(write.id)
    .bind(write.claim_token)
    .bind(comment_id)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Record a definitive GitHub refusal without retrying an identical create.
pub async fn fail_comment(
    uow: &mut UnitOfWork<'_>,
    write: &CommentWrite,
    reason: &str,
) -> Result<bool, sqlx::Error> {
    let changed = sqlx::query(
        "UPDATE tasks.publication_comments SET status='failed',failure=$3,updated_at=now() \
         WHERE id=$1 AND claim_token=$2 AND status='sending'",
    )
    .bind(write.id)
    .bind(write.claim_token)
    .bind(reason)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Restore a definitively rejected create for a fresh token or rate-limit retry.
pub async fn defer_comment(
    uow: &mut UnitOfWork<'_>,
    write: &CommentWrite,
    reason: &str,
    delay_seconds: i64,
) -> Result<bool, sqlx::Error> {
    let changed = sqlx::query(
        "UPDATE tasks.publication_comments SET status='recorded',claim_token=NULL, \
         failure=$3,retry_at=now()+$4*interval '1 second',updated_at=now() \
         WHERE id=$1 AND claim_token=$2 AND status='sending'",
    )
    .bind(write.id)
    .bind(write.claim_token)
    .bind(reason)
    .bind(delay_seconds)
    .execute(uow.connection())
    .await?;
    Ok(changed.rows_affected() == 1)
}

/// Find uncertain comment creates due for independent readback after worker retries.
pub async fn due_comment_readbacks(database: &Database) -> Result<Vec<Uuid>, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT id FROM tasks.publication_comments \
         WHERE ((status='sending' OR (status='failed' AND failure='operator_failed')) \
             AND updated_at < now()-interval '30 seconds') \
         OR (status='recorded' AND retry_at IS NOT NULL AND retry_at<=now()) \
         ORDER BY updated_at,id LIMIT 100",
    )
    .fetch_all(database.pool())
    .await
}

/// Record one host decision about a pending comment create and fence older claims.
pub async fn resolve_comment(
    uow: &mut UnitOfWork<'_>,
    id: Uuid,
    actor: &str,
    resolution: CommentResolution<'_>,
) -> anyhow::Result<bool> {
    anyhow::ensure!(!actor.trim().is_empty(), "operator actor is empty");
    let row: Option<(i64, i64, String)> = sqlx::query_as(
        "SELECT repository_id,issue_number,status FROM tasks.publication_comments WHERE id=$1 FOR UPDATE",
    )
    .bind(id)
    .fetch_optional(uow.connection())
    .await?;
    let Some((repository_id, number, status)) = row else {
        return Ok(false);
    };
    if status == "written" {
        return Ok(false);
    }
    let (action, outcome) = match resolution {
        CommentResolution::Written { comment_id, url } => {
            anyhow::ensure!(
                comment_id > 0 && url.starts_with("https://"),
                "comment URL is invalid"
            );
            sqlx::query(
                "UPDATE tasks.publication_comments SET status='written',comment_id=$2,comment_url=$3, \
                 failure=NULL,claim_token=$4,updated_at=now() WHERE id=$1",
            )
            .bind(id).bind(comment_id).bind(url).bind(Uuid::now_v7())
            .execute(uow.connection()).await?;
            ("writes.resolve.written", "written")
        }
        CommentResolution::Failed => {
            sqlx::query(
                "UPDATE tasks.publication_comments SET status='failed',failure='operator_failed', \
                 claim_token=$2,updated_at=now() WHERE id=$1",
            )
            .bind(id)
            .bind(Uuid::now_v7())
            .execute(uow.connection())
            .await?;
            ("writes.resolve.failed", "failed")
        }
        CommentResolution::Resend => {
            sqlx::query(
                "UPDATE tasks.publication_comments SET status='recorded',failure=NULL, \
                 claim_token=NULL,retry_at=NULL,updated_at=now() WHERE id=$1",
            )
            .bind(id)
            .execute(uow.connection())
            .await?;
            enqueue_comment_job(
                uow,
                id,
                IssueKey {
                    repository_id,
                    number,
                },
            )
            .await?;
            ("writes.resolve.resend", "recorded")
        }
    };
    sqlx::query(
        "INSERT INTO tasks.operator_actions (actor,action,target,outcome) VALUES ($1,$2,$3,$4)",
    )
    .bind(actor)
    .bind(action)
    .bind(id.to_string())
    .bind(outcome)
    .execute(uow.connection())
    .await?;
    Ok(true)
}
