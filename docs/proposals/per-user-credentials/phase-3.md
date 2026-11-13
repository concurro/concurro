# Phase 3: frozen admission and provider fences

Implemented against signed phase-2 backend `e255130078e1bb585623803f33faaa10af1136ce`.
Production inference still uses organization credentials: the current Gateway adapter
advertises no contributed routes. No credentials, deployment or live-account test is needed
for this release. Phase 4 supplies actual personal credential injection and its route
capabilities; subscription acceptance remains phase 5.

## Runtime boundary

`Controller.ask` and `Design.reserve` now freeze an admission on each operation before its
charge/checkpoint. It records funding, exact credential identity/version, selected provider
owner/epoch/grant, call, ownership generation and transcript scope. The existing attempt
UUID is the Exchange turn UUID (hyphens added only for transport). Serial retries select
again for their new attempt; exact transport replay never selects again. A whole initial
review roster preflights before any debit. Existing task/credential budgets are unchanged.

The private operator `GET /exchange/operator/capabilities` advertises only routes actually
implemented by the gateway adapter and backed by the retirement-event channel. With
`funding_mode: "contributed"` in target configuration, unsupported adapters refuse as
`Blocked` **before charging**. Gateway key creation independently refuses contributed
bindings on the org adapter. There is no org fallback. Dedicated `awaiting_credentials`
outcomes, provider observations and the production adapter are phase-4 work.

For supported isolated adapters, the shared serial/batch preflight resolves profiles via
`credential_routes` and `engine/credential_routes.py`, verifies fresh stable-ID writer
permission once per selected owner/reservation, and freezes exact current record/version,
kind and model coverage. Source revisions are checked before and after I/O; an outage or
unknown coverage spends no call. Transient private I/O uses the existing wait-until
backoff before dispatch; permanent conflicts retain their existing blocked outcome. Only selected providers' pending transitions are flushed
for admission. Required coverage aggregates across owners. Today's model-call paths are
required; this does not introduce the proposed optional evaluator/routing policy.

Saved results recover before capabilities/current credentials. Completed design results
also retain their original transcript scope. Unfinished crashed attempts remain
uncertain; a later attempt receives a new UUID/charge. Existing versioned audit artifacts
retain each pre-dispatch checkpoint, including failed/replaced admissions: historical
attribution uses artifact ID/revision, not a second ledger. Old org credential records and
completion receipts retain their legacy fields; new receipts additionally bind the frozen
admission digest.

## Source consent and cutover

The Registry records funding-policy activation when `Worker.load` reads a **current,
unpinned** target. Startup and normal polling observe current targets before future starts
where possible. Receipt identity binds repository, full config digest, funding mode,
service-clock activation and previous receipt. Changing policy/config closes that interval;
pinned task/recovery loads never activate old configuration. The initializer selects only
an exact config receipt whose effective interval covers the start/label source timestamp.
This records service activation, not a transactional GitHub configuration snapshot. Clock
regression refuses a new activation. No operator-supplied backdating is accepted.

An event predating observed activation remains `unknown`, even if today's mode and token
would allow it. Such a starter cannot become a personal payer. Fresh contributed starts
with interval evidence may fill only uncovered providers from historical route availability;
explicit earlier sponsors retain precedence even without credentials. Each starter/label grant keeps its
source-policy receipt. Previously frozen org/unknown grants are never relabelled.

At contributed cutover, org/unknown starter/label grants become unassigned via normal
monotonic provider transitions; explicit-comment grants survive. Once-only fill is not
rerun. If historical availability cannot be reconstructed, confirmed explicit claims can
now be adopted without waiting for that reconstruction: initialization retains its original
boundary, `initializer_status: "unknown"`, and unknown metadata/availability. Other providers
stay unassigned until their own consent. No retry, withdrawal, invalid credential or future
provider automatically reallocates a slot.

## Exchange authority

Version-2 bindings carry only the selected provider's funding identity. Session/staffing identities cannot cross owners, epochs or credential versions. Retained
transcripts preserve their existing task/role scope across payer transfers and token replacement,
as the epic requires. The admission freezes that transcript digest independently of payment;
no live-backend evidence yet justifies a provider-epoch scope fallback. New admission checks exact vault metadata outside
the lifecycle lock, then atomically rechecks turn replay, ownership generation, selected
provider fence and recorded revocations before insertion. A missing provider fence may be
initialized only from the operator's frozen checkpoint grant. Exact replay returns the
original receipt/status even if the adapter or vault is now unavailable.

A stale-generation refusal identifies the exact unapplied transition. A current controller
checkpoints a new delivery ID/generation for that request only, retaining source and epoch CAS;
committed peer receipts in a permission-loss batch keep their IDs.

Transfer is graceful for already admitted work. Self-withdrawal cuts off only that caller's
admissions for that task/provider, including their older epochs. Permission loss publishes
all that caller's provider cutoffs on the task together. Other owners/providers/repos are
unaffected; tombstone epochs never reset. Key, claim, heartbeat, completion, polling and
replacement staffing check the recorded authority; new task ownership fences contributed
work without changing legacy org-session behavior.

Retirement-only vault event consumption was brought forward from later integration because
phase-3 execution guards need it. Cursor advancement and exact record/version revocations
commit together, replay idempotently and survive restart. Cleanup failure cannot undo the
cutoff; normal reconciliation retries deletion. This consumer never wakes a task or retries
inference. Event delivery is asynchronous; it cannot retract work already accepted by an
upstream. The existing org key-lock/activation path remains serialized. Contributed key
activation permits concurrent retirement, then rechecks authority/deadline and deletes a
late key before replying. Partial activation adopts the same recorded key and spent budget.

## Configuration and upgrade

Org-only deployments need no new secret or target setting. For an implemented contributed
adapter, the Exchange's existing private env file accepts all four together:

| Setting | Purpose |
| ---- | ---- |
| `EXCHANGE_VAULT_METADATA_URL` | Vault public listener's private service-metadata endpoint base |
| `EXCHANGE_VAULT_METADATA_TOKEN` | Dedicated metadata-only 32-byte hex bearer |
| `EXCHANGE_VAULT_EVENTS_URL` | Private listener base, e.g. `http://concurro-vault-private:8091` |
| `EXCHANGE_VAULT_EVENTS_TOKEN` | Separate gateway/private-listener 32-byte hex bearer |

Use the phase-2 private-network overlay and owner-private env-file provisioning. The two
principals must differ; metadata cannot resolve secrets. The events bearer is the existing
private gateway principal, which also authorizes resolver access; it is not events-only. Exchange reads only allow-listed
nonsecret metadata/events and has no secret-resolution method. Phase 4 implements the
resolver/injection caller. No bearer or provider material enters admissions or checkpoints.
Exchange image builds now use repository-root context to copy the **same** route catalog.

Registry schema **7** adds policy intervals, preserving schema-2–6 state on migration.
Charged checkpoints now write **format 9**; this reader still accepts prior formats,
including 7/8 and old receipts for forward migration. Recovery is roll-forward only; no
deployment rollback or downgrade support is required. Corrective releases must preserve
current checkpoints, audit history, withdrawal cutoffs, event cursors and revocation evidence.
Backups support data recovery. This change does not authorize deployment.

The existing portal remains the org-funded shadow view; no frontend change or production
funding switch ships here. Isolated tests exercise real Controller, Agents, RunnerExecutor,
Registry and Exchange paths with fake GitHub, jobs, metadata and credential-aware adapters.
They prove admission/fence behavior, not subscription backend/account compatibility. The
user-deferred live-account checks from phase 0 remain pending.
