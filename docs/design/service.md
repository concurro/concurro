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
| `service/actions.py` | Backoffice commands (`POST /actions`, internal network only): resolve the writer, check write permission, post the signed delegated command comment and enqueue the task. The comment is posted directly, not through the mutation gateway: it changes no task state until a tick applies it as a command |

| State | Authority and recovery |
| ---- | ---- |
| Task state, approvals, outboxes, report references | GitHub audit and immutable report artifacts; verified by the engine |
| Ownership generations and write outcomes | Service registry; must survive restarts |
| Runner sessions, OIDC binding, queued turns and results | Exchange database; lost/expired sessions cannot deliver accepted results |
| Retained model transcripts | Private service storage, transported to replacement role jobs |
| Run diagnostics | Optional `diagnostics_dir` (`service/diagnostics.py`): each turn's request, reconstructed prompt, result and transcript archive; best effort, never read by the engine |
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
instructions come from that configuration commit. A writer's
[`/concurro resume`](checkpoint-resume.md) adopts the current default-branch configuration
and bundle, preserving completed work and recording separate execution authority.
The checkpoint is saved before the registry pin changes; a later tick recovers a lost
pin update before dispatching. Bundle drift blocks model work, not closure or readiness
observation. Explicit `/concurro invalidate` restarts assessment without resetting spent
budgets. Implementer-skill-only retry remains available for its narrower case.

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

The ingress verifies HMAC, installation and enabled repository. It handles issues,
issue comments (including deletions), PR/review/thread events, check runs/suites,
workflow runs, commit statuses and base-branch pushes. Receipt and wakeup are committed
in one registry transaction before returning 202. CI events wake readiness only and do
not bypass deferred engine retries. Internal CI events may omit a sender.

Service-owned readiness tasks wait for external events, including after handoff; they
no longer schedule five-minute controller ticks. Actions-owned tasks retain their timer.
Active work, incomplete publication and transient failures keep their existing scheduling.

Every five minutes (`serve --poll-seconds`), App-authenticated recovery lists retained
webhook delivery summaries, following every cursor page. Attempts are grouped by GUID:
a success suppresses retries; pending or unrecognized outcomes remain unresolved;
HTTP failures and explicit transport failures are candidates for redelivery. A detail
read verifies the enabled repository and installation before requesting redelivery.
Ordinary idle tasks cause no issue, PR, CI or artifact reads. Listing cost depends on
retained delivery volume (100 summaries per page), not task count.

The registry records coverage separately for each App/target set. Full reconciliation
runs for bootstrap, a changed target set, or a coverage gap of three days (GitHub's
retention window); `reconcile-once` remains available. Failed scans do not establish
coverage. Unresolved deliveries hold the prior coverage time, so persistent failure
cannot silently age out. Successful exceptional reconciliation establishes a new
coverage point. Reconciliation reads tracked tasks even if their enrollment label was
removed; failure must propagate before that coverage is recorded.

### List index

`service/list_index.py` keeps a second SQLite file, `list-index.sqlite3`, in the state
directory: one revision per repository and list kind (`issues`, `sponsorships`, `pulls`,
`runs`), which the backoffice uses to decide when a saved list must be read again. A kind
is in one of two modes. In `signals` the index holds nothing else. In `rows` it holds the
list itself: today each fork's executor runs. It holds no scheduling state, and deleting
it costs one re-read or rebuild per list.

- **Deliveries:** after a delivery passes the installation check, and before the task
  filter, ingress offers its GUID, event and repository to a bounded queue. A writer
  thread records a receipt and raises the kind's revision in one transaction. A full
  queue or a failing index never changes a delivery's status or its wakeups.
- **Sponsorships:** the registry reports each repository whose sponsorship documents, or
  a task's terminal outcome, changed in a committed transaction; that raises
  `sponsorships`. An unchanged document is not reported, except when a credential poll
  consumed events: those change the vault-read coverage of sponsorship views.
- **Repair:** each recovery scan hands its delivery history to `ListIndex.check`. A
  delivery of a list-changing event, delivered after the kind's baseline and without a
  receipt, gets one and raises the revision; more than 20 for one kind move the baseline
  instead. A kind last verified, or begun and never verified, longer ago than GitHub
  keeps history is rebaselined by
  the check itself, independently of the registry's coverage. A completed check records
  the scan's start as the kind's verified time; a kind is `current` once a check has
  completed in the running process and while that time is at most ten minutes old, and
  `catching-up` otherwise.
- **Lost local signals:** at every start, and after a queue overflow, `sponsorships` is
  raised for every repository and a repository-wide resynchronization change is logged.
- **Start:** a kind never verified, or not within GitHub's three-day history, gets a new
  baseline. Coverage of a repository no longer configured is dropped. Full reconciliation rebaselines everything.
- **Rows (executor runs):** a `workflow_run` delivery for the executor workflow's
  `workflow_dispatch` runs, on any branch, is reduced to the listed fields and stored per
  run attempt. A snapshot older than the stored one is dropped and a newer one replaces
  it; a tie with different content is settled by reading that exact attempt. A rerun seen
  without its attempt 1 owes a read of exactly that attempt. An attempt GitHub no longer
  has loses its document, and a run whose latest attempt is gone is not listed. In this mode the repair of a
  missing delivery fetches its payload from GitHub's delivery history; one that cannot be
  fetched or applied, like more than 20, is covered by a rebuild.
- **Reads from GitHub** run on a second thread, one request at a time: rebuilds (the
  fork's 50 most recent executor runs, plus attempt 1 of each rerun) and owed item reads.
  A result is applied under the same ordering rule and wins a tie only if nothing changed
  the row since the read began. A rebuild replaces the displayed membership except for
  runs created while it ran, and its start becomes the baseline. Owed reads are stored,
  survive a restart, and a read settles only requests made before it began. A read that
  fails waits for the next recovery scan.
- **State of a kind in `rows`:** `rebuilding` while a first or requested rebuild is owed,
  `failed` when its last attempt failed, otherwise as above, and `catching-up` also while
  an owed read is older than ten minutes.
- **Endpoints:** `POST /lists/revisions`, `POST /lists` (the rows of one kind) and
  `POST /lists/refresh` on the service's listener, which the edge does not forward. They require the bearer in
  `lists_token_path` (32 random bytes as hex, shared with the backoffice) and answer 503
  when it is unset. They take no user token.

Snapshots cover discussion, enrollment, requirements and PR/CI state. The worker
acknowledges its pre-tick snapshot, read without the hint cache, with the comments its
controller actually consumed. Changed input can wake work; unchanged input and CI
cannot bypass a deferred failure. Issue `updated_at` is only a cache hint.

Backoffice commands (`service/actions.py`) are App-authored comments recognized by the
`command_comment` predicate, `delegation.marked`, on `Ingress` and `Reconciler`. App-authored
command events schedule durable observations, not engine ticks. Edits/deletions also
match prior-body evidence or previously observed comment IDs. Each job retains expected
comment identity, body digest and edit/deletion evidence. An uncached snapshot goes
through `registry.observe`; a successful but stale HTTP response leaves the job pending.
Jobs retry with backoff, survive restart, and retain newer events arriving during a read.
After a successful or uncertain post, the service calls
`registry.request_observation(key, comment=...)` with the exact nonce-bearing body and the
returned ID when available. Before posting, a task with no outcome, live tick or deferred
retry also gets a delayed `backoffice:<kind>` queue entry (`Registry.enqueue_unscheduled`).


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

For an uncertain pending-review thread/reply or a confirmed submission, use:

```sh
python3 -m service --settings /path/to/settings.json recover-review INTENT --operator NAME
```

The command requires no live task owner and no unrelated unresolved write. It verifies
the task, App, PR, commit, review ID and body. For a pending edit it journals a compensating
delete and resolves the original as `discarded` only after confirmed deletion. A crash
between deletion and resolution resumes from that durable receipt. Unknown compensating
deletes remain stopped for manual `resolve`; uncertain create/delete operations also
stay manual. For submission, only the exact confirmed `COMMENTED` review settles the
intent; pending or missing reviews remain stopped when the submit is uncertain.
A failed compensating delete can be attempted again after ownership is rechecked; the new
child receipt replaces the recovery pointer atomically, and the old failed receipt stays
in the journal. In-flight or unknown deletes still require manual resolution.

Normal publication can resume a first submit after a checkpoint crash or definite failure.
The gateway admits it only if every prior submit for that review ID definitely failed,
with no manual resolution. Uncertain same-ID retry is not enabled.

A confirmed create stores the returned review ID atomically with its successful receipt.
Registry schema 5 adds webhook coverage and durable observation jobs, preserving review
identity and compensation progress. Schemas 2–4 migrate transactionally. Back up the
stopped registry before upgrading; earlier services cannot open schema 5. Recovery timing is an operator decision, not evidence that a lost
request has finished. All compensating writes retain normal ownership and pacing checks.


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
Report detachment uses checkpoint format 3, parallel review batches require format 4,
large Git-backed file inventories require format 5, and writer-directed resume receipts
require format 6. Older engines reject unknown
formats. Rollback must preserve
consumed calls and publication progress and use a compatible engine, not an older audit.
Legacy `adopt`/`release` commands support old controller markers; they are not a supported
host-execution fallback. Every session names the served engine commit, which its runner
must run.


## Provider funding and credential routes

Each issue selects organization or personal funding independently for OpenAI and Anthropic.
Use `/concurro funding openai personal` (or `anthropic`) to grant your own consent and select
personal credentials; use `/concurro funding openai org` to select organization credentials.
Both require current upstream write access. `/concurro sponsor <provider>` records prospective
consent; `/concurro withdraw <provider>` revokes only the caller's consent, even after writer
access is lost. Withdrawal never switches a personally funded provider to organization funding.
Organization selection preserves personal consent and historical charges. Funding selection
does not resume a paused task: after changing funding, a writer uses `/concurro retry` when
manual recovery is needed. Stale personal credential recovery receipts are cleared.

Configure provider defaults once; existing profile overrides take precedence:

```json
{
  "provider_credential_routes": {
    "openai": "openai-api-key",
    "anthropic": "claude-subscription"
  },
  "credential_routes": {
    "optional-special-profile": "anthropic-api-key"
  }
}
```

Routes resolve against the profile's actual provider. Before a new charge, personal funding
requires confirmed consent, current upstream writer authority, a supported adapter and validated
coverage for every selected model using that provider. Organization-funded providers require no
personal credential coverage. Missing coverage pauses calls without payer fallback.

Ordered commands work before enrollment and use the same durable checkpoint/Exchange transition
receipts afterward. `provider_funding` freezes initial task choices; later explicit funding
commands change only future admissions. Admitted payer, route, credential version and spent budgets
remain immutable. Existing `funding_mode` configurations retain their initialization semantics:
absent means organization funding; `contributed` means both providers use personal funding with
legacy consent rules. Existing tasks migrate from their recorded policy, preserving historical
org-funded sponsor attribution. Configuration adoption alone does not switch their payer.

The private `/sponsorships` API exposes per-provider choices, actionable pending source intent and
historical debits. Rejected or superseded funding sources stay in history and are excluded from
pending adoption. Pending owner and payer details come from the validated transition outbox;
queued commands show general pending confirmation until their transition is validated. Registry schema 8 and checkpoint format 9 accept these additive fields through forward
migration; no downgrade path is required. The optional `vault_metadata_url` and
`vault_service_token_path` supply nonsecret coverage and credential-event recovery evidence.

## Credential-event recovery

Every delivery-recovery timer tick runs current-policy observation and reads the vault's
service-authenticated nonsecret
`/metadata/events` feed. Registry schema 8 atomically persists its cursor, latest validation
per owner/route and matching task queue receipts. The service has no gateway resolver bearer.
A paused task records current provider grants, failed versions and its original retry deadline.
Only eligible `awaiting_credentials` tasks are reconsidered. The owned tick processes writer
commands/source changes first, then current authority, all selected route coverage, versions
and budget before any new charge. Duplicate events, wrapping-key rotation and restart do not
reset retry clocks or make an old failed token newly valid. Other suspended/human stages stay
writer-controlled. [Phase 6](../proposals/per-user-credentials/phase-6.md) records the exact
recovery and forward-migration contracts.
