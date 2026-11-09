# Proposal: per-user provider credentials

**Status: proposal (epic), not implemented.** Revised after two Codex reviews against
the code and the pinned agentgateway v1.7.0.

Each person who works with Concurro registers their own model-provider credentials, and
the model calls for the work they sponsor use those credentials and nobody else's. A
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

Each task has one **sponsor** at a time.

- **The record:** `{github_user_id, login, credential record and version per route,
  epoch}`. The stable user ID is the key, since a login can be renamed.
- **The first sponsor:** the actor who started the task, by stable user ID:
  - **autonomous mode:** the writer who applied the `concurro` label (enrollment must
    also record that actor's user ID, not just the login);
  - **manual mode:** the writer who posted the accepted `/concurro start` comment.
- **Transfer:** only through an explicit writer command, `/concurro sponsor`, which
  increments the epoch. No other command transfers sponsorship implicitly.
  - **Turns already admitted** finish under the epoch they were admitted with. Their
    results are accepted, and charged to the previous sponsor's credential.
  - **New turns** use the new epoch.
- **Enrollment is separate.** Enrollment authority (`refresh_autonomous`) and
  sponsorship are different things. Whether a sponsor transfer may also re-enroll the
  task, for example after the original enroller loses access, is an explicit decision
  (open question 1). Until it's decided, losing the enroller's access still blocks the
  task.
- **Audit and accounting:** each model call records `Credential(kind="contributed",
  account=<user id>, route, credential version)`. This extends `Credential` and
  `Credential.parse` together, and `charge_call` keys its counters by the full identity
  (kind, account, route, version) rather than `credential.id`, so different sponsors'
  calls are never merged. The dashboard shows the sponsor and the calls charged to each
  account.

### 2. Admission identity

- **The sponsor tuple:** `(user id, credential version, epoch)` joins the turn binding
  across `RunnerExecutor.binding`, `Worker.executor` → `Exchange.submit`,
  `session_binding` and `staffing`. That's a coordinated protocol change, because
  `session_binding` rejects unknown fields today.
- **Effect:** because the staffing group hashes the binding, runner sessions are never
  shared across sponsors or epochs.
- **Retained transcripts are preserved across a sponsor change.** A transcript is task
  content, not a credential, so `sessions.prepare` keeps its scope and the new sponsor
  pays for replaying it. This avoids a design-review restart on every transfer. If the
  Phase 0 spike finds a harness whose resume is bound to the provider account, that
  harness's transcript scope gains the epoch instead, and the existing design-review
  restart and eligibility rules apply.
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
- **Preflight:** at `/concurro start`, and before each phase is admitted, check that the
  sponsor has a valid credential for each required entry. If not, the task waits in
  `awaiting_credentials` with a notice that lists what's missing and links to the portal.
- **Per-attempt checks:** a runner obtains its key once, at `claim` (`Exchange.key`), then
  takes turns through `Exchange.next`, so a key-time check alone leaves reused-runner
  turns unchecked. Replacement staffing (`Exchange.poll`) also passes through `key` and
  `next`. The checks are:
  - **A new attempt** (`Exchange.submit`, inserting a turn): the task's current sponsor is
    still authorized and their credential is valid. A revocation between phases stops the
    next attempt, not the next phase.
  - **An existing attempt** (`Exchange.submit` replaying an admission): verify its
    request digest and return its recorded identity, status or result. It is never
    rebound to the current sponsor, re-checked as new, or charged again, so an old-epoch
    turn survives a graceful transfer.
  - **Queued work and key issuance** (`Exchange.next`, `Exchange.key`): check the
    **recorded credential version** of the attempt or session is still usable under the
    [vault's](#5-vault) revocation and failed-validation rules. They don't
    check whether its epoch is still the task's current one, so a graceful transfer alone
    never invalidates it; an explicit revocation does.

  The implementation brief must name the trusted source of the writer and
  credential-status decisions used by each check.
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
  - list your routes, and each credential's status: valid, expired, exhausted or
    revoked;
  - add or replace a credential (write-only, shown afterwards only as a fingerprint);
  - revoke a credential;
  - view your recent usage.

  Validate a new credential with a cheap provider call before accepting it.
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
    recorded as a revocation of that sponsor's credentials for the task, which later
    `key` and `next` checks then enforce; those checks never query GitHub themselves.
- **Two different stops:**
  - **Graceful transfer** (`/concurro sponsor`): turns already admitted finish under the
    old epoch and their results are accepted. Their keys are deleted as their sessions
    end normally.
  - **Explicit revocation** (the sponsor revokes the credential, or loses permission):
    no new turn is admitted under it. Its sessions are retired with `Exchange.end`, which
    marks running turns lost and deletes their keys through the retry-tolerant deletion
    path. A lost turn's result is not accepted, and the turn stays charged.
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
1. **Sponsorship in shadow mode:** record the sponsor, the epoch and the transfer command,
   and attribute each call. The org pool still pays, and this phase is **labelled as
   org-funded**. It makes the model visible without changing who pays.
2. **Portal and vault:** API keys only, with validation, revocation and status.
3. **Enforcement:** per-sponsor injection, coverage preflight and `awaiting_credentials`.
   Pause conservatively: any credential-category failure pauses the task, and only a
   writer resumes it. The org pool is off unless explicitly enabled.
4. **Automatic recovery:** rate-limit resume, and resume from `auth` or
   `quota_exhausted` when the portal shows a valid credential. `budget_denied` stays
   writer-only.
5. **Subscription credentials:** refresh, expiry handling and portal flows for subscription
   logins.

## Invariants

- No model call uses a credential other than the sponsor's at admission, except through an
  operator-enabled org pool.
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
- **Refresh complexity:** subscription tokens expire, and the refresh logic becomes a
  dependency. Phase 5 is deliberately last.
- **Stuck tasks:** a sponsor who leaves stalls the task. The pause notice and the explicit
  takeover command address this.

## Open questions

1. When the original enroller loses access, should `/concurro sponsor` also re-enroll the
   task, or should a separate re-enrollment command exist?
2. Should non-writers, such as an issue author without write access, be able to sponsor
   their own requests?
3. Should sponsors be able to cap their spend per task? That needs per-key usage
   aggregation, and interacts with [risk-driven routing](risk-driven-routing.md).
4. Org pool policy: never, or opt-in per target for shared bot accounts?
