---
name: concurro-review-pr
description: Review the exact published Concurro PR head independently.
---

# concurro-review-pr

For standalone use, first read the [shared instructions](../../instructions.md) and the target repository's versioned project charter (`.concurro/charter.json`). In the pipeline these are already supplied once with this stage; do not reload other skills.

Review every entry in `changes` (each changed file's diff against the approved base), acceptance criteria and applicable repository review guidance. Read whole files, the base branch and history in the repository. Peer votes are withheld. Request changes for implementation defects. Signal requirements or plan defects using verdict reopen and structured design_issue evidence, with no findings or dispositions; fresh unanimous design approval is required before further changes.

Findings:

- Report only new defects in `findings`. Name the file (`path`) whenever a finding can realistically be attributed to one, and anchor it with `start_line` and `line` on the reviewed head version when it concerns specific lines; use `null` otherwise. A finding about the change as a whole has a `null` path.
- Give each new finding a `severity` and a `title`, a plain label under 100 characters. `critical` breaks consensus, security, funds or data; `high` breaks required behaviour or an acceptance criterion; `medium` is a real defect with limited impact; `low` is a minor correctness or clarity issue. Severity orders findings for readers; every open finding blocks the merge regardless.
- `own_findings` lists your earlier findings that are still open or were withdrawn by a design reopen. Answer each exactly once in `dispositions`: `fixed` with evidence naming the changed lines that fix it, `open` with what remains wrong, or, for a withdrawn finding only, `obsolete` with evidence that the new plan no longer makes it apply. Do not repeat an answered finding as a new one.
- Approve only with no new findings and no `open` disposition.
- Keep the whole JSON response within `review_output_budget.max_json_bytes`; prefer fewer, precise findings to long ones.

The candidate may change files beyond the plan's primary files inside its approved scope; review every changed file, and use verdict reopen when the change has drifted from the approved design even inside the scope.

In the pipeline, use the supplied external [response contract](../../schemas/review.json). For ad hoc use, apply these decisions in the requesting provider without assuming pipeline transport or publication authority.
