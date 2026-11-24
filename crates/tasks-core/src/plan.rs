//! Validation of a planner's structured output against pinned target policy.

use std::collections::BTreeSet;

use anyhow::Result;
use serde_json::Value;

use crate::policy::{edit_policy, editable_files, effective_limits, glob_matches};

/// Show the planner the enforceable limits and path rules pinned to this task.
pub fn plan_output_contract(config: &Value) -> Result<Value> {
    Ok(serde_json::json!({
        "limits": effective_limits(config)?,
        "publication_items_min": 1,
        "edit_policy": edit_policy(config)?,
        "instruction": "files names primary files, not every anticipated edit. scope.paths may use narrow directories/globs to include related tests and follow-on edits. Null limits mean no configured cap. Paths must be repository-relative, without empty, . or .. components. Neither scope nor an issue reply overrides edit_policy. If required work is forbidden, ask for a target-policy decision in questions; do not omit required work or claim that a reply grants permission. Keep publication lists concise; the full design belongs in summary, steps and tests. Genuine questions may leave files/scope incomplete and stop implementation."
    }))
}

/// Check the planner output before it can become a reviewable plan.
pub fn validate_plan(plan: &Value, config: &Value) -> Result<(), String> {
    let object = plan.as_object().ok_or("invalid plan fields")?;
    let required = [
        "summary",
        "questions",
        "files",
        "steps",
        "tests",
        "responses",
        "publication",
    ];
    if object.len() < required.len()
        || !required.iter().all(|key| object.contains_key(*key))
        || object
            .keys()
            .any(|key| !required.contains(&key.as_str()) && key != "scope" && key != "headline")
        || object
            .get("headline")
            .is_some_and(|value| !value.is_string())
    {
        return Err("invalid plan fields".to_owned());
    }
    let mut problems = Vec::new();
    let publication = &plan["publication"];
    if publication.as_object().is_none_or(|fields| {
        fields.len() != 2 || !fields.contains_key("changes") || !fields.contains_key("review_focus")
    }) {
        problems.push("plan publication needs changes and review_focus arrays".to_owned());
    } else {
        for key in ["changes", "review_focus"] {
            if publication[key].as_array().is_none_or(|items| {
                items.is_empty()
                    || items
                        .iter()
                        .any(|item| item.as_str().is_none_or(|text| text.trim().is_empty()))
            }) {
                problems.push(format!("publication.{key} must contain non-empty strings"));
            }
        }
    }
    if plan["responses"].as_array().is_none_or(|items| {
        items.iter().any(|item| {
            item.as_object().is_none_or(|fields| {
                fields.len() != 2
                    || fields
                        .get("id")
                        .and_then(Value::as_str)
                        .is_none_or(|text| text.trim().is_empty())
                    || fields
                        .get("response")
                        .and_then(Value::as_str)
                        .is_none_or(|text| text.trim().is_empty())
            })
        })
    }) {
        problems.push("invalid planner objection responses".to_owned());
    }
    let mut lists_valid = true;
    for key in ["questions", "files", "steps", "tests"] {
        if plan[key]
            .as_array()
            .is_none_or(|items| items.iter().any(|item| !item.is_string()))
        {
            problems.push(format!("plan {key} must be an array of strings"));
            lists_valid = false;
        }
    }
    if plan["summary"]
        .as_str()
        .is_none_or(|text| text.trim().is_empty())
    {
        problems.push("plan summary missing".to_owned());
    }
    if lists_valid && plan["questions"].as_array().is_some_and(Vec::is_empty) {
        let files = plan["files"].as_array().expect("checked files");
        let distinct = files
            .iter()
            .filter_map(Value::as_str)
            .collect::<BTreeSet<_>>();
        if files.is_empty() || distinct.len() != files.len() {
            problems.push(
                "plan must name distinct primary files; put related follow-on edits in scope"
                    .to_owned(),
            );
        }
        let limits = effective_limits(config).map_err(|error| error.to_string())?;
        if let Some(maximum) = limits["primary_files"].as_u64()
            && files.len() as u64 > maximum
        {
            problems.push(format!(
                "plan files exceed limits.primary_files ({maximum})"
            ));
        }
        for (index, path) in files.iter().enumerate() {
            let path = path.as_str().expect("checked file string");
            if editable_files(config, &[path.to_owned()])
                .map_err(|error| error.to_string())?
                .is_empty()
            {
                problems.push(format!("files[{index}] is protected or unsupported by the target edit_policy; a scope or issue reply cannot override it"));
                break;
            }
        }
        if let Some(error) = scope_error(plan, &limits)? {
            problems.push(error);
        }
        if plan["steps"].as_array().is_some_and(Vec::is_empty)
            || plan["tests"].as_array().is_some_and(Vec::is_empty)
        {
            problems.push("plan needs steps and validation criteria".to_owned());
        }
    }
    if problems.is_empty() {
        Ok(())
    } else {
        Err(problems.join("; "))
    }
}

/// Check the optional path envelope without granting it policy authority.
fn scope_error(plan: &Value, limits: &Value) -> Result<Option<String>, String> {
    let Some(scope) = plan.get("scope") else {
        return Ok(None);
    };
    let Some(fields) = scope.as_object() else {
        return Ok(Some("plan scope needs paths".to_owned()));
    };
    if !fields.contains_key("paths")
        || fields
            .keys()
            .any(|key| !matches!(key.as_str(), "paths" | "max_files" | "max_changed_lines"))
    {
        return Ok(Some("plan scope needs paths".to_owned()));
    }
    let Some(paths) = scope["paths"].as_array() else {
        return Ok(Some(
            "plan scope paths must contain distinct non-empty strings".to_owned(),
        ));
    };
    let distinct = paths
        .iter()
        .filter_map(Value::as_str)
        .filter(|path| !path.is_empty())
        .collect::<BTreeSet<_>>();
    if paths.is_empty() || distinct.len() != paths.len() {
        return Ok(Some(
            "plan scope paths must contain distinct non-empty strings".to_owned(),
        ));
    }
    if let Some(maximum) = limits["scope_paths"].as_u64()
        && paths.len() as u64 > maximum
    {
        return Ok(Some(format!(
            "plan scope paths exceed limits.scope_paths ({maximum})"
        )));
    }
    if let Some(maximum) = limits["scope_path_characters"].as_u64()
        && paths
            .iter()
            .any(|path| path.as_str().expect("checked path").chars().count() as u64 > maximum)
    {
        return Ok(Some(format!(
            "plan scope paths exceed limits.scope_path_characters (at most {maximum} characters each)"
        )));
    }
    let patterns = paths
        .iter()
        .map(|path| {
            let path = path.as_str().expect("checked path");
            if path.ends_with('/') {
                format!("{path}**")
            } else {
                path.to_owned()
            }
        })
        .collect::<Vec<_>>();
    if patterns.iter().any(|path| {
        path.starts_with('/')
            || path.contains('\\')
            || path
                .split('/')
                .any(|part| part.is_empty() || part == "." || part == "..")
    }) {
        return Ok(Some("plan scope paths must be relative paths, directories or globs without empty, . or .. components".to_owned()));
    }
    if let Some(max_files) = fields.get("max_files") {
        let file_count = plan["files"].as_array().expect("checked files").len() as u64;
        if max_files.as_u64().is_none_or(|limit| limit < file_count) {
            return Ok(Some(
                "plan scope max_files must cover its primary files".to_owned(),
            ));
        }
    }
    if plan["files"]
        .as_array()
        .expect("checked files")
        .iter()
        .any(|file| {
            !patterns
                .iter()
                .any(|pattern| glob_matches(file.as_str().expect("checked file"), pattern))
        })
    {
        return Ok(Some("plan files must lie inside the plan scope".to_owned()));
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{plan_output_contract, validate_plan};

    #[test]
    fn planner_output_respects_pinned_scope_and_clarification_escape() {
        let config = json!({
            "policy_documents": ["README.md"],
            "paths": {
                "protected": [], "protected_extensions": [], "protected_names": [],
                "protected_name_prefixes": [], "protected_prefixes": [".concurro/"],
                "protected_files": ["secrets.txt"], "generated": []
            },
            "limits": {"primary_files": 2, "scope_paths": 2, "scope_path_characters": 20}
        });
        let contract = plan_output_contract(&config).expect("contract");
        assert_eq!(contract["limits"]["primary_files"], 2);
        let mut plan = json!({
            "summary": "Add the change", "questions": [], "files": ["src/lib.rs"],
            "steps": ["Update code"], "tests": ["Run suite"], "responses": [],
            "publication": {"changes": ["New behavior"], "review_focus": ["Correctness"]},
            "scope": {"paths": ["src/"]}
        });
        assert_eq!(validate_plan(&plan, &config), Ok(()));
        plan["files"] = json!(["secrets.txt"]);
        assert!(
            validate_plan(&plan, &config)
                .unwrap_err()
                .contains("protected")
        );
        plan["files"] = json!(["src/lib.rs"]);
        plan["scope"] = json!({"paths": ["tests/"]});
        assert!(
            validate_plan(&plan, &config)
                .unwrap_err()
                .contains("inside the plan scope")
        );
        plan["questions"] = json!(["May the target policy change?"]);
        plan["files"] = json!([]);
        assert_eq!(validate_plan(&plan, &config), Ok(()));
    }
}
