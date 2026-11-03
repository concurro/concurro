---
name: concurro-design
description: Design or revise a bounded improvement to the target repository before implementation.
---

# concurro-design

For standalone use, first read the [shared instructions](../../instructions.md) and the target repository's versioned project charter (`.concurro/charter.json`). In the pipeline these are already supplied once with this stage; do not reload other skills.

Produce precise files, implementation steps and observable tests from the requirements, charter and source inventory. Apply the supplied validation skill and capability catalog. `validation` selects executable named checks and file rules; `tests` describes semantic acceptance checks, not shell commands. Address `validation_feedback` before another review. Address consolidated objections and relevant prior outcomes as evidence. Return one `responses` entry per supplied open objection, using its exact ID and explaining the change or a reasoned disagreement. Use an empty array when there are no open objections. Your response does not resolve an objection; its reviewer must reassess it. Questions stop implementation. For an existing PR, reconcile every accumulated changed file within the new scope; otherwise escalate through questions.

Give the plan a `headline`: an imperative summary of the change under 60 characters, such as "Recommend plain cargo build in the README". It becomes the pull request's title, so do not prefix it with "Plan:". Keep the summary about the design, without earlier verdicts or claims of approval. Put concise changes since the previous version and the points needing reviewer attention in `publication.changes` and `publication.review_focus`. The full plan belongs in its document artifact; these lists appear in the chronological issue comment. Do not repeat full steps or tests in them.

In the pipeline, use the supplied external [response contract](../../schemas/plan.json). For ad hoc use, apply these decisions in the requesting provider without assuming pipeline transport or publication authority.
