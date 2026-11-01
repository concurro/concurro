# Concurro service

Status: proposal, revision 3; migration step 1 implemented. Describes responsibilities,
module boundaries and contracts. Technology choices (hosting, queue, stores,
secret backend, compute) are deliberately left open; examples are illustrative,
not decisions.

## Problem

Today a target repository runs the controller as a GitHub Actions job on a
dedicated self-hosted runner, with provider credentials stored as environment
secrets. That shape has limits:

| Limitation | Effect |
| ---- | ---- |
| Secrets are static per run | Rotating credentials (OAuth refresh tokens) cannot be written back; a refresh invalidates the stored copy |
| One credential per provider per repository | No per-person, per-team or per-task credential choice; no shared limits across tasks |
| Triggering lives in workflow expressions | Event filtering, de-duplication and concurrency are approximated with `if:` and one concurrency group holding a single pending run |
| One App token per run | Per-provider Apps (distinct authors, reviewer Apps) need several keys and token steps in every target |
| Per-target setup | Each target repeats workflow, environment, variables, secrets and runner configuration |

The supervised local pilot avoids most of these because one process on one host
owns its credentials. The service generalizes that: a long-lived component that
reacts to GitHub events and owns credentials and identities.

## Scope

### Service v1

Moves triggering, credentials and identities out of Actions. Engine semantics are
unchanged, including two deliberate limits:

- A base-branch advance blocks other active tasks pending reconciliation.
- Paths that trigger an execution profile (including Rust) never merge
  autonomously.

### Later milestones

Autonomous feature delivery for a repository such as Stacks Core needs at least
automatic base reconciliation, merge policy for build-checked paths backed by CI
and review evidence, and native approval through a reviewer App. Each needs its
own design; this document only keeps their seams open.

## Goals and non-goals

Goals:

- React to GitHub events across several installed repositories, with recovery for
  missed events.
- Make worker ownership, retries and controller handoff explicit and safe.
- Own provider credentials, including rotating ones, with guarantees stated per
  credential type rather than assumed.
- Choose a credential per model call through a policy, with shared admission
  limits, recording which credential was used.
- Act as distinct GitHub identities per role or provider.
- Keep execution placement pluggable behind an authenticated request/result
  contract.
- Leave designed extension points for contributed credentials used only for
  explicitly claimed work.

Non-goals for v1:

- Contributed-credential registration, claims or any user interface.
- Changing consensus, profiles, gates, budgets or audit semantics.
- Serving organizations other than the operator's (a hosted multi-tenant product).
- Replacing CI. Target checks such as `Concurro Rust` stay in the target's CI.

## What stays

- Authoritative task state remains on GitHub (issue artifacts, outbox, receipts).
- The controller advances one issue by restartable ticks.
- Agent and execution containers, their isolation, and bundle identity checks.
- Targets keep `.concurro/config.json`, `charter.json` and profiles in their own
  repository, reviewed through their own branch protection.

## First deployment

A thick service: controller decisions stay in the trusted worker, every GitHub
mutation passes through the mutation gateway, and bounded inference and execution
operations are delegated to one service-controlled Linux executor with
retained-session affinity. Module
boundaries below are contracts; v1 may deploy several modules as one process.

## Modules

| Module | Responsibility | Trust |
| ---- | ---- | ---- |
| Ingress | Verify event signatures, normalize to `(installation, repository, issue, cause)`, drop irrelevant events, enqueue | Public surface; holds only the webhook secret |
| Reconciler | Periodically enqueue enrolled issues whose outcome is due or whose GitHub inputs changed since last observed | Read-only GitHub access |
| Scheduler | Grant ownership per issue, de-duplicate, bound concurrency, retry with backoff | Owns ownership generations |
| Controller worker | Run engine ticks for owned issues at a pinned engine version; submit GitHub writes to the gateway | Trusted code; holds no GitHub write token |
| Mutation gateway | Sole executor of GitHub writes; serializes them per issue and enforces ownership | Holds installation tokens |
| Executor | Run authorized inference and execution operations; return authenticated results | Receives one operation's material only |
| Identity service | Hold GitHub App keys; mint installation tokens per App, repository and permission set | Highest GitHub privilege |
| Credential service | Store provider credentials; resolve, admit, lease, refresh, revoke | Highest provider privilege |
| Inference proxy | Forward provider API calls while retaining API keys; enforce admission and record usage | Holds API keys |
| Registry | Targets, engine versions, credential records, ownership, reservations, claims | Durable |
| Audit and telemetry | Operational logs and metrics without secrets or model content | Internal |
| Operator interface | Enable targets, rotate and revoke credentials, pause work | Operator-only |

## State

| State | Authority | On loss |
| ---- | ---- | ---- |
| Task state, outbox, receipts, reviews | GitHub | Not lost; the engine's existing recovery applies |
| Event queue, caches | Service | Rebuilt by the reconciler |
| Ownership generations, operation records | Registry | Must survive restarts; reset only by an operator cutover that fences all workers |
| Credential records and material | Credential service | Needs backup and recovery; a restore must not resurrect revoked or superseded material |
| Admission reservations and usage | Registry | Must survive; on loss, block admission until reconciled against provider usage where available |
| Target enablement, engine versions | Registry | Must survive; on loss nothing runs (fail closed) |

## Ownership and fencing

The scheduler grants a worker ownership of one issue with a generation number
that only increases. Timing alone cannot fence a writer: a worker can pass any
check, pause, and later send a request GitHub will accept, because GitHub never
sees generations. Fencing is therefore enforced by token custody.

### Mutation gateway

- Workers hold no GitHub write token. Every mutation (artifact, comment, label,
  branch, PR, review, merge) is a request to the mutation gateway carrying the
  issue, the worker's generation and an operation ID.
- The gateway alone obtains installation tokens from the identity service. It
  executes one issue's mutations serially and checks the generation inside the
  same critical section that grants ownership, so a request is either executed
  under the current generation or rejected.
- Handoff is serialized with writes: a new generation is granted only when the
  issue has no mutation in flight. Queued requests from older generations are
  rejected.
- A mutation whose outcome is unknown (lost response, gateway restart) is
  reconciled against GitHub before any further mutation or handoff for that
  issue. If it cannot be reconciled, the issue is blocked for operator attention
  instead of being taken over automatically.
- v1 runs one gateway process. Replicating it later needs a single leader per
  issue; that is an open question, not a v1 requirement.

Read access is not fenced; a stale worker may read but cannot change anything.

### Operations and retries

- Each model call and mutation has a stable logical operation ID, assigned before
  the first attempt and carried in the outbox and receipts.
- Each attempt has its own attempt ID, and the attempt count per operation is
  persisted before the attempt starts.
- The recovery rule is enforced from that count: a model call whose outcome is
  uncertain may be retried once; a further uncertain outcome blocks the task.
  A mutation is retried only after reconciliation shows it did not take effect.

Guarantees for duplicate or redelivered events:

- No GitHub mutation from a stale generation.
- No duplicate GitHub-visible publication: duplicates are recognized by operation
  ID and receipt.
- At most one additional model call per uncertain call.
- No merge of a head other than the reviewed head; merges stay bound to the exact
  head SHA. Branch updates use non-forced (fast-forward only) ref updates; GitHub
  does not offer an exact expected-previous-commit condition, so the gateway's
  serialization, not the ref update, prevents conflicting writers.

### Cutover from Actions

The marker only fences controllers that understand it, so cutover is ordered:

1. Release an engine version whose controller refuses to act on tasks whose state
   names another controller, and deploy it to every target's Actions path.
2. Disable the Actions controller (`CONCURRO_ENABLED=false`) and wait for running
   jobs to finish.
3. The service writes a controller marker with its first generation into each
   migrated task's state.

A re-enabled Actions job running a marker-aware version then cannot resume
migrated tasks. Tasks without the marker stay with Actions until migrated.

## Tick outcomes

Each tick returns one outcome, recorded with the task:

| Outcome | Scheduling |
| ---- | ---- |
| `continue` | Enqueue again now |
| `wait-until(t)` | Re-enqueue at `t` (CI, rate limits, backoff), or earlier if inputs change |
| `await-human` | Enqueue when relevant inputs change |
| `blocked` | Enqueue when an operator acts or a `/concurro retry` appears |
| `terminal` | Never re-enqueued |

### Change-driven reconciliation

Webhooks can be lost, so every non-terminal outcome is also recoverable by
polling. The service persists an observation marker per task: the newest
relevant comment, label event, issue edit, PR review, PR head and check-run state
it has acted on. The reconciler periodically compares GitHub's current inputs
with those markers and enqueues a task only when a relevant input changed or its
`wait-until` time passed. Polling costs GitHub reads only; a controller tick (and
any model call) runs only when there is something new to act on.

## Admission and limits

Per-task budgets remain. In addition, every model call needs an admission
reservation against the selected credential's limits and any target and global
limits, taken atomically before the call.

- A completed call consumes its reservation. A call whose outcome is uncertain
  (worker death, lost response) is counted as spent; its single permitted retry
  needs a new reservation and keeps the logical operation ID with a new attempt
  ID.
- Rate-limited or exhausted credentials yield `wait-until` or fall back under
  policy; they never spin.
- Fairness: reservations are granted across targets and tasks in a bounded,
  starvation-free order (for example per-target round robin).

## Execution contract

The worker sends the executor an operation request; the executor returns a
result bound to it.

Request fields:

| Field | Purpose |
| ---- | ---- |
| Repository ID, installation ID, issue | Which task |
| Ownership generation, logical operation ID, attempt ID | Which owner, which operation and which attempt |
| Engine version, agent or execution image digest | What code runs |
| Bundle identity, configuration digest | Which instructions and policy |
| Source revision or checkpoint tree | Which source |
| Role, provider, model | Which agent |
| Credential ID and lease reference | Which credential, without secret material |
| Approved checks or schema | What may be returned |
| Deadline | When the result stops being acceptable |

The request is authenticated by the worker; the result carries the request digest
and is authenticated by the executor. The worker rejects results that are stale
(older generation, superseded attempt, past deadline), mismatched (any field
differs) or unsolicited. A late result from an earlier attempt never satisfies its
retry; it is discarded, though its usage still counts against admission.

Placement-specific requirements:

- Service-controlled executor (v1): authenticated channel to the worker; no
  GitHub token is ever present.
- CI-runner executor (later): additionally requires an allowed workflow file at an
  allowed revision, the dispatched run ID and attempt, and a workload identity
  (for example OIDC) whose claims match them.
- Thin placement, where the controller tick runs inside the executor, exposes an
  installation token to the executor and moves GitHub write authority there; it
  needs its own review before use. Thick placement exposes none.

## Credential service

### Records

| Field | Meaning |
| ---- | ---- |
| `id` | Stable, non-secret identifier recorded in audit state |
| `provider` | `claude`, `codex`, `copilot`, … |
| `kind` | `org` (operator- or organization-owned) or `contributed` (a person's own) |
| `owner` | Operator, team, or GitHub user for contributed credentials |
| `account` | Non-secret provider account identity, used for session binding |
| `adapter` | How material is held and handed out (below) |
| `scope`, `limits`, `status` | Allowed targets and stages; admission limits; active, refreshing, uncertain, suspended, revoked |
| `version` | Increments on every rotation |

### Adapters

A service lease is an accounting and authorization record; it does not by itself
make delivered material expire. Each adapter states what it can actually enforce:

| Adapter | Material reaching the executor | Expiry | Revocation | Guarantee rests on |
| ---- | ---- | ---- | ---- | ---- |
| Proxied API key (`proxied-api-key`) | Per-operation proxy token | Proxy-enforced | Immediate at the proxy | Broker retains the key |
| Direct API key (`direct-api-key`) | The key itself | None | Provider-side rotation only | Trusted executor |
| Provider-issued access token (`access-token`) | Short-lived token | Provider-enforced | Usually none until expiry | Provider expiry |
| Long-lived token (`long-lived-token`) | The token itself | Provider-defined, long | Provider-side only | Trusted executor |

Proxied keys are preferred wherever the provider CLI accepts a custom API
endpoint. Direct and long-lived forms are acceptable only with the
service-controlled executor, and their records say so. Ending a lease never claims
to recall delivered material.

### Rotating credentials

Applies to credentials the service refreshes (for example OAuth refresh tokens).
Refresh happens only inside the credential service, never in an executor, and is
serialized per credential:

1. Record a refresh intent and move the record to `refreshing`.
2. Call the provider.
3. Durably store the new material as `version + 1` and return to `active`.

A crash between steps 2 and 3 leaves the record `refreshing` with an unknown
provider state. Recovery moves it to `uncertain`: it is never reissued, and it
returns to `active` only after a successful refresh attempt or operator
re-registration. Writes carrying an older version are rejected. Executors receive
only short-lived access material and are restarted with new material when it
renews.

For Codex, OpenAI documents an app-server flow matching this model: the calling
application passes an access token through the app-server process environment,
renews it itself, restarts app-server with the new token and resumes the thread
by ID. That documentation is scoped to open-source and locally hosted apps, and
each client is bound to one user and workspace; remotely hosted offerings are
directed to OpenAI separately. Eligibility for this deployment must be confirmed,
and the flow is not permission to pool subscriptions.

### Policy

The resolver chooses independently for each provider, so one task may mix
credentials. The v1 policy is one org credential per provider. A policy may block
or fall back, but never substitutes a different person's contributed credential.

### Audit

Audit state records the credential `id`, `kind` and adapter used by each model
call, never secret material.

## Retained sessions

- Session identity adds the credential's `account`. A different account starts a
  fresh session; a renewed token for the same account keeps it.
- Authentication material is never stored with transcripts. Provider home
  directories are currently retained alongside transcripts; v1 must separate
  credential files and environment from retained storage so transcripts can move
  or persist without carrying credentials.
- The scheduler keeps a task's sessions on the executor that holds them. If that
  executor is lost, the engine's existing restart path applies once, with its
  budget accounting.
- Sessions are deleted when their task ends or their scope changes.

## Identities and authorship

- One canonical controller identity authors audit artifacts, state and status
  comments. Artifact validation and comment recovery currently accept only
  `bot_login`; per-provider publication requires extending those contracts to an
  explicit allow-list of `(identity, event type)` pairs, with the controller
  identity remaining the only author of state.
- Optional per-provider Apps author their own review comments.
- A reviewer App that submits native GitHub approvals is a later-milestone
  capability with its own authority:
  - It approves only when the engine's review gates pass for the exact PR head
    SHA and review generation, from verified model verdicts.
  - A push, design change or reopened objection invalidates the approval; the
    controller requests its dismissal before further gates are evaluated.
  - Approvals and dismissals are gateway mutations under the same generation
    checks as every other write. Its installation tokens stay in the mutation
    gateway, never in the worker or model containers.
  - It is enabled per target by operator policy, not by default.

## Contributed credentials and claims (extension point)

Not built in v1; the contracts above are shaped so it can be added later.

- A person registers their own credential with `kind: contributed` and their
  GitHub identity as `owner`.
- It is used only for work its owner explicitly claims, for example with
  `/concurro claim` on an enrolled issue, until `/concurro release`, task
  completion or revocation.
- Claims are validated like other commands (current writer, registered
  credential) and recorded in audit state. A claim affects credential choice only;
  it never approves, unblocks or widens anything.
- Unclaimed work, providers the claimant has no credential for, and claims whose
  credential becomes invalid fall back under policy or block; they never use
  another contributor's credential.
- Provider terms for this use must be confirmed per provider before enabling it.

## Targets and engine versions

The service serves every repository where its controller App is installed and the
operator has enabled the target. Targets select their engine version in their own
configuration; the service runs only versions it has and the operator allows.
Upgrading a target with active tasks follows the existing rule that bundle
identity changes block in-flight approvals.

## Self-improvement

Improvements to the engine, skills, roles or governing policy are proposed as
measured changes, with the evidence that motivates them, through the same review
process as any other change to the `concurro` repository. Promoting a new engine
version or bundle to a target remains a separate, explicit operator action. The
service never applies its own proposals.

## Trust boundaries

Changes relative to the Actions model:

- Ingress is a public endpoint; it verifies signatures and only normalizes and
  enqueues.
- The identity service holds App private keys, broader than one repository's
  installation token; its compromise affects every installation. Only the
  mutation gateway receives write tokens from it.
- The credential service and inference proxy are the highest-value stores;
  contributed credentials would raise that further.
- Environment reviewers no longer gate runs. Supervision moves to operator
  enablement per target, the existing `/concurro` commands, and autonomous merge
  remaining off by default.
- Agent and execution containers keep today's boundaries regardless of placement.

## Acceptance scenarios

Required before deployment. Ownership takeover and missed-event recovery are
gating: the service is not deployed until both pass.

- Duplicate and redelivered events, including during an active tick.
- Lost webhooks for a clarification, approval, review and `/concurro retry`,
  each recovered by the reconciler without unnecessary model calls.
- Worker death during a model call and during a GitHub write.
- Ownership takeover while the old worker survives and attempts to publish and
  merge; every such request is rejected by the gateway.
- Gateway restart with a mutation in flight: reconciled or blocked, never
  handed over blindly.
- Lost GitHub responses for artifact, comment, branch and merge writes.
- Credential refresh failure, including a crash between provider rotation and
  durable write-back.
- Account switching for a retained session.
- A PR push after approval, with approval invalidation.
- Admission exhaustion and rate limiting across several tasks sharing one
  credential.
- Actions-to-service cutover with a marker-aware Actions controller re-enabled
  afterwards.
- End to end: one task merges while another must reconcile its base.

## Migration

Each step is useful alone and keeps the Actions path working until removed.

1. Credential resolver in the engine (implemented): the controller selects a
   credential identity per model call through `engine/credentials.py`; the
   transport receives only that credential's material; audit state counts calls
   per credential ID. The first resolver reads today's environment.
2. Identity abstraction and authorship allow-list, still with one App.
3. Tick outcomes, logical operation and attempt IDs, and controller-marker
   support in the engine, released to the Actions path before any cutover.
4. Service skeleton: ingress, change-driven reconciler, scheduler with ownership
   generations, mutation gateway, and a worker running the current controller
   with the service-controlled executor and session affinity.
5. Credential service, admission, and the inference proxy for API keys.
6. Rotating credentials, starting with the documented Codex app-server flow.
7. Per-provider Apps through the identity service.
8. Later: CI-runner executor, contributed credentials and claims, reviewer App.

## Open questions

- How a stuck worker is detected and its ownership revoked (gateway-enforced, so
  timing affects liveness only, not safety).
- Secret backend and its backup and restore semantics.
- Which provider CLIs accept a custom API endpoint for the inference proxy.
- Codex app-server eligibility for this deployment (assumed, not verified).
- How targets select engine versions, and who approves upgrades.
- Provider terms for contributed subscriptions.
- Replicating the scheduler, mutation gateway or credential service while
  keeping single-writer guarantees.
- Usage accounting across credentials, teams and targets.
