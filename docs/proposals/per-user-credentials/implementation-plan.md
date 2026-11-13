# Per-user credentials: implementation plan

**Status: phases 1–6 implemented; optional credential refresh remains deferred.**
See [phase 1](phase-1.md), [phase 2](phase-2.md) and [phase 3](phase-3.md) for shipped runtime/API behavior and migration boundaries.
Phase 3 froze admissions on actual org paths and exercised contributed enforcement with isolated adapters. Phase 4 now supplies configured production injection and observation; see its runtime record below.
The [isolated phase-0 spike](phase-0.md) is implemented and measured; real-account checks
remain pending by user scope.
This supplies the execution plan for the [epic](../per-user-credentials.md), including
subscription connections. The epic and [cross-cutting contracts](../README.md#cross-cutting-rules)
remain authoritative unless a decision below explicitly calls out a change. The
[review record](review.md) includes the original Claude review and the CLI-token
follow-up, plus the review and repairs for provider-specific sponsorship and explicit-first initiation.

The current [phase-4 runtime](phase-4.md) supplies native contributed injection, trusted request observations and credential pauses. [Phase 5](phase-5.md) adds both CLI-token forms, atomic selected-model validation and manual replacement. Earlier phase records describe historical boundaries.

## What this plan delivers

Different people can sponsor different providers on one task. Explicit issue-comment
sponsors take precedence; initial start/label fills only still-absent providers covered by
the initiator's available credentials.
Canonical slots are `openai` and `anthropic`, with independent owners and epochs.
They use their own subscriptions through the same
CLI-issued tokens and gateway routes as the current deployment. The user logs in locally
with each CLI and pastes the resulting token into the GitHub-authenticated portal.
**Token-only storage, validation, use and manual replacement are the complete initial
subscription deliverable.** Automatic refresh is optional, not a prerequisite.

| Layer | PRs | Deliverable |
| ---- | ---- | ---- |
| Provider sponsorship and admission | 1-3 | Pre-enrollment issue intents, explicit-first initiation, independent slots/epochs, frozen payer/version, withdrawal and usage |
| Sponsored inference | 0, 4 | Measured per-user injection into the existing ChatGPT and Claude subscription routes; API-key routes remain distinct |
| Both subscription token forms | 5 | Local CLI instructions, pasted tokens, ChatGPT account metadata, validation, expiry/status and manual replacement |
| Optional refresh | 5a | Only for supplied refresh material with demonstrated refresh behavior for that CLI-issued grant |
| Automatic task recovery | 6 | Credential-valid events wake eligible pauses; ordinary admission and budget rules still apply |

No custom provider OAuth registration, Concurro login helper, GitHub device flow or new
public vault import route is needed. Provider terms remain the operator's and users'
responsibility, as the epic specifies; this plan makes no provider-approval claim and
adds no agreement prerequisite.

For example, Alice comments `/concurro sponsor openai` before Bob labels the issue.
Concurro confirms Alice's OpenAI intent first; Bob supplies Anthropic only if his available
credentials cover a selected Anthropic route. Alice keeps OpenAI even if her token needs
replacement. The backoffice can show her sponsorship before enrollment. Bob's later
withdrawal never retires Alice's OpenAI sessions or invalidates an approval.

## Baseline and integration boundary

Reviewed on 2026-11-12 at **`0f0bd17dd91e7cba47327c8e907021b66e7f31c8`**.
Source paths/symbols below refer to that tree; recheck them when implementing.

The original checkout had unrelated deployment/launcher work in progress. Its relevant
read-only diff adds an optional `engine` field to `session_binding`, authenticated engine
discovery, a runner claim stating its engine commit, independently versioned
launcher/engine identities, and separately deployed backoffice frontend routing. None is
copied here. Integrate with that protocol; do not remove its engine binding or restore
workflow-SHA-equals-engine-SHA. Recheck `gateway/exchange/exchange.py`, `engine/relay.py`,
`engine/runner.py`, `service/worker.py`, Compose/Caddy and deployment instructions after
that work lands.

The epic's "Today" section predates several implemented pieces:

| Existing capability | Verified source and reuse |
| ---- | ---- |
| Browser-bound GitHub sign-in and API-key forwarding | `backoffice/auth.py:Sessions`, `GitHubOAuth`; `backoffice/app.py:create_app`; `backoffice/credentials.py:Vault`. Sessions are in memory and last as long as the user token; no refresh or remote logout revocation. Subscription entries are placeholders (`ROUTES` with no vault route). |
| API-key envelope vault | `gateway/vault/vault.py:Identity`, `Validator`, `Envelope`, `Store`. Two API routes, `/models` validation only. Callers authenticate only with a GitHub user token (`Identity.resolve`); there is no service-to-service API. `Store.put`/`revoke` destroy older ciphertext. No injection, resolver or refresh. No Caddy route (`gateway/compose.yaml`). |
| Writer-verified backoffice commands | `service/actions.py:Actions`; `engine/delegation.py:render`, `resolve`; `backoffice/tasks.py:COMMANDS`. Markers sign `actor_id`, but `resolve` restores only the login, leaving the App's ID in the effective comment. Fix before attribution. |
| Persistent call charging/recovery | `engine/controller.py:Controller.ask`, `begin_attempt`, `charge_call`, `recover`; `engine/design_batch.py:Design.reserve`; `engine/attempt_result.py`. `begin_attempt` already persists a per-attempt UUID (`operation["attempt"]`) before dispatch. Reuse these admission points. |
| Frozen runner setup/atomic insertion | `engine/agents.py:RunnerExecutor.binding`, `admit`, `submit(identifier=)`; `gateway/exchange/exchange.py:session_binding`, `staffing`, `Exchange.submit`, `retire`. The binding is stored on turns and sessions and hashed into both the request digest and the staffing group. |
| Pauses and durable wake-ups | `Controller.suspend` with `vocab.SUSPENDED_STAGES`; `CommandHandler.retry`; `service/registry.py:Registry.enqueue`, `request_observation`. |
| Unenrolled issue ingress | `Ingress.comment` already queues human `/concurro` comments without a label; `Registry.receive` atomically stores receipt+wakeup. Normal human comments do not create exact observation jobs, receipts contain only ID/time, and edits away from a command can be missed. |
| Source observation and issue views | `Registry.request_observation`/`observations`/`finish_observation`, `Reconciler.snapshot`/`observe_pending`, `observations.satisfied` and `Recovery.poll` provide durable retry/uncached reads. Candidates exclude unenrolled issues; `UserAccess.task_issues` filters by label and `Tasks.task_view` rejects no-checkpoint/no-stage issues. Extend these paths rather than invent a second queue. |
| Native harnesses, retained history, diagnostics | `engine/agent_entry.py:provider_environment`, `command_for`; `engine/sessions.py:prepare`; `service/diagnostics.py:Store`, `Recorder`. Diagnostics are best effort, unsuitable as the accounting authority. |

Agentgateway is pinned in `gateway/compose.yaml` to v1.7.0, image digest
`sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b`.
`Gateway.create` mints virtual keys with model allow-list, token budget and
`concurro.session` metadata; this admin client never sees provider responses. Existing
subscription routes in `gateway/agentgateway.yaml` establish operator configuration, not
user registration, refresh or multi-user isolation.

## CLI tokens and existing subscription routes

The chosen baseline is the user's existing CLI-login/token procedure, extended from
operator-owned tokens to one versioned credential per person and route. Local help was
checked without logging in, reading credential files or calling providers:

| CLI | Observed local help | Portal instructions |
| ---- | ---- | ---- |
| Codex 0.163.0 (runner pin rechecked 2026-11-13) | `codex login --device-auth` is available. Help exposes no token-export command. | Complete device login locally, obtain the issued access token using the existing operator procedure, then paste it. Do not claim that the login command itself prints a token or invent an export flag. |
| Claude Code 2.1.301 | `claude setup-token` creates a long-lived subscription authentication token; `claude auth login --claudeai` signs in through the Claude account flow. No device-auth flag is shown. | Use `claude setup-token` and paste its resulting token, or the existing locally obtained Claude subscription token. Call this CLI/browser login, not a literal device-code flow. |

No login runs in a Concurro job. The portal does not ask for a provider password, initiate
a new OAuth client flow or read a local CLI credential file. Preserve existing CLI and
harness pins; the inspected local versions above are instruction evidence, not a change
to deployed runner versions.

### Registration and validation

Extend `backoffice/credentials.py:ROUTES` so `chatgpt_subscription` maps to
`chatgpt-subscription` and `claude_subscription` to `claude-subscription`. Reuse the existing
`PUT /api/me/credentials/{route}` → BFF → vault `PUT /credentials/{route}` path and GitHub
user-token forwarding. `vault.Identity.resolve` independently establishes the stable owner;
a submitted user ID never chooses whose slot is written. Extend `SecretBody` and
`backoffice.credentials.Vault.put` for route metadata
such as ChatGPT account ID; update `route()`'s "Subscription credentials aren't supported yet."
note, `UNUSED`, and expiry/account fields in the same PR. Subscription writes remain write-only.

The existing gateway configuration defines the exact route to validate and inject:

| Route | Token and metadata | Preserve from `gateway/agentgateway.yaml` |
| ---- | ---- | ---- |
| ChatGPT subscription | CLI access token plus `chatgpt-account-id` | `https://chatgpt.com/backend-api/codex`; replace upstream `authorization` with `Bearer <token>`; set the account header from the stored version. Today's `x-concurro-plan: chatgpt` selector becomes trusted server-side route selection for sponsored calls; without it `gpt-*` falls through to the `$OPENAI_API_KEY` entry, so stripping the client header must not leave a sponsored call on that entry. The Claude entry has no selector and is the only `claude-*` model; there is no Anthropic API-key backend today. PR 4 adds one and gives both Claude routes explicit server-side selection, so a sponsored `claude-*` call can never match the operator-token entry. |
| Claude subscription | CLI-issued subscription token | Anthropic backend; replace upstream `authorization` with `Bearer <token>`; append `oauth-2025-04-20` to the harness's `anthropic-beta` value; remove `x-api-key`. Preserve the client's other supported betas. |

ChatGPT account metadata is part of the versioned route record. Extract it from an
authentically verified token claim if this CLI token format supports that; otherwise ask
the user for the account ID alongside the token, as the operator configuration does today.
Unsigned JWT decoding is only a hint. Confirm the token/account combination with a cheap
model inference on the configured route, recording any authenticated account evidence
returned by that route. The spike must establish how that backend validates the account
header; a 2xx that ignores it must not be described as verified account identity. Never
substitute the operator's account ID when metadata is missing or rejected.

Validate both providers with the same configured-route adapter used for sponsored inference,
including the selected model set, fixed request shape, headers and stream completion. Validate
each distinct selected model once and publish the entire set atomically; a failed member
preserves the previous complete version. Portal defaults reuse target route requirements
and previous validated models, without a separate operator model catalog. The probe carries only
the candidate token and account metadata and never traverses a gateway model entry that injects
`$CHATGPT_ACCESS_TOKEN`, `$CHATGPT_ACCOUNT_ID`, `$CLAUDE_OAUTH_TOKEN` or `$OPENAI_API_KEY`; a 2xx
obtained with operator credentials is not validation.
Do not send a CLI token to an API-key `/models` validator or to any endpoint other than the
configured subscription route. A successful cheap inference proves that token/version/model
combination worked at validation time; it does not promise future quota. Record sanitized
failure/status, validation time and model evidence. An invalid replacement leaves the
previous usable version intact, matching today's `Validator`-before-`Store.put` order.

The UI shows route, fingerprint, account label when available, validation time, expiry
when known, and Replace/Revoke. Clear the token form after submission and never persist
it in browser storage or return it from listing APIs. Known expiry prompts local CLI
login and replacement; unknown expiry is labelled unknown, never assigned a guessed
lifetime. Auth failure prompts replacement; quota exhaustion pauses required work with a retry
action when capacity returns; a transient validation outage preserves the current version and asks the user to
retry validation. Replacing a token does not reset call budgets, transfer sponsorship or
invalidate approvals. Its interruption semantics are specified below.

## Authority and persistent records

Each decision has one authority; other components hold frozen copies or fences.

| Decision | Authority | Read or enforced at |
| ---- | ---- | ---- |
| Sponsor and epoch for a provider | Confirmed issue-intent projection before enrollment; checkpoint `sponsorships[provider]` after initialization, with immutable grant source | Exchange fences are keyed by task + provider; unrelated slot changes do not alter a session binding |
| Sponsor still a writer | GitHub, read by the service-side controller with the installation token | Once per sponsor per admission/batch; denial revokes all that user's grants/old admissions on this task, across providers, without affecting anyone else or another task |
| Credential status, version, tokens | Vault | Controller preflight (metadata); `Exchange.submit` for new turns (metadata); resolver on every inference request (secret) |
| Call budget | Checkpoint `calls` (`Controller.charge_call`) | Before every charge |
| What an attempt was admitted with | Checkpoint operation record, mirrored in the Exchange turn/session binding | Never recomputed |

The Exchange does not call back into the service for writer decisions. The service is
already the Exchange's only operator, so a callback would have the same principal vouch
for its own submission while adding a dependency cycle under the lifecycle lock. This
refines the epic's new-attempt check: the controller makes the writer decision
immediately before submitting, and `Exchange.submit` enforces its recorded outcome (the
revocation table) together with the credential check.

No new spend cap or automatic org fallback is introduced.

| Owner | Proposed record |
| ---- | ---- |
| Issue intent, service registry | Add `issue_sponsor_sources` keyed by `(repository, issue, comment_id, revision_digest)` (digest includes action, body/previous-body and revision timestamp, so deletion is distinct) for normalized command/provider, stable actor, source timestamps/order, action and validation/provenance; retain revision/deletion evidence, no credential bytes. Add `issue_sponsorships` keyed by `(repository, issue, provider)` with projected owner, actor-specific withdrawal tombstones, revision and provisional/confirmed status. Existing observation jobs carry reconciliation; checkpoint initialization imports a confirmed projection by digest. |
| Task checkpoint | `sponsorships[provider] = {epoch, sponsor_id, login, grant_id, provenance, status}`. `status` is `unassigned`, `active` or `withdrawn`; null owner and monotonic epoch survive withdrawal. `pending_sponsorships[provider]` carries a transition until acknowledged. History records provider, actor, before/after epoch and immutable grant source; `grant_id` references immutable `{provenance, source_id, source_revision, actor_id, provider, source_order, source_funding_mode}` evidence. `funding_initialization` records the first start-comment/label-event ID, confirmed source revision and available-route evidence, once. Org policy is separately labelled; approvals are untouched. |
| Charged operation/batch member | `operation["admission"]`, written by `begin_attempt` and immutable afterwards: schema version, call number, owner generation, canonical `sponsorship_provider`, sponsor ID and that slot's epoch/grant ID, funding, the `Credential` record, transcript scope. The attempt ID, request digest and profile (`operation["attempt"]`, `["request"]`, `["agent"]`) already exist and are not duplicated. |
| `Credential` | Extend `engine/credentials.py` and its exact parser together with `route`, `record_id`, `version`; `account` is the decimal GitHub ID for contributed calls. For contributed credentials `id` is a digest of `(kind, account, route, record_id, version)` (the route declares its canonical provider), so `charge_call` keeps keying its ledger by `credential.id` and never merges sponsors. Org identities and existing ledger entries (`gateway:<harness>`) are unchanged. |
| Exchange binding | `session_binding` accepts a second, versioned field set adding `sponsorship_provider`, `sponsor`, `epoch`, `grant_id`, `funding`, `route`, `credential` (record ID) and `credential_version`. Because the binding is already persisted, digested and hashed into staffing, this is the frozen admission on the Exchange side: sessions are never shared across provider slots, sponsors, epochs or versions; the whole slot map is never hashed into one session, and no extra admission table is needed. |
| Exchange tables | `sponsorship` keyed by `(repository, issue, provider)`, holding generation, monotonic epoch, nullable sponsor, grant ID and transition ID. Revocations record `(task, provider, sponsor, through_epoch, reason)`; withdrawal writes one provider cutoff, permission loss writes all provider cutoffs for that user on this task; global credential revocations key record/version. Keep tombstones and vault-event cursor; no provider tokens. |
| Vault database | Extend `(user_id, route, version)` with an opaque record ID, kind, route account metadata, verified provider identity when available, status reason, validation/known-expiry/last-used times, model evidence, wrapping-key ID and an event log. Token and account metadata are replaced as one version; global revoke targets every use of that record/version, without touching another owner's credentials. AAD keeps binding identity and version (`vault.aad`). |
| Optional renewable material, vault only | Only fields actually supplied and validated for the CLI-issued grant: refresh token and any required metadata, encrypted with the access token; known expiries, token revision and refresh lease. No assumed OAuth client ID, scopes or ID token. Demonstrated refresh changes revision within one credential version; a user-pasted replacement creates a new version. |
| Durable usage | Exchange outbox keyed by turn and inference request ID, retained beyond `FINISHED_RETENTION_SECONDS`. The engine call count stays authoritative; missing token usage displays unknown, not zero. Diagnostics/backoffice consume non-secret projections. |

Credential selection is per **session**, not per active turn: every turn a session serves
has the same binding, so a key resolves to exactly one `(credential, version)` for its
whole life. Only that selected provider's epoch belongs in its binding; changing another
slot never rotates its session, key or transcript. Usage/events carry provider, sponsor,
epoch and route/version; the existing full credential ledger keeps accounts separate.
The active turn matters only for attributing observations.

`attempt_result.binding` gains the admission digest only when the operation has an
admission. `restore` compares bindings exactly, so an upload can't restore under another
payer, and receipts written before this change (no admission) still match their
operations and stay legacy-org records. Never infer a past sponsor from current state.

## Provider sponsorship, transfer and withdrawal

The trusted route catalog declares `sponsorship_provider`: `openai` for OpenAI API and
ChatGPT routes, `anthropic` for Anthropic API and Claude subscription routes. Resolve the
profile's route first, then its provider slot; never infer payment from harness, model
family, lineage or client headers. A new provider added after first initialization stays
unassigned until explicit sponsorship. Same-provider route/model changes and authorized
retries retain the grant, subject to that owner's credential coverage and authority.

**Explicit sponsorship.** `/concurro sponsor openai|anthropic` assigns only the verified
writer issuing it. It works on an issue before a task/checkpoint exists. Bare `sponsor`
shows usage/slots; unknown provider arguments do not assign anyone. The same user can
sponsor both via separate commands or a clear UI action that posts both results. A token
upload alone is not an issue sponsorship. Explicit sponsors need not have usable tokens
yet: their slot remains theirs with missing coverage, never handed to the initiator.

**Initial start/label.** At the first accepted `/concurro start` or autonomous enrollment,
reconcile explicit issue intents first, then fill only slots without an active explicit
grant for which that initiator has an available selected-route credential. Record exact stable actor,
start-comment/label-event ID, provider, available route/model evidence and provenance
`task_start` or `label`. An active explicit claim (even missing credentials) or unresolved source validation
prevents fill. Before this first enrollment only, a withdrawn slot has no active owner:
another initiator may fill it, but the withdrawing/revoked person is not auto-reassigned
without a new explicit provider grant. Partial coverage can leave an assigned slot
missing another required route; show that pause instead of substituting a payer. This
is a once-only initialization rule, not recurring fallback on retry, relabel, resume,
withdrawal, credential failure or token revoke after initialization. Existing explicit grants always
win initial selection.

Reuse `Command.argument`, command parsing/handlers, `delegation.command_text`/`DESCRIPTIONS`,
`service/actions.py:Actions` and `backoffice/tasks.py:COMMANDS`/`command_argument`. The BFF
continues `{command:"sponsor", argument:"openai"}`. Sign the complete command including
provider, restore signed login and `actor_id`, and forbid arbitrary payer-ID input.

### Before enrollment: ingestion, order and visibility

1. **Receive and retain.** Extend `Ingress.comment` and its marked-App-comment path to
   recognize sponsor/withdraw candidates on unenrolled issues, including edits away and
   deletions of known candidates. Author association is a filter hint, not permission
   authority; self-withdrawal must not be discarded by the writer-association filter.
   Extend `Registry.receive` to commit the normalized source hint, receipt and existing
   `queue_observation` job together. Receipt alone is not a sponsorship record. Mark the
   issue projection provisional; do not run models or create a task merely to accept it.
2. **Confirm.** Reuse `Reconciler.observe_pending` with uncached, fully paginated issue
   comments/events and `observations.satisfied` expectations for human as well as delegated
   changes. Verify the stable actor for every candidate and current writer permission for `sponsor`
   only; `withdraw` is confirmed on authenticated actor identity alone. App commands additionally
   need exact `delegation.resolve` signature/provider validation. Store immutable source facts:
   comment/event ID, provider, actor ID, body/revision digest, created/updated timestamps,
   source order and `explicit_comment` provenance. Delivery ID/time is diagnostic only.
   Project confirmed slots even without a checkpoint. `Actions.act` already durably requests
   exact App-comment observation after posting, including uncertain replies; retain that path.
3. **Order.** Fold explicit commands by effective source timestamp (`created_at`, or
   `updated_at` for a revised command), comment ID, then proven same-comment revision order
   from previous-body evidence. Latest accepted claim wins each provider; a self-withdrawal
   only clears its caller's grant and leaves that actor's no-auto-reclaim tombstone. Never order by
   webhook arrival.
   For the initiation boundary, use the confirmed start-comment or label-event timestamp;
   same-second explicit comments take precedence over initializer fill (defined tie rule).
   Different comment IDs break same-second explicit ties; unordered conflicting revisions
   of one comment stay provisional until source observation resolves them.
4. **Edits/deletion before freeze.** Reconcile a candidate's current body/existence, not
   a stale delivered body. Removing or changing a confirmed grant before initialization withdraws
   that source and leaves an actor/provider tombstone; an edited valid new claim is a new revision
   in source order. Initiator fill is evaluated against explicit grants as of the initiation
   boundary, not as of the freeze. A sponsor, withdrawal, edit or deletion whose effective source
   time is after that boundary (or a deletion with no provable earlier time) but is observed before
   freeze is applied after fill as an ordinary post-initialization event: a new claim takes over
   through the epoch path, and a removal leaves the slot `withdrawn`. It never widens the
   initiator's fill. Record the applied source revisions with initialization so normal command
   processing does not apply them again. An explicit withdrawal is durable and never restores an
   older grant or reassigns the withdrawing person when its comment is deleted. It does
   not veto a different initiator's first-enrollment grant. After checkpoint adoption,
   processed grants are immutable: edits/deletions do not rewrite them; use explicit
   withdrawal/takeover. Keep accepted revision facts and the initialization cutoff.
5. **Freeze before charge.** `Worker.run` confirms the issue projection before
   `Controller.initialize`/`CommandHandler.start` can seed slots or admit a call. Read all
   pages without the reconciler's hint cache; satisfy known expectations, confirm the
   initiating event, and retry if source digest or observation revision changed during the
   read. In one registry transaction bind the confirmed revision to initialization only
   if no newer job/source hint intervened; the checkpoint adopts that exact grant set
   before initial command cursors can skip earlier comments. This fences local registry
   observations, not GitHub itself; it cannot prove an unreceived event does not exist.
   No charge while source history is known incomplete, a relevant observed delivery/read
   is unresolved, or permission lookup failed. Recheck the adopted revision against new
   sponsorship hints immediately before the first charge; reconcile if it changed.
   Existing retry/backoff handles uncertainty; no new consent click is needed for start.

`Registry.observations`/`finish_observation` already preserve a newer job when an older
revision finishes. Reuse that race handling; extend `Reconciler.candidates` with known
unenrolled intent keys, not a broad all-issue poll. `Recovery.poll` redelivers failed App
hooks and performs exceptional reconciliation; keep its existing coverage limits. A
label/start always does authoritative comment reconstruction even if its webhook arrived
before an older sponsorship webhook. `Worker.consumed`/acknowledgment must retain the
source revision actually adopted, never acknowledge a newer projection unseen.

GitHub pagination is not an atomic historical snapshot. If delayed source evidence is
found after an admission, never rebind or refund that attempt. Record an ordering diagnostic
with the frozen grant/source revision. A late-confirmed pre-boundary withdrawal by the
actor holding a starter/label grant applies once by source ID through ordinary task-local
withdrawal: epoch +1, withdrawn slot and caller/provider cutoff through the prior epoch.
It stops further use of that consent, without refunding or rebinding charged attempts.
A later explicit grant by that actor supersedes the old withdrawal; retain only audit
evidence in that case. Never rewrite an adopted slot in place. If the late evidence
proves an explicit claim that preceded the initiation boundary and still stands (source present, no
later withdrawal/takeover/revocation for that provider, actor passes a fresh writer check), apply it
for **future** admissions as an ordinary transfer: `pending_sponsorships[provider]`, epoch +1, a new
`grant_id` from that source and the same Exchange compare-and-set. The displaced owner's admitted
work finishes under its recorded epoch. A later confirmed takeover or withdrawal supersedes it:
retain the audit evidence without changing that slot. Hold affected new admissions for
reconciliation only when conflicting evidence cannot be ordered. Do not relabel a discovered
old claim as a new latest command merely because its webhook arrived late. Identified gaps in
pre-edit/deletion history are uncertainty, not permission to invent an initiator grant.

**Backoffice before a task exists.** Add issue-addressed
`GET /api/repos/{owner}/{repo}/issues/{issue}/sponsorships` for slots, provenance,
provisional/confirmed state, source links, projection revision (slot epoch only after
checkpoint adoption) and missing coverage, and the corresponding
issue-addressed command action through existing `ServiceCommands`/`Actions`. BFF verifies
issue visibility with the user's token, then reads the daemon projection (never opens
registry SQLite). This works without `Tasks.task_view` or a checkpoint. Merge known intent
issues into the target list (current `UserAccess.task_issues` lists labelled issues only)
and show an “OpenAI sponsored” badge with pending/confirmed detail; do not fabricate task
stage/budget. Targeted issue reads can trigger existing observation jobs when no projection
exists. Unseen issues outside webhook/history coverage appear when opened or enrolled;
no complete historical index is claimed.

**Transfer.** Distinct accepted provider commands increment only that slot's epoch, including
same-person re-opt-in; replaying a consumed comment ID is a no-op. Epochs exist only from checkpoint
adoption: the issue projection carries a revision, not an epoch (the issue-level API reports the
revision until a checkpoint exists). Adoption sets a slot with an adopted grant to epoch 1 and any
other slot to 0; every later owner change increments by one. Existing
admitted work finishes under its recorded sponsor/epoch and is accepted unless explicitly
revoked. New work uses the new owner. The other slot, approvals and findings are untouched.

**Task-local withdrawal.** `/concurro withdraw openai|anthropic`, using the same signed
command+argument transport, withdraws only the caller's consent for that task/provider.
If they own the current slot, clear its owner, set `withdrawn` and increment its epoch;
retain that tombstone. Retire only caller-owned sessions/queued work for that provider
through the recorded prior epoch, including their older epochs. Other prior/current
sponsors' admissions are unaffected. A former sponsor can withdraw their own older work
without clearing the current owner's slot or advancing its epoch. After initialization, no matching
grant is an idempotent no-op. Before first enrollment, a confirmed `withdraw <provider>` from an
actor with no grant still records that actor/provider tombstone, so that actor's own later
start/label does not fill that provider without a new explicit grant; other initiators are
unaffected. This never revokes the stored provider token globally.
Self-withdrawal requires authenticated owner identity, not continuing writer authority;
it authorizes no model work. Service/controller command authorization must permit this
narrow self-revocation even after writer loss, while sponsor/start/retry keep writer checks.

**Publication and races.** Checkpoint `pending_sponsorships[provider]` with operation,
actor, command ID, expected/new epoch, owner, retirement cutoff and transition ID. Exchange
applies it under the lifecycle lock with compare-and-set on that provider's fence and a
non-stale generation; same transition ID/digest replays as success, conflicting reuse
fails. Checkpoint the acknowledgment, then consume the command. Withdrawal also commits
its caller/provider cutoff revocation before retirement/key deletion, whose failures
reconcile normally. A historical-only withdrawal records its cutoff/idempotency event
without rewriting the slot. Before enrollment, preserve every actor's withdrawal tombstone
in the issue projection even if another actor later owns that provider. Crash at any boundary
replays the recorded operation.
Only affected-provider new admissions wait on a pending transition; aggregate required
preflight still requires all needed slots ready. Never reset/delete slot epochs, so
withdrawal and re-opt-in cannot reuse an earlier identity (ABA).

`Exchange.submit` matches only its selected provider's fence and advances that fence's
generation monotonically; first use initializes from the checkpoint, including an
unassigned tombstone at epoch 0; every owner-changing transition increments by exactly
one. Matching grant ID is required for a contributed new admission. It identifies an
explicit comment or confirmed one-time starter/label grant, never an inferred legacy payer.
Concurrent same-provider commands serialize by the existing comment cursor/transition
CAS; different-provider commands preserve independent epochs. Session cleanup filters
by task, provider, recorded sponsor and cutoff, never all sessions for a task.
Current-owner withdrawal uses the pre-transition epoch as cutoff; former-owner withdrawal
snapshots the current provider epoch as cutoff. Future re-opt-in must have a later epoch.

**Permission and enrollment.** A detected writer loss revokes all that user's consent
and admissions on this task across provider slots, including their old epochs; it never
revokes another sponsor or the user's other tasks/repositories/token. Clear only their
currently owned slots through the same monotonic transition path; record all provider
cutoffs atomically in Exchange before cleanup. These epoch cutoffs exclude any later
explicit re-opt-in, even in the same ownership generation. New opt-in requires
a fresh writer check and later epoch; stale credential-valid events cannot resurrect it.
Enrollment remains separate: sponsor changes never re-enroll or bypass
`refresh_autonomous`. Preserve its legacy `(actor,event)` comparison until saved stable
ID is known, then compare `(actor_id,event)`; update matching records without blocking
existing enrollment. Enroller/start actor identity alone is not enough: payment also needs the recorded
one-time initialization decision and its route-availability/source evidence.

The task UI lists each provider's owner, epoch, consent/pending/withdrawn state and missing
route/model coverage. A credential pause names the affected provider/sponsor; another
person cannot donate someone else's token or silently fill a missing slot.

## Coverage, admission and charge order

The provider-slot checks below govern contributed funding. Org-funded work keeps its
existing org credential binding, route coverage, call budget and execution-authority
checks; prospective personal slots are not payer grants or a prerequisite for org calls.
Incomplete shadow projections wait separately and never fabricate a once-only fill.

Add explicit profile-to-route selection to target settings and canonical provider metadata
to the trusted route catalog. Reuse `model_config.resolve`/`assigned`. Required coverage
is the union of roles across their provider-slot owners: at start use the configured
required roster; phase preflight uses effective assignments. Initial enrollment adopts
explicit-first slots and eligible initiator fill; any still-unassigned required provider
pauses before a charge. Recheck routing changes, all reviewer seats and
repair roles, not unused catalog profiles. Never change model or effort to fit a
credential.

Coverage checks `(route, credential kind, selected model)` and status through a
service-authenticated vault metadata endpoint (new: the vault accepts only user tokens
today). `Validator.valid` establishes authentication only. Keep entitlement evidence separately: for
API-key routes, the account's model list where the provider publishes one, otherwise a cheap
inference; for subscription routes, always the configured-route inference probe. Validation probes
are vault operations, disclosed as validation usage, never
task attempts.

Selection replaces the two `Agents.credential` call sites (`Controller.ask`,
`Design.reserve`) with one shared helper; `Agents.prepare` then checks the selected
credential against the resolved profile's route instead of against a recomputed org
identity. For serial calls and `Design.reserve` alike:

1. **Recover first.** Recover saved results/admissions before credential selection
   (`Controller.recover`); verify ownership and finish pending transitions for providers
   required by this admission. Do not reselect the payer of saved work.
2. **Preflight.** For every required role, resolve its route/provider, that slot's active confirmed
grant (explicit comment or recorded one-time start/label grant) and adopted source revision, owner's
exact credential version/model coverage and writer
   permission by ID:
   [resolve the login from the ID](https://docs.github.com/en/rest/users/users#get-a-user-using-their-id),
   apply the collaborator rule used by `GitHub.permitted`, and require the response's
   user ID to match. `Controller.permissions` caches by login for a whole invocation and
   is not used here. One fresh check per distinct sponsor covers one serial attempt or batch reservation.
   Preflight the entire initial batch across all owners before any charge. A
   GitHub outage is transient: nothing is admitted or charged, and it is not evidence of
   permission loss. A denial records task-local revocation for all that user's provider
   grants and retires only their work. Missing required consent/coverage pauses without
   charging; another provider's already admitted work is not retired.
3. **Budget.** Check `max_model_calls`. Optional work also protects pending required
   attempts with the cross-cutting formula; a missing optional sponsor, credential or allowance
   takes the deterministic path.
4. **Freeze, charge, save.** `begin_attempt` records the admission with the attempt;
   `charge_call` debits; the checkpoint is saved before any Exchange I/O. The initial
   design roster prepares fully, then charges and saves as one local batch, as
   `Design.reserve` does today. In `Controller.ask` selection moves inside the
   two-attempt loop: the restart after a `SessionFailure` is a new charged attempt and
   gets its own preflight and admission.
5. **Submit under the attempt's ID.** Derive the Exchange turn ID as
   `str(uuid.UUID(operation["attempt"]))`, passed through the existing
   `RunnerExecutor.submit(identifier=)`. `begin_attempt` stores 32-character hex, while
   the Exchange's `UUID` and HTTP route patterns require hyphens; normalize at the
   transport boundary without rewriting checkpoint or receipt identities. Pass it through
   both the serial path (`Agents.ask`/`RunnerExecutor.run`) and `review_batch.run`.
   Retransmission inside `RunnerExecutor.admit` therefore replays one turn.
6. **Exchange, existing turn.** Under the lifecycle lock the turn ID is looked up before
   any sponsor or status check. An exact replay returns the original admission, status
   or result with no charge, including after a transfer or revocation. A different
   digest is 409; a withdrawal tombstone still wins a late submission.
7. **Exchange, new turn.** Read vault metadata for the bound record/version and verify
   its route declares the bound canonical provider. Under the lock recheck only that
   provider's sponsorship fence, frozen grant and the
   local revocation tables and insert the turn, staffing and session atomically as
   today. Dispatch only after commit.

A charge saved before an Exchange refusal or timeout stays spent. Preflight avoids
predictable waste but can't make the checkpoint and the Exchange database one
transaction. After a crash the engine does not resubmit: as today, an unfinished attempt
becomes uncertain, its exchange result is never adopted by polling (`Design.open`), and
any further execution is a new attempt with a new charge and a new admission. The old
attempt keeps its frozen record in the versioned audit artifact written before dispatch
(`Checkpoints.save`/`history`), even when a later attempt replaces the live operation
fields. Reference that artifact revision for historical attribution rather than relying
on expiring Exchange turns or best-effort diagnostics. Do not broaden that recovery.

`Exchange.key`, `Exchange.next` and replacement staffing through `poll` check the
session's recorded provider/sponsor/epoch and credential version against task/provider/
caller cutoffs, task-authority and credential revocations, never another slot or GitHub. Graceful
takeover lets admitted work
finish; explicit revocation retires it. `key` rechecks after gateway activation before
returning; a partial creation still adopts its recorded key and budget. Host sessions
(`kind="host"`) carry no binding and can never resolve a contributed credential.

## Gateway selection and empirical phase 0

The [isolated result and reproduction](phase-0.md), with [hashed evidence](phase-0-evidence.json),
select native header-derived resolver metadata plus a narrow response-observation hop.
The fixtures use the exact pinned image and CLIs; production integration is later work.
Do not use `response.body` metadata expressions here: measured v1.7.0 behavior buffers
the main provider stream. Private resolver response headers avoid that side effect.

Recommended first choice: agentgateway authenticates the virtual key, then a resolver in
the gateway zone maps the key's `concurro.session` to the session's frozen binding in the
Exchange database and supplies request-local backend auth.
[Agentgateway's
documentation](https://agentgateway.dev/docs/standalone/latest/documentation/configuration/security/backend-authn/key/)
describes a backend key computed by a CEL `expression` from external-authorization
metadata, with routing-mode examples; it doesn't confirm the same for `llm` models or
for API-key metadata. `LocalAPIKey` has no backend-secret slot. Validate the actual image
and schema.

The secret-returning resolver endpoint is a separate vault listener on a network shared
only with the gateway container, with its own credential. The existing `edge` listener
retains user-owned credential writes and gains service-authenticated metadata; no public
import endpoint is added. Strip incoming
sponsor/account/plan/internal-auth headers, including repeated and mixed-case variants,
then derive upstream headers from the admitted session. Missing metadata, resolver
outage or revocation never reaches operator credentials. Disable implicit backend and
credential retries. Resolve on every request, enforcing revocation and known expiry; only an implemented
refresh adapter may supply a newer revision within that same version. Cache by version
and token revision with revocation invalidation. Keep secrets out of the gateway's request-log
database, traces
and logs; client error suppression alone does not protect internal logging.

Build isolated `gateway/tests/` fixtures using the exact image, fake providers and two
accounts. Save schema/config hash, image/CLI versions, redacted traces, results and the
chosen mechanism in this directory. Required acceptance:

| Experiment | Pass evidence |
| ---- | ---- |
| Provider isolation | Alice/OpenAI and Bob/Anthropic on one task use their recorded accounts/versions; forged route/provider headers cannot select a payer. Changes/withdrawal in one slot never rotate or retire another slot's session; no org fallback. |
| Resolver composition | Verified key metadata reaches external/model auth; denial makes zero provider requests; no shared mutable credential slot or credential-bearing response/diagnostic. |
| Streaming observations | 401, quota/rate 429, session budget denial, 5xx, HTTP-200 error event, disconnect and delayed completion map to sanitized request/attempt records correctly. |
| Revocation/refresh | Revoke before key/next, during activation and mid-stream; restart after a lost event or ack. Old results never revive retired turns. Single-flight refresh is tested with optional PR 5a, not required for token-only delivery. |
| Native harnesses | Pinned Claude/Codex fresh/retained/account-transfer calls preserve model, effort and result schema; no tokens in homes or archives. Both subscription routes preserve today's configured backend, auth/account/beta headers and actual pinned-CLI request shape; replacement after known/unknown expiry and rejected ChatGPT account metadata are covered. |
| Metering | Several HTTP requests in one CLI turn map to one charged attempt; delayed usage stays with the original turn; missing terminal usage stays unknown. |

Second choice is per-user/version dynamic model resources, only if metadata access blocks
the native path and isolation, resource reconciliation and model naming pass identical
tests. Do not put secrets in virtual-key metadata or silently widen the admin trust zone.
If native injection works but lacks trusted response observations, add a narrow
gateway-side provider adapter for injection, SSE and observations. A full inference proxy
is the last fallback, retaining agentgateway key/model/token-budget enforcement; prove
metering, cancellation and backpressure. A plausible CEL configuration alone is not
acceptance evidence.

## Login, refresh, revoke and trust boundaries

**GitHub session.** GitHub login and provider connection are separate. Extend
`GitHubOAuth` to keep the refresh token server-side and serialize refresh;
[GitHub's
flow](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/refreshing-user-access-tokens)
replaces both tokens and invalidates the old pair (eight hours and six months when
expiry is enabled for the App or requested with `offline_access`). Keep the browser
transaction binding; require the CSRF header on logout as on other writes
(`create_app.logout` accepts but doesn't check it). Logout ends the local session first
and attempts [remote token
revocation](https://docs.github.com/en/rest/apps/oauth-applications#delete-an-app-token),
with durable retry and status on failure. It does not revoke provider grants or transfer
tasks; an expired browser session doesn't stop work that still has repository authority
and a usable grant.

**Where secrets move.** The user pastes the CLI token into the authenticated portal;
it transits BFF memory through the existing credential PUT path and is encrypted/stored
only in the vault. Redact request bodies and exception validation input as for API keys;
never persist tokens in BFF sessions, browser storage, logs or diagnostics. Runtime
resolution stays in the gateway zone. No provider tokens reach engine state, GitHub,
runner env/homes/archives or token-returning APIs outside the resolver listener.

**Expiry and optional refresh.** A pasted access token is sufficient. Store a trustworthy
expiry only when the token or a demonstrated provider response supplies one; user-entered
or unsigned-token expiry is a hint, not validated metadata. Otherwise expiry is unknown.
When a token is known expired with no usable refresh path, or the configured route
terminally rejects its authentication, mark it unusable and notify the sponsor to log in locally and
replace it. Unknown expiry does
not make a successfully validated token unusable.

Refresh is a later optional adapter only when the user supplies usable refresh material
and the existing CLI-issued route's refresh behavior has been demonstrated. An opaque
Claude setup token or a standalone Codex access token is not assumed refreshable. Do not
borrow SIWC client registration, scopes, lifetimes, `earliest_refresh_at` or error codes.
For a demonstrated adapter, vault owns on-demand refresh and any provider-supported
schedule. Without it, manual replacement is the supported recovery path.

Refresh-capable adapters serialize per grant across processes with durable lease/revision
compare-and-set: persist `refreshing` before I/O, save the replacement envelope before
acknowledgment, and compare revocation fence before commit. Known transient failures use
bounded backoff. A timeout/crash after possible rotation, including an expired in-flight
lease, is ambiguous: request manual replacement unless this CLI grant demonstrably permits
replay recovery. A late worker cannot overwrite newer material or resurrect revocation.
Refresh never silently creates another account, version, budget or paid retry.

**Replace, rotate, revoke.** These are different events:

| Event | Credential version | Admitted work |
| ---- | ---- | ---- |
| Token refresh | Same version, new token revision | Continues |
| User pastes a replacement key/token (same or another provider account) | New version; old version retired and its ciphertext destroyed, as `Store.put` does today | Sessions on the old version are retired; the portal warns that unfinished work may be lost |
| User revokes, or the stored version is definitively unusable (known expiry with no usable refresh path, or trusted terminal `auth` evidence) | Status change and vault event committed together, then ciphertext removed | Sessions on that version are retired |
| Rejected replacement candidate or transient validation outage | Existing version unchanged; candidate is discarded | Existing sessions unaffected |
| Runtime quota exhaustion or rate limit | Preserve stored ciphertext; record the trusted category and retry information | Follow the pause/backoff rules below; do not require a new token merely to clear temporary quota exhaustion |
| Sponsor loses write permission | Stored tokens unchanged; task/user authority revocation and monotonic clearing of their owned slots | All that user's old/current provider admissions on this task retire; another user's slots/work and other tasks/repositories are unaffected |
| Provider-slot takeover | Unchanged; only this provider's epoch advances | Old admitted work finishes; other providers untouched |
| Task-local `withdraw <provider>` | Stored token unchanged; caller-specific task/provider cutoff, slot tombstone if currently owned | Only this caller's selected-provider admissions through the cutoff retire; other sponsors/providers continue |

The Exchange consumes vault events idempotently by cursor, retires matching sessions
through the existing `Exchange.retire` path (running turns lost, keys deleted with
reconciliation retry) and advances the cursor only after durable state. The resolver
denies a revoked version on its own, without waiting for that cleanup. In-flight paid
requests may finish; results of retired turns are refused. Local revocation always stops Concurro use and
destroys usable stored material. Attempt remote revocation only when supported for the
supplied CLI credential; otherwise direct the user to the provider/CLI account controls
without claiming the token has been invalidated there. A supported remote retry job is
separately encrypted and non-usable for inference, with `remote_pending` shown on failure.
No promise of instant cancellation or remote revocation for an opaque pasted token.

## Failure and recovery behavior

At HTTP request start, snapshot the authenticated key's session and active turn. Late
responses stay with that turn, never the next turn of a reused session. The runner
supplies neither the category nor the turn ID. Observations and usage reach the Exchange
through a private authenticated channel with unique request/event IDs. Activation and
model-list probes are non-turn traffic. Unattributable observations are not guessed from
the model name.

The Exchange returns the turn's trusted category with its result (`Exchange.observation`).
A resolver denial for known expiry without usable refresh, or terminal credential status,
makes no provider request and records trusted `auth` for the snapshotted turn. Token
expiry mid-turn therefore pauses in `awaiting_credentials` instead of consuming the
automatic session restart as `unknown`; revocation still retires the session.
`Agents.decode_result` raises a dedicated provider outcome for `auth`,
`quota_exhausted`, `rate_limited` and `budget_denied`, ahead of the generic `SessionFailure`, so
`Controller.ask` and `Design.failed` don't spend their automatic session restart on a
known provider condition. A failed result with no recorded observation is `unknown` and keeps
today's handling; any retry it triggers passes normal admission, which sees the vault's
current status.

| Category | Required-role behavior |
| ---- | ---- |
| Trusted `auth` / `quota_exhausted` | Pause in a new `awaiting_credentials` stage added to `SUSPENDED_STAGES`, reusing `Controller.suspend` and `CommandHandler.retry`; record the failed admission, route and model; notify the sponsor once per pause/version. Phase 6 retries only after a newer validated version and current admission checks. |
| `rate_limited` with bounded retry-after | Phase 6 schedules a fresh charged attempt after the recorded hint and normal admission. |
| Gateway `budget_denied` | `budget_exhausted`; only a writer retry opens the normal new charged session. A valid credential never auto-resumes this. |
| `unknown`, ambiguous stream or unattributed failure | Existing uncertainty/session handling; no raw provider text. |
| Missing/unusable required credential before charge | `awaiting_credentials` without a charge, naming the missing provider consent or exact route, model and role. |

Classification is an allow-list of structured codes observed on each configured CLI
route; status or message matching alone cannot separate quota from rate limits. Capture
redacted fixtures for both subscription backends in phase 0. Codes, refresh errors and
lifetimes from a separate OAuth product are not assumed to apply. Ambiguous responses
stay `unknown`; only trusted route evidence marks a credential unusable.

**Mixed-provider batches.** `review_batch.run` currently cancels every live handle in
`finally` when `Design.failed` raises. Introduce a provider-pause outcome in that existing
adapter/scheduler path: save the affected member's outcome and pause reason, suppress new
attempts for the paused batch, and continue observing/retaining unaffected already-admitted
members while ownership, task execution authority and their grants remain valid. Apply
results under the existing verdict/eligibility rules. Once those handles settle, persist
`awaiting_credentials` with the remaining work; a restart uses normal saved-result recovery,
never polling/adopting old-generation exchange results. No automatic credential restart
or new charge occurs while draining. Ownership loss, stop, invalidated work or a terminal
verdict still uses existing whole-batch cancellation. Tests revoke Bob's Anthropic grant
while Alice's OpenAI reviewer is live: only Bob's session retires, Alice's valid result is
retained, then the task pauses without another admission. This behavior belongs in PR 4,
not just Exchange session filtering.

For optional model unavailability, keep accepted reports and take deterministic
publication; ownership or execution-authority loss still stops optional work. Preserve
reports, findings, approvals, publication outboxes, transcripts and spent counters.
Unfinished workspace retention remains separate work.

[Phase 6](phase-6.md) implements automatic recovery. Vault events wake only tasks paused for that
provider/sponsor/route and recorded consent, through `Registry.enqueue`; the woken tick
rechecks current slots, withdrawal cutoffs, authority, coverage, epochs and budget before any new
charge. Backoff survives restart and duplicate
events. Never auto-wake stopped tasks, human waits, unknown failures or budget blocks.
The UI shows provider-slot owners/pending consent, org-funded shadow status, route/version, missing
coverage or
reconnect, and charged calls; token usage is supplementary. No secrets or provider
response text in GitHub notices.

## Implementation sequence and acceptance

New module paths describe responsibilities; search for existing helpers before adding
any. These PR numbers name work packages, not an unconditional landing order. Land the
service-authenticated vault metadata endpoint from PR 2 first. PR 1 then owns the trusted
route catalog's `sponsorship_provider` metadata and profile-to-route setting; PRs 3-4
consume them. A failed/unavailable metadata read never records an empty once-only fill:
contributed initialization retries without a charge, as for an unresolved permission
check. Org-funded work may continue under its existing binding while shadow bookkeeping
waits; it must not record an unavailable metadata read as an empty grant decision.

Release PRs 1-3 together before enabling provider-grant initialization or consuming
sponsor/withdraw commands on initialized tasks. This is a release dependency, not a new
product gate: all exposed commands have their complete checkpoint/Exchange transition
path. An earlier ingestion-only release may retain source hints/observations, but must
not advance a sponsorship command cursor or initialize grants. Tests cover intent
collected before the full release and applied exactly once afterwards. PRs 1-3 do not
depend on PR 0; PR 4 does. PR 5 completes both providers with manual replacement;
optional PR 5a does not hold it up.

| PR | Concrete files/deliverable | Acceptance evidence |
| ---- | ---- | ---- |
| 0: empirical selection | `gateway/tests/` spike, this directory's mechanism/evidence note, pinned gateway/CLI config | The isolation/streaming/account-transfer experiments above; failures retained and a mechanism chosen. |
| 1: issue intents and initiation in shadow | `service/ingress.py`, `registry.py`, `observations.py`, `reconciler.py`, `webhook_recovery.py`, `worker.py`; controller `step`/`initialize`, commands/delegation; `service/actions.py`; backoffice issue views/transport; trusted route/provider catalog and profile-to-route setting | Pre-label explicit sponsor wins delayed/out-of-order hooks; latest source claim/ties deterministic; provisional→confirmed badges without checkpoint; edits/deletions/withdraw tombstones; source permission/signature checks; initiator fills initially uncovered routes once; pre-enrollment withdrawal blocks only that actor, not another starter; no charge on incomplete history; legacy enrollment and receipts survive. |
| 2: vault lifecycle | `gateway/vault/vault.py` plus storage modules as needed: both subscription route record schemas, token/account metadata, known/unknown expiry, status/event log, service metadata API (prerequisite of PR 1), resolver listener, migrations; `gateway/compose.yaml`; `backoffice/auth.py`, `app.py`. Subscription `ROUTES` entries keep `None` and user PUTs keep today's 404 until PR 5 supplies the configured-route validator; the API-key `/models` `Validator` never accepts a CLI token. | Two-user isolation; invalid replacement preserves the prior token; successful replacement retires its version; AAD/wrapping-key rotation; redaction; event replay; metadata endpoint never returns secrets. |
| 3: provider admission and fences | `engine/agents.py`, `review_batch.py`, `relay.py`; Exchange provider fences/cutoffs; controller transition/grant publication; `Worker.consumed`; diagnostics | Source revision races initialization; post-label removal cannot widen initiator fill; late prior claim never rebinds charged work or defeats later withdrawal; UUID/digest/lost-reply replay; independent slot epochs; concurrent same/different-provider changes; withdrawal/ABA/crash cases; old caller epochs only; late pre-boundary initiator withdrawal; org-era starter grants never become payers at cutover, explicit grants do; pre-enrollment self-withdraw after writer loss and without prior grant; adoption epochs; permission-loss isolation; key/poll/revoke races. |
| 4: sponsored inference | Gateway config/resolver/private network/`Gateway.create`; both subscription and separate API backends; consume PR 1 route/provider catalog; `vocab.py`, aggregate coverage/pause rendering | Alice/OpenAI + Bob/Anthropic on one task; missing required grants and initial-batch coverage cause zero new charges; optional unassigned sponsor falls back; routing to new provider pauses; spoof resistance/no org fallback; token/account/beta injection, redaction and trusted failures unchanged; mixed-provider pause drains unaffected admitted reviewers without new charges, while task-authority loss still cancels all. |
| 5: both subscription token forms | Existing BFF/vault PUT schemas and route mappings; portal local-CLI instructions, token/account fields, validation/status/expiry and Replace/Revoke; matching frontend work in its own repo | Authorized local CLI login and token paste for each provider; selected-model probe through current configured route; rejected/missing ChatGPT account metadata; known/unknown expiry; token-only expiry and auth rejection during a running turn; pause→manual replacement→charged retry→retained resume→takeover→revoke. Two accounts per provider; invalid replacement keeps old token; no custom OAuth helper/edge import. Fake tests plus separately authorized real acceptance. |
| 5a: optional refresh | Only demonstrated CLI-grant adapter, supplied refresh material, vault lease/CAS and any supported schedule/revoke job | Single-flight, crash at each step, expired in-flight lease, refresh versus revoke, token expiry during a long turn and unsupported refresh falling back to manual replacement. No inferred SIWC metadata or automatic paid retry. |
| 6: recovery and provider-slot UI | Vault-event scheduling, `service/registry.py`, coverage/usage UI and docs | Valid-credential events never restore withdrawn consent; unrelated provider changes preserve sessions/transcripts; retries use recorded current slot consent and spend remains; budget never auto-resumes; duplicate events preserve backoff. |

Extend `engine/tests/test_attempt_artifacts.py`, `test_turns.py`, `test_delegation.py`,
design-batch/command tests, `gateway/tests/test_exchange*.py`, `test_vault.py`,
`service/tests/test_actions.py`, `test_worker.py`, ingress/observation/webhook-recovery tests and
`backoffice/tests/test_backoffice.py`. Use fake clocks/SSE and independent exchange
instances for races. Existing Python CI runs component `unittest discover`, Ruff,
Pyright, compileall and bundle packaging; run affected suites and full CI before release.
The original documentation review ran no application tests. Isolated phase-0 tests and
limitations are now recorded in [phase-0.md](phase-0.md); real-account acceptance remains
pending and is not implied by fake-provider results.

## Migration, operation and roll-forward recovery

1. **Back up.** Consistent vault/exchange/service databases, with the wrapping key
   escrowed separately; test restore with non-production keys. Additive, versioned
   migrations (the Exchange's `add_columns` pattern); verify old ciphertext still opens.
   No raw database or token dumps in proposal evidence.
2. **Readers before writers.** The Exchange accepts both binding field sets before any
   controller sends the new one; runners read binding fields by name and tolerate the
   additions. Add a sponsorship checkpoint format after report format 7
   (`report_store.FORMAT`); don't reuse bundle-resume format 6. Preserve legacy org
   receipts and counters. Coordinate with the launcher/engine compatibility work noted
   above.
3. **Existing tasks** keep legacy attempts, single-payer records and receipts as historical
   bindings; do not clone a legacy sponsor, enroller or starter into provider slots.
   Preserve provider-specific explicit grants when source evidence is sufficient. Recover
   a historical starter/label grant only with the actual immutable initiating event,
   actor, explicit-comment precedence and then-available-route evidence; do not infer old
   credential availability from today's vault. Otherwise keep that slot unassigned pending
   reconciliation or an explicit grant, without rewriting historical attempts.
   A target has one funding mode: org-funded (org credentials pay; slots are prospective
   attribution) or contributed (provider slots pay; no org fallback). Record the mode in
   force at each grant's source event, with its configuration evidence; if historical
   mode is unknown, a starter/label grant cannot authorize personal payment. Org-mode
   attempts keep the org credential identity/binding and org accounting, not prospective
   personal credentials. Existing admitted work always retains its recorded mode.

   On switching to contributed funding, explicit-comment grants carry over: the command
   and its UI mean consent to future contributed calls on that task/provider, including
   when the task currently uses org funding. State that meaning next to shadow status.
   Starter/label grants created under org funding remain historical attribution only:
   clear them via normal provider epoch transitions, do not rerun once-only fill, and
   wait for explicit provider sponsorship. A shadow start never silently becomes a
   personal-payment commitment. Migration is idempotent and retains transition receipts.
4. **Enable enforcement** after required slots have confirmed contributed-mode starter
   grants or explicit-comment grants, plus credential coverage, at an idle boundary or
   explicit writer recovery. New bindings pay future turns; accepted work and approvals
   stay. API routes may ship first; each subscription route waits for its own evidence.
5. **Observe** missing coverage, pause reasons, oldest pending revocation, stale keys,
   refresh ambiguity, resolver failures, fence conflicts and unknown usage, using
   sanitized task/attempt/event IDs. After restart, reconcile vault events, sponsor
   provider fences/withdrawal cutoffs, scoped session/key retirement and usage outboxes.
6. **Roll forward only.** Recover with corrective releases and forward migrations;
   deployment rollback and downgrades are not supported or required. Preserve recorded
   payers, spent budgets, approvals, transition receipts and revocation evidence. If an
   incident requires pausing admissions, drain or explicitly retire admitted work without
   switching its payer to org. Continue revocation and reconciliation during recovery.

Update `gateway/README.md`, `backoffice/README.md`, inference/service design and config
docs only as behavior ships. Deployment remains separately authorized, with exact release
CI and required route/settings migrations stated beforehand, and
the full-SHA command from the then-current deployment procedure. This proposal performs
no live login, provider spend, permission change, task retry or deployment.

## Remaining technical evidence

- **Gateway integration:** the isolated spike proves native header-based resolver injection
  and a narrow response observer. Production request identity, private authentication and
  durable observations are implemented in phase 4; deployment remains separately authorized.
- **CLI token instructions:** document the existing local Codex token-retrieval procedure
  without inventing an export flag; pinned help establishes device login, not token export.
  The portal uses the approved existing local retrieval procedure; Concurro reads no CLI cache.
- **Route/account validation:** prove both CLI-token model probes against current backends
  and how ChatGPT authenticates the supplied account header. Record known token expiry
  when trustworthy and display unknown otherwise. Phase 5 proves isolated fake-provider
  journeys only; real backend/account-header/transfer acceptance remains pending by user scope.
- **Optional refresh/revoke:** discover only the supplied CLI grant's usable material and
  demonstrated behavior; token-only manual replacement already completes the baseline.
- **Transcript portability:** preserve scope on takeover by default. If the account-transfer
  spike fails, add the selected provider slot's epoch for that harness in `sessions.prepare` and use existing
  restart/eligibility rules; document the exception before route activation.
- **Unchanged:** enrollment takeover stays separate; no sponsor/task cap; explicit target
  opt-in for org funding; non-writer sponsorship, broader payer assignment and invoicing
  remain outside scope. Provider terms stay the operator's/users' responsibility.
