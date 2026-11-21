//! Complete report publication as immutable issue generic artifacts.

use anyhow::{Context, Result, ensure};
use concurro_db::Database;
use concurro_github_v1::{WriteFailure, classify_write_failure};
use concurro_tasks_postgres::{
    ReportArtifactWrite, confirm_report_artifact, fail_report_artifact, mark_report_artifact_sent,
    report_artifact_write, retry_report_artifact,
};
use graphile_worker::{IntoTaskHandlerResult, TaskHandler, WorkerContext};
use reqwest::Method;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use uuid::Uuid;

use crate::{PublicationContext, PublicationTarget};

/// Queue payload for one complete task report's visible artifact.
#[derive(Deserialize, Serialize)]
pub struct PublishTaskReportJob {
    /// Stable report identity.
    pub id: Uuid,
}

impl TaskHandler for PublishTaskReportJob {
    const IDENTIFIER: &'static str = "publish_task_report";

    /// Reconcile the exact App artifact before any new create.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let publication = context
            .get_ext::<PublicationContext>()
            .ok_or("report publication context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        publish_task_report(&database, publication, self.id)
            .await
            .map_err(|error| error.to_string())
    }
}

/// Find the one exact App-authored artifact, including its immutable first version.
async fn readback_report(
    context: &PublicationContext,
    target: &PublicationTarget,
    write: &ReportArtifactWrite,
) -> Result<Option<i64>> {
    let (owner, repository) = target
        .repository
        .split_once('/')
        .context("issue repository must be owner/name")?;
    let issue = write.issue.number.to_string();
    let mut page = 1;
    let mut found = None;
    loop {
        let page_text = page.to_string();
        let items: Value = context
            .github
            .read(
                &target.repository,
                &target.read_permissions,
                &["repos", owner, repository, "issues", &issue, "artifacts"],
                &[("per_page", "100"), ("page", &page_text)],
            )
            .await?;
        let items = items
            .as_array()
            .context("issue artifact list is incomplete")?;
        for item in items {
            if item["name"] != write.name
                || item["creator"]["login"]
                    .as_str()
                    .is_none_or(|login| !login.eq_ignore_ascii_case(&target.bot_login))
            {
                continue;
            }
            ensure!(found.is_none(), "duplicate App report artifacts");
            ensure!(
                item["body"] == write.body && item["type"] == "generic",
                "App report artifact changed"
            );
            let number = item["number"]
                .as_i64()
                .context("report artifact number is missing")?;
            let number_text = number.to_string();
            let detail: Value = context
                .github
                .read(
                    &target.repository,
                    &target.read_permissions,
                    &[
                        "repos",
                        owner,
                        repository,
                        "issues",
                        &issue,
                        "artifacts",
                        &number_text,
                    ],
                    &[],
                )
                .await?;
            let artifact = &detail["artifact"];
            let version = &detail["versions"][0];
            ensure!(
                artifact["id"] == item["id"]
                    && artifact["number"] == number
                    && artifact["name"] == write.name
                    && artifact["body"] == write.body
                    && artifact["type"] == "generic"
                    && artifact["updated_by_actor"]["login"]
                        .as_str()
                        .is_some_and(|login| login.eq_ignore_ascii_case(&target.bot_login))
                    && version["version"] == 1
                    && version["name"] == write.name
                    && version["body"] == write.body
                    && version["actor"]["login"]
                        .as_str()
                        .is_some_and(|login| login.eq_ignore_ascii_case(&target.bot_login)),
                "report artifact version changed"
            );
            found = Some(number);
        }
        if items.len() < 100 {
            return Ok(found);
        }
        page += 1;
    }
}

/// Send a recorded report once, or read back an uncertain create without resending.
pub async fn publish_task_report(
    database: &Database,
    context: &PublicationContext,
    id: Uuid,
) -> Result<()> {
    let Some(write) = report_artifact_write(database, id).await? else {
        return Ok(());
    };
    if matches!(write.status.as_str(), "confirmed" | "failed") {
        return Ok(());
    }
    let targets = if write.standalone_task_id.is_some() {
        &context.standalone_targets
    } else {
        &context.targets
    };
    let target = targets
        .get(&write.issue.repository_id)
        .context("report publication target unavailable")?;
    if let Some(number) = readback_report(context, target, &write).await? {
        let mut work = database.begin().await?;
        confirm_report_artifact(&mut work, id, number).await?;
        work.commit().await?;
        return Ok(());
    }
    if write.status == "sent" {
        anyhow::bail!("uncertain report artifact create awaits exact App readback");
    }
    if write.standalone_task_id.is_some() {
        let (owner, repository) = target
            .repository
            .split_once('/')
            .context("PR repository must be owner/name")?;
        let pr: Value = context
            .github
            .read(
                &target.repository,
                &target.read_permissions,
                &[
                    "repos",
                    owner,
                    repository,
                    "pulls",
                    &write.issue.number.to_string(),
                ],
                &[],
            )
            .await?;
        ensure!(
            pr["state"] == "open" && pr["merged"] != true,
            "standalone PR is closed; report artifact awaits reopening"
        );
    }
    let token = context
        .github
        .token(&target.repository, &target.write_permissions)
        .await?;
    let mut work = database.begin().await?;
    let sent = mark_report_artifact_sent(&mut work, id).await?;
    work.commit().await?;
    if !sent {
        return Ok(());
    }
    let (owner, repository) = target
        .repository
        .split_once('/')
        .context("issue repository must be owner/name")?;
    let issue = write.issue.number.to_string();
    let posted: Result<Value> = context
        .github
        .write_prepared(
            &target.repository,
            &target.write_permissions,
            &token,
            Method::POST,
            &["repos", owner, repository, "issues", &issue, "artifacts"],
            Some(json!({"type":"generic","name":write.name,"body":write.body})),
        )
        .await;
    if let Some(number) = readback_report(context, target, &write).await? {
        let mut work = database.begin().await?;
        confirm_report_artifact(&mut work, id, number).await?;
        work.commit().await?;
        return Ok(());
    }
    if let Err(error) = posted {
        match classify_write_failure(&error) {
            WriteFailure::Definitive(status) => {
                let mut work = database.begin().await?;
                fail_report_artifact(&mut work, id, &format!("HTTP {status}")).await?;
                work.commit().await?;
                return Ok(());
            }
            WriteFailure::Retryable(_, _) => {
                let mut work = database.begin().await?;
                retry_report_artifact(&mut work, id).await?;
                work.commit().await?;
                return Err(error);
            }
            WriteFailure::Unknown => return Err(error),
        }
    }
    anyhow::bail!("created report artifact is awaiting exact App readback")
}
