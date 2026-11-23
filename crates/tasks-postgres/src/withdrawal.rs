//! Durable later withdrawal steps under the existing request outcome family.

use anyhow::{Result, ensure};
use concurro_contracts::{RequestOutcomeStatus, TaskId};
use concurro_db::{Database, UnitOfWork};
use concurro_events_postgres::Outbox;
use concurro_tasks_core::Request;
use sqlx::types::Json;
use uuid::Uuid;

use super::{append_request_fact, publication};

/// Record the committed cutoff and its exact affected transition set with the request.
pub(crate) async fn record_withdrawal_blocked(
    work: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    request: &Request,
    task: Option<TaskId>,
    transitions: &[String],
    standalone: bool,
) -> Result<()> {
    let inserted = sqlx::query(
        "INSERT INTO tasks.withdrawal_steps \
         (source_id,request_id,standalone_command_id,request,task_id,transitions) \
         VALUES ($1,$2,$3,$4,$5,$6) \
         ON CONFLICT (source_id) DO UPDATE SET source_id=EXCLUDED.source_id \
         WHERE tasks.withdrawal_steps.transitions=EXCLUDED.transitions",
    )
    .bind(request.id)
    .bind((!standalone).then_some(request.id))
    .bind(standalone.then_some(request.id))
    .bind(Json(request))
    .bind(task.map(|id| id.0))
    .bind(transitions)
    .execute(work.connection())
    .await?;
    ensure!(
        inserted.rows_affected() == 1,
        "withdrawal step changed on replay"
    );
    append_request_fact(
        work,
        outbox,
        request,
        task,
        RequestOutcomeStatus::Blocked,
        None,
    )
    .await?;
    publication::queue_request_step_reply(work, request, "blocked").await?;
    Ok(())
}

/// Find withdrawals whose exchange acknowledgements and stream bound have passed.
pub async fn due_withdrawal_steps(database: &Database) -> Result<Vec<Uuid>> {
    Ok(sqlx::query_scalar(
        "SELECT w.source_id FROM tasks.withdrawal_steps w \
         WHERE w.stopped_at IS NULL AND w.blocked_at <= now() - interval '40 seconds' \
         AND NOT EXISTS (SELECT 1 FROM unnest(w.transitions) AS tid(transition) \
         LEFT JOIN tasks.sponsorship_transitions s ON s.transition=tid.transition \
         WHERE s.status IS DISTINCT FROM 'acknowledged' OR s.acknowledged_at IS NULL \
         OR s.acknowledged_at > now() - interval '40 seconds') \
         ORDER BY w.blocked_at LIMIT 100",
    )
    .fetch_all(database.pool())
    .await?)
}

/// Publish the stopped step exactly once, rechecking the bound under the row lock.
pub async fn complete_withdrawal_step(
    work: &mut UnitOfWork<'_>,
    outbox: &Outbox,
    id: Uuid,
) -> Result<bool> {
    let row: Option<(Json<Request>, Option<Uuid>)> = sqlx::query_as(
        "SELECT w.request,w.task_id FROM tasks.withdrawal_steps w \
         WHERE w.source_id=$1 AND w.stopped_at IS NULL \
         AND w.blocked_at <= now() - interval '40 seconds' \
         AND NOT EXISTS (SELECT 1 FROM unnest(w.transitions) AS tid(transition) \
         LEFT JOIN tasks.sponsorship_transitions s ON s.transition=tid.transition \
         WHERE s.status IS DISTINCT FROM 'acknowledged' OR s.acknowledged_at IS NULL \
         OR s.acknowledged_at > now() - interval '40 seconds') FOR UPDATE OF w",
    )
    .bind(id)
    .fetch_optional(work.connection())
    .await?;
    let Some((Json(request), task)) = row else {
        return Ok(false);
    };
    sqlx::query("UPDATE tasks.withdrawal_steps SET stopped_at=now() WHERE source_id=$1")
        .bind(id)
        .execute(work.connection())
        .await?;
    append_request_fact(
        work,
        outbox,
        &request,
        task.map(TaskId),
        RequestOutcomeStatus::Stopped,
        None,
    )
    .await?;
    publication::queue_request_step_reply(work, &request, "stopped").await?;
    Ok(true)
}
