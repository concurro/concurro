//! Full GitHub target lists retained beyond the webhook stream window.

use std::collections::{BTreeMap, HashSet};

use anyhow::{Context, Result, ensure};
use chrono::{DateTime, Utc};
use concurro_contracts::{Envelope, GithubEvent, WorkflowRunPayload};
use concurro_db::{Database, UnitOfWork};
use concurro_events::{Handler, Outcome};
use concurro_github_v1::AppClient;
use concurro_tasks_postgres::{
    IndexRow, held_issue_numbers, historical_pulls, index_scan_started, owned_pr_numbers,
    replace_target_index, upsert_index_event,
};
use serde_json::{Value, json};

/// One configured target and the exact repository-scoped read grants it uses.
#[derive(Clone)]
pub struct IndexTarget {
    /// Numeric issue repository identity.
    pub issue_repository_id: i64,
    /// Issue repository owner/name.
    pub issue_repository: String,
    /// Configured upstream source repository.
    pub upstream: String,
    /// Configured runner fork.
    pub fork: String,
    /// Issue repository read permission set.
    pub issue_permissions: BTreeMap<String, String>,
    /// Upstream read permission set.
    pub upstream_permissions: BTreeMap<String, String>,
    /// Fork read permission set.
    pub fork_permissions: BTreeMap<String, String>,
}

/// Keep executor runs current between complete fork scans.
#[derive(Clone)]
pub struct IndexRunHandler {
    /// Configured fork names whose executor runs are displayed.
    pub forks: HashSet<String>,
}

impl Handler<GithubEvent<WorkflowRunPayload>> for IndexRunHandler {
    /// Apply a newer mapped workflow delivery before broker acknowledgement.
    async fn handle(
        &self,
        work: &mut UnitOfWork<'_>,
        event: Envelope<GithubEvent<WorkflowRunPayload>>,
    ) -> Outcome {
        if !self
            .forks
            .contains(&event.data.repository.full_name.to_ascii_lowercase())
            || !event.data.payload.path.ends_with("/concurro-executor.yml")
        {
            return Outcome::Done;
        }
        let run = &event.data.payload;
        let row = IndexRow {
            repository_id: event.data.repository.id,
            kind: "run",
            item_id: format!("{}/{}", run.id, run.run_attempt),
            source_at: run.updated_at.0,
            body: json!({"repository":event.data.repository.full_name,
                "id":run.id,"attempt":run.run_attempt,"status":run.status,
                "conclusion":run.conclusion,"head_branch":run.head_branch,
                "head_sha":run.head_sha,"created_at":run.created_at,
                "updated_at":run.updated_at}),
        };
        match upsert_index_event(work, &row).await {
            Ok(()) => Outcome::Done,
            Err(error) => Outcome::Retry(error.to_string()),
        }
    }
}

/// Read every page of a configured target's issue, PR and executor-run lists.
pub async fn scan_target_index(
    database: &Database,
    github: &AppClient,
    target: &IndexTarget,
) -> Result<()> {
    let issue_id =
        repository_id(github, &target.issue_repository, &target.issue_permissions).await?;
    ensure!(
        issue_id == target.issue_repository_id,
        "issue target ID changed"
    );
    let upstream_id = repository_id(github, &target.upstream, &target.upstream_permissions).await?;
    let fork_id = repository_id(github, &target.fork, &target.fork_permissions).await?;

    let held = held_issue_numbers(database, issue_id).await?;
    let held = held.into_iter().collect::<HashSet<_>>();
    let (issue_owner, issue_name) = target
        .issue_repository
        .split_once('/')
        .context("issue target name invalid")?;
    let started = index_scan_started(database).await?;
    let mut rows = Vec::new();
    let mut page = 1;
    loop {
        let page_text = page.to_string();
        let batch: Vec<Value> = github
            .read(
                &target.issue_repository,
                &target.issue_permissions,
                &["repos", issue_owner, issue_name, "issues"],
                &[("state", "all"), ("per_page", "100"), ("page", &page_text)],
            )
            .await?;
        let full = batch.len() == 100;
        for issue in batch {
            if !issue["pull_request"].is_null() {
                continue;
            }
            let number = issue["number"]
                .as_i64()
                .context("index issue number missing")?;
            let enrolled = issue["labels"]
                .as_array()
                .is_some_and(|labels| labels.iter().any(|label| label["name"] == "concurro"));
            if !enrolled && !held.contains(&number) {
                continue;
            }
            let updated = source_time(&issue)?;
            rows.push(IndexRow {
                repository_id: issue_id,
                kind: "issue",
                item_id: number.to_string(),
                source_at: updated,
                body: json!({"repository":target.issue_repository,"number":number,
                    "title":issue["title"],"state":issue["state"],
                    "labels":issue["labels"],"updated_at":issue["updated_at"],
                    "html_url":issue["html_url"],"user":issue["user"]}),
            });
        }
        if !full {
            break;
        }
        page += 1;
    }
    replace_target_index(database, issue_id, "issue", started, &rows).await?;

    let (upstream_owner, upstream_name) = target
        .upstream
        .split_once('/')
        .context("upstream target name invalid")?;
    let owned = owned_pr_numbers(database, &target.upstream).await?;
    let owned = owned.into_iter().collect::<HashSet<_>>();
    let started = index_scan_started(database).await?;
    let mut rows = Vec::new();
    let mut page = 1;
    loop {
        let page_text = page.to_string();
        let batch: Vec<Value> = github
            .read(
                &target.upstream,
                &target.upstream_permissions,
                &["repos", upstream_owner, upstream_name, "pulls"],
                &[
                    ("state", "all"),
                    ("sort", "updated"),
                    ("direction", "desc"),
                    ("per_page", "100"),
                    ("page", &page_text),
                ],
            )
            .await?;
        let full = batch.len() == 100;
        for pull in batch {
            let number = pull["number"].as_i64().context("index PR number missing")?;
            let branch = pull["head"]["ref"].as_str().unwrap_or("");
            if !task_branch(branch) && !owned.contains(&number) {
                continue;
            }
            rows.push(IndexRow {
                repository_id: upstream_id,
                kind: "pull",
                item_id: number.to_string(),
                source_at: source_time(&pull)?,
                body: json!({"repository":target.upstream,"number":number,
                    "title":pull["title"],"state":pull["state"],"merged_at":pull["merged_at"],
                    "draft":pull["draft"],"head":pull["head"],"base":pull["base"],
                    "updated_at":pull["updated_at"],"html_url":pull["html_url"]}),
            });
        }
        if !full {
            break;
        }
        page += 1;
    }
    for (number, document) in historical_pulls(database, &target.upstream).await? {
        if rows.iter().any(|row| row.item_id == number.to_string()) {
            continue;
        }
        let source_at = document["updated_at"]
            .as_str()
            .and_then(|value| DateTime::parse_from_rfc3339(value).ok())
            .map(|time| time.with_timezone(&Utc))
            .unwrap_or(DateTime::<Utc>::UNIX_EPOCH);
        rows.push(IndexRow {
            repository_id: upstream_id,
            kind: "pull",
            item_id: number.to_string(),
            source_at,
            body: json!({"repository":target.upstream,"number":number,
                "historical":true,"document":document}),
        });
    }
    replace_target_index(database, upstream_id, "pull", started, &rows).await?;

    let (fork_owner, fork_name) = target
        .fork
        .split_once('/')
        .context("fork target name invalid")?;
    let started = index_scan_started(database).await?;
    let mut rows = Vec::new();
    let mut page = 1;
    loop {
        let page_text = page.to_string();
        let listing: Value = github
            .read(
                &target.fork,
                &target.fork_permissions,
                &[
                    "repos",
                    fork_owner,
                    fork_name,
                    "actions",
                    "workflows",
                    "concurro-executor.yml",
                    "runs",
                ],
                &[("per_page", "100"), ("page", &page_text)],
            )
            .await?;
        let batch = listing["workflow_runs"]
            .as_array()
            .context("executor run listing missing")?;
        let full = batch.len() == 100;
        for run in batch {
            let id = run["id"].as_i64().context("executor run ID missing")?;
            let attempt = run["run_attempt"]
                .as_i64()
                .context("executor attempt missing")?;
            rows.push(IndexRow {
                repository_id: fork_id,
                kind: "run",
                item_id: format!("{id}/{attempt}"),
                source_at: source_time(run)?,
                body: json!({"repository":target.fork,"id":id,"attempt":attempt,
                    "status":run["status"],"conclusion":run["conclusion"],
                    "head_branch":run["head_branch"],"head_sha":run["head_sha"],
                    "created_at":run["created_at"],"updated_at":run["updated_at"],
                    "html_url":run["html_url"]}),
            });
        }
        if !full {
            break;
        }
        page += 1;
    }
    replace_target_index(database, fork_id, "run", started, &rows).await?;
    Ok(())
}

/// Resolve the numeric source repository before assigning index rows.
async fn repository_id(
    github: &AppClient,
    repository: &str,
    permissions: &BTreeMap<String, String>,
) -> Result<i64> {
    let (owner, name) = repository
        .split_once('/')
        .context("index repository must be owner/name")?;
    let value: Value = github
        .read(repository, permissions, &["repos", owner, name], &[])
        .await?;
    value["id"].as_i64().context("index repository ID missing")
}

/// Parse one authoritative GitHub update timestamp.
fn source_time(value: &Value) -> Result<DateTime<Utc>> {
    Ok(DateTime::parse_from_rfc3339(
        value["updated_at"]
            .as_str()
            .context("index update time missing")?,
    )?
    .with_timezone(&Utc))
}

/// Match the task branch grammar used by Python's existing list index.
pub(crate) fn task_branch(branch: &str) -> bool {
    let Some(rest) = branch.strip_prefix("concurro/issue-") else {
        return false;
    };
    let Some((number, suffix)) = rest.split_once('-') else {
        return false;
    };
    !number.is_empty()
        && !number.starts_with('0')
        && number.bytes().all(|byte| byte.is_ascii_digit())
        && !suffix.is_empty()
        && suffix.bytes().all(|byte| byte.is_ascii_hexdigit())
}
