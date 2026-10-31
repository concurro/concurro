---
name: concurro-review-design
description: Independently review one exact Concurro design during cooperative revisions and fresh final approval.
---

# concurro-review-design

For standalone use, first read the [shared instructions](../../instructions.md) and the target repository's versioned project charter (`.concurro/charter.json`). In the pipeline these are already supplied once with this stage; do not reload other skills.

Review the exact plan, base, requirements, charter and bundle identity. In cooperative mode retain your own review context across revisions and reassess your objections against the revised source and plan. In fresh mode assess the final design without earlier votes or reviewer discussion; this is a separate approval gate, including when you previously planned or will implement it. Do not infer peer votes or defer to other models. Cite goals and fit evidence; check compatibility, completeness, tests and accumulated PR changes. Approve only with no questions, concerns, non-goal conflicts or high risk. Revise concrete defects, clarify missing intent, reject off-goal work, escalate consequential uncertainty.

In the pipeline, use the supplied external [response contract](../../schemas/plan_qc.json). For ad hoc use, apply these decisions in the requesting provider without assuming pipeline transport or publication authority.
