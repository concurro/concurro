---
name: concurro-review-design
description: Review an implementation plan before coding or after revision. Independently assess source evidence, requirements, scope and risks; approve or raise actionable design objections.
---

# concurro-review-design

## Review

Review the exact plan, base, requirements, charter and bundle identity against current source,
not planner claims or earlier approvals. Assess coherence, independent reviewability and whether
scope fits the design and its risks. Revise unnecessarily broad envelopes, especially around
consensus/wire code. Judge dependency changes as design decisions; require their manifests and
lockfiles in scope.

- Cooperative mode: retain your review context and reassess each own open objection through
  `objection_updates`. Reuse its ID for the same defect. Open details must equal `concerns` in
  order; resolved dispositions may accompany revise.
- Fresh mode: independently assess the final design without earlier votes or discussion, even
  if you previously planned or will implement it. This remains a separate approval gate.

Do not infer peer votes or defer to other models. Approve only with every criterion satisfied
and no unanswered questions, concerns, non-goal conflicts or unresolved human decisions.
Revise concrete defects, clarify missing intent, reject off-goal work, and escalate consequential
uncertainty.

## Evidence and report

Return the supplied [response contract](../../schemas/plan_qc.json). Cover every `review_criteria`
ID in `assessments` with source IDs, an explanation and a considered counterexample.

Declare citations in `sources`: unique `id`, repository-relative `path`, 1-based `line` and
literal `quote`. Assessments reference only those IDs: `["S1"]`, not `["src/file.rs:42"]`.
External issue/PR URLs and search-output prose are not repository citations. Insufficient
evidence requires reconsidering the verdict and preserving genuine questions/objections,
never inventing citations for approval.

Include `classification` using the response-schema rubric, assessing the exact plan independently
of peer ratings. Keep evidence proportionate but cover every criterion and objection; full
reviews are archived, and audit history does not reduce the response allowance. Do not negotiate
a prose budget.
