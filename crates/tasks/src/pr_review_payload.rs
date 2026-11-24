//! Independent PR reviewer requests pinned to one published commit.

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use concurro_tasks_core::{
    AttemptSpec, DesignReviewer, IssueKey, TaskState, edit_policy, effective_limits,
    legacy_requirements, pr_review_binding, python_digest,
};
use concurro_tasks_postgres::PrReviewBatch;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::plan_payload::ExecutionAttemptInputs;
use crate::{
    InitializationTarget, IntakeTransport,
    bundle::ResolvedBundle,
    design_payload::selected_policy,
    plan_payload::{RetainedAttempt, prepare_task_attempt},
};

/// Published-head evidence and owned findings for one independent PR assessment.
pub struct PrReviewAttemptInputs<'a> {
    /// Verified resource bytes and controller wording for this exact turn.
    pub bundle: &'a ResolvedBundle,
    /// Runner OIDC claims rechecked before admission.
    pub expected: Value,
    /// Frozen plan or published-head batch defining this review.
    pub batch: &'a PrReviewBatch,
    /// Independent reviewer identity and model configuration.
    pub reviewer: &'a DesignReviewer,
    /// Pinned file contents and diffs for this published head.
    pub changes: &'a [Value],
    /// Published PR description corresponding to the frozen head.
    pub published_body: &'a str,
    /// Earlier outcomes supplied as untrusted historical evidence.
    pub prior_outcomes: &'a [Value],
    /// Findings owned by this reviewer for disposition against this head.
    pub own_findings: &'a [Value],
    /// Prior invalid report and bounded repair feedback, if present.
    pub correction: Option<(&'a Value, &'a str)>,
    /// Exact predecessor journal and session binding for this role.
    pub retained: &'a RetainedAttempt,
    /// Immutable turn identity shared by admission, runner and replay.
    pub turn: Uuid,
    /// Admission clock used to derive queue and execution deadlines.
    pub now: DateTime<Utc>,
    /// Applicable advisory intake remarks with matching source provenance.
    pub intake_remarks: &'a [String],
}

/// Freeze a private reviewer request against the verified PR head and file identities.
pub(crate) fn prepare_pr_review_attempt(
    issue: IssueKey,
    task: &TaskState,
    target: &InitializationTarget,
    transport: &IntakeTransport,
    attempt_inputs: PrReviewAttemptInputs<'_>,
) -> Result<AttemptSpec> {
    let PrReviewAttemptInputs {
        bundle,
        expected,
        batch,
        reviewer,
        changes,
        published_body,
        prior_outcomes,
        own_findings,
        correction,
        retained,
        turn,
        now,
        intake_remarks,
    } = attempt_inputs;
    let snapshot = &batch.snapshot;
    let config = &snapshot.configuration;
    let harness = reviewer
        .agent
        .as_ref()
        .and_then(|agent| agent["harness"].as_str())
        .unwrap_or(&reviewer.provider);
    let effort = reviewer
        .effort
        .as_deref()
        .unwrap_or(if harness == "claude" { "medium" } else { "low" });
    let base_branch = config["base"]
        .as_str()
        .context("target base branch is missing")?;
    let paths = batch
        .changed_paths
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>();
    let policy = selected_policy(snapshot, &paths);
    let attention = paths
        .iter()
        .filter(|path| {
            path.starts_with(".github/")
                || config["policy_documents"]
                    .as_array()
                    .is_some_and(|documents| {
                        documents.iter().any(|item| item.as_str() == Some(path))
                    })
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
        .copied()
        .collect::<Vec<_>>();
    let refs = Value::Object(Map::from_iter([
        (
            base_branch.to_owned(),
            Value::String(snapshot.pins.base_sha.clone()),
        ),
        (batch.branch.clone(), Value::String(batch.head.clone())),
        (
            "concurro/published".to_owned(),
            Value::String(batch.head.clone()),
        ),
    ]));
    let mut context = json!({
        "issue":legacy_requirements(&snapshot.requirements),
        "policy":policy,
        "edit_policy":edit_policy(config)?,
        "limits":effective_limits(config)?,
        "plan":batch.plan,
        "base":snapshot.pins.base_sha,
        "bundle_id":snapshot.bundle_id,
        "charter":serde_json::from_str::<Value>(&snapshot.charter)?,
        "prior_outcomes_untrusted":prior_outcomes,
        "changes":changes,
        "head":batch.head,
        "published_pr_body":published_body,
        "own_findings":own_findings,
        "review_output_budget":{"max_json_bytes":300000,"encoding":"UTF-8 JSON"},
        "reviewer_identity":reviewer,
        "pr_review_binding":pr_review_binding(snapshot, &batch.plan, &batch.plan_id, &batch.head, reviewer)
            .map_err(anyhow::Error::msg)?,
        "repository_commit":batch.head,
        "repository":{"refs":refs,"head":batch.branch},
    });
    if !intake_remarks.is_empty() {
        context["intake_remarks"] = json!(intake_remarks);
    }
    if !attention.is_empty() {
        context["workflow_and_policy_changes"] = json!(attention);
        context["workflow_and_policy_review"] =
            json!(bundle.instruction("contribution_policy_review")?);
    }
    if let Some((report, reason)) = correction {
        context["review_validation_feedback"] = json!({
            "invalid_report_untrusted":report,
            "reason":reason,
            "instruction":bundle.instruction("review_correction")?,
        });
    }
    if let Some(agent) = &reviewer.agent {
        context["agent"] = agent.clone();
    }
    let phase = python_digest(&json!(["review", batch.head, reviewer.id]))?;
    prepare_task_attempt(
        issue,
        task,
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
            retained: Some(retained),
            turn,
            now,
            bundle,
            standalone_review: false,
        },
    )
}

/// Include a bounded unified diff alongside identities of the complete pinned files.
pub(crate) fn review_change(path: &str, before: Option<&str>, after: Option<&str>) -> Value {
    const MAX_DIFF_BYTES: usize = 8192;
    let digest =
        |content: Option<&str>| content.map(|value| hex::encode(Sha256::digest(value.as_bytes())));
    let old = before.unwrap_or("");
    let new = after.unwrap_or("");
    let old_lines = old.lines().collect::<Vec<_>>();
    let new_lines = new.lines().collect::<Vec<_>>();
    let mut diff = format!(
        "--- {}\n+++ {}\n@@ -{},{} +{},{} @@\n",
        if before.is_some() {
            format!("a/{path}")
        } else {
            "/dev/null".to_owned()
        },
        if after.is_some() {
            format!("b/{path}")
        } else {
            "/dev/null".to_owned()
        },
        if old_lines.is_empty() { 0 } else { 1 },
        old_lines.len(),
        if new_lines.is_empty() { 0 } else { 1 },
        new_lines.len(),
    );
    for line in old_lines {
        diff.push('-');
        diff.push_str(line);
        diff.push('\n');
    }
    if !old.is_empty() && !old.ends_with('\n') {
        diff.push_str("\\ No newline at end of file\n");
    }
    for line in new_lines {
        diff.push('+');
        diff.push_str(line);
        diff.push('\n');
    }
    if !new.is_empty() && !new.ends_with('\n') {
        diff.push_str("\\ No newline at end of file\n");
    }
    let truncated = diff.len() > MAX_DIFF_BYTES;
    if truncated {
        let mut end = MAX_DIFF_BYTES;
        while !diff.is_char_boundary(end) {
            end -= 1;
        }
        diff.truncate(end);
    }
    json!({
        "path":path,
        "status":if before.is_none() { "added" } else if after.is_none() { "deleted" } else { "modified" },
        "base_sha256":digest(before),
        "head_sha256":digest(after),
        "base_bytes":before.map_or(0, str::len),
        "head_bytes":after.map_or(0, str::len),
        "diff":diff,
        "truncated":truncated,
    })
}
