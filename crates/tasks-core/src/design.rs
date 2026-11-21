//! Stable reviewer identities and criteria for pinned design reviews.

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{InitializationSnapshot, Requirements, python_digest};

/// Split the substantive issue request into stable independent review criteria.
pub fn design_criteria(requirements: &Requirements) -> Vec<Value> {
    let mut values = vec![requirements.title.clone()];
    if let Some(body) = &requirements.body {
        values.extend(requirement_parts(body, false));
    }
    for comment in &requirements.discussion {
        values.extend(requirement_parts(&comment.body, true));
    }
    let mut seen = BTreeSet::new();
    values
        .into_iter()
        .filter(|value| seen.insert(value.clone()))
        .enumerate()
        .map(|(index, text)| json!({"id":format!("R{}",index+1),"text":text}))
        .collect()
}

/// Retain text units while omitting structural labels and courtesy-only replies.
fn requirement_parts(text: &str, acknowledgements: bool) -> Vec<String> {
    if acknowledgements
        && matches!(
            text.trim()
                .trim_end_matches(['.', '!', ' '])
                .to_ascii_lowercase()
                .as_str(),
            "thanks" | "thank you" | "thanks a lot" | "thank you very much" | "cheers"
        )
    {
        return Vec::new();
    }
    let lines = text.lines().collect::<Vec<_>>();
    let mut retained = Vec::new();
    let mut previous_structural = false;
    for (index, line) in lines.iter().enumerate() {
        let value = line.trim();
        if previous_structural
            && value.len() >= 3
            && (value.bytes().all(|byte| byte == b'-') || value.bytes().all(|byte| byte == b'='))
        {
            previous_structural = false;
            continue;
        }
        previous_structural = false;
        let heading = value
            .trim_start_matches('#')
            .trim()
            .trim_end_matches('#')
            .trim()
            .trim_matches('*')
            .trim_end_matches(':')
            .to_ascii_lowercase();
        let structural = matches!(
            heading.as_str(),
            "acceptance"
                | "acceptance criteria"
                | "requirements"
                | "context"
                | "background"
                | "summary"
                | "scope"
                | "validation"
                | "tests"
                | "expected behavior"
                | "implementation"
        );
        if structural
            && (value.ends_with(':')
                || value.starts_with('#')
                || lines.get(index + 1).is_some_and(|next| {
                    let underline = next.trim();
                    underline.len() >= 3
                        && (underline.bytes().all(|byte| byte == b'-')
                            || underline.bytes().all(|byte| byte == b'='))
                }))
        {
            previous_structural = true;
            continue;
        }
        retained.push(*line);
    }
    let mut parts = Vec::new();
    let mut current = String::new();
    for line in retained {
        let trimmed = line.trim();
        let bullet = trimmed.starts_with("- ")
            || trimmed.starts_with("* ")
            || trimmed.starts_with("+ ")
            || trimmed
                .split_once(['.', ')'])
                .is_some_and(|(prefix, rest)| {
                    !prefix.is_empty()
                        && prefix.bytes().all(|byte| byte.is_ascii_digit())
                        && rest.starts_with(' ')
                });
        if (trimmed.is_empty() || bullet) && !current.trim().is_empty() {
            parts.push(current.trim().to_owned());
            current.clear();
        }
        if !trimmed.is_empty() {
            if !current.is_empty() {
                current.push('\n');
            }
            current.push_str(line);
        }
    }
    if !current.trim().is_empty() {
        parts.push(current.trim().to_owned());
    }
    parts
}

/// Represent pinned requirements exactly as the retained controller hashes them.
pub fn legacy_requirements(requirements: &Requirements) -> Value {
    let discussion = requirements
        .discussion
        .iter()
        .map(|comment| json!({"author":comment.author.login,"body":comment.body}))
        .collect::<Vec<_>>();
    json!({
        "title": requirements.title,
        "body": requirements.body,
        "discussion": discussion,
    })
}

/// Bind a planner result to its pinned base and requirements.
pub fn plan_identity(plan: &Value, snapshot: &InitializationSnapshot) -> Result<String, String> {
    python_digest(&json!({
        "plan": plan,
        "base": snapshot.pins.base_sha,
        "requirements": legacy_requirements(&snapshot.requirements),
    }))
    .map_err(|error| error.to_string())
}

/// Bind one design review phase to all inputs its votes can authorize.
pub fn design_binding(
    plan: &Value,
    plan_id: &str,
    snapshot: &InitializationSnapshot,
    roster: &[DesignReviewer],
    generation: u64,
    phase: &str,
    parent: Option<&str>,
    changed_files: &[String],
) -> Result<String, String> {
    let config_digest = configuration_digest(&snapshot.configuration)?;
    let charter: Value =
        serde_json::from_str(&snapshot.charter).map_err(|error| error.to_string())?;
    let charter_id = python_digest(&charter).map_err(|error| error.to_string())?;
    python_digest(&json!({
        "plan":plan_id,
        "plan_content":plan,
        "base":snapshot.pins.base_sha,
        "requirements":legacy_requirements(&snapshot.requirements),
        "config":config_digest,
        "bundle":snapshot.bundle_id,
        "charter":charter_id,
        "generation":generation,
        "phase":phase,
        "roster":roster,
        "parent":parent,
        "changed_files":changed_files,
    }))
    .map_err(|error| error.to_string())
}

/// Hash task configuration while excluding service pins and account descriptors.
pub(crate) fn configuration_digest(configuration: &Value) -> Result<String, String> {
    let mut config = configuration
        .as_object()
        .ok_or("target configuration is invalid")?
        .clone();
    config.remove("engine");
    config.remove("actors");
    python_digest(&Value::Object(config)).map_err(|error| error.to_string())
}

/// One independent reviewer identity for a pinned review role.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct DesignReviewer {
    /// Python-compatible identity digest.
    pub id: String,
    /// Named profile or legacy provider used for this seat.
    pub provider: String,
    /// Exact selected model.
    pub model: String,
    /// Named profile effort, absent for a legacy reviewer.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub effort: Option<String>,
    /// Named agent identity, absent for a legacy reviewer.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub agent: Option<Value>,
    /// Legacy roles represented by this deduplicated seat.
    pub roles: Vec<String>,
}

/// Resolve named reviewers or the legacy deduplicated cross-role roster.
pub fn design_roster(config: &Value) -> Result<Vec<DesignReviewer>, String> {
    if config.get("agents").is_some() {
        return named_review_roster(config, "plan_reviewers", "plan_qc");
    }
    let planner = config["planner"]
        .as_str()
        .ok_or("target planner is invalid")?;
    let implementer = config["implementer"]
        .as_str()
        .ok_or("target implementer is invalid")?;
    let qc = config["qc"].as_str().ok_or("target qc is invalid")?;
    let mut members = vec![(planner, "plan"), (implementer, "develop"), (qc, "intake")];
    let reviewers = config["reviewers"]
        .as_array()
        .ok_or("target reviewers are invalid")?;
    for reviewer in reviewers {
        members.push((
            reviewer.as_str().ok_or("target reviewer is invalid")?,
            "review",
        ));
    }
    let mut roster: Vec<DesignReviewer> = Vec::new();
    for (provider, role) in members {
        let override_model = (role == "intake")
            .then(|| {
                config["qc_model"]
                    .as_str()
                    .filter(|model| !model.is_empty())
            })
            .flatten();
        let model = override_model
            .or_else(|| config["models"][provider].as_str())
            .ok_or("target reviewer model is invalid")?;
        let id = python_digest(&json!([provider, model])).map_err(|error| error.to_string())?;
        if let Some(existing) = roster.iter_mut().find(|member| member.id == id) {
            if !existing.roles.iter().any(|existing| existing == role) {
                existing.roles.push(role.to_owned());
            }
        } else {
            roster.push(DesignReviewer {
                id,
                provider: provider.to_owned(),
                model: model.to_owned(),
                effort: None,
                agent: None,
                roles: vec![role.to_owned()],
            });
        }
    }
    Ok(roster)
}

/// Select only the configured PR reviewers, preserving their pinned identities.
pub fn pr_review_roster(config: &Value) -> Result<Vec<DesignReviewer>, String> {
    if config.get("agents").is_some() {
        return named_review_roster(config, "pr_reviewers", "review");
    }
    let roster = design_roster(config)?
        .into_iter()
        .filter(|member| member.roles.iter().any(|role| role == "review"))
        .collect::<Vec<_>>();
    if roster.is_empty() {
        return Err("target PR reviewer roster is empty".to_owned());
    }
    Ok(roster)
}

/// Resolve a named reviewer role with the same identity used by design review.
fn named_review_roster(
    config: &Value,
    assignment: &str,
    role: &str,
) -> Result<Vec<DesignReviewer>, String> {
    let agents = &config["agents"];
    let names = agents[assignment]
        .as_array()
        .ok_or("target reviewer roster is invalid")?;
    if names.is_empty() {
        return Err("target reviewer roster is empty".to_owned());
    }
    names
        .iter()
        .map(|name| {
            let name = name.as_str().ok_or("reviewer name is invalid")?;
            let profile = &config["models"][name];
            let provider = profile["provider"]
                .as_str()
                .ok_or("reviewer provider is invalid")?;
            let harness = match provider {
                "openai" => "codex",
                "anthropic" => "claude",
                _ => return Err("reviewer provider is unsupported".to_owned()),
            };
            let model = profile["model"]
                .as_str()
                .ok_or("reviewer model is invalid")?;
            let effort = profile["effort"]
                .as_str()
                .ok_or("reviewer effort is invalid")?;
            let agent = json!({
                "profile":name,"provider":provider,"model":model,
                "effort":effort,"harness":harness,
            });
            Ok(DesignReviewer {
                id: python_digest(&agent).map_err(|error| error.to_string())?,
                provider: name.to_owned(),
                model: model.to_owned(),
                effort: Some(effort.to_owned()),
                agent: Some(agent),
                roles: vec![role.to_owned()],
            })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use crate::{Actor, RequirementComment, Requirements};

    use super::{design_criteria, design_roster, pr_review_roster};

    #[test]
    fn criteria_preserve_short_answers_and_drop_structural_labels() {
        let author = Actor {
            id: 1,
            login: "writer".to_owned(),
        };
        let requirements = Requirements {
            title: "Guide".to_owned(),
            body: Some(
                "# Acceptance criteria\n\n- Preserve this behavior.\n\nValidation:\n\n2. Check it."
                    .to_owned(),
            ),
            discussion: [
                "Thanks!",
                "Yes.",
                "No.",
                "Thanks, but keep merging disabled.",
                "# Never enable merging",
                "Yes.",
            ]
            .into_iter()
            .map(|body| RequirementComment {
                author: author.clone(),
                body: body.to_owned(),
            })
            .collect(),
        };
        let criteria = design_criteria(&requirements);
        let texts = criteria
            .iter()
            .map(|item| item["text"].as_str().expect("criterion text"))
            .collect::<Vec<_>>();
        assert_eq!(texts.len(), 7);
        assert!(texts.contains(&"Yes."));
        assert!(texts.contains(&"No."));
        assert!(!texts.contains(&"Validation:"));
    }

    #[test]
    fn legacy_roster_deduplicates_roles_but_keeps_qc_model_separate() {
        let config = json!({
            "planner":"codex","implementer":"codex","qc":"codex",
            "reviewers":["claude","codex"],"qc_model":"qc-model",
            "models":{"codex":"main-model","claude":"other-model"}
        });
        let roster = design_roster(&config).expect("roster");
        assert_eq!(roster.len(), 3);
        assert_eq!(roster[0].roles, ["plan", "develop", "review"]);
        assert_eq!(roster[1].roles, ["intake"]);
        assert_eq!(roster[2].roles, ["review"]);
        let pr = pr_review_roster(&config).expect("PR roster");
        assert_eq!(pr.len(), 2);
        assert_eq!(pr[0].provider, "codex");
        assert_eq!(pr[1].provider, "claude");
    }

    #[test]
    fn named_pr_roster_uses_its_own_assignment_and_stable_identity() {
        let config = json!({
            "agents":{"plan_reviewers":["sol"],"pr_reviewers":["opus"]},
            "models":{
                "sol":{"provider":"openai","model":"gpt-6.1-sol","effort":"high"},
                "opus":{"provider":"anthropic","model":"opus","effort":"high"}
            }
        });
        let design = design_roster(&config).expect("design roster");
        let pr = pr_review_roster(&config).expect("PR roster");
        assert_eq!(design[0].provider, "sol");
        assert_eq!(pr[0].provider, "opus");
        assert_eq!(pr[0].roles, ["review"]);
        assert_ne!(pr[0].id, design[0].id);
    }
}
