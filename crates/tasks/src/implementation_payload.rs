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

use crate::plan_payload::ExecutionAttemptInputs;
use crate::{
    InitializationTarget, IntakeTransport,
    bundle::ResolvedBundle,
    design_payload::selected_policy,
    intake_payload::assigned_agent,
    plan_payload::{RetainedAttempt, prepare_task_attempt},
};

/// Writer-approved design, source parent and feedback for one implementer turn.
pub struct ImplementationAttemptInputs<'a> {
    /// Verified resource bytes and controller wording for this exact turn.
    pub bundle: ResolvedBundle,
    /// Runner OIDC claims rechecked before admission.
    pub expected: Value,
    /// Approved plan, authorization and parent files for implementation.
    pub inputs: &'a ImplementationInputs,
    /// Prior invalid report and bounded repair feedback, if present.
    pub correction: Option<&'a ImplementationCorrection>,
    /// Exact predecessor journal and session binding for this role.
    pub retained: &'a RetainedAttempt,
    /// Immutable turn identity shared by admission, runner and replay.
    pub turn: Uuid,
    /// Admission clock used to derive queue and execution deadlines.
    pub now: DateTime<Utc>,
    /// Applicable advisory intake remarks with matching source provenance.
    pub intake_remarks: &'a [String],
}

/// Freeze one implementer request under the exact writer-approved design binding.
pub(crate) fn prepare_implementation_attempt(
    issue: IssueKey,
    task: &TaskState,
    snapshot: &InitializationSnapshot,
    target: &InitializationTarget,
    transport: &IntakeTransport,
    attempt_inputs: ImplementationAttemptInputs<'_>,
) -> Result<AttemptSpec> {
    let ImplementationAttemptInputs {
        bundle,
        expected,
        inputs,
        correction,
        retained,
        turn,
        now,
        intake_remarks,
    } = attempt_inputs;
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
    let implementation_feedback = correction
        .map(|correction| {
            Ok::<_, anyhow::Error>(format!(
                "{}. {}",
                correction.reason,
                bundle.instruction("implementation_correction")?
            ))
        })
        .transpose()?;
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
        "implementation_feedback":implementation_feedback,
        "design_authorization":inputs.authorization,
        "conventional_commits":config.get("conventional_commits").cloned().unwrap_or_else(|| json!(false)),
        "current_description":inputs.current_description,
        "current_validation":inputs.current_validation,
        "repository_commit":inputs.parent,
        "repository":{"refs":Value::Object(refs),"head":checkout},
    });
    if !intake_remarks.is_empty() {
        context["intake_remarks"] = json!(intake_remarks);
    }
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
        ExecutionAttemptInputs {
            expected,
            stage: "develop",
            phase,
            context,
            agent_config: (harness, model, effort, agent),
            retained: Some(retained),
            turn,
            now,
            bundle: &bundle,
            standalone_review: false,
        },
    )
}
