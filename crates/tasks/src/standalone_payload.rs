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

use crate::plan_payload::ExecutionAttemptInputs;
use crate::{
    InitializationTarget, IntakeTransport, bundle::ResolvedBundle, design_payload::selected_policy,
    plan_payload::prepare_execution_attempt,
};

/// Frozen standalone PR source and reviewer feedback for one native turn.
pub struct StandaloneReviewAttemptInputs<'a> {
    /// Verified resource bytes and controller wording for this exact turn.
    pub bundle: ResolvedBundle,
    /// Runner OIDC claims rechecked before admission.
    pub expected: Value,
    /// Independent reviewer identity and model configuration.
    pub reviewer: &'a DesignReviewer,
    /// Findings owned by this reviewer for disposition against this head.
    pub own_findings: &'a [Value],
    /// Prior invalid report and bounded repair feedback, if present.
    pub correction: Option<(&'a Value, &'a str)>,
    /// Immutable turn identity shared by admission, runner and replay.
    pub turn: Uuid,
    /// Admission clock used to derive queue and execution deadlines.
    pub now: DateTime<Utc>,
}

/// Build one independent reviewer turn from a frozen standalone PR round.
pub(crate) fn prepare_standalone_review_attempt(
    round: &StandaloneReviewRound,
    snapshot: &InitializationSnapshot,
    target: &InitializationTarget,
    transport: &IntakeTransport,
    attempt_inputs: StandaloneReviewAttemptInputs<'_>,
) -> Result<AttemptSpec> {
    let StandaloneReviewAttemptInputs {
        bundle,
        expected,
        reviewer,
        own_findings,
        correction,
        turn,
        now,
    } = attempt_inputs;
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
            "instruction":bundle.instruction("review_correction")?,
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
        context["workflow_and_policy_review"] =
            json!(bundle.instruction("standalone_policy_review")?);
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
        ExecutionAttemptInputs {
            expected,
            stage: "review",
            phase,
            context,
            agent_config: (
                harness.to_owned(),
                reviewer.model.clone(),
                effort.to_owned(),
                reviewer.agent.clone(),
            ),
            retained: None,
            standalone_review: true,
            turn,
            now,
            bundle: &bundle,
        },
    )
}
