//! Reconstruct missed GitHub sources and refresh active work after an ingress gap.

use std::collections::BTreeMap;

use anyhow::{Context, Result, bail, ensure};
use chrono::{DateTime, Utc};
use concurro_contracts::{Envelope, EventId, GithubEvent, IssueCommentPayload, Timestamp};
use concurro_db::Database;
use concurro_events::{Handler, Outcome};
use concurro_events_postgres::Outbox;
use concurro_github_v1::response_status;
use concurro_tasks_core::{Actor, CommentRevision, IssueKey, Request, RequestKind, SourceOrder};
use concurro_tasks_postgres::{
    PendingCoverageGap, comment_revision_recorded, complete_coverage_gap, known_live_comments,
    pending_issues, record_comment_revision, record_coverage_source_failure, record_request,
    resolve_coverage_source_failure, source_after_activation, standalone_task_repository_id,
    unresolved_coverage_source_count, watched_gate_issues, watched_standalone_review_prs,
};
use reqwest::StatusCode;
use serde_json::{Value, json};
use uuid::Uuid;

use crate::{
    CommentHandler, DecisionContext, GitPublicationContext, IndexTarget, StandaloneReviewContext,
    label::{LabelScanJob, scan_label_events},
    run_standalone_review_pr_state, run_task_decision, run_task_gates, scan_target_index,
    standalone::scan_standalone_review_events,
};

/// Inputs shared by `reconcile_coverage_gap`.
pub struct CoverageInputs<'a> {
    /// Current target configuration and verified issue authority reader.
    pub decisions: &'a DecisionContext,
    /// Independent PR review source and authority reader.
    pub standalone: &'a StandaloneReviewContext,
    /// GitHub publication settings used for current-head observations.
    pub publication: &'a GitPublicationContext,
    /// Comment intake handler used when recovering missed sources.
    pub comments: &'a CommentHandler,
    /// Configured targets whose issue and PR indexes need reconciliation.
    pub index_targets: &'a [IndexTarget],
}

/// Inputs shared by `scan_repository`.
pub struct CoverageRepository<'a> {
    /// Numeric GitHub repository identity.
    pub repository_id: i64,
    /// GitHub owner/repository whose source timeline is being read.
    pub repository: &'a str,
    /// Exact GitHub App read permissions for this repository.
    pub permissions: &'a BTreeMap<String, String>,
}

/// Re-read changed issue sources in one configured GitHub repository.
async fn scan_repository(
    database: &Database,
    outbox: &Outbox,
    decisions: &DecisionContext,
    standalone: &StandaloneReviewContext,
    comments: &CommentHandler,
    gap: &PendingCoverageGap,
    inputs: CoverageRepository<'_>,
) -> Result<()> {
    let CoverageRepository {
        repository_id,
        repository,
        permissions,
    } = inputs;
    let (owner, name) = repository
        .split_once('/')
        .context("coverage target repository must be owner/name")?;
    let since = gap.from_at.to_rfc3339();
    let mut page = 1;
    loop {
        let page_text = page.to_string();
        let batch: Vec<Value> = decisions
            .github
            .read(
                repository,
                permissions,
                &["repos", owner, name, "issues", "comments"],
                &[("since", &since), ("per_page", "100"), ("page", &page_text)],
            )
            .await?;
        let full = batch.len() == 100;
        for comment in batch {
            let updated = DateTime::parse_from_rfc3339(
                comment["updated_at"]
                    .as_str()
                    .context("comment update time missing")?,
            )?
            .with_timezone(&Utc);
            let created = DateTime::parse_from_rfc3339(
                comment["created_at"]
                    .as_str()
                    .context("comment creation time missing")?,
            )?
            .with_timezone(&Utc);
            if updated < gap.from_at || updated > gap.to_at {
                continue;
            }
            let body = comment["body"].as_str().unwrap_or("");
            let issue_url = comment["issue_url"]
                .as_str()
                .context("comment issue URL missing")?;
            let number: i64 = issue_url
                .rsplit('/')
                .next()
                .context("comment issue number missing")?
                .parse()?;
            ensure!(
                issue_url.contains(&format!("/repos/{repository}/issues/{number}")),
                "comment belongs to another repository"
            );
            let number_text = number.to_string();
            let issue: Value = match decisions
                .github
                .read(
                    repository,
                    permissions,
                    &["repos", owner, name, "issues", &number_text],
                    &[],
                )
                .await
            {
                Ok(issue) => issue,
                Err(error)
                    if matches!(
                        response_status(&error),
                        Some(StatusCode::NOT_FOUND | StatusCode::FORBIDDEN)
                    ) =>
                {
                    record_coverage_source_failure(
                        database,
                        gap.event_id,
                        repository_id,
                        "comment_issue",
                        &format!("{}:{number}", comment["id"]),
                        &error.to_string(),
                    )
                    .await?;
                    continue;
                }
                Err(error) => return Err(error),
            };
            ensure!(issue["number"] == number, "comment issue identity changed");
            resolve_coverage_source_failure(
                database,
                gap.event_id,
                repository_id,
                "comment_issue",
                &format!("{}:{number}", comment["id"]),
            )
            .await?;
            let comment_id = comment["id"].as_i64().context("comment ID missing")?;
            if updated > created && body.trim().starts_with("/concurro ") {
                if comment_revision_recorded(database, comment_id, updated).await? {
                    resolve_coverage_source_failure(
                        database,
                        gap.event_id,
                        repository_id,
                        "edited_command",
                        &format!("{}:{}", comment["id"], updated.to_rfc3339()),
                    )
                    .await?;
                    continue;
                }
                record_coverage_source_failure(
                    database,
                    gap.event_id,
                    repository_id,
                    "edited_command",
                    &format!("{}:{}", comment["id"], updated.to_rfc3339()),
                    "GitHub readback has no authenticated editor for this command revision",
                )
                .await?;
                continue;
            }
            let payload: GithubEvent<IssueCommentPayload> = serde_json::from_value(json!({
                "delivery_id":format!("gap:{}:{}", gap.event_id, comment["id"]),
                "mapping_revision":1,
                "received_at":gap.to_at,
                "installation_id":gap.installation_id,
                "repository":{"id":repository_id,"owner_id":0,"full_name":repository},
                "sender":comment["user"],
                "action":if updated > created { "edited" } else { "created" },
                "issue":{
                    "number":number,"title":issue["title"],"state":issue["state"],
                    "is_pull_request":!issue["pull_request"].is_null(),
                    "user":issue["user"],"labels":issue["labels"],
                    "created_at":issue["created_at"],"updated_at":issue["updated_at"]
                },
                "comment":{
                    "id":comment["id"],"body":comment["body"],"user":comment["user"],
                    "author_association":comment["author_association"].as_str().unwrap_or(""),
                    "created_at":comment["created_at"],"updated_at":comment["updated_at"],
                    "html_url":comment["html_url"]
                },
                "previous_body":null
            }))?;
            let mut work = database.begin().await?;
            match comments
                .handle(
                    &mut work,
                    Envelope::fact(EventId::mint(), "gap-recovery", Timestamp(updated), payload),
                )
                .await
            {
                Outcome::Done => work.commit().await?,
                Outcome::Retry(error) | Outcome::Hold(error) | Outcome::Reject(error) => {
                    bail!("comment source recovery: {error}")
                }
            }
        }
        if !full {
            break;
        }
        page += 1;
    }
    for (comment_id, number) in known_live_comments(database, repository_id).await? {
        let comment_text = comment_id.to_string();
        let read: Result<Value> = decisions
            .github
            .read(
                repository,
                permissions,
                &["repos", owner, name, "issues", "comments", &comment_text],
                &[],
            )
            .await;
        match read {
            Ok(_) => {}
            Err(error) if response_status(&error) == Some(StatusCode::NOT_FOUND) => {
                let mut work = database.begin().await?;
                record_comment_revision(
                    &mut work,
                    outbox,
                    &CommentRevision {
                        issue: IssueKey {
                            repository_id,
                            number,
                        },
                        comment_id,
                        order: SourceOrder {
                            at: gap.to_at,
                            source_id: comment_id,
                            source: format!("gap-deleted:{}:{comment_id}", gap.event_id),
                            index: 0,
                        },
                        deleted: true,
                        request: None,
                    },
                )
                .await?;
                work.commit().await?;
            }
            Err(error) => return Err(error),
        }
    }
    let mut page = 1;
    loop {
        let page_text = page.to_string();
        let batch: Vec<Value> = decisions
            .github
            .read(
                repository,
                permissions,
                &["repos", owner, name, "issues"],
                &[
                    ("state", "all"),
                    ("since", &since),
                    ("per_page", "100"),
                    ("page", &page_text),
                ],
            )
            .await?;
        let full = batch.len() == 100;
        for issue in batch {
            let number = issue["number"].as_i64().context("issue number missing")?;
            let events =
                crate::label::read_events(&decisions.github, repository, permissions, number)
                    .await?;
            if decisions.targets.contains_key(&repository_id) && issue["pull_request"].is_null() {
                if let Some(event) = events.iter().find(|event| {
                    matches!(event["event"].as_str(), Some("labeled" | "unlabeled"))
                        && event["label"]["name"] == "concurro"
                        && !event["actor"].is_null()
                }) {
                    let at = DateTime::parse_from_rfc3339(
                        event["created_at"]
                            .as_str()
                            .context("label event time missing")?,
                    )?
                    .with_timezone(&Utc);
                    scan_label_events(
                        database,
                        decisions,
                        &LabelScanJob {
                            delivery_id: format!("gap:{}:label:{number}", gap.event_id),
                            repository_id,
                            issue_number: number,
                            action: if event["event"] == "labeled" {
                                "labeled"
                            } else {
                                "unlabeled"
                            }
                            .to_owned(),
                            actor_id: event["actor"]["id"]
                                .as_i64()
                                .context("label actor missing")?,
                            updated_at: at,
                        },
                    )
                    .await?;
                }
                for event in &events {
                    if event["event"] != "closed" {
                        continue;
                    }
                    let at = DateTime::parse_from_rfc3339(
                        event["created_at"]
                            .as_str()
                            .context("close event time missing")?,
                    )?
                    .with_timezone(&Utc);
                    if at < gap.from_at || at > gap.to_at {
                        continue;
                    }
                    let mut work = database.begin().await?;
                    if source_after_activation(&mut work, repository_id, at).await? {
                        let actor: Actor = serde_json::from_value(event["actor"].clone())?;
                        let author: Actor = serde_json::from_value(issue["user"].clone())?;
                        let source_id = event["id"].as_i64().context("close event ID missing")?;
                        let request = Request {
                            id: Uuid::now_v7(),
                            channel: "issue_close".to_owned(),
                            reply: format!("issue_closed:{number}"),
                            comment_id: None,
                            issue: IssueKey {
                                repository_id,
                                number,
                            },
                            issue_author: author,
                            actor,
                            kind: RequestKind::Stop,
                            order: SourceOrder {
                                at,
                                source_id,
                                source: format!("issue_closed:event:{source_id}"),
                                index: 0,
                            },
                        };
                        record_request(&mut work, &request).await?;
                    }
                    work.commit().await?;
                }
            }
            if let Some(target) = standalone.targets.get(&repository_id)
                && !issue["pull_request"].is_null()
                && let Some(event) = events.iter().find(|event| {
                    matches!(
                        event["event"].as_str(),
                        Some("review_requested" | "review_request_removed")
                    ) && event["requested_reviewer"]["id"] == target.reviewer_id
                        && !event["actor"].is_null()
                })
            {
                let at = DateTime::parse_from_rfc3339(
                    event["created_at"]
                        .as_str()
                        .context("review event time missing")?,
                )?
                .with_timezone(&Utc);
                scan_standalone_review_events(
                    database,
                    standalone,
                    &concurro_tasks_postgres::StandaloneReviewWake {
                        delivery_id: format!("gap:{}:review:{number}", gap.event_id),
                        repository_id,
                        upstream: repository.to_owned(),
                        pr_number: number,
                        action: event["event"].as_str().unwrap_or_default().to_owned(),
                        actor_id: event["actor"]["id"]
                            .as_i64()
                            .context("review actor missing")?,
                        reviewer_id: target.reviewer_id,
                        updated_at: at,
                    },
                )
                .await?;
            }
        }
        if !full {
            break;
        }
        page += 1;
    }
    let unresolved =
        unresolved_coverage_source_count(database, gap.event_id, repository_id).await?;
    ensure!(
        unresolved == 0,
        "{unresolved} coverage sources remain unresolved"
    );
    Ok(())
}

/// Complete one gap only after all configured source and active-task reads succeed.
pub async fn reconcile_coverage_gap(
    database: &Database,
    outbox: &Outbox,
    inputs: CoverageInputs<'_>,
    gap: &PendingCoverageGap,
) -> Result<()> {
    let CoverageInputs {
        decisions,
        standalone,
        publication,
        comments,
        index_targets,
    } = inputs;
    let mut repositories = std::collections::BTreeMap::new();
    let mut failures = Vec::new();
    for (id, target) in &decisions.targets {
        match decisions
            .github
            .repository_installation_id(&target.repository)
            .await
        {
            Ok(installation) if installation == gap.installation_id => {
                repositories.insert(
                    *id,
                    (target.repository.clone(), target.read_permissions.clone()),
                );
            }
            Ok(_) => {}
            Err(error) => failures.push(format!("target {}: {error}", target.repository)),
        }
    }
    for (id, target) in &standalone.targets {
        match decisions
            .github
            .repository_installation_id(&target.source.upstream)
            .await
        {
            Ok(installation) if installation == gap.installation_id => {
                repositories.entry(*id).or_insert_with(|| {
                    (
                        target.source.upstream.clone(),
                        target.source.upstream_permissions.clone(),
                    )
                });
            }
            Ok(_) => {}
            Err(error) => failures.push(format!("target {}: {error}", target.source.upstream)),
        }
    }
    for (id, (repository, permissions)) in &repositories {
        if let Err(error) = scan_repository(
            database,
            outbox,
            decisions,
            standalone,
            comments,
            gap,
            CoverageRepository {
                repository_id: *id,
                repository,
                permissions,
            },
        )
        .await
        {
            failures.push(format!("repository {repository}: {error}"));
        }
    }
    for target in index_targets {
        if repositories.contains_key(&target.issue_repository_id)
            && let Err(error) = scan_target_index(database, &decisions.github, target).await
        {
            failures.push(format!("index {}: {error}", target.upstream));
        }
    }
    for issue in pending_issues(database).await? {
        if repositories.contains_key(&issue.repository_id)
            && let Err(error) = run_task_decision(database, decisions, issue).await
        {
            failures.push(format!("decision {issue:?}: {error}"));
        }
    }
    for issue in watched_gate_issues(database).await? {
        if repositories.contains_key(&issue.repository_id)
            && let Err(error) = run_task_gates(database, publication, issue).await
        {
            failures.push(format!("gates {issue:?}: {error}"));
        }
    }
    for (task_id, observe_pr) in watched_standalone_review_prs(database).await? {
        if observe_pr {
            let repository_id = standalone_task_repository_id(database, task_id).await?;
            if repositories.contains_key(&repository_id)
                && let Err(error) =
                    run_standalone_review_pr_state(database, standalone, task_id).await
            {
                failures.push(format!("review {task_id}: {error}"));
            }
        }
    }
    if !failures.is_empty() {
        bail!(
            "{} coverage reads remain pending: {}",
            failures.len(),
            failures.join("; ")
        );
    }
    complete_coverage_gap(
        database,
        outbox,
        gap.event_id,
        &repositories.keys().copied().collect::<Vec<_>>(),
    )
    .await?;
    Ok(())
}
