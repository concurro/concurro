//! Managed issue stage-label publication with durable desired-state readback.

use anyhow::{Context, Result};
use concurro_contracts::Label;
use concurro_db::Database;
use concurro_github_v1::{WriteFailure, classify_write_failure, response_status};
use concurro_tasks_postgres::{
    begin_stage_label_write, finish_stage_label_write, observe_stage_label, stage_label_surface,
};
use graphile_worker::{IntoTaskHandlerResult, TaskHandler, WorkerContext};
use reqwest::{Method, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use uuid::Uuid;

use crate::PublicationContext;

/// One managed stage label operation selected from a fresh issue read.
enum StageLabelAction {
    /// Create the repository label before attaching it to the issue.
    Create(String),
    /// Attach the desired label to the task's issue.
    Add(String),
    /// Remove an obsolete Concurro stage label from the task's issue.
    Remove(String),
}

/// Serialized reconciliation of one task's managed stage label.
#[derive(Deserialize, Serialize)]
pub struct StageLabelJob {
    /// Task whose latest stage is desired on its issue.
    pub task_id: Uuid,
}

impl TaskHandler for StageLabelJob {
    const IDENTIFIER: &'static str = "stage_label";

    /// Read the current issue and take at most one recorded physical action.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let publication = context
            .get_ext::<PublicationContext>()
            .ok_or("publication context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        publish_stage_label(&database, publication, self.task_id)
            .await
            .map_err(|error| error.to_string())
    }
}

/// Reconcile the current stage, keeping uncertain physical sends under a durable watch.
pub async fn publish_stage_label(
    database: &Database,
    context: &PublicationContext,
    task_id: Uuid,
) -> Result<()> {
    let Some(surface) = stage_label_surface(database, task_id).await? else {
        return Ok(());
    };
    let target = context
        .targets
        .get(&surface.repository_id)
        .context("stage label target unavailable")?;
    let (owner, name) = target
        .repository
        .split_once('/')
        .context("stage label repository must be owner/name")?;
    let issue = surface.issue_number.to_string();
    let mut labels = Vec::<Label>::new();
    let mut page = 1;
    loop {
        let page_number = page.to_string();
        let batch: Vec<Label> = context
            .github
            .read(
                &target.repository,
                &target.read_permissions,
                &["repos", owner, name, "issues", &issue, "labels"],
                &[("per_page", "100"), ("page", &page_number)],
            )
            .await?;
        let count = batch.len();
        labels.extend(batch);
        if count < 100 {
            break;
        }
        page += 1;
    }
    let mut current = labels
        .iter()
        .map(|label| label.name.as_str())
        .filter(|name| name.starts_with("concurro:stage:"))
        .collect::<Vec<_>>();
    current.sort_unstable();
    let action = if let Some(desired) = surface.desired.as_deref() {
        if !current.contains(&desired) {
            let label: Result<Value> = context
                .github
                .read(
                    &target.repository,
                    &target.read_permissions,
                    &["repos", owner, name, "labels", desired],
                    &[],
                )
                .await;
            match label {
                Ok(_) => Some(StageLabelAction::Add(desired.to_owned())),
                Err(error) if response_status(&error) == Some(StatusCode::NOT_FOUND) => {
                    Some(StageLabelAction::Create(desired.to_owned()))
                }
                Err(error) => return Err(error),
            }
        } else {
            current
                .into_iter()
                .find(|name| *name != desired)
                .map(|name| StageLabelAction::Remove(name.to_owned()))
        }
    } else {
        current
            .into_iter()
            .next()
            .map(|name| StageLabelAction::Remove(name.to_owned()))
    };
    let Some(action) = action else {
        let mut observed = database.begin().await?;
        observe_stage_label(&mut observed, &surface).await?;
        observed.commit().await?;
        return Ok(());
    };
    let token = context
        .github
        .token(&target.repository, &target.write_permissions)
        .await?;
    let (verb, label) = match &action {
        StageLabelAction::Create(label) => ("create", label.as_str()),
        StageLabelAction::Add(label) => ("add", label.as_str()),
        StageLabelAction::Remove(label) => ("remove", label.as_str()),
    };
    let mut begin = database.begin().await?;
    let write_id = begin_stage_label_write(&mut begin, &surface, verb, label).await?;
    begin.commit().await?;
    let Some(write_id) = write_id else {
        return Ok(());
    };
    let response: Result<Value> = match &action {
        StageLabelAction::Create(label) => {
            context
                .github
                .write_prepared(
                    &target.repository,
                    &target.write_permissions,
                    &token,
                    Method::POST,
                    &["repos", owner, name, "labels"],
                    Some(json!({"name":label,"color":"8250df","description":"Concurro progress display; does not grant execution or merge approval."})),
                )
                .await
        }
        StageLabelAction::Add(label) => {
            context
                .github
                .write_prepared(
                    &target.repository,
                    &target.write_permissions,
                    &token,
                    Method::POST,
                    &["repos", owner, name, "issues", &issue, "labels"],
                    Some(json!({"labels":[label]})),
                )
                .await
        }
        StageLabelAction::Remove(label) => {
            context
                .github
                .write_prepared(
                    &target.repository,
                    &target.write_permissions,
                    &token,
                    Method::DELETE,
                    &["repos", owner, name, "issues", &issue, "labels", label],
                    None,
                )
                .await
        }
    };
    let result = match response {
        Err(error)
            if matches!(action, StageLabelAction::Remove(_))
                && response_status(&error) == Some(StatusCode::NOT_FOUND) =>
        {
            Ok(())
        }
        Err(error)
            if matches!(action, StageLabelAction::Create(_))
                && response_status(&error) == Some(StatusCode::UNPROCESSABLE_ENTITY) =>
        {
            context
                .github
                .read::<Value>(
                    &target.repository,
                    &target.read_permissions,
                    &["repos", owner, name, "labels", label],
                    &[],
                )
                .await
                .map(|_| ())
        }
        Ok(_) => Ok(()),
        Err(error) => Err(error),
    };
    match result {
        Ok(()) => {
            let mut settled = database.begin().await?;
            finish_stage_label_write(&mut settled, write_id, task_id, "written", None, None)
                .await?;
            settled.commit().await?;
            Ok(())
        }
        Err(error) => {
            if matches!(classify_write_failure(&error), WriteFailure::Definitive(..)) {
                let mut settled = database.begin().await?;
                finish_stage_label_write(
                    &mut settled,
                    write_id,
                    task_id,
                    "failed",
                    Some("github_client_refusal"),
                    None,
                )
                .await?;
                settled.commit().await?;
                Ok(())
            } else if let WriteFailure::Retryable(_, delay) = classify_write_failure(&error) {
                let mut settled = database.begin().await?;
                finish_stage_label_write(
                    &mut settled,
                    write_id,
                    task_id,
                    "failed",
                    Some("retryable_refusal"),
                    Some(delay),
                )
                .await?;
                settled.commit().await?;
                Ok(())
            } else {
                Err(error)
            }
        }
    }
}
