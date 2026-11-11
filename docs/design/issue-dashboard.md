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

The mutable audit is not a report archive. Artifact history defaults to 30 versions per
page; live probes exposed only the latest 100 revisions. Normal audit and bound-plan reads
request one version, with older pages fetched only for provenance. Reports therefore need
independent immutable storage. New audit JSON is hidden from rendered HTML inside a
versioned comment; it remains public through the raw API. Fenced legacy audits remain readable.

The current audit carries a confirmed dashboard pointer and at most one pending pointer.
Readback confirms publication; the next ordinary checkpoint stores its receipt. Either
witness can verify a pointer whose version has expired. Visible dashboard content, excluding
its verified recovery marker, determines freshness; receipt-only saves do not refresh it.
Unbound legacy plans whose origin has expired are preserved. A durable replacement intent
allows a new view from the audit to be recovered after a lost reply and bound without new
model calls. Missing audit history without a witness remains a verification failure.

- **Canonical report data:** new design and PR reviews store their full JSON in
  immutable, content-addressed issue artifacts. The retained response, accepted
  vote, review history and rounds table share the same report artifact. Its bounded readable
  view describes submitted model output; canonical JSON is hidden in an HTML comment. Controller
  annotations and advisory ratings live in per-event receipt snapshots, without wrapper artifacts.
  Existing format-3 archives are reused when their complete report matches.
- **Audit references:** statuses and small collections stay in the audit;
  bulky reports, history collections and report indexes are referenced by digest and artifact numbers. Derived finding text
  uses a path into its source report rather than a second stored copy. References
  are hydrated before policy checks. Known audit, plan and report identities are
  read directly, including small inline reports with canonical publication receipts. Receipt
  identities are verified before reuse; invalid references do not fall back to discovery.
  Marker discovery remains necessary when no canonical reference survived.
  Completed responses remain available until their results are consumed. Checkpoint
  pressure never discards a response or turns it into an uncertain model call.
  Current, unarchived and pending-publication ratings remain intact; only inline archived
  historical ratings may be shortened or dropped from the checkpoint. Detached timing,
  ratings, snapshots and assessment links are not deleted to relieve audit pressure. The full
  archived report remains unchanged. New publication snapshots and event timing are never trimmed.
  Historical trimming is covered through the 48-call budget in regression tests.
  An unrelated duplicate cannot replace an already bound artifact. New immutable parts accept
  byte-identical, unedited App copies, choosing the lowest number on discovery and retaining
  pinned IDs. Conflicting copies, missing data and invalid provenance or digests stop recovery.
  Legacy duplicate rules are unchanged.
- **History blocks:** oversized lists store completed eight-entry blocks as immutable data.
  Their ordered directory and unfinished tail remain in the mutable audit. Report bodies
  within the tail still use canonical references. Appends create a history artifact only
  when a block fills; changing older metadata replaces its affected block. Eight is storage
  granularity, not a history limit. The full report index is retained; only its dashboard view
  shortens. If packed metadata exceeds the audit's 60,000-byte target, a verified immutable
  envelope holds it behind a compact audit pointer. Both layouts use checkpoint format 7.
  Existing whole-list and block-manifest artifacts remain reusable, including interrupted writes.
- **Cache:** the service shares an LRU holding up to 32 MiB of encoded, verified report content across ticks,
  scoped by repository, issue, author, digest and artifact numbers. Returned values
  are copies. A warm cache uses the originally verified immutable bytes, even if
  someone later edits or deletes the remote artifact; it cannot accept new bytes.
  Eviction or restart re-verifies remotely and detects such changes. Mutable audit
  and plan artifacts are never trusted through this cache. The archive digest
  index is built once when a new write needs discovery.
- **Publication:** new format-6 events reuse the canonical artifact instead of creating a
  separate readable archive. Acceptance/rejection, fixed diagnostics, attribution and source
  context remain in the event index and receipts. Stable event and run identities keep repeated
  content distinct in the dashboard and audit tool. Formats 3–5 retain their original writers
  and readers, including queued recovery; no existing artifact is rewritten.
  New version-2 data parts hide JSON in comments, escaping `>` as `\u003e`. Every part records
  the renderer and complete multipart layout. Continuations are written first; the entry links
  their verified IDs. Readers verify stored view text against its digest without rendering.
  Renderer 1 freezes its own formatting helpers for interrupted writes; new presentation needs
  a new renderer version. Any surviving part fixes recovery boundaries despite changed packing
  settings. New parts fit the shared 16,000,000-byte artifact response budget and GitHub's
  independent 16,777,215-byte body ceiling. Packing counts JSON escaping, descriptor/link
  overhead and an allowance for rendered HTML. A frozen compact renderer (version 2) replaces
  the readable view before splitting canonical data; no evidence is shortened. The original
  renderer remains available for interrupted writes. A lower response budget refuses an
  incompatible frozen layout rather than repacking it. Legacy queued writers retain their
  old allowances. Comments and full readable views keep separate 60,000-byte allowances.
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
- **Checkpoint format:** new audits use format 7 for comment encoding, dashboard witnesses
  and plan-replacement intents, alongside detached histories, overflow metadata and shared-report
  annotation receipts. New shared parts require this reader even though the checkpoint stays at 7.
  Formats 2–6 remain readable and migrate on save without changing approvals or budgets.
  Rollback requires this phase's reader, not merely an earlier format-7 build. Never restore
  an old audit snapshot to roll back: that loses spent calls and publication progress.
- **Archive compatibility:** queued formats 1–3 retain their exact rendering and
  receipt-gated compaction. Referenced format-4 reports keep full evidence after
  publication. Oversized PR thread text is excerpted with a dashboard link; repair
  files retain the complete finding. Artifact discovery uses one entry per page
  so large bodies fit the same response budget. Legacy whole-list detection can still scan the
  collection when no representation is known. Verified history directories and canonical
  report receipts avoid collection scans on ordinary cold saves and republication.


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
