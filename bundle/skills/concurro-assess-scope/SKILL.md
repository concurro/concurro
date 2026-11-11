---
name: concurro-assess-scope
description: Triage repository change requests before planning. Assess charter fit, requirement clarity and acceptance criteria; decide whether to proceed, clarify, reject or escalate.
---

# concurro-assess-scope

## Decision

Choose `proceed`, `clarify`, `reject` or `escalate`. Proceed requires actionable requirements,
observable acceptance criteria, known charter goals with fit evidence, and no non-goal conflicts or
unanswered questions. Clarify missing human intent, reject unsuitable work, and escalate
consequential decisions requiring human authority. Explain insufficient context; do not guess
consensus compatibility.

Distinguish the requested outcome from implementation hazards:

- `scope.conflicts`: actual charter incompatibilities. Name the rule and why fulfilling the
  request violates it; a hypothetical bad implementation is not a conflict.
- `concerns`: advisory compatibility risks, edge cases and validation gaps for planning and
  review. These may accompany proceed and need no human answer. Preserving consensus encoding
  during cleanup is a concern; an explicitly requested, charter-forbidden encoding change is
  a conflict.
- `questions`: missing intent or decisions repository inspection cannot settle. Investigate
  code behavior yourself.

Before proceeding, confirm `questions` and `scope.conflicts` are empty. Preserve real blockers
with a non-proceed decision; do not disguise them as concerns.

## Report

Return the supplied [response contract](../../schemas/intake.json). Write concerns in Markdown,
using inline code for identifiers/paths and fenced examples where helpful. Include
`classification` using the response-schema rubric: this is a provisional estimate of the issue
as currently understood.
