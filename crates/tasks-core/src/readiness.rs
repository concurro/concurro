//! Current-head GitHub CI evidence for a contribution's ready gate.

use std::collections::{HashMap, HashSet};

use serde_json::Value;

/// Complete CI surfaces observed for one exact head or test-merge commit.
pub struct CiObservation {
    /// Commit SHA shared by every accepted observation item.
    pub sha: String,
    /// Check runs, including those in superseded workflow suites.
    pub checks: Vec<Value>,
    /// Commit statuses in GitHub's combined status history.
    pub statuses: Vec<Value>,
    /// Check suites at this commit.
    pub suites: Vec<Value>,
    /// Actions workflow runs at this commit.
    pub workflows: Vec<Value>,
}

/// Select the highest-ID item for each stable key without reordering first occurrences.
fn latest_by<'a>(
    items: impl IntoIterator<Item = &'a Value>,
    key: impl Fn(&Value) -> String,
) -> Vec<&'a Value> {
    let mut indexes: HashMap<String, usize> = HashMap::new();
    let mut latest: Vec<&'a Value> = Vec::new();
    for item in items {
        let name = key(item);
        if let Some(index) = indexes.get(&name).copied() {
            if item["id"].as_i64().unwrap_or(0) > latest[index]["id"].as_i64().unwrap_or(0) {
                latest[index] = item;
            }
        } else {
            indexes.insert(name, latest.len());
            latest.push(item);
        }
    }
    latest
}

/// Ignore a queued external suite only when GitHub proves that it contains no checks.
fn empty_external_suite(suite: &Value, populated: &HashSet<i64>) -> bool {
    let slug = suite["app"]["slug"].as_str();
    suite["id"]
        .as_i64()
        .is_some_and(|id| id > 0 && !populated.contains(&id))
        && slug.is_some_and(|slug| {
            !slug.trim().is_empty() && !slug.eq_ignore_ascii_case("github-actions")
        })
        && suite["latest_check_runs_count"].as_i64() == Some(0)
        && suite["status"] == "queued"
        && suite.get("conclusion") == Some(&Value::Null)
}

/// Require every current CI surface to pass and at least one real successful check or status.
pub fn ci_reasons(observations: &[CiObservation]) -> Vec<String> {
    let mut reasons = Vec::new();
    let mut success = false;
    for observation in observations {
        let sha = &observation.sha;
        let workflows = latest_by(&observation.workflows, |run| {
            format!(
                "{}:{}:{}",
                run.get("workflow_id").unwrap_or(&run["id"]),
                run["event"],
                run["head_branch"]
            )
        });
        let active_suites = workflows
            .iter()
            .filter_map(|run| run["check_suite_id"].as_i64())
            .collect::<HashSet<_>>();
        let superseded = observation
            .workflows
            .iter()
            .filter_map(|run| run["check_suite_id"].as_i64())
            .filter(|id| !active_suites.contains(id))
            .collect::<HashSet<_>>();
        let populated = observation
            .checks
            .iter()
            .filter_map(|check| check["check_suite"]["id"].as_i64())
            .collect::<HashSet<_>>();
        for suite in &observation.suites {
            if suite["id"]
                .as_i64()
                .is_some_and(|id| superseded.contains(&id))
                || (suite["head_sha"] == *sha && empty_external_suite(suite, &populated))
            {
                continue;
            }
            let id = suite["id"].to_string();
            if suite["head_sha"] != *sha || suite["status"] != "completed" {
                reasons.push(format!("CI suite pending: {id}"));
            } else if !matches!(
                suite["conclusion"].as_str(),
                Some("success" | "neutral" | "skipped")
            ) {
                reasons.push(format!("CI suite unsuccessful: {id}"));
            }
        }
        for run in workflows {
            let name = run["name"]
                .as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| run["id"].to_string());
            if run["head_sha"] != *sha || run["status"] != "completed" {
                reasons.push(format!("CI workflow pending: {name}"));
            } else if !matches!(
                run["conclusion"].as_str(),
                Some("success" | "neutral" | "skipped")
            ) {
                reasons.push(format!("CI workflow unsuccessful: {name}"));
            }
        }
        let checks = latest_by(
            observation.checks.iter().filter(|check| {
                check["check_suite"]["id"]
                    .as_i64()
                    .is_none_or(|id| !superseded.contains(&id))
            }),
            |check| {
                format!(
                    "{}:{}:{}",
                    check["app"]["id"], check["check_suite"]["id"], check["name"]
                )
            },
        );
        for check in checks {
            let name = check["name"].as_str().unwrap_or("unnamed check");
            if check["head_sha"] != *sha || check["status"] != "completed" {
                reasons.push(format!("CI pending: {name}"));
            } else if check["conclusion"] == "success" {
                success = true;
            } else if !matches!(check["conclusion"].as_str(), Some("neutral" | "skipped")) {
                reasons.push(format!("CI unsuccessful: {name}"));
            }
        }
        let statuses = latest_by(&observation.statuses, |status| {
            status["context"].to_string()
        });
        for status in statuses {
            if status["state"] == "success" {
                success = true;
            } else {
                reasons.push(format!(
                    "CI {}: {}",
                    status["state"].as_str().unwrap_or("unknown"),
                    status["context"].as_str().unwrap_or("unnamed status")
                ));
            }
        }
    }
    if !success {
        reasons.push("CI has no successful result yet".to_owned());
    }
    let mut seen = HashSet::new();
    reasons.retain(|reason| seen.insert(reason.clone()));
    reasons
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::{CiObservation, ci_reasons};

    /// A successful external status cannot hide a waiting Actions workflow.
    #[test]
    fn waiting_workflow_blocks_a_successful_external_status() {
        let result = ci_reasons(&[CiObservation {
            sha: "a".repeat(40),
            checks: vec![],
            suites: vec![],
            statuses: vec![json!({"id":1,"context":"CLA","state":"success"})],
            workflows: vec![json!({"id":10,"workflow_id":1,"head_sha":"a".repeat(40),
                "name":"CI","status":"waiting","conclusion":null})],
        }]);
        assert_eq!(result, vec!["CI workflow pending: CI"]);
    }

    /// Neutral and skipped results alone cannot establish a real successful check.
    #[test]
    fn neutral_suite_and_check_need_a_real_success() {
        let sha = "a".repeat(40);
        let result = ci_reasons(&[CiObservation {
            sha: sha.clone(),
            statuses: vec![],
            workflows: vec![],
            suites: vec![
                json!({"id":1,"head_sha":sha,"status":"completed","conclusion":"neutral"}),
            ],
            checks: vec![
                json!({"id":2,"name":"optional","head_sha":sha,"status":"completed",
                "conclusion":"skipped"}),
            ],
        }]);
        assert_eq!(result, vec!["CI has no successful result yet"]);
    }

    /// An older failed workflow suite does not defeat its current successful rerun.
    #[test]
    fn superseded_workflow_suite_does_not_block_current_success() {
        let sha = "a".repeat(40);
        let result = ci_reasons(&[CiObservation {
            sha: sha.clone(),
            statuses: vec![],
            workflows: vec![
                json!({"id":1,"workflow_id":9,"event":"pull_request","head_branch":"topic",
                    "check_suite_id":3,"head_sha":sha,"status":"completed","conclusion":"failure"}),
                json!({"id":2,"workflow_id":9,"event":"pull_request","head_branch":"topic",
                    "check_suite_id":4,"head_sha":sha,"status":"completed","conclusion":"success"}),
            ],
            suites: vec![
                json!({"id":3,"head_sha":sha,"status":"completed","conclusion":"failure"}),
                json!({"id":4,"head_sha":sha,"status":"completed","conclusion":"success"}),
            ],
            checks: vec![json!({"id":8,"name":"build","check_suite":{"id":4},
                "head_sha":sha,"status":"completed","conclusion":"success"})],
        }]);
        assert!(result.is_empty(), "{result:?}");
    }
}
