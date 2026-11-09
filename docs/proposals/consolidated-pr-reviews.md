# Proposal: consolidated PR reviews

**Status: proposal, not implemented.** Revised after two Codex reviews against the code.

Post **one** GitHub review per review round instead of one per reviewer model, with each
finding attributed to its reviewer. Later phases add duplicate grouping and contradiction
handling. Internally, each reviewer stays independent.

## Today

- **Review flow:**
  - Every profile in `pr_reviewers` (at least 2, each a distinct provider/model, with no
    upper limit) reviews the current head **serially** (`Controller.review`).
  - `record_pr_review` persists each report separately in `state["reviews"][provider]`,
    applies its findings (`reviews.apply`) and queues one outbox item per reviewer
    (`queue_review`).
  - A restart can find every report already present, with no reviewer call left to make.
- **Publication:**
  - Each `review` item becomes its own GitHub review, with its own issue conversation
    event and its own report archive (`conversation`, `report_store`).
  - `Publisher.run` treats every outbox item that isn't a `review` as a withdrawal.
- **Findings and threads:**
  - Findings get task-wide IDs (`F<n>`), owned by the raising profile. Only the owner can
    disposition them in later rounds. `obsolete` is allowed only for withdrawn findings.
  - **Placement:** a finding is inline, file-level, or in the **review body**
    (`thread="body"`), so not every finding has a thread.
  - **Verification:** threads are verified by a finding-specific opening body and digest
    (`finding_body`, `opening_matches`, `map_threads`). Resolution is receipted per
    finding (`resolve`), and there is no way to unresolve a thread.
- **The implementer** gets all reports and all open findings in one turn per round.
  `develop.json` has no per-finding responses.
- **The gate requires unanimity** (`Controller.review`, `readiness.review_reasons`).
- **Size:**
  - `REVIEW_BODY_LIMIT` is 60,000 **characters** for the review body; format-3 finding
    excerpts use a **byte** allowance.
  - All inline findings go in one create-review request.
- **Admission** (`service/admission.py`, `MutationGateway.ours`/`bound`) allows only
  `COMMENT` reviews, file-subject comments and replies on App-authored threads whose
  first comment starts with `FINDING_MARKER`.
- **Nothing merges or compares findings across reviewers.**

### What duplication actually costs

Two reviewers **don't** cause an extra round; the round already waits for both. The costs
are:

- the same defect becomes two findings, two threads and two later dispositions;
- the implementer reads both copies and may address the issue twice;
- contradictory recommendations go undetected, which can loop the task until
  `max_fix_rounds` runs out;
- a human sees several bot reviews with overlapping or conflicting comments.

## Goals

- One GitHub review per round, listing each reviewer's verdict and every finding with its
  attribution.
- Later: one thread per group of duplicates, and visible, recoverable contradictions.
- Keep per-reviewer independence, ownership, archives, the unanimity gate and crash
  safety.

## Non-goals

- Changing how reviewers review: what they see, their schemas, or their verdicts.
- Letting a consolidator drop or rewrite a reviewer's finding, or change a verdict or
  disposition.
- Parallel PR review dispatch, which is separate work.

## Design

### Phase 1: one review per round

This phase is deterministic and needs no model.

- **Finalization is a separate, replayable step.** Once every assigned reviewer has an
  accepted report for the head, a finalizer runs, **including on restart when no reviewer
  call happens**.
  - **The round's identity:** `(head, input_revision, round)`. It stays the same while
    the round's reports arrive.
  - **What it persists:** the segment's immutable inputs (references to the reports) and
    a publication intent, **before** any GitHub write or stage advance.
  - **What it queues:** one `{kind: "round", ...}` outbox item per segment (normally one
    per round).
- **Publication segments.** A round is published in one or more segments, each
  identified by `(round identity, segment index)`. A segment's **report membership** (the
  report sequences it covers) is sealed when it is created and never changes.
  - **A `reopen`, escalation or stop** that interrupts a round finalizes the reports
    accepted so far as a segment, so their findings aren't lost.
  - **Resumed work:** if the same logical round later continues, only the reviewers
    without an accepted report are called; accepted reports are never requested again.
    Their reports form the **next segment** of that round, not a new round and not an
    amendment.
  - **Notifications:** each segment gets its own issue notification, which names the
    round and the segment.
- **Terminal PRs:** closure saves with `outboxes=False` and returns before any outbox is
  flushed (`Controller._reconcile_external`), and that stays as it is. Closure never
  waits on PR publication, and **no new PR write starts once closure is observed**. A
  write already in flight when a human closes the PR can't be prevented.
  - **Two separate facts per segment:**
    - **archival:** complete once the segment's reports are in the immutable archive and
      linked from the issue summary;
    - **publication knowledge:** `confirmed` (every receipt), `partial` (some receipts,
      kept as they are), `unconfirmed` (an uncertain create-review attempt with no
      receipt) or `none` (known not published: no create-review attempt ever started,
      or every attempt was definitively rejected, with no applied or unknown attempt).
      The record keeps the evidence that distinguishes those two cases.

    The summary reports this as it is, and never claims nothing was published when
    publication is `unconfirmed`.
  - **Terminal finalization is scheduled.** Closure persists a pending terminal summary,
    on both the closed-unmerged and the observed-merged paths. Today `Controller.outcome`
    returns `terminal` for those stages, and `Registry.active` stops polling terminal
    tasks, so no later tick is guaranteed. While a summary is pending, the task keeps
    its terminal stage but gets an explicit retry schedule (a `wait-until` outcome) until
    the summary has a receipt; only then is it `terminal`. This changes scheduling after
    closure, not what closure means. A summary failure never restarts model work and
    never replaces the terminal stage with a block.
- **One publication mode per round.** Each round records its mode, `per_reviewer` or
  `round`.
  - **Switching:** a task switches only at a clean boundary, meaning no unfinished or
    partially published round.
  - **Legacy items:** format-3 `review` items and finding formats stay publishable and
    dispositionable.
  - **Old readers:** durable round records require a **new checkpoint wire version**.
    `Store.validate_state` rejects unknown versions, but not unknown flags, so a
    capability flag alone wouldn't stop an older service. Config is pinned per task, so
    a config flag isn't a rollout mechanism either.
- **Publisher support:** `Publisher.run` and `report_for` gain explicit support for
  `round` items, which render from several report sequences. A non-`review` kind no longer
  implies a withdrawal.
- **Rendering:** the body has a verdict header per reviewer, then every finding, each with
  a **Raised by** column. **Existing per-finding threads are kept as they are**, so each
  finding still gets its own inline or file comment, or body placement.
- **Thread links:** thread IDs don't exist when the review body is posted, so the body
  never links threads. Links appear in later replies, or not at all. The body must
  re-render byte-identically during recovery.
- **Size:**
  - Fixed, deterministic body and excerpt bounds, applied consistently to characters
    and bytes.
  - Excerpts, `Droppable` and archive links for long content.
  - Don't assume any numeric inline-comment cap from GitHub.
- **Versioned render attempts.** Each publication attempt for a segment has its own
  immutable rendering, including every finding's placement, persisted before its
  create-review request. Recovery compares an existing review body exactly
  (`Publisher.review`).
- **A typed publication outcome per create-review attempt.** A create-review request
  isn't idempotent, so resending an identical body can create a duplicate review. Each
  attempt persists one of three outcomes:
  - **Definitively rejected:** a validation rejection of the **create-review request
    itself**. Only this permits a replacement rendering, which is how the overflow
    fallback re-renders with file or body placement. A rejected later reply or
    file-comment request says nothing about the review.
  - **Applied:** reconstruct the receipt from the observed review.
  - **Unknown** (a lost response, a timeout): observe and reconcile. No further create
    request is sent until non-publication is established, and a lookup that finds no
    matching review does not by itself establish it.
- **The outcome must reach `Publisher`.** `service/settlement.py:settle` already produces
  `Rejected(status=...)` for definitive REST failures, and the gateway stops on unknown
  writes (`Unresolved`). But `Publisher.run` catches generic exceptions and switches
  placement, and the standalone `GitHub.api` transport raises generic errors. Both need a
  typed classification contract, or must treat every failure as unknown.
- **Archives are unchanged:** each reviewer report keeps its own archive and its
  conversation and dashboard references. The issue gets one notification per segment
  (normally one per round) that links them. Pending round inputs are protected from
  compaction.

### Phase 2: candidate duplicates (no merging)

- Findings from different reviewers on the same path with overlapping or adjacent lines
  are **candidates** only. They are shown together ("possibly related") but stay
  separate findings and threads. Adjacent lines can hold unrelated defects, so proximity
  alone never merges anything.
- Phase 2 data shows how often semantic grouping would help.

### Phase 3: clusters (consolidator model)

- **A new role, `pr_consolidator`.** This needs coordinated changes to:
  - `model_config.AgentAssignments`;
  - the exchange `session_binding` roles;
  - a bundle schema and role;
  - charging, through `charge_call`.
- **Output:** a partition of the round's finding IDs into clusters. Each cluster has a
  merged title, its members and an optional `conflict`.
  - **Validation** proves only that every ID was retained, not that the grouping is right.
  - **Visibility:** every member stays visible inside its cluster as a bounded excerpt
    with a link to its full text in the immutable archive.
  - **Persistence:** the output is stored before queuing.
  - **Fallback:** if the consolidator is unavailable, fails validation or exceeds the
    budget, every finding is its own cluster. Accepted reviews are always published.
- **The cluster publication record:** a separate record with its member mapping, an
  immutable opening snapshot and its own receipts. The canonical marker is the
  **numerically lowest** member ID, which satisfies the gateway's `FINDING_MARKER` check.
  - **Frozen at first publication:** membership is fixed when the cluster is first
    published. A later duplicate gets its own thread, plus a receipted "related to"
    reply. Clusters are never merged or split after publication.
  - **Resolution:** a cluster thread resolves only when **every** member is closed (fixed,
    or obsolete if withdrawn), with no member open or withdrawn. The canonical owner
    alone never resolves it.
  - **Body-only clusters:** clusters without a code location stay in the body, with no
    thread.
- **Ownership is unchanged:** each reviewer dispositions its own member IDs.

### Phase 4: contradictions

- **This is a workflow change, not presentation.** A cluster with `conflict` changes how
  the task proceeds.
- **The implementer's choice:** the implementer response gains optional per-cluster
  `responses` for conflicting clusters (a `develop.json` change), following the planner's
  per-objection responses. The implementer must choose a position, or a third way, and
  justify it.
- **Escalation:** if the conflict is still open after one further round, the task
  escalates to a human.
- **A new resolution command:** this needs a new writer command, because no existing
  command resolves a PR conflict while preserving ownership. The human's decision guides
  the next repair, and **never waives** reviewer dispositions; each reviewer still closes
  its own finding.

## Invariants

- Reports, verdicts, ledger entries, dispositions and archives stay per reviewer.
- The unanimity gate and `readiness.review_reasons` are unchanged.
- Every segment's inputs, render attempt and publication intent are persisted before any
  write, and each attempt re-renders byte-identically. Segment membership is sealed; only
  a definitively rejected render attempt is ever superseded, and an attempt with an
  unknown outcome is never resent.
- No finding is hidden. Every accepted report is archived and linked from the issue, and
  published to the PR while the PR is open, even if the consolidator fails.
- Closure never waits on PR publication, and no new PR write starts once closure is
  observed. The terminal issue summary is retried on a schedule until it has a receipt.
- Published openings are immutable. Cluster membership is frozen once published.

## Open questions

1. Phase 4: should the implementer respond for every cluster, or only for conflicting
   ones?
2. Phase 3: is a consolidator worth its cost? Phase 2 data on candidate pairs answers
   this.
