---
name: concurro-assess-scope
description: Assess a user request against the Concurro charter before planning.
---

# concurro-assess-scope

For standalone use, first read the [shared instructions](../../instructions.md) and the target repository's versioned project charter (`.concurro/charter.json`). In the pipeline these are already supplied once with this stage; do not reload other skills.

Decide proceed, clarify, reject or escalate. Proceed requires actionable requirements, observable acceptance criteria, known goals with fit evidence, no non-goal conflicts and no unanswered questions. Advisory `concerns` may accompany proceed: record compatibility risks, edge cases and validation gaps for planning and review. They are not blocking objections and do not require a human answer. Use clarify for missing human intent, reject for unsuitable work, and escalate for consequential decisions requiring human authority. Explain insufficient context; do not guess consensus compatibility.

Distinguish the requested outcome from mistakes a future implementation could make:

- `scope.conflicts` records actual incompatibilities between the requested outcome and the charter. Name the charter rule and explain why satisfying the request would violate it. A hypothetical bad implementation is not a charter conflict.
- `concerns` records hazards and guardrails the planner and reviewers can address. For example, "Changing padding normalization could alter consensus encoding; preserve the existing encoding" is an advisory concern when the request does not require that change. A request that explicitly requires a consensus change forbidden by the charter is a conflict.
- `questions` records missing human intent or decisions that repository inspection cannot settle. Investigate code behavior yourself; do not ask a human to do that research.

Concerns are shown to humans as Markdown. Use inline code for identifiers and paths where helpful, and fenced code blocks for examples.

Before returning `proceed`, verify that `questions` and `scope.conflicts` are empty. Keep real blockers and choose a non-proceed decision; do not hide them as concerns to satisfy the contract.

In the pipeline, use the supplied external [response contract](../../schemas/intake.json). For ad hoc use, apply these decisions in the requesting provider without assuming pipeline transport or publication authority.

Return the required `classification` using the shared rubric. Assess the issue as currently understood; this is a provisional estimate. High change risk alone does not prevent proceeding or approval; report real defects or unresolved human decisions through the existing verdict fields.
