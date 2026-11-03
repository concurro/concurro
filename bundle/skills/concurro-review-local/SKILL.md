---
name: concurro-review-local
description: Review an Concurro candidate and real local check evidence before publication.
---

# concurro-review-local

For standalone use, first read the [shared instructions](../../instructions.md) and the target repository's versioned project charter (`.concurro/charter.json`). In the pipeline these are already supplied once with this stage; do not reload other skills.

Review every entry in `changes` (each changed file's diff against the approved base) and the real local checks. Read whole files, the base branch and history in the repository, where the head branch is the local candidate. Approve only without findings. Request concrete fixes for implementation defects. Use verdict reopen plus structured design_issue evidence for a requirements or plan defect; do not force implementation beyond the approved design. Missing source or coverage requires findings.

The candidate may change files beyond the plan's primary files inside its approved scope; review every changed file, and use verdict reopen when the change has drifted from the approved design even inside the scope.

In the pipeline, use the supplied external [response contract](../../schemas/local_review.json). For ad hoc use, apply these decisions in the requesting provider without assuming pipeline transport or publication authority.
