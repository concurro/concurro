# Proposal: ordered personal credentials

**Status (2026-11-27): implemented.** Native accounts storage, ordered selection and
same-payer fallback are in the Rust stack. Add/reorder/remove reached task-visible metadata
in live checks; paid fallback on the Phase 5 stack remains unqualified live. See
[Phase 5 status](architecture/phase-5-plan.md#implementation-status).

The ordered list replaces the original four fixed credential cards. A person can keep
several subscriptions or API keys for one provider, choose where each is used,
temporarily disable entries, and let Concurro try the next matching credential when one fails.
Pasted credentials write through accounts to OpenBao without provider probes or
model-access checks. Sponsor entries store a relay binding instead of provider material.
Google supports `gemini-api-key` and Sponsor-only `antigravity-subscription`; agy owns
the subscription login and renewal ([operation](../antigravity.md)).

## Agreed behavior

- One list per provider, mixing API keys and subscriptions. Position determines priority.
- Optional role and profile filters. Unfiltered entries match all work for that provider;
  when both filters are present, both must match. Specificity never overrides list order.
- Automatic fallback to the next enabled matching entry after a definite credential failure.
- Existing sponsorship and org/personal funding choices remain authoritative. Fallback stays
  within the selected sponsor's list for that provider.
- Existing admitted work keeps its exact credential/version. Reordering, filtering or
  disabling changes future selection; removing an entry revokes its stored material.
- Existing credentials, audit history, spent budgets and unfinished work migrate forward.

Example:

| Priority | Name | Type | Roles | Profiles |
| ---- | ---- | ---- | ---- | ---- |
| 1 | Anthropic credits | API key | Triage/intake | `sonnet55-high` on the demo target |
| 2 | Claude personal | Subscription | All | All |
| 3 | Claude work | Subscription | All | All |

Matching triage starts with the API credits. Other work starts with Claude personal. If the
credits are unavailable, triage can use the subscriptions below them. The $100 allowance
is the user's reason for arranging the list; Concurro does not add a dollar-budget tracker.

## Current implementation and reuse

| Responsibility | Implementation |
| ---- | ---- |
| Credential writes and lifecycle | [Accounts native store](../../crates/accounts/src/credentials/native/mod.rs), [PostgreSQL storage](../../crates/accounts-postgres/src/credentials.rs); material in OpenBao |
| Owner views and as-of metadata | [Accounts views](../../crates/accounts/src/credentials/view.rs); activated history remains visible after material retirement |
| Ordered eligibility | [Shared metadata client and selection](../../crates/vault-v1/src/lib.rs), used against accounts in Rust mode |
| Grants, fallback and consent | [Task application](../../crates/tasks/) and [task storage](../../crates/tasks-postgres/); retries preserve the chosen payer |
| Execution and credential resolution | [Rust exchange](../../crates/exchange/) and [inference gateway](../../crates/inference-gateway/); an admitted attempt keeps its exact version |

The `concurro-vault-v1` crate name reflects its original bridge, not the live metadata
owner. The native accounts store replaced the vault in Phase 5. The frontend's
`me/credentials` page provides ordered entries and Sponsor device management. The sections
below retain the feature's behavior specification; old fixed-card and Python-backoffice
migration instructions no longer apply.

## Page and editing

Show provider headings with compact rows: drag handle, chosen name, credential type, enabled
toggle, a short filter summary and an actions menu. Provide keyboard-accessible move-up/down
buttons as well as drag reordering. Show saved entries only, with an Add credential button;
an empty provider list needs no placeholder card.

Add/edit asks for a name and type. New server-stored entries also need a secret and, for
ChatGPT, the supplied account ID. Sponsor entries use device pairing instead of a pasted
secret. Put optional filters under **Use for…**. Token replacement is a separate action on an existing entry; renaming or changing filters never requires entering the token
again. Type/provider are fixed for an entry; use another entry to change them.

Keep fingerprints, timestamps and connection instructions in expandable details or the edit
dialog. Remove repeated storage/funding explanations and internal terms such as “last
resolved” from the main list. An operational problem may show a compact status with a link
to the affected task; “Saved” must not claim provider authentication succeeded.

Disable retains the secret and skips the entry for future selections. Enable restores
selection eligibility but does not erase recorded execution failures. Remove hides the entry,
revokes its usable versions and retires affected sessions through the existing event path;
it does not delete audit metadata or revoke the account at the provider. Keep that distinction
in the removal confirmation. Adding and reordering require no extra confirmation.

## Filters and selection

Use these stable role keys, independent of display labels and controller stage names:

| Role key | Display | Existing stages |
| ---- | ---- | ---- |
| `triage` | Triage / intake | `intake` |
| `planner` | Planning | `plan` |
| `plan_reviewers` | Plan review | `plan_qc` |
| `implementer` | Implementation | `develop` |
| `pr_reviewers` | PR review | `review`, including standalone PRs |

Correction calls inherit the originating role and effective profile. Do not infer the role
from the task's paused stage or from a profile's name. Resolve an omitted filter or empty
selection as “All”. An all-roles entry also permits other existing model-call roles; a
role-restricted entry only matches its named roles. Do not create new evaluator or
consolidator behavior as part of this work.

Profile filters identify `(target upstream repository, profile key)`, using the canonical
target binding, not a display label or model string. This avoids two repositories' identically
named profiles being confused. Offer known profiles as a picker. Preserve selections whose
target/profile is temporarily unavailable; they match nothing until it returns. A profile
rename does not silently widen the filter. A model change under the same profile key keeps
matching; existing configuration-adoption rules still apply to the task.

For each call about to be admitted:

1. Resolve its effective role, target, profile, model, harness and canonical provider.
2. Apply the task's existing funding choice. Org-funded calls keep the org path.
3. For personal funding, resolve the current consenting sponsor and read their provider list
   and current credential metadata as one consistent selection snapshot.
4. Scan in list order, keeping entries that are enabled, not removed, match both filters,
   have usable stored material and have a supported adapter for this provider/harness.
   Apply exact-version failure exclusions and cooldowns described below.
5. Choose the first candidate. Freeze its existing `record_id`, route and version through
   normal admission; record entry ID, list revision and role/profile selection context with
   the attempt for explanation and recovery. Then charge and dispatch normally.

Do not probe the model or alter the requested model/effort to make an entry work. Route
compatibility means a supported transport, not proof of entitlement. Retain the exact
`Credential` identity shape where possible; selection explanation can live alongside the
frozen admission without changing its historical digest.

The selection snapshot defines ordering for concurrent edits: an edit affects selections
read after it commits. A prepared/admitted attempt keeps its snapshot. Exchange still checks
exact material availability and revocation; it must not reselect by current priority or
reject an admitted record merely because its entry was subsequently disabled or filtered.

Check credential availability for the imminent serial call or uncharged required batch,
not every possible future role. Intake must be able to run with a triage-only credential;
planning may pause later if it has no match. Preserve the existing batch reservation and
budget rules. Already completed or charged peers do not need fresh selection. Optional roles
retain their deterministic fallback rather than becoming prerequisites for required work.

For new personal selections, `provider_credential_routes` and per-profile `credential_routes`
no longer choose or constrain the credential type. Continue accepting old configuration and
reading old bindings; no target edit is required. New choices use the list. Update diagnostic
views to report a selected entry or “no matching enabled credential”, not “route not configured”.

## Storage, APIs and lifecycle

The identity and browser API rules remain. Vault encryption, AAD and lifecycle-feed
references below describe the original implementation; native storage uses the
[accounts contract](architecture/phase-5/s3-accounts.md) and `credential.*.changed` facts.

Add a stable entry identity separate from the immutable secret-record identity:

| Record | Fields / responsibility |
| ---- | ---- |
| Credential entry | Owner, entry ID, provider, fixed route/type, name, enabled flag, role/profile filters, position, entry revision, created/updated/removal times. |
| Secret version | Existing encrypted token/account record, linked to one entry. Replacement retires only that entry's prior material. |
| Provider list | Owner/provider revision plus ordered entry IDs, changed atomically with policy edits. |
| Policy history | Nonsecret revision history sufficient to reconstruct entry availability, filters and order at an enrollment boundary. |

Keep allocating `version` monotonically across **all** records for an owner/route, not per
entry. This preserves the existing primary key, `record_id(user_id, route, version)` and
AAD. An entry's versions need not be consecutive. Link existing rows through additive entry
metadata; change `destroy`, replacement, listing, metadata and rotation joins to use that
association. Removing one subscription must never destroy another subscription's records.
Keep exact-version private resolution and key rotation; neither endpoint performs selection.

Replace the route-scoped `credential_fences` check with an entry revision for edits, secret
replacement and removal. Retain a tombstone/revision after removal so an older write cannot
resurrect the entry. Reordering uses the provider-list revision. Entry mutations advance their
entry revision and the list's snapshot revision in the same transaction, but an individual
entry write compares only its entry revision: changing account A must not reject a save to
account B. Keep route-wide version allocation transactional solely to preserve immutable
record identities; it is not an optimistic-write fence. Migrate legacy fence evidence to its
entry without creating save events, then retire the old table/code from active use.

The owner-facing contract should provide:

| Operation | BFF endpoint | Behavior |
| ---- | ---- | ---- |
| List | `GET /api/me/credentials` | Ordered provider groups, entries, list revisions and supported types; no secret fields. |
| Add | `POST /api/me/credentials` | Type, name, secret/account, filters; append enabled by default and return stable entry ID. |
| Edit | `PATCH /api/me/credentials/{entry_id}` | Name, enabled and filters; preserve material/version. |
| Replace token | `PUT /api/me/credentials/{entry_id}/secret` | Atomically replace this entry's token/account version. |
| Reorder | `PUT /api/me/credentials/order/{provider}` | Exact ordered entry IDs from the current list, committed atomically. |
| Remove | `DELETE /api/me/credentials/{entry_id}` | Idempotent removal and scoped material revocation. |

Accounts implements these owner-authenticated operations natively. The browser never
chooses the owner, and accounts does not retain plaintext material in its database. Reuse existing CSRF, private-principal separation,
sanitized errors and write-only form handling. Mutations use the entry or list revision above
to detect concurrent changes; on conflict refresh the list and retain nonsecret draft fields. This is
write-conflict handling, not a provider-validation or confirmation step.

Add/replace need owner-scoped mutation IDs so a lost save reply cannot create another entry
or retire a second version on replay. Replaying the same mutation returns the original
result; reusing its ID for different input is a conflict. Persist no plaintext secret in an
idempotency journal. Reorder, enable/disable and removal must also be safe after a lost reply.

Remove route-addressed write endpoints in the coordinated release: they cannot identify one
of several entries. Return an actionable reload/upgrade response to a stale client rather
than guessing an entry. Keep historical reads and private record/version resolution compatible.

Extend the lifecycle feed with entry/list changes and stable entry IDs. Existing `stored`,
`validated`, `snapshot`, `retired`, `revoked`, `unusable` and `rewrapped` records remain readable;
do not rewrite their IDs or sequence numbers. Policy edits are selection events, never
credential-revocation events. Exchange can advance past them without ending live sessions.
Service consumes them to refresh candidate evidence and wake eligible paused tasks. Keep
event application, recovery scheduling and cursor acknowledgment transactional.

Current and historical metadata must return the current version **per entry**. Historical
queries also reconstruct that boundary's enabled/filter/order state. Preserve already frozen
initiator-fill evidence and explicit-first grants; current edits cannot retroactively make
someone a sponsor. New initialization uses the same provider/role/profile matching as selection.

## Automatic fallback and recovery

Fallback is a new admitted attempt through the service/controller, not a gateway retry with
a different secret. Settle the failed attempt and retire its execution session before dispatching
another. Preserve completed results and drain unaffected batch members as today. Retry only
unfinished work; do not repeat a successful reviewer because its peer changed credentials.

Persist failure exclusions in task recovery state, keyed by the consenting owner and exact
credential record/version. They survive role changes, duplicate events and process restarts;
they do not change the owner's enabled toggle or globally disable the entry on other tasks.
Thus an authentication failure is not rediscovered on every later phase of the same task.
If a trusted refusal is specific to model access, scope that exclusion to the requested
model as well; it says nothing about the same credential's access to other models.
Another task may spend one attempt discovering the same dead token. Accept this task-scoped
behavior for the first implementation and show the operational problem on the credentials
page; do not silently introduce global automatic disabling.

| Observed outcome | Selection and scheduling |
| ---- | ---- |
| Definite credential authentication failure | Skip that record/version on this task; try the next matching entry now. |
| Definite model-access denial | Skip that record/version for this model on the task; try the next matching entry without changing the model. |
| Definite quota exhaustion | Skip that record/version; use a trustworthy reset deadline if supplied, otherwise wait for replacement or explicit retry after alternatives are exhausted. Do not guess monthly reset dates. |
| Definite rate limit | Try another eligible entry now. Use its provider deadline when available, otherwise local exponential backoff. Never apply its deadline to a different credential. |
| Structured terminal credential-unavailable rejection, ambiguous between throttling and spending limits | Try another matching entry now; use a provider retry deadline if supplied, otherwise local backoff. Do not label the account exhausted or permanently exclude it. |
| Removed, revoked or expired material | Skip the unusable record; use another matching entry if the task's sponsorship still authorizes it. |
| Unknown outcome, transport timeout, unsettled request or unclassified error | Preserve existing uncertainty/reconciliation behavior. Do not automatically duplicate work on another account. |
| Task stop, withdrawn consent, authority loss or exhausted task budget | Existing task control wins; credential edits cannot bypass it. |

Use only authoritative execution observations joined to the admitted turn/request. CLI/model
prose is not failure authority. The current `provider_protocol.category` recognizes OpenAI API
codes and Anthropic API authentication, but subscription taxonomy is still unverified and
Anthropic `rate_limit_error` does not distinguish rate from spending limits. **Closing this gap
is part of implementation**, not an assumption that subscription fallback already works.

Separate certainty that a request was rejected from certainty about why it was rejected.
Add tested structured classifications for the routes used by the list. An authoritative
terminal credential-unavailable response can justify fallback without proving quota exhaustion.
A generic 403/429 or an interrupted stream alone is insufficient. Require the rejected outcome
to be joined to the exact request and the overall attempt to settle before starting another.

Existing pilot diagnostics record ChatGPT `token_expired` and Claude `429 / rate_limit_error`.
The Claude responses came from the failed validator while the native CLI succeeded: they
establish neither exhausted credits nor native-CLI throttling. No captured Anthropic
credit-exhaustion response is currently established. Use these records only for what they
show; do not invent missing payload fields when constructing fixtures.

Phase 3 must provide a classification matrix with route, structured response shape, source
evidence, rejection certainty and recovery action. Distinguish documented/synthetic behavior
from captured live behavior. The headline exhausted-credits case needs a supported terminal
rejection path; a matrix marking every relevant Anthropic case unsupported is not completion.
Use documented contracts, synthetic fixtures and sanitized existing evidence first. If a
remaining behavior needs live verification, report the specific gap and request a bounded
check separately. This proposal authorizes no live calls and introduces no save-time probes.

For throttling or coarse temporary unavailability without a provider deadline, reuse
[`service.failures.backoff`](../../service/failures.py): 60 seconds initially, doubling per
consecutive failure of that exact task/record/version, capped at one hour. Persist the count,
chosen deadline and whether its source is local policy or the provider. Reset the local count
after successful use of that version; a replacement starts its own count. Never shorten a
provider deadline to the local cap or label local backoff a provider reset estimate. Duplicate
observations, list edits and restart must not reset the count or deadline. Once due, the entry
can compete in ordinary list order again. These are normally charged attempts, subject to
the existing task budget, rather than a permanent demotion or an immediate retry loop.

After each settled failure, re-read current consent and list state and scan from the top,
excluding failed/cooling versions. Do not maintain a fragile numeric “next row” cursor. A
reorder or re-enable may expose another candidate but cannot erase a failure for the same
version. A newly saved version is a distinct candidate, even on the same entry. Key rewrap,
rename and repeated delivery are not new credentials and cannot reset a consumed retry.

If a candidate already exists, schedule recovery immediately through the existing queue;
do not wait for a new vault event or the five-minute metadata poll. If the remaining viable
candidates are cooling down, persist the earliest eligible wake time. If none can be selected, keep
`awaiting_credentials` with the affected role/profile and a useful reason. Entry saves,
enables, filter changes and reordering can wake that pause when they expose a candidate.
The owned tick rechecks the latest state before charging.

Reuse `/concurro retry` as explicit permission to reconsider the current failed choices for
that task after reconciliation. It must not reset spent calls, uncertainty allowances,
completed results or deadlines that still apply. A new failure records a new exclusion;
automatic recovery alone never clears an authentication, model-access or definite-quota
exclusion lacking a reset deadline. Preserve stop/resume semantics
for credential-paused standalone rounds as well as contribution tasks.

Every fallback attempt counts against `max_model_calls` and retains its actual owner,
record/version and failure in the ledger. Reading candidates or skipping an entry costs no
model-call attempt. Provider billing remains separate. Never switch sponsors, fall back to
org credentials, choose a different model, or reinterpret a provider failure as approval.

Account/type changes need a new execution session binding. Reuse Concurro's existing retained
conversation mechanism and task/report context; do not rely on a provider account's opaque
session ID being portable. Route-specific headers and account metadata come only from the
newly selected record. Prove switching in both directions between API and subscription routes,
including plan-review independence and retained PR-review follow-ups.

## Forward migration and release

Historical vault list migration, already implemented. The subsequent Phase 5 import
into accounts/OpenBao is documented in [S3](architecture/phase-5/s3-accounts.md).

1. Migrate vault schema 3 to a new schema transactionally. Create one deterministic entry for
   each existing owner/route and associate **all** its historical versions. Preserve ciphertext,
   AAD, record IDs, token revisions, timestamps, retirement history and event cursors. No token
   refresh or user re-entry is required. Reopening a migrated database must be a no-op.
2. Live stored entries start enabled with unrestricted filters and a type-based name. Seed
   order from the owner's existing personal-route preferences when unambiguous: if relevant
   target/profile selections agree on one type, place that type first. Org-funded routing is
   not evidence of a personal preference. Where selections disagree or no preference can be
   established, use subscriptions-first and explicitly report the conflict or missing evidence
   in the migration summary. Users can reorder immediately. Existing revoked entries remain
   historical/removed, never resurrected. Expired material stays expired.
3. Migration does not fabricate a fresh save or erase failed-version evidence. Seed entry/list
   policy history from known legacy availability; preserve unknown historical evidence as unknown.
   For pre-migration boundaries, preserve the old route-based initialization semantics and already
   frozen grants. New selection uses list priority, so old target route settings can cease to
   determine the account on a **new** attempt; old admitted work is unchanged.
4. Migrate Registry's owner/route cache to entry/record-aware candidate state. Map legacy failed
   versions to their exact migrated records; do not apply a route-wide `minimum_version` to a
   different entry. Retain consumed receipts and cooldowns. Reconcile eligible credential pauses
   once against the full list, including alternatives which existed before the failure.
5. Version new checkpoint/recovery state explicitly and read existing checkpoints/admissions.
   Preserve task ownership, original approval bindings, reports, unknown operations and budgets.
   Only the normal owned recovery path may admit new work after migration.

The vault cannot derive target preferences itself. Provide a read-only migration preparation
command in the service that emits a nonsecret owner/provider ordering manifest from current
personal grants and their effective target/profile configurations, including explicit
pre-enrollment personal selections. Record the source configuration refs and agreement,
conflict or unavailable evidence; do not infer preferences from historical charges alone.
Pass that manifest into the vault migration. Missing/inaccessible configuration is reported
and uses the stated default, not an extra enrollment gate. Do not rewrite the owner's order
on subsequent starts. Include entry-fence migration and preference seeding in phase 1.

Use a coordinated release with an idle execution window. Stop the backoffice API and service
dispatch, drain admitted work, capture the ordering manifest, and back up consistent
vault/Registry/Exchange state with their keys. Install the new readers, vault migration and API/UI together before allowing list writes
or dispatch. Earlier readers assume one record per owner/route and cannot safely consume the
new selection events. Recovery is roll-forward only; never restore old databases to undo a
list edit. No new GitHub App permission is required.

The feature should not require reviewer-prompt changes. If implementation changes bundle
identity or target configuration, follow the existing idle-deployment / writer-resume rules.
Publish exact migration and deployment instructions with the implementation, after choosing
the next schema/format numbers against then-current main.

## Implementation sequence and acceptance

Create one implementation worktree/branch per repository. Start the backend branch from the
commit containing this proposal; the backoffice baseline is named above. Commit coherent phase
changes on those branches and give Codex both branch heads plus a concise verification record
for consolidated review. Per-phase commits are authorized; pushing and merging before that
review are not. Keep unrelated pilot evidence out of these implementation commits.

| Phase | Deliverable | Acceptance focus |
| ---- | ---- | ---- |
| 1. Storage and contracts | Entry/list model, preference manifest, entry fences, forward migration, owner/private metadata contracts and replay-safe mutations. | Existing secrets and historical record identities survive; migration preserves unambiguous preferences; independent entries do not conflict. |
| 2. Selection | Shared eligibility/order logic, role/profile context, initialization, service admission and gateway compatibility. | No target route requirement; no future-role availability gate; exact bindings and org behavior preserved. |
| 3. Fallback | Structured execution errors, task-scoped exclusions, immediate recovery, cooldowns and session handoff. | Actual Controller/Registry/Exchange journeys, settled failures and restart races; no success inferred from a fake selector alone. |
| 4. UI and projections | Compact list, filters, ordering, toggles, details and matching mocks; update task funding/recovery explanations. | Live/mock parity, keyboard interaction, responsive layout and light/dark inspection; no route-coverage panel. |
| 5. Integrated acceptance | Migration fixtures, regression suite, implementation docs and Codex review of both committed branches. | No push or merge before review; any live pilot remains separately authorized. |

Required scenarios:

- Two entries of the same route save, replace, resolve and remove independently; cross-owner
  operations and internal bearers on owner APIs remain refused. Token/account pairs stay atomic.
- A migrated token resolves under its original binding. Historical as-of queries, removed
  versions, legacy snapshots and rewrap work before and after restart. Interrupted migration
  cannot partly adopt entries or lose usable ciphertext.
- Migration preserves agreeing API-first or subscription-first personal preferences, reports
  conflicting/absent evidence, ignores org-only route preferences and applies the manifest
  only once. Independent entry writes do not conflict; stale same-entry writes cannot undo removal.
- Exact list order, disabled entries, no filters, either filter alone, both filters, duplicate
  profile names across targets, deleted/renamed profiles and originating-role repair calls.
- The example's API-only triage preference with subscription fallback; planning goes directly
  to the subscription. Intake runs without a credential for a future role. Standalone reviews
  select their actual PR-review profiles.
- Disable/reorder/filter after a selection leaves its admitted binding intact; the next
  selection sees the edit. Removal still retires that record's live sessions. Concurrent edits
  and lost create/replace/reorder replies do not duplicate entries or silently lose changes.
- A real encrypted-vault → service → Exchange journey switches between two same-type accounts
  and between API/subscription types, both directions. The selected token/account and charge
  ledger agree; successful peers and prior reports are retained.
- Every supported structured provider failure fixture reaches the intended fallback. Unknown
  or partially completed requests do not create a second attempt; error text cannot steer selection.
- All candidates are excluded or cooling down, then the task pauses without immediate cycling.
  Rate cooldown permits another entry immediately and survives restart. No-hint temporary
  failures use persisted local backoff, return to priority order when due and remain within the
  task budget; definite auth/quota exclusions do not gain an invented provider reset time.
- A usable alternative already present at failure schedules recovery without a new credential
  event. Fail-before-save and save-before-fail converge; duplicate/out-of-order events, rewrap,
  consumed receipts and a crash between settlement and queueing create no extra attempt.
- Mixed-provider batches, sponsor transfer/withdrawal, stopped/paused standalone rounds, manual
  retry, exhausted budgets and ordinary org-funded contribution flows retain their contracts.

Run affected backend suites, lint/type checks and frontend checks/tests/builds. Extend existing
fixtures and recovery paths rather than building a second scheduler or gateway retry engine.
Review both repository diffs together. No additional product decision is required to start;
the classification matrix must state what is supported rather than claiming unverified cases.
Live verification gaps, if any, remain explicit in the review handoff.

## Related work

This proposal supersedes the single-current-route selection and newer-route-version recovery
rules in [per-user credentials](per-user-credentials.md) and its [phase 6 brief](per-user-credentials/phase-6.md).
It preserves the [cross-cutting admission rules](README.md#cross-cutting-rules): within-owner
credential fallback is separate from choosing a different sponsor or funding mode.
[Risk-driven routing](risk-driven-routing.md) supplies the effective profile to match, and
[consolidated reviews](consolidated-pr-reviews.md) retains its optional-model behavior.
The original list rollout covered the four vault-backed types. The
[relay integration](sponsor-relay.md) now uses the same entry/list policy and
immutable record versions for Claude subscription custody.
