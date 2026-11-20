//! One mutable issue dashboard with recorded creates and edits.

use concurro_db::{Database, UnitOfWork};
use concurro_tasks_core::{TaskState, dashboard_body};
use uuid::Uuid;

use crate::publication::queue_task_comment;

/// Current desired body and the first comment create for one task dashboard.
#[derive(Clone, Debug)]
pub struct DashboardSurface {
    /// Task that owns this dashboard.
    pub task_id: Uuid,
    /// Issue repository identity.
    pub repository_id: i64,
    /// Issue receiving the dashboard.
    pub issue_number: i64,
    /// Latest task revision represented by the desired body.
    pub revision: i64,
    /// Exact body wanted on GitHub.
    pub desired_body: String,
    /// Durable initial create identity.
    pub create_id: Uuid,
    /// GitHub comment ID after the create is observed.
    pub comment_id: Option<i64>,
}

/// Queue one serialized dashboard reconciliation for a task.
pub async fn enqueue_dashboard(
    uow: &mut UnitOfWork<'_>,
    task_id: Uuid,
    suffix: &str,
) -> Result<(), sqlx::Error> {
    let queue = format!("dashboard:{task_id}");
    let key = format!("dashboard:{task_id}:{suffix}");
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'dashboard',payload => json_build_object('task_id',$1), \
         queue_name => $2,max_attempts => 25,job_key => $3)).id",
    )
    .bind(task_id)
    .bind(queue)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Record the latest dashboard body in the same transaction as its task state.
pub async fn record_dashboard_desire(
    uow: &mut UnitOfWork<'_>,
    state: &TaskState,
) -> anyhow::Result<()> {
    let create_id: Option<Uuid> = sqlx::query_scalar(
        "SELECT id FROM tasks.publication_comments WHERE task_id=$1 AND kind='dashboard'",
    )
    .bind(state.id.0)
    .fetch_optional(uow.connection())
    .await?;
    let body = dashboard_body(state);
    let create_id = match create_id {
        Some(id) => id,
        None => queue_task_comment(uow, state, "dashboard", "dashboard", &body).await?,
    };
    let revision = i64::try_from(state.revision)?;
    let changed: Option<Uuid> = sqlx::query_scalar(
        "INSERT INTO tasks.dashboard_surfaces \
         (task_id,repository_id,issue_number,revision,desired_body,create_id) \
         VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (task_id) DO UPDATE SET \
         revision=EXCLUDED.revision,desired_body=EXCLUDED.desired_body,watch=true, \
         last_error=NULL,retry_at=NULL,updated_at=now() \
         WHERE tasks.dashboard_surfaces.revision<EXCLUDED.revision RETURNING task_id",
    )
    .bind(state.id.0)
    .bind(state.issue.repository_id)
    .bind(state.issue.number)
    .bind(revision)
    .bind(body)
    .bind(create_id)
    .fetch_optional(uow.connection())
    .await?;
    if changed.is_some() {
        sqlx::query(
            "UPDATE tasks.dashboard_writes SET status='obsolete_uncertain',updated_at=now() \
             WHERE task_id=$1 AND revision<$2 AND status='sending'",
        )
        .bind(state.id.0)
        .bind(revision)
        .execute(uow.connection())
        .await?;
        enqueue_dashboard(uow, state.id.0, &revision.to_string()).await?;
    }
    Ok(())
}

/// Read a watched dashboard and its initial comment ID without holding a write lock.
pub async fn dashboard_surface(
    database: &Database,
    task_id: Uuid,
) -> Result<Option<DashboardSurface>, sqlx::Error> {
    let row: Option<(Uuid, i64, i64, i64, String, Uuid, Option<i64>)> = sqlx::query_as(
        "SELECT s.task_id,s.repository_id,s.issue_number,s.revision,s.desired_body, \
         s.create_id,c.comment_id FROM tasks.dashboard_surfaces s \
         JOIN tasks.publication_comments c ON c.id=s.create_id \
         WHERE s.task_id=$1 AND s.watch",
    )
    .bind(task_id)
    .fetch_optional(database.pool())
    .await?;
    Ok(row.map(
        |(task_id, repository_id, issue_number, revision, desired_body, create_id, comment_id)| {
            DashboardSurface {
                task_id,
                repository_id,
                issue_number,
                revision,
                desired_body,
                create_id,
                comment_id,
            }
        },
    ))
}

/// Record an edit before sending it, fenced to the latest dashboard revision.
pub async fn begin_dashboard_edit(
    uow: &mut UnitOfWork<'_>,
    surface: &DashboardSurface,
) -> Result<Option<i64>, sqlx::Error> {
    let current: Option<i64> = sqlx::query_scalar(
        "SELECT revision FROM tasks.dashboard_surfaces WHERE task_id=$1 \
         AND revision=$2 AND watch AND (retry_at IS NULL OR retry_at<=now()) FOR UPDATE",
    )
    .bind(surface.task_id)
    .bind(surface.revision)
    .fetch_optional(uow.connection())
    .await?;
    if current.is_none() {
        return Ok(None);
    }
    sqlx::query_scalar(
        "INSERT INTO tasks.dashboard_writes (task_id,revision,body,status) \
         VALUES ($1,$2,$3,'sending') RETURNING id",
    )
    .bind(surface.task_id)
    .bind(surface.revision)
    .bind(&surface.desired_body)
    .fetch_optional(uow.connection())
    .await
}

/// Settle a definite edit result while retaining any older uncertain write watch.
pub async fn finish_dashboard_edit(
    uow: &mut UnitOfWork<'_>,
    task_id: Uuid,
    write_id: i64,
    status: &str,
    failure: Option<&str>,
    retry_seconds: Option<u64>,
) -> Result<(), sqlx::Error> {
    let revision: Option<i64> = sqlx::query_scalar(
        "UPDATE tasks.dashboard_writes SET status=$3,failure=$4,updated_at=now() \
         WHERE id=$1 AND task_id=$2 AND status IN ('sending','obsolete_uncertain') \
         RETURNING revision",
    )
    .bind(write_id)
    .bind(task_id)
    .bind(status)
    .bind(failure)
    .fetch_optional(uow.connection())
    .await?;
    if let Some(revision) = revision {
        if status == "failed" {
            sqlx::query(
                "UPDATE tasks.dashboard_surfaces SET \
                 watch=$3='retryable_refusal' OR EXISTS(SELECT 1 FROM tasks.dashboard_writes w \
                     WHERE w.task_id=$1 AND w.status IN ('sending','obsolete_uncertain')), \
                 last_error=$3,retry_at=CASE WHEN $4::bigint IS NULL THEN NULL \
                     ELSE now()+$4::bigint * interval '1 second' END,updated_at=now() \
                 WHERE task_id=$1 AND revision=$2",
            )
            .bind(task_id)
            .bind(revision)
            .bind(failure)
            .bind(retry_seconds.map(|seconds| seconds.min(i32::MAX as u64) as i64))
            .execute(uow.connection())
            .await?;
        }
        enqueue_dashboard(uow, task_id, &format!("write:{write_id}")).await?;
    }
    Ok(())
}

/// Clear the watch only after a matching read and all older sends are accounted for.
pub async fn observe_dashboard(
    uow: &mut UnitOfWork<'_>,
    surface: &DashboardSurface,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE tasks.dashboard_surfaces SET \
         watch=EXISTS(SELECT 1 FROM tasks.dashboard_writes w WHERE w.task_id=$1 \
             AND w.status IN ('sending','obsolete_uncertain')), \
         last_error=NULL,retry_at=NULL,updated_at=now() \
         WHERE task_id=$1 AND revision=$2",
    )
    .bind(surface.task_id)
    .bind(surface.revision)
    .execute(uow.connection())
    .await?;
    Ok(())
}

/// Find dashboards still awaiting create, edit or uncertain-write readback.
pub async fn watched_dashboards(database: &Database) -> Result<Vec<Uuid>, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT task_id FROM tasks.dashboard_surfaces WHERE watch \
         AND (retry_at IS NULL OR retry_at<=now()) ORDER BY updated_at,task_id",
    )
    .fetch_all(database.pool())
    .await
}
