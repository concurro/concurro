//! Durable desired stage labels and observable physical GitHub writes.

use concurro_db::{Database, UnitOfWork};
use concurro_tasks_core::TaskState;
use uuid::Uuid;

/// Latest desired stage label for one task issue.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StageLabelSurface {
    /// Task that owns this managed display label.
    pub task_id: Uuid,
    /// Numeric repository selected by the task.
    pub repository_id: i64,
    /// Issue receiving the managed label.
    pub issue_number: i64,
    /// Latest durable task revision.
    pub revision: i64,
    /// Label wanted on the issue, or no stage label during initialization.
    pub desired: Option<String>,
}

/// Schedule a serialized label reconciliation for one task surface.
pub async fn enqueue_stage_label(
    uow: &mut UnitOfWork<'_>,
    task_id: Uuid,
    suffix: &str,
) -> Result<(), sqlx::Error> {
    let queue = format!("stage-label:{task_id}");
    let key = format!("stage-label:{task_id}:{suffix}");
    let _: i64 = sqlx::query_scalar(
        "SELECT (graphile_worker.add_job( \
         identifier => 'stage_label',payload => json_build_object('task_id',$1), \
         queue_name => $2,max_attempts => 25,job_key => $3)).id",
    )
    .bind(task_id)
    .bind(queue)
    .bind(key)
    .fetch_one(uow.connection())
    .await?;
    Ok(())
}

/// Record the newest label desire in the task-state transaction.
pub async fn record_stage_label_desire(
    uow: &mut UnitOfWork<'_>,
    state: &TaskState,
    stage: Option<&str>,
) -> Result<(), sqlx::Error> {
    let desired = stage.map(|stage| format!("concurro:stage:{}", stage.replace('_', "-")));
    let revision = i64::try_from(state.revision).expect("task revision fits bigint");
    let changed: Option<Uuid> = sqlx::query_scalar(
        "INSERT INTO tasks.stage_label_surfaces \
         (task_id,repository_id,issue_number,revision,desired) VALUES ($1,$2,$3,$4,$5) \
         ON CONFLICT (task_id) DO UPDATE SET revision=EXCLUDED.revision, \
         desired=EXCLUDED.desired,watch=true,last_error=NULL,retry_at=NULL,updated_at=now() \
         WHERE tasks.stage_label_surfaces.revision<EXCLUDED.revision RETURNING task_id",
    )
    .bind(state.id.0)
    .bind(state.issue.repository_id)
    .bind(state.issue.number)
    .bind(revision)
    .bind(desired)
    .fetch_optional(uow.connection())
    .await?;
    if changed.is_some() {
        sqlx::query(
            "UPDATE tasks.stage_label_writes SET status='obsolete_uncertain',updated_at=now() \
             WHERE task_id=$1 AND revision<$2 AND status='sending'",
        )
        .bind(state.id.0)
        .bind(revision)
        .execute(uow.connection())
        .await?;
        enqueue_stage_label(uow, state.id.0, &revision.to_string()).await?;
    }
    Ok(())
}

/// Read the current desired label without holding a row lock across GitHub reads.
pub async fn stage_label_surface(
    database: &Database,
    task_id: Uuid,
) -> Result<Option<StageLabelSurface>, sqlx::Error> {
    let row: Option<(Uuid, i64, i64, i64, Option<String>)> = sqlx::query_as(
        "SELECT task_id,repository_id,issue_number,revision,desired \
         FROM tasks.stage_label_surfaces WHERE task_id=$1 AND watch \
         AND (retry_at IS NULL OR retry_at<=now())",
    )
    .bind(task_id)
    .fetch_optional(database.pool())
    .await?;
    Ok(row.map(
        |(task_id, repository_id, issue_number, revision, desired)| StageLabelSurface {
            task_id,
            repository_id,
            issue_number,
            revision,
            desired,
        },
    ))
}

/// Record a physical write before its HTTP request, fenced to the desired revision.
pub async fn begin_stage_label_write(
    uow: &mut UnitOfWork<'_>,
    surface: &StageLabelSurface,
    action: &str,
    label: &str,
) -> Result<Option<i64>, sqlx::Error> {
    let current: Option<i64> = sqlx::query_scalar(
        "SELECT revision FROM tasks.stage_label_surfaces \
         WHERE task_id=$1 AND revision=$2 AND watch \
         AND (retry_at IS NULL OR retry_at<=now()) FOR UPDATE",
    )
    .bind(surface.task_id)
    .bind(surface.revision)
    .fetch_optional(uow.connection())
    .await?;
    if current.is_none() {
        return Ok(None);
    }
    sqlx::query_scalar(
        "INSERT INTO tasks.stage_label_writes (task_id,revision,action,label,status) \
         VALUES ($1,$2,$3,$4,'sending') RETURNING id",
    )
    .bind(surface.task_id)
    .bind(surface.revision)
    .bind(action)
    .bind(label)
    .fetch_optional(uow.connection())
    .await
}

/// Record a definite write result and schedule a fresh read of the current desire.
pub async fn finish_stage_label_write(
    uow: &mut UnitOfWork<'_>,
    write_id: i64,
    task_id: Uuid,
    status: &str,
    failure: Option<&str>,
    retry_seconds: Option<u64>,
) -> Result<(), sqlx::Error> {
    let revision: i64 = sqlx::query_scalar(
        "UPDATE tasks.stage_label_writes SET status=$2,failure=$3,updated_at=now() \
         WHERE id=$1 AND task_id=$4 AND status IN ('sending','obsolete_uncertain') \
         RETURNING revision",
    )
    .bind(write_id)
    .bind(status)
    .bind(failure)
    .bind(task_id)
    .fetch_one(uow.connection())
    .await?;
    if status == "failed" {
        sqlx::query(
            "UPDATE tasks.stage_label_surfaces SET \
             watch=$4::bigint IS NOT NULL OR EXISTS(SELECT 1 FROM tasks.stage_label_writes w WHERE w.task_id=$1 \
                 AND w.status IN ('sending','obsolete_uncertain')), \
             last_error=$3,retry_at=CASE WHEN $4::bigint IS NULL THEN NULL \
                 ELSE now()+$4::bigint * interval '1 second' END, \
             updated_at=now() WHERE task_id=$1 AND revision=$2",
        )
        .bind(task_id)
        .bind(revision)
        .bind(failure)
        .bind(retry_seconds.map(|seconds| seconds.min(i32::MAX as u64) as i64))
        .execute(uow.connection())
        .await?;
    } else {
        enqueue_stage_label(uow, task_id, &format!("write:{write_id}")).await?;
    }
    Ok(())
}

/// Stop routine reads only when the desired state matches and no send is uncertain.
pub async fn observe_stage_label(
    uow: &mut UnitOfWork<'_>,
    surface: &StageLabelSurface,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "UPDATE tasks.stage_label_surfaces SET \
         watch=EXISTS(SELECT 1 FROM tasks.stage_label_writes w \
             WHERE w.task_id=$1 AND w.status IN ('sending','obsolete_uncertain')), \
         retry_at=NULL,updated_at=now() WHERE task_id=$1 AND revision=$2",
    )
    .bind(surface.task_id)
    .bind(surface.revision)
    .execute(uow.connection())
    .await?;
    Ok(())
}

/// Find surfaces whose current or uncertain writes still need a GitHub readback.
pub async fn watched_stage_labels(database: &Database) -> Result<Vec<Uuid>, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT task_id FROM tasks.stage_label_surfaces WHERE watch \
         AND (retry_at IS NULL OR retry_at<=now()) ORDER BY updated_at,task_id",
    )
    .fetch_all(database.pool())
    .await
}
