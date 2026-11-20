//! Managed issue dashboard creation, revision edits and late-write readback.

use anyhow::{Context, Result, ensure};
use concurro_contracts::GithubComment;
use concurro_db::Database;
use concurro_github_v1::{WriteFailure, classify_write_failure};
use concurro_tasks_postgres::{
    begin_dashboard_edit, dashboard_surface, finish_dashboard_edit, observe_dashboard,
};
use graphile_worker::{IntoTaskHandlerResult, TaskHandler, WorkerContext};
use reqwest::Method;
use serde::{Deserialize, Serialize};
use serde_json::json;
use uuid::Uuid;

use crate::{PublicationContext, publish_request_reply};

/// Serialized reconciliation of a task's single dashboard comment.
#[derive(Deserialize, Serialize)]
pub struct DashboardJob {
    /// Task whose dashboard must match its latest state revision.
    pub task_id: Uuid,
}

impl TaskHandler for DashboardJob {
    const IDENTIFIER: &'static str = "dashboard";

    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let database = Database::from_pool(context.pg_pool().clone());
        let publication = context
            .get_ext::<PublicationContext>()
            .expect("publication context");
        publish_dashboard(&database, publication, self.task_id)
            .await
            .map_err(|error| error.to_string())
    }
}

/// Create the managed comment once, then converge its body to the newest task revision.
pub async fn publish_dashboard(
    database: &Database,
    context: &PublicationContext,
    task_id: Uuid,
) -> Result<()> {
    let Some(surface) = dashboard_surface(database, task_id).await? else {
        return Ok(());
    };
    let Some(comment_id) = surface.comment_id else {
        publish_request_reply(database, context, surface.create_id).await?;
        return Ok(());
    };
    let target = context
        .targets
        .get(&surface.repository_id)
        .context("dashboard target unavailable")?;
    let (owner, name) = target
        .repository
        .split_once('/')
        .context("dashboard repository must be owner/name")?;
    let id = comment_id.to_string();
    let comment: GithubComment = context
        .github
        .read(
            &target.repository,
            &target.read_permissions,
            &["repos", owner, name, "issues", "comments", &id],
            &[],
        )
        .await?;
    ensure!(
        comment.id == comment_id
            && comment.user.login.eq_ignore_ascii_case(&target.bot_login)
            && comment
                .body
                .as_deref()
                .is_some_and(|body| body.starts_with("<!-- concurro:v1 -->")),
        "dashboard comment identity changed"
    );
    if comment.body.as_deref() == Some(&surface.desired_body) {
        let mut work = database.begin().await?;
        observe_dashboard(&mut work, &surface).await?;
        work.commit().await?;
        return Ok(());
    }
    let token = context
        .github
        .token(&target.repository, &target.write_permissions)
        .await?;
    let mut work = database.begin().await?;
    let write_id = begin_dashboard_edit(&mut work, &surface).await?;
    work.commit().await?;
    let Some(write_id) = write_id else {
        return Ok(());
    };
    let response: Result<GithubComment> = context
        .github
        .write_prepared(
            &target.repository,
            &target.write_permissions,
            &token,
            Method::PATCH,
            &["repos", owner, name, "issues", "comments", &id],
            Some(json!({"body":surface.desired_body})),
        )
        .await;
    match response {
        Ok(updated) => {
            ensure!(
                updated.id == comment_id
                    && updated.user.login.eq_ignore_ascii_case(&target.bot_login)
                    && updated.body.as_deref() == Some(&surface.desired_body),
                "GitHub dashboard edit differs from recorded write"
            );
            let mut work = database.begin().await?;
            finish_dashboard_edit(&mut work, task_id, write_id, "written", None, None).await?;
            work.commit().await?;
            Ok(())
        }
        Err(error) if matches!(classify_write_failure(&error), WriteFailure::Retryable(..)) => {
            let WriteFailure::Retryable(_, delay) = classify_write_failure(&error) else {
                unreachable!()
            };
            let mut work = database.begin().await?;
            finish_dashboard_edit(
                &mut work,
                task_id,
                write_id,
                "failed",
                Some("retryable_refusal"),
                Some(delay),
            )
            .await?;
            work.commit().await?;
            Ok(())
        }
        Err(error) if matches!(classify_write_failure(&error), WriteFailure::Definitive(..)) => {
            let mut work = database.begin().await?;
            finish_dashboard_edit(
                &mut work,
                task_id,
                write_id,
                "failed",
                Some("github_client_refusal"),
                None,
            )
            .await?;
            work.commit().await?;
            Ok(())
        }
        Err(error) => Err(error),
    }
}
