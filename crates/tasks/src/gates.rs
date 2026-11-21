//! Current PR and CI observation before a once-only ready-for-review write.

use std::collections::BTreeSet;

use anyhow::{Context, Result, ensure};
use concurro_db::Database;
use concurro_github_v1::{WriteFailure, classify_write_failure};
use concurro_tasks_core::{
    CiObservation, IssueKey, ci_reasons, editable_files, plan_scope_contains,
};
use concurro_tasks_postgres::{
    GateInputs, confirm_ready, fail_ready_intent, gate_inputs, mark_ready_sent, observe_ready_seen,
    record_gate_reason, record_terminal_pr, reserve_ready_intent, retry_ready_intent,
    terminal_pr_inputs,
};
use graphile_worker::{IntoTaskHandlerResult, TaskHandler, WorkerContext};
use reqwest::Method;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{GitPublicationContext, GitPublicationTarget, git_publication::pr_paths};

/// A queued current-head readiness observation for one contribution.
#[derive(Deserialize, Serialize)]
pub struct TaskGatesJob {
    /// Numeric issue repository ID.
    pub repository_id: i64,
    /// Repository-local issue number.
    pub issue_number: i64,
}

impl TaskHandler for TaskGatesJob {
    const IDENTIFIER: &'static str = "task_gates";

    /// Observe current GitHub state before considering the ready mutation.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let publication = context
            .get_ext::<GitPublicationContext>()
            .ok_or("PR gate context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_task_gates(
            &database,
            publication,
            IssueKey {
                repository_id: self.repository_id,
                number: self.issue_number,
            },
        )
        .await
        .map_err(|error| error.to_string())
    }
}

/// Read all pages of one GitHub REST list using the target's exact read permissions.
async fn pages(
    context: &GitPublicationContext,
    target: &GitPublicationTarget,
    segments: &[&str],
    field: Option<&str>,
    extra: &[(&str, &str)],
) -> Result<Vec<Value>> {
    let mut all = Vec::new();
    let mut page = 1;
    loop {
        let page_text = page.to_string();
        let mut query = extra.to_vec();
        query.extend([("per_page", "100"), ("page", page_text.as_str())]);
        let value: Value = context
            .github
            .read(
                &target.upstream,
                &target.upstream_read_permissions,
                segments,
                &query,
            )
            .await?;
        let items = field
            .map_or(&value, |name| &value[name])
            .as_array()
            .context("GitHub list response is incomplete")?;
        let more = items.len() == 100;
        all.extend(items.iter().cloned());
        if !more {
            return Ok(all);
        }
        page += 1;
    }
}

/// Read unresolved GitHub review threads, including every pagination cursor.
async fn unresolved_threads(
    context: &GitPublicationContext,
    target: &GitPublicationTarget,
    number: i64,
) -> Result<bool> {
    let (owner, repository) = target
        .upstream
        .split_once('/')
        .context("upstream must be owner/name")?;
    let mut cursor: Option<String> = None;
    let mut unresolved = false;
    loop {
        let value: Value = context.github.write(
            &target.upstream, &target.upstream_write_permissions, Method::POST, &["graphql"],
            Some(json!({
                "query":"query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$cursor){nodes{isResolved}pageInfo{hasNextPage endCursor}}}}}",
                "variables":{"owner":owner,"name":repository,"number":number,"cursor":cursor}
            })),
        ).await?;
        ensure!(
            value["errors"].is_null(),
            "GitHub review thread query failed"
        );
        let threads = &value["data"]["repository"]["pullRequest"]["reviewThreads"];
        let nodes = threads["nodes"]
            .as_array()
            .context("PR review threads are missing")?;
        unresolved |= nodes.iter().any(|node| node["isResolved"] != true);
        if threads["pageInfo"]["hasNextPage"] != true {
            return Ok(unresolved);
        }
        cursor = Some(
            threads["pageInfo"]["endCursor"]
                .as_str()
                .context("review thread cursor missing")?
                .to_owned(),
        );
    }
}

/// Collect every current check surface for the head and GitHub test-merge commit.
async fn ci_observations(
    context: &GitPublicationContext,
    target: &GitPublicationTarget,
    pr: &Value,
    head: &str,
) -> Result<(Vec<CiObservation>, Vec<String>)> {
    let (owner, repository) = target
        .upstream
        .split_once('/')
        .context("upstream must be owner/name")?;
    let mut refs = vec![head.to_owned()];
    let mut reasons = Vec::new();
    if pr["merged"] != true {
        if let Some(merge) = pr["merge_commit_sha"]
            .as_str()
            .filter(|merge| *merge != head)
        {
            let commit: Value = context
                .github
                .read(
                    &target.upstream,
                    &target.upstream_read_permissions,
                    &["repos", owner, repository, "git", "commits", merge],
                    &[],
                )
                .await?;
            let parents = commit["parents"]
                .as_array()
                .context("test-merge parents missing")?
                .iter()
                .filter_map(|parent| parent["sha"].as_str())
                .collect::<Vec<_>>();
            if parents != [pr["base"]["sha"].as_str().unwrap_or(""), head] {
                reasons.push("GitHub test-merge commit is not current".to_owned());
            }
            refs.push(merge.to_owned());
        }
    }
    let mut observations = Vec::with_capacity(refs.len());
    for sha in refs {
        let checks = pages(
            context,
            target,
            &["repos", owner, repository, "commits", &sha, "check-runs"],
            Some("check_runs"),
            &[("filter", "all")],
        )
        .await?;
        let statuses = pages(
            context,
            target,
            &["repos", owner, repository, "commits", &sha, "statuses"],
            None,
            &[],
        )
        .await?;
        let suites = pages(
            context,
            target,
            &["repos", owner, repository, "commits", &sha, "check-suites"],
            Some("check_suites"),
            &[],
        )
        .await?;
        let workflows = pages(
            context,
            target,
            &["repos", owner, repository, "actions", "runs"],
            Some("workflow_runs"),
            &[("head_sha", &sha)],
        )
        .await?;
        observations.push(CiObservation {
            sha,
            checks,
            statuses,
            suites,
            workflows,
        });
    }
    Ok((observations, reasons))
}

/// Check current PR identity, scope, findings and external review threads.
fn review_reasons(
    inputs: &GateInputs,
    target: &GitPublicationTarget,
    pr: &Value,
    paths: &BTreeSet<String>,
    threads_unresolved: bool,
) -> Result<Vec<String>> {
    let mut reasons = Vec::new();
    if pr["state"] != "open" {
        reasons.push("PR is closed".to_owned())
    }
    if pr["number"] != inputs.pr_number || pr["user"]["login"] != target.bot_login {
        reasons.push("PR identity changed".to_owned());
    }
    if pr["head"]["sha"] != inputs.head
        || pr["head"]["ref"] != inputs.branch
        || pr["head"]["repo"]["full_name"]
            .as_str()
            .is_none_or(|name| !name.eq_ignore_ascii_case(&target.fork))
    {
        reasons.push("PR head changed".to_owned());
    }
    if pr["base"]["ref"] != inputs.snapshot.configuration["base"] {
        reasons.push("PR base branch changed".to_owned());
    }
    if inputs.unresolved_findings {
        reasons.push("open or withdrawn review findings".to_owned())
    }
    if threads_unresolved {
        reasons.push("unresolved PR review threads".to_owned())
    }
    if *paths
        != inputs
            .changed_paths
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>()
    {
        reasons.push("PR file inventory changed".to_owned());
    }
    if editable_files(&inputs.snapshot.configuration, &inputs.changed_paths)?.len()
        != inputs.changed_paths.len()
    {
        reasons.push("PR contains a protected path".to_owned());
    }
    for path in &inputs.changed_paths {
        if !plan_scope_contains(&inputs.plan, path)? {
            reasons.push("PR contains a path outside the approved plan".to_owned());
            break;
        }
    }
    Ok(reasons)
}

/// Observe PR closure, then both CI refs and the once-only ready intent.
pub async fn run_task_gates(
    database: &Database,
    context: &GitPublicationContext,
    issue: IssueKey,
) -> Result<()> {
    let Some(terminal) = terminal_pr_inputs(database, issue).await? else {
        return Ok(());
    };
    let target = context
        .targets
        .get(&issue.repository_id)
        .context("PR gate target unavailable")?;
    let (owner, repository) = target
        .upstream
        .split_once('/')
        .context("upstream must be owner/name")?;
    let number = terminal.pr_number.to_string();
    let pr: Value = context
        .github
        .read(
            &target.upstream,
            &target.upstream_write_permissions,
            &["repos", owner, repository, "pulls", &number],
            &[],
        )
        .await?;
    if pr["state"] == "closed" {
        let latest: Value = context
            .github
            .read(
                &target.upstream,
                &target.upstream_write_permissions,
                &["repos", owner, repository, "pulls", &number],
                &[],
            )
            .await?;
        ensure!(
            latest == pr,
            "closed PR changed during terminal observation"
        );
        let identity_ok = pr["number"] == terminal.pr_number
            && pr["user"]["login"] == target.bot_login
            && pr["head"]["ref"] == terminal.branch
            && pr["head"]["repo"]["full_name"]
                .as_str()
                .is_some_and(|name| name.eq_ignore_ascii_case(&target.fork))
            && pr["base"]["ref"] == terminal.base;
        let mut work = database.begin().await?;
        record_terminal_pr(
            &mut work,
            &context.outbox,
            issue,
            terminal.state.revision,
            terminal.pr_number,
            &terminal.head,
            identity_ok,
            &pr,
        )
        .await?;
        work.commit().await?;
        return Ok(());
    }
    let Some(inputs) = gate_inputs(database, issue).await? else {
        return Ok(());
    };
    ensure!(
        inputs.pr_number == terminal.pr_number && inputs.head == terminal.head,
        "gate PR differs from the latest published review batch"
    );
    let paths = pr_paths(context, target, inputs.pr_number).await?;
    let threads = unresolved_threads(context, target, inputs.pr_number).await?;
    let mut reasons = review_reasons(&inputs, target, &pr, &paths, threads)?;
    let (observations, mut merge_reasons) =
        ci_observations(context, target, &pr, &inputs.head).await?;
    reasons.append(&mut merge_reasons);
    reasons.extend(ci_reasons(&observations));
    let latest: Value = context
        .github
        .read(
            &target.upstream,
            &target.upstream_write_permissions,
            &["repos", owner, repository, "pulls", &number],
            &[],
        )
        .await?;
    if latest["head"] != pr["head"]
        || latest["base"] != pr["base"]
        || latest["merge_commit_sha"] != pr["merge_commit_sha"]
    {
        reasons.push("PR changed while observing CI".to_owned());
    }
    reasons.extend(review_reasons(&inputs, target, &latest, &paths, threads)?);
    if latest["draft"] == false
        && latest["number"] == inputs.pr_number
        && latest["user"]["login"] == target.bot_login
        && latest["head"]["sha"] == inputs.head
        && latest["head"]["ref"] == inputs.branch
        && latest["head"]["repo"]["full_name"]
            .as_str()
            .is_some_and(|name| name.eq_ignore_ascii_case(&target.fork))
        && let Some(base) = latest["base"]["sha"].as_str()
    {
        let mut work = database.begin().await?;
        observe_ready_seen(
            &mut work,
            issue,
            inputs.state.revision,
            inputs.pr_number,
            &inputs.head,
            base,
            latest["merge_commit_sha"].as_str(),
        )
        .await?;
        work.commit().await?;
    }
    if latest["draft"] == true && inputs.ready_seen {
        reasons.push("human must mark the returned draft ready again".to_owned());
    }
    if !reasons.is_empty() {
        let reason = reasons
            .into_iter()
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect::<Vec<_>>()
            .join("; ");
        let mut work = database.begin().await?;
        record_gate_reason(
            &mut work,
            &context.outbox,
            issue,
            inputs.state.revision,
            &reason,
        )
        .await?;
        work.commit().await?;
        return Ok(());
    }
    let base = latest["base"]["sha"]
        .as_str()
        .context("PR base SHA missing")?;
    let merge = latest["merge_commit_sha"].as_str();
    if let Some((number, saved_base, saved_merge)) = &inputs.intent_identity
        && (*number != inputs.pr_number || saved_base != base || saved_merge.as_deref() != merge)
        && inputs.intent_status.as_deref() != Some("recorded")
    {
        let mut work = database.begin().await?;
        if latest["draft"] == false
            && matches!(inputs.intent_status.as_deref(), Some("sent" | "confirmed"))
        {
            confirm_ready(
                &mut work,
                &context.outbox,
                issue,
                &inputs.head,
                &target.upstream,
            )
            .await?;
        } else {
            record_gate_reason(
                &mut work,
                &context.outbox,
                issue,
                inputs.state.revision,
                "ready intent refers to a prior PR base or test-merge commit",
            )
            .await?;
        }
        work.commit().await?;
        return Ok(());
    }
    let mut work = database.begin().await?;
    let reserved = reserve_ready_intent(
        &mut work,
        issue,
        inputs.state.revision,
        inputs.pr_number,
        &inputs.head,
        base,
        merge,
    )
    .await?;
    work.commit().await?;
    if !reserved {
        return Ok(());
    }
    if latest["draft"] == false {
        let mut work = database.begin().await?;
        confirm_ready(
            &mut work,
            &context.outbox,
            issue,
            &inputs.head,
            &target.upstream,
        )
        .await?;
        work.commit().await?;
        return Ok(());
    }
    if inputs.intent_status.as_deref() == Some("sent") {
        let mut work = database.begin().await?;
        record_gate_reason(
            &mut work,
            &context.outbox,
            issue,
            inputs.state.revision,
            "mark-ready response is awaiting PR readback",
        )
        .await?;
        work.commit().await?;
        return Ok(());
    }
    let preflight: Value = context
        .github
        .read(
            &target.upstream,
            &target.upstream_write_permissions,
            &["repos", owner, repository, "pulls", &number],
            &[],
        )
        .await?;
    if preflight["head"] != latest["head"]
        || preflight["base"] != latest["base"]
        || preflight["merge_commit_sha"] != latest["merge_commit_sha"]
        || preflight["state"] != "open"
    {
        return Ok(());
    }
    let mut work = database.begin().await?;
    let sent = mark_ready_sent(&mut work, issue, &inputs.head).await?;
    work.commit().await?;
    if !sent {
        return Ok(());
    }
    let node = preflight["node_id"]
        .as_str()
        .context("PR node ID missing")?;
    let mutation: Result<Value> = context.github.write(
        &target.upstream, &target.upstream_write_permissions, Method::POST, &["graphql"],
        Some(json!({
            "query":"mutation($input:MarkPullRequestReadyForReviewInput!){markPullRequestReadyForReview(input:$input){pullRequest{id isDraft headRefOid}}}",
            "variables":{"input":{"pullRequestId":node}}
        })),
    ).await;
    let observed: Value = context
        .github
        .read(
            &target.upstream,
            &target.upstream_write_permissions,
            &["repos", owner, repository, "pulls", &number],
            &[],
        )
        .await?;
    if observed["draft"] == false && observed["head"]["sha"] == inputs.head {
        let mut work = database.begin().await?;
        confirm_ready(
            &mut work,
            &context.outbox,
            issue,
            &inputs.head,
            &target.upstream,
        )
        .await?;
        work.commit().await?;
    } else if let Err(error) = mutation {
        match classify_write_failure(&error) {
            WriteFailure::Definitive(status) => {
                let mut work = database.begin().await?;
                fail_ready_intent(
                    &mut work,
                    &context.outbox,
                    issue,
                    &inputs.head,
                    &format!("HTTP {status}"),
                )
                .await?;
                work.commit().await?;
            }
            WriteFailure::Retryable(_, _) => {
                let mut work = database.begin().await?;
                retry_ready_intent(&mut work, issue, &inputs.head).await?;
                work.commit().await?;
                return Err(error);
            }
            WriteFailure::Unknown => {}
        }
    } else if let Ok(value) = mutation
        && value["data"]["markPullRequestReadyForReview"].is_null()
        && let Some(errors) = value["errors"]
            .as_array()
            .filter(|errors| !errors.is_empty())
    {
        let recognized = errors.iter().all(|error| {
            matches!(
                error["type"]
                    .as_str()
                    .or_else(|| error["extensions"]["code"].as_str()),
                Some("FORBIDDEN" | "UNPROCESSABLE" | "NOT_FOUND")
            )
        });
        if recognized {
            let mut work = database.begin().await?;
            fail_ready_intent(
                &mut work,
                &context.outbox,
                issue,
                &inputs.head,
                "GraphQL rejected mark-ready",
            )
            .await?;
            work.commit().await?;
        }
    }
    Ok(())
}
