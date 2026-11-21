//! Shared turn ownership for contribution and standalone review lifecycles.

use concurro_db::UnitOfWork;
use uuid::Uuid;

/// The independent lifecycle row that owns one shared Exchange attempt.
pub(crate) enum ExecutionOwner {
    /// A contribution issue with its configured runner target identity.
    Contribution {
        repository_id: i64,
        issue_number: i64,
    },
    /// A standalone PR review with upstream and runner target identities.
    Standalone {
        task_id: Uuid,
        repository_id: i64,
        pr_number: i64,
        runner_repository_id: i64,
    },
}

impl ExecutionOwner {
    /// Resolve the configured target used by runner refs and dispatch.
    pub(crate) fn runner_repository_id(&self) -> i64 {
        match self {
            Self::Contribution { repository_id, .. } => *repository_id,
            Self::Standalone {
                runner_repository_id,
                ..
            } => *runner_repository_id,
        }
    }

    /// Resolve the issue or PR conversation used by archive and turn events.
    pub(crate) fn conversation(&self) -> (i64, i64) {
        match self {
            Self::Contribution {
                repository_id,
                issue_number,
            } => (*repository_id, *issue_number),
            Self::Standalone {
                repository_id,
                pr_number,
                ..
            } => (*repository_id, *pr_number),
        }
    }
}

/// Lock the owning lifecycle before a shared attempt's state or runner changes.
pub(crate) async fn lock_execution_owner(
    uow: &mut UnitOfWork<'_>,
    turn: Uuid,
) -> Result<Option<ExecutionOwner>, sqlx::Error> {
    let ids: Option<(Option<Uuid>, Option<Uuid>)> =
        sqlx::query_as("SELECT task_id,standalone_task_id FROM tasks.attempts WHERE id=$1")
            .bind(turn)
            .fetch_optional(uow.connection())
            .await?;
    let Some((contribution, standalone)) = ids else {
        return Ok(None);
    };
    match (contribution, standalone) {
        (Some(task), None) => {
            let issue: Option<(i64, i64)> = sqlx::query_as(
                "SELECT repository_id,issue_number FROM tasks.task_state WHERE task_id=$1",
            )
            .bind(task)
            .fetch_optional(uow.connection())
            .await?;
            let Some((repository_id, issue_number)) = issue else {
                return Ok(None);
            };
            sqlx::query(
                "SELECT 1 FROM tasks.issue_controls \
                 WHERE repository_id=$1 AND issue_number=$2 FOR UPDATE",
            )
            .bind(repository_id)
            .bind(issue_number)
            .fetch_one(uow.connection())
            .await?;
            let confirmed: Option<i64> = sqlx::query_scalar(
                "SELECT repository_id FROM tasks.task_state WHERE task_id=$1 FOR UPDATE",
            )
            .bind(task)
            .fetch_optional(uow.connection())
            .await?;
            Ok(confirmed.map(|repository_id| ExecutionOwner::Contribution {
                repository_id,
                issue_number,
            }))
        }
        (None, Some(task)) => {
            let row: Option<(i64, i64, i64)> = sqlx::query_as(
                "SELECT repository_id,pr_number,runner_repository_id \
                 FROM tasks.standalone_review_tasks WHERE id=$1 FOR UPDATE",
            )
            .bind(task)
            .fetch_optional(uow.connection())
            .await?;
            Ok(row.map(|(repository_id, pr_number, runner_repository_id)| {
                ExecutionOwner::Standalone {
                    task_id: task,
                    repository_id,
                    pr_number,
                    runner_repository_id,
                }
            }))
        }
        _ => unreachable!("attempt owner is constrained by schema"),
    }
}
