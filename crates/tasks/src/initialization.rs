//! GitHub reads that freeze one contribution's source inputs before intake.

use std::collections::BTreeMap;

use anyhow::{Context, Result, ensure};
use concurro_github_v1::AppClient;
use concurro_tasks_core::{
    Actor, InitializationSnapshot, PinnedInputs, Request, RequirementComment, Requirements,
};
use serde::Deserialize;

use crate::{bundle::TARGET_INSTRUCTIONS, bundle_identity, decode_target_config};

/// Repositories and exact read permissions used for one task source snapshot.
#[derive(Clone, Deserialize)]
pub struct InitializationTarget {
    /// Repository containing the issue and comments.
    pub issue_repository: String,
    /// Configured executor fork.
    pub fork: String,
    /// Upstream source repository.
    pub upstream: String,
    /// Read permissions for the issue repository.
    pub issue_permissions: BTreeMap<String, String>,
    /// Read permissions for the fork.
    pub fork_permissions: BTreeMap<String, String>,
    /// Read permissions for upstream source.
    pub upstream_permissions: BTreeMap<String, String>,
    /// Verified App bot login.
    pub bot_login: String,
    /// Other owned publisher accounts omitted from requirements.
    pub publisher_logins: Vec<String>,
    /// Exact Rust engine release serving the task.
    pub engine_sha: String,
}

/// Issue text and immutable author returned by the Issues API.
#[derive(Deserialize)]
struct IssueDetails {
    title: String,
    body: Option<String>,
    user: Actor,
}

/// One listed issue comment before the start source.
#[derive(Deserialize)]
struct ListedComment {
    id: i64,
    body: String,
    user: Actor,
}

/// Complete recursive Git tree at one pinned commit.
#[derive(Deserialize)]
struct GitTree {
    truncated: bool,
    tree: Vec<GitTreeEntry>,
}

/// One path in a Git tree response.
#[derive(Deserialize)]
struct GitTreeEntry {
    path: String,
    #[serde(rename = "type")]
    kind: String,
}

/// Read all M1 source inputs at their respective fork and upstream commits.
pub async fn read_initialization(
    github: &AppClient,
    target: &InitializationTarget,
    request: &Request,
    config_sha: &str,
) -> Result<InitializationSnapshot> {
    ensure!(
        config_sha.len() == 40 && config_sha.bytes().all(|byte| byte.is_ascii_hexdigit()),
        "pinned configuration commit is invalid"
    );
    let config_json = github
        .file_text(
            &target.fork,
            &target.fork_permissions,
            ".concurro/config.json",
            config_sha,
        )
        .await?;
    let config_yaml = github
        .file_text(
            &target.fork,
            &target.fork_permissions,
            ".concurro/config.yaml",
            config_sha,
        )
        .await?;
    let config = decode_target_config(
        config_json.as_deref(),
        config_yaml.as_deref(),
        &target.fork,
        &target.bot_login,
        &target.engine_sha,
    )?;
    let base_sha = github
        .branch_head(&target.upstream, &target.upstream_permissions, &config.base)
        .await?;
    let charter = github
        .file_text(
            &target.fork,
            &target.fork_permissions,
            ".concurro/charter.json",
            config_sha,
        )
        .await?
        .context("target charter is missing")?;
    let mut instruction_texts = BTreeMap::new();
    for path in TARGET_INSTRUCTIONS {
        if let Some(text) = github
            .file_text(&target.fork, &target.fork_permissions, path, config_sha)
            .await?
        {
            instruction_texts.insert(path.to_owned(), text);
        }
    }
    let bundle_id = bundle_identity(&charter, &instruction_texts)?;

    let (owner, name) = target
        .upstream
        .split_once('/')
        .context("upstream repository must be owner/name")?;
    let tree: GitTree = github
        .read(
            &target.upstream,
            &target.upstream_permissions,
            &["repos", owner, name, "git", "trees", &base_sha],
            &[("recursive", "1")],
        )
        .await?;
    ensure!(!tree.truncated, "upstream base tree is truncated");
    let base_files = tree
        .tree
        .iter()
        .filter(|entry| entry.kind != "tree")
        .map(|entry| entry.path.clone())
        .collect::<Vec<_>>();
    let mut policy_documents = BTreeMap::new();
    let policy_paths = config.policy_documents.iter().cloned().chain(
        base_files
            .iter()
            .filter(|path| path.starts_with(".github/instructions/") && path.ends_with(".md"))
            .cloned(),
    );
    for path in policy_paths {
        let text = github
            .file_text(
                &target.upstream,
                &target.upstream_permissions,
                &path,
                &base_sha,
            )
            .await?
            .with_context(|| format!("upstream policy file {path} is missing"))?;
        policy_documents.insert(path, text);
    }

    let (issue_owner, issue_name) = target
        .issue_repository
        .split_once('/')
        .context("issue repository must be owner/name")?;
    let issue_number = request.issue.number.to_string();
    let issue: IssueDetails = github
        .read(
            &target.issue_repository,
            &target.issue_permissions,
            &["repos", issue_owner, issue_name, "issues", &issue_number],
            &[],
        )
        .await?;
    ensure!(
        issue.user == request.issue_author,
        "issue author changed during initialization"
    );
    let start_comment = request
        .comment_id
        .context("M1 initialization requires a comment start source")?;
    let mut discussion = Vec::new();
    let mut page = 1;
    loop {
        let page_text = page.to_string();
        let comments: Vec<ListedComment> = github
            .read(
                &target.issue_repository,
                &target.issue_permissions,
                &[
                    "repos",
                    issue_owner,
                    issue_name,
                    "issues",
                    &issue_number,
                    "comments",
                ],
                &[("per_page", "100"), ("page", &page_text)],
            )
            .await?;
        let reached_start = comments.iter().any(|comment| comment.id >= start_comment);
        let full_page = comments.len() == 100;
        for comment in comments
            .into_iter()
            .filter(|comment| comment.id <= start_comment)
        {
            if comment.body.starts_with("<!-- concurro:v1 -->")
                || comment.body.trim().starts_with("/concurro ")
                || comment.user.login == target.bot_login
                || target.publisher_logins.contains(&comment.user.login)
            {
                continue;
            }
            let eligible = comment.user == issue.user
                || github
                    .writer_permission(
                        &target.issue_repository,
                        &comment.user.login,
                        &target.issue_permissions,
                    )
                    .await?;
            if eligible {
                discussion.push(RequirementComment {
                    author: comment.user,
                    body: comment.body,
                });
                if discussion.len() > 20 {
                    discussion.remove(0);
                }
            }
        }
        if reached_start || !full_page {
            break;
        }
        page += 1;
    }
    Ok(InitializationSnapshot {
        pins: PinnedInputs {
            config_sha: config_sha.to_owned(),
            base_sha,
            max_model_calls: config.max_model_calls,
            max_fix_rounds: config.max_fix_rounds,
            max_plan_revisions: config.max_plan_revisions,
        },
        configuration: config.raw,
        requirements: Requirements {
            title: issue.title,
            body: issue.body,
            discussion,
        },
        charter,
        instruction_texts,
        policy_documents,
        base_files,
        bundle_id,
    })
}
