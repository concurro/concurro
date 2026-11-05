---
name: concurro-design
description: Design or revise a bounded improvement to the target repository before implementation.
---

# concurro-design

For standalone use, first read the [shared instructions](../../instructions.md) and the target repository's versioned project charter (`.concurro/charter.json`). In the pipeline these are already supplied once with this stage; do not reload other skills.

Settle the architecture, design decisions and risks, and produce implementation steps and observable tests from the requirements, charter and source inventory. Do not try to predict every edit: `files` are the primary files the design is about, and `scope` draws the area the implementer may edit. Make `scope.paths` cover where the design legitimately reaches, generous enough for mechanical follow-on edits (callers of a changed signature, imports, tests) and no wider; set `max_files` and `max_changed_lines` in proportion to the design. When the design changes dependencies, say so as a design decision with its risk, and put the manifests and lockfiles it touches inside the envelope. Lockfiles count toward `max_files`, not `max_changed_lines`. `tests` describes observable acceptance criteria and useful validation commands. The implementer chooses how to validate in the target environment; no check catalog is imposed. Address consolidated objections and relevant prior outcomes as evidence. Return one `responses` entry per supplied open objection, using its exact ID and explaining the change or a reasoned disagreement. Use an empty array when there are no open objections. Your response does not resolve an objection; its reviewer must reassess it. Questions stop implementation. For an existing PR, keep every accumulated changed file inside the new scope; otherwise escalate through questions.

Give the plan a `headline`: an imperative summary of the change under 60 characters, such as "Recommend plain cargo build in the README". It becomes the pull request's title, so do not prefix it with "Plan:". Keep the summary about the design, without earlier verdicts or claims of approval. Put concise changes since the previous version and the points needing reviewer attention in `publication.changes` and `publication.review_focus`. The full plan belongs in its document artifact; these lists appear in the chronological issue comment. Do not repeat full steps or tests in them.

In the pipeline, use the supplied external [response contract](../../schemas/plan.json). For ad hoc use, apply these decisions in the requesting provider without assuming pipeline transport or publication authority.
