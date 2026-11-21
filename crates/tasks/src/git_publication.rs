//! Recoverable fork commit and upstream draft pull request publication.

use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    fmt,
};

use anyhow::{Context, Result, bail, ensure};
use concurro_db::Database;
use concurro_github_v1::{AppClient, WriteFailure, classify_write_failure, response_status};
use concurro_tasks_core::{
    ImplementationEdit, IssueKey, markdown_text, plan_scope_contains, task_branch,
};
use concurro_tasks_postgres::{
    GitPublicationInput, GitPublicationIntent, block_git_publication, complete_git_publication,
    git_publication_input, queue_implementation_started, reserve_git_publication,
    save_git_publication,
};
use graphile_worker::{IntoTaskHandlerResult, TaskHandler, WorkerContext};
use reqwest::{Method, StatusCode};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use uuid::Uuid;

use crate::initialization;

const INLINE_BLOB_LIMIT: usize = 64_000;

/// Repository identities and exact installation token permissions for Git publication.
#[derive(Clone)]
pub struct GitPublicationTarget {
    /// Repository containing the issue closed by the contribution.
    pub issue_repository: String,
    /// Executor fork receiving the branch and commit.
    pub fork: String,
    /// Upstream repository receiving the draft pull request.
    pub upstream: String,
    /// Read token permissions for the fork.
    pub fork_read_permissions: BTreeMap<String, String>,
    /// Write token permissions for the fork.
    pub fork_write_permissions: BTreeMap<String, String>,
    /// Read token permissions for source files in upstream.
    pub upstream_read_permissions: BTreeMap<String, String>,
    /// Write token permissions for upstream pull requests.
    pub upstream_write_permissions: BTreeMap<String, String>,
    /// App account that must own the recovered pull request.
    pub bot_login: String,
}

/// GitHub client, targets and outbox for queued contribution publication.
#[derive(Clone)]
pub struct GitPublicationContext {
    /// Repository-scoped GitHub App client.
    pub github: AppClient,
    /// Configured repositories by numeric issue repository ID.
    pub targets: HashMap<i64, GitPublicationTarget>,
    /// Task state event writer.
    pub outbox: concurro_events_postgres::Outbox,
}

impl fmt::Debug for GitPublicationContext {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("GitPublicationContext")
            .field("target_count", &self.targets.len())
            .finish_non_exhaustive()
    }
}

/// Reconcile one approved candidate with its task-owned fork branch and draft PR.
#[derive(Deserialize, Serialize)]
pub struct TaskGitPublicationJob {
    /// Issue repository identity.
    pub repository_id: i64,
    /// Issue number owning the candidate.
    pub issue_number: i64,
}

impl TaskHandler for TaskGitPublicationJob {
    const IDENTIFIER: &'static str = "task_git_publication";

    /// Resume recorded Git writes from their exact current readback.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let publication = context
            .get_ext::<GitPublicationContext>()
            .ok_or("Git publication context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        run_task_git_publication(
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

/// Reconcile branch, immutable Git objects, ref advance and draft PR in order.
pub async fn run_task_git_publication(
    database: &Database,
    context: &GitPublicationContext,
    issue: IssueKey,
) -> Result<()> {
    let Some(mut input) = git_publication_input(database, issue).await? else {
        return Ok(());
    };
    let target = context
        .targets
        .get(&issue.repository_id)
        .context("Git publication target unavailable")?;
    let repository = input.snapshot.configuration["repository"]
        .as_str()
        .unwrap_or(&target.issue_repository);
    let branch = task_branch(repository, issue.number)?;
    let parent = input.candidate["parent"]
        .as_str()
        .context("approved candidate parent missing")?
        .to_owned();
    let prior = input.prior_pr.clone();
    ensure!(
        prior.as_ref().map_or(
            parent == input.snapshot.pins.base_sha,
            |(_, head, saved_branch, _, _)| { parent == *head && branch == *saved_branch }
        ),
        "publication candidate parent differs from approved source"
    );
    if let Some((number, _, _, _, _)) = &prior {
        let existing = pr_paths(context, target, *number).await?;
        let in_scope = existing.iter().try_fold(true, |allowed, path| {
            Ok::<bool, anyhow::Error>(allowed && plan_scope_contains(&input.plan, path)?)
        })?;
        if !in_scope {
            block(
                database,
                context,
                input.turn,
                "draft PR contains a path outside the approved plan",
            )
            .await?;
            return Ok(());
        }
    }
    if input.intent.is_none() {
        if read_branch(context, target, &branch).await?
            != prior.as_ref().map(|(_, head, _, _, _)| head.clone())
        {
            block(
                database,
                context,
                input.turn,
                "implementation branch differs from its approved parent",
            )
            .await?;
            return Ok(());
        }
        let mut work = database.begin().await?;
        let reserved = reserve_git_publication(&mut work, input.turn, &branch, &parent).await?;
        work.commit().await?;
        if !reserved {
            return Ok(());
        }
        input = git_publication_input(database, issue)
            .await?
            .context("reserved publication disappeared")?;
    }
    let (owner, mut revision, mut intent) =
        input.intent.clone().context("publication intent missing")?;
    if owner != input.turn {
        block(
            database,
            context,
            input.turn,
            "publication intent belongs to another implementation attempt",
        )
        .await?;
        return Ok(());
    }
    ensure!(
        intent.branch == branch && intent.parent == parent,
        "publication branch or parent changed"
    );
    let description_only = input.candidate["edits"]
        .as_array()
        .is_some_and(Vec::is_empty);
    if description_only && prior.is_none() {
        block(
            database,
            context,
            input.turn,
            "description-only candidate has no published pull request",
        )
        .await?;
        return Ok(());
    }

    let mut head = read_branch(context, target, &branch).await?;
    if head.is_none() {
        if prior.is_some() {
            block(
                database,
                context,
                input.turn,
                "published implementation branch disappeared",
            )
            .await?;
            return Ok(());
        }
        if intent.branch_created {
            block(
                database,
                context,
                input.turn,
                "reserved implementation branch disappeared",
            )
            .await?;
            return Ok(());
        }
        let (owner, repository) = target
            .fork
            .split_once('/')
            .context("fork must be owner/name")?;
        let created: Result<Value> = context
            .github
            .write(
                &target.fork,
                &target.fork_write_permissions,
                Method::POST,
                &["repos", owner, repository, "git", "refs"],
                Some(json!({"ref":format!("refs/heads/{branch}"),"sha":parent})),
            )
            .await;
        head = read_branch(context, target, &branch).await?;
        if head.is_none() {
            if let Err(error) = created {
                if let WriteFailure::Definitive(status) = classify_write_failure(&error) {
                    block(
                        database,
                        context,
                        input.turn,
                        &format!("branch create refused by GitHub (HTTP {status})"),
                    )
                    .await?;
                    return Ok(());
                }
                return Err(error);
            }
            bail!("branch create was not confirmed by exact ref readback");
        }
    }
    let current = head.context("branch readback missing")?;
    if current != parent && intent.commit_sha.as_deref() != Some(current.as_str()) {
        block(
            database,
            context,
            input.turn,
            "implementation branch points to a foreign commit",
        )
        .await?;
        return Ok(());
    }
    if !intent.branch_created {
        intent.branch_created = true;
        persist_intent(database, input.turn, &mut revision, &intent).await?;
    }
    if description_only && intent.commit_sha.is_none() {
        intent.commit_sha = Some(parent.clone());
        intent.ref_advanced = true;
        persist_intent(database, input.turn, &mut revision, &intent).await?;
    }
    let mut notice = database.begin().await?;
    queue_implementation_started(&mut notice, input.turn, &target.fork, &branch).await?;
    notice.commit().await?;

    if intent.commit_sha.is_none() {
        if intent.bot_user_id.is_none() || intent.bot_login.is_none() {
            intent.bot_user_id = Some(
                context
                    .github
                    .bot_user_id(
                        &target.fork,
                        &target.fork_read_permissions,
                        &target.bot_login,
                    )
                    .await?,
            );
            intent.bot_login = Some(target.bot_login.clone());
            persist_intent(database, input.turn, &mut revision, &intent).await?;
        }
        ensure!(
            intent.bot_login.as_deref() == Some(target.bot_login.as_str()),
            "publication bot identity changed"
        );
        let commit = match create_candidate_commit(context, target, &input, &intent).await? {
            Ok(commit) => commit,
            Err(reason) => {
                block(database, context, input.turn, &reason).await?;
                return Ok(());
            }
        };
        intent.commit_sha = Some(commit);
        persist_intent(database, input.turn, &mut revision, &intent).await?;
    }
    let commit = intent
        .commit_sha
        .as_deref()
        .context("candidate commit missing")?;
    if !intent.ref_advanced {
        let head = read_branch(context, target, &branch).await?;
        match head.as_deref() {
            Some(sha) if sha == parent => {
                let (owner, repository) = target
                    .fork
                    .split_once('/')
                    .context("fork must be owner/name")?;
                let mut path = vec!["repos", owner, repository, "git", "refs", "heads"];
                path.extend(branch.split('/'));
                let updated: Result<Value> = context
                    .github
                    .write(
                        &target.fork,
                        &target.fork_write_permissions,
                        Method::PATCH,
                        &path,
                        Some(json!({"sha":commit,"force":false})),
                    )
                    .await;
                let confirmed = read_branch(context, target, &branch).await?;
                if confirmed.as_deref() != Some(commit) {
                    if let Err(error) = updated {
                        if let WriteFailure::Definitive(status) = classify_write_failure(&error) {
                            block(
                                database,
                                context,
                                input.turn,
                                &format!("branch advance refused by GitHub (HTTP {status})"),
                            )
                            .await?;
                            return Ok(());
                        }
                        return Err(error);
                    }
                    bail!("branch advance was not confirmed by exact ref readback");
                }
            }
            Some(sha) if sha == commit => {}
            _ => {
                block(
                    database,
                    context,
                    input.turn,
                    "implementation branch changed before publication",
                )
                .await?;
                return Ok(());
            }
        }
        intent.ref_advanced = true;
        persist_intent(database, input.turn, &mut revision, &intent).await?;
    }

    if intent.pr_title.is_none() || intent.pr_body.is_none() {
        let (title, body) = pr_text(&input, target);
        intent.pr_title = Some(title);
        intent.pr_body = Some(body);
        persist_intent(database, input.turn, &mut revision, &intent).await?;
    }
    let (owner, repository) = target
        .upstream
        .split_once('/')
        .context("upstream must be owner/name")?;
    let base = input.snapshot.configuration["base"]
        .as_str()
        .context("pinned target base branch missing")?;
    let mut prs = matching_prs(context, target, &branch, base).await?;
    if prior.is_some() && prs.is_empty() {
        block(
            database,
            context,
            input.turn,
            "published pull request disappeared",
        )
        .await?;
        return Ok(());
    }
    if prs.is_empty() && !intent.pr_create_sent {
        intent.pr_create_sent = true;
        persist_intent(database, input.turn, &mut revision, &intent).await?;
        let (fork_owner, fork_repository) = target
            .fork
            .split_once('/')
            .context("fork must be owner/name")?;
        let created: Result<Value> = context
            .github
            .write(
                &target.upstream,
                &target.upstream_write_permissions,
                Method::POST,
                &["repos", owner, repository, "pulls"],
                Some(json!({
                    "title":intent.pr_title,"body":intent.pr_body,
                    "head":format!("{fork_owner}:{branch}"),"head_repo":fork_repository,
                    "base":base,"draft":true,"maintainer_can_modify":false
                })),
            )
            .await;
        prs = matching_prs(context, target, &branch, base).await?;
        if prs.is_empty() {
            if let Err(error) = created {
                match classify_write_failure(&error) {
                    WriteFailure::Definitive(status) => {
                        block(
                            database,
                            context,
                            input.turn,
                            &format!("draft PR create refused by GitHub (HTTP {status})"),
                        )
                        .await?;
                        return Ok(());
                    }
                    WriteFailure::Retryable(_, _) => {
                        intent.pr_create_sent = false;
                        persist_intent(database, input.turn, &mut revision, &intent).await?;
                        return Err(error);
                    }
                    WriteFailure::Unknown => {}
                }
            }
            bail!("draft PR creation is unconfirmed; readback is required");
        }
    }
    if prs.len() != 1 {
        if prs.is_empty() {
            bail!("sent draft PR creation remains unconfirmed; readback is required");
        }
        block(
            database,
            context,
            input.turn,
            "duplicate pull requests use the publication branch",
        )
        .await?;
        return Ok(());
    }
    let pr = &prs[0];
    let number = pr["number"]
        .as_i64()
        .context("pull request number missing")?;
    if pr["head"]["sha"] != commit {
        bail!("pull request head has not converged to the approved commit");
    }
    if pr["state"] != "open"
        || pr["user"]["login"] != target.bot_login
        || pr["head"]["ref"] != branch
        || pr["head"]["repo"]["full_name"]
            .as_str()
            .is_none_or(|name| !name.eq_ignore_ascii_case(&target.fork))
        || pr["base"]["ref"] != base
        || intent.pr_number.is_some_and(|saved| saved != number)
        || prior
            .as_ref()
            .is_some_and(|(saved, _, _, _, _)| *saved != number)
    {
        block(
            database,
            context,
            input.turn,
            "pull request does not match the approved publication",
        )
        .await?;
        return Ok(());
    }
    if intent.pr_number != Some(number) {
        intent.pr_number = Some(number);
        persist_intent(database, input.turn, &mut revision, &intent).await?;
    }
    if let Some((_, _, _, old_title, old_body)) = &prior {
        let old_title = old_title.as_deref().context("prior PR title missing")?;
        let old_body = old_body
            .as_deref()
            .context("prior PR description missing")?;
        let desired_title = intent.pr_title.clone().context("PR title missing")?;
        let desired_body = intent.pr_body.clone().context("PR description missing")?;
        let number_text = number.to_string();
        let pr_path = ["repos", owner, repository, "pulls", number_text.as_str()];
        let current: Value = context
            .github
            .read(
                &target.upstream,
                &target.upstream_write_permissions,
                &pr_path,
                &[],
            )
            .await?;
        let current_text = (current["title"].as_str(), current["body"].as_str());
        if current["head"]["sha"] != commit || current["number"] != number {
            bail!("pull request changed before description reconciliation");
        }
        if current_text != (Some(desired_title.as_str()), Some(desired_body.as_str())) {
            if current_text != (Some(old_title), Some(old_body)) {
                block(
                    database,
                    context,
                    input.turn,
                    "pull request description was edited outside Concurro",
                )
                .await?;
                return Ok(());
            }
            if !intent.pr_update_sent {
                intent.pr_update_sent = true;
                persist_intent(database, input.turn, &mut revision, &intent).await?;
                let updated: Result<Value> = context
                    .github
                    .write(
                        &target.upstream,
                        &target.upstream_write_permissions,
                        Method::PATCH,
                        &pr_path,
                        Some(json!({"title":desired_title,"body":desired_body})),
                    )
                    .await;
                let confirmed: Value = context
                    .github
                    .read(
                        &target.upstream,
                        &target.upstream_write_permissions,
                        &pr_path,
                        &[],
                    )
                    .await?;
                if confirmed["number"] != number || confirmed["head"]["sha"] != commit {
                    bail!("pull request changed during description update");
                }
                if (confirmed["title"].as_str(), confirmed["body"].as_str())
                    != (Some(desired_title.as_str()), Some(desired_body.as_str()))
                {
                    if let Err(error) = updated {
                        match classify_write_failure(&error) {
                            WriteFailure::Definitive(status) => {
                                block(
                                    database,
                                    context,
                                    input.turn,
                                    &format!(
                                        "PR description update refused by GitHub (HTTP {status})"
                                    ),
                                )
                                .await?;
                                return Ok(());
                            }
                            WriteFailure::Retryable(_, _) => {
                                intent.pr_update_sent = false;
                                persist_intent(database, input.turn, &mut revision, &intent)
                                    .await?;
                                return Err(error);
                            }
                            WriteFailure::Unknown => return Err(error),
                        }
                    }
                    bail!("PR description update is unconfirmed; exact readback is required");
                }
            } else {
                bail!("sent PR description update remains unconfirmed; exact readback is required");
            }
        }
    }
    let expected_paths = input.candidate["edits"]
        .as_array()
        .context("candidate edits missing")?
        .iter()
        .map(|edit| {
            edit["path"]
                .as_str()
                .map(str::to_owned)
                .context("candidate edit path missing")
        })
        .collect::<Result<BTreeSet<_>>>()?;
    let actual_paths = pr_paths(context, target, number).await?;
    if prior.is_none() && actual_paths != expected_paths {
        bail!("draft PR file inventory has not converged to the approved candidate");
    }
    if prior.is_some() {
        let in_scope = actual_paths.iter().try_fold(true, |allowed, path| {
            Ok::<bool, anyhow::Error>(allowed && plan_scope_contains(&input.plan, path)?)
        })?;
        if !in_scope {
            block(
                database,
                context,
                input.turn,
                "draft PR contains a path outside the approved plan",
            )
            .await?;
            return Ok(());
        }
    }
    let changed_paths = actual_paths.into_iter().collect::<Vec<_>>();
    let mut work = database.begin().await?;
    complete_git_publication(
        &mut work,
        &context.outbox,
        input.turn,
        number,
        &changed_paths,
    )
    .await?;
    work.commit().await?;
    Ok(())
}

/// Read an exact branch ref and distinguish absence from other API failures.
async fn read_branch(
    context: &GitPublicationContext,
    target: &GitPublicationTarget,
    branch: &str,
) -> Result<Option<String>> {
    match context
        .github
        .branch_head(&target.fork, &target.fork_read_permissions, branch)
        .await
    {
        Ok(head) => Ok(Some(head)),
        Err(error) if response_status(&error) == Some(StatusCode::NOT_FOUND) => Ok(None),
        Err(error) => Err(error),
    }
}

/// Save one compare-and-swap intent revision after external readback.
async fn persist_intent(
    database: &Database,
    turn: Uuid,
    revision: &mut i64,
    intent: &GitPublicationIntent,
) -> Result<()> {
    let mut work = database.begin().await?;
    ensure!(
        save_git_publication(&mut work, turn, *revision, intent).await?,
        "publication intent changed concurrently"
    );
    work.commit().await?;
    *revision += 1;
    Ok(())
}

/// Record a permanent GitHub identity conflict in task state.
async fn block(
    database: &Database,
    context: &GitPublicationContext,
    turn: Uuid,
    reason: &str,
) -> Result<()> {
    let mut work = database.begin().await?;
    block_git_publication(&mut work, &context.outbox, turn, reason).await?;
    work.commit().await?;
    Ok(())
}

/// Create immutable Git objects from the exact source-validated replacement set.
async fn create_candidate_commit(
    context: &GitPublicationContext,
    target: &GitPublicationTarget,
    input: &GitPublicationInput,
    intent: &GitPublicationIntent,
) -> Result<Result<String, String>> {
    let (owner, repository) = target
        .fork
        .split_once('/')
        .context("fork must be owner/name")?;
    let parent = &intent.parent;
    let parent_commit: Value = context
        .github
        .read(
            &target.fork,
            &target.fork_read_permissions,
            &["repos", owner, repository, "git", "commits", parent],
            &[],
        )
        .await?;
    let base_tree = parent_commit["tree"]["sha"]
        .as_str()
        .context("parent tree SHA missing")?;
    let edits: Vec<ImplementationEdit> = serde_json::from_value(input.candidate["edits"].clone())?;
    let paths = edits
        .iter()
        .map(|edit| edit.path.clone())
        .collect::<Vec<_>>();
    let (source_repository, source_permissions) = if input.prior_pr.is_some() {
        (&target.fork, &target.fork_read_permissions)
    } else {
        (&target.upstream, &target.upstream_read_permissions)
    };
    let manifest = initialization::read_file_manifest(
        &context.github,
        source_repository,
        source_permissions,
        parent,
        &paths,
    )
    .await?;
    let mut entries = Vec::with_capacity(edits.len());
    for edit in edits {
        let mode = manifest[&edit.path]["mode"].as_str().unwrap_or("100644");
        let mut entry = json!({"path":edit.path,"mode":mode,"type":"blob"});
        match edit.content {
            Some(content) if content.len() > INLINE_BLOB_LIMIT => {
                let blob: Value = match context
                    .github
                    .write(
                        &target.fork,
                        &target.fork_write_permissions,
                        Method::POST,
                        &["repos", owner, repository, "git", "blobs"],
                        Some(json!({"content":content,"encoding":"utf-8"})),
                    )
                    .await
                {
                    Ok(blob) => blob,
                    Err(error) => match classify_write_failure(&error) {
                        WriteFailure::Definitive(status) => {
                            return Ok(Err(format!(
                                "Git blob create refused by GitHub (HTTP {status})"
                            )));
                        }
                        _ => return Err(error),
                    },
                };
                entry["sha"] = blob["sha"].clone();
            }
            Some(content) => entry["content"] = Value::String(content),
            None => entry["sha"] = Value::Null,
        }
        entries.push(entry);
    }
    let tree: Value = match context
        .github
        .write(
            &target.fork,
            &target.fork_write_permissions,
            Method::POST,
            &["repos", owner, repository, "git", "trees"],
            Some(json!({"base_tree":base_tree,"tree":entries})),
        )
        .await
    {
        Ok(tree) => tree,
        Err(error) => match classify_write_failure(&error) {
            WriteFailure::Definitive(status) => {
                return Ok(Err(format!(
                    "Git tree create refused by GitHub (HTTP {status})"
                )));
            }
            _ => return Err(error),
        },
    };
    let tree_sha = tree["sha"].as_str().context("candidate tree SHA missing")?;
    let metadata = &input.candidate["commit"];
    let subject = if input.snapshot.configuration["conventional_commits"] == true {
        format!(
            "{}: {}",
            metadata["type"].as_str().unwrap_or("fix"),
            metadata["subject"].as_str().unwrap_or("Address issue")
        )
    } else {
        input.plan["headline"]
            .as_str()
            .or_else(|| input.plan["summary"].as_str())
            .unwrap_or("Address issue")
            .to_owned()
    };
    let mut message = markdown_text(&subject, 256);
    message.push_str("\n\n");
    if let Some(changes) = metadata["changes"].as_array() {
        for change in changes {
            message.push_str("- ");
            message.push_str(&markdown_text(change.as_str().unwrap_or_default(), 1000));
            message.push('\n');
        }
    }
    if target
        .issue_repository
        .eq_ignore_ascii_case(&target.upstream)
    {
        message.push_str(&format!("\nRefs #{}", input.state.issue.number));
    } else {
        message.push_str(&format!(
            "\nRefs https://github.com/{}/issues/{}",
            target.issue_repository, input.state.issue.number
        ));
    }
    let bot_user_id = intent
        .bot_user_id
        .context("publication bot identity missing")?;
    let bot_login = intent
        .bot_login
        .as_deref()
        .context("publication bot login missing")?;
    let identity = json!({
        "name":bot_login,
        "email":format!("{bot_user_id}+{bot_login}@users.noreply.github.com"),
        "date":intent.commit_date,
    });
    let commit: Value = match context
        .github
        .write(
            &target.fork,
            &target.fork_write_permissions,
            Method::POST,
            &["repos", owner, repository, "git", "commits"],
            Some(json!({
                "message":message,"tree":tree_sha,"parents":[parent],
                "author":identity,"committer":identity,
            })),
        )
        .await
    {
        Ok(commit) => commit,
        Err(error) => match classify_write_failure(&error) {
            WriteFailure::Definitive(status) => {
                return Ok(Err(format!(
                    "Git commit create refused by GitHub (HTTP {status})"
                )));
            }
            _ => return Err(error),
        },
    };
    let sha = commit["sha"]
        .as_str()
        .context("candidate commit SHA missing")?;
    ensure!(
        sha.len() == 40 && sha.bytes().all(|byte| byte.is_ascii_hexdigit()),
        "candidate commit SHA is invalid"
    );
    Ok(Ok(sha.to_owned()))
}

/// Render the approved plan and implementation metadata as frozen PR text.
fn pr_text(input: &GitPublicationInput, target: &GitPublicationTarget) -> (String, String) {
    let headline = input.plan["headline"]
        .as_str()
        .or_else(|| input.plan["summary"].as_str())
        .unwrap_or("Address issue");
    let title = markdown_text(headline, 256)
        .trim_end_matches('.')
        .to_owned();
    let description = &input.candidate["description"];
    let mut body = String::new();
    body.push_str(&markdown_text(
        description["lead"].as_str().unwrap_or_default(),
        2000,
    ));
    for (heading, key) in [
        ("Problem", "problem"),
        ("Changes", "changes"),
        ("Compatibility", "compatibility"),
        ("Review focus", "review_focus"),
    ] {
        let values = description[key]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>();
        if values.is_empty() {
            continue;
        }
        body.push_str(&format!("\n\n### {heading}\n\n"));
        for value in values {
            body.push_str("- ");
            body.push_str(&markdown_text(value, 1000));
            body.push('\n');
        }
    }
    let closing = if target
        .issue_repository
        .eq_ignore_ascii_case(&target.upstream)
    {
        format!("\nCloses #{}", input.state.issue.number)
    } else {
        format!(
            "\nCloses https://github.com/{}/issues/{}",
            target.issue_repository, input.state.issue.number
        )
    };
    if body.len() + closing.len() > 60_000 {
        let note = "\n\nPR description shortened to fit GitHub's limit.";
        let available = 60_000usize.saturating_sub(note.len() + closing.len());
        body = format!(
            "{}{}",
            markdown_text(description["lead"].as_str().unwrap_or_default(), available),
            note
        );
    }
    body.push_str(&closing);
    (title, body)
}

/// Read all PRs for the exact fork head and pinned upstream base.
async fn matching_prs(
    context: &GitPublicationContext,
    target: &GitPublicationTarget,
    branch: &str,
    base: &str,
) -> Result<Vec<Value>> {
    let (owner, repository) = target
        .upstream
        .split_once('/')
        .context("upstream must be owner/name")?;
    let (fork_owner, _) = target
        .fork
        .split_once('/')
        .context("fork must be owner/name")?;
    let mut all = Vec::new();
    let mut page = 1;
    loop {
        let batch: Vec<Value> = context
            .github
            .read(
                &target.upstream,
                &target.upstream_write_permissions,
                &["repos", owner, repository, "pulls"],
                &[
                    ("state", "all"),
                    ("head", &format!("{fork_owner}:{branch}")),
                    ("base", base),
                    ("per_page", "100"),
                    ("page", &page.to_string()),
                ],
            )
            .await?;
        let more = batch.len() == 100;
        all.extend(batch);
        if !more {
            return Ok(all);
        }
        page += 1;
    }
}

/// Read every changed PR path, including both sides of a detected rename.
pub(crate) async fn pr_paths(
    context: &GitPublicationContext,
    target: &GitPublicationTarget,
    number: i64,
) -> Result<BTreeSet<String>> {
    let (owner, repository) = target
        .upstream
        .split_once('/')
        .context("upstream must be owner/name")?;
    let number = number.to_string();
    let mut paths = BTreeSet::new();
    let mut page = 1;
    loop {
        let files: Vec<Value> = context
            .github
            .read(
                &target.upstream,
                &target.upstream_write_permissions,
                &["repos", owner, repository, "pulls", &number, "files"],
                &[("per_page", "100"), ("page", &page.to_string())],
            )
            .await?;
        let more = files.len() == 100;
        for file in files {
            paths.insert(
                file["filename"]
                    .as_str()
                    .context("PR file path missing")?
                    .to_owned(),
            );
            if file["status"] == "renamed" {
                paths.insert(
                    file["previous_filename"]
                        .as_str()
                        .context("renamed PR file lacks its previous path")?
                        .to_owned(),
                );
            }
        }
        if !more {
            return Ok(paths);
        }
        page += 1;
    }
}
