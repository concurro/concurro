//! Timeline-backed opt-in label requests.

use std::collections::BTreeMap;

use anyhow::{Context, Result, ensure};
use chrono::{DateTime, Utc};
use concurro_db::Database;
use concurro_github_v1::AppClient;
use concurro_tasks_core::{Actor, IssueKey, LaterCommand, Request, RequestKind, SourceOrder};
use concurro_tasks_postgres::{
    defer_label_wake, observe_label_wake, record_request, source_after_activation,
};
use graphile_worker::{IntoTaskHandlerResult, TaskHandler, WorkerContext};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use uuid::Uuid;

use crate::DecisionContext;

/// Columns read by `opt_in_events`.
type OptInEventsRow = (i64, DateTime<Utc>, Actor, bool);

/// One issue's label-delivery wake, anchored to its GitHub update time.
#[derive(Deserialize, Serialize)]
pub struct LabelScanJob {
    /// Stable delivery GUID for durable readback.
    pub delivery_id: String,
    /// Numeric target repository ID.
    pub repository_id: i64,
    /// Repository-local issue number.
    pub issue_number: i64,
    /// Delivery action to observe in the timeline.
    pub action: String,
    /// Numeric sender identity from the authenticated delivery.
    pub actor_id: i64,
    /// GitHub issue update time in the delivery.
    pub updated_at: DateTime<Utc>,
}

impl TaskHandler for LabelScanJob {
    const IDENTIFIER: &'static str = "task_label_scan";

    /// Record every visible opt-in label event, retrying a missing delivery event.
    async fn run(self, context: WorkerContext) -> impl IntoTaskHandlerResult {
        let decisions = context
            .get_ext::<DecisionContext>()
            .ok_or("task decision context unavailable")?;
        let database = Database::from_pool(context.pg_pool().clone());
        scan_label_wake(&database, decisions, &self)
            .await
            .map_err(|error| error.to_string())
    }
}

/// Read all per-issue events so delivery and reconciliation share source identities.
pub(crate) async fn read_events(
    github: &AppClient,
    repository: &str,
    read_permissions: &BTreeMap<String, String>,
    issue: i64,
) -> Result<Vec<Value>> {
    let (owner, name) = repository
        .split_once('/')
        .context("label target repository must be owner/name")?;
    let issue = issue.to_string();
    let mut events = Vec::new();
    let mut page = 1;
    loop {
        let page_text = page.to_string();
        let batch: Vec<Value> = github
            .read(
                repository,
                read_permissions,
                &["repos", owner, name, "issues", &issue, "events"],
                &[("per_page", "100"), ("page", &page_text)],
            )
            .await?;
        let full = batch.len() == 100;
        events.extend(batch);
        if !full {
            break;
        }
        page += 1;
    }
    Ok(events)
}

/// Return label events with the exact opt-in name in source order.
fn opt_in_events(events: &[Value]) -> Result<Vec<OptInEventsRow>> {
    let mut listed = Vec::new();
    for event in events {
        let labeled = match event["event"].as_str() {
            Some("labeled") => true,
            Some("unlabeled") => false,
            _ => continue,
        };
        if event["label"]["name"] != "concurro" {
            continue;
        }
        if event["actor"].is_null() {
            continue;
        }
        let id = event["id"].as_i64().context("label event ID missing")?;
        let at = DateTime::parse_from_rfc3339(
            event["created_at"]
                .as_str()
                .context("label event time missing")?,
        )?
        .with_timezone(&Utc);
        let actor =
            serde_json::from_value(event["actor"].clone()).context("label event actor missing")?;
        listed.push((id, at, actor, labeled));
    }
    listed.sort_by_key(|(id, at, _, _)| (*at, *id));
    Ok(listed)
}

/// Create one durable request per visible opt-in event and verify the delivery wake.
pub async fn scan_label_events(
    database: &Database,
    context: &DecisionContext,
    wake: &LabelScanJob,
) -> Result<()> {
    let target = context
        .targets
        .get(&wake.repository_id)
        .context("label target unavailable")?;
    let (owner, name) = target
        .repository
        .split_once('/')
        .context("label target repository must be owner/name")?;
    let number = wake.issue_number.to_string();
    let issue: Value = context
        .github
        .read(
            &target.repository,
            &target.read_permissions,
            &["repos", owner, name, "issues", &number],
            &[],
        )
        .await?;
    ensure!(
        issue["number"] == wake.issue_number,
        "label issue identity changed"
    );
    let author: Actor = serde_json::from_value(issue["user"].clone())?;
    let events = read_events(
        &context.github,
        &target.repository,
        &target.read_permissions,
        wake.issue_number,
    )
    .await?;
    let listed = opt_in_events(&events)?;
    let mut work = database.begin().await?;
    for (id, at, actor, labeled) in &listed {
        if !source_after_activation(&mut work, wake.repository_id, *at).await? {
            continue;
        }
        let request = Request {
            id: Uuid::now_v7(),
            channel: "label".to_owned(),
            reply: format!("issue_label:{id}"),
            comment_id: None,
            issue: IssueKey {
                repository_id: wake.repository_id,
                number: wake.issue_number,
            },
            issue_author: author.clone(),
            actor: actor.clone(),
            kind: if *labeled {
                RequestKind::Start
            } else {
                RequestKind::Later(LaterCommand::Unenroll)
            },
            order: SourceOrder {
                at: *at,
                source_id: *id,
                source: format!("issue_label:{id}"),
                index: 0,
            },
        };
        record_request(&mut work, &request).await?;
    }
    work.commit().await?;
    let observed = listed.iter().any(|(_, at, actor, labeled)| {
        *at == wake.updated_at
            && actor.id == wake.actor_id
            && if *labeled {
                wake.action == "labeled"
            } else {
                wake.action == "unlabeled"
            }
    });
    ensure!(observed, "label delivery event not in issue timeline yet");
    observe_label_wake(database, &wake.delivery_id).await?;
    Ok(())
}

/// Apply durable backoff to every failed delivery scan, retaining exhausted evidence.
pub async fn scan_label_wake(
    database: &Database,
    context: &DecisionContext,
    wake: &LabelScanJob,
) -> Result<()> {
    if let Err(error) = scan_label_events(database, context, wake).await {
        let exhausted = defer_label_wake(database, &wake.delivery_id, &error.to_string()).await?;
        if exhausted {
            tracing::warn!(delivery_id = %wake.delivery_id, %error, "label wake exhausted; reconciliation required");
        }
        return Err(error);
    }
    Ok(())
}

/// Check the current issue and last opt-in event before applying a label start.
pub(crate) async fn current_label_source(
    github: &AppClient,
    repository: &str,
    read_permissions: &BTreeMap<String, String>,
    request: &Request,
) -> Result<bool> {
    let (owner, name) = repository
        .split_once('/')
        .context("label target repository must be owner/name")?;
    let number = request.issue.number.to_string();
    let issue: Value = github
        .read(
            repository,
            read_permissions,
            &["repos", owner, name, "issues", &number],
            &[],
        )
        .await?;
    let has_label = issue["labels"]
        .as_array()
        .is_some_and(|labels| labels.iter().any(|label| label["name"] == "concurro"));
    if !has_label {
        return Ok(false);
    }
    let events = read_events(github, repository, read_permissions, request.issue.number).await?;
    Ok(opt_in_events(&events)?
        .last()
        .is_some_and(|(id, at, actor, labeled)| {
            *labeled
                && *id == request.order.source_id
                && *at == request.order.at
                && actor.id == request.actor.id
        }))
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::opt_in_events;

    #[test]
    fn label_timeline_keeps_distinct_repeated_sources() {
        let events = vec![
            json!({"id": 103, "event": "labeled", "created_at": "2026-11-20T10:00:02Z",
                "actor": {"id": 7, "login": "writer"}, "label": {"name": "concurro"}}),
            json!({"id": 101, "event": "labeled", "created_at": "2026-11-20T10:00:00Z",
                "actor": {"id": 7, "login": "writer"}, "label": {"name": "concurro"}}),
            json!({"id": 102, "event": "unlabeled", "created_at": "2026-11-20T10:00:01Z",
                "actor": {"id": 8, "login": "other"}, "label": {"name": "concurro"}}),
            json!({"id": 104, "event": "labeled", "created_at": "2026-11-20T10:00:03Z",
                "actor": {"id": 7, "login": "writer"}, "label": {"name": "unrelated"}}),
            json!({"id": 105, "event": "unlabeled", "created_at": "2026-11-20T10:00:04Z",
                "actor": null, "label": {"name": "concurro"}}),
        ];
        let listed = opt_in_events(&events).expect("label events");
        assert_eq!(
            listed
                .iter()
                .map(|(id, _, _, labeled)| (*id, *labeled))
                .collect::<Vec<_>>(),
            vec![(101, true), (102, false), (103, true)]
        );
    }
}
