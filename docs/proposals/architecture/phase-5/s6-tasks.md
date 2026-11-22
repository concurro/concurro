# S6: task application on the Rust exchange

Part of the [phase 5 plan](../phase-5-plan.md). The same release starts in either mode,
`exchange = "python"` or `"rust"`, chosen in settings. S7 switches it to `"rust"` after
the quiesce, and the retirement slice removes the Python mode.

The funding module keeps consent, selection, grants, budgets and accounting. Phase 5
changes where it reads credentials, how its grants reach the exchange, and how it
learns about revocations and usage.

## Crates

- **`crates/tasks` and `crates/tasks-postgres`:** the changes below. SQL stays in the
  storage crate.
- **`crates/exchange-client`** ([S4](s4-exchange.md)) replaces `crates/exchange-v1` in
  Rust mode.
- **`crates/vault-v1`'s client and `Metadata::select`** are reused against accounts'
  metadata API. Only the base URL, path and bearer change. The crate is renamed when the
  Python mode goes.

## Submits

- **The body is the operator API v1 submit,** built once and stored as today, so replays
  resend it unchanged. It carries:
  - the attempt as the turn;
  - the attempt's grant: `task_org` with the org route, or `task_contributed` with the
    selected credential;
  - the binding (today's, without its funding fields), the staffing group, expected
    OIDC claims, models, token budget, lease, and turn payload v2.
- **The org route** comes from the target configuration that sets today's
  `gateway.headers.codex` plan: ChatGPT subscription or the OpenAI key for Codex, and
  Claude OAuth for Claude. It is frozen on the grant.
  - A migration lets `task_org` grants carry a `route`. Today a check constraint keeps
    it null for org grants (`crates/tasks-postgres/migrations/0004_funding_grants.sql:20-23`,
    `0008_standalone_reviews.sql:182-186`).
- **The retained archive** is referenced by digest. The submit waits until the archive
  object is verified in the object store, which the task application already does for
  session restore (`crates/tasks/src/archive.rs:156`).
- **Effects are unchanged:** the effect job claims, sends, retries transient failures,
  and settles a refusal as today (`crates/tasks-postgres/src/effects.rs`).
- **No sponsorship slot transitions are sent in Rust mode;** the slot adapter doesn't
  run.

## Results

- **A durable consumer of `exchange.turn.*`** converts each event into the task-domain
  observation that `record_observation` takes, explicitly:
  - **Status mapping:** `queued` and `running` stay; the `finished` fact becomes
    `done`; `lost` and `cancelled` stay. Today's path accepts only those
    (`crates/tasks-postgres/src/effects.rs:551`).
  - **Status snapshots** keep the highest revision. A `dispatch` queues the runner
    dispatch, as a submit reply's does today. A `provider_failure` ends the attempt, as
    today.
  - **The `finished` fact** settles the attempt with its result, blobs,
    `provider_requests` and usage summary.
- **Credential failures are validated against the attempt's grant,** not the Python
  binding. Today `record_credential_failure` checks the failure's `binding.sponsor`,
  credential, version and slot `grant_id` (`crates/tasks-postgres/src/sponsorship.rs:853`),
  and phase 5 removes those binding fields and slots. The new check:
  - the failure's `grant_id` equals the attempt's grant;
  - its `record_id` and `version` equal the grant's credential;
  - its `model` equals the request's.

  Exclusions are recorded from it as today, with the same categories and backoff.
- **Blobs replace base64 in ingestion:**
  - The transcript and retained archive are read from the object store by digest and
    indexed with `concurro-tasks-diagnostics`, replacing the base64 decode
    (`crates/tasks-postgres/src/archive.rs:362`).
  - The workspace blob feeds publication.
- **`turn.finished` changes.** It carries the exchange's `provider_requests` (`some`,
  `none` or `unknown`) instead of always `unknown`, and a usage summary. Both fields are
  additive. The read API keeps consuming `turn.*` from the task application.
- **Reconciliation.** At startup, and every five minutes, every attempt that isn't
  terminal and hasn't seen an exchange event for five minutes is read with
  `GET /v1/turns/{turn_id}`. That covers a consumer outage or lost events. Today's 15
  second poll loop goes.

## Grant revocation

- **Every revocation of an `issued` grant records an exchange effect:** withdrawal
  cutoffs, attempt cancellation, permission loss, and a revoked credential version.
  - The effect is `POST /v1/grants/{grant_id}/revoke`, through recorded, `sending` and
    settled, retried until acknowledged.
  - It's recorded in the same transaction as the revocation.
- **Spending stopped.** The withdrawal's "spending stopped" step
  (`request.tasks.stopped`) is published 40 seconds after every revoked grant's effect is
  settled: the gateway's worst-case stream bound, which fails closed during an exchange
  outage ([gateway](s5-gateway.md#cancellation-and-revocation)). Phase 3's time bound is
  replaced by this shorter, documented one.

## Credential facts

A durable consumer of `credential.<user_id>.changed`. For each `{record_id, version}`
in `revoked`, in one transaction:

- record the version in `revoked_credential_versions`;
- revise every grant on that version (contribution and standalone) to `revoked`;
- record the exchange revoke effect for those still `issued`.

**Reordering is safe in both directions:**

- **Grant insertion checks the marker table** in its own transaction. A selection made
  from a metadata read taken before the revocation can't insert a grant on a revoked
  version once its fact has been applied. That selection is refused and re-runs, as a
  selection that finds no eligible credential does today.
- **The exchange fences unknown grants.** A revocation that reaches it before the
  grant's first submit leaves a revoked record, and a later submit under that grant ID
  is refused with `410 grant_revoked`
  ([contract](../../contracts.md#exchange-operator-api)).

Selection needs no change: the next metadata read lists only valid versions. A
replacement credential is picked up by the next attempt, as today.

## Usage

- **A durable consumer of `inference.usage`** records each request in `funding_usage`,
  deduplicated by event ID. Each row holds:
  - the attempt (the turn ID), grant, request ID and model;
  - tokens, including cache tokens when reported, or the reason they're unavailable.
- **Read views** can then show tokens beside call counts. Showing them is optional for
  phase 5's parity, and calls stay the charged unit.

## Settings

| Setting | Meaning |
| ---- | ---- |
| `exchange.kind` | `python` or `rust` |
| `exchange.url`, `exchange.bearer_file` | The operator API |
| `credentials.url`, `credentials.bearer_file` | Accounts' metadata API in Rust mode; the vault's in Python mode |
| `consumers` | Names for the exchange, credential and usage consumers |
| `workflow_ref`, per-target `engine_sha` | The launcher tag and engine commit (existing settings). S7 sets both to the v2 release |

## Acceptance

- **Org submit to finished,** against the Rust exchange and a fake gateway: the submit
  body, dispatch, status snapshots, the `finished` fact and blob ingestion. The
  read API's `turn.*` events, transcript and archive match what the poll path recorded
  for the same recorded turn.
- **Contributed:** selection from accounts' metadata API, the grant carried in the
  submit, and replay of an admitted attempt resending the same body.
- **Withdrawal:** the cutoff, revoke effect and acknowledgement come first, and
  "spending stopped" is published after the bound. A delayed acknowledgement delays the
  step and doesn't skip it.
- **Credential replaced:** the fact revises the grant to `revoked` and records a revoke
  effect. The next attempt selects the new version.
- **Contributed failure and fallback:** a `quota_exhausted` failure on the first
  credential records the exclusion against the grant's version, and the retry attempt
  selects the sponsor's next credential and finishes.
- **Reordered revocation:** a selection from a metadata read taken before a removal,
  inserted after the removal's fact was applied, is refused by the marker check and
  re-runs. A revoke that reaches the exchange before the first submit fences it.
- **Relay grant:** selection carries `custody` and `relay_id` into the submitted grant,
  and the exchange signs a claim the S1 verifier accepts, without the vault's
  resolver.
- **Lost events:** with the exchange consumer stopped, reconciliation settles a finished
  turn from `GET /v1/turns/{id}`. Replaying the events afterwards changes nothing.
- **Usage:** one row per request, however often it's delivered, and unavailable usage
  is stored with its reason.
- **Mode selection:** Python mode runs today's suites unchanged. Rust mode never
  starts the slot adapter or the poll loop.
