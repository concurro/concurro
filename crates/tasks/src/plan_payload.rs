//! Planner turn submission from the task's pinned initialization snapshot.

use anyhow::{Context, Result, ensure};
use chrono::{DateTime, Duration, Utc};
use concurro_db::Database;
use concurro_events_postgres::Outbox;
use concurro_exchange_v1::TurnSubmission;
use concurro_tasks_core::{
    AttemptSpec, InitializationSnapshot, IssueKey, TaskState, edit_policy, editable_files,
    effective_limits, legacy_requirements, plan_output_contract,
};
use concurro_tasks_postgres::{
    PlanCorrection, PlanRevision, refuse_stage_preflight, retained_preparation,
};
use glob::Pattern;
use serde_json::{Map, Value, json};
use uuid::Uuid;

use crate::{
    ArchiveContext, InitializationTarget,
    archive::{RetainedPreflightRefusal, preflight_retained},
    bundle::{plan_prompt_parts, python_digest, python_pretty_json},
    intake_payload::{IntakeTransport, assigned_agent, mcp_servers},
};

/// Preflighted role journal bytes and its exact sequential session identity.
pub(crate) struct RetainedAttempt {
    /// Runner session metadata and predecessor digest.
    pub session: Value,
    /// Original prior gzip bytes for the runner, if continuing.
    pub gzip_base64: Option<String>,
}

/// Prepare the next role turn from the latest exact journal revision.
#[allow(clippy::too_many_arguments)]
pub(crate) async fn prepare_retained_attempt(
    database: &Database,
    archive: &ArchiveContext,
    outbox: &Outbox,
    task: &TaskState,
    snapshot: &InitializationSnapshot,
    stage: &str,
    provider: &str,
    model: &str,
    reviewer_id: Option<&str>,
    session_epoch: i32,
) -> Result<RetainedAttempt> {
    let config = &snapshot.configuration;
    let role = format!("{stage}:{provider}:{model}");
    let scope = python_digest(&json!([
        task.id.0.to_string(),
        config["repository"],
        task.issue.number,
        provider,
        model,
        stage,
        snapshot.pins.base_sha,
        legacy_requirements(&snapshot.requirements),
        snapshot.bundle_id,
        python_digest(config)?,
        reviewer_id,
        session_epoch,
    ]))?;
    let preparation = retained_preparation(database, task.id.0, &scope).await?;
    let gzip_base64 = match preflight_retained(archive, &preparation).await {
        Ok(bytes) => bytes,
        Err(error) => {
            if let Some(refusal) = error.downcast_ref::<RetainedPreflightRefusal>() {
                let mut work = database.begin().await?;
                refuse_stage_preflight(
                    &mut work,
                    outbox,
                    task.id.0,
                    task.revision,
                    &format!("Retained continuation unavailable: {}", refusal.0),
                )
                .await?;
                work.commit().await?;
            }
            return Err(error);
        }
    };
    Ok(RetainedAttempt {
        session: json!({
            "scope":scope,
            "turn":preparation.turn,
            "role":role,
            "predecessor_digest":preparation.predecessor_digest,
            "gzip_sha256":preparation.gzip_sha256,
        }),
        gzip_base64,
    })
}

/// Freeze the planner request and exact runner inputs before its admission.
pub(crate) fn prepare_plan_attempt(
    issue: IssueKey,
    task: &TaskState,
    snapshot: &InitializationSnapshot,
    target: &InitializationTarget,
    transport: &IntakeTransport,
    expected: Value,
    correction: Option<&PlanCorrection>,
    revision: Option<&PlanRevision>,
    open_objections: &[Value],
    retained: &RetainedAttempt,
    turn: Uuid,
    now: DateTime<Utc>,
) -> Result<AttemptSpec> {
    let config = &snapshot.configuration;
    let (harness, model, effort, agent) = assigned_agent(config, "planner")?;
    ensure!(
        transport
            .models
            .iter()
            .any(|allowed| Pattern::new(allowed).is_ok_and(|pattern| pattern.matches(&model))),
        "planner model is outside gateway model routes"
    );
    let base = config["base"]
        .as_str()
        .context("target base branch is missing")?;
    let policy = snapshot
        .policy_documents
        .iter()
        .filter(|(path, content)| !path.starts_with(".github/instructions/") && !content.is_empty())
        .map(|(path, content)| (path.clone(), Value::String(content.clone())))
        .collect::<Map<_, _>>();
    let discussion = snapshot
        .requirements
        .discussion
        .iter()
        .map(|comment| json!({"author":comment.author.login,"body":comment.body}))
        .collect::<Vec<_>>();
    let limits = effective_limits(config)?;
    let refs = Value::Object(Map::from_iter([(
        base.to_owned(),
        Value::String(snapshot.pins.base_sha.clone()),
    )]));
    let mut context = json!({
        "issue": {
            "title": snapshot.requirements.title,
            "body": snapshot.requirements.body,
            "discussion": discussion,
        },
        "policy": policy,
        "edit_policy": edit_policy(config)?,
        "limits": limits,
        "plan": revision.map(|prior| &prior.plan),
        "plan_output_contract": plan_output_contract(config)?,
        "base": snapshot.pins.base_sha,
        "bundle_id": snapshot.bundle_id,
        "charter": serde_json::from_str::<Value>(&snapshot.charter)?,
        "prior_outcomes_untrusted": [],
        "editable_files": editable_files(config, &snapshot.base_files)?,
        "open_objections": open_objections,
        "repository_commit": snapshot.pins.base_sha,
        "repository": {"refs":refs,"head":base},
    });
    if let Some(agent) = &agent {
        context["agent"] = agent.clone();
    }
    if let Some(revision) = revision {
        if !revision.design_feedback.is_empty() {
            context["design_feedback"] = json!(revision.design_feedback);
        }
    }
    if let Some(correction) = correction {
        context["plan_validation_feedback"] = json!({
            "output": python_pretty_json(&correction.report)?,
            "truncated": false,
            "reason": correction.reason,
            "attempt": correction.attempt,
        });
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
        "plan",
        phase,
        context,
        (harness, model, effort, agent),
        Some(retained),
        turn,
        now,
    )
}

/// Freeze the common runner envelope for a pinned task stage.
#[allow(clippy::too_many_arguments)]
pub(crate) fn prepare_task_attempt(
    issue: IssueKey,
    task: &TaskState,
    snapshot: &InitializationSnapshot,
    target: &InitializationTarget,
    transport: &IntakeTransport,
    expected: Value,
    stage: &str,
    phase: String,
    context: Value,
    agent_config: (String, String, String, Option<Value>),
    retained: Option<&RetainedAttempt>,
    turn: Uuid,
    now: DateTime<Utc>,
) -> Result<AttemptSpec> {
    let config = &snapshot.configuration;
    let (harness, model, effort, agent) = agent_config;
    ensure!(
        transport
            .models
            .iter()
            .any(|allowed| Pattern::new(allowed).is_ok_and(|pattern| pattern.matches(&model))),
        "task model is outside gateway model routes"
    );
    let base = config["base"]
        .as_str()
        .context("target base branch is missing")?;
    let limits = effective_limits(config)?;
    let (refs, checkout) = if matches!(stage, "review" | "develop") {
        let repository = &context["repository"];
        let refs = repository["refs"]
            .as_object()
            .context("task repository refs are invalid")?;
        let checkout = repository["head"]
            .as_str()
            .context("task repository head is missing")?;
        ensure!(
            refs.get(checkout) == context.get("repository_commit"),
            "task checkout differs from the pinned source head"
        );
        (Value::Object(refs.clone()), checkout.to_owned())
    } else {
        (
            Value::Object(Map::from_iter([(
                base.to_owned(),
                Value::String(snapshot.pins.base_sha.clone()),
            )])),
            base.to_owned(),
        )
    };
    let (mut prompt, schema) = match stage {
        "plan" => plan_prompt_parts(&context, snapshot)?,
        "plan_qc" => crate::bundle::plan_qc_prompt_parts(&context, snapshot)?,
        "develop" => crate::bundle::develop_prompt_parts(&context, snapshot)?,
        "review" => crate::bundle::review_prompt_parts(&context, snapshot)?,
        _ => anyhow::bail!("unsupported task attempt stage"),
    };
    let prompt_bytes = serde_json::to_vec(&prompt)?.len() + serde_json::to_vec(&schema)?.len();
    let maximum = config["max_context_bytes"]
        .as_u64()
        .context("target max_context_bytes is missing or invalid")?;
    ensure!(
        prompt_bytes as u64 <= maximum,
        "plan prompt exceeds the size limit ({prompt_bytes} of {maximum} bytes)"
    );
    prompt["response_schema"] = schema;
    let prompt_body = serde_json::to_vec(&prompt)?;
    let mut request = json!({
        "provider": harness,
        "model": model,
        "effort": effort,
        "stage": stage,
        "gateway": transport.gateway,
        "bundle_id": snapshot.bundle_id,
        "charter": snapshot.charter,
        "instruction_texts": snapshot.instruction_texts,
        "max_retained_bytes": config.get("max_retained_bytes").cloned().unwrap_or_else(|| json!(64 * 1024 * 1024)),
        "context": context,
        "session": retained.map(|value| &value.session),
        "local_candidate": false,
        "evaluation": null,
    });
    if let Some(agent) = &agent {
        request["agent"] = agent.clone();
    }
    if let Some(servers) = mcp_servers(config) {
        request["mcp_servers"] = servers;
    }
    let timeout = if stage == "develop" {
        None
    } else {
        match config.get("turn_timeout_seconds") {
            Some(Value::Null) => None,
            Some(value) => Some(value.as_i64().context("target turn timeout is invalid")?),
            None => Some(1200),
        }
    };
    ensure!(
        timeout.is_none_or(|value| value > 0),
        "target turn timeout is invalid"
    );
    let payload = json!({
        "request": request,
        "feedback": {},
        "spec": {
            "overlay_files_limit": limits["overlay_files"],
            "workspace_files_limit": limits["workspace_files"],
            "refs": refs,
            "head": checkout,
            "base_branch": base,
            "candidate": null,
        },
        "retained": retained.and_then(|value| value.gzip_base64.as_deref()),
        "timeout": timeout,
    });
    let binding = json!({
        "repository": target.issue_repository,
        "issue": issue.number,
        "generation": 1,
        "upstream": target.upstream,
        "fork": target.fork,
        "config_sha": snapshot.pins.config_sha,
        "engine": target.engine_sha,
        "base": snapshot.pins.base_sha,
        "role": format!("{stage}:{harness}"),
        "phase": phase,
    });
    let claims = expected
        .as_object()
        .context("runner claims must be an object")?
        .iter()
        .filter(|(key, _)| key.as_str() != "ref")
        .map(|(key, value)| (key.clone(), value.clone()))
        .collect::<Map<_, _>>();
    let staffing_group = python_digest(&json!([
        claims,
        binding,
        [model],
        transport.budget,
        transport.lease as f64
    ]))?;
    let submission = TurnSubmission {
        turn,
        expected,
        models: vec![model],
        budget: transport.budget,
        lease: transport.lease,
        payload,
        pin: None,
        binding,
    };
    Ok(AttemptSpec {
        id: turn,
        task: task.id,
        expected_revision: task.revision,
        staffing_group,
        fork: target.fork.clone(),
        config_sha: snapshot.pins.config_sha.clone(),
        submit_body: serde_json::to_vec(&submission)?,
        prompt_body: Some(prompt_body),
        deadline: now + Duration::seconds(1800 + timeout.unwrap_or(0)),
        unbounded_running: timeout.is_none(),
    })
}
