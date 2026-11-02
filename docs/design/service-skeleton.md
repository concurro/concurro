# Service skeleton (migration step 4)

Status: accepted plan (revision 4). Step 1 (registry and mutation gateway) and
step 2 (settings, identity service, transports, transient errors) are
implemented.
Implements the v1 deployment from [the service design](service.md): one process
on one Linux host, a thick worker, the mutation gateway, and the existing local
executor. Every module
sits behind an interface whose first implementation is local (a local transactional
store, threads, the host's Docker), so hosting, queue, store and secret choices stay
open.

## Scope

In scope:

- Event ingress with signature verification and delivery de-duplication.
- Change-driven reconciler.
- Scheduler with ownership generations, per-issue serialization, bounded
  concurrency and fairness.
- Mutation gateway as the only GitHub writer, and a GitHub facade for the engine.
- Identity service minting installation tokens.
- Worker running the existing controller tick, with the existing local executor.
  Retained sessions stay on this host, so session affinity holds trivially.
- Operator commands, served by the running daemon: adopt, release, status, resolve.

Out of scope here (later steps): credential service, admission and inference proxy
(step 5), rotating credentials (step 6), per-provider Apps (step 7), remote or CI
executors, replication.

## Implementation order

1. Registry and mutation gateway, with their acceptance tests.
2. Identity service and the facade.
3. Worker and scheduler, then ingress, reconciler and the operator commands.

Autonomous merging stays disabled throughout.

## Layout

A new top-level `service/` package; the engine stays importable on its own and
gains no dependency on it.

| Module | Contents |
| ---- | ---- |
| `service/settings.py` | Operator settings outside any repository, validated strictly |
| `service/registry.py` | `Registry` interface and its local transactional implementation |
| `service/ingress.py` | Signature verification, normalization, HTTP handler |
| `service/reconciler.py` | Observation snapshots and due-work discovery |
| `service/scheduler.py` | Queue, concurrency and fairness |
| `service/identity.py` | App JWT and installation tokens (extracted from `engine/pilot.py`) |
| `service/transport.py` | HTTPS to the GitHub API with one repository's token; read and write forms |
| `service/gateway.py` | `MutationGateway`, ownership critical section, `GatewayGitHub` facade |
| `service/failures.py` | The shared failure classifier, mapping errors and stops to outcomes |
| `service/worker.py` | Load a target at its approved revision and run one tick |
| `service/control.py` | Owner-only local control channel for operator commands |
| `service/__main__.py` | `serve` plus clients for `reconcile-once`, `adopt`, `release`, `status`, `resolve` |
| `service/tests/` | Fake GitHub and fake provider tests, no network or Docker |

## Settings

A private JSON file (directory 700, file 600), like the pilot's:

| Field | Meaning |
| ---- | ---- |
| `controller_id` | Ownership marker this service writes, `service:<name>` |
| `engine` | This service's engine commit |
| `app_id`, `private_key_path` | The controller App |
| `webhook_secret_path` | HMAC secret for ingress |
| `state_dir`, `registry_id` | Registry location and the identity it must contain |
| `targets` | Enabled repositories, each with its `credentials_file` |
| `agent_image`, `execution_image` | Pinned digests, process-wide |
| `limits` | Global and per-repository concurrent ticks |

`settings.load(path)` returns a frozen `Settings` and refuses:

- unknown or missing fields, or wrong types;
- a `controller_id` not matching `service:[a-z0-9-]{1,32}`, an `engine` that is not
  40 hex digits, or an `app_id` that is not a positive integer;
- paths that are relative or not canonical (a symlink anywhere along the path, a
  `.` or `..` component, a trailing slash), files not owned by the user with mode
  600, directories not mode 700, or any path inside the engine checkout;
- a `state_dir` that is not an existing owner-only directory;
- no targets, a repository not of the form `owner/name` or repeated (ignoring
  case);
- a secret file (App key, webhook secret, each `credentials_file`) that has
  another hard link, or that is the same file as another secret, compared by
  filesystem identity rather than spelling;
- images not pinned by digest (`policy.IMAGE`);
- limits outside `1 ≤ per_repository ≤ global`.

Checks that need the host (the engine checkout's HEAD equals `engine`; the images
exist locally) run when `serve` starts, in step 3. The private-file checks move
from `engine/pilot.py` into `engine/private.py`, shared by the pilot and the
service; the pilot keeps its function names as thin wrappers.

The service refuses to start if any file is group- or world-readable, as the pilot
does. Process-wide values (images, Docker settings, report directory) are fixed at
startup; nothing per task is placed in the process environment or working
directory.

## Registry

### Contract

The registry is the service's only durable state and has exactly one writer.

- **Exclusive ownership:** `serve` takes an exclusive lock on the state directory
  for its lifetime and refuses to start if another process holds it. Operator
  commands never open the store; they are requests to the running daemon over
  the control channel (a local, owner-only socket).
- **Durability:** every state change is one transaction committed durably
  (synchronously flushed to storage) before its effect is acted on: an ownership
  grant before the tick starts, a mutation record before the request is sent, a
  completion before the response is returned.
- **Missing-state detection:** the store holds `registry_id` and a schema
  version. At startup a missing, empty or mismatched store is fatal unless the
  operator explicitly initializes a new one, so lost state is never silently
  replaced by an empty registry.
- **Corruption fails closed:** an integrity failure stops scheduling and
  mutation admission entirely.

The first implementation (`service/registry.py`) uses SQLite in WAL mode with
full synchronous commits. `grant` consumes the task's work item in the same
commit; at startup, in-flight writes become `unknown` and interrupted
generations end with their tasks enqueued again. A JSON implementation would need write-ahead journaling,
flush ordering and integrity checks to meet the same contract, so it is not
planned.

### Interface

Signatures are indicative.

```python
class Registry:
    # Delivery de-duplication
    def seen(self, delivery_id) -> bool
    # Queue
    def enqueue(self, key, cause, due_at)            # key = (repository, issue)
    def due(self, now) -> list[WorkItem]
    # Ownership and mutations: called only inside the gateway's critical section
    def grant(self, key, worker, lease_seconds) -> int          # new generation
    def current(self, key) -> Ownership | None
    def end(self, key, generation, outcome)
    def renew(self, key, generation, lease_seconds) -> bool
    def admit(self, key, generation, method, path, request_digest) -> str   # intent ID (key, generation, sequence)
    def settle(self, intent_id, result)                         # done | failed | unknown
    def in_flight(self, key) -> list[MutationRecord]
    def unresolved(self, key) -> list[MutationRecord]           # status unknown
    def resolve(self, intent_id, disposition, evidence, operator)
    # Observation and outcomes
    def acknowledged(self, key) -> Snapshot | None
    def acknowledge(self, key, snapshot)
    def outcome(self, key) -> Outcome | None
    def active(self, repository) -> list[key]        # every key with a non-terminal outcome
```

## Mutation gateway

### One critical section per key

Ownership changes and mutation admission share one per-key lock held by the
gateway. Inside it, and only inside it:

- `acquire(key)` grants a new generation only if the key has no live owner, no
  mutation in flight and no unresolved mutation.
- `release(key, generation, outcome)` ends that generation only.
- `mutate` validates the generation and admits the mutation (records it) before
  leaving the critical section; the GitHub request is then sent while the key's
  in-flight marker blocks any `acquire`, and any further write of the task, until
  it settles. A waiting write then rechecks ownership and unresolved writes
  before its own admission.

So no handoff can interleave between a generation check and the record it
authorizes.

### Mutation identity

Identical payloads are not the same operation: label sync legitimately repeats
"add `concurro:stage:intake`" after `intake → plan → intake`, and attention labels
cycle through failure and recovery. The gateway therefore never deduplicates or
replays by payload.

- Every `mutate` call is a new **occurrence** with an intent ID
  `(key, generation, sequence)`, where `sequence` counts mutations within the
  generation. The intent ID is recorded before the request is sent.
- The request digest (method, path, body) is recorded alongside for audit and
  for checking an operator's resolution; it never identifies the occurrence.
- The gateway never resends. A later write with the same payload, in this or a
  later generation, is a new occurrence. The only path that refers back to an
  earlier occurrence is operator resolution, which names its intent ID.
- Responses go to the caller and are not stored for replay, so there is no
  "done without a stored response" state to recover.

Settling an occurrence:

| Result | Recorded as | Raised to the engine |
| ---- | ---- | ---- |
| REST 2xx | `done` | The payload is returned |
| REST 2xx whose body cannot be read or parsed | `done`: the status establishes it applied | `Transient("response_lost")`; the next tick reconstructs the result instead of resending |
| GraphQL 200 with a complete JSON body, no `errors` and a non-null `createLinkedBranch` | `done` | The payload is returned |
| Not sent: no token, or connection or TLS setup failed before any request byte | `failed` | `Transient("unavailable")` |
| 401 (REST or GraphQL) | `failed`: rejected before execution | `Transient("unavailable")`; the cached token is dropped |
| 403 with throttling evidence (REST or GraphQL) | `failed`: rejected before execution | `Transient("throttled")` |
| REST 404 | `failed` | `NotFound` |
| Other REST 4xx except 408 and 429 | `failed`: GitHub rejected the request | `Rejected` |
| Any other GraphQL response: `errors` present, a null result, an unreadable body or another status | `unknown` | `Unresolved` |
| 408, 429, 3xx, 5xx, timeout, connection loss after sending, process crash | `unknown`: the write may have applied, possibly late | `Unresolved` |

GraphQL execution can report errors after a resolver ran, with nulls propagating
upward, so neither a null result nor an error type shows that nothing changed.

Throttling evidence is a `Retry-After` header, `X-RateLimit-Remaining: 0`, or a
body `message` naming a rate limit. Ordinary quota headers on another 403 are not
evidence; that 403 is a plain rejection.

### Write allow-list and result reconstruction

The facade admits only write endpoints whose results the engine can reconstruct
from GitHub on its next tick, after any outcome. Any other write is refused before
admission, so a future engine write needs a rule here first.

| Engine write | Result the caller uses | Reconstruction on the next tick |
| ---- | ---- | ---- |
| Add or remove an issue label; create a label | None | State-derived and idempotent: the next sync recomputes the desired labels |
| Create a summary, health or conversation comment | Comment ID | Found by its marker and author (summary pointer, health marker, event marker) |
| Edit a comment | None | The next tick rewrites the content from state |
| Create or update an audit or plan artifact | Artifact number, revision | Found by creator and name or marker; revision read back and verified |
| Create a Git tree or commit | SHA | Unreferenced objects are harmless; publication recreates them from state |
| Update a branch ref (fast-forward only) | Ref | Read back and compared with the intended commit |
| Create the linked Development branch (exactly the engine's GraphQL document, for this task's issue node) | Ref | Found through the issue's linked branches |
| Open the pull request | PR number | Found by head branch |
| Merge the pull request | Merge SHA | Observed from the PR's merged state and merge commit |

The queue-cursor comments of the Actions `QueueScan` are not used by the service.

### Resolution

An `unknown` or interrupted (`in-flight` at startup) occurrence makes its key
unresolved. Unresolved keys are never scheduled, acquired or released.

`resolve <intent-id> <disposition> <evidence>` records, for that occurrence, the
disposition (`applied` or `not-applied`), the operator's evidence (for example the
GitHub object observed and its request digest) and who resolved it. `applied`
settles it as `done`, `not-applied` as `failed`. Either way, the key becomes
schedulable and the engine's next tick reconstructs the result through the rules
above; nothing is resent and no empty success is returned to a caller. The
operator `release` command refuses keys with unresolved mutations; the gateway's
`release` still ends a generation, so a stopped tick never stays live.

### Facade

`GatewayGitHub` subclasses the engine's `GitHub` client and overrides `api()` and
`archive()`:

- Reads (`GET`, and GraphQL documents that are queries) run directly with a
  read-only installation token.
- Writes on the allow-list (`POST`, `PATCH`, `PUT`, `DELETE`, and the one GraphQL
  mutation) become `gateway.mutate(key, generation, method, path, body)`; any other
  write raises before admission.

### Control exceptions

`OwnershipLost` (stale generation) and `Unresolved` (key has an unknown mutation)
stop the tick. They derive from a `GatewayStop` that subclasses `BaseException`,
not `Exception`, so they pass through every engine wrapper and best-effort path
untouched:

- `Checkpoints.request()` (which wraps `Exception` as `CheckpointError`),
  conversation publication, stage and attention label sync, health reporting and
  the shared failure handling all catch `Exception` only.
- The engine's `BaseException` handlers (workspace, snapshot and session
  cleanup) remove local containers and volumes and re-raise; none writes to
  GitHub.
- After raising a stop once, the gateway repeats it for every further call of
  that generation without admission, and records it (`gateway.stop(key,
  generation)`).
- Local cleanup (`engine/cleanup.py`) never replaces an exception already
  propagating: a removal that fails or times out while a stop is in flight is
  swallowed, and raises only on a normal exit.

The worker catches `GatewayStop` explicitly and also classifies by the gateway's
record, so no later exception can turn a stopped tick into an ordinary failure
with diagnostic writes. No engine change is needed beyond
keeping these properties, which tests pin.

### Transient errors

`engine/github.py` gains `Transient(RuntimeError)`, the one typed error for "not
applied, or its result not read; retry later". Its fixed `code` is one of:

- `throttled`;
- `unavailable`;
- `response_lost`: a write applied but its response was lost, so the next tick
  reconstructs it.

Its `retry_after` is the delay GitHub requested, in seconds, or None. The delay
comes from `Retry-After`, from `X-RateLimit-Reset` when the remaining quota is 0,
or is 60 seconds for a secondary limit with neither. It is at least 1 and is never
shortened. Its text is fixed per code, so it is safe to log.

- Engine wrappers that replace `Exception` re-raise `Transient` unchanged:
  `Checkpoints.request()`, `Checkpoints.collection()` and `conversation.publish()`.
  Label sync and health reporting still swallow it, since they are display only.
- Outcome lessons (`controller.py`) skip another issue's checkpoint only on
  integrity errors. A transient read now propagates, so model input never depends
  on a transient failure.
- `Transient` is neither a `Blocked` nor a `CheckpointError`, so no
  `except Blocked` or `except CheckpointError` handler catches it.
- The engine's `gh` transport never raises it, so the Actions path is unchanged.
- `service/failures.py` adds `classify(error, stop)`, the shared classifier. This
  step covers its first rows:
  - a recorded stop or a `GatewayStop` returns that stop;
  - `Transient` returns `Outcome("wait-until", retry_after=…)`, with no stage
    effect, no health notice and no further write. The delay is GitHub's
    `retry_after` when given. Otherwise it is local exponential backoff from 60
    seconds by attempt, capped at 3600 seconds; only this local backoff is capped.

  Step 3 adds the remaining rows.

A transient failure while publishing a comment leaves the events in the saved
checkpoint's `pending` outbox: the checkpoint is saved before publication, and
`pending` is cleared only after it succeeds. The next tick publishes them, and
recovery by marker prevents duplicates. No `/concurro retry` is needed.

### Transport

`service/transport.py` speaks HTTPS to `api.github.com` with `http.client`, with
one repository's token:

- Paths must be `repos/<repository>/…` or `graphql`; anything else is refused
  locally, before a token is used.
- Each request sends `Authorization: Bearer`, `Accept: application/vnd.github+json`,
  `X-GitHub-Api-Version: 2022-11-28` and a fixed `User-Agent`. There is no proxy,
  nothing read from the environment, and no automatic redirect.
- The token is fetched and the connection (TCP and TLS) is opened before any
  request byte is written. A failure there is `NotSent` (internal to the
  transport, surfaced as `Transient("unavailable")`); any failure after the
  request starts is ambiguous.
- Responses are bounded per endpoint, with timeouts of 45 s (120 s for an
  archive):

  | Endpoint | Limit | Basis |
  | ---- | ---- | ---- |
  | `issues/<n>/artifacts…`, read or write | 16 MB | Up to 31 bodies (current plus 30 versions) of at most 60,000 bytes (`checkpoints.LIMIT`), with worst-case 6× JSON escaping; 1,493,376 bytes measured for 24 KB bodies ([measurements](../artifact-measurements.md)) |
  | `git/trees/<sha>?recursive=1` | 16 MB | GitHub truncates recursive trees itself, and the engine refuses `truncated` |
  | Any other JSON | 4 MB | Covers a base64 blob of `repository.MAX_FILE` (2 MB) |
  | Archive | 100 MB | As in the engine |

  An oversized response is unreadable, never truncated. A regression fixture
  reproduces the measured artifact response and one at the worst-case bound.
- `request(method, path, data)` (reads) returns parsed JSON. A 404 raises
  `NotFound`. A 408, 429, 5xx, rate-limited 403, timeout or connection failure
  raises `Transient`. A GraphQL query answered with HTTP 200 whose errors report
  rate limiting (type `RATE_LIMITED`, or a message naming a rate limit) also
  raises `Transient("throttled")`, with the delay from the headers or 60 seconds;
  any other GraphQL errors are returned for the engine to handle. Anything else
  raises a `RuntimeError` naming only the method, path and status, mirroring
  `GitHub.api`. GraphQL mutation settlement is unchanged: any errors are
  `unknown`.
- `archive(path)` expects exactly one 302 to `https://codeload.github.com/` and
  follows it without the `Authorization` header, since the redirect carries its
  own token. Any other host, or a second redirect, is refused.
- `send(method, path, body)` (writes) returns a `Response` and never raises for an
  HTTP status: `sent` (false only for `NotSent`), `status` (None if no status
  line arrived), the `Retry-After`, `X-RateLimit-Remaining` and
  `X-RateLimit-Reset` headers, and `body` (bytes), or `body_error` when the body
  could not be read completely or exceeded its limit. The gateway parses the body
  and settles from all of these.

`MutationGateway(registry, writer, reader)` takes per-repository factories:
`writer(repository)` returns a transport with the write token, and
`reader(repository)` one with the read token. The gateway uses `reader` for its
issue-node lookup, and each facade receives only `reader(repository)`.

The worker never holds a write token: the facade has only the gateway handle. In
one process this is a code boundary, not an OS boundary; it becomes a process
boundary when the gateway is split out.

## Identity service

`Identity(settings, http)` extracts the pilot's JWT signing and installation-token
code into one module:

- **App JWT:** signed with `openssl dgst -sha256 -sign`, as in the pilot. It
  expires after 9 minutes and is re-signed 1 minute before that.
- **App identity:** `verify()` reads `GET /app` once at startup and records
  `bot_login` as the App slug plus `[bot]`. In step 3 the worker requires each
  target's configured `bot_login` to equal it.
- **Installation:** for each repository, `repository_selection` must be
  `selected` and the account login must equal the owner. The installation ID is
  cached.
- **Tokens:** `read_token(repository)` and `write_token(repository)` are each
  minted for that one repository, with exactly these permissions:

  | Kind | Permissions | Holder |
  | ---- | ---- | ---- |
  | Read | `contents`, `issues`, `pull_requests`, `checks`, `administration`: read | Facade reads; the gateway's node lookup |
  | Write | `contents`, `issues`, `pull_requests`: write | The gateway's `send` only |
  | Cutover | `actions`, `actions_variables`, `environments`: read | `adopt` only |

  `administration: read` covers branch protection and rulesets. No kind has
  `workflows`, so no candidate commit can change a workflow.

  The cutover token is used for the `adopt` checks: repository and `concurro`
  environment variables (`CONCURRO_ENABLED`) and the repository's workflow runs.
  It is minted for that one command, verified like the others, never cached, and
  revoked when the command ends. The App's registration must grant these
  permissions; without them, `adopt` is refused. The workflow file and its engine
  pin are read with the read token. The engine-repository compare runs
  unauthenticated, so `adopt` is refused if the engine repository is not public.
- **Verification after minting:** the granted permissions, without `metadata`,
  must equal the request exactly, and `installation/repositories` must list only
  that repository. Otherwise the token is revoked and minting fails.
- **Caching:** each token is cached until 10 minutes before its `expires_at`. One
  lock per repository and kind means concurrent callers mint once. A 401 drops the
  cached token. A refresh does not revoke the old token, since another thread may
  still be using it; `close()` revokes every cached token, best effort.
- **Failures** raise `IdentityError` with sanitized text. No token appears in an
  exception, a log or a registry record.

## Worker

For one key and generation:

1. Take an observation snapshot of the key (see Reconciler) before anything
   else.
2. Read the target's `.concurro/` at its default-branch head through the read
   token into a private temporary directory, validate the configuration, and
   require its `engine` field to equal the service's engine commit.
3. Build one `Bundle(engine_root, target=tmp, profiles=...)` and inject that same
   instance, the facade, and a facade-backed archive loader everywhere they are
   needed. This requires two small engine changes:
   - `Agents(config, archive_loader, credentials, bundle=None)` accepts an injected
     bundle instead of building one from the working directory;
   - `Controller(..., bundle=None)` accepts the same instance.

   Credentials come from an `EnvironmentCredentials` built over that target's own
   credentials file, never the process environment.
4. Construct `Controller(facade, agents, config, workspace_factory,
   bundle=bundle, controller_id=settings.controller_id)` and call `advance`.
5. Classify the result (see Failures), then acknowledge the step-1 snapshot only
   if the tick completed or blocked durably.

No per-task working-directory change or environment mutation is used.

## Failures

Failure handling is shared with `controller.main()` through one classifier. The
service maps each class to a stage effect and an outcome:

| Failure | Stage effect | Outcome |
| ---- | ---- | ---- |
| `OwnershipLost` (stale generation; `GatewayStop`) | None; no further write | None recorded; the new owner continues |
| `Unresolved` (unknown mutation; `GatewayStop`) | None; no further write | Key unscheduled until resolved |
| `Transient` on a read or write (throttled, or not sent) | None; stage preserved; no health notice | `wait-until`, `retry_after` or exponential backoff |
| Uncertain model outcome within its allowance | None; stage preserved | `wait-until` (the next tick makes the one permitted retry) |
| Checkpoint failure on verified, owned state | Health notice; stage preserved | `wait-until`, backoff |
| `Blocked` (policy, validation, budget, a second uncertain outcome) | Suspended to `blocked`, as today | `blocked` |
| Any other exception | Suspended to `blocked` with the fixed transport text | `blocked` |

Only the last two require `/concurro retry` or an operator. The Actions path keeps
its current handling (it retries only on its schedule) until it adopts the same
classifier in a later change.

## Reconciler

### Snapshot and acknowledgment

A snapshot holds, per key, only task inputs:

- human comment IDs with their `updated_at` (edits), and their ID set (deletions);
- the issue's state and a digest of its title and body;
- enrollment label events with their actors;
- the linked PR's head SHA, merged state, review IDs with their states, and
  check-run conclusions for that head.

Comments and label events authored by any Concurro identity are excluded. The
issue's own `updated_at` changes for those excluded writes too, so it is only a
rescan hint: it decides whether to fetch the inputs above, and a change in it
alone never enqueues work.

- The worker snapshots before its tick and acknowledges exactly that snapshot
  afterwards, never a fresh read, so input arriving during the tick is never
  acknowledged unseen.
- Events arriving during a tick (webhook or poll) mark the key dirty; when the
  tick ends, a dirty key is enqueued again.

### Polling

Every interval (default five minutes) and on `reconcile-once`:

1. Build the candidate set per target: issues currently labelled `concurro` plus
   every key the registry holds as active, so a task whose label was removed is
   still observed.
2. Snapshot each candidate and enqueue it when the snapshot differs from the
   acknowledged one, or its `continue` / `wait-until` outcome is due.
   `terminal` keys are skipped unless their issue is reopened.

Polling uses GitHub reads only; ticks (and model calls) run only for due work.

## Ingress

- Verifies `X-Hub-Signature-256` against the webhook secret before parsing; rejects
  anything else with no side effect.
- Accepts only `issues`, `issue_comment`, `pull_request`, `pull_request_review`,
  `check_suite` and `check_run` from enabled repositories and the controller App's
  installation.
- Maps each event to `(repository, issue)`: PR and check events through the
  registry's record of each task's PR.
- Drops events the Actions workflow already ignores and events authored by
  Concurro identities.
- Records the delivery ID; a redelivery is acknowledged without enqueueing.
- Enqueues with `due_at = now`, or marks the key dirty if a tick is running. It
  never reads task state or calls the engine.

The HTTP surface is a single POST endpoint plus a health endpoint. How it is
exposed (reverse proxy, tunnel, cloud ingress) is a deployment choice.

## Scheduler

- Work items are keyed by `(repository, issue)`; several causes for one key
  collapse into one item.
- Before a tick, the gateway's `acquire` grants a generation (see the critical
  section); keys with unresolved mutations are skipped.
- Concurrency: at most one tick per key, `limits.repository` per repository and
  `limits.global` overall, on a bounded worker thread pool.
- Fairness: due items are taken round-robin across repositories, oldest first
  within a repository.
- After the tick, `release` records the outcome; `continue` re-enqueues
  immediately, `wait-until` at its time, others only on a snapshot change.
- Leases last 15 minutes and are renewed during long local stages. Expiry affects
  liveness only: a replacement generation still requires no mutation in flight.

## Cutover and release

`adopt <repository> [issue…]`:

1. Require the target's Actions controller to be marker-aware: its workflow's
   engine pin must equal or descend from the step-3 commit (compared through the
   engine repository's compare API).
2. Require `CONCURRO_ENABLED` to be `false` and no Actions controller run in
   progress for the repository.
3. For each task (all enrolled issues if none are given): acquire ownership, load
   its state through the facade, require marker `actions`, write the marker
   `service:<name>` through the gateway, then explicitly enqueue it with a fresh
   snapshot.

`release <repository> <issue>` writes the marker back to `actions` for an idle key
without unresolved mutations, so the operator can roll back before re-enabling
Actions.

## Tests

All with fake GitHub and fake providers; no network, Docker or credentials.

| Scenario | Expectation |
| ---- | ---- |
| `acquire` racing a mutation between validation and admission | Impossible by construction: the stale write is rejected or the grant waits |
| Takeover while the old worker survives | Every old-generation write rejected; no write after `OwnershipLost` |
| Stage labels through `intake → plan → intake`, using the engine's real label sync | Each transition sends its writes; the final label is `intake` |
| Attention label through failure, recovery, failure | Each change sends its write; the label ends present |
| `OwnershipLost` raised inside checkpoint save, conversation publication, label sync and health reporting (real engine paths) | Reaches the worker unwrapped; no further mutation attempted |
| `OwnershipLost` inside the workbench, then a cleanup timeout | The stop still propagates and is recorded by the gateway; no further mutation attempted |
| Write not on the allow-list | Refused before admission |
| HTTP 500 or timeout on a write | Recorded `unknown`; key unresolved and unscheduled |
| HTTP 422 on a write | Recorded `failed`; normal engine handling |
| Applied-but-unknown comment, artifact and PR creation, resolved `applied` | Next tick finds each object and continues without a duplicate |
| `resolve` applied or not-applied | Disposition and evidence recorded; key schedulable; `release` refused before it |
| Second process opening the state directory | Refused; operator commands work only through the daemon |
| Missing or mismatched store at startup | Refuses to start without explicit initialization |
| Crash between transaction commit and action | Restart sees the committed record and recovers accordingly |
| Two targets ticking concurrently | Each task sees only its own configuration, bundle identity, archive and credentials |
| Duplicate and redelivered webhooks, including during a tick | One tick per change; the dirty flag schedules a follow-up |
| Bad signature, foreign repository or installation | Rejected with no state change |
| Lost webhook for a clarification, approval, review, retry, comment edit or deletion | Reconciler enqueues each; no model call without a change |
| Input arriving during a tick | Not acknowledged; a follow-up tick processes it |
| Enrollment label removed from an active task | Still observed and advanced (the engine decides) |
| Concurro-authored comments and labels | Never wake a task |
| Issue `updated_at` changes with no input change | Rescan only; nothing enqueued |
| Waiting tasks (`await-human`, `blocked`, `wait-until` before due) | Not dispatched; no model calls |
| Settings with an unknown field, wrong type, readable or symlinked file, path in the engine checkout, repeated target or out-of-range limits | Refused at load |
| App slug, all-repositories installation, foreign owner, or granted permissions or repositories differing from the request | Minting fails; any token minted is revoked |
| Read and write tokens | Each narrowed to one repository with exactly its permissions; concurrent callers mint once; re-minted near expiry; dropped after a 401; revoked on `close()` |
| Two targets through the transports | Each repository's requests carry only its own token; a foreign path is refused before a token is used |
| Write over a loopback server: connection refused | Not sent; the occurrence is `failed`, `Transient` is raised and the key is not unresolved |
| Write over a loopback server: connection closed after the request, or a 5xx | `unknown`; the key is unresolved |
| REST write over a loopback server: 2xx with a truncated body | `done`, then `Transient("response_lost")` |
| GraphQL 200 with `errors` of any type, a null result, or an unreadable body | `unknown`; the key is unresolved |
| Artifact responses: the measured 1,493,376-byte fixture, and one at the worst-case bound | Read successfully; one byte over the endpoint limit is refused |
| `Transient` raised inside checkpoint requests, the artifact collection and conversation publication (real engine paths) | Reaches `classify` unwrapped with its code and `retry_after`; result `wait-until` |
| Transient failure publishing a conversation comment, then a clean tick | Pending events kept in the checkpoint; published once on the next tick, with no `/concurro retry` |
| Transient read of another issue's checkpoint while gathering outcome lessons | Propagates; the lesson is not silently skipped |
| Write responses: 401, throttled 403, and a 429 | `failed` with `Transient` for the first two; `unknown` for the 429 |
| A 403 carrying only ordinary quota headers | `Rejected`, not throttled |
| `Retry-After: 7200`, and a reset two hours away | `retry_after` 7200 each; local backoff alone is capped at 3600 |
| `classify` on a `Transient` | `kind == "wait-until"`, `progressed` false, and `retry_after` set |
| Comment and PR creation applied with a truncated 2xx body, then a clean tick | `done`, `Transient("response_lost")`, `wait-until`; the next tick finds each object, with no duplicate and no `/concurro retry` |
| `adopt` without the cutover permissions | Refused before any write |
| GraphQL query rate-limited with HTTP 200 (`RATE_LIMITED`, or a secondary-limit message), through the real transport and `branch_linked` | `Transient("throttled")` with the header delay; `wait-until`; the next tick resumes |
| Settings paths with `.` or `..`, a symlinked directory or ancestor, a symlinked or missing `state_dir`, a hard link, or one secret file named twice | Refused at load |
| Archive redirect | Followed once to codeload without `Authorization`; another host or a second redirect is refused |
| Oversized response | Refused |
| GitHub read failure or an uncertain model call mid-tick | Stage preserved; `wait-until`; resumes without `/concurro retry` |
| Cutover with a marker-aware Actions controller re-enabled | Actions makes zero writes to adopted tasks |
| Cutover refused | Non-marker-aware pin, `CONCURRO_ENABLED=true`, or a running Actions job |
| Fairness and limits | Round-robin across repositories; limits never exceeded |
| End to end | A fake-provider task advances from intake to `gates` through the service, matching the Actions path |

## Decisions

Accepted in review:

1. Engine version: targets name an explicit engine commit in a new optional
   `.concurro/config.json` field `engine`; the service serves a target only when it
   equals its own engine commit.
2. `service/` lives in the `concurro` repository.
3. One process, with a thread pool and in-process per-key critical sections.
4. Ambiguous writes are resolved by an operator, per attempt, with recorded
   evidence.
5. Leases of 15 minutes, renewed during long stages; liveness only.

Revision 4, accepted in review:

6. A failure while explicitly opening the connection, completed before any request
   byte is sent, counts as not sent. A failure during transmission is uncertain.
7. No GraphQL error type counts as a rejection. Without a GitHub guarantee that
   nothing changed, any GraphQL response with errors is unknown.
8. The pilot keeps its own token code until it is retired at cutover. Only the
   private-file helpers are shared, in `engine/private.py`.

Open:

- Whether the Actions path adopts the shared failure classifier in this step or a
  later one.
