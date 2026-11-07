# PR reviews

Status: implemented. Independent reviewers assess the exact published head. Their reports
become native GitHub `COMMENT` reviews and finding threads; they do not count as human
approvals. See [readiness](mvp-execution.md#reviews-handoff-and-readiness) for draft-to-ready
conditions and [comment format](comment-format.md) for rendering.

## Review identity and evidence

Every review records the reviewer profile, model/effort, head and input revision.
Each reviewer list requires distinct `(provider, model)` pairs; aliases or different
efforts for the same pair do not count as independent slots. Each reviewer sees its
own outstanding findings, not peer verdicts. It can inspect the pinned Git checkout and
run experiments; exploratory edits and test results are advisory and are not published.
There is no separate local-review stage.

PR context includes every base-to-head changed file, including accumulated repairs and
deletions. Metadata remains when diff previews shrink. Large selected feedback is provided
as verified runner files with a prompt index. A preview is not the complete evidence.
Manifest assessments are required for changed manifests identified by the controller.
Classification is advisory metadata, not a readiness condition.

The response includes a verdict, summary, new findings, dispositions, manifest assessments
and optional design-reopen evidence, as defined by `bundle/schemas/review.json` and
`policy.validate_pr_review`. New findings have severity, title, detail and a nullable
path/line range. Lines refer to the reviewed head and are 1-based.

- `approve` requires no new findings and no open disposition.
- `changes_requested` requires a new finding or an open disposition.
- `reopen` asks to revise the design; it does not authorize out-of-scope edits.

The controller assigns each finding a stable `F<n>` ID. Only its raising reviewer profile
can disposition it. The ledger's `provider` field holds that profile identifier (or the
legacy provider name), not an interchangeable vendor identity. Every open finding blocks
readiness regardless of severity.

## Finding lifecycle

| Status | Meaning |
| ---- | ---- |
| `open` | Requires action before readiness |
| `fixed` | Raising reviewer verified the correction |
| `withdrawn` | An open finding needs reassessment after design reopening |
| `obsolete` | Raising reviewer explains why it no longer applies under the new plan |

A reviewer supplies one disposition for each of its open/withdrawn findings. An open
finding may stay open or become fixed; a withdrawn one may also become obsolete.
Design approval alone does not retire withdrawn findings. Repair publishes a new head,
which needs current independent approvals; applying one review's dispositions does not
by itself invalidate the other review of the same input revision.

The ledger retains finding identity, source head, review sequence, detail digest and
publication/resolution receipts. Full evidence is kept in immutable report artifacts;
large reproducible fragments can reference their report rather than duplicate its text.
Historical excerpts never replace the canonical evidence used for recovery.

## Publication and placement

The report, ledger changes and outbox items are saved before any PR writes. The App
publishes the review, then any file-level comments and disposition replies. Marker,
author, exact body and bindings recover a lost response without posting twice.
Multiple or conflicting matches block publication.

The engine checks the PR head before and after listing changed files. Both must match
the reviewed head. Placement is based on the returned patch, not just hunk-header counts:

| Finding location | GitHub placement |
| ---- | ---- |
| Every cited line is a visible right-side line in one hunk | Inline review comment, with a range when supplied |
| File changed, but no usable line range or complete patch | File-level comment on that commit |
| No changed file attributable | Review body |

Each native review includes `commit_id` and `event: COMMENT`. Finding roots, dispositions
and withdrawals have distinct recovery markers. Full summaries and finding text may be
excerpted for GitHub's display capacity, with a link to the immutable report; the repair
agent still receives the complete selected feedback.

A non-transient publication failure retains the outbox item and falls back from inline
to file-level placement on retry. Repeated failure blocks after the configured publication
attempt bound (`MAX_ATTEMPTS` in `reviews.py`). Transient transport errors retain the item
without that fallback. Unknown service writes require operator settlement before another
write. Review publication must finish before readiness.

## Thread resolution

Concurro replies with evidence when a finding is fixed or obsolete, then may resolve its
owned thread once. Before sending, it records resolution as `intended` and saves.

On recovery, a resolved thread becomes `done`. An unresolved thread after an intended
attempt becomes `manual`: the previous attempt may have succeeded and a human reopened
it. A thread recorded as done is not automatically resolved again if a human reopens it.
An unresolved thread in either case requires human handling. Human threads are never resolved by Concurro; unresolved threads block
readiness. This conservative rule avoids undoing a human reopening after a lost response.

Design reopening withdraws open findings and posts one explanatory reply per published
thread. It leaves those threads unresolved until the raising reviewer retires or re-adopts
them under the new plan.

## Human feedback

PR-thread replies do **not** automatically enter the repair loop. Thread-state webhooks
wake observation, and unresolved threads can prevent readiness, but there is no admission
protocol that converts a human thread reply into a model instruction.

To request a bounded repair, a writer posts fresh feedback on the issue (up to 4,000 bytes)
and then `/concurro retry`. The latest new writer comment supplies the feedback. Without
new feedback, retry rechecks readiness. Changed requirements can reopen the design.
Direct commits to the controller's branch require reconciliation; suggested-change
application and automatic adoption of external commits are not supported.

## Service authorization

PR writes are allowed only for the recorded App-authored PR on the task's fork branch.
Replies and resolutions must target an App-authored root with the finding marker. The
controller additionally binds it to the ledger. The gateway admits only the exact supported
GraphQL mutations and treats incomplete or error-bearing mutation responses as unknown.

Token permissions and webhook subscriptions are maintained in
[the deployment guide](../../gateway/README.md#target-and-app-prerequisites). All GitHub
write credentials remain central; reviewer jobs hold only their own runner/inference grants.
