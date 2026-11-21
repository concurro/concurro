//! Writer-approved implementer submission from the pinned design.

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use concurro_tasks_core::{
    AttemptSpec, InitializationSnapshot, IssueKey, TaskState, edit_policy, effective_limits,
    legacy_requirements, python_digest,
};
use concurro_tasks_postgres::{ImplementationCorrection, ImplementationInputs};
use serde_json::{Map, Value, json};
use uuid::Uuid;

use crate::{
    InitializationTarget, IntakeTransport,
    design_payload::selected_policy,
    intake_payload::assigned_agent,
    plan_payload::{RetainedAttempt, prepare_task_attempt},
};

/// Freeze one implementer request under the exact writer-approved design binding.
#[allow(clippy::too_many_arguments)]
pub(crate) fn prepare_implementation_attempt(
    issue: IssueKey,
    task: &TaskState,
    snapshot: &InitializationSnapshot,
    target: &InitializationTarget,
    transport: &IntakeTransport,
    expected: Value,
    inputs: &ImplementationInputs,
    correction: Option<&ImplementationCorrection>,
    retained: &RetainedAttempt,
    turn: Uuid,
    now: DateTime<Utc>,
) -> Result<AttemptSpec> {
    let config = &snapshot.configuration;
    let (harness, model, effort, agent) = assigned_agent(config, "implementer")?;
    let base = config["base"]
        .as_str()
        .context("target base branch is missing")?;
    let paths = inputs.plan["files"]
        .as_array()
        .context("approved plan paths are invalid")?
        .iter()
        .map(|path| path.as_str().context("approved path is invalid"))
        .collect::<Result<Vec<_>>>()?;
    let mut refs = Map::from_iter([(
        base.to_owned(),
        Value::String(snapshot.pins.base_sha.clone()),
    )]);
    if let Some(branch) = &inputs.branch {
        refs.insert(branch.clone(), Value::String(inputs.parent.clone()));
    }
    let checkout = inputs.branch.as_deref().unwrap_or(base);
    let mut context = json!({
        "issue":legacy_requirements(&snapshot.requirements),
        "policy":selected_policy(snapshot, &paths),
        "edit_policy":edit_policy(config)?,
        "limits":effective_limits(config)?,
        "plan":inputs.plan,
        "base":snapshot.pins.base_sha,
        "bundle_id":snapshot.bundle_id,
        "charter":serde_json::from_str::<Value>(&snapshot.charter)?,
        "prior_outcomes_untrusted":[],
        "prior_reviews":inputs.prior_reviews,
        "review_findings":inputs.review_findings,
        "implementation_feedback":correction.map(|correction| format!(
            "{}. Reduce the change or request a design reopen. Nothing was applied.",
            correction.reason
        )),
        "design_authorization":inputs.authorization,
        "conventional_commits":config.get("conventional_commits").cloned().unwrap_or_else(|| json!(false)),
        "current_description":inputs.current_description,
        "current_validation":inputs.current_validation,
        "repository_commit":inputs.parent,
        "repository":{"refs":Value::Object(refs),"head":checkout},
    });
    if let Some(agent) = &agent {
        context["agent"] = agent.clone();
    }
    let phase = if let Some(agent) = &agent {
        python_digest(&json!(["single", agent]))?
    } else {
        "single".to_owned()
    };
    prepare_task_attempt(
        issue,
        task,
        snapshot,
        target,
        transport,
        expected,
        "develop",
        phase,
        context,
        (harness, model, effort, agent),
        Some(retained),
        turn,
        now,
    )
}
