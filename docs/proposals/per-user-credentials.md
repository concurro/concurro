# Proposal: per-user provider credentials

**Status: org-funded shadow sponsorship implemented; contributed inference remains proposed.**
Current behavior and remaining boundaries are recorded in [phase 1](per-user-credentials/phase-1.md). Revised after two Codex reviews against
the code and the pinned agentgateway v1.7.0.

The [implementation plan](per-user-credentials/implementation-plan.md) records the current
code baseline, coordinated protocol changes and delivery sequence. The user-directed
subscription flow reuses local CLI login and user-pasted tokens through the existing
portal/vault path; manual replacement is the initial deliverable, with refresh optional.
The "Today" section below predates backoffice sign-in and the API-key vault.

People register their own credentials and sponsor provider slots on an issue or task.
One person can sponsor OpenAI and another Anthropic, each using only their own credentials. A
credential failure pauses the task without losing accepted work.

## Today

- **One operator-owned pool.**
  - **Where the secrets are:** provider secrets live only in the gateway deployment
    (`/etc/concurro/gateway/secrets.env`, which `gateway/agentgateway.yaml` reads):
    - a ChatGPT/Codex subscription route, selected by the `x-concurro-plan: chatgpt`
      header and an account header;
    - an OpenAI API-key route;
    - a Claude subscription OAuth route.

    There is no Anthropic API-key route, and no service that refreshes credentials.
  - **Settings:** service settings hold the gateway URL, allowed models, a per-session token
    budget and per-harness headers, all deployment-wide.
- **Secrets never reach jobs.**
  - **Key minting:** for each runner session, `Exchange.key` → `Gateway.create` registers a
    virtual key (`llm.apiKey`) with metadata, allowed models and a token budget.
  - **The pinned schema:** agentgateway v1.7.0's `LocalAPIKey` has **no backend-credential
    field**. The schema does support model-level backend auth, CEL-derived credentials,
    external authorization and dynamic model resources.
  - **Inference bypasses the exchange:** `Gateway.call` sees only admin and activation
    requests, never provider responses.
- **No acting user on model calls.**
  - **Commands:** they require a writer (`controller.permitted`).
  - **Enrollment:** in autonomous mode, the enroller (the last writer to apply the
    `concurro` label) is stored as `state["enrollment"]`: its login and event ID, not its
    stable user ID (`Controller.enrollment`). `Controller.refresh_autonomous` blocks the
    task if that enrollment changes or loses authorization. Manual mode has no enrollment;
    `CommandHandler.start` starts the task.
  - **Recorded actors:** approvals and invalidations record an actor. An ordinary
    `/concurro retry` records only the consumed comment ID.
  - **Model turns carry no user.** CI events don't start repairs on their own;
    `retry_reviewed` requires a writer.
  - **The OIDC `actor_id`** in exchange claims is the App actor, not a person.
- **The audit hook already exists.** `engine/credentials.py:Credential` has the kinds
  `org` and `contributed`, plus an `account` field. Only `org` is used:
  `Agents.credential` names a harness route, never a person. `Credential.parse` accepts
  exactly the current fields, and `Controller.charge_call` keys its per-credential
  counters by `credential.id`.
- **Retained transcripts are scoped separately from runners.** `sessions.prepare` derives
  each role's transcript scope from the task, model, stage and config, and
  `Agents.prepare` loads the transcript into whichever runner takes the turn.
- **Provider failure categories aren't available.**
  - `agent_entry.run`, `Agents.decode_result` and `Controller.ask` distinguish
    uncertainty, timeout and runner termination. Ordinary failures may trigger one
    charged session restart before blocking.
  - Provider text is suppressed deliberately, so a 429, an exhausted quota and an auth
    failure can't be told apart.
- **Unfinished work isn't retained.** `turns.execute` extracts implementation files only
  after a successful result. Retained transcripts are not retained workspaces.
- **No user-facing web surface or OAuth.** `service/identity.py` does installation-token
  authentication only, and the edge serves only the webhook, the exchange and inference
  routes.

## Goals

- **No pooling:** a model call uses only the credentials of the sponsor recorded when that
  call was admitted. An org pool exists only where an operator explicitly enables it for a
  target, and it is never an automatic fallback.
- **Private, secure registration:** a user signs in with GitHub, adds, replaces or revokes
  their own credentials, and never sees anyone else's.
- **Recoverable failure:** a missing, expired or exhausted credential pauses the task with
  a clear notice to the sponsor. Accepted reports, published state and spent budgets are
  preserved. An interrupted turn's unfinished workspace may be lost; retaining it is
  separate work.
- **Keep the invariant:** provider secrets stay in the gateway's trust zone and never
  reach jobs, the engine or GitHub.

## Non-goals

- Billing, invoicing or cost reporting across users, beyond per-user usage visibility.
- Deciding whether a provider's terms allow a given use. That's the operator's and the
  users' responsibility; this design only ensures that each user's credentials are used
  solely for their own work.
- Workspace recovery for interrupted implementation turns.

## Design

### 1. Sponsorship

Each task has independent **provider sponsorship slots**, initially `openai` and
`anthropic`. The configured route declares its canonical provider; harnesses, model names
and client headers never choose the sponsor. Each slot has one
owner or is unassigned, with its own monotonic epoch. Explicit issue-comment grants take
precedence; first start/label can fill absent providers from the initiator's available
configured-route credentials.

- **Explicit sponsorship:** `/concurro sponsor openai` or `/concurro sponsor anthropic` assigns the
  verified writer issuing it, never a third party. Consent covers that task/provider's
  future authorized attempts/retries until transfer, withdrawal or authority loss. It
  works before enrollment; bare `sponsor` shows usage and current issue/provider slots.
  The same user can opt into both through two commands or an equally explicit UI action.
- **Initiator fill:** reconcile earlier explicit claims first, even when their webhooks
  arrive later. First start/label fills only absent slots with the initiator's available
  selected-route credentials; explicit sponsors awaiting credentials keep their slots.
  Evaluate fill at the start/label event, not the later worker freeze: a subsequent
  withdrawal, edit or deletion never broadens what the initiator pays for. A withdrawal
  before that boundary blocks auto-reclaim by that person (even without a prior grant),
  not a different initiator with matching credentials. After initialization, withdrawal, retry,
  resume, relabel and token failure never trigger automatic reassignment; future new providers require explicit sponsorship.
- **Record:** `sponsorships[provider] = {sponsor_id, login, epoch, grant_id, provenance,
  status}`; unassigned/withdrawn slots retain their epoch. Before adoption, issue intents
  have a projection revision, not an epoch; adoption assigns epoch 1 to grants and 0
  to other slots. Stable GitHub ID is the key.
  Immutable source comment/event ID, actor, revision and source order identify provenance
  `explicit_comment`, `task_start` or `label`. Signed commands bind the provider/actor.
- **Transfer:** only that provider's new admissions use the next epoch. Old admitted
  turns finish under their recorded payer, with their results accepted. Another provider's
  epoch, sessions and transcripts do not change. Command replay never increments twice.
- **Withdraw:** `/concurro withdraw <provider>` revokes only the caller's consent/work
  on that task/provider, including older caller-owned epochs. Clear/increment the slot
  only if still theirs; never erase another current/prior sponsor's work. Keep monotonic
  tombstones. This is self-revocation, available by authenticated identity even after writer
  loss, and does not revoke the provider token globally or invalidate approvals.
- **Authority:** detected writer loss revokes all that user's sponsorships/admissions
  on this task across providers, never another person's slot or another repository/task.
  Enrollment remains separate; sponsorship cannot bypass `refresh_autonomous`.
- **Accounting:** each attempt records canonical provider, sponsor ID, slot epoch and
  exact route/credential version. `Credential.id` represents full credential identity;
  existing call-ledger mechanics keep payers separate. The task UI shows every slot's
  owner, pending/withdrawn state and coverage; usage/events include provider attribution.

For example, Alice comments `/concurro sponsor openai` before Bob labels the issue.
Alice keeps OpenAI priority; Bob fills Anthropic only if his credentials cover a selected
route. Alice's missing token pauses OpenAI rather than charging Bob. Her sponsorship is
visible in the backoffice before enrollment.

Before initialization, persist/confirm issue intents through existing webhook observation
jobs and source GitHub comments. Latest valid explicit claim wins by source timestamp/ID,
not delivery order; same-second explicit claims beat initiator fill. Confirmed actor/provider withdrawal
tombstones survive comment deletion without vetoing another initiator's first grant. Pre-freeze edits/deletions reconcile source revisions;
after adoption, processed grants change only through commands. Incomplete history retries
before charging; late discoveries never rebind admitted calls. A still-effective earlier
claim changes future payment only through the normal epoch/Exchange transfer; later
confirmed changes win. See the
[reconciliation plan](per-user-credentials/implementation-plan.md#before-enrollment-ingestion-order-and-visibility).

### 2. Admission identity

- **The sponsor tuple:** `(canonical provider, user id, credential version, slot epoch)`
  joins the turn binding across `RunnerExecutor.binding`, `Worker.executor` → `Exchange.submit`,
  `session_binding` and `staffing`. That's a coordinated protocol change, because
  `session_binding` rejects unknown fields today.
- **Effect:** because the staffing group hashes the binding, runner sessions are never
  shared across provider slots, sponsors or epochs. Only the selected slot is bound;
  the whole task sponsorship map is never part of a session identity.
- **Retained transcripts are preserved across a sponsor change.** A transcript is task
  content, not a credential, so `sessions.prepare` keeps its scope and the new sponsor
  pays for replaying it. This avoids a design-review restart on every transfer. If the
  Phase 0 spike finds a harness whose resume is bound to the provider account, that
  harness's transcript scope gains the selected provider slot's epoch instead, and the
  existing design-review restart and eligibility rules apply.
- **A frozen record:** the tuple is fixed when the turn is admitted, and is never
  recomputed from "current" state during recovery (see the
  [cross-cutting rules](README.md#cross-cutting-rules)).
- **Approval stays separate:** approval identity (what was reviewed) and payment identity
  (who paid for it) stay separate. A sponsor change never invalidates an approval.

### 3. Coverage

- **Definition:** coverage is the set of `(route, credential type, model entitlement)`
  entries that the task's roster requires, for example "Claude subscription with access
  to `claude-opus-*`" or "OpenAI API key". Provider names alone aren't enough: OpenAI API
  access and ChatGPT access are different routes.
- **Required and optional roles:** coverage includes only roles the task can't proceed
  without. An optional role, such as the
  [PR consolidator](consolidated-pr-reviews.md#phase-3-clusters-consolidator-model), is
  never part of preflight and never puts the task in `awaiting_credentials`; without
  coverage it takes its deterministic fallback.
- **Preflight:** at `/concurro start`, and before each phase is admitted, aggregate each
  required role's provider-slot consent and its owner's credential/model coverage.
  Preflight all required owners before initial batch charging; missing coverage waits in
  `awaiting_credentials` with a notice that lists what's missing and links to the portal.
- **Per-attempt checks:** a runner obtains its key once, at `claim` (`Exchange.key`), then
  takes turns through `Exchange.next`, so a key-time check alone leaves reused-runner
  turns unchecked. Replacement staffing (`Exchange.poll`) also passes through `key` and
  `next`. The checks are:
  - **A new attempt** (admission, ending in `Exchange.submit` inserting a turn): the
    route-selected provider has a confirmed current grant, its sponsor is authorized and
    their exact credential is valid. A revocation between phases stops the next attempt, not the next phase.
  - **An existing attempt** (`Exchange.submit` replaying an admission): verify its
    request digest and return its recorded identity, status or result. It is never
    rebound to the current sponsor, re-checked as new, or charged again, so an old-epoch
    turn survives a graceful transfer.
  - **Queued work and key issuance** (`Exchange.next`, `Exchange.key`): check the
    **recorded credential version** of the attempt or session is still usable under the
    [vault's](#5-vault) revocation and failed-validation rules. They don't
    check whether its epoch is still the selected provider's current one. A graceful
    transfer never invalidates it; an explicit revocation does.

  The trusted sources are named in the
  [plan](per-user-credentials/implementation-plan.md#authority-and-persistent-records):
  the service-side controller reads GitHub for the writer decision, and the vault owns
  credential status.
- **No silent substitution:** a missing route is never replaced by another model.

### 4. Portal

- **Sign-in:** a GitHub App user-to-server web flow on the existing App:
  - a callback URL and the App's client credentials;
  - `state` and PKCE verification;
  - a `/user` lookup for the stable user ID.
- **Callback binding:** the callback must complete only in the browser that started the
  sign-in, so an attacker's valid callback can't sign a victim's browser into the
  attacker's account ([RFC 9700 §4.7](https://www.rfc-editor.org/rfc/rfc9700.html#section-4.7)).
  - Starting sign-in creates a short-lived transaction bound to the browser by a
    `SameSite=Lax`, HTTP-only cookie (a `Strict` cookie isn't sent on the cross-site
    return from GitHub).
  - The transaction holds the one-use `state`, the PKCE verifier and an expiry. The
    callback is accepted only if its `state` matches the transaction in that browser's
    cookie; the transaction is then consumed.
  - The authenticated session cookie that follows can be `Strict`, HTTP-only, with CSRF
    protection on every write.
- **Authorization is unchanged:** a GitHub login proves identity only. Repository-writer
  authorization (`permitted`) still governs commands and sponsorship.
- **Functions:**
  - view provider slots on unenrolled issues as provisional/confirmed, with source and coverage;
  - explicitly sponsor or withdraw your own payment; first start/label may fill absent
    covered slots, without replacing an existing explicit sponsor;
  - list your routes, and each credential's status: valid, expired, exhausted or
    revoked;
  - add or replace a credential (write-only, shown afterwards only as a fingerprint);
  - revoke a credential;
  - view your recent usage.

  Validate a new credential with a cheap model probe on its configured provider route
  before accepting it. For subscriptions, the user logs into the CLI locally and pastes
  the issued token (plus ChatGPT account metadata when needed); Concurro does not run a
  custom provider OAuth flow. Show known expiry or unknown, with Replace/Revoke actions.
- **Routes:** a small portal service behind the edge, for example `/concurro/portal/*`.

### 5. Vault

- **Location:** in the gateway's trust zone, **not** in the service registry.
- **Encryption:** envelope encryption, with the master key in a separate owner-only file.
  Records are keyed by `(user id, route)` and versioned.
- **Detection boundary:** a credential stops being usable when the portal records a
  revocation or a failed validation, or when the sponsor fails the writer check
  (`permitted`) at a new attempt's admission. Nothing outside those checks is detected;
  a permission change takes effect at the next admission, not instantly.
  - Only new-attempt admission checks GitHub permissions. A detected permission loss is
    recorded as revocation of all that user's provider grants/admissions on this task, which later
    `key` and `next` checks then enforce; those checks never query GitHub themselves.
- **Two different stops:**
  - **Graceful provider transfer** (`/concurro sponsor <provider>`): turns already admitted finish under the
    old epoch and their results are accepted. Their keys are deleted as their sessions
    end normally.
  - **Explicit revocation** (task/provider self-withdrawal, global credential revoke, or
    task-local permission loss):
    no new turn is admitted under it. Its sessions are retired with `Exchange.end`, which
    marks running turns lost and deletes their keys through the retry-tolerant deletion
    path. Withdrawal selects task/provider/caller/epoch cutoff; global revoke selects
    credential version; permission loss selects only that user's work on that task. A lost
    turn's result is not accepted, and the turn stays charged.
- **No promise of instant cancellation:** a provider request already in flight may still
  complete before its key is deleted.

### 6. Injection (decided by the Phase 0 spike)

Three candidate mechanisms, in order of preference:

1. **A native agentgateway route plus a small credential-resolution service:** model-level
   backend auth whose credential comes from the vault, keyed by the authenticated virtual
   key's metadata (a CEL-derived credential, or an external authorization hook).
2. **Dynamic per-user model resources,** if (1) can't key on the virtual key.
3. **A full Concurro inference proxy** between agentgateway and the providers.

Requirements for any of them:

- **No client-controlled sponsor:** the sponsor is never selected from a header the client
  controls. It derives only from the virtual key the exchange minted.
- **No fallback:** sponsored requests never fall back to the operator routes.
- **The runner is unchanged:** jobs see only a session key.

### 7. Failure classification and recovery

- **A trusted, attempt-scoped channel:** a gateway-side observation, keyed to the
  admitted turn and attempt, which the exchange records. A runner's own report of a
  credential failure is never trusted.
- **Categories:** an allow-list of `auth`, `rate_limited`, `quota_exhausted`,
  `budget_denied` (the gateway's own token budget) and `unknown`, with a bounded
  retry-after. No raw provider text is exported.
  - HTTP status alone isn't reliable: a quota response and a rate limit can share a
    status, and a streamed failure can follow an HTTP 200. Those cases map to `unknown`.
- **Outcomes:**
  - **`rate_limited`,** with a retry-after: `Transient`, and the task resumes
    automatically.
  - **`auth` or `quota_exhausted`** (the credential is unusable): the task waits in
    `awaiting_credentials` and the sponsor is notified. It resumes on `/concurro retry`,
    or automatically once the portal shows a valid credential (a later phase).
  - **`budget_denied`** (the gateway session's token budget is spent): this is not a
    credential problem, and a valid credential doesn't fix it. The task blocks as
    `budget_exhausted`, is **never resumed automatically**, and resumes only on a writer's
    `/concurro retry`, which opens a new, charged session exactly as a restart does
    today. Any other recovery, such as a larger allowance, needs its own accounting
    design first.
  - **`unknown`:** today's handling.
- **No refunds:** a charged attempt is never refunded, even with no final output, because
  it may already have made paid provider requests.

### 8. Spending limits

- `Gateway.create` budgets are per session. A cap per sponsor or per task (open question 3)
  needs separate accounting, for example per-key usage aggregated by the exchange.
- `max_model_calls` charging is unchanged.

## Phasing

0. **Spike,** against the pinned agentgateway:
   - Which injection mechanism works?
   - Can the gateway attribute provider failures to a virtual key or turn?
   - How do streamed failures appear?
   - Can each harness resume a retained transcript under a different provider account?
     (This decides the transcript rule in [Admission identity](#2-admission-identity).)
1. **Sponsorship in shadow mode:** record explicit issue intents, one-time initiator fill, independent epochs,
   transfer and withdrawal commands,
   and attribute each call. The org pool still pays, and this phase is **labelled as
   org-funded**. It makes the model visible without changing who pays. An explicit sponsor command
   authorizes future contributed calls, and the UI states that even during shadow mode.
   A starter/label grant made while org-funded does not: at contributed cutover it becomes
   unassigned via the normal epoch transition and needs explicit sponsorship; fill is
   never rerun. Org-funded attempts always use org identities/accounting. Deliver the
   metadata prerequisite and complete command/Exchange transitions in the release order
   specified by the implementation plan before exposing these behaviors.
2. **Portal and vault:** token storage, validation, revocation/status and provider-slot UI;
   preserve the local CLI token-paste baseline in phase 5.
3. **Enforcement:** provider-slot injection, aggregate coverage preflight and `awaiting_credentials`.
   Pause conservatively: a credential-category failure stops new admissions, lets valid
   unaffected already-admitted reviewers settle, and then pauses the task. Only a writer
   resumes it; execution-authority loss still stops the whole batch. The org pool is off unless explicitly enabled.
4. **Automatic recovery:** rate-limit resume, and resume from `auth` or
   `quota_exhausted` when the portal shows a valid credential. `budget_denied` stays
   writer-only.
5. **Subscription credentials:** reuse local Codex device login and Claude CLI token setup;
   users paste their tokens into the authenticated portal for versioned vault storage,
   route/model validation, expiry status and manual replacement. Preserve the existing
   gateway backends and account/beta headers. Refresh is optional only for supplied usable
   refresh material with demonstrated CLI-grant behavior (see the
   [plan](per-user-credentials/implementation-plan.md#cli-tokens-and-existing-subscription-routes)).

## Invariants

- Every contributed call has a confirmed provider grant (explicit comment or recorded
  one-time starter/label assignment) and uses that slot's
  sponsor/credential version frozen at admission. An explicitly org-funded target uses
  org identities instead; it is a distinct mode, never a fallback.
- Provider secrets never leave the gateway trust zone: not in jobs, the engine, state,
  logs, GitHub comments or errors.
- Spent budgets are never reset or refunded by a credential change, a failure or a sponsor
  transfer.
- Approval bindings don't depend on payment identity.

## Risks

- **Session-key misuse:** hostile code in a job can spend the sponsor's quota until the
  session key is deleted or its budget runs out. That's the same exposure as today, but
  now it hits a person's quota. Keep per-session budgets tight.
- **Vault compromise** exposes every user's credentials. Mitigations: encryption at rest,
  a minimal portal surface, no read-back, and prompt revocation.
- **Token expiry:** subscription tokens need manual replacement when expired or rejected.
  Automatic refresh is optional and depends on the supplied CLI grant, not assumed OAuth
  metadata; it does not block initial subscription support.
- **Stuck tasks:** a sponsor who leaves stalls the task. The pause notice and the explicit
  takeover command address this.

## Open questions

1. How should a separate re-enrollment command recover lost enrollment authority?
   Provider sponsorship and self-withdrawal never implicitly re-enroll the task.
2. Should non-writers, such as an issue author without write access, be able to sponsor
   their own requests?
3. Should sponsors be able to cap their spend per task? That needs per-key usage
   aggregation, and interacts with [risk-driven routing](risk-driven-routing.md).
4. Org pool policy: never, or opt-in per target for shared bot accounts?
