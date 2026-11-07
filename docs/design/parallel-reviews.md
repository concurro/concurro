# Parallel review dispatch

Status: revision 3, 2026-11-07. Part 1 timing is implemented locally, uncommitted for review.
Parts 2 and 3 remain proposals; no parallel dispatch or deployment.

## Objective and delivery scope

Run independent reviewers concurrently against frozen inputs, while one controller applies
results serially. Reduce elapsed waiting without changing approval requirements or reviewer
independence. Build one coordinator for design and PR reviews, with separate stage policies.

Deliver three separately reviewable parts:

1. **Timing first:** add minimal timing evidence while reviews remain serial, establishing
   a baseline before parallelism is enabled.
2. **Parallel dispatch at current recovery guarantees:** enable cooperative and fresh design
   reviews first, then PR reviews in a separate integration using the same coordinator.
3. **Stronger recovery, deferred:** consider exact resubmission across process loss and
   recovery of old-generation exchange results only if pilot evidence justifies it.

Do not parallelize planning, implementation or successive turns of one retained session.
No new scheduler service, agent framework or configurable concurrency knob is needed.
Concurrency follows the review roster and existing task-worker limits. Parallelism saves
elapsed time, not model calls; failure recovery can consume additional calls.

## Current behavior and prerequisites

Both paths are serial: `Controller.quality_control()` selects one missing design vote and
`Controller.review()` one missing PR review. Each blocks in `ask()` until its turn finishes.
The controller has singleton operation, consumption, correction and rejected-draft records,
and mutates budgets, sessions, outboxes and checkpoints. Concurrent calls to `ask()` are unsafe.

`RunnerExecutor.run()` creates an in-memory exchange turn UUID. Submission retries already
reuse that UUID and exact body while the process lives. After process loss, unresolved
controller attempts become uncertain and retry within existing allowances. Results already
saved by the controller can be replayed. Parallel v1 preserves these guarantees.

Two execution constraints must be explicit:

- **Staffing:** the exchange maintains at most one live runner per staffing group, and a
  runner claims one turn at a time. The role and session scope/phase contribute to the
  binding; `RunnerExecutor.admit()` also incorporates the resolved agent profile into the
  phase. Distinct reviewers must therefore receive distinct staffing groups and dispatches,
  including different models using the same CLI. Test this through the exchange, not only
  a fake engine executor.
- **Ownership:** staffing includes the worker's ownership generation. Each new worker tick
  gets a new generation; cleanup retires sessions whose generation is no longer live.
  Keep the batch within one renewing worker lease, as today's blocking call does. Do not
  yield a tick with healthy reviews still running or weaken generation checks.

Executor error cleanup currently ends every session in its local set. Replace that behavior
with cleanup scoped to the failing member before enabling concurrency.

Only the owning controller publishes. App/provider credentials remain central, OIDC binds
runners to their issued sessions, cache access stays read-only, and reviews authorize only
an exact design or PR head.

## Part 1: minimal timing evidence

Expose the exchange's existing turn ID, status, `created`, `started` and `finished` through
operator submit/replay/poll responses. Keep the additions backward compatible. Record only
observed values; replay must not change timestamps. Finished turns are currently removed
from the exchange after 24 hours, so retain available timing with the controller's durable
attempt history during normal processing.

Attach one small receipt to the current operation and its existing report-index entry,
linking the attempt to the reviewer, run/phase and report artifact. There is no independent
attempt-history list today, and this change adds none. The current operation keeps only
its latest attempt; the report index keeps at most 40 reports. Failed attempts without
reports can lose timing when their operation is replaced. Checkpoint pressure may discard
older timing receipts, then pending/current telemetry, before it can block core state.

Do not copy report bodies or reconstruct missing observations after a crash. No recovery
backfill service, extra model calls or historical scanning is required. The timing receipt
is evidence, not authority to resume an exchange turn. Extend the full-call-budget capacity
test with receipts on every attempt, including pending publication and reload; require
bounded saves and unchanged active reports, with missing timing reported as unknown.

| Evidence | Interpretation |
| ---- | ---- |
| `started - created` | Admission-to-claim delay, including staffing/setup; not pure GitHub queue time |
| `finished - started` for done turns | Whole turn execution, including tools and transcript handling; not inference time |
| First admission to last terminal result in a round | Observed exchange-clock round span, only when coverage is complete |
| Overlap of running intervals after parallelism | Evidence of concurrency, not measured savings against an unrun serial counterfactual |

Use the exchange clock for these durations. Queued cancellation has no execution duration;
missing timestamps and lost outcomes remain unknown. Incomplete rounds are reported as
partial observations. Separate corrections, failures and fresh phases. Tokens and monetary
cost remain unavailable unless independently observed.

Timing must not enter model contexts, call identities, eligibility digests or approval
bindings. Store it once and derive summaries in `design_audit.py` and Diagnostics. Do not
publish gateway addresses, grants, tokens or raw prompts. Telemetry omission must not block
review validation or approval.

Extend the read-only audit tool to record covered pilots, artifact ranges, engine/config
identities when available and timing coverage. Ship this change while reviews are serial;
record a serial baseline from an authorized pilot before enabling parallel dispatch.
Compare equivalent phases and model rosters, noting differences in tasks, setup and repairs.
PR repair rounds repeat the reviewer roster and may benefit substantially, but their
relative savings are unmeasured.

## Part 2: shared coordinator

Use a small coordinator with stage adapters. Reuse existing helpers rather than building
a second execution framework.

| Shared responsibility | Stage-specific responsibility |
| ---- | ---- |
| Per-member operation and admission records | Build independent design or PR contexts |
| Budget charges, submission, polling and scoped cancellation | Validate reports, evidence and citations |
| Saved-result replay and ownership checks | Apply design objections or PR findings/dispositions |
| Status, correction scheduling and timing | Decide revision, fresh phase, repair or handoff |

Split `Agents.ask()` into preparation and result decoding and split executor submit, observe
and cancel from its blocking `run()` wrapper. Preserve serial wrappers for other callers.
Transport code never applies verdicts; the coordinator never calls state-mutating `ask()`
concurrently.

### Frozen inputs and per-member state

A batch has one stage, phase, exact review binding and fixed roster. Design uses the existing
approval binding and fresh-context rules. PR review later uses the exact base/head, PR,
input revision, plan approval and roster, with each reviewer's own finding snapshot.

Build all initial contexts before applying results. Corrections use that member's frozen
context plus controller validation feedback, never a sibling's result. Recheck task authority
and freshness before admission and application. A changed binding invalidates the batch.

Replace singleton operation, consuming, correction and draft slots for reviews with small
per-member records: logical operation/attempt identity, request commitment, owner generation,
session identity, status, charge, allowances and saved-result reference. Keep eligibility
receipts outside operations as today. Use resolved profile identities, not CLI names.

Reuse immutable report storage and private retained-session storage. Do not duplicate full
reports, prepared requests or transcripts in checkpoints. Persist completed results before
consuming them, retaining existing replay and outbox guarantees. Replaying a saved result
must not charge again, advance the session again or apply findings twice.

The executor keeps the UUID and exact submission body for retries during the live attempt,
as today. Persisting controller attempt records or observed turn IDs does **not** promise
exact exchange resubmission after process loss. There is no new private payload spool.

### Scheduling and verdicts

Submit the initial roster before waiting for results. GitHub dispatch writes remain paced
serially; models execute concurrently. Poll members round-robin using existing zero-wait
polls and a modest delay between sweeps, not sequential 45-second long polls. Keep lease
renewal independent of polling and writes, and check ownership between operations.

Apply ready results serially; use roster order when several are ready in the same sweep.
A fast later reviewer need not wait for a slow earlier one. Durable consumption and outbox
identities must survive replay independently of completion order.

Gather ordinary approve/revise results before revising the plan. A valid clarify, reject
or escalate stops further admission, preserves already completed results and cancels/drains
remaining work before the terminal transition. Late approvals cannot override that decision.

A correctable output failure affects only its member. Archive each rejected report before
replacing its draft or admitting its correction. One provider failure must not terminate a
healthy sibling. If a member exhausts its allowance, stop new admissions, preserve completed
results, cancel/drain active siblings and persist the block/escalation. Retry preserves
reusable votes and eligibility receipts and renews only applicable existing allowances.
Unanimity and fresh-phase rules remain unchanged.

### Budget accounting and deliberate behavior changes

Before a phase starts, require enough calls for every missing member's initial attempt.
Reserve and charge that roster in one checkpoint before submitting any member, updating
task and credential-route counts together. **This blocks earlier than today's serial path**,
which can spend its remaining call on one reviewer before discovering the next cannot run.

Do not reserve hypothetical corrections. Admit corrections and provider retries serially
within the remaining budget, using roster order for competing requests. Each new model
attempt has a new charge; transport retries and replay of saved results do not.

Reservations are not refunded. **A crash after charging the roster can strand one call per
reviewer**, including attempts not yet submitted, rather than at most one serial reservation.
On restart, unresolved reserved attempts use uncertain-call recovery and replacement calls
are charged again. This conservative trade-off avoids inventing a refund or resubmission
protocol. Distinguish charged attempts from observed execution; never claim unobserved
reservations certainly reached a model.

Keep gateway per-session spending controls and model allow-lists unchanged. They are not a
reservation-backed task-wide monetary cap. Concurrent usage can overshoot spending limits;
parallelism increases instantaneous load but never increases the task's model-call allowance.

### Recovery at current guarantees

| Interruption | Parallel v1 behavior |
| ---- | ---- |
| Crash before roster reservation save | Nothing was authorized for submission; prepare again |
| Process loss after reservation, before a result is durably saved | Each unresolved member becomes uncertain; keep its charge and retry within its own existing allowance |
| Lost submit reply in the live owning process | Retry the same UUID and exact body within current transport bounds |
| Lost workflow-dispatch reply | Use existing mutation reconciliation; do not blindly dispatch another job |
| Crash after saving a result, before application | Replay the saved result once without another model call |
| Crash after application, before report publication | Resume the existing deterministic outbox |
| One member fails | Apply that member's bounded failure policy; do not erase completed siblings |
| Stop, invalidation or changed design/head | Stop admission, invalidate old results' authority and cancel/drain remaining work |
| Ownership changes | Stop old-owner writes; retire abandoned sessions through existing cleanup; retain saved results only if their bindings still match |

Do not recover results that exist only in the old generation's exchange records, even if
execution happened to finish. Do not poll old queued turns: normal polling can staff them.
No observe-only endpoint is needed in v1. Existing cleanup retires abandoned-generation
sessions; unresolved cleanup or mutation outcomes remain reconciliation work, not grounds
for weakening ownership checks. Do not rebind an old attempt to a new generation.

Keep uncertain-call and provider-failure allowances per member with the existing bounds,
not one shared batch counter. A restart of a retained reviewer session must call the
eligibility restart path: it loses first-pass eligibility even at turn zero. Retry may
renew correction allowance but cannot restore lost eligibility under the same binding.
Completed accepted votes remain usable when their original bindings remain valid.

### PR integration on the same coordinator

Design-specific eligibility and objection policy stay in the design adapter. Test the
shared coordinator with a PR-shaped adapter from the start; production PR dispatch remains
serial until a separate reviewed integration:

- Freeze the exact base/head and each reviewer's own findings before submission.
- Use fresh PR-review calls, without cooperative transcripts or `first_pass` semantics.
- Apply findings, dispositions and native COMMENT-review outboxes serially with existing
  freshness and duplicate-publication checks.
- Gather ordinary changes-requested reviews before implementation repair begins.
- Treat design reopen as batch invalidation: retain useful completed reports as history,
  cancel remaining work, and prevent late results from resolving findings or approving.
- Preserve current PR validation/failure policy; shared dispatch does not introduce
  design-style output corrections into PR reviews.

## Implementation sequence

1. **Timing-only change:** expose timestamps, retain minimal observed receipts, extend the
   audit reader and test the serial path. Review and ship separately; capture a serial baseline.
2. **Lifecycle extraction:** split prepare/decode and submit/observe/cancel while keeping
   serial wrappers. Scope cleanup per member. Prove exchange staffing separation.
3. **Batch state and admission:** per-member operations, corrections/drafts, saved results,
   atomic whole-roster charging and format protection. Reuse storage and eligibility helpers.
4. **Design integration and recovery:** submit the roster, poll under one renewing lease,
   apply serially, cancel/drain on terminal outcomes and test current-guarantee crash recovery.
5. **Visibility and comparison:** show all members as queued/running/correcting/complete,
   with detailed timing in Diagnostics. Publish state transitions, not every poll. Use a
   fixture overlap proof; a separately authorized pilot supplies actual timing evidence.
6. **Separate PR integration:** reuse the coordinator with native finding/publication tests.
   Do not build a second dispatch mechanism or wait for the deferred recovery project.

## Required regression tests

- Timing responses and retained receipts survive normal replay; missing/cancelled/lost
  observations remain unknown. Fake-clock durations are deterministic. Telemetry changes
  neither model requests nor approval/eligibility digests and adds no mandatory gate.
- Exchange-level submissions for two reviewers each return their own dispatch, including
  two distinct model profiles using the same CLI. Each issued runner claims only its own
  work and at most one turn at a time; same-session work remains serialized.
- Both fake reviewers start before either completes; a fast second member is consumed while
  the first runs. Contexts contain no peer results, including during corrections.
- Independent correction allowances and rejected drafts survive reload. One provider's
  failure does not kill a healthy sibling; exhaustion follows terminal cancel/drain rules.
- Whole-roster budget is sufficient, exactly sufficient or insufficient. Insufficiency
  dispatches and charges nothing. Competing corrections cannot overspend the final call.
- Crash after charging but before any submission, and after only part of the roster submits:
  all reservations stay charged, unresolved members recover independently within bounds,
  and replacement attempts require new charges. Saved-result replay is free.
- Every recovery-table row, including partial completion, unknown mutation outcomes and
  ownership loss. An exchange-only old-generation result is not recovered or used to vote.
- A retained-session restart through parallel recovery loses first-pass eligibility.
  Validation-only correction preserves eligible receipts; legacy in-flight work gains none.
- Opposite completion orders preserve verdict and objection meaning, with no duplicate
  findings or outbox writes. Late results cannot override terminal decisions or new bindings.
- Full call budget and supported roster fit checkpoints using references; pending results
  and publications are protected. No prepared payloads or credentials enter public artifacts.
- A PR-shaped adapter covers exact-head staleness and reopen. Production serial PR behavior
  remains unchanged until its separate integration tests pass.
- Lease renewal continues during polling and paced writes; cleanup preserves live-generation
  siblings and retires abandoned ones. The dashboard lists every pending reviewer.

Run focused and full engine, exchange and service suites plus bundle, whitespace and docs
checks for implementation. No tests require live providers. Fixtures prove concurrency and
recovery rules, not live time savings.

## Compatibility and deployment

Finish or reconcile legacy singleton operations before batching remaining reviewers. Preserve
completed votes, allowances, eligibility receipts and spent budgets; do not repeat reviews
just to obtain parallelism. Missing legacy exchange IDs remain unavailable.

Batch-bearing checkpoints need a new format guard so old engines cannot ignore active
members and dispatch duplicate serial calls. Keep readers for supported older formats;
unsupported readers must fail closed. Rollback after a batch-format save requires a
compatible reader, not an assumed downgrade. The timing-only change should remain additive
and must not require the batch format or a recovery upgrade.

No bundle instruction/schema change is intended. Coordinate engine, service, exchange and
workflow releases as applicable, reconcile unresolved writes and use an idle window for the
execution change. Each part requires its own review and deployment authorization. Part 1
was authorized for local implementation only; no deployment, retries or interruption of a
live pilot are authorized.

## Part 3: deferred recovery proposal

Durable pre-send exchange UUIDs, a private spool of exact prepared payloads, recovering
exchange-only results across ownership generations and observe-only polling are deferred.
They improve recovery but are not prerequisites for either review type to run in parallel.

Revisit only with evidence of lost completed turns, repeated uncertain calls or material
budget waste. A separate proposal must justify spool retention/security, exact-payload
replay, cancellation/takeover races and compatibility. Do not infer lost completed work
from missing timing alone. This work must not block either design or PR parallelism.
