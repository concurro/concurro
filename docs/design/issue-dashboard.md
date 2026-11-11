# Issue dashboard

Status: implemented. The dashboard is the current task view; full reports and prior runs
remain reachable without crowding current progress. [Comment formatting](comment-format.md)
governs rendering, and [operations](../concurro.md) describes commands.

`engine/dashboard.py` renders the read-only view. The controller creates a fresh view
on each render and retains responsibility for saving state and publishing the comment.

## Current run

The dashboard opens with an activity alert, then a progress line and one table. Report
rounds sit directly below their stage, with one reviewer per line in a cell. It lists
all reviewers still outstanding, distinguishing the current call from a waiting reviewer.
Waiting for a result does not prove that a runner is already executing.
A PR closed without merging shows “Closed without merge” with a stop indicator on
the step reached. Unfinished reviewers stay “Paused” and later steps stay Pending.
Only a confirmed merge shows “Merged” with a success indicator.

```markdown
> [!NOTE]
> We're waiting for `opus55-high` and `sol61-high` to review the pull request.

| Step | Status | Detail |
| ---- | ---- | ---- |
| Plan | ✅ Version 1 | [Implementation plan](plan-url) |
| Code review | ▶️ 0 of 2 approve | [PR](pr-url) |
| ↳ Round 1 | Current | opus55-high: Reviewing<br>sol61-high: Waiting |
```

Normal progress uses `NOTE`; ready handoff uses `TIP`; human decisions use `WARNING`;
blocked tasks use `CAUTION`. The final stage is Handoff, and only confirmed merges say
Merged. Transient readiness reasons appear in diagnostics. A paused stage never says merely
“Waiting” as though work were proceeding. Output correction is named in the activity
message without repeating the reviewer twice.

`/concurro invalidate` starts a new display run. The main table shows only its intake,
plan, reviews and assessments. Display numbering restarts without resetting actual model,
repair or revision budgets. Previous report links and the invalidation reason move under
Diagnostics. The previous classification snapshot is dropped, so it cannot seed the new
run's provisional estimate. Older invalidation records are reconstructed conservatively;
malformed display history must not prevent task loading.

A separate compact assessment table shows complexity, change risk, confidence, coverage,
freshness and report links. It follows the [classification contract](complexity-risk.md).

## Diagnostics and updates

The collapsed Diagnostics section contains output-validation reasons and correction
history, prior invalidated runs and the public audit link. A rejected design review links
its immutable output; older records without retained evidence say so. These are formatting
or validation failures, not semantic review objections. The plan artifact is linked once
in the main table, not repeated in the footer.

The machine-readable checkpoint reference stays outside the collapsed section so recovery
can find it. The dashboard is regenerated on a save, not merely because a new release
was deployed. An unchanged task already watching a ready PR may need no new save.

## New comments

| Comment | When |
| ---- | ---- |
| Welcome and compact team roster | Once at enrollment; pinned models and efforts, no redundant heading |
| Intake approved with concerns | When accepted intake has concerns; Markdown preserved, not a request for human action |
| Input needed | Clarification requiring an answer |
| Escalated / Blocked / Rejected | Task needs a decision, correction or operator attention |
| Plan ready for approval | Manual-mode plan is ready for the exact approval command |
| Handed off for merge | Current approvals, complete publication and passing CI allow human review |
| Reassessment notice | Explicit invalidation with an existing PR |

Ordinary stage narration and copies of PR reviews are not posted as issue comments.
Archive links and the dashboard carry them. Action comments are keyed by occurrence;
handoff is posted once per head. Handoff means Concurro's work is complete, not that
GitHub's branch protection permits merging. Concurro never merges.

An Input-needed request records its questions and the actual answer: an eligible reply
or an issue-content change. Merely leaving clarification does not mark it answered.
Identical questions do not create another request; changed questions supersede the prior
one. Answered/superseded edits use exact-body recovery; an unexpected manual edit blocks
rather than overwriting it.

Blocked notices are less strict because they are display history. On resumption, the
engine keeps only the quoted, struck-through heading and original reason, followed by
**This blocker has been resolved.** It removes the emoji, instructions and event footer.
Only IDs and body digests are stored. Edited/deleted notices are left alone and forgotten,
not allowed to block the task. Stopping or closing does not claim the blocker was resolved.
Discovery skips terminal stages. Lost edits recover by the intended resolved-body digest.

## Report archive

The mutable audit is not a report archive: artifact reads have returned only the newest
30 versions. Reports therefore need independent immutable storage; checkpoint history
cannot guarantee that a compacted report remains retrievable.

- **Canonical report data:** new design and PR reviews store their full JSON in
  immutable, content-addressed issue artifacts. The retained response, accepted
  vote, review history and rounds archive share the same report data. Controller
  annotations and advisory ratings reference its core instead of duplicating it.
  Existing format-3 archives are reused when their complete report matches.
- **Audit references:** mutable history, statuses and annotations stay in the audit;
  bulky reports are referenced by digest and artifact numbers. Derived finding text
  uses a path into its source report rather than a second stored copy. References
  are hydrated before policy checks. Known audit, plan and report identities are
  read directly; marker discovery is for initial creation or lost-write recovery.
  Current, unarchived and pending-publication ratings remain intact; only archived
  historical ratings may be shortened or dropped from the checkpoint. The full
  archived report remains unchanged. Historical trimming is covered through the 48-call budget in regression tests.
  An unrelated duplicate cannot replace an already bound artifact. Ambiguous
  discovery, missing data and invalid provenance or digests stop recovery.
- **Cache:** the service shares an LRU holding up to 32 MiB of encoded, verified report content across ticks,
  scoped by repository, issue, author, digest and artifact numbers. Returned values
  are copies. A warm cache uses the originally verified immutable bytes, even if
  someone later edits or deletes the remote artifact; it cannot accept new bytes.
  Eviction or restart re-verifies remotely and detects such changes. Mutable audit
  and plan artifacts are never trusted through this cache. The archive digest
  index is built once when a new write needs discovery.
- **Publication:** new format-5 reports render verdicts, findings, objections, assessments
  and agent-reported validation as Markdown. Canonical data links remain under Raw data;
  format-5 archives carry a view marker and audit readers parse only that terminal section.
  Rejected views use the controller diagnostic, never the rejected draft's own reason;
  formats 2–4 remain readable and replay byte-for-byte. Criterion labels are frozen when
  the report is queued. Excerpts shrink to fit without altering canonical evidence. Each artifact stays within the conservative 60,000-byte publication allowance. Lost
  responses are recovered by marker and exact body; artifacts are never edited.
  The response transport capacity remains 300,000 UTF-8 bytes, independent of
  checkpoint size. Valid reviews are not sent back for shortening to fit an audit.
- **Runner feedback:** lengthy selected feedback is delivered as digest-checked
  JSON files outside the candidate checkout and retained provider home. Prompts
  carry an index of every finding/objection, its identity and status, and the file
  location. Agents must read full evidence; previews are not substitutes. Only
  feedback already selected for that role is exported: fresh design reviewers
  receive no previous votes, and PR reviewers receive only their own findings.
  Files are temporary transport, not another durable evidence store. Call identity
  still uses the original feedback, so file projection cannot duplicate a paid call.
- **Checkpoint format:** serial audit writes use version 3; tasks that start parallel reviews
  use version 4 thereafter. Versions 2 and 3 remain readable
  and migrates on the next save without changing approvals or budgets. Unknown
  versions fail closed. Older engines reject version 3 instead of treating reference
  placeholders as state. After a v3 or v4 save, rollback requires an engine supporting that format;
  never restore an old audit snapshot to roll back, since that loses consumed calls
  and publication progress. Deploy the matching engine and runner workflow together.
- **Archive compatibility:** queued formats 1–3 retain their exact rendering and
  receipt-gated compaction. Referenced format-4 reports keep full evidence after
  publication. Oversized PR thread text is excerpted with a dashboard link; repair
  files retain the complete finding. Artifact discovery uses ten entries per page
  to stay within the existing read bound, but idle ticks do not scan the collection.


## Compatibility and future work

Queued comments keep their recorded format/body; a new renderer must not change recovery
identity. Existing welcome comments are not backfilled. Display code must not alter task
authorization, consume budget or trigger model calls.

An optional configurable summarizer remains future work: `agents.summarizer` is not a
supported role today. If added, it should summarize meaningful changes under a separate
bounded budget, fall back to the factual display and never delay progress or decide readiness.

Readiness failures requiring a maintainer (failed CI, unresolved threads, a closed PR or a
return to draft) use a WARNING with the corresponding action. Transient CI/mergeability
observations remain NOTE diagnostics. Operator-only failures use an explicit mapping of
controller reasons; model prose mentioning an operator does not change the action.

The merge-blocking explanation appears only while findings are open or withdrawn.
Operator and readiness reasons share constants with their producers.

Large plan, charter, template, requirements, candidate and conversation fields can be stored
by verified reference in format-7 checkpoints. Individual publication limits still apply;
the number of report parts and pending conversation events has no fixed ceiling. Full plan
data remains recoverable when its generated view is excerpted.
