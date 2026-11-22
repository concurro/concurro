# S4: Rust exchange and runner API v2

Part of the [phase 5 plan](../phase-5-plan.md). The exchange is deployed inactive and
takes traffic at S7. It replaces `gateway/exchange/exchange.py`. The inference forwarder
in `gateway/exchange/inference.py` is replaced by the [gateway](s5-gateway.md) together
with the exchange's admission API.

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
  - `concurro-secrets`: the claim signing key and the verifier release credential;
  - `object_store`, as the task application configures it, for blobs;
  - `graphile_worker`, for verifier releases.

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
- prune turns finished over a day ago into tombstones;
- retry outstanding verifier releases.

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
