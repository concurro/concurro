//! Typed pull request review findings and owner-bound dispositions.

use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{
    DesignReviewer, InitializationSnapshot, design::configuration_digest, legacy_requirements,
    markdown_text, python_digest,
};

/// Which design input needs reassessment after concrete new evidence.
#[derive(Clone, Debug, Eq, PartialEq, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DesignIssueKind {
    /// The issue's requirements need clarification or a new source judgment.
    Requirements,
    /// The approved plan needs another revision and design review.
    Plan,
}

/// Structured evidence for reopening design from implementation or PR review.
#[derive(Clone, Debug, Eq, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DesignIssue {
    /// Requirements or plan scope of the reported issue.
    pub kind: DesignIssueKind,
    /// Plain explanation for the writer and next planner.
    pub explanation: String,
    /// Concrete observations that support reassessment.
    pub evidence: Vec<String>,
}

/// One new finding anchored to the reviewed head when a source path is known.
#[derive(Clone, Debug, Eq, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PrFinding {
    /// Repository-relative path, or none for a general finding.
    pub path: Option<String>,
    /// First line of an optional right-side range.
    pub start_line: Option<u64>,
    /// Last right-side line of an optional range.
    pub line: Option<u64>,
    /// Complete reviewer evidence.
    pub detail: String,
    /// Optional presentation severity.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub severity: Option<String>,
    /// Optional presentation title.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
}

/// The owner reviewer's answer to one previously open or withdrawn finding.
#[derive(Clone, Debug, Eq, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PrDisposition {
    /// Stable finding identity.
    pub id: String,
    /// Fixed, open, or obsolete for a withdrawn finding.
    pub status: String,
    /// Concrete basis for the status.
    pub evidence: String,
}

/// Validated independent review against one immutable PR head and binding.
#[derive(Clone, Debug, Eq, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PrReviewReport {
    /// Approve, request changes, or reopen design.
    pub verdict: String,
    /// Reviewer's summary.
    pub summary: String,
    /// New findings owned by this reviewer.
    pub findings: Vec<PrFinding>,
    /// One answer for each of the reviewer's outstanding findings.
    pub dispositions: Vec<PrDisposition>,
    /// Structured design issue only with a reopen verdict.
    pub design_issue: Option<DesignIssue>,
    /// Optional display headline.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub headline: Option<String>,
}

/// Bind one reviewer's report to the immutable PR head and pinned governance inputs.
pub fn pr_review_binding(
    snapshot: &InitializationSnapshot,
    plan: &Value,
    plan_id: &str,
    head: &str,
    reviewer: &DesignReviewer,
) -> Result<String, String> {
    review_binding(snapshot, plan, Some(plan_id), head, reviewer)
}

/// Bind a standalone report to its frozen source without a contribution plan.
pub fn standalone_pr_review_binding(
    snapshot: &InitializationSnapshot,
    head: &str,
    reviewer: &DesignReviewer,
) -> Result<String, String> {
    review_binding(snapshot, &Value::Null, None, head, reviewer)
}

/// Hash the inputs shared by contribution and standalone PR assessments.
fn review_binding(
    snapshot: &InitializationSnapshot,
    plan: &Value,
    plan_id: Option<&str>,
    head: &str,
    reviewer: &DesignReviewer,
) -> Result<String, String> {
    let agent = reviewer.agent.clone().unwrap_or_else(|| {
        let (vendor, effort) = match reviewer.provider.as_str() {
            "claude" => (json!("anthropic"), json!("medium")),
            "codex" => (json!("openai"), json!("low")),
            _ => (Value::Null, Value::Null),
        };
        json!({
            "profile":reviewer.provider,
            "harness":reviewer.provider,
            "provider":vendor,
            "model":reviewer.model,
            "effort":effort,
        })
    });
    let requirements = python_digest(&legacy_requirements(&snapshot.requirements))
        .map_err(|error| error.to_string())?;
    let plan_content = python_digest(plan).map_err(|error| error.to_string())?;
    python_digest(&json!({
        "base":snapshot.pins.base_sha,
        "head":head,
        "requirements":requirements,
        "plan":plan_id,
        "config":configuration_digest(&snapshot.configuration)?,
        "bundle":snapshot.bundle_id,
        "plan_content":plan_content,
        "agent":agent,
    }))
    .map_err(|error| error.to_string())
}

/// Check one design issue before any plan or approval is invalidated.
pub fn validate_design_issue(raw: Value) -> Result<DesignIssue, String> {
    let issue: DesignIssue = serde_json::from_value(raw)
        .map_err(|_| "design reopening requires structured evidence".to_owned())?;
    if issue.explanation.trim().is_empty()
        || issue.evidence.is_empty()
        || issue.evidence.iter().any(|item| item.trim().is_empty())
    {
        return Err("design reopening requires structured evidence".to_owned());
    }
    Ok(issue)
}

/// Validate a PR review against exactly this reviewer's outstanding finding IDs.
pub fn validate_pr_review(
    mut raw: Value,
    open_ids: &[String],
    withdrawn_ids: &[String],
) -> Result<PrReviewReport, String> {
    let fields = raw.as_object_mut().ok_or("invalid review fields")?;
    fields.remove("classification");
    if [
        "verdict",
        "summary",
        "findings",
        "dispositions",
        "design_issue",
    ]
    .iter()
    .any(|key| !fields.contains_key(*key))
    {
        return Err("invalid review fields".to_owned());
    }
    if fields
        .get("headline")
        .is_some_and(|value| !value.is_string())
    {
        return Err("invalid presentation field".to_owned());
    }
    if let Some(findings) = fields.get("findings").and_then(Value::as_array) {
        for finding in findings {
            if ["path", "start_line", "line", "detail"]
                .iter()
                .any(|key| finding.get(*key).is_none())
            {
                return Err("invalid finding".to_owned());
            }
            for key in ["severity", "title"] {
                if finding.get(key).is_some_and(|value| !value.is_string()) {
                    return Err("invalid presentation field".to_owned());
                }
            }
        }
    }
    let report: PrReviewReport =
        serde_json::from_value(raw).map_err(|_| "invalid review fields or content".to_owned())?;
    if !matches!(
        report.verdict.as_str(),
        "approve" | "changes_requested" | "reopen"
    ) {
        return Err("invalid review verdict".to_owned());
    }
    if report.verdict == "reopen" {
        let issue = report
            .design_issue
            .as_ref()
            .ok_or("design reopening requires structured evidence")?;
        validate_design_issue(serde_json::to_value(issue).map_err(|error| error.to_string())?)?;
        if !report.findings.is_empty() || !report.dispositions.is_empty() {
            return Err("design reopening cannot also report or answer findings".to_owned());
        }
        return Ok(report);
    }
    if report.design_issue.is_some() {
        return Err("review verdict contradicts design issue".to_owned());
    }
    for finding in &report.findings {
        if finding.detail.trim().is_empty()
            || finding.path.as_deref().is_some_and(|path| {
                path.trim().is_empty()
                    || path.starts_with('/')
                    || path.split('/').any(|part| part == "..")
            })
            || finding.line == Some(0)
            || finding.start_line == Some(0)
            || finding
                .start_line
                .is_some_and(|start| finding.line.is_none_or(|end| start > end))
            || (finding.path.is_none() && finding.line.is_some())
            || finding
                .severity
                .as_deref()
                .is_some_and(|severity| !matches!(severity, "critical" | "high" | "medium" | "low"))
        {
            return Err("invalid PR finding".to_owned());
        }
    }
    let expected = open_ids
        .iter()
        .chain(withdrawn_ids)
        .cloned()
        .collect::<BTreeSet<_>>();
    let answered = report
        .dispositions
        .iter()
        .map(|item| item.id.clone())
        .collect::<BTreeSet<_>>();
    if answered != expected || answered.len() != report.dispositions.len() {
        return Err("dispositions must answer each owned finding once".to_owned());
    }
    let withdrawn = withdrawn_ids.iter().collect::<BTreeSet<_>>();
    for answer in &report.dispositions {
        if answer.evidence.trim().is_empty()
            || !matches!(answer.status.as_str(), "fixed" | "open" | "obsolete")
            || (answer.status == "obsolete" && !withdrawn.contains(&answer.id))
        {
            return Err("invalid disposition".to_owned());
        }
    }
    let still_open =
        !report.findings.is_empty() || report.dispositions.iter().any(|item| item.status == "open");
    if (report.verdict == "approve") == still_open {
        return Err("review verdict contradicts findings".to_owned());
    }
    Ok(report)
}

/// Validate a standalone PR assessment without a contribution design to reopen.
pub fn validate_standalone_pr_review(
    raw: Value,
    owned_ids: &[String],
) -> Result<PrReviewReport, String> {
    let report = validate_pr_review(raw, &[], owned_ids)?;
    if report.verdict == "reopen" {
        return Err("standalone review cannot reopen contribution design".to_owned());
    }
    Ok(report)
}

/// Render one bounded, exact-body native PR review from a stored report.
pub fn pr_review_body(
    marker: &str,
    reviewer: &DesignReviewer,
    report: &PrReviewReport,
    finding_ids: &[String],
) -> String {
    let mut lines = vec![
        marker.to_owned(),
        format!(
            "### {} · {}",
            markdown_text(&reviewer.id, 100),
            markdown_text(&report.verdict.replace('_', " "), 100),
        ),
        String::new(),
        markdown_text(&report.summary, 12_000),
    ];
    if let Some(issue) = &report.design_issue {
        lines.push(String::new());
        lines.push(format!(
            "Design reopened ({}): {}",
            markdown_text(&format!("{:?}", issue.kind).to_lowercase(), 30),
            markdown_text(&issue.explanation, 2_000),
        ));
        for evidence in &issue.evidence {
            lines.push(format!("- {}", markdown_text(evidence, 1_000)));
        }
    }
    if !report.findings.is_empty() {
        lines.extend([String::new(), "New findings:".to_owned(), String::new()]);
        for (id, finding) in finding_ids.iter().zip(&report.findings) {
            let location = finding.path.as_deref().map_or_else(
                || "general".to_owned(),
                |path| match finding.line {
                    Some(line) => format!("{}:{line}", markdown_text(path, 300)),
                    None => markdown_text(path, 300),
                },
            );
            lines.push(format!(
                "- **{id}** ({location}): {}",
                markdown_text(&finding.detail, 2_000),
            ));
        }
    }
    if !report.dispositions.is_empty() {
        lines.extend([String::new(), "Earlier findings:".to_owned(), String::new()]);
        for disposition in &report.dispositions {
            lines.push(format!(
                "- **{}** {}: {}",
                markdown_text(&disposition.id, 100),
                markdown_text(&disposition.status, 100),
                markdown_text(&disposition.evidence, 2_000),
            ));
        }
    }
    let body = lines.join("\n");
    if body.len() <= 60_000 {
        body
    } else {
        format!(
            "{marker}\n### {} · {}\n\n{}\n\nThe complete report is retained in the task archive.",
            markdown_text(&reviewer.id, 100),
            markdown_text(&report.verdict.replace('_', " "), 100),
            markdown_text(&report.summary, 2_000),
        )
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use serde_json::json;

    use crate::{InitializationSnapshot, PinnedInputs, Requirements, pr_review_roster};

    use super::{pr_review_binding, validate_pr_review, validate_standalone_pr_review};

    #[test]
    fn standalone_review_accepts_findings_but_refuses_design_reopen() {
        let reviewed = validate_standalone_pr_review(
            json!({"verdict":"changes_requested","summary":"Needs a fix",
                "findings":[{"path":"src/lib.rs","start_line":null,"line":3,
                    "detail":"This branch misses the case"}],
                "dispositions":[],"design_issue":null}),
            &[],
        )
        .expect("standalone findings");
        assert_eq!(reviewed.findings.len(), 1);
        let settled = validate_standalone_pr_review(
            json!({"verdict":"approve","summary":"Earlier finding no longer applies",
                "findings":[],"dispositions":[{"id":"F1","status":"obsolete",
                    "evidence":"The changed code no longer contains that path"}],
                "design_issue":null}),
            &["F1".to_owned()],
        )
        .expect("second-round disposition");
        assert_eq!(settled.dispositions[0].status, "obsolete");
        let reopened = validate_standalone_pr_review(
            json!({"verdict":"reopen","summary":"Change the plan",
                "findings":[],"dispositions":[],
                "design_issue":{"kind":"plan","explanation":"Plan is wrong",
                    "evidence":["The observed path differs"]}}),
            &[],
        );
        assert_eq!(
            reopened.expect_err("no contribution design"),
            "standalone review cannot reopen contribution design"
        );
    }

    /// A review cannot carry over to a changed head, model profile, or governed plan.
    #[test]
    fn binding_tracks_head_plan_and_agent_but_ignores_service_engine_pin() {
        let config = json!({
            "repository":"acme/demo","engine":"a".repeat(40),
            "agents":{"pr_reviewers":["sol"],"plan_reviewers":["sol"]},
            "models":{"sol":{"provider":"openai","model":"gpt","effort":"high"}}
        });
        let snapshot = InitializationSnapshot {
            pins: PinnedInputs {
                config_sha: "a".repeat(40),
                base_sha: "b".repeat(40),
                max_model_calls: 5,
                max_fix_rounds: 2,
                max_plan_revisions: 1,
            },
            configuration: config.clone(),
            requirements: Requirements {
                title: "Task".to_owned(),
                body: None,
                discussion: vec![],
            },
            charter: "{}".to_owned(),
            instruction_texts: BTreeMap::new(),
            policy_documents: BTreeMap::new(),
            base_files: vec![],
            engine_resources: BTreeMap::new(),
            bundle_id: "bundle".to_owned(),
        };
        let reviewer = pr_review_roster(&config).expect("roster").remove(0);
        let plan = json!({"summary":"Change function"});
        let binding = pr_review_binding(&snapshot, &plan, "plan-id", &"c".repeat(40), &reviewer)
            .expect("binding");
        assert_ne!(
            binding,
            pr_review_binding(&snapshot, &plan, "plan-id", &"d".repeat(40), &reviewer)
                .expect("changed head")
        );
        assert_ne!(
            binding,
            pr_review_binding(
                &snapshot,
                &json!({"summary":"Different"}),
                "plan-id",
                &"c".repeat(40),
                &reviewer
            )
            .expect("changed plan")
        );
        let mut service_upgrade = snapshot.clone();
        service_upgrade.configuration["engine"] = json!("d".repeat(40));
        assert_eq!(
            binding,
            pr_review_binding(
                &service_upgrade,
                &plan,
                "plan-id",
                &"c".repeat(40),
                &reviewer
            )
            .expect("service upgrade")
        );
        let mut model_change = snapshot.clone();
        model_change.configuration["models"]["sol"]["model"] = json!("gpt-next");
        let changed_reviewer = pr_review_roster(&model_change.configuration)
            .expect("changed roster")
            .remove(0);
        assert_ne!(
            binding,
            pr_review_binding(
                &model_change,
                &plan,
                "plan-id",
                &"c".repeat(40),
                &changed_reviewer
            )
            .expect("changed model")
        );
    }

    /// Findings and dispositions must be owned and consistent with the verdict.
    #[test]
    fn review_answers_each_owned_finding_once() {
        let report = json!({
            "verdict":"approve","summary":"Fixed.","findings":[],"design_issue":null,
            "dispositions":[{"id":"F1","status":"fixed","evidence":"Test added."}]
        });
        assert!(validate_pr_review(report.clone(), &["F1".to_owned()], &[]).is_ok());
        assert!(validate_pr_review(report.clone(), &["F2".to_owned()], &[]).is_err());
        let mut open = report;
        open["dispositions"][0]["status"] = json!("open");
        assert!(validate_pr_review(open, &["F1".to_owned()], &[]).is_err());
    }

    /// A withdrawn finding can be retired, but an open finding must be fixed or kept open.
    #[test]
    fn obsolete_disposition_requires_withdrawn_owner_record() {
        let report = json!({
            "verdict":"approve","summary":"Prior plan replaced.","findings":[],
            "design_issue":null,
            "dispositions":[{"id":"F1","status":"obsolete","evidence":"The old path was removed."}]
        });
        assert!(validate_pr_review(report.clone(), &[], &["F1".to_owned()]).is_ok());
        assert!(validate_pr_review(report, &["F1".to_owned()], &[]).is_err());
    }

    /// A design reopen carries evidence and cannot smuggle findings into the old head.
    #[test]
    fn reopen_requires_structured_issue_without_findings() {
        let mut report = json!({
            "verdict":"reopen","summary":"The approved design misses a case.",
            "findings":[],"dispositions":[],
            "design_issue":{"kind":"requirements","explanation":"Input format differs.",
                "evidence":["The documented fixture uses a second variant."]}
        });
        assert!(validate_pr_review(report.clone(), &[], &[]).is_ok());
        report["findings"] = json!([{"path":null,"start_line":null,"line":null,"detail":"Issue"}]);
        assert!(validate_pr_review(report, &[], &[]).is_err());
    }

    /// A finding location is a repository path and a valid right-side line range.
    #[test]
    fn finding_rejects_invalid_path_and_range() {
        let mut report = json!({
            "verdict":"changes_requested","summary":"Fix this.","dispositions":[],
            "design_issue":null,
            "findings":[{"path":"src/lib.rs","start_line":2,"line":3,"detail":"Incorrect result."}]
        });
        assert!(validate_pr_review(report.clone(), &[], &[]).is_ok());
        report["findings"][0]["path"] = json!("../src/lib.rs");
        assert!(validate_pr_review(report.clone(), &[], &[]).is_err());
        report["findings"][0]["path"] = json!("src/lib.rs");
        report["findings"][0]["start_line"] = json!(4);
        assert!(validate_pr_review(report, &[], &[]).is_err());
    }
}
