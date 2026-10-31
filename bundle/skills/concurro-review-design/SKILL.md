---
name: concurro-review-design
description: Independently review one exact Concurro design during cooperative revisions and fresh final approval.
---

# concurro-review-design

For standalone use, first read the [shared instructions](../../instructions.md) and the target repository's versioned project charter (`.concurro/charter.json`). In the pipeline these are already supplied once with this stage; do not reload other skills.

Review the exact plan, base, requirements, charter and bundle identity using the supplied validation skill and check catalog. Cover every `review_criteria` ID in `assessments`, with repository `sources`, an explanation and a considered counterexample. Verify current source rather than accepting a planner claim or earlier approval. In cooperative mode retain your own review context and reassess each own open objection through `objection_updates`. Its open details must equal `concerns` in order; resolved dispositions may accompany a revise verdict. Refine the same ID for the same defect rather than issuing a duplicate. In fresh mode assess the final design without earlier votes or reviewer discussion; this is a separate gate, including when you previously planned or will implement it. Do not infer peer votes or defer to other models. Approve only with all criteria satisfied and no questions, concerns, non-goal conflicts or high risk. Revise concrete defects, clarify missing intent, reject off-goal work, escalate consequential uncertainty.

In the pipeline, use the supplied external [response contract](../../schemas/plan_qc.json). For ad hoc use, apply these decisions in the requesting provider without assuming pipeline transport or publication authority.
