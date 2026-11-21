//! Durable issue artifact writes for complete task reports.

use anyhow::Result;
use concurro_db::{Database, UnitOfWork};
use concurro_tasks_core::{IssueKey, TaskState};
use serde_json::Value;
use uuid::Uuid;

use crate::read_model::append_report_view;

/// One immutable report body awaiting its issue artifact readback.
pub struct ReportArtifactWrite {
    /// Stable report identity.
    pub id: Uuid,
    /// Issue receiving the artifact.
    pub issue: IssueKey,
    /// Exact artifact name.
    pub name: String,
    /// Exact body expected from GitHub.
    pub body: String,
    /// Recorded, sent, confirmed, or failed.
    pub status: String,
    /// Standalone owner requires an open PR before a new artifact write.
    pub standalone_task_id: Option<Uuid>,
}

/// Record and queue the visible copy in the report event's transaction.
pub(crate) async fn record_report_artifact(
    uow: &mut UnitOfWork<'_>,
    task: &TaskState,
    report_id: Uuid,
    body: &Value,
) -> Result<()> {
    insert_report_artifact(uow, report_id, Some(task.id.0), None, task.issue, body).await
}

/// Record a full standalone reviewer report on its existing PR artifact surface.
pub(crate) async fn record_standalone_review_report_artifact(
    uow: &mut UnitOfWork<'_>,
    task_id: Uuid,
    issue: IssueKey,
    report_id: Uuid,
    body: &Value,
) -> Result<()> {
    insert_report_artifact(uow, report_id, None, Some(task_id), issue, body).await
}

/// Insert the common immutable artifact and queue its exact publication readback.
async fn insert_report_artifact(
    uow: &mut UnitOfWork<'_>,
    report_id: Uuid,
    task_id: Option<Uuid>,
    standalone_task_id: Option<Uuid>,
    issue: IssueKey,
    body: &Value,
) -> Result<()> {
    let name = format!("concurro-task-report-{report_id}");
    let content = format!(
        "<!-- concurro:task-report:{report_id} -->\n{}",
        serde_json::to_string_pretty(body)?
    );
    sqlx::query(
        "INSERT INTO tasks.report_artifact_writes \
         (report_id,task_id,standalone_task_id,repository_id,issue_number,name,body) \
         VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (report_id) DO NOTHING",
    )
    .bind(report_id)
    .bind(task_id)
    .bind(standalone_task_id)
    .bind(issue.repository_id)
    .bind(issue.number)
    .bind(name)
    .bind(content)
    .execute(uow.connection())
    .await?;
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job(identifier => 'publish_task_report', \
         payload => json_build_object('id',$1),queue_name => $2, \
         max_attempts => 25,job_key => $3)).id",
    )
    .bind(report_id)
    .bind(format!(
        "task-report:{}",
        task_id.or(standalone_task_id).expect("artifact owner")
    ))
    .bind(format!("task-report:{report_id}"))
    .fetch_one(uow.connection())
    .await?;
    append_report_view(uow, report_id).await?;
    Ok(())
}

/// Read the frozen report artifact without changing its send fence.
pub async fn report_artifact_write(
    database: &Database,
    id: Uuid,
) -> Result<Option<ReportArtifactWrite>> {
    let row: Option<(i64, i64, String, String, String, Option<Uuid>)> = sqlx::query_as(
        "SELECT repository_id,issue_number,name,body,status,standalone_task_id \
         FROM tasks.report_artifact_writes WHERE report_id=$1",
    )
    .bind(id)
    .fetch_optional(database.pool())
    .await?;
    Ok(row.map(
        |(repository_id, number, name, body, status, standalone_task_id)| ReportArtifactWrite {
            id,
            issue: IssueKey {
                repository_id,
                number,
            },
            name,
            body,
            status,
            standalone_task_id,
        },
    ))
}

/// Fence an uncertain artifact POST before sending it to GitHub.
pub async fn mark_report_artifact_sent(uow: &mut UnitOfWork<'_>, id: Uuid) -> Result<bool> {
    let changed = sqlx::query(
        "UPDATE tasks.report_artifact_writes SET status='sent' \
         WHERE report_id=$1 AND status='recorded'",
    )
    .bind(id)
    .execute(uow.connection())
    .await?;
    if changed.rows_affected() == 1 {
        append_report_view(uow, id).await?;
    }
    Ok(changed.rows_affected() == 1)
}

/// Reopen a write after a response proves GitHub did not apply it.
pub async fn retry_report_artifact(uow: &mut UnitOfWork<'_>, id: Uuid) -> Result<bool> {
    let changed = sqlx::query(
        "UPDATE tasks.report_artifact_writes SET status='recorded',error=NULL \
         WHERE report_id=$1 AND status='sent'",
    )
    .bind(id)
    .execute(uow.connection())
    .await?;
    if changed.rows_affected() == 1 {
        append_report_view(uow, id).await?;
    }
    Ok(changed.rows_affected() == 1)
}

/// Keep a definitive GitHub refusal visible to the operator.
pub async fn fail_report_artifact(
    uow: &mut UnitOfWork<'_>,
    id: Uuid,
    reason: &str,
) -> Result<bool> {
    let changed = sqlx::query(
        "UPDATE tasks.report_artifact_writes SET status='failed',error=$2 \
         WHERE report_id=$1 AND status='sent'",
    )
    .bind(id)
    .bind(reason)
    .execute(uow.connection())
    .await?;
    if changed.rows_affected() == 1 {
        append_report_view(uow, id).await?;
    }
    Ok(changed.rows_affected() == 1)
}

/// Confirm only the exact App-authored artifact found by readback.
pub async fn confirm_report_artifact(
    uow: &mut UnitOfWork<'_>,
    id: Uuid,
    number: i64,
) -> Result<bool> {
    let changed = sqlx::query(
        "UPDATE tasks.report_artifact_writes SET status='confirmed',artifact_number=$2,error=NULL \
         WHERE report_id=$1 AND status IN ('recorded','sent')",
    )
    .bind(id)
    .bind(number)
    .execute(uow.connection())
    .await?;
    if changed.rows_affected() == 1 {
        append_report_view(uow, id).await?;
    }
    Ok(changed.rows_affected() == 1)
}

/// Continue uncertain artifact readback after worker retries are spent.
pub async fn pending_report_artifact_readbacks(database: &Database) -> Result<Vec<Uuid>> {
    Ok(sqlx::query_scalar(
        "SELECT report_id FROM tasks.report_artifact_writes WHERE status IN ('recorded','sent') \
         ORDER BY recorded_at,report_id",
    )
    .fetch_all(database.pool())
    .await?)
}
