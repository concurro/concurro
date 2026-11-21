//! Pinned standalone reviewer inputs for the shared Exchange runner.

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use concurro_contracts::TaskId;
use concurro_tasks_core::{
    AttemptSpec, DesignReviewer, InitializationSnapshot, IssueKey, legacy_requirements,
    python_digest, standalone_pr_review_binding,
};
use concurro_tasks_postgres::StandaloneReviewRound;
use serde_json::{Map, Value, json};
use uuid::Uuid;

use crate::{
    InitializationTarget, IntakeTransport, design_payload::selected_policy,
    plan_payload::prepare_execution_attempt,
};

/// Build one independent reviewer turn from a frozen standalone PR round.
#[allow(clippy::too_many_arguments)]
pub(crate) fn prepare_standalone_review_attempt(
    round: &StandaloneReviewRound,
    snapshot: &InitializationSnapshot,
    target: &InitializationTarget,
    transport: &IntakeTransport,
    expected: Value,
    reviewer: &DesignReviewer,
    own_findings: &[Value],
    correction: Option<(&Value, &str)>,
    turn: Uuid,
    now: DateTime<Utc>,
) -> Result<AttemptSpec> {
    const MAX_DIFF_BYTES: usize = 8192;
    let source = &round.snapshot;
    let head = source["head"].as_str().context("review head missing")?;
    let base = source["merge_base"]
        .as_str()
        .context("review merge base missing")?;
    let base_ref = source["base_ref"]
        .as_str()
        .context("review base ref missing")?;
    let files = source["files"]
        .as_array()
        .context("review file inventory missing")?;
    let mut changes = Vec::with_capacity(files.len());
    let mut unrendered = Vec::new();
    for file in files {
        let path = file["filename"]
            .as_str()
            .context("review file path missing")?;
        let Some(patch) = file["patch"].as_str() else {
            unrendered.push(json!({"path":path,"note":"GitHub did not supply a text patch"}));
            continue;
        };
        let mut end = patch.len().min(MAX_DIFF_BYTES);
        while !patch.is_char_boundary(end) {
            end -= 1;
        }
        changes.push(json!({
            "path":path,
            "status":file["status"],
            "head_blob_sha":file["sha"],
            "diff":&patch[..end],
            "truncated":end < patch.len(),
        }));
    }
    let paths = source["changed_paths"]
        .as_array()
        .context("review changed paths missing")?
        .iter()
        .map(|path| path.as_str().context("review changed path is invalid"))
        .collect::<Result<Vec<_>>>()?;
    let policy = selected_policy(snapshot, &paths);
    let review_ref = format!("concurro/review/pr-{}", round.pr_number);
    let refs = Value::Object(Map::from_iter([
        (base_ref.to_owned(), Value::String(base.to_owned())),
        (review_ref.clone(), Value::String(head.to_owned())),
    ]));
    let mut context = json!({
        "issue":legacy_requirements(&snapshot.requirements),
        "policy":policy,
        "base":base,
        "base_branch":base_ref,
        "bundle_id":snapshot.bundle_id,
        "charter":serde_json::from_str::<Value>(&snapshot.charter)?,
        "prior_outcomes_untrusted":[],
        "changes":changes,
        "head":head,
        "published_pr_body":source["body"],
        "own_findings":own_findings,
        "review_round":round.number,
        "pull_request_author":source["author"],
        "review_output_budget":{"max_json_bytes":300000,"encoding":"UTF-8 JSON"},
        "reviewer_identity":reviewer,
        "pr_review_binding":standalone_pr_review_binding(snapshot, head, reviewer)
            .map_err(anyhow::Error::msg)?,
        "repository_commit":head,
        "repository":{"refs":refs,"head":review_ref},
    });
    if !unrendered.is_empty() {
        context["unrendered_changes"] = json!(unrendered);
    }
    if let Some((report, reason)) = correction {
        context["review_validation_feedback"] = json!({
            "invalid_report_untrusted":report,
            "reason":reason,
            "instruction":"Correct the same review for the pinned head and reviewer identity.",
        });
    }
    let attention = paths
        .iter()
        .filter(|path| {
            path.starts_with(".github/")
                || matches!(
                    path.rsplit('/').next(),
                    Some(
                        "AGENTS.md"
                            | "CLAUDE.md"
                            | "CONTRIBUTING.md"
                            | "SECURITY.md"
                            | "CODEOWNERS"
                            | "GOVERNANCE.md"
                    )
                )
        })
        .collect::<Vec<_>>();
    if !attention.is_empty() {
        context["workflow_and_policy_changes"] = json!(attention);
        context["workflow_and_policy_review"] = json!(
            "Review changes to CI, workflows and policy documents explicitly, including whether the required checks still validate the change."
        );
    }
    if let Some(agent) = &reviewer.agent {
        context["agent"] = agent.clone();
    }
    let harness = reviewer
        .agent
        .as_ref()
        .and_then(|agent| agent["harness"].as_str())
        .unwrap_or(&reviewer.provider);
    let effort = reviewer
        .effort
        .as_deref()
        .unwrap_or(if harness == "claude" { "medium" } else { "low" });
    let phase = python_digest(&json!(["review", head, reviewer.id]))?;
    prepare_execution_attempt(
        IssueKey {
            repository_id: round.repository_id,
            number: round.pr_number,
        },
        TaskId(round.task_id),
        u64::try_from(round.number)?,
        snapshot,
        target,
        transport,
        expected,
        "review",
        phase,
        context,
        (
            harness.to_owned(),
            reviewer.model.clone(),
            effort.to_owned(),
            reviewer.agent.clone(),
        ),
        None,
        true,
        turn,
        now,
    )
}
