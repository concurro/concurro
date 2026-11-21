//! Native PR review publication with a durable send fence and exact readback.

use anyhow::{Context, Result, ensure};
use concurro_db::Database;
use concurro_github_v1::{WriteFailure, classify_write_failure};
use concurro_tasks_postgres::{
    PrReviewPublication, block_pr_review_batch, confirm_pr_review_publication,
    fail_pr_review_publication, mark_pr_review_sent, pr_review_publication,
    retry_pr_review_publication,
};
use graphile_worker::{IntoTaskHandlerResult, TaskHandler, WorkerContext};
use reqwest::Method;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use uuid::Uuid;

use crate::{GitPublicationContext, GitPublicationTarget};

/// A queued App-authored review write for one accepted reviewer report.
#[derive(Deserialize, Serialize)]
pub struct TaskPrReviewPublishJob {
    /// Reviewer turn whose exact body was frozen before this job.
    pub turn: Uuid,
}

impl TaskHandler for TaskPrReviewPublishJob {
    const IDENTIFIER: &'static str = "task_pr_review_publish";

    /// Send once, then reconcile every later run from GitHub's review list.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let publication = context
            .get_ext::<GitPublicationContext>()
            .ok_or("PR publication context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        publish_pr_review(&database, publication, self.turn)
            .await
            .map_err(|error| error.to_string())
    }
}

/// Recheck the PR's current owner, branch and head around a native review write.
pub(crate) async fn review_pr_current(
    context: &GitPublicationContext,
    target: &GitPublicationTarget,
    publication: &PrReviewPublication,
) -> Result<bool> {
    let (owner, repository) = target
        .upstream
        .split_once('/')
        .context("upstream must be owner/name")?;
    let number = publication.pr_number.to_string();
    let pr: Value = context
        .github
        .read(
            &target.upstream,
            &target.upstream_write_permissions,
            &["repos", owner, repository, "pulls", &number],
            &[],
        )
        .await?;
    Ok(pr["number"] == publication.pr_number
        && pr["state"] == "open"
        && pr["head"]["sha"] == publication.head
        && pr["head"]["ref"] == publication.branch
        && pr["head"]["repo"]["full_name"]
            .as_str()
            .is_some_and(|name| name.eq_ignore_ascii_case(&target.fork))
        && pr["user"]["login"] == target.bot_login)
}

/// Find exactly one submitted App review with this frozen marker, head and body.
pub(crate) async fn readback_pr_review(
    context: &GitPublicationContext,
    target: &GitPublicationTarget,
    publication: &PrReviewPublication,
) -> Result<Option<i64>> {
    let (owner, repository) = target
        .upstream
        .split_once('/')
        .context("upstream must be owner/name")?;
    let number = publication.pr_number.to_string();
    let mut found = None;
    let mut page = 1;
    loop {
        let reviews: Vec<Value> = context
            .github
            .read(
                &target.upstream,
                &target.upstream_write_permissions,
                &["repos", owner, repository, "pulls", &number, "reviews"],
                &[("per_page", "100"), ("page", &page.to_string())],
            )
            .await?;
        let more = reviews.len() == 100;
        for review in reviews {
            let Some(body) = review["body"].as_str() else {
                continue;
            };
            if !body.starts_with(&publication.marker) {
                continue;
            }
            if publication.threaded
                && review["state"] == "PENDING"
                && review["node_id"].as_str() == publication.review_node_id.as_deref()
                && body == publication.marker
                && review["commit_id"] == publication.head
                && review["user"]["login"] == target.bot_login
                && review["user"]["id"].as_i64() == publication.bot_user_id
            {
                continue;
            }
            ensure!(found.is_none(), "review marker appears more than once");
            ensure!(
                body == publication.body
                    && review["commit_id"] == publication.head
                    && review["state"] == "COMMENTED"
                    && review["user"]["login"] == target.bot_login
                    && review["user"]["id"].as_i64() == publication.bot_user_id,
                "App review with the recorded marker changed identity or body"
            );
            found = Some(
                review["id"]
                    .as_i64()
                    .context("matching review ID missing")?,
            );
        }
        if !more {
            return Ok(found);
        }
        page += 1;
    }
}

/// Send one frozen native PR review or keep reading back an uncertain POST.
pub async fn publish_pr_review(
    database: &Database,
    context: &GitPublicationContext,
    turn: Uuid,
) -> Result<()> {
    let Some(mut publication) = pr_review_publication(database, turn).await? else {
        return Ok(());
    };
    let target = context
        .targets
        .get(&publication.issue.repository_id)
        .context("PR review target unavailable")?;
    if publication.threaded {
        return crate::pr_review_threads::publish_threaded_review(
            database,
            context,
            target,
            publication,
        )
        .await;
    }
    if !review_pr_current(context, target, &publication).await? {
        let mut work = database.begin().await?;
        if publication.status == "sent" {
            fail_pr_review_publication(
                &mut work,
                &context.outbox,
                &publication,
                "PR head or identity changed before review confirmation",
            )
            .await?;
        } else {
            block_pr_review_batch(
                &mut work,
                &context.outbox,
                publication.issue,
                publication.batch_id,
                "PR head or identity changed before review publication",
            )
            .await?;
        }
        work.commit().await?;
        return Ok(());
    }
    if publication.status == "recorded" {
        let bot_user_id = context
            .github
            .bot_user_id(
                &target.upstream,
                &target.upstream_write_permissions,
                &target.bot_login,
            )
            .await?;
        let token = context
            .github
            .token(&target.upstream, &target.upstream_write_permissions)
            .await?;
        if let Some(existing) = readback_pr_review(
            context,
            target,
            &PrReviewPublication {
                bot_user_id: Some(bot_user_id),
                ..publication
            },
        )
        .await?
        {
            anyhow::bail!("unfenced review {existing} already exists before first send");
        }
        publication = pr_review_publication(database, turn)
            .await?
            .context("review publication disappeared before send")?;
        let mut work = database.begin().await?;
        let marked = mark_pr_review_sent(&mut work, turn, bot_user_id).await?;
        work.commit().await?;
        if !marked {
            return Ok(());
        }
        publication.status = "sent".to_owned();
        publication.bot_user_id = Some(bot_user_id);
        if !review_pr_current(context, target, &publication).await? {
            let mut work = database.begin().await?;
            fail_pr_review_publication(
                &mut work,
                &context.outbox,
                &publication,
                "PR head or identity changed before review send",
            )
            .await?;
            work.commit().await?;
            return Ok(());
        }
        let (owner, repository) = target
            .upstream
            .split_once('/')
            .context("upstream must be owner/name")?;
        let number = publication.pr_number.to_string();
        let sent: Result<Value> = context
            .github
            .write_prepared(
                &target.upstream,
                &target.upstream_write_permissions,
                &token,
                Method::POST,
                &["repos", owner, repository, "pulls", &number, "reviews"],
                Some(json!({
                    "commit_id":publication.head,
                    "event":"COMMENT",
                    "body":publication.body,
                })),
            )
            .await;
        if let Err(error) = sent {
            match classify_write_failure(&error) {
                WriteFailure::Definitive(status) => {
                    let mut work = database.begin().await?;
                    fail_pr_review_publication(
                        &mut work,
                        &context.outbox,
                        &publication,
                        &format!("HTTP {status}"),
                    )
                    .await?;
                    work.commit().await?;
                    return Ok(());
                }
                WriteFailure::Retryable(_, _) => {
                    let mut work = database.begin().await?;
                    retry_pr_review_publication(&mut work, turn).await?;
                    work.commit().await?;
                    return Err(error);
                }
                WriteFailure::Unknown => {}
            }
        }
    }
    let Some(review_id) = readback_pr_review(context, target, &publication).await? else {
        anyhow::bail!("uncertain PR review submission awaits exact App readback");
    };
    if !review_pr_current(context, target, &publication).await? {
        let mut work = database.begin().await?;
        fail_pr_review_publication(
            &mut work,
            &context.outbox,
            &publication,
            "PR head or identity changed after review submission",
        )
        .await?;
        work.commit().await?;
        return Ok(());
    }
    let mut work = database.begin().await?;
    confirm_pr_review_publication(&mut work, &context.outbox, turn, review_id).await?;
    work.commit().await?;
    Ok(())
}
