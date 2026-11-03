# PR reviews

Status: accepted (revision 5, reviewed); rollout steps 1–3 implemented. Publishes
the engine's independent PR reviews as native GitHub reviews, with findings as threads that the engine replies to and
resolves once a re-review verifies the fix, and that writers can answer to drive
the next repair round.

## Problem

PR reviews are published as issue comments, like every other conversation event.
The issue keeps a good audit trail, but a human reviewing the PR gets no native
review UI: findings are not anchored to code, there are no threads to reply in,
and nothing shows which commit fixed which finding.

## Decisions

| Question | Decision |
| ---- | ---- |
| Review type | Always `COMMENT`, like Copilot code review. A PR's author cannot approve or request changes on it, and a bot approval must not count toward a ruleset's required approval |
| Author | The controller App, with the provider and model named in each review's header, until per-provider Apps exist ([service design](service.md), step 7) |
| Threads | Every finding attributable to a file gets a thread: inline (one line or a range) when its lines are in the diff, otherwise file-level. Only findings tied to no file of the PR stay in the review body |
| Who resolves engine threads | The engine, at most once per thread, when the provider that raised the finding marks it fixed on a newer head, or obsolete under a new plan. It never resolves a thread that was reopened, or any human thread |
| Reopening a thread | Blocks the gates; it alone triggers no edits and no model calls |
| Writer replies | A current writer's reply in an engine thread is admitted durably, reopens a fixed finding, and starts one bounded repair round |
| Source of truth | The checkpoint's finding ledger. Reviews, comments, replies and resolutions are projections published through an outbox and reconstructed by marker |
| Issue conversation | Keeps one short event per review, linking to it on the PR |
| Local review | Unchanged: it runs before a PR exists, keeps its current contract and stays on the issue |
| Suggested changes | Not supported. A human applying one would commit to the engine's branch, which the controller treats as tampering |

## Finding ledger

The checkpoint holds every PR review finding once, in `state["findings"]`:

| Field | Meaning |
| ---- | ---- |
| `id` | `F<n>`, from a per-task counter that never resets, assigned when the finding is first reported |
| `provider` | The reviewer that raised it; only that provider dispositions it |
| `path`, `start_line`, `line`, `detail` | As reported, on `head` |
| `head`, `review` | The head and the review sequence that raised it |
| `status` | `open`, `fixed`, `withdrawn` (the design was reopened while it was open) or `obsolete` (retired by its provider under a new plan) |
| `thread`, `comment` | The thread ID and the App comment holding the full text once published, or `body` and the review's ID |
| `text_digest` | Digest of the full `detail`, so compacted text can be re-read from `comment` and verified |
| `resolution` | `none`, `intended` (recorded before sending), `done`, or `manual` (never auto-resolved again: reopened, or an ambiguous attempt) |
| `replies` | Every reply version ever admitted: comment ID, body digest, its text, author, permission decision, the finding status it replaced, and the round that took it, if any |

The task also holds `repair_requested` (an admitted reply awaits a repair round)
and an input revision. The input revision changes only when review input
changes: a reply is admitted or dropped, the design is reopened, or a new head is
published. Applying a report's dispositions and recording publication or
resolution receipts do not change it.

Compaction never drops these fields. Once every projection of a finding is
published, only `detail` and disposition evidence are cut to excerpts; the full
text stays in the App's comment and is checked against `text_digest` when read
back. A reply's text is kept while its finding is open or withdrawn, until the
raising provider's disposition answering it has been saved. This follows the existing conversation compaction, which compacts only
published narrative.

### Withdrawal and retirement

When the design is reopened, every open finding becomes `withdrawn`. The engine
replies once in its thread that the design changed and leaves it unresolved.

At its first review of the new plan, the provider that raised a withdrawn finding
receives it with its replies and answers it like any other:

- `fixed`: the engine replies with the evidence and resolves the thread once, as
  for any fix;
- `obsolete`, with evidence that the new plan no longer makes it apply: the
  engine replies and resolves the thread once;
- `open`: the finding is re-adopted as open under the new plan.

Approving the new plan does not clear a withdrawn finding; only its provider's
answer does.

## Review contract

PR review gets its own schema and validator, `policy.validate_pr_review`. Local
review keeps `validate_review`, and its stored evidence stays valid.

The reviewer receives its own open and withdrawn findings, with their admitted
writer replies, and returns:

- `findings`: new findings only, each `{path, start_line, line, detail}`. `path` is
  null for a finding about the change as a whole; `start_line` and `line` are
  optional, on the reviewed head, 1-based, with `start_line ≤ line`.
- `dispositions`: exactly one `{id, status, evidence}` for each of its open and
  withdrawn findings. `status` is `fixed` or `open`, or for a withdrawn finding
  also `obsolete`.
- `verdict`: `approve` requires no new findings and no `open` disposition;
  `changes_requested` requires a new finding or an `open` disposition; `reopen`
  is unchanged, and is published as a review stating the design issue.

Each report records the input revision it answered. A report from an older
input revision never counts toward the gates; a report's own dispositions never
make it, or another provider's report, stale.

The `concurro-review-pr` skill asks reviewers to anchor a finding to the lines it
concerns, to name its file whenever it can realistically be attributed to one,
and to give evidence naming the changed lines that fix a finding.

## Publication

The validated report, its ledger changes and its outbox items are saved in one
checkpoint write. Publication runs from the outbox after the save, like the issue
conversation; nothing is published that the checkpoint does not record.

### Placement

Placement uses `pulls/<pr>/files`, which has no head parameter. The engine reads
the PR's head before and after listing the files and requires both to equal the
reviewed head; otherwise the review is stale and is not published.

| Finding | Placement |
| ---- | ---- |
| Every line from `start_line` to `line` is a right-side line shown in one hunk of the file's `patch` | Inline comment in the review: `{path, line, side: RIGHT}`, plus `start_line` and `start_side: RIGHT` for a range |
| Its file is one of the PR's files, but it has no line, its lines fail the check above, or the file has no or a truncated `patch` | File-level comment: `POST pulls/<pr>/comments` with `{commit_id, path, subject_type: "file", body}`, after the review |
| No path, or a path the PR does not change | The review body |

A line counts only if the patch text shows it on the right side, as a context or
added line inside a hunk; the hunk header's advertised range is not enough.

### Writes

1. The review: `POST pulls/<pr>/reviews` with `{commit_id, event: "COMMENT", body,
   comments}`. The body carries `<!-- concurro:review:v1:<task>:<sequence> -->`,
   the provider and model, the verdict, the summary, the dispositions, and any
   body-placed findings.
2. One file-level comment per file-placed finding.
3. One reply per disposition, in that finding's thread:
   `POST pulls/<pr>/comments/<root-comment-id>/replies`: `Fixed in <head>:
   <evidence>`, `Still open on <head>: <evidence>`, or `Obsolete under plan
   <plan>: <evidence>`, marked with
   `<!-- concurro:disposition:v1:<id>:<sequence> -->`. Dispositions of
   body-placed findings appear in the review body only.
4. For each `fixed` or `obsolete` finding whose `resolution` is `none`: check
   that it is still `fixed` or `obsolete` with no admitted reply newer than its
   disposition, record `intended`, save, then send
   `resolveReviewThread` for its thread.

Every finding comment carries `<!-- concurro:finding:v1:<id> -->`.

### Failures and completion

- A failed write stays in the outbox with a fixed diagnostic and is retried on
  the next tick; it never completes by being skipped.
- A rejected review (HTTP 422) is retried once with its inline findings moved to
  file-level comments. A 422 can also be spam prevention, so after three failed
  attempts of any item the task is blocked with a diagnostic.
- Gates gain three reasons: `PR review publication incomplete` while the outbox
  holds PR items; `open review findings` while the ledger holds an `open`
  finding or `repair_requested` is set; and `withdrawn findings awaiting
  retirement`, naming their threads, while any finding is `withdrawn`. The
  existing unresolved-thread gate keeps covering human threads and `manual`
  engine threads.

## Threads

### Resolution, at most once

A thread is resolved by at most one attempt. On a later tick, a finding whose
`resolution` is `intended`:

- with its thread resolved, becomes `done`;
- with its thread unresolved, becomes `manual`. An unresolved thread does not
  prove the attempt failed, since a writer may have reopened it after it
  succeeded. The thread stays unresolved, blocks the gates, and is left to a
  human.

A recorded `done` thread found unresolved again becomes `manual` too. Under the
service, an attempt whose gateway outcome is unknown keeps the existing operator
resolution requirement.

### Writer replies

A reply in an engine thread is a candidate only if it is not by a Concurro
identity and its exact version (comment ID and body digest) was never admitted
before; editing a reply back to an admitted version is not new input. The issue
author's exception for issue replies does not apply: these replies direct code
changes.

**Admission** happens at the start of a tick, after commands. For each
candidate, the engine checks that its author is a current writer through the
collaborator permission API, and that the reply fits:

- A reply over 8 KB is refused: the engine posts one short notice in its thread
  asking for a shorter instruction, marked by the reply version so it is never
  posted twice. No status changes and no round is spent; an edited, shorter
  version is a new candidate.
- A reply that would push the checkpoint past its size limit (with the usual
  margin) is not admitted either: the engine posts a notice and blocks the task
  with a diagnostic, since writer input must never be silently dropped.

Otherwise it saves in one checkpoint write: the reply
version, its text and author, the permission decision, the finding change, the
new input revision, and `repair_requested`. A reply on a `fixed` or `obsolete`
finding reopens it: it becomes `open`, its previous status is recorded with the
reply, its resolution history is kept, and its resolution becomes `manual` if it
was `intended` or `done`. Its provider must answer it again. A reply on an
`open` or `withdrawn` finding changes no status; it reaches that finding's
provider with its next disposition. A crash before that write
admits nothing; after it, nothing is admitted twice.

**Scheduling.** At the next safe point, the start of `review` or `gates`, a set
`repair_requested` starts a repair round. One checkpoint write increments
`round` (within `max_fix_rounds`, or the task is blocked), freezes the round's
batch (every admitted reply not yet taken), records the round on each reply,
clears `repair_requested`, discards the cached reviews for the current head, and
moves to `implement`. A writer's reopen is not sent back to the reviewer first:
the reviewer cannot overrule the writer, and the next head is reviewed anyway.

**The repair round** gives the implementer the open findings and the frozen
batch's stored text, alongside the prior reports it already receives; it never
re-reads the replies from GitHub. A reply admitted after
the batch was frozen waits for the next round, so each reply causes at most one
round.

**Before the batch is frozen**, each reply in it is checked again: a reply
deleted since admission is dropped, and so is a reply whose author is no longer
a writer. A finding reopened only by dropped replies returns to the status
recorded with them (`fixed` or `obsolete`), keeping a `manual` resolution, so
its thread is left to a human rather than resolved again. If the whole batch is
dropped, no round starts and `repair_requested` is cleared. If any cached report
now answers an older input revision, the task returns to `review` and those
reports are discarded, so the providers review the same head again; no repair
round is spent. A reply already taken by a round stands even if it is later
deleted or its author's access is revoked: the resulting head is reviewed
independently.

Toggling a thread's resolved state is never input on its own.

## Reconstruction

Every write is found again on the next tick, after any outcome. A match must be
authored by the controller App, carry the expected marker and bindings
(`commit_id`, path, thread), and match the rendered content. Two matches, or a
match with other content, is a conflict that blocks the task, as in the issue
conversation publisher.

| Write | Reconstruction |
| ---- | ---- |
| `POST pulls/<pr>/reviews` | The review with the review marker and `commit_id` |
| `POST pulls/<pr>/comments` (file-level) | The comment on that path with the finding marker |
| `POST pulls/<pr>/comments/<id>/replies` | The reply in that thread with the disposition marker, or for a refusal notice `<!-- concurro:notice:v1:<comment-id>:<digest> -->` |
| `resolveReviewThread` | The ledger's resolution rules above; never retried |

Threads, their first comments and their replies come from a bounded GraphQL
`reviewThreads` query, extending the one the gates already make.

## Service integration

- **Settlement.** `settle` gains a rule per GraphQL mutation. A
  `resolveReviewThread` is `done` only with no `errors` and
  `data.resolveReviewThread.thread.isResolved` true; any other GraphQL response
  stays `unknown`.
- **PR binding.** The gateway admits PR writes only for the task's own PR,
  verified on first use by reading the PR (its head branch is the task's branch,
  its base the configured base) and cached like the issue node. It does not
  depend on the registry's PR link, which is recorded only after a tick
  completes.
- **Finding binding.** Replies and resolutions must target a thread whose first
  comment is authored by the App and carries a finding marker; the engine further
  requires that finding to be in its ledger. Each GraphQL mutation keeps
  exact-document validation.
- **Observation.** Snapshots add, for each engine thread, its ID, `isResolved`,
  and writer-eligible replies by comment ID and body digest. Ingress accepts
  `pull_request_review_thread` and `pull_request_review_comment` through the
  recorded PR, dropping the App's own events. The engine's own resolutions change
  `isResolved`, so they can cost one read-only follow-up tick, never a review
  turn.

## Changes

| Area | Change |
| ---- | ---- |
| `bundle/schemas/review.json` (the PR review stage), skill, `policy.py` | PR-specific contract and `validate_pr_review`; local review keeps `local_review.json` and `validate_review` |
| `controller.py` | Ledger and revisions, finding IDs, dispositions, reply admission and repair scheduling, withdrawal and retirement, the three new gate reasons |
| New `engine/reviews.py` | Patch parsing and placement, rendering, outbox items, marker recovery, resolution rules |
| `conversation.py` | Short pointer event per review; compaction keeps reports until their PR items publish |
| `github.py` | `review_threads()` with first comments and replies; `resolve_thread()` with the exact mutation |
| Service gateway | Allow-list entries, PR and finding binding, per-mutation settlement |
| Service reconciler and ingress | Thread and reply observation; the two webhook events |

Token permissions are unchanged. Reviews, comments and replies need
`pull_requests: write`. `resolveReviewThread` is reported to also need
`contents: write`; the write token holds both. The App must subscribe to
`pull_request_review_thread` and `pull_request_review_comment`.

## Rollout

1. Contract (implemented): PR schema and validator, the ledger with IDs,
   dispositions, the input revision, withdrawal and retirement, the open and
   withdrawn gate reasons, and the repair context, with publication still on the
   issue. The ledger's `thread`, `comment`, `resolution` and `replies` fields
   arrive with the steps that need them.
2. Reviews on the PR (implemented): placement, the review, file-level comments,
   failures and completion gates, and recovery, on both the Actions and service
   paths. The issue keeps a short pointer event per review. The service gateway
   admits only `COMMENT` reviews, file-level comments and replies on the task's
   own PR, and the reconciler ignores the App's own reviews.
3. Threads (implemented): disposition and withdrawal replies, at-most-once
   resolution, and the gateway's binding of replies and resolutions to the App's
   own finding threads, with per-mutation settlement. First confirm on the fork
   that the App's write token can resolve a thread it opened on its own PR.
4. Writer replies: intake, reopening, repair transitions and service observation.
5. Live run on the fork with an issue whose first review requests a change, and
   a writer reply that reopens a fixed finding.

## Tests

| Scenario | Expectation |
| ---- | ---- |
| Lines all shown on the right side in one hunk; a line beyond the shown lines; no line; no or truncated patch; no path or a path outside the PR | Inline; file-level; file-level; file-level; body |
| Line range | Inline with `start_line` and `start_side: RIGHT` |
| Head moves while files are listed | Not published as stale |
| Crash between the report's validation and its publication | Report and outbox saved together; published on the next tick |
| Review rejected with 422 | Retried once with file-level comments; after three failures, blocked with a diagnostic |
| Lost review, comment or reply response | Found by marker and bindings; nothing posted twice; a conflicting match blocks |
| Opening comment or thread missing from a read | Review kept pending and retried; never recorded as unthreaded |
| Opening comment edited under its marker | Blocked before any reply or resolution |
| Resolution response lost, thread later reopened | `manual`; never resolved again |
| Re-review marks a finding fixed, or open | Reply with the head and evidence; resolved once, or left open |
| Design reopened with open findings | Findings withdrawn; one reply each; threads left unresolved |
| Withdrawal, then unanimous approval of the new plan | Gates blocked naming the withdrawn threads until each provider answers fixed, obsolete or open; fixed and obsolete threads resolved once |
| Writer reply on a fixed finding at `gates` | Admitted; finding reopened; one repair round within budget; blocked beyond it |
| Writer reply after its provider approved, while another review is pending | Admitted; at the next safe point, cached reviews discarded and a repair round starts; no stale approval reaches the gates |
| Writer reply after its finding was compacted | Provider, resolution history and receipts intact; reopened and repaired like any other |
| Crash after admitting a reply; crash after starting a round | Admitted once; the round and budget increment saved with the batch, never spent twice |
| Reply edited, edited back, or deleted before its round; author's access revoked before or after the round takes it | New input once, not again; dropped; dropped before, stands after |
| Pending resolution of a finding reopened since its disposition | Not sent |
| Both providers fix their findings on one head | Their dispositions do not make each other's reports stale; gates reached without another review |
| Crash after a batch is frozen, then the reply is edited or deleted | The implementer receives the stored, accepted text |
| Restart after the repaired head is published but before its review, with the reply deleted | The reviewer receives the stored text |
| Whole batch dropped at `review` or at `gates` after both providers approved | Stale reports discarded; same head reviewed again; no round spent |
| Reply over 8 KB; reply that would overflow the checkpoint | One notice per version, nothing else changes; not admitted, notice posted, task blocked |
| Overflow capacity check | Counts the notice, the refusal record and the blocked-state bookkeeping within the reserved margin; the overflow response itself always fits |
| Crash after saving a refusal notice's intent, then its response is lost | Published exactly once; the overflow block kept; the instruction never admitted |
| Writer reply on an obsolete finding | Re-adopted as open with its history; its provider answers it again; if the reply is dropped before freezing, obsolete again |
| Non-writer or App reply; a thread toggled without a reply | No input; no model call |
| Gates with publication pending, an open finding, a pending repair or a withdrawn finding | Blocked with the matching reason |
| Local review evidence from before this change | Still valid |
| Gateway: a write for another PR, or a reply or resolution for a thread without the App's finding marker | Refused before admission |
| Successful `resolveReviewThread` under the gateway | Settled `done`, not `unknown` |

## Open questions

- Whether writer replies in human-authored threads should also feed repair
  rounds, or stay for humans only.
- Suggested changes, once the engine can safely adopt a writer's commit on its
  branch.
