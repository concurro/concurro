# Phase 5 plan: exchange, inference gateway and secrets in Rust

**Status: plan, not implemented.** Phase 5 of the [migration](../architecture.md#migration),
planned against the deployed phase 4 system (backend `6e04c6b`, frontend `8a65807`).
Each slice has its own brief in [`phase-5/`](phase-5/).

## Outcome

Phase 5 retires the Python exchange, its inference forwarder, agentgateway and the
Python vault. In their place:

- **A Rust exchange** with its own Postgres database: runner sessions, the turn queue,
  grants as the exchange sees them, per-request inference records and usage, runner
  API v2, and turn results published as events.
- **A Rust inference gateway,** a separate stateless process. It authenticates session
  keys against the exchange, injects org or personal credentials, forwards to providers
  or the relay verifier, and reports usage.
- **The accounts service** owns credential metadata in its own database and writes
  personal material to OpenBao, which it cannot read back.
- **OpenBao** holds personal credential versions and the operational secrets that are
  routinely rotated.
- **The relay verifier** gets separate caller credentials, its owner API v1, and signed
  session claims with persisted revocation and allowance. It ships as a `concurro-relay`
  release.

Unchanged:

- The task application keeps consent, selection, grants, budgets and accounting in its
  funding module.
- The read API keeps its privileges.
- The runner stays the Python engine, on runner API v2. A Rust runner is a separate,
  later decision.

## Decisions

Each decision names the older proposal text it replaces, where there is one.

1. **One coordinated cutover.** The Python exchange resolves personal material through
   the vault's resolver and checks the vault's metadata, so credentials can't move to
   accounts and OpenBao while it serves traffic. A new gateway can't authorize against
   the Python exchange's sessions without a Python bridge, and runner API v1 exists only
   in the Python exchange. So credentials, the exchange, the gateway and runner v2 switch
   together in one idle window. Everything else ships before it
   ([deployability](#deployability)).
2. **Startup-selected modes, never dual-running.** Until the cutover, accounts
   (`credential_store = "vault" | "native"`) and the task application
   (`exchange = "python" | "rust"`, with the metadata source following it) choose their
   backend at startup. That keeps `main` deployable for fixes before the window. Nothing
   runs both paths at once, and the retirement slice deletes the old ones.
3. **The gateway asks the exchange per request.** It stays a separate stateless process.
   For every inference request it calls the exchange's internal admission API. That
   authenticates the session key, checks the session, turn, grant, models and remaining
   budget, records the request for attribution, and returns the credential reference.
   - On finishing, the gateway reports the outcome and usage.
   - While a stream runs, it rechecks its sessions every 10 seconds and closes revoked
     ones. A session it can't check for 30 seconds is closed too, so the worst-case bound
     is 40 seconds.
   - This replaces the event-driven `session.<session>.binding`,
     `inference.binding.closed` and `exchange.grant.released` sketches, along with
     their late-binding races (old scenarios 15 and 16).
   - "Spending stopped" is the exchange's acknowledged revocation plus the stream
     recheck bound, as phase 3's time bound was. The owner accepts that tolerance.
4. **Sponsorship slots retire.** The exchange admits a turn only under the grant
   submitted with it, and the task application revokes grants through the operator
   API. Today's slot transitions, generations and epochs are a Python bridge, and the
   funding module already enforces cutoffs when it inserts a grant.
5. **Runner API v2 changes the transport, not the turn.** v2 brings:
   - one OIDC exchange for a runner token;
   - an explicit heartbeat;
   - content-addressed blobs instead of base64 inside results;
   - a result that references those blobs.

   The turn's content stays payload v1's `request`, `feedback`, `spec` and `timeout`,
   which the engine already executes. The redesigned turn spec in the older sketch
   belongs to a future Rust runner and is withdrawn. The Rust exchange serves v2 only,
   and every target moves to the v2 launcher in the cutover window.
6. **The exchange publishes `exchange.turn.*` for the task application.** The task
   application keeps owning turn records, transcripts and the read API's `turn.*`
   events, so the read API doesn't change. This replaces "the exchange publishes
   `turn.*` itself".
7. **Blobs live in the object store** under the task application's existing
   content-addressed layout (`<prefix>/sha256/<digest>.tar.gz` for archives, `.json` for
   the workspace). The exchange writes runner
   uploads and serves blobs back to runners; the task application reads them for
   ingestion.
8. **Usage flows gateway → exchange → `inference.usage`.**
   - The funding module records usage per attempt.
   - Accounts updates `last_used` from the same facts.
   - Org traffic gets attributed usage for the first time.
   - When usage is genuinely unknown, the record says so instead of zero.
9. **OpenBao 2.7 on PostgreSQL storage, with the static seal.** OpenBao gets its own
   database and role. The seal key is a file outside Postgres. Declarative self-init
   creates the mounts, policies, AppRoles and audit device. Rust clients use `vaultrs`
   0.8.
10. **What moves into OpenBao:**
    - Personal credential versions.
    - Org provider credentials, which are rotated routinely.
    - Phase 5's new service credentials.

    Bootstrap material stays in files: the seal key, OpenBao's storage URL, each service's
    AppRole secret ID, database URLs, NATS credentials and the GitHub App key.
    ([OpenBao brief](phase-5/s2-openbao.md)).
11. **Usage parsing is ported from `gateway/provider_protocol.py`** into the gateway.
    `concurro-relay`'s `provider-protocol` has different responsibilities: it validates
    and audits the verifier's native path. So the older plan to reuse it is withdrawn.
12. **Codex WebSocket keeps `responses-bridge`,** pointed at the Rust gateway instead
    of agentgateway. Native WebSocket support in the gateway is later work.
13. **The relay release deploys first.** It accepts relay protocols 4 and 5, so
    installed Sponsors keep working. It splits the three caller credentials, adds owner
    API v1, persists claim revocation and allowance, and verifies signed claims. Claims
    stay optional until the cutover makes them required.
14. **No runner network isolation in phase 5.** The tailnet option is evaluated and
    deferred ([exchange brief](phase-5/s4-exchange.md#network-isolation)).

## Slices

| Slice | Delivers | Brief |
| ---- | ---- | ---- |
| S0 | Contract decisions and feasibility spikes, on top of the recorded phase 4 baseline | [s0](phase-5/s0-baseline.md) |
| S1 | `concurro-relay` release: protocol range, caller credentials, owner API v1, claims, persisted revocation and allowance | [s1](phase-5/s1-relay.md) |
| S2 | OpenBao and the shared secrets client | [s2](phase-5/s2-openbao.md) |
| S3 | Accounts credential store, OpenBao writes, metadata API, change facts, vault import | [s3](phase-5/s3-accounts.md) |
| S4 | Rust exchange, runner API v2, launcher and engine client | [s4](phase-5/s4-exchange.md) |
| S5 | Rust inference gateway | [s5](phase-5/s5-gateway.md) |
| S6 | Task application: exchange client, turn events, grant revocation, credential facts, usage ledger | [s6](phase-5/s6-tasks.md) |
| S7 | Coordinated cutover and retirement | [below](#cutover) |

### Dependencies

```text
S0 ──┬──▶ S1 (relay repo) ─────────────────────────────┐
     ├──▶ S2 ──┬──▶ S3 ───────────────┐                │
     │         └──────────▶ S5 ◀──┐   │                │
     └──▶ S4 ─────────────────────┴──▶ S6 ──▶ S7 ◀─────┘
```

- **S4 before S5 and S6:** S4 defines the operator, internal and runner APIs, and the
  exchange events they build on.
- **S3 and S6 meet at two contracts:** the credential metadata API and
  `credential.<user_id>.changed`. S3 fixes both before S6 consumes them.
- **An org-funded vertical path comes first in S4–S6.** Before contributed or relay
  work:
  1. The task application, in Rust mode, submits an org turn.
  2. The exchange dispatches a v2 runner.
  3. The runner calls the gateway's org route.
  4. The result comes back as `exchange.turn.*` and is ingested.

  This runs end to end in the integration harness, so integration problems surface long
  before the cutover.
- **S1 lands independently.** It must be deployed before S5's relay route can be
  qualified.
- **S2 lands independently.** It is a prerequisite for S3's and S5's secret reads.

### Deployability

For each slice: whether it can be merged (M), deployed while inactive (D), serve
production traffic on its own (P), or needs the coordinated switch (C).

| Slice | M | D | P | C | Running before → after | Authoritative stores before → after | Retires |
| ---- | ---- | ---- | ---- | ---- | ---- | ---- | ---- |
| S0 | ✓ | — | — | — | No change | No change | — |
| S1 | ✓ | ✓ | ✓ | — | Verifier with one bearer → verifier with three credentials and owner API v1, claims optional; the vault and Python exchange use their own credentials | Verifier registry file → plus a durable revocation and allowance store | The shared verifier bearer |
| S2 | ✓ | ✓ | ✓ | — | — → OpenBao running, unused except by its own checks | — → OpenBao database | — |
| S3 | ✓ | ✓ | — | ✓ | Accounts proxies to the vault → the same, with native mode deployed inactive | Vault → vault until S7 | — |
| S4 | ✓ | ✓ | — | ✓ | Python exchange → the same, with the Rust exchange deployed inactive | Exchange SQLite → unchanged until S7 | — |
| S5 | ✓ | ✓ | — | ✓ | agentgateway and forwarder → the same, with the Rust gateway deployed inactive | — | — |
| S6 | ✓ | ✓ | — | ✓ | Task application on the Python exchange → the same release can start in Rust mode | Task database (unchanged ownership) | — |
| S7 | ✓ | — | — | ✓ | Python exchange, vault, agentgateway → Rust exchange, gateway, accounts native mode | Vault, exchange SQLite → accounts database, OpenBao, exchange database | Python exchange, forwarder, vault, agentgateway, sponsorship slots, payload v1 transport, runner API v1 |

**Why not more independent slices:**

- Accounts can't own credentials while the Python exchange resolves material from the
  vault.
- The Rust gateway can't serve under the Python exchange's sessions without a Python
  binding bridge.
- Each of those bridges would be dual-write or adapter work that exists only to be
  deleted, so S3 to S6 deploy inactive and share S7.

## Parity checklist

Checked at S7's qualification. Each line is a behavior, not an HTTP status.

### Inference routes

| Route | Funding, custody | Behavior to keep |
| ---- | ---- | ---- |
| ChatGPT subscription | Org | Codex Responses over SSE to `chatgpt.com/backend-api/codex` with the org access token and account header. The org route is chosen from target configuration and frozen on the grant, not by a client header |
| OpenAI API key | Org | Responses over SSE with the org key |
| Claude OAuth | Org | Messages over SSE, bearer plus `anthropic-beta: oauth-2025-04-20`, no `x-api-key` |
| `openai-api-key` | Contributed, secret store | Bearer from the exact version |
| `chatgpt-subscription` | Contributed, secret store | Bearer and `chatgpt-account-id` from the version's account |
| `anthropic-api-key` | Contributed, secret store | `x-api-key`, no `authorization` |
| `claude-subscription` | Contributed, secret store | Bearer plus the joined `anthropic-beta` |
| `claude-subscription` | Contributed, relay | Verifier `/dispatch/v1/messages` with a signed claim |
| `chatgpt-subscription` | Contributed, relay | Verifier `/dispatch/v1/responses` with a signed claim; Codex WebSocket through `responses-bridge` |

**Across every route:**

- **Header policy.** Hop-by-hop, private and client-spoofable headers are stripped,
  and `accept-encoding: identity` is forced.
- **Body limits.** 48 MiB per request.
- **Model allow-list.** The grant's models only.
- **Budget exhaustion.** Recorded as `budget_denied`, which is terminal for the turn.
- **Provider errors.** Sanitized with today's categories, keeping `Retry-After` for
  rate limits and unavailability.
- **Cancellation.** It stops the upstream request: on turn cancel, session end, grant
  revocation, the request deadline and client disconnect.

### Funding

- **Implicit fill** reads the initiator's credentials as of the boundary, with
  `history_complete`. History imported from the vault keeps old boundaries answerable.
- **New attempts** select the sponsor's currently eligible credential in list order,
  with role and profile filters and exclusions.
- **Replay** of an admitted attempt keeps its grant and credential version.
- **Exclusions** work as today: `auth`, `model_denied`, `quota_exhausted`,
  `rate_limited`, `unavailable`, with backoff, cleared by a success.
- **Withdrawal** cuts off new grants at once and stops spending within the documented
  bound.
- **A replaced, removed, expired or unusable version** revokes the grants that use it
  (new in phase 5), and the version is excluded.
- **Org and contributed modes** keep the per-target funding switch and consent rules.
- **Usage** is recorded per request and attempt, and genuinely unknown usage says so.

### Runner and sessions

- **OIDC rules** are today's:
  - issuer, audience `concurro-gateway`, 60-second skew, a token issued within the last
    600 seconds, single-use `jti`;
  - the repository and owner IDs, `job_workflow_ref` and `workflow_dispatch`;
  - the forced `refs/heads/concurro/runs/<session>` ref;
  - `github-hosted` runners, run attempt 1, and the session bound to its run.

  A refusal is recorded unless the repository or owner doesn't match.
- **Engine handshake:** the exchange names the engine commit, and the key step
  requires the same commit.
- **Liveness:** 1,200 seconds to claim, 180 seconds between heartbeats, and a deadline
  of creation time plus the lease.
- **Staffing:** groups as today, FIFO, one running turn per session. A dead unclaimed
  session is re-dispatched, and the last refusal is shown with it.
- **Turn states and tombstones:** today's states and lost rules, `turn_expired` and
  `turn_cancelled`, a cancel that fences a turn not yet submitted, and finished turns
  retained for a day.
- **Cancel** preserves a terminal result, returns every session the turn used, and can
  be retried after a partial failure.
- **Results:** resending the same result succeeds; a different result, or one after
  the turn stopped running, is `409`. The runner retries up to five times.
- **Session restore:** the retained archive reaches the runner as a blob, verified by
  digest.

### Diagnostics and views

- Turn records, statuses and runner run IDs keep their fields in the read API.
- Transcripts are indexed at ingestion and their archives verified in the object store.
- Each turn's trusted `provider_failure` drives credential fallback.
- `provider_requests` is reported as `some`, `none` or `unknown` from real request
  records; today it is always `unknown`.
- Refusal reasons for OIDC and engine mismatches stay visible.
- On the credentials page, `lastUsed`, the expiry display, fingerprints, relay status
  and approvals are unchanged.
- Historical turns, imported transcripts, charges and sponsorship history stay
  readable.

## Cutover

One idle window, roll-forward only. Release ordering and the steps are below; the
installation's own commands belong in its private deployment records.

**Before the window:**

1. **The phase 4 baseline holds.** Its live checks of credential add, reorder and
   delete, a browser command's receipt and actor, and fresh Sponsor pairing passed on
   the old stack ([S0](phase-5/s0-baseline.md#phase-4-baseline)).
2. **S1 is deployed and serving.** The vault and the Python exchange use their own
   verifier credentials.
3. **S2 is running.** Its self-init policies are verified. OpenBao holds:
   - the org provider credentials;
   - the exchange's claim signing key and key ID;
   - the verifier `inference` and `release` credentials;
   - the admission bearer.

   The verifier has the claim key's public half and `kid`.
4. **Releases are built,** with S3 to S6 deployed inactive: accounts in vault mode, the
   task application in Python mode, and the exchange and gateway configured but not
   routed.
   - The v2 launcher and the v2 engine are tagged.
   - The engine at that commit speaks v2 only. Until the window every target's
     `engine_sha` keeps naming today's v1 engine, so nothing chooses a protocol at run
     time.
5. **Optional live probe:** with the exchange and gateway unrouted, an `exchange probe`
   session runs one org-funded turn through a manually dispatched v2 launcher on one
   fork ([exchange brief](phase-5/s4-exchange.md#probe)).

**In the window:**

1. **Quiesce.** No task is running or queued, `effects pending` is empty, and every
   attempt is settled. The Python exchange has no queued or running turn and no live
   session. Stop the task application.
2. **Pause credential writes** by stopping the vault's public listener. Accounts then
   answers credential writes with `503`. Confirm the vault has no pending relay
   intents.
3. **Back up** the vault database and keyring, the exchange database, the agentgateway
   state, the task, accounts and OpenBao databases, and the verifier registry. Verify
   each archive.
4. **Import** with `accounts import-vault`. Metadata imports in one transaction, and
   material imports resumably. The verification report must be clean:
   - every user's metadata compared at every policy revision;
   - every material version written and read back.
5. **Switch,** in this order:
   1. Update each target fork's executor workflow ref to the v2 launcher tag.
   2. Make claims required at the verifier.
   3. Start accounts in native mode, the exchange and the gateway.
   4. Install the edge routes for the runner and inference APIs, and point
      `responses-bridge` at the gateway.
   5. Start the task application in Rust mode, with `workflow_ref` set to the v2 launcher
      tag and every target's `engine_sha` set to the v2 engine commit.
6. **Qualify,** in order:
   1. An org-funded intake turn.
   2. A contributed turn on a secret-store credential.
   3. A relay turn on a paired Sponsor.
   4. Cancel a running turn.
   5. Withdraw consent and see spending stop within the bound.
   6. Replace a credential, and see its grant revoked and the next attempt select the
      new version.
   7. A standalone review.
   8. Read views: turns, transcripts, usage, credentials and relay status.
   9. The phase 4 baseline again, on the new store:
      - credential add, reorder and delete;
      - a browser command's receipt and actor;
      - fresh Sponsor pairing through owner API v1.
7. **Retire** the Python exchange, the forwarder, the vault and agentgateway (below).

**Forward recovery:**

- A defect is fixed by a corrective release; the old stack is not restarted.
- The vault and exchange archives are kept as evidence and kept out of the running
  configuration.
- The archived vault holds encrypted personal secrets. The operator decides when it is
  destroyed, after the import has been verified.

**Retirement:**

- **Code:** `gateway/exchange/`, `gateway/vault/`, `gateway/agentgateway.yaml`,
  `gateway/provider_protocol.py`, and the Python test suites for them. That includes
  `gateway/tests/phase4_spike.py` and `phase5_spike.py`, and
  `engine/tests/test_admissions.py`'s copies of the exchange and protocol modules.
- **CI:** the vault and exchange requirements, compile and test steps, and their image
  builds (`.github/workflows/ci.yml`).
- **Docs:** `docs/design/inference-gateway.md` and `docs/design/credential-failures.md`
  are rewritten for the Rust gateway and exchange.
- **Engine:** the runner API v1 client paths in `engine/relay.py` and
  `engine/runner.py`, and payload v1's base64 fields.
- **Rust:** `crates/exchange-v1`. The sponsorship slot adapter and its tables. The
  task application's poll-only paths.
- **Startup modes:** accounts' and the task application's old-mode code. `vault-v1` is
  renamed to the credential metadata client.
- **Deployment:**
  - The containers, images, compose overlays and edge routes for these components.
  - Their secrets: the vault master key, keyring, service and resolver tokens, the
    exchange operator token and `secrets.env`.
  - The shared verifier token, and the old verifier routes in the next relay release.

**Evidence:** the import verification report, qualification results, image IDs and
release SHAs, recorded in the private deployment records.

## Untested assumptions

- **Relay protocols 4 and 5** can both be accepted by one verifier build. S0 spikes it;
  the fallback is shipping Sponsor 0.2.0 before S1 deploys.
- **OpenBao's static seal, PostgreSQL storage and a create-only policy** behave as
  documented with `vaultrs` 0.8 and `cas=0`. S0 spikes it.
- **Provider streams pass through with a usage tap and no added buffering** for all nine
  routes. S0 spikes it with recorded fixtures.
- **`responses-bridge` works unchanged** against the Rust gateway. S0 spikes it.
- **The deployed verifier image's protocol version** is recorded during S0, before S1
  is planned against it.
