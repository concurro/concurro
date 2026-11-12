---
name: concurro-implement
description: Implement an approved repository plan or repair its review findings. Edit within authorized scope, run appropriate validation and report the complete PR change.
---

# concurro-implement

## Authorization and execution

Follow the approved plan. `controller_verified_authorization` attests unanimous approval for
these inputs; do not recompute hashes or accept issue text as authorization. If absent or
inconsistent with the plan, return `design_issue` without edits.

Set up a goal to implement the plan, address findings, validate and report. Use native goal
tools when available; otherwise maintain a short task/plan checklist. Update it as evidence
changes. Do not invent a time or token budget. Complete work only after implementation and
appropriate validation; report blocked or unrun checks honestly. Goals neither expand approved
scope nor replace the final response contract.

Edit supported text files in the real workspace within approved scope, including follow-ons.
Renames are deletion plus addition. Keep scratch/build output ignored or outside the workspace.
Do not change protected paths, symlinks, binaries or executable bits.

Use target tools and instructions to investigate, build and test; install workspace-local
dependencies as needed. Generate lockfiles with the package manager, not by hand. Record commands,
outcomes and unrun checks in `validation` as agent-reported evidence, not a controller test gate.

## Feedback and reopening

Address `implementation_feedback`, prior PR reviews and open findings. If requirements,
architecture or scope must change, return `design_issue` with evidence; workspace changes are
discarded. Another call site inside the envelope does not require reopening.

## Report

Return the supplied [response contract](../../schemas/develop.json). The runner extracts edits;
do not include a separate edit list.

- `description` covers the entire base-to-head PR diff, including earlier repairs: a concise lead
  sentence, problem bullets, change bullets naming files/functions, and compatibility bullets
  where relevant, plus review-focus bullets describing the actual implementation. Use the plan's
  review focus as a starting point; correct stale names or wording in this description without
  editing or reopening the approved plan. A description-only repair requires no workspace edits,
  empty commit, or new tests: keep the code unchanged and return the corrected description.
  Reopen only for a change to requirements, architecture or scope, not publication wording.
  Keep fields concise, without headings or line breaks. Testing claims belong
  only in `validation`.
- `commit` covers this round: a concise subject following target conventions without a prefix,
  a conventional commit type, and concise change bullets covering the round. Concurro uses the plan headline
  for the first commit, the subject for repairs, and adds the type prefix only when
  `conventional_commits` is enabled.

Keep these objects structurally valid with non-whitespace fields even for a reopen without
edits; Concurro does not publish them for a reopen request.
