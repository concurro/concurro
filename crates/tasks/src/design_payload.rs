//! Independent design-review inputs prepared from one accepted plan.

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use concurro_tasks_core::{
    AttemptSpec, DesignReviewer, InitializationSnapshot, IssueKey, TaskState, design_criteria,
    edit_policy, effective_limits, glob_matches, legacy_requirements, python_digest,
};
use concurro_tasks_postgres::{DesignBatch, DesignCorrection};
use serde_json::{Map, Value, json};
use uuid::Uuid;

use crate::{
    InitializationTarget, IntakeTransport,
    plan_payload::{RetainedAttempt, prepare_task_attempt},
};

const OBJECTION_INSTRUCTIONS: &str = "For each new concern, add an objection_updates entry with id \"\" and status \"open\"; the controller assigns its ID. Reuse an ID only from own_objections, exactly once per supplied objection. concerns must equal the detail strings of all open entries in order. A revise verdict needs at least one open entry, even when own_objections is empty.";

/// Freeze one reviewer's private first-pass assessment against the shared batch.
#[allow(clippy::too_many_arguments)]
pub(crate) fn prepare_design_attempt(
    issue: IssueKey,
    task: &TaskState,
    snapshot: &InitializationSnapshot,
    target: &InitializationTarget,
    transport: &IntakeTransport,
    expected: Value,
    batch: &DesignBatch,
    reviewer: &DesignReviewer,
    own_objections: &[Value],
    manifest: &Value,
    correction: Option<&DesignCorrection>,
    retained: Option<&RetainedAttempt>,
    turn: Uuid,
    now: DateTime<Utc>,
) -> Result<AttemptSpec> {
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
    let base = config["base"]
        .as_str()
        .context("target base branch is missing")?;
    let paths = batch.plan["files"]
        .as_array()
        .context("accepted plan files are invalid")?
        .iter()
        .map(|path| path.as_str().context("accepted plan path is invalid"))
        .collect::<Result<Vec<_>>>()?;
    let policy = selected_policy(snapshot, &paths);
    let mut plan = batch.plan.clone();
    let plan_fields = plan.as_object_mut().context("accepted plan is invalid")?;
    plan_fields.remove("publication");
    if batch.phase == "fresh" {
        plan_fields.remove("responses");
    }
    let refs = Value::Object(Map::from_iter([(
        base.to_owned(),
        Value::String(snapshot.pins.base_sha.clone()),
    )]));
    let mut context = json!({
        "issue": legacy_requirements(&snapshot.requirements),
        "policy": policy,
        "edit_policy": edit_policy(config)?,
        "limits": effective_limits(config)?,
        "plan": plan,
        "base": snapshot.pins.base_sha,
        "bundle_id": snapshot.bundle_id,
        "charter": serde_json::from_str::<Value>(&snapshot.charter)?,
        "prior_outcomes_untrusted": [],
        "review_phase": batch.phase,
        "review_criteria": design_criteria(&snapshot.requirements),
        "objection_instructions": OBJECTION_INSTRUCTIONS,
        "own_objections": own_objections,
        "files": manifest,
        "design_binding": batch.binding,
        "reviewer_identity": reviewer,
        "accumulated_changed_files": [],
        "published_parent": null,
        "review_output_budget": {"max_json_bytes":300000,"encoding":"UTF-8 JSON"},
        "repository_commit": snapshot.pins.base_sha,
        "repository": {"refs":refs,"head":base},
    });
    if batch.phase == "fresh" {
        context
            .as_object_mut()
            .expect("review context")
            .remove("own_objections");
    }
    if let Some(agent) = &reviewer.agent {
        context["agent"] = agent.clone();
    }
    if let Some(correction) = correction.filter(|correction| !correction.retry) {
        context["review_validation_feedback"] = json!({
            "detail": correction.reason,
            "output": correction.report,
            "attempt": correction.count,
            "instruction": "Correct the report against its schema and the validation feedback. Reconsider the verdict if necessary; approval is never required. Preserve genuine concerns and unanswered questions, and report actionable objections when the evidence warrants them. This feedback concerns report validity, not a request to approve the design.",
        });
    }
    let phase = python_digest(&json!(["design", batch.binding, reviewer.id]))?;
    prepare_task_attempt(
        issue,
        task,
        snapshot,
        target,
        transport,
        expected,
        "plan_qc",
        phase,
        context,
        (
            harness.to_owned(),
            reviewer.model.clone(),
            effort.to_owned(),
            reviewer.agent.clone(),
        ),
        retained,
        turn,
        now,
    )
}

/// Select pinned policy text that applies to at least one planned file.
pub(crate) fn selected_policy(
    snapshot: &InitializationSnapshot,
    paths: &[&str],
) -> Map<String, Value> {
    snapshot
        .policy_documents
        .iter()
        .filter(|(path, content)| {
            if content.is_empty() {
                return false;
            }
            if !path.starts_with(".github/instructions/") {
                return true;
            }
            let applies_to = content.lines().find_map(|line| {
                line.strip_prefix("applyTo: \"")
                    .and_then(|patterns| patterns.strip_suffix('"'))
            });
            applies_to.is_some_and(|patterns| {
                patterns
                    .split(',')
                    .any(|pattern| paths.iter().any(|path| glob_matches(path, pattern.trim())))
            })
        })
        .map(|(path, content)| (path.clone(), Value::String(content.clone())))
        .collect::<Map<_, _>>()
}
