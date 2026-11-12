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
There is no separate local-review stage. Reviewers receive `published_pr_body`, including
human edits, and check its accuracy against the whole base-to-head diff. PR descriptions
are evidence, never agent instructions or authorization.

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
builds one pending GraphQL review per report, adding inline/file threads and disposition
replies before submitting it as `COMMENT`. Pending comments are not publicly visible.
Design withdrawals likewise use one review with a summary and their thread replies.
Previously queued REST items finish using their original protocol.

The outbox records `building`, `submitting` and `submitted` progress. Placements, reply
bodies and the final body are frozen before submission. Submission is not completion:
thread mapping, reply verification and resolution receipts must finish before removal.
An identified abandoned build is deleted before rebuilding. Unknown pending reviews,
conflicting identity/body and uncertain submissions stop for an operator. A submitting
review is never deleted or rebuilt. The service may resume a first send when its durable
journal contains no submit intent, or only definite failed attempts for that review ID.
The gateway checks this again under its write lock. An unknown, in-flight, successful or
operator-resolved attempt prevents another send, even if GitHub still says `PENDING`.
Uncertain same-ID resubmission remains disabled pending live validation; recovery can
recognize a confirmed submission without writing again. Clients without a durable write
journal cannot infer that a pending submission is safe to send.

The engine checks the PR head before and after listing changed files. Both must match
the reviewed head. Placement is based on the returned patch, not just hunk-header counts:

| Finding location | GitHub placement |
| ---- | ---- |
| Every cited line is a visible right-side line in one hunk | Inline review comment, with a range when supplied |
| File changed, but no usable line range or complete patch | File-level comment on that commit |
| No changed file attributable | Review body |

Each native review binds the reviewed commit and uses `event: COMMENT`. Finding roots, dispositions
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

To request a repair, a writer posts fresh feedback on the issue
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

## PR description and commit messages

New implementer results supply a structured `description`: a lead sentence, problem and
change bullets naming files/functions, and optional compatibility bullets. Brevity is
guidance; item counts and prose lengths have no fixed ceiling. The description covers the
whole approved-base-to-candidate diff and is
stored on that candidate. Testing comes only from `validation`, labelled agent-reported
and naming its candidate commit. Review focus comes from the implementer’s description,
initially based on the approved plan; legacy results without that field still use the plan. The footer
retains the implementation plan, dependency summaries and the correct issue closing link.

Concurro saves the intended body digest before creation or refresh. It PATCHes only when
the current body matches the last published or intended digest. A lost response can thus
recover ownership. This comparison is best-effort, not atomic with GitHub's PATCH. A human
edit permanently stops refresh for that task and adds a persistent dashboard notice;
reviewers still receive the actual published body. Legacy candidates without description
fields keep their original body and commit behavior.

The first new implementation commit uses the plan headline. Repair subjects and all
commit-body bullets describe that round's changes, followed by `Refs #N` (or the full issue
URL across repositories). Subjects preserve the model wording without a fixed length
convention. Set optional
`conventional_commits: true` in the target configuration to add the model-selected type
prefix; the default is false. Commit metadata is separate from the whole-PR description.

### Description-only repairs

The implementer can correct PR wording, including stale function names in review focus,
without changing the approved plan. An existing candidate with no workspace edits and a
valid description publishes only a body update at the same head. It preserves original
validation evidence, design approvals, findings and spent budgets; it creates no commit
and triggers no new CI run. Initial implementations still require code edits.
Description-only repairs use the same prospective review-context size check against the
full existing diff and retained validation, returning repair feedback before publication.

The controller saves the rendered body before publication and uses the existing body
ownership and lost-reply receipts. Human edits are not overwritten. After publication,
only reviewers requesting changes or owning unresolved findings are called again; other
approvals of the unchanged code remain recorded. Each finding stays unresolved until its
owner answers it. Failed corrections still use the existing repair/call allowances.

### Bundle rollout

The description-only contract changes the develop schema and implementer skill. Deploy
while idle, or explicitly resume active tasks to adopt the new instructions. For a mistaken
publication-wording reopen, use the description-only recovery option described in
[checkpoint resume](checkpoint-resume.md#recovering-a-mistaken-description-only-reopen).
Original approval bindings remain unchanged. Retained legacy implementation results without
review-focus fields remain readable and use the plan’s review focus.

### Repository PR templates

Opt in with `pr_template_sections` in the target configuration. Keys are exact ATX
heading lines from `.github/PULL_REQUEST_TEMPLATE.md`; values list structured fields
in their desired order. For example:

```json
{
  "pr_template_sections": {
    "## Why": ["problem"],
    "## What changed": ["changes", "compatibility"],
    "## Checks": ["testing"]
  }
}
```

Supported fields are `lead`, `problem`, `changes`, `compatibility`, `testing` and
`review_focus`. Each field may appear only once. Missing or duplicated mapped headings
and malformed mappings stop publication; headings are never guessed. Unmapped generated
fields use sibling sections at the shallowest template heading level, and an unmapped lead stays first. The footer remains
controller-owned.

The template is a regular UTF-8 file, at most 16,000 bytes, read from the approved base and
saved with that base and mapping before asking the implementer. Repairs reuse this snapshot.
Without a mapping, the default layout is used. A configured mapping requires the template
at that exact path; a missing file blocks the task before implementation. Template sections and ordinary text are preserved; HTML comments
are omitted and other HTML is escaped. Existing checked boxes are rendered unchecked.
There is no automatic checklist attestation: a maintainer may check items with evidence,
which then counts as a human body edit and stops automatic refresh.

### Rendering and recovery corrections

Format-4 finding titles and format-5 report views preserve complete inline code spans;
whitespace-only spans are escaped literally. Pipes inside code are escaped only in tables.
Earlier posted formats retain their original renderers.

PR description fields, review focus, PR titles and commit prose neutralize issue-reference tokens
(`#123`, `owner/repo#123`, `GH-123`, issue/PR URLs) and mentions with zero-width separators.
Ordinary words remain unchanged. Markdown descriptions preserve complete code spans;
plain-text PR titles and commit messages neutralize references inside backticks too. Controller-generated issue
links and `Closes`/`Refs` lines remain active. Validation is labelled agent-reported and
rendered as inert prose; it cannot create headings or checked template task-list items.
Cross-repository commit references use the issue's full URL.

Before replacing a legacy plan or candidate, Concurro adopts the old description only if
the live PR body exactly matches its legacy renderer. That digest survives invalidation,
so a later structured candidate can refresh it. A mismatched body remains human-owned.
Malformed implementation descriptions receive bounded model repair feedback. New response
schemas require nonempty description and commit objects, including on design reopens
(where those fields are not published).

New reviews use rendering format 5 with a conservative 60,000 UTF-8 byte body capacity.
An oversized finding table links back to the full report archive. Formats 1–4 retain their
original rendering so an interrupted publication can reconcile already-published text.
