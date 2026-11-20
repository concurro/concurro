//! Pinned target path policy used when exposing editable base files to a runner.

use anyhow::{Context, Result};
use glob::Pattern;
use serde_json::{Map, Value, json};

const PROTECTED_NAMES: [&str; 4] = ["AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "SECURITY.md"];

/// Match a path by segments, with a double star covering zero or more segments.
pub(crate) fn glob_matches(path: &str, pattern: &str) -> bool {
    let parts = path.split('/').collect::<Vec<_>>();
    let patterns = pattern.split('/').collect::<Vec<_>>();
    let mut matches = vec![false; parts.len() + 1];
    matches[0] = true;
    for pattern in patterns {
        if pattern == "**" {
            for index in 1..matches.len() {
                matches[index] |= matches[index - 1];
            }
        } else {
            let segment = Pattern::new(pattern).ok();
            for index in (1..matches.len()).rev() {
                matches[index] = matches[index - 1]
                    && segment
                        .as_ref()
                        .map_or(parts[index - 1] == pattern, |value| {
                            value.matches(parts[index - 1])
                        });
            }
            matches[0] = false;
        }
    }
    matches[parts.len()]
}

/// Return the effective path rules from the pinned target configuration.
pub(crate) fn path_rules(config: &Value) -> Result<Value> {
    let paths = config
        .get("paths")
        .and_then(Value::as_object)
        .context("target paths policy is missing")?;
    if paths.contains_key("editable_extensions") {
        return Ok(Value::Object(paths.clone()));
    }
    let mut rules: Map<String, Value> = json!({
        "protected_extensions": [],
        "protected_names": [],
        "protected_name_prefixes": [],
        "protected_prefixes": [".concurro/"],
        "protected_files": [],
        "protected": [],
        "generated": [],
    })
    .as_object()
    .expect("default path rules")
    .clone();
    rules.extend(paths.clone());
    Ok(Value::Object(rules))
}

/// Build the runner-visible edit policy from the same pinned target rules.
pub(crate) fn edit_policy(config: &Value) -> Result<Value> {
    let paths = path_rules(config)?;
    let names = if paths.get("editable_extensions").is_some() {
        PROTECTED_NAMES.to_vec()
    } else {
        Vec::new()
    };
    Ok(json!({
        "paths": paths,
        "policy_documents": config.get("policy_documents").context("target policy documents missing")?,
        "protected_instruction_names": names,
        "reserved_components": [".git", ".concurro"],
    }))
}

/// Select regular base paths that the target allows an intake agent to edit.
pub(crate) fn editable_files(config: &Value, base_files: &[String]) -> Result<Vec<String>> {
    let rules = path_rules(config)?;
    let list = |key: &str| -> Result<Vec<&str>> {
        rules[key]
            .as_array()
            .context(format!("target paths.{key} is invalid"))?
            .iter()
            .map(|value| {
                value
                    .as_str()
                    .context(format!("target paths.{key} is invalid"))
            })
            .collect()
    };
    let protected = list("protected")?;
    let names = list("protected_names")?;
    let name_prefixes = list("protected_name_prefixes")?;
    let prefixes = list("protected_prefixes")?;
    let files = list("protected_files")?;
    let extensions = if rules.get("editable_extensions").is_some() {
        Some(list("editable_extensions")?)
    } else {
        None
    };
    let protected_extensions = if extensions.is_none() {
        list("protected_extensions")?
    } else {
        Vec::new()
    };
    let policy_documents = config["policy_documents"]
        .as_array()
        .context("target policy documents are invalid")?
        .iter()
        .map(|value| value.as_str().context("target policy document is invalid"))
        .collect::<Result<Vec<_>>>()?;
    Ok(base_files
        .iter()
        .filter(|path| {
            let parts = path.split('/').collect::<Vec<_>>();
            !path.is_empty()
                && !path.starts_with('/')
                && !path.contains('\\')
                && path
                    .chars()
                    .all(|value| value.is_alphanumeric() || "/_.-".contains(value))
                && parts.iter().all(|part| {
                    !part.is_empty()
                        && *part != "."
                        && *part != ".."
                        && !part.eq_ignore_ascii_case(".git")
                        && !part.eq_ignore_ascii_case(".concurro")
                })
                && !protected.iter().any(|pattern| glob_matches(path, pattern))
                && if let Some(extensions) = &extensions {
                    extensions.iter().any(|extension| path.ends_with(extension))
                        && !parts
                            .iter()
                            .any(|part| part.starts_with('.') || PROTECTED_NAMES.contains(part))
                        && !policy_documents.contains(&path.as_str())
                } else {
                    !protected_extensions
                        .iter()
                        .any(|extension| path.ends_with(extension))
                }
                && !parts.iter().any(|part| {
                    names.contains(part)
                        || name_prefixes.iter().any(|prefix| part.starts_with(prefix))
                })
                && !files.contains(&path.as_str())
                && !prefixes.iter().any(|prefix| path.starts_with(prefix))
        })
        .cloned()
        .collect())
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{editable_files, glob_matches};

    #[test]
    fn double_star_and_legacy_edit_policy_match_pinned_paths() {
        assert!(glob_matches("rust-toolchain.toml", "**/rust-toolchain*"));
        assert!(glob_matches(
            "a/b/rust-toolchain.toml",
            "**/rust-toolchain*"
        ));
        assert!(!glob_matches("a/b/Cargo.toml", "**/rust-toolchain*"));
        let config = json!({"policy_documents": ["README.md"], "paths": {
            "editable_extensions": [".rs", ".md"],
            "protected_names": [], "protected_name_prefixes": [],
            "protected_prefixes": ["tools/"], "protected_files": [],
            "protected": ["**/rust-toolchain*"], "generated": [],
        }});
        assert_eq!(
            editable_files(
                &config,
                &[
                    "src/lib.rs".to_owned(),
                    "README.md".to_owned(),
                    "AGENTS.md".to_owned(),
                    "tools/migrate.rs".to_owned(),
                    "rust-toolchain.toml".to_owned(),
                ]
            )
            .expect("edit policy"),
            vec!["src/lib.rs"]
        );
    }
}
