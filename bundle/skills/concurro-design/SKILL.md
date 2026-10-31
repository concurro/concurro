---
name: concurro-design
description: Design or revise a bounded improvement to the target repository before implementation.
---

# concurro-design

For standalone use, first read the [shared instructions](../../instructions.md) and the target repository's versioned project charter (`.concurro/charter.json`). In the pipeline these are already supplied once with this stage; do not reload other skills.

Produce precise files, implementation steps and observable tests from the requirements, charter and source inventory. Address consolidated objections and relevant prior outcomes as evidence. Return one `responses` entry per supplied open objection, using its exact ID and explaining the change or a reasoned disagreement. Use an empty array when there are no open objections. Your response does not resolve an objection; its reviewer must reassess it. Do not assume earlier approvals apply to a revision. Questions stop implementation. For an existing PR, reconcile every accumulated changed file within the new scope; otherwise escalate through questions.

In the pipeline, use the supplied external [response contract](../../schemas/plan.json). For ad hoc use, apply these decisions in the requesting provider without assuming pipeline transport or publication authority.
