//! Intake turn submission from a pinned task snapshot and current runner identity.

use anyhow::{Context, Result, ensure};
use chrono::{DateTime, Duration, Utc};
use concurro_exchange_v1::TurnSubmission;
use concurro_tasks_core::{
    AttemptSpec, InitializationSnapshot, IssueKey, Provider, TaskState, edit_policy,
    editable_files, effective_limits,
};
use concurro_tasks_postgres::IntakeCorrection;
use glob::Pattern;
use serde::Deserialize;
use serde_json::{Map, Value, json};
use uuid::Uuid;

use crate::{
    InitializationTarget,
    bundle::{ResolvedBundle, python_digest, python_json, python_pretty_json},
};

/// Inputs shared by `prepare_intake_attempt`.
pub struct IntakeAttemptInputs<'a> {
    /// Verified runtime resources and addenda for this exact attempt.
    pub bundle: ResolvedBundle,
    /// Runner OIDC claims rechecked before admission.
    pub expected: Value,
    /// Prior invalid report and bounded repair feedback, if present.
    pub correction: Option<&'a IntakeCorrection>,
    /// Immutable turn identity shared by admission, runner and replay.
    pub turn: Uuid,
    /// Admission clock used to derive queue and execution deadlines.
    pub now: DateTime<Utc>,
}

const QUEUE_SECONDS: i64 = 1800;
const DEFAULT_TURN_SECONDS: i64 = 1200;
const DEFAULT_RETAINED_BYTES: u64 = 64 * 1024 * 1024;

/// Keep feedback identities while shortening long previews in a prompt index.
fn feedback_index(value: &Value, field: Option<&str>) -> Value {
    match value {
        Value::String(text)
            if !matches!(
                field,
                Some("id" | "status" | "path" | "head" | "reviewer" | "provider" | "source_ids")
            ) && text.chars().count() > 160 =>
        {
            Value::String(format!(
                "{} [continued in feedback file]",
                text.chars().take(160).collect::<String>()
            ))
        }
        Value::Object(fields) => Value::Object(
            fields
                .iter()
                .map(|(key, value)| (key.clone(), feedback_index(value, Some(key))))
                .collect(),
        ),
        Value::Array(items) => Value::Array(
            items
                .iter()
                .map(|value| feedback_index(value, field))
                .collect(),
        ),
        _ => value.clone(),
    }
}

/// Operator settings supplied to each pinned intake submission.
#[derive(Clone, Debug, Deserialize)]
pub struct IntakeTransport {
    /// Runner gateway URL, headers and optional Codex WebSocket URL.
    pub gateway: Value,
    /// Launcher ref expected in runner OIDC claims.
    pub workflow_ref: String,
    /// Model patterns allowed by the inference gateway.
    pub models: Vec<String>,
    /// Per-session gateway token budget.
    pub budget: i64,
    /// Session lease in seconds.
    pub lease: i64,
}

/// Resolve a pinned role into its harness, model, effort and optional named identity.
pub(crate) fn assigned_agent(
    config: &Value,
    role: &str,
) -> Result<(String, String, String, Option<Value>)> {
    if let Some(agents) = config.get("agents") {
        let name = agents[role]
            .as_str()
            .context("target agent assignment is invalid")?;
        let profile = config["models"]
            .get(name)
            .context("target agent profile is missing")?;
        let provider = profile["provider"]
            .as_str()
            .context("target agent provider is invalid")?;
        let harness = Provider::from_name(provider)
            .context("target agent provider is unsupported")?
            .harness();
        let model = profile["model"]
            .as_str()
            .context("target agent model is invalid")?;
        let effort = profile["effort"]
            .as_str()
            .context("target agent effort is invalid")?;
        ensure!(!model.trim().is_empty(), "target agent model is empty");
        Provider::from_name(provider)
            .expect("validated provider")
            .validate_model(model, effort)?;
        return Ok((
            harness.to_owned(),
            model.to_owned(),
            effort.to_owned(),
            Some(json!({
                "profile": name,
                "provider": provider,
                "model": model,
                "effort": effort,
                "harness": harness,
            })),
        ));
    }
    let legacy_role = match role {
        "planner" => "planner",
        "implementer" => "implementer",
        _ => "qc",
    };
    let harness = config[legacy_role]
        .as_str()
        .context("target qc provider is missing")?;
    ensure!(
        Provider::from_harness(harness).is_some(),
        "target qc provider is unsupported"
    );
    let override_model = (role == "triage")
        .then(|| {
            config["qc_model"]
                .as_str()
                .filter(|value| !value.is_empty())
        })
        .flatten();
    let model = override_model
        .or_else(|| config["models"][harness].as_str())
        .context("target qc model is missing")?;
    ensure!(!model.trim().is_empty(), "target qc model is empty");
    let effort = if harness == "claude" { "medium" } else { "low" };
    Provider::from_harness(harness)
        .context("unsupported provider")?
        .validate_model(model, effort)?;
    Ok((
        harness.to_owned(),
        model.to_owned(),
        effort.to_owned(),
        None,
    ))
}

/// Normalize configured MCP process settings for a runner request.
pub(crate) fn mcp_servers(config: &Value) -> Option<Value> {
    let servers = config
        .get("mcp")?
        .get("servers")?
        .as_object()
        .filter(|servers| !servers.is_empty())?;
    Some(Value::Object(
        servers
            .iter()
            .map(|(name, server)| {
                (
                    name.clone(),
                    json!({
                        "command": server["command"],
                        "args": server.get("args").cloned().unwrap_or_else(|| json!([])),
                        "env": server.get("env").cloned().unwrap_or_else(|| json!({})),
                    }),
                )
            })
            .collect(),
    ))
}

/// Freeze one org-funded triage submission before its transactional call charge.
pub fn prepare_intake_attempt(
    issue: IssueKey,
    task: &TaskState,
    snapshot: &InitializationSnapshot,
    target: &InitializationTarget,
    transport: &IntakeTransport,
    inputs: IntakeAttemptInputs<'_>,
) -> Result<AttemptSpec> {
    let IntakeAttemptInputs {
        bundle,
        expected,
        correction,
        turn,
        now,
    } = inputs;
    let config = &snapshot.configuration;
    let (harness, model, effort, agent) = assigned_agent(config, "triage")?;
    ensure!(
        transport
            .models
            .iter()
            .any(|allowed| { Pattern::new(allowed).is_ok_and(|pattern| pattern.matches(&model)) }),
        "triage model is outside gateway model routes"
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
        .map(|comment| json!({"author": comment.author.login, "body": comment.body}))
        .collect::<Vec<_>>();
    let limits = effective_limits(config)?;
    let refs = Value::Object(Map::from_iter([(
        base.to_owned(),
        Value::String(snapshot.pins.base_sha.clone()),
    )]));
    let repository = json!({"refs": refs, "head": base});
    let mut context = json!({
        "issue": {
            "title": snapshot.requirements.title,
            "body": snapshot.requirements.body,
            "discussion": discussion,
        },
        "policy": policy,
        "edit_policy": edit_policy(config)?,
        "limits": limits,
        "plan": null,
        "base": snapshot.pins.base_sha,
        "bundle_id": snapshot.bundle_id,
        "charter": serde_json::from_str::<Value>(&snapshot.charter)?,
        "prior_outcomes_untrusted": [],
        "editable_files": editable_files(config, &snapshot.base_files)?,
        "repository_commit": snapshot.pins.base_sha,
        "repository": repository,
    });
    if let Some(agent) = &agent {
        context["agent"] = agent.clone();
    }
    if let Some(correction) = correction {
        context["intake_validation_feedback"] = json!({
            "output": python_pretty_json(&correction.report)?,
            "truncated": false,
            "reason": correction.reason,
            "attempt": correction.attempt,
            "instruction": bundle.instruction("intake_correction")?,
        });
    }
    let mut feedback = json!({});
    if let Some(selected) = context.get("intake_validation_feedback").cloned() {
        let wrapped = json!({"intake_validation_feedback": selected});
        if python_json(&wrapped)?.len() > 8000 && python_json(&selected)?.len() >= 512 {
            let digest = python_digest(&selected)?;
            context["intake_validation_feedback"] = json!({
                "feedback_file": format!("{digest}.json"),
                "digest": digest,
                "index": feedback_index(&selected, None),
                "notice": bundle.instruction("feedback_index")?,
            });
            feedback[digest] = selected;
            context["controller_feedback"] = feedback.clone();
        }
    }
    let (mut prompt_document, response_schema) =
        bundle.prompt_parts(&context, snapshot, "intake", false)?;
    let prompt_bytes =
        serde_json::to_vec(&prompt_document)?.len() + serde_json::to_vec(&response_schema)?.len();
    prompt_document["response_schema"] = response_schema;
    let prompt_body = serde_json::to_vec(&prompt_document)?;
    let max_context_bytes = config["max_context_bytes"]
        .as_u64()
        .context("target max_context_bytes is missing or invalid")?;
    ensure!(
        prompt_bytes as u64 <= max_context_bytes,
        "intake prompt (context, instructions and schema) exceeds the size limit ({prompt_bytes} of {max_context_bytes} bytes); reduce the supplied context or raise max_context_bytes"
    );
    context
        .as_object_mut()
        .expect("intake context is an object")
        .remove("controller_feedback");
    let mut request = json!({
        "provider": harness,
        "model": model,
        "effort": effort,
        "stage": "intake",
        "gateway": transport.gateway,
        "bundle_id": snapshot.bundle_id,
        "charter": snapshot.charter,
        "instruction_texts": snapshot.instruction_texts,
        "engine_resources": bundle.resources(),
        "max_retained_bytes": config.get("max_retained_bytes")
            .cloned().unwrap_or_else(|| json!(DEFAULT_RETAINED_BYTES)),
        "context": context,
        "session": null,
        "local_candidate": false,
        "evaluation": null,
    });
    if let Some(agent) = &agent {
        request["agent"] = agent.clone();
    }
    if let Some(servers) = mcp_servers(config) {
        request["mcp_servers"] = servers;
    }
    let timeout = match config.get("turn_timeout_seconds") {
        Some(Value::Null) => None,
        Some(value) => Some(value.as_i64().context("target turn timeout is invalid")?),
        None => Some(DEFAULT_TURN_SECONDS),
    };
    ensure!(
        timeout.is_none_or(|value| value > 0),
        "target turn timeout is invalid"
    );
    let payload = json!({
        "request": request,
        "feedback": feedback,
        "spec": {
            "overlay_files_limit": limits["overlay_files"],
            "workspace_files_limit": limits["workspace_files"],
            "refs": refs,
            "head": base,
            "base_branch": base,
            "candidate": null,
        },
        "retained": null,
        "timeout": timeout,
    });
    let phase = if let Some(agent) = &agent {
        python_digest(&json!(["single", agent]))?
    } else {
        "single".to_owned()
    };
    let binding = json!({
        "repository": target.issue_repository,
        "issue": issue.number,
        "generation": 1,
        "upstream": target.upstream,
        "fork": target.fork,
        "config_sha": snapshot.pins.config_sha,
        "engine": target.engine_sha,
        "base": snapshot.pins.base_sha,
        "role": format!("intake:{harness}"),
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
        deadline: now + Duration::seconds(QUEUE_SECONDS + timeout.unwrap_or(0)),
        unbounded_running: timeout.is_none(),
    })
}

#[cfg(test)]
mod tests {
    use std::{
        collections::BTreeMap,
        io::Write,
        process::{Command, Stdio},
    };

    use chrono::Utc;
    use concurro_contracts::TaskId;
    use concurro_tasks_core::{
        Actor, InitializationSnapshot, IssueKey, PinnedInputs, Requirements, Stage, TaskState,
    };
    use concurro_tasks_postgres::IntakeCorrection;
    use serde_json::{Value, json};
    use uuid::Uuid;

    use crate::{InitializationTarget, bundle::ResolvedBundle, bundle_identity};

    use super::{IntakeAttemptInputs, IntakeTransport, prepare_intake_attempt};

    #[test]
    fn large_correction_matches_python_feedback_file_and_prompt() {
        let charter = "{\"version\":1,\"id\":\"test\",\"goals\":{\"g\":\"v\"},\"non_goals\":[\"n\"],\"constraints\":[\"c\"],\"acceptance\":[\"a\"]}";
        let configuration: Value = serde_json::from_str(include_str!(
            "../../../engine/tests/fixtures/legacy-config.json"
        ))
        .expect("legacy configuration");
        let snapshot = InitializationSnapshot {
            pins: PinnedInputs {
                config_sha: "a".repeat(40),
                base_sha: "b".repeat(40),
                max_model_calls: 48,
                max_fix_rounds: 2,
                max_plan_revisions: 2,
            },
            configuration,
            requirements: Requirements {
                title: "Review café 🌍".to_owned(),
                body: Some("Issue body".to_owned()),
                discussion: vec![],
            },
            charter: charter.to_owned(),
            instruction_texts: BTreeMap::new(),
            policy_documents: BTreeMap::new(),
            base_files: vec!["src/lib.rs".to_owned()],
            engine_resources: BTreeMap::new(),
            bundle_id: bundle_identity(charter, &BTreeMap::new()).expect("bundle"),
        };
        let issue = IssueKey {
            repository_id: 8,
            number: 3,
        };
        let task = TaskState {
            id: TaskId(Uuid::now_v7()),
            issue,
            author: Actor {
                id: 1,
                login: "author".to_owned(),
            },
            stage: Stage::Intake,
            revision: 2,
            stage_reason: None,
            retry_stage: None,
            pins: Some(snapshot.pins.clone()),
            used_model_calls: 1,
            current_attempt: None,
        };
        let target = InitializationTarget {
            issue_repository: "cylewitruk/stacks-core-autobot".to_owned(),
            fork: "cylewitruk/stacks-core-autobot".to_owned(),
            upstream: "cylewitruk/stacks-core".to_owned(),
            issue_permissions: BTreeMap::new(),
            fork_permissions: BTreeMap::new(),
            upstream_permissions: BTreeMap::new(),
            bot_login: "concurro[bot]".to_owned(),
            publisher_logins: vec![],
            engine_sha: "engine".to_owned(),
        };
        let transport = IntakeTransport {
            gateway: json!({"url": "https://gateway.example"}),
            workflow_ref: "workflow-ref".to_owned(),
            models: vec!["gpt-*".to_owned()],
            budget: 1000,
            lease: 21600,
        };
        let correction = IntakeCorrection {
            report: json!({"note": "é".repeat(9000)}),
            reason: "report invalid".to_owned(),
            attempt: 1,
        };
        let prepared = prepare_intake_attempt(
            issue,
            &task,
            &snapshot,
            &target,
            &transport,
            IntakeAttemptInputs {
                bundle: ResolvedBundle::resolve(&snapshot).expect("resolved bundle"),
                expected: json!({"repository_id": "8"}),
                correction: Some(&correction),
                turn: Uuid::now_v7(),
                now: Utc::now(),
            },
        )
        .expect("corrected intake attempt");
        let submission: Value = serde_json::from_slice(&prepared.submit_body).expect("submission");
        assert!(
            !submission["payload"]["feedback"]
                .as_object()
                .expect("feedback files")
                .is_empty()
        );
        let mut prompt_context = submission["payload"]["request"]["context"].clone();
        prompt_context["controller_feedback"] = submission["payload"]["feedback"].clone();
        let (prompt, schema) = ResolvedBundle::resolve(&snapshot)
            .expect("bundle")
            .prompt_parts(&prompt_context, &snapshot, "intake", false)
            .expect("prompt");
        let prompt_bytes = serde_json::to_vec(&prompt).expect("prompt bytes").len()
            + serde_json::to_vec(&schema).expect("schema bytes").len();
        let mut oracle = Command::new("python3")
            .arg(concat!(
                env!("CARGO_MANIFEST_DIR"),
                "/tests/fixtures/intake_oracle.py"
            ))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("start Python oracle");
        oracle
            .stdin
            .take()
            .expect("oracle stdin")
            .write_all(
                &serde_json::to_vec(&json!({
                    "snapshot": snapshot,
                    "submission": submission,
                    "correction": {
                        "report": correction.report,
                        "reason": correction.reason,
                        "attempt": correction.attempt,
                    },
                }))
                .expect("oracle input"),
            )
            .expect("send oracle input");
        let compared = oracle.wait_with_output().expect("Python comparison");
        assert!(
            compared.status.success(),
            "{}",
            String::from_utf8_lossy(&compared.stderr)
        );
        let oracle: Value = serde_json::from_slice(&compared.stdout).expect("oracle output");
        assert_eq!(
            prompt_bytes as u64,
            oracle["size"].as_u64().expect("prompt size")
        );
        let prompt: Value =
            serde_json::from_slice(prepared.prompt_body.as_deref().expect("diagnostic prompt"))
                .expect("Rust diagnostic prompt");
        assert_eq!(prompt, oracle["prompt"]);
        assert_eq!(prepared.staffing_group, oracle["staffing_group"]);
        let mut at_bound = snapshot.clone();
        at_bound.configuration["max_context_bytes"] = json!(prompt_bytes);
        assert!(
            prepare_intake_attempt(
                issue,
                &task,
                &at_bound,
                &target,
                &transport,
                IntakeAttemptInputs {
                    bundle: ResolvedBundle::resolve(&snapshot).expect("resolved bundle"),
                    expected: json!({"repository_id": "8"}),
                    correction: Some(&correction),
                    turn: Uuid::now_v7(),
                    now: Utc::now()
                },
            )
            .is_ok()
        );
        at_bound.configuration["max_context_bytes"] = json!(prompt_bytes - 1);
        assert!(
            prepare_intake_attempt(
                issue,
                &task,
                &at_bound,
                &target,
                &transport,
                IntakeAttemptInputs {
                    bundle: ResolvedBundle::resolve(&snapshot).expect("resolved bundle"),
                    expected: json!({"repository_id": "8"}),
                    correction: Some(&correction),
                    turn: Uuid::now_v7(),
                    now: Utc::now()
                },
            )
            .is_err()
        );
    }
}
