//! Validation of complete text edits before any Git object is written.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::{editable_files, effective_limits, plan_scope_contains};

/// A whole-file replacement or deletion proposed by the implementer.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ImplementationEdit {
    /// Repository-relative path in the approved envelope.
    pub path: String,
    /// Complete UTF-8 file body, or null for deletion.
    pub content: Option<String>,
}

/// One candidate's summary and complete set of text edits.
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ImplementationReport {
    /// Human-readable change summary.
    pub summary: String,
    /// Proposed whole-file edits.
    pub edits: Vec<ImplementationEdit>,
}

/// Refuse out-of-scope, invalid or unchanged edits before a Git side effect.
pub fn validate_edits(
    raw: Value,
    plan: &Value,
    originals: &BTreeMap<String, Option<String>>,
    config: &Value,
) -> Result<ImplementationReport, String> {
    let report: ImplementationReport =
        serde_json::from_value(raw).map_err(|_| "invalid implementation response".to_owned())?;
    validated_edit_paths(&report, plan, config)?;
    for edit in &report.edits {
        if edit.content.is_none() && originals.get(&edit.path).is_none_or(Option::is_none) {
            return Err("a deletion must name an existing regular text file".to_owned());
        }
        if edit
            .content
            .as_ref()
            .is_some_and(|content| content.contains('\0'))
        {
            return Err("invalid text replacement".to_owned());
        }
        if originals
            .get(&edit.path)
            .is_some_and(|original| original == &edit.content)
        {
            return Err("unchanged replacement".to_owned());
        }
    }
    Ok(report)
}

/// Validate proposed paths before reading their contents from the pinned parent.
pub fn validated_edit_paths(
    report: &ImplementationReport,
    plan: &Value,
    config: &Value,
) -> Result<Vec<String>, String> {
    if report.summary.trim().is_empty() {
        return Err("implementation summary is empty".to_owned());
    }
    if report.edits.is_empty() {
        return Err("implementation must contain edits".to_owned());
    }
    let limits = effective_limits(config).map_err(|error| error.to_string())?;
    if let Some(maximum) = limits["changed_files"].as_u64()
        && report.edits.len() as u64 > maximum
    {
        return Err(format!(
            "candidate changes {} files, over limits.changed_files ({maximum})",
            report.edits.len()
        ));
    }
    if let Some(maximum) = plan["scope"]["max_files"].as_u64()
        && report.edits.len() as u64 > maximum
    {
        return Err(format!(
            "candidate changes {} files, over the approved budget of {maximum}",
            report.edits.len()
        ));
    }
    let mut seen = BTreeSet::new();
    for edit in &report.edits {
        let allowed = editable_files(config, std::slice::from_ref(&edit.path))
            .map_err(|error| error.to_string())?
            .len()
            == 1;
        let in_scope = plan_scope_contains(plan, &edit.path).map_err(|error| error.to_string())?;
        if !allowed || !in_scope || !seen.insert(&edit.path) {
            return Err("edit outside approved scope or duplicate path".to_owned());
        }
    }
    Ok(seen.into_iter().cloned().collect())
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use serde_json::json;

    use super::{validate_edits, validated_edit_paths};

    /// Reject protected and traversal paths before source reads or Git writes.
    #[test]
    fn candidate_paths_refuse_unapproved_files_before_contents_are_read() {
        let config = json!({
            "policy_documents":[],
            "paths":{"protected":[],"protected_extensions":[],"protected_names":[],
                "protected_name_prefixes":[],"protected_prefixes":[".concurro/"],
                "protected_files":[],"generated":[]}
        });
        let plan = json!({"files":["src/lib.rs"],"scope":{"paths":["src/"]}});
        for path in [
            "../src/lib.rs",
            "src/../lib.rs",
            ".concurro/state",
            "other.rs",
        ] {
            let report = json!({"summary":"Change","edits":[{"path":path,"content":"new"}]});
            let parsed = serde_json::from_value(report.clone()).expect("edit shape");
            assert!(validated_edit_paths(&parsed, &plan, &config).is_err());
            assert!(validate_edits(report, &plan, &BTreeMap::new(), &config).is_err());
        }
        let original = BTreeMap::from([("src/lib.rs".to_owned(), Some("old".to_owned()))]);
        let report = json!({
            "summary":"Change","edits":[{"path":"src/lib.rs","content":"new"}]
        });
        assert!(validate_edits(report, &plan, &original, &config).is_ok());
    }
}
