---
name: concurro-review-design
description: Independently review one exact Concurro design during cooperative revisions and fresh final approval.
---

# concurro-review-design

For standalone use, first read the [shared instructions](../../instructions.md) and the target repository's versioned project charter (`.concurro/charter.json`). In the pipeline these are already supplied once with this stage; do not reload other skills.

Review the exact plan, base, requirements, charter and bundle identity against repository evidence. Cover every `review_criteria` ID in `assessments`, with repository `sources`, an explanation and a considered counterexample. Verify current source rather than accepting a planner claim or earlier approval. In cooperative mode retain your own review context and reassess each own open objection through `objection_updates`. Its open details must equal `concerns` in order; resolved dispositions may accompany a revise verdict. Refine the same ID for the same defect rather than issuing a duplicate. In fresh mode assess the final design without earlier votes or reviewer discussion; this is a separate gate, including when you previously planned or will implement it. Do not infer peer votes or defer to other models. Judge whether the plan is coherent and independently reviewable, and whether its `scope` paths fit the design and its risks; an envelope wider than the design needs, especially around consensus or wire code, is a reason to revise. Line count alone is not a defect or a reason to revise; any legacy `max_changed_lines` field is ignored. Do not negotiate a line allowance or trim necessary tests to fit one. A dependency change is a design decision: judge it, and require its manifests and lockfiles inside the envelope. Approve only with all criteria satisfied and no unanswered questions, concerns, non-goal conflicts or unresolved decisions requiring human judgment. Revise concrete defects, clarify missing intent, reject off-goal work, escalate consequential uncertainty.

Declare repository citations in `sources`, each with a unique `id`, repository-relative `path`, 1-based `line` and literal `quote`. In each assessment, `sources` contains only those exact IDs: if the citation has `id: "S1"`, use `["S1"]`, not `["src/file.rs:42"]`. External issue/PR URLs and search-output prose are not repository citations. If evidence is insufficient, reconsider the verdict and preserve genuine questions or objections; do not invent citations to obtain approval.

In the pipeline, use the supplied external [response contract](../../schemas/plan_qc.json). For ad hoc use, apply these decisions in the requesting provider without assuming pipeline transport or publication authority.

Return the required `classification` using the shared rubric. Assess the exact proposed plan independently of peer ratings. High change risk alone does not prevent proceeding or approval; report real defects or unresolved human decisions through the existing verdict fields.

Keep evidence clear and proportionate. Reviews are archived in full; audit history does not reduce your response allowance. Cover every criterion and objection without negotiating a prose budget.
