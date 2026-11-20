//! Config-pinned runner refs and one-send workflow dispatch publication.

use std::{
    collections::{BTreeMap, HashMap},
    fmt,
};

use anyhow::{Context, Result, ensure};
use concurro_db::Database;
use concurro_exchange_v1::TurnSubmission;
use concurro_github_v1::{AppClient, WriteFailure, classify_write_failure, response_status};
use concurro_tasks_postgres::{
    begin_ref_write, claim_dispatch, fail_dispatch, finish_ref_write, observe_dispatch_run,
    observe_ref, refuse_ref_conflict, runner_session, runner_submission, settle_dispatch_sent,
};
use graphile_worker::{IntoTaskHandlerResult, TaskHandler, WorkerContext};
use reqwest::{Method, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use uuid::Uuid;

/// Exact fork and App permission scopes for one target's runner publication.
#[derive(Clone)]
pub struct RunnerTarget {
    /// Configured executor fork.
    pub fork: String,
    /// Exact read permissions for refs and workflow runs.
    pub read_permissions: BTreeMap<String, String>,
    /// Exact write permissions for refs and workflow dispatch.
    pub write_permissions: BTreeMap<String, String>,
}

/// Scoped GitHub client and runner targets indexed by issue repository ID.
#[derive(Clone)]
pub struct RunnerContext {
    /// GitHub App client for runner reads and writes.
    pub github: AppClient,
    /// Configured runner publication targets.
    pub targets: HashMap<i64, RunnerTarget>,
}

impl fmt::Debug for RunnerContext {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("RunnerContext")
            .field("target_count", &self.targets.len())
            .finish_non_exhaustive()
    }
}

/// Serialized reconciliation of one exchange runner session.
#[derive(Deserialize, Serialize)]
pub struct RunnerSessionJob {
    /// Exchange session whose ref and dispatch are being reconciled.
    pub session_id: Uuid,
}

impl TaskHandler for RunnerSessionJob {
    const IDENTIFIER: &'static str = "runner_session";

    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let database = Database::from_pool(context.pg_pool().clone());
        let runner = context.get_ext::<RunnerContext>().expect("runner context");
        publish_runner_session(&database, &runner, self.session_id)
            .await
            .map_err(|error| error.to_string())
    }
}

/// Reconcile one pinned ref and send each session's dispatch at most once.
pub async fn publish_runner_session(
    database: &Database,
    context: &RunnerContext,
    session_id: Uuid,
) -> Result<()> {
    let Some(session) = runner_session(database, session_id).await? else {
        return Ok(());
    };
    let target = context
        .targets
        .get(&session.repository_id)
        .context("runner target unavailable")?;
    ensure!(
        target.fork.eq_ignore_ascii_case(&session.fork),
        "runner fork differs from target"
    );
    let (owner, name) = session
        .fork
        .split_once('/')
        .context("runner fork must be owner/name")?;
    let branch = format!("concurro/runs/{session_id}");
    let full_ref = format!("refs/heads/{branch}");

    // A ref 404 is absence only after the fork itself was read successfully.
    let _: Value = context
        .github
        .read(
            &session.fork,
            &target.read_permissions,
            &["repos", owner, name],
            &[],
        )
        .await?;
    let head = match context
        .github
        .branch_head(&session.fork, &target.read_permissions, &branch)
        .await
    {
        Ok(sha) => Some(sha),
        Err(error) if response_status(&error) == Some(StatusCode::NOT_FOUND) => None,
        Err(error) => return Err(error),
    };
    if let Some(sha) = &head {
        if sha != &session.config_sha {
            let mut work = database.begin().await?;
            refuse_ref_conflict(&mut work, &session).await?;
            work.commit().await?;
            return Ok(());
        }
    }
    if session.desired_present != head.is_some() {
        let token = context
            .github
            .token(&session.fork, &target.write_permissions)
            .await?;
        let action = if session.desired_present {
            "create"
        } else {
            "delete"
        };
        let mut work = database.begin().await?;
        let write_id = begin_ref_write(&mut work, &session, action).await?;
        work.commit().await?;
        let Some(write_id) = write_id else {
            return Ok(());
        };
        let response: Result<Value> = if session.desired_present {
            context
                .github
                .write_prepared(
                    &session.fork,
                    &target.write_permissions,
                    &token,
                    Method::POST,
                    &["repos", owner, name, "git", "refs"],
                    Some(json!({"ref":full_ref,"sha":session.config_sha})),
                )
                .await
        } else {
            let mut segments = vec!["repos", owner, name, "git", "refs", "heads"];
            segments.extend(branch.split('/'));
            context
                .github
                .write_prepared(
                    &session.fork,
                    &target.write_permissions,
                    &token,
                    Method::DELETE,
                    &segments,
                    None,
                )
                .await
        };
        match response {
            Ok(_) => {
                let mut work = database.begin().await?;
                finish_ref_write(&mut work, session_id, write_id, "written", None, None).await?;
                work.commit().await?;
                return Ok(());
            }
            Err(error)
                if response_status(&error) == Some(StatusCode::NOT_FOUND)
                    && !session.desired_present =>
            {
                let mut work = database.begin().await?;
                finish_ref_write(&mut work, session_id, write_id, "written", None, None).await?;
                work.commit().await?;
                return Ok(());
            }
            Err(error) if matches!(classify_write_failure(&error), WriteFailure::Retryable(..)) => {
                let WriteFailure::Retryable(_, delay) = classify_write_failure(&error) else {
                    unreachable!()
                };
                let mut work = database.begin().await?;
                finish_ref_write(
                    &mut work,
                    session_id,
                    write_id,
                    "failed",
                    Some("retryable_refusal"),
                    Some(delay),
                )
                .await?;
                work.commit().await?;
                return Ok(());
            }
            Err(error)
                if matches!(classify_write_failure(&error), WriteFailure::Definitive(..)) =>
            {
                let mut work = database.begin().await?;
                finish_ref_write(
                    &mut work,
                    session_id,
                    write_id,
                    "failed",
                    Some("github_client_refusal"),
                    None,
                )
                .await?;
                work.commit().await?;
                return Ok(());
            }
            Err(error) => return Err(error),
        }
    }
    let mut work = database.begin().await?;
    observe_ref(&mut work, &session, head.is_some()).await?;
    work.commit().await?;

    if matches!(
        session.dispatch_status.as_str(),
        "sending" | "sent" | "obsolete_uncertain"
    ) {
        let mut page = 1;
        loop {
            let page_number = page.to_string();
            let runs: Value = context
                .github
                .read(
                    &session.fork,
                    &target.read_permissions,
                    &[
                        "repos",
                        owner,
                        name,
                        "actions",
                        "workflows",
                        "concurro-executor.yml",
                        "runs",
                    ],
                    &[
                        ("branch", &branch),
                        ("event", "workflow_dispatch"),
                        ("per_page", "100"),
                        ("page", &page_number),
                    ],
                )
                .await?;
            let items = runs["workflow_runs"]
                .as_array()
                .context("GitHub workflow run listing is invalid")?;
            if let Some(run_id) = items
                .iter()
                .find(|run| {
                    run["head_branch"] == branch
                        && run["head_sha"] == session.config_sha
                        && run["event"] == "workflow_dispatch"
                })
                .and_then(|run| run["id"].as_i64())
                .filter(|id| *id > 0)
            {
                let mut work = database.begin().await?;
                observe_dispatch_run(&mut work, session_id, run_id).await?;
                work.commit().await?;
                return Ok(());
            }
            if items.len() < 100 {
                break;
            }
            page += 1;
        }
        return Ok(());
    }
    if !session.desired_present || session.dispatch_status != "recorded" {
        return Ok(());
    }
    let token = context
        .github
        .token(&session.fork, &target.write_permissions)
        .await?;
    let mut work = database.begin().await?;
    let claimed = claim_dispatch(&mut work, session_id).await?;
    work.commit().await?;
    if !claimed {
        return Ok(());
    }
    let mut inputs = json!({"session":session_id.to_string()});
    if let Ok(submission) = serde_json::from_slice::<TurnSubmission>(
        &runner_submission(database, session.turn_id).await?,
    ) {
        let request = &submission.payload["request"];
        let binding = &submission.binding;
        let task_label = match (binding["repository"].as_str(), binding["issue"].as_i64()) {
            (Some(repository), Some(issue)) => format!("{repository}#{issue}"),
            _ => "Agent task".to_owned(),
        };
        let task_label = task_label.split_whitespace().collect::<Vec<_>>().join(" ");
        let operation = match request["stage"].as_str() {
            Some("intake") => "Task assessment",
            Some("plan") => "Design",
            Some("plan_qc") => "Design review",
            Some("develop") => "Implementation",
            Some("review") => "PR review",
            _ => "Agent task",
        };
        let operation = if request["evaluation"].as_bool() == Some(true) {
            format!("{operation} evaluation")
        } else if request["context"]["review_phase"] == "fresh" {
            format!("{operation} (fresh)")
        } else {
            operation.to_owned()
        };
        let model = request["model"].as_str().unwrap_or("Agent");
        let effort = request["effort"]
            .as_str()
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| {
                if request["provider"] == "claude" {
                    "medium"
                } else {
                    "low"
                }
            });
        let mut agent = format!("{model} · {effort} effort");
        if let Some(profile) = request["agent"]["profile"]
            .as_str()
            .filter(|profile| !profile.is_empty())
        {
            agent = format!("{profile} ({agent})");
        }
        inputs["task-label"] = json!(task_label);
        inputs["operation-label"] = json!(operation);
        inputs["agent-label"] = json!(agent.split_whitespace().collect::<Vec<_>>().join(" "));
    }
    let response: Result<Value> = context
        .github
        .write_prepared(
            &session.fork,
            &target.write_permissions,
            &token,
            Method::POST,
            &[
                "repos",
                owner,
                name,
                "actions",
                "workflows",
                "concurro-executor.yml",
                "dispatches",
            ],
            Some(json!({"ref":branch,"inputs":inputs})),
        )
        .await;
    match response {
        Ok(_) => {
            let mut work = database.begin().await?;
            settle_dispatch_sent(&mut work, session_id).await?;
            work.commit().await?;
            Ok(())
        }
        Err(error)
            if matches!(
                classify_write_failure(&error),
                WriteFailure::Definitive(..) | WriteFailure::Retryable(..)
            ) =>
        {
            let mut work = database.begin().await?;
            fail_dispatch(&mut work, session_id, "github_client_refusal").await?;
            work.commit().await?;
            Ok(())
        }
        Err(error) => Err(error),
    }
}
