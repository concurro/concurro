//! GitHub reads that freeze one contribution's source inputs before intake.

use std::collections::BTreeMap;

use anyhow::{Context, Result, ensure};
use base64::{Engine, engine::general_purpose::STANDARD};
use concurro_github_v1::{AppClient, response_status};
use concurro_tasks_core::{
    Actor, InitializationSnapshot, PinnedInputs, Request, RequirementComment, Requirements,
};
use reqwest::StatusCode;
use serde::Deserialize;
use serde_json::{Value, json};

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
    sha: Option<String>,
    size: Option<u64>,
    mode: Option<String>,
}

/// Read one complete recursive tree at an immutable upstream commit.
async fn read_tree(
    github: &AppClient,
    upstream: &str,
    permissions: &BTreeMap<String, String>,
    commit: &str,
) -> Result<GitTree> {
    let (owner, name) = upstream
        .split_once('/')
        .context("upstream repository must be owner/name")?;
    let tree: GitTree = github
        .read(
            upstream,
            permissions,
            &["repos", owner, name, "git", "trees", commit],
            &[("recursive", "1")],
        )
        .await?;
    ensure!(!tree.truncated, "upstream base tree is truncated");
    Ok(tree)
}

/// Identify planned files at the pinned base for independent review contexts.
pub(crate) async fn read_file_manifest(
    github: &AppClient,
    upstream: &str,
    permissions: &BTreeMap<String, String>,
    commit: &str,
    paths: &[String],
) -> Result<Value> {
    let tree = read_tree(github, upstream, permissions, commit).await?;
    let by_path = tree
        .tree
        .into_iter()
        .map(|entry| (entry.path.clone(), entry))
        .collect::<BTreeMap<_, _>>();
    let files = paths
        .iter()
        .map(|path| {
            let value = by_path.get(path).map(|entry| {
                let mut fields = serde_json::Map::new();
                for (key, value) in [
                    ("sha", entry.sha.as_ref().map(|value| json!(value))),
                    ("size", entry.size.map(|value| json!(value))),
                    ("mode", entry.mode.as_ref().map(|value| json!(value))),
                    ("type", Some(json!(entry.kind))),
                ] {
                    if let Some(value) = value {
                        fields.insert(key.to_owned(), value);
                    }
                }
                Value::Object(fields)
            });
            (path.clone(), value.unwrap_or(Value::Null))
        })
        .collect::<serde_json::Map<_, _>>();
    Ok(Value::Object(files))
}

/// Read regular UTF-8 files by pinned blob SHA, returning source defects as candidate feedback.
pub(crate) async fn read_source_files(
    github: &AppClient,
    upstream: &str,
    permissions: &BTreeMap<String, String>,
    commit: &str,
    paths: &[String],
) -> Result<Result<BTreeMap<String, Option<String>>, String>> {
    let tree = read_tree(github, upstream, permissions, commit).await?;
    let entries = tree
        .tree
        .into_iter()
        .map(|entry| (entry.path.clone(), entry))
        .collect::<BTreeMap<_, _>>();
    let (owner, name) = upstream
        .split_once('/')
        .context("upstream repository must be owner/name")?;
    let mut files = BTreeMap::new();
    for path in paths {
        let Some(entry) = entries.get(path) else {
            let mut ancestor = String::new();
            for part in path
                .split('/')
                .take(path.split('/').count().saturating_sub(1))
            {
                if !ancestor.is_empty() {
                    ancestor.push('/');
                }
                ancestor.push_str(part);
                if entries
                    .get(&ancestor)
                    .is_some_and(|entry| entry.kind != "tree")
                {
                    return Ok(Err("non-directory path ancestor".to_owned()));
                }
            }
            files.insert(path.clone(), None);
            continue;
        };
        if entry.kind != "blob" || !matches!(entry.mode.as_deref(), Some("100644" | "100755")) {
            return Ok(Err("source must be a regular text file".to_owned()));
        }
        let Some(sha) = entry.sha.as_deref() else {
            return Ok(Err("source blob SHA is missing".to_owned()));
        };
        let blob: Value = match github
            .read(
                upstream,
                permissions,
                &["repos", owner, name, "git", "blobs", sha],
                &[],
            )
            .await
        {
            Ok(blob) => blob,
            Err(error) if response_status(&error) == Some(StatusCode::NOT_FOUND) => {
                return Ok(Err("pinned source blob is missing".to_owned()));
            }
            Err(error) => return Err(error),
        };
        if blob["sha"] != sha || blob["encoding"] != "base64" {
            return Ok(Err("unsupported source blob response".to_owned()));
        }
        let Some(content) = blob["content"].as_str() else {
            return Ok(Err("source blob content is missing".to_owned()));
        };
        let encoded = content
            .bytes()
            .filter(|byte| !byte.is_ascii_whitespace())
            .collect::<Vec<_>>();
        let Ok(bytes) = STANDARD.decode(encoded) else {
            return Ok(Err("source blob content has invalid base64".to_owned()));
        };
        if entry.size.is_some_and(|size| size != bytes.len() as u64)
            || blob["size"]
                .as_u64()
                .is_some_and(|size| size != bytes.len() as u64)
        {
            return Ok(Err("source blob size differs from its tree".to_owned()));
        }
        if bytes.contains(&0) {
            return Ok(Err("source is binary".to_owned()));
        }
        let Ok(text) = String::from_utf8(bytes) else {
            return Ok(Err("source is not UTF-8".to_owned()));
        };
        files.insert(path.clone(), Some(text));
    }
    Ok(Ok(files))
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

    let tree = read_tree(
        github,
        &target.upstream,
        &target.upstream_permissions,
        &base_sha,
    )
    .await?;
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

    let (requirements, _) = read_requirements(github, target, request).await?;
    Ok(InitializationSnapshot {
        pins: PinnedInputs {
            config_sha: config_sha.to_owned(),
            base_sha,
            max_model_calls: config.max_model_calls,
            max_fix_rounds: config.max_fix_rounds,
            max_plan_revisions: config.max_plan_revisions,
        },
        configuration: config.raw,
        requirements,
        charter,
        instruction_texts,
        policy_documents,
        base_files,
        bundle_id,
    })
}

/// Read the issue and eligible discussion with comment IDs through one command source.
pub async fn read_requirements(
    github: &AppClient,
    target: &InitializationTarget,
    request: &Request,
) -> Result<(Requirements, Vec<i64>)> {
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
        .context("requirements read requires a comment source")?;
    let mut discussion = Vec::new();
    let mut comment_ids = Vec::new();
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
                comment_ids.push(comment.id);
                discussion.push(RequirementComment {
                    author: comment.user,
                    body: comment.body,
                });
                if discussion.len() > 20 {
                    discussion.remove(0);
                    comment_ids.remove(0);
                }
            }
        }
        if reached_start || !full_page {
            break;
        }
        page += 1;
    }
    Ok((
        Requirements {
            title: issue.title,
            body: issue.body,
            discussion,
        },
        comment_ids,
    ))
}

#[cfg(test)]
mod tests {
    use std::{collections::BTreeMap, process::Command, sync::Arc};

    use axum::{
        Json, Router,
        extract::Path,
        http::StatusCode,
        routing::{get, post},
    };
    use base64::{Engine, engine::general_purpose::STANDARD};
    use chrono::{Duration, Utc};
    use concurro_github_v1::AppClient;
    use serde_json::{Value, json};

    use super::read_source_files;

    /// A pinned blob over the Contents API limit is readable; bad ancestors and bytes are feedback.
    #[tokio::test]
    async fn source_reads_blob_sha_and_rejects_non_directory_or_binary_inputs() {
        let large = Arc::new("x".repeat(1_050_000));
        let app = Router::new()
            .route("/repos/acme/demo/installation", get(|| async {
                Json(json!({"id":7,"repository_selection":"selected","account":{"login":"acme"}}))
            }))
            .route("/app/installations/7/access_tokens", post(|Json(body): Json<Value>| async move {
                Json(json!({"token":"scoped-token","permissions":body["permissions"],
                    "expires_at":(Utc::now()+Duration::minutes(55)).to_rfc3339()}))
            }))
            .route("/installation/repositories", get(|| async {
                Json(json!({"total_count":1,"repositories":[{"full_name":"acme/demo"}]}))
            }))
            .route("/repos/acme/demo/git/trees/{sha}", get({
                let large = large.clone();
                move |Path(_sha): Path<String>| {
                    let large = large.clone();
                    async move { Json(json!({"truncated":false,"tree":[
                        {"path":"docs","type":"tree","mode":"040000"},
                        {"path":"docs/link","type":"blob","mode":"120000","sha":"a".repeat(40)},
                        {"path":"large.txt","type":"blob","mode":"100644","sha":"b".repeat(40),"size":large.len()},
                        {"path":"binary.txt","type":"blob","mode":"100644","sha":"c".repeat(40),"size":3},
                        {"path":"nonutf8.txt","type":"blob","mode":"100644","sha":"d".repeat(40),"size":2}
                    ]})) }
                }
            }))
            .route("/repos/acme/demo/git/blobs/{sha}", get({
                let large = large.clone();
                move |Path(sha): Path<String>| {
                    let large = large.clone();
                    async move {
                        let bytes: Vec<u8> = if sha == "b".repeat(40) {
                            large.as_bytes().to_vec()
                        } else if sha == "c".repeat(40) {
                            vec![b'a', 0, b'b']
                        } else {
                            vec![0xff, 0xfe]
                        };
                        (StatusCode::OK, Json(json!({"sha":sha,"encoding":"base64",
                            "size":bytes.len(),"content":STANDARD.encode(bytes)})))
                    }
                }
            }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind");
        let address = listener.local_addr().expect("address");
        let server = tokio::spawn(async move { axum::serve(listener, app).await });
        let key = Command::new("openssl")
            .args([
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
            ])
            .output()
            .expect("test App key");
        assert!(key.status.success());
        let github = AppClient::new(
            reqwest::Client::new(),
            &format!("http://{address}"),
            42,
            &key.stdout,
        )
        .expect("App client");
        let permissions = BTreeMap::from([("contents".to_owned(), "read".to_owned())]);
        let read = |path: &str| vec![path.to_owned()];
        let ancestor = read_source_files(
            &github,
            "acme/demo",
            &permissions,
            &"e".repeat(40),
            &read("docs/link/new.txt"),
        )
        .await
        .expect("tree read");
        assert_eq!(
            ancestor.expect_err("non-directory ancestor"),
            "non-directory path ancestor"
        );
        let source = read_source_files(
            &github,
            "acme/demo",
            &permissions,
            &"e".repeat(40),
            &read("large.txt"),
        )
        .await
        .expect("large blob")
        .expect("valid text");
        assert_eq!(
            source["large.txt"].as_ref().expect("content").len(),
            large.len()
        );
        for (path, reason) in [
            ("binary.txt", "source is binary"),
            ("nonutf8.txt", "source is not UTF-8"),
        ] {
            let source = read_source_files(
                &github,
                "acme/demo",
                &permissions,
                &"e".repeat(40),
                &read(path),
            )
            .await
            .expect("blob read");
            assert_eq!(source.expect_err("invalid text"), reason);
        }
        server.abort();
    }
}
