//! Structured independent design-review validation before a vote is recorded.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::{IntakeScope, Requirements, design_criteria};

/// One repository source cited by an assessment.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DesignSource {
    /// Reviewer-local citation ID.
    pub id: String,
    /// Repository-relative path at the pinned base.
    pub path: String,
    /// One-based first source line.
    pub line: usize,
    /// Literal source fragment.
    pub quote: String,
}

/// Evidence and status for one substantive requirement.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DesignAssessment {
    /// Exact criterion ID from the private prompt.
    pub criterion: String,
    /// Satisfied, defect, or uncertain.
    pub status: String,
    /// IDs declared in the same report's source list.
    pub sources: Vec<String>,
    /// Explanation of the assessment.
    pub reason: String,
    /// Concrete contrary example or reason none applies.
    pub counterexample: String,
}

/// One newly raised or previously owned objection disposition.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DesignObjectionUpdate {
    /// Existing owned objection ID, or empty for a new one.
    pub id: String,
    /// Open or resolved.
    pub status: String,
    /// Exact concern text while open.
    pub detail: String,
    /// Concrete failure scenario.
    pub counterexample: String,
    /// Evidence of resolution or remaining work.
    pub closure: String,
    /// Repository or requirement evidence.
    pub evidence: String,
}

/// A complete reviewer judgment with criterion and objection evidence.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DesignReviewReport {
    /// Approve, revise, clarify, reject, or escalate.
    pub decision: String,
    /// Optional presentation heading.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub headline: Option<String>,
    /// Explanation of the judgment.
    pub rationale: String,
    /// Low, medium, or high consequence risk.
    pub risk: String,
    /// Questions requiring a human response.
    pub questions: Vec<String>,
    /// Exact details of all open objections.
    pub concerns: Vec<String>,
    /// Judgment against pinned charter goals.
    pub scope: IntakeScope,
    /// Repository citations used by assessments.
    pub sources: Vec<DesignSource>,
    /// One entry per supplied criterion.
    pub assessments: Vec<DesignAssessment>,
    /// Dispositions for the reviewer's own objections.
    pub objection_updates: Vec<DesignObjectionUpdate>,
}

/// Validate a private design report against pinned requirements and source bytes.
pub fn validate_design_review(
    mut raw: Value,
    requirements: &Requirements,
    charter: &str,
    files: &BTreeMap<String, String>,
    own_objections: &[String],
) -> Result<DesignReviewReport, String> {
    raw.as_object_mut()
        .ok_or("invalid QC fields")?
        .remove("classification");
    let mut report: DesignReviewReport =
        serde_json::from_value(raw).map_err(|_| "invalid QC fields".to_owned())?;
    if !matches!(
        report.decision.as_str(),
        "approve" | "revise" | "clarify" | "reject" | "escalate"
    ) || !matches!(report.risk.as_str(), "low" | "medium" | "high")
    {
        return Err("invalid QC decision or risk".to_owned());
    }
    if report.rationale.trim().is_empty() {
        return Err("QC rationale missing".to_owned());
    }
    if report
        .questions
        .iter()
        .chain(&report.concerns)
        .any(|value| value.trim().is_empty())
    {
        return Err("invalid QC questions or concerns".to_owned());
    }
    report.scope.validate(charter)?;
    if report.decision == "approve"
        && (report.scope.goal_ids.is_empty() || !report.scope.conflicts.is_empty())
    {
        return Err("acceptance contradicts charter scope".to_owned());
    }
    if report.decision == "approve" && (!report.questions.is_empty() || !report.concerns.is_empty())
    {
        return Err("QC acceptance contradicts outstanding questions or concerns".to_owned());
    }
    if report.decision == "clarify" && report.questions.is_empty() {
        return Err("QC clarification requires questions".to_owned());
    }
    if report.decision == "revise" && (report.concerns.is_empty() || !report.questions.is_empty()) {
        return Err("QC revision requires concerns and no unanswered questions".to_owned());
    }
    if report.sources.is_empty() {
        return Err("design review needs repository evidence".to_owned());
    }
    let mut source_ids = BTreeSet::new();
    for source in &mut report.sources {
        if source.id.is_empty()
            || !source_ids.insert(source.id.clone())
            || source.line == 0
            || source.quote.is_empty()
        {
            return Err("invalid design evidence source".to_owned());
        }
        if source.path.is_empty()
            || source.path.starts_with('/')
            || source.path.contains('\\')
            || source
                .path
                .split('/')
                .any(|part| part.is_empty() || matches!(part, "." | ".." | ".git"))
        {
            return Err("review_evidence: sources.path".to_owned());
        }
        let content = files
            .get(&source.path)
            .ok_or_else(|| "review_source: source path is unavailable".to_owned())?;
        source.line = citation_line(content, source.line, &source.quote)
            .ok_or_else(|| "review_citation: quote does not match pinned source".to_owned())?;
    }
    let expected = design_criteria(requirements)
        .into_iter()
        .filter_map(|criterion| criterion["id"].as_str().map(str::to_owned))
        .collect::<BTreeSet<_>>();
    let mut seen = BTreeSet::new();
    let mut defect = false;
    let mut uncertain = false;
    let mut all_satisfied = true;
    for assessment in &report.assessments {
        if !matches!(
            assessment.status.as_str(),
            "satisfied" | "defect" | "uncertain"
        ) || !seen.insert(assessment.criterion.clone())
            || assessment.reason.trim().is_empty()
            || assessment.counterexample.trim().is_empty()
            || assessment.sources.is_empty()
            || assessment
                .sources
                .iter()
                .any(|source| !source_ids.contains(source))
        {
            return Err("invalid criterion assessment".to_owned());
        }
        defect |= assessment.status == "defect";
        uncertain |= assessment.status == "uncertain";
        all_satisfied &= assessment.status == "satisfied";
    }
    if seen != expected {
        return Err("design review must assess every supplied criterion exactly once".to_owned());
    }
    let own = own_objections.iter().cloned().collect::<BTreeSet<_>>();
    let mut updated = BTreeSet::new();
    let mut open = Vec::new();
    for update in &report.objection_updates {
        if !matches!(update.status.as_str(), "open" | "resolved")
            || [
                &update.detail,
                &update.counterexample,
                &update.closure,
                &update.evidence,
            ]
            .iter()
            .any(|value| value.trim().is_empty())
        {
            return Err("invalid objection disposition".to_owned());
        }
        if update.id.is_empty() {
            if update.status != "open" {
                return Err("review_objection_id".to_owned());
            }
        } else if !own.contains(&update.id) || !updated.insert(update.id.clone()) {
            return Err("review_objection_id".to_owned());
        }
        if update.status == "open" {
            open.push(update.detail.clone());
        }
    }
    if updated != own {
        return Err("review_objection_coverage".to_owned());
    }
    if open != report.concerns {
        return Err("review_concerns".to_owned());
    }
    if report.decision == "approve" && (!open.is_empty() || !all_satisfied) {
        return Err("approval contradicts incomplete criterion evidence".to_owned());
    }
    if defect && open.is_empty() {
        return Err("criterion defects need actionable objections".to_owned());
    }
    if uncertain && open.is_empty() && report.questions.is_empty() {
        return Err("criterion uncertainty needs an objection or clarification".to_owned());
    }
    Ok(report)
}

/// Return the pinned source line or a unique relocated literal quote.
fn citation_line(content: &str, requested: usize, quote: &str) -> Option<usize> {
    let lines = source_lines(content);
    if citation_matches(&lines, requested, quote) {
        return Some(requested);
    }
    let quoted = source_lines(quote);
    let first = quoted.first().filter(|first| !first.is_empty())?;
    let mut found = None;
    for (index, line) in lines.iter().enumerate() {
        let candidate = index + 1;
        if line.contains(first) && citation_matches(&lines, candidate, quote) {
            if found.is_some() || quoted.len() == 1 && line.matches(first).count() > 1 {
                return None;
            }
            found = Some(candidate);
        }
    }
    found
}

/// Match a literal fragment on one line or an anchored contiguous multiline quote.
fn citation_matches(lines: &[&str], number: usize, quote: &str) -> bool {
    if number == 0 || number > lines.len() {
        return false;
    }
    let quoted = source_lines(quote);
    if quoted.first().is_none_or(|first| first.is_empty()) {
        return false;
    }
    if quoted.len() == 1 {
        return lines[number - 1].contains(quoted[0]);
    }
    let window = &lines[number - 1..];
    window.len() >= quoted.len()
        && window[0].ends_with(quoted[0])
        && window[1..quoted.len() - 1] == quoted[1..quoted.len() - 1]
        && window[quoted.len() - 1].starts_with(quoted[quoted.len() - 1])
}

/// Keep LF source positions while normalizing terminal CR and one final newline.
fn source_lines(text: &str) -> Vec<&str> {
    let mut lines = text.split('\n').collect::<Vec<_>>();
    if lines.last() == Some(&"") {
        lines.pop();
    }
    lines
        .into_iter()
        .map(|line| line.strip_suffix('\r').unwrap_or(line))
        .collect()
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use serde_json::json;

    use crate::Requirements;

    use super::validate_design_review;

    #[test]
    fn approval_requires_complete_criteria_and_literal_pinned_citation() {
        let requirements = Requirements {
            title: "Change output".to_owned(),
            body: None,
            discussion: vec![],
        };
        let files = BTreeMap::from([("src/lib.rs".to_owned(), "fn output() {}\n".to_owned())]);
        let report = json!({
            "decision":"approve","rationale":"The design covers the change.","risk":"low",
            "questions":[],"concerns":[],
            "scope":{"goal_ids":["G1"],"fit":"Keeps output correct.","conflicts":[]},
            "sources":[{"id":"S1","path":"src/lib.rs","line":1,"quote":"fn output()"}],
            "assessments":[{"criterion":"R1","status":"satisfied","sources":["S1"],
                "reason":"The output path is included.","counterexample":"None observed."}],
            "objection_updates":[]
        });
        let charter = r#"{"goals":{"G1":"Correctness"}}"#;
        validate_design_review(report.clone(), &requirements, charter, &files, &[])
            .expect("complete approval");
        let mut false_citation = report.clone();
        false_citation["sources"][0]["quote"] = json!("invented source");
        assert!(
            validate_design_review(false_citation, &requirements, charter, &files, &[])
                .expect_err("false citation")
                .contains("review_citation")
        );
        let mut missing_criterion = report;
        missing_criterion["assessments"] = json!([]);
        assert!(
            validate_design_review(missing_criterion, &requirements, charter, &files, &[])
                .expect_err("missing criterion")
                .contains("every supplied criterion")
        );
    }
}
