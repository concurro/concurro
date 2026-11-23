# S4: Rust exchange and runner API v2

Part of the [phase 5 plan](../phase-5-plan.md). The exchange is deployed inactive and
takes traffic at S7. It replaces `gateway/exchange/exchange.py`. The inference forwarder
in `gateway/exchange/inference.py` is replaced by the [gateway](s5-gateway.md) together
with the exchange's admission API.

## Implementation work order

S4 is implemented in four deliverables. These are integration and review checkpoints,
not separate production cutovers. They may merge and deploy inactive; S7 still switches
traffic once the exchange, gateway, task application and runner are ready together.
Authorization, idempotency and recovery belong with the workflow that introduces them.

### Ownership and parallel work

- The S4 owner owns the exchange operator, admission and runner APIs, turn payload v2,
  `exchange.turn.*` and `inference.usage`, including shared types and clients.
- The S3 owner owns credential metadata and `credential.<user_id>.changed`. Changes to
  another owner's family are agreed with that owner before implementation. Producers
  and consumers use the same shared contracts; fixtures do not invent alternate shapes.
- S3 proceeds independently on accounts' store, metadata, history and import. Relay
  approval then integrates S1's owner API.
- After S4a, S5's org gateway can proceed alongside S4b using the admission client and
  fixtures. S6's org submit/result adapter can proceed against the operator client and
  exchange event fixtures. Neither waits for all contributed or relay behavior.
- Before S4c integration, assign owners for its three pieces: exchange admission and
  settlement (S4), org gateway routes (S5), and task Rust-mode submits, results and blob
  ingestion (S6). S4c coordinates those pieces rather than waiting for all of S5 or S6.

### S4a: operator workflow and shared contracts

Deliver a working operator workflow against PostgreSQL and the shared outbox:

1. Define the exchange contract families in `concurro-contracts`, generate schemas and
   fixtures, and implement the operator and admission HTTP clients. Admission covers
   `requests`, `requests/{id}/finish` and `sessions/status` now, even though its server
   behavior lands in S4c. Exercise the clients against contract fixtures; do not use
   client methods that merely return an unimplemented error.
2. Implement grant validation and immutable grant IDs, canonical turn-body digests,
   submit replay, observation reads and initial session allocation with its dispatch.
3. Implement cancellation/retirement, durable turn tombstones, and grant-revocation
   fences, including revocation before first submit. These apply to org grants too.
   Preserve terminal results and the complete retired-session set through replay and
   pruning. Relay release delivery itself lands in S4d; do not report pending releases
   as acknowledged.
4. Append status snapshots in the same transaction as their state changes. Use the
   existing database, outbox and event infrastructure. Wire the operator listener and
   settings so the workflow is usable through the real HTTP API.

**Exit:** through the real operator client and HTTP API, submit an org turn, obtain and
read its dispatch, replay it unchanged, reject changed contents, then cancel it. Cover
cancel-before-submit, revoke-before-submit, stable retirement results after pruning,
restart persistence, bearer isolation and transactional event rollback. Admission
clients round-trip the agreed fixtures. Keep tests representative and focused.

**Review:** a focused contract/API review, including Opus, at the completed checkpoint.
This unblocks consumers; it does not claim that runner execution works yet.

### S4b: runner execution and blobs

Implement OIDC claim and engine-key handshake, deterministic token replay, heartbeat,
next-turn delivery, content-addressed uploads/downloads, result submission and session
end. Add the runner liveness/replacement behavior used by this workflow. Complete the
runner and event types from S4a without changing their agreed semantics.

This deliverable intentionally includes the migration changes to `engine/relay.py`,
`engine/runner.py`, `engine/turns.py` and the launcher workflow. It changes transport,
not engine behavior, and is permitted Python migration work under `AGENTS.md`.

**Exit:** the real engine runner completes a canned turn against the Rust exchange and
fake inference, including blobs and result replay. Verify lost claim reply across
restart, OIDC refusals, cancellation, and representative liveness behavior. A late
retirement of a prior turn must not end a shared session serving a subsequent turn;
propagate active-turn ownership or avoid session reuse. Run the
relevant tests here; fold the consolidated review into S4c.

### S4c: org-funded execution end to end

Implement the admission server, request accounting and settlement, alongside the S5 org
routes and S6 org integration. Prove this path before completing contributed or relay
work:

```text
task app -> exchange -> v2 runner -> org gateway -> provider fixture
         <- exchange.turn.* and result blobs <- runner
```

Core failure handling is part of this deliverable:

- budget denial is recorded and ends the turn;
- admission refuses a failed turn and a cancelled or revoked session/grant;
- finish reports are idempotent and usage is attributed;
- the settlement deadline and `provider_requests` distinguish observed, undispatched
  and unreported requests;
- failed results wait for their requests to settle;
- cancellation/revocation closes gateway streams within the agreed bound;
- result events and reconciliation settle the task once and preserve its transcript,
  retained archive and publication input.

**Exit:** an org turn completes across the real services in the integration harness,
with fake provider responses and the existing read-model ingestion. Representative
failure/recovery cases exercise the rules above. No real paid turn is required.

**Review:** one consolidated integration review, including Opus, across S4b/S4c and the
participating S5/S6 changes. This is the main integration checkpoint.

### S4d: contributed funding and relay completion

Complete personal-grant handling and credential-failure attribution, and integrate S3's
metadata and S6's credential-version revocation markers, selection and fallback. Add
signed relay claims, durable release retries and S1 verifier integration. Generic grant
fencing and org-path failure handling are already implemented in S4a/S4c.

**Exit:** contributed selection, failure followed by fallback, credential replacement,
withdrawal and relay claim/release pass through the integrated services. Run the
remaining S4 acceptance cases below and relevant changed-component suites.

**Review:** one consolidated final review, including Opus. Report remaining S7 release
and live-cutover work separately; local acceptance does not authorize deployment.

## Crates

- **`crates/exchange`:** the binary, with three listeners and modules `runner`
  (runner API v2, OIDC), `operator`, `admission`, `queue` (staffing and liveness),
  `claims`, `blobs`, `events` and `reconcile`.
- **`crates/exchange-postgres`:** migrations and all SQL, on `concurro-db` units of
  work.
- **`crates/exchange-client`:** the operator client for the task application and the
  admission client for the gateway.
- **Wire types** in `concurro-contracts`, with generated schemas and Python models. The
  engine's runner client uses those models.
- **Shared:**
  - `concurro-events-postgres` and `concurro-events-nats`: the outbox and the
    `EXCHANGE` stream;
  - `concurro-secrets`: the claim signing key; the verifier release bearer is a
    private file setting;
  - `object_store`, as the task application configures it, for blobs;
  - the PostgreSQL release ledger and periodic exchange worker, for verifier
    releases across process restarts.

## Listeners

| Listener | Network | Callers | Auth |
| ---- | ---- | ---- | ---- |
| Runner API v2 | Edge, at `/concurro/runner/*` | Launcher workflows | OIDC once, then a runner token |
| Operator API v1 | Platform | Task application | Its bearer, from a file |
| Admission API | Private network shared with the gateway | Gateway | Admission bearer, from OpenBao |

## Storage

| Table | Holds |
| ---- | ---- |
| `grants` | `grant_id`, `purpose`, `payer`, `credential` (route, record ID, version, custody, entry ID, relay ID; or the org route), `models`, `limits`, `status`, `revoked_at` |
| `turns` | `turn_id`, `attempt_id`, `task_id`, `grant_id`, `digest`, `staffing_group`, `expected_claims`, `models`, `budget`, `lease`, `payload`, `status`, `session_id`, `result`, `provider_failure`, `provider_requests`, `created`, `started`, `finished` |
| `turn_tombstones` | `turn_id`, `digest` (null for a cancel that came before the submit) |
| `turn_sessions` | Every session that served or was allocated to a turn |
| `sessions` | `session_id`, `staffing_group`, `grant_id`, `expected_claims`, `run_id`, `run_attempt`, `engine_sha`, key and runner-token hashes, `models`, `budget`, `spent_tokens`, `created`, `bound_at`, `last_seen`, `deadline`, `ended`, `end_reason`, `refusal` |
| `oidc_jti` | Used token IDs until they expire |
| `inference_requests` | `request_id`, `session_id`, `turn_id`, `grant_id`, `model`, `started`, `finished`, `category`, `usage` (or null with a reason) |
| `relay_claims` | `claim_id`, `session_id`, `kid`, `expires`, `release_status` |
| `blobs` | `sha256`, `size`, `uploaded_by`, `stored_at` |

Key and token values are stored only as SHA-256 hashes. They can be recovered without
being stored ([below](#runner-api-v2)).

## Operator API v1

[Contract](../../contracts.md#exchange-operator-api).

- **`PUT /v1/turns/{turn_id}`: submit.** The body holds the turn, its attempt and task,
  the grant, the staffing group, the expected OIDC claims, models, token budget, lease,
  and turn payload v2.
  - The digest is taken over the canonical body. The same digest returns the current
    observation; a different one is `409`.
  - A tombstone answers `410 turn_expired` (same digest) or `410 turn_cancelled` (no
    digest).
  - Admission requires an `issued` grant whose models include the turn's models, a
    budget and lease within its limits, and for contributed grants a credential.
  - A grant ID resubmitted with different contents is `409`.
  - The reply includes `dispatch {session_id}` when the turn needs a new session.
- **`GET /v1/turns/{turn_id}`:** the observation, used for reconciliation. It isn't
  the primary path for results.
- **`DELETE /v1/turns/{turn_id}`: cancel or retire,** with today's semantics
  (`exchange.py:1441`):
  - a terminal result is kept;
  - an unknown ID gets a fence tombstone;
  - every session in `turn_sessions` is retired, and the full set is returned, also on a
    retry after pruning;
  - a partial failure answers `502` and can be retried.
- **`POST /v1/grants/{grant_id}/revoke`:**
  - Marks the grant revoked, refuses new admissions under it, and ends its sessions.
  - Queues a verifier release for each of its relay claims, and answers once every
    release is acknowledged as durable.
  - It returns the sessions, and a repeat returns the same answer.

## Staffing and sessions

Today's rules are ported (`exchange.py:326, 1143, 1355`):

- **The staffing group** is a digest of the claims (less the ref), the binding, models,
  budget and lease.
- **Queueing:** FIFO within a group, one running turn per session.
- **New sessions.** A new session is issued when no live session exists for the group.
- **Liveness:** 1,200 seconds to bind, then a heartbeat within every 180 seconds, and a
  deadline of creation time plus the lease.
- **A dead, unbound session is replaced** by a new one, and the status snapshot carries
  the new `dispatch` and the group's latest refusal.
- **Pins and unbound legacy sessions aren't ported:** the task application never uses
  them.

Under the task application, each session serves one turn: it retires a turn's sessions
once the turn is terminal. The queue still supports a group serving successive turns, as
today.

## Runner API v2

[Contract](../../contracts.md#runner-api).

| Call | Auth | Behavior |
| ---- | ---- | ---- |
| `POST /runner/v2/sessions/{id}/claim` | OIDC | Applies today's claim rules (below) and binds the run ID and attempt. Returns `{engine {repository, sha}, runner_token, deadline}`, the engine coming from the turn's `binding`. A repeat claim from the same run returns the same runner token |
| `POST /runner/v2/sessions/{id}/key` | Runner token | `{engine_sha}` must equal the named engine, or the refusal is recorded and `409` returned. Returns `{session_key, inference_base_url, models, deadline, binding}` |
| `POST /runner/v2/sessions/{id}/heartbeat` | Runner token | `204`; `410` once the session has ended |
| `GET /runner/v2/sessions/{id}/turns/next?wait=` | Runner token | The next turn, `{turn_id, payload, deadline}`, or `204` after up to 50 seconds. Also a heartbeat |
| `PUT /runner/v2/sessions/{id}/blobs/{kind}/{sha256}` | Runner token | Content-addressed upload, verified against the digest, at most 256 MiB, stored create-only in the object store |
| `GET /runner/v2/sessions/{id}/blobs/{sha256}` | Runner token | Only blobs this session's turns reference, such as the retained archive |
| `PUT /runner/v2/sessions/{id}/turns/{turn}/result` | Runner token | At most 1 MiB of JSON, referencing uploaded blobs, which must exist. Accepted only while this session runs the turn. Repeating the same result succeeds; a different result is `409` |
| `DELETE /runner/v2/sessions/{id}` | Runner token | Ends the session; `410` counts as success |

**OIDC rules** are today's (`exchange.py:147, 906`):

- RS256 against GitHub's cached JWKS, issuer, audience `concurro-gateway`, 60 seconds of
  skew, issued within 600 seconds, single-use `jti`.
- The expected repository ID, owner ID and `job_workflow_ref`, `workflow_dispatch`,
  ref `refs/heads/concurro/runs/<session>`, `github-hosted` runners and run attempt 1.

A repository or owner mismatch is refused without recording; other mismatches record
the session's refusal. v2 needs one OIDC token per session instead of two.

**Session keys and runner tokens are derived, not random.** Each is
HMAC-SHA256 under the exchange's token key, read from OpenBao at
`ops/exchange/token-key`:

- the runner token over `runner/<session>/<run_id>/<run_attempt>`;
- the session key over `key/<session>`.

They are prefixed `rt-concurro-` and `sk-concurro-`, and only their hashes are stored.

- **A repeat claim from the same run** (a new OIDC token with a new `jti`) derives the
  same runner token. So does one after a lost reply and an exchange restart.
- **A different run** is refused, because the session is bound to its first run.
- **A reused `jti`** is refused.
- **Rotating the token key** invalidates the tokens of live sessions, so it is done
  only while idle.

**Turn payload v2** is payload v1 with its base64 fields replaced:

- **To the runner:** `{request, feedback, spec, timeout, retained: {sha256, size} | null}`.
- **Back:** `{outcome, response, termination_reason?, elapsed_seconds?, workspace:
  {sha256, size} | null, transcript: {sha256, size} | null, retained: {sha256, size} | null}`.

The engine executes the same request ([decision 5](../phase-5-plan.md#decisions)).

## Admission API, for the gateway

[Contract](../../contracts.md#exchange-admission-api).

- **`POST /internal/v1/requests`:** `{key, model, request_id?}`.
  - It authenticates the key's hash and checks:
    - the session is live and has exactly one running turn;
    - the grant is issued and the model allowed;
    - `spent_tokens` is below the budget.
  - It records the request and returns:
    - `{request_id, session_id, turn_id, grant_id, credential, models, remaining_tokens,
      deadline}`;
    - for relay custody, also a signed claim, issued once per session and reused.
  - Refusals carry today's categories. A refusal for `budget_denied` is recorded as a
    finished request, which sets the turn's `provider_failure`, as today's native budget
    denial is recorded against the request (`inference.py:427-436`).
  - Once the turn has a `provider_failure`, every later admission for it is refused, as
    today (`inference.py:100-101`).
  - `probe: true` (the gateway's `GET /v1/models`) checks the session and returns its
    models without recording a request, as today's probe (`inference.py:92-93`).
- **`POST /internal/v1/requests/{id}/finish`:** `{dispatched, category, usage | null,
  usage_unavailable?, retry_after?}`.
  - **`dispatched`** is true once the request was sent to the provider or the verifier.
    A local failure, such as a destroyed version in OpenBao, is
    `dispatched = false, category = auth`. It is still a trusted credential failure,
    because the version is unusable, but it isn't a provider request.
  - Idempotent.
  - Adds tokens to `spent_tokens`, sets the turn's `provider_failure` from the first
    terminal failure, and appends `inference.usage`.
- **`POST /internal/v1/sessions/status`:** `{session_ids}` → the ones still live. The
  gateway calls it every 10 seconds for its open streams ([gateway](s5-gateway.md)).

**Settlement deadline.** A request still open five minutes after the first of these is
closed with `usage_unavailable = not_reported`, and its fact appended:

- its turn's result arriving;
- its session ending.

Five minutes is the gateway's retry window for finish reports. A finish arriving after
the deadline is acknowledged and logged, but changes nothing.

**`provider_requests`:**

- `some` if any request was dispatched, even if another was closed `not_reported`. The
  usage summary counts those separately as unavailable.
- `none` if every request finished undispatched.
- `unknown` if none was dispatched and at least one was closed `not_reported`.

**Results wait for their requests.** A non-`ok` result isn't published as `finished`
until each of the turn's requests is finished or reaches the settlement deadline. That
way credential fallback sees
the failure, as today's poll does (`exchange.py:1194-1199, 1420-1422`). Meanwhile the
status snapshot carries any `provider_failure` as soon as it is recorded, and the task
application ends the attempt on it, as it does today
(`crates/tasks-postgres/src/effects.rs:540-554`).

**Budget overshoot.** Usage is counted after a response, so requests in flight together
can overshoot the budget, as they can today.

## Claims

The exchange signs relay claims with its Ed25519 key from OpenBao, as an EdDSA compact
JWS with a `kid` ([contract](../../contracts.md#session-claims)):

- **Scope:** the session, relay, user, entry, record, version, route and models.
- **Expiry:** the session's deadline.
- **Allowance:** `calls` from settings (default 1,000 per session) and `tokens` from the
  session's budget.

**Release.** A grant revocation or session end releases the session's claims through the
verifier's `release` credential, by a job retried until the verifier acknowledges it.

## Events

All appended in the transaction that makes the change
([contract](../../contracts.md#exchange-events)):

- **`exchange.turn.<turn>.status`:** a snapshot of status, session, run ID, a
  `dispatch` when a new session is needed, the refusal, and timestamps. Its revision
  increases per change.
- **`exchange.turn.<turn>.finished`:** a fact with the result, blob references,
  `provider_failure`, `provider_requests` (`some`, `none` or `unknown`, from request
  records), and a usage summary.
- **`inference.usage`:** one fact per finished request.

## Reconciliation

Every 60 seconds, and at startup:

- end dead sessions and close their open requests;
- turn sessions lost to death or revocation into lost turns;
- prune turns finished over a day ago into tombstones.

A separate exchange worker retries pending verifier releases, pausing one
second between scans until S1 acknowledges them. Keep its signing, verifier URL
and release bearer settings configured while claims await release.

Tombstones and session rows are kept, as today.

## Launcher and engine

- **`.github/workflows/agent.yml`** at the release tag claims once with an OIDC token,
  fetches the engine commit, gets the key, and runs the engine with v2.
  - The inputs and the `CONCURRO_GATEWAY_URL` secret are unchanged. The runner derives
    `/runner/v2` from that URL, and the `key` response names the inference base URL.
- **`engine/relay.py` and `engine/runner.py` move to v2:**
  - the heartbeat route replaces `next(0, 0)`;
  - the retained archive is downloaded by digest;
  - the transcript, retained archive and workspace are uploaded as blobs before the
    result;
  - the result keeps up to five attempts with backoff.
- **`engine/turns.py`** executes as today and writes files instead of base64.
- **Rollout.** Each target fork's `concurro-executor.yml` calls `agent.yml@<tag>`, and
  the task application's `workflow_ref` names the same tag. Each target's `engine_sha`
  names the engine commit the exchange hands the runner. All three change in S7's
  window. The v2 engine speaks only v2, and no target stays on v1.

## Probe

`exchange probe --payload <file> --expected-claims <file>` creates a session, an org
grant and one turn from a submit payload the operator exports from the task database. It
prints the session ID. The operator creates the run ref and dispatches the fork's
launcher, and the probe waits for the result.

The operator, not the App, dispatches it, so its expected claims name the operator's own
user ID as `actor_id`.

It's optional. It proves OIDC, runner v2 and the gateway's org route against GitHub
before the window, without the task application.

## Network isolation

**Evaluated: runners reaching the exchange and gateway over a tailnet,** joined with the
Tailscale GitHub action, OIDC workload identity and ephemeral tagged nodes.

- **For:** it would take pre-authentication code off the internet.
- **Against, for now:**
  - It adds a Tailscale account, tailnet ACLs and a per-job join on every runner.
  - It adds a third-party dependency to every turn.
  - It doesn't make an authenticated runner less dangerous.

**Decision:** not in phase 5. It stays in
[later hardening](../../architecture.md#later-hardening), and the runner listener keeps
its OIDC and token checks.

## Acceptance

- **Operator API** against PostgreSQL:
  - submit idempotency;
  - `409` and both `410`s;
  - cancel before submit;
  - cancel returning the full session set after pruning and after a lost reply;
  - grant revocation ending sessions and answering only after its releases are
    acknowledged by a fake verifier.
- **OIDC:** recorded tokens are refused for each claim rule and for a reused `jti`.
  Refusals are recorded except for repository and owner mismatches.
- **Lost claim reply:** after a claim's reply is lost and the exchange restarts, a new
  claim from the same run returns the same runner token, and the session key from `key`
  is the same. A claim from another run is refused.
- **Grant fence:** a revoke for a grant never submitted answers, and the later submit is
  `410 grant_revoked`.
- **Settlement:** a finish arriving before the settlement deadline is recorded; one
  after it is ignored, and the request stays `not_reported`. A turn with one dispatched
  request and one `not_reported` request reports `provider_requests = some`, and its
  usage summary counts one request as unavailable.
- **Runner v2 end to end:** the real Python engine runner, in a test, against the
  exchange, with a fake gateway and a canned turn:
  - claim, key, heartbeat, next, blob download and upload, result and end;
  - result idempotency;
  - a result after the turn was cancelled is `409`;
  - the engine's five-attempt retry against injected failures.
- **Liveness:** a session that never binds is replaced and re-dispatched with its
  refusal. A missed heartbeat ends the session and loses the turn.
- **Admission:** budget refusal after usage reaches the budget; model refusal; refusal
  after grant revocation; and the request record and `inference.usage` fact appended
  once per finish, however often the finish is retried.
- **Events:** one status snapshot per change, one `finished` fact, and nothing appended
  on a rollback.
- **Claims:** a verifier built from S1 accepts the exchange's claim. After release and a
  verifier restart, the claim is refused.
