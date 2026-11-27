//! Allowlisted current target settings for the private read API.

use std::collections::BTreeMap;

use anyhow::{Context, Result, anyhow};
use concurro_github_v1::AppClient;
use concurro_tasks_core::{
    Provider, design_roster, effective_limits, path_rules, pr_review_roster,
};
use serde_json::{Value, json};

use crate::{InitializationTarget, initialization, intake_payload::assigned_agent};

/// Resolve current settings once, then read both config candidates at that commit.
pub async fn current_configuration(
    github: &AppClient,
    target: &InitializationTarget,
) -> Result<Value> {
    let (branch, sha) = github
        .default_head(&target.fork, &target.fork_permissions)
        .await?;
    let config = initialization::read_target_config(github, target, &sha).await?;
    let mut view = configuration(&config.raw)?;
    view["source"] = json!({"repository":target.fork,"branch":branch,"sha":sha});
    view["default_branch"] = json!(branch);
    Ok(view)
}

/// Normalize runtime assignments without exposing arbitrary config, MCP or credentials.
fn configuration(config: &Value) -> Result<Value> {
    let mut roster = Vec::new();
    let mut profiles = BTreeMap::new();
    let mut seat = |role: &str, name: String, provider: &str, model: String, effort: String| {
        profiles.insert(
            name.clone(),
            json!({"name":name,"provider":provider,"model":model,"effort":effort}),
        );
        roster.push(json!({"role":role,"agent":name,"profile":name}));
    };
    for role in ["triage", "planner", "implementer"] {
        let (harness, model, effort, agent) = assigned_agent(config, role)?;
        let name = agent
            .as_ref()
            .and_then(|agent| agent["profile"].as_str())
            .map(str::to_owned)
            .unwrap_or_else(|| {
                if config["models"][&harness].as_str() == Some(model.as_str()) {
                    harness.clone()
                } else {
                    format!("{harness}:triage")
                }
            });
        seat(
            role,
            name,
            Provider::from_harness(&harness)
                .context("unsupported agent harness")?
                .as_str(),
            model,
            effort,
        );
    }
    for (role, members) in [
        ("plan_reviewer", design_roster(config)),
        ("pr_reviewer", pr_review_roster(config)),
    ] {
        for member in members.map_err(|_| anyhow!("target reviewer configuration is invalid"))? {
            let harness = member
                .agent
                .as_ref()
                .and_then(|agent| agent["harness"].as_str())
                .unwrap_or(&member.provider);
            let name = member
                .agent
                .as_ref()
                .and_then(|agent| agent["profile"].as_str())
                .map(str::to_owned)
                .unwrap_or_else(|| {
                    if config["models"][&member.provider].as_str() == Some(member.model.as_str()) {
                        member.provider.clone()
                    } else {
                        format!("{}:triage", member.provider)
                    }
                });
            let effort = member
                .effort
                .unwrap_or_else(|| if harness == "claude" { "medium" } else { "low" }.to_owned());
            seat(
                role,
                name,
                Provider::from_harness(harness)
                    .context("unsupported agent harness")?
                    .as_str(),
                member.model,
                effort,
            );
        }
    }
    let mut settings = json!({
        "base":config["base"], "paths":path_rules(config)?,
        "policy_documents":config["policy_documents"],
    });
    // Only numeric runtime limits are displayable, including defaulted workload limits.
    for key in [
        "max_model_calls",
        "max_fix_rounds",
        "max_plan_revisions",
        "max_review_repairs",
        "max_context_bytes",
        "max_retained_bytes",
    ] {
        settings[key] = config[key]
            .as_u64()
            .map_or(Value::Null, |value| json!(value));
    }
    let limits = effective_limits(config)?;
    let mut safe_limits = json!({});
    for key in [
        "primary_files",
        "scope_paths",
        "scope_path_characters",
        "changed_files",
        "overlay_files",
        "workspace_files",
    ] {
        safe_limits[key] = limits[key]
            .as_u64()
            .map_or(Value::Null, |value| json!(value));
    }
    settings["limits"] = safe_limits;
    Ok(
        json!({"mode":if config["autonomous"].as_bool() == Some(true) { "autonomous" } else { "manual" },
        "roster":roster,"profiles":profiles.into_values().collect::<Vec<_>>(),"settings":settings}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Legacy overrides and the actual cross-role review panel retain distinct profiles.
    #[test]
    fn legacy_roster_and_policy_are_normalized_and_allowlisted() {
        let mut config: Value = serde_json::from_str(include_str!(
            "../../../engine/tests/fixtures/legacy-config.json"
        ))
        .unwrap();
        config["qc_model"] = json!("triage-model");
        config["autonomous"] = json!(true);
        config["mcp"] = json!({"servers":{"private":{"env":{"TOKEN":"secret-value"}}}});
        config["gateway"] = json!({"token":"secret-value"});
        config["limits"] = json!({"primary_files":12,"secret":"secret-value"});
        let view = configuration(&config).unwrap();
        assert_eq!(view["mode"], "autonomous");
        assert_eq!(view["settings"]["limits"]["primary_files"], 12);
        assert_eq!(view["settings"]["limits"]["scope_path_characters"], 200);
        assert!(
            view["profiles"]
                .as_array()
                .unwrap()
                .iter()
                .any(|profile| profile["model"] == "triage-model")
        );
        assert!(
            view["roster"]
                .as_array()
                .unwrap()
                .iter()
                .any(|seat| seat["role"] == "plan_reviewer")
        );
        assert!(!view.to_string().contains("secret-value"));
        assert!(view["settings"].get("mcp").is_none());
    }

    /// Named profiles retain provider, model and effort while private profile keys are omitted.
    #[test]
    fn named_profiles_use_runtime_assignments() {
        let config = json!({"agents":{"triage":"fast","planner":"deep","implementer":"deep",
            "plan_reviewers":["fast","deep"],"pr_reviewers":["deep"]},
            "models":{"fast":{"provider":"openai","model":"gpt-fast","effort":"low","token":"secret"},
                "deep":{"provider":"anthropic","model":"claude-deep","effort":"high"}},
            "paths":{"protected_extensions":[]},"policy_documents":["README.md"],"base":"main"});
        let view = configuration(&config).unwrap();
        assert_eq!(view["roster"].as_array().unwrap().len(), 6);
        assert_eq!(view["profiles"].as_array().unwrap().len(), 2);
        assert_eq!(view["profiles"][0]["model"], "claude-deep");
        assert_eq!(view["profiles"][0]["effort"], "high");
        assert!(!view.to_string().contains("secret"));
    }
}
