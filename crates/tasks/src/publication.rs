//! Repository-scoped App replies with durable uncertain-create readback.

use std::{
    collections::{BTreeMap, HashMap},
    fmt,
};

use anyhow::{Context, Result, ensure};
use concurro_contracts::GithubComment;
use concurro_db::Database;
use concurro_github_v1::{AppClient, WriteFailure, classify_write_failure};
use concurro_tasks_postgres::{
    CommentWrite, claim_comment, comment_scope, defer_comment, fail_comment, settle_comment,
};
use graphile_worker::{IntoTaskHandlerResult, TaskHandler, WorkerContext};
use reqwest::Method;
use serde::{Deserialize, Serialize};
use serde_json::json;
use uuid::Uuid;

/// Exact repository and permission scopes for one target's issue replies.
#[derive(Clone)]
pub struct PublicationTarget {
    /// Issue repository receiving App comments.
    pub repository: String,
    /// Exact App permissions for readback.
    pub read_permissions: BTreeMap<String, String>,
    /// Exact App permissions for comment creation.
    pub write_permissions: BTreeMap<String, String>,
    /// App bot identity required for adoption.
    pub bot_login: String,
}

/// GitHub client and configured issue repositories for queued comment writes.
#[derive(Clone)]
pub struct PublicationContext {
    /// Scoped GitHub App client.
    pub github: AppClient,
    /// Targets indexed by numeric repository identity.
    pub targets: HashMap<i64, PublicationTarget>,
}

impl fmt::Debug for PublicationContext {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("PublicationContext")
            .field("target_count", &self.targets.len())
            .finish_non_exhaustive()
    }
}

/// One final request reply queued in the request's decision transaction.
#[derive(Deserialize, Serialize)]
pub struct PublishRequestReplyJob {
    /// Durable logical comment write.
    pub id: Uuid,
}

impl TaskHandler for PublishRequestReplyJob {
    const IDENTIFIER: &'static str = "publish_request_reply";

    /// Send once or read back a create whose response was lost.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let publication = context
            .get_ext::<PublicationContext>()
            .ok_or("publication context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        publish_request_reply(&database, publication, self.id)
            .await
            .map_err(|error| error.to_string())
    }
}

/// Find exactly one App-authored comment with this write's full marked body.
async fn readback_comment(
    github: &AppClient,
    target: &PublicationTarget,
    write: &CommentWrite,
) -> Result<GithubComment> {
    let (owner, name) = target
        .repository
        .split_once('/')
        .context("publication repository must be owner/name")?;
    let issue = write.issue.number.to_string();
    let mut page = 1;
    let mut found = None;
    loop {
        let current_page = page.to_string();
        let comments: Vec<GithubComment> = github
            .read(
                &target.repository,
                &target.read_permissions,
                &["repos", owner, name, "issues", &issue, "comments"],
                &[("per_page", "100"), ("page", &current_page)],
            )
            .await?;
        let count = comments.len();
        for comment in comments {
            if comment
                .body
                .as_deref()
                .is_some_and(|body| body.contains(&write.marker))
                && comment.user.login.eq_ignore_ascii_case(&target.bot_login)
            {
                ensure!(
                    comment.body.as_deref() == Some(write.body.as_str()),
                    "App comment with the reply marker was edited"
                );
                ensure!(
                    found.is_none(),
                    "reply marker appears in multiple App comments"
                );
                found = Some(comment);
            }
        }
        if count < 100 {
            break;
        }
        page += 1;
    }
    found.context("uncertain reply create remains pending readback")
}

/// Settle one final reply without resending an uncertain create.
pub async fn publish_request_reply(
    database: &Database,
    context: &PublicationContext,
    id: Uuid,
) -> Result<()> {
    let Some((repository_id, status)) = comment_scope(database, id).await? else {
        return Ok(());
    };
    let target = context
        .targets
        .get(&repository_id)
        .context("publication target unavailable")?;
    let prepared_token = if status == "recorded" {
        Some(
            context
                .github
                .token(&target.repository, &target.write_permissions)
                .await?,
        )
    } else {
        None
    };
    let mut claim = database.begin().await?;
    let write = claim_comment(&mut claim, id).await?;
    if write.as_ref().is_some_and(|write| !write.readback_only) && prepared_token.is_none() {
        claim.rollback().await?;
        anyhow::bail!("reply changed to a new send while preparing credentials");
    }
    claim.commit().await?;
    let Some(write) = write else {
        return Ok(());
    };
    ensure!(
        write.issue.repository_id == repository_id,
        "publication target changed"
    );
    let observed: Result<GithubComment> = if write.readback_only {
        readback_comment(&context.github, target, &write).await
    } else {
        let (owner, name) = target
            .repository
            .split_once('/')
            .context("publication repository must be owner/name")?;
        let issue = write.issue.number.to_string();
        context
            .github
            .write_prepared(
                &target.repository,
                &target.write_permissions,
                prepared_token.as_deref().expect("first send has token"),
                Method::POST,
                &["repos", owner, name, "issues", &issue, "comments"],
                Some(json!({"body": write.body})),
            )
            .await
    };
    let comment = match observed {
        Ok(comment) => comment,
        Err(error)
            if !write.readback_only
                && matches!(classify_write_failure(&error), WriteFailure::Retryable(..)) =>
        {
            let WriteFailure::Retryable(status, delay) = classify_write_failure(&error) else {
                unreachable!()
            };
            let mut deferred = database.begin().await?;
            defer_comment(
                &mut deferred,
                &write,
                &format!("http_{}", status.as_u16()),
                delay.min(i64::MAX as u64) as i64,
            )
            .await?;
            deferred.commit().await?;
            return Ok(());
        }
        Err(error)
            if !write.readback_only
                && matches!(classify_write_failure(&error), WriteFailure::Definitive(..)) =>
        {
            let WriteFailure::Definitive(status) = classify_write_failure(&error) else {
                unreachable!()
            };
            let mut failed = database.begin().await?;
            fail_comment(&mut failed, &write, &format!("http_{}", status.as_u16())).await?;
            failed.commit().await?;
            return Ok(());
        }
        Err(error) => return Err(error),
    };
    ensure!(
        comment.id > 0
            && comment.user.login.eq_ignore_ascii_case(&target.bot_login)
            && comment.body.as_deref() == Some(write.body.as_str()),
        "GitHub reply differs from the recorded App write"
    );
    let mut settled = database.begin().await?;
    settle_comment(&mut settled, &write, comment.id).await?;
    settled.commit().await?;
    Ok(())
}
