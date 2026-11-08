# Concurro service

Status: implemented; audited 2026-11-07. Complements the [MVP](mvp.md) and
[runner/publication contracts](mvp-execution.md). Installation and commands live in
[the deployment guide](../../gateway/README.md) and [operations guide](../concurro.md).

## Components and state

One service process on a Linux host owns the scheduler, worker pool, GitHub mutation
gateway and SQLite registry. The exchange and inference gateway are separate services.
All model turns and exploratory builds run on ephemeral GitHub-hosted jobs, including
operator-requested design evaluation. There is no host executor or build workbench.

| Module | Responsibility |
| ---- | ---- |
| `service/ingress.py` | Verify webhook signatures, filter and enqueue |
| `service/reconciler.py` | Observe relevant GitHub changes and recover missed events |
| `service/scheduler.py` | Bound concurrent ticks and distribute work across repositories |
| `service/worker.py` | Run the engine under ownership and classify tick failures |
| `service/target_loader.py` | Load and validate pinned target configuration and bundles |
| `service/github_reads.py` | Read target files, default-branch heads and runner identities |
| `service/runner_diagnostics.py` | Diagnose termination of the OIDC-bound Actions run |
| `service/gateway.py` | Fence, bind, pace and record all GitHub mutations |
| `service/admission.py` | Pure request-shape admission for task, publication and runner writes |
| `service/settlement.py` | Classify write outcomes and log bounded rejection diagnostics |
| `service/identity.py`, `targets.py` | Mint and verify repository-specific installation tokens |
| `service/registry.py` | Durable queue, ownership, mutation intents and observations |
| `service/control.py`, `cutover.py` | Owner-only operator channel and legacy controller migration |

| State | Authority and recovery |
| ---- | ---- |
| Task state, approvals, outboxes, report references | GitHub audit and immutable report artifacts; verified by the engine |
| Ownership generations and write outcomes | Service registry; must survive restarts |
| Runner sessions, OIDC binding, queued turns and results | Exchange database; lost/expired sessions cannot deliver accepted results |
| Retained model transcripts | Private service storage, transported to replacement role jobs |
| Provider credentials and inference accounting | Gateway deployment; never runner configuration |
| Verified immutable report cache | Disposable service memory; misses re-read and verify artifacts |

The service locks its state directory for its lifetime. SQLite uses WAL and full
synchronous commits. A missing or mismatched registry fails startup unless explicitly
initialized. Operator commands use the daemon's owner-only socket, not a second registry
writer. Losing the registry is not equivalent to losing a cache: do not initialize an
empty replacement while old workers or unresolved writes may exist.

## Configuration and identity

Private JSON settings select the controller identity, App key, webhook secret, state
location, target pairs, concurrency limits and gateway connection. The optional engine
pin must match the clean checkout at startup. Settings and secrets live outside that
checkout in owner-only locations; paths, symlinks and duplicate secret files are validated.
There are no agent/execution image settings or per-target provider credential files.
See `service/settings.py` and the deployment example for the exact schema.

Each task pins the fork configuration commit independently from its upstream source base.
The worker supplies the same bundle to the controller and runner transport. Setup and
instructions come from that configuration commit; new default-branch configuration
applies to new tasks. Bundle drift blocks model work, not closure or readiness observation.
A writer retry may adopt a proven engine-implementer-skill-only update for a blocked
implementation, preserving its plan and approvals. Other instruction changes require
explicit `/concurro invalidate`, which adopts the current bundle and restarts assessment without
resetting spent budgets or silently changing the pinned target configuration.

Installation tokens are narrowed to one repository and checked against the exact requested
permissions (excluding implicit metadata). A mismatch revokes the token and fails admission.
`service.targets.permissions()` unions permissions when repositories have multiple roles:

| Role | Read token | Write token |
| ---- | ---- | ---- |
| Upstream | Contents, Pull requests, Checks, Commit statuses, Actions: read | Pull requests, Contents: write |
| Fork | Contents, Actions: read | Contents, Actions, Workflows: write |
| Issue source | Adds Issues: read | Adds Issues: write |

Contents write upstream retains the tested owned-thread resolution combination. Workflows
write on the fork permits publication from upstream commits containing workflow changes;
path policy still forbids agents from editing dot-prefixed paths. Read and upstream-only
tokens do not gain Workflows write. Exact-match verification means an unaccepted grant can
prevent all fork write-token issuance, not just the operation that first needs it.

## Scheduling and observation

A task key is `(issue_repository, issue_number)`. Its PR destination and fork branch are
recorded separately. At most one tick owns a key. Concurrency is bounded globally and per
repository; due work is selected round-robin across repositories, oldest first within one.
Leases last 15 minutes and are renewed every five minutes independently of GitHub writes.

| Tick outcome | Next action |
| ---- | ---- |
| `continue` | Queue another tick immediately |
| `wait-until` | Queue at the requested time; relevant new input may wake it sooner |
| `await-human` / `blocked` | Wait for relevant input or operator action |
| `terminal` | Stop ordinary polling; reopening can make the issue relevant again |

The ingress checks HMAC, installation and enabled repository before enqueueing. It handles
issues, issue comments, PRs, PR reviews, review comments/threads, check suites/runs and
commit statuses. Unrelated deliveries and Concurro-authored events are ignored; accepted
delivery IDs are deduplicated. Check events map by recorded PR or head SHA and wake
readiness only, not a paused model stage. Subscribe the App to the relevant events as
well as granting permissions; otherwise progress depends on polling.

The reconciler polls every five minutes by default, including tracked issues whose
enrollment label was removed. Snapshots cover relevant human discussion, issue content,
enrollment and PR/CI state. The worker acknowledges the snapshot taken before its tick,
so input arriving during the tick is not lost. A failed tick defers its observed snapshot;
an unchanged poll or CI event does not bypass failure backoff. An issue `updated_at`
change is a rescan hint, not evidence that new model work is needed.

## Ownership and writes

Workers use a facade, not installation write tokens. The mutation gateway alone sends
writes. This is an in-process code boundary, not isolation from a compromised controller.
Native runner jobs have neither the App key nor these tokens.

Ownership grants and mutation admission share a per-task critical section. Each mutation
records a new occurrence `(task, generation, sequence)` before sending. Identical request
bodies are not globally deduplicated: labels and other state can legitimately cycle.
Ownership is rechecked after waiting. Handoff requires no live owner, in-flight mutation
or unresolved outcome; lease expiry alone cannot authorize a competing writer.

One process-wide slot serializes writes across tasks and installations, with at least one
second from response completion to the next send, including failures. Reads and lease
renewal bypass it. The slot resets on restart, is not FIFO, and a slow request delays other
writes until completion or timeout. It is pacing, not a guarantee against rate limiting.

Writes are limited to task-bound issue artifacts/comments/labels, fork Git objects and
refs, runner dispatch, upstream draft PRs and readiness, and owned review projections.
The gateway does not admit merges, arbitrary workflow edits or linked-development-branch
creation. PR adoption verifies the recorded number, App author and fork head. New PRs
use `maintainer_can_modify: false`. Comment edits and finding-thread writes validate
App authorship and the expected marker before mutation.

### Settlement and retries

| Observation | Treatment |
| ---- | ---- |
| REST success | Applied; if its body is lost, reconstruct the result on the next tick |
| Supported GraphQL mutation with complete expected success and no errors | Applied |
| Definitely not sent, 401, or 403 with rate-limit evidence | Failed; schedule a transient retry |
| Other definite REST rejection (except 408/429) | Failed; report the rejection |
| Timeout/lost connection after sending, 408/429/5xx, ambiguous GraphQL response or interrupted send | Unknown; stop further writes and ownership handoff |

There is no blanket automatic resend of mutations. The engine reconstructs publications
from markers, authors, exact content and receipts; a later identical write is a new
occurrence under current ownership. Two bounded exceptions handle Git object visibility:

- Commit creation rejected with 422 for a missing tree: structured `tree`/`missing` errors,
  or the normalized legacy message when structured errors are absent.
- Tree creation rejected with 422 when it references a blob uploaded in that publication.

Both get at most two additional attempts after one and two seconds. Each attempt is
separately paced and fenced. Unknown outcomes and unrelated validation failures do not
qualify. Detailed GitHub rejection text is sanitized and bounded in operator logs, not
copied into public task comments.

Unknown writes require `resolve` with an observed applied/not-applied disposition and
operator evidence. Resolving the last unknown write makes the task schedulable; the engine
then reconstructs the result. Do not clear intents or blindly redispatch jobs.

Transient reads, unsent writes and recognized throttling preserve the stage and schedule
GitHub's requested delay, or local exponential backoff from 60 seconds capped at an hour.
The cap applies only to local backoff. Uncertain model attempts use their existing bounded
recovery allowance and paid-call accounting. Verified policy failures suspend the task;
unclassified verified failures use fixed public transport/schema text. Failures before
ownership and state verification make no diagnostic GitHub writes.

## Transport and recovery boundaries

GitHub requests use repository-scoped tokens, bounded responses and timeouts, with no
ambient proxy configuration or arbitrary redirects. Source archives may redirect once
to GitHub's codeload host without forwarding Authorization. Blob responses have a larger
read allowance for complete source files; they do not enlarge unrelated JSON endpoints.
Exact endpoint bounds live in `service/transport.py`.

Report storage is described in [the dashboard contract](issue-dashboard.md#report-archive).
New checkpoints use format 3; an older engine cannot read them. Rollback must preserve
consumed calls and publication progress and use a compatible engine, not an older audit.
Legacy `adopt`/`release` commands support old controller markers; they are not a supported
host-execution fallback. Deployment requires the served engine and `deployed` workflow
ref to agree before new sessions are dispatched.
