# Phase 1: org-funded sponsorship in shadow

**Implemented in this changeset; contributed inference remains deferred.** Base:
`b6526d69cf97a78455d78d3b6ba04ee0722e2d10` (phase 0 rebased onto
main `a4263cb9d10923f5c091b39e021be1ed7f7b3ed0`).

The service records prospective provider consent while every inference call keeps its
existing org credential, runner binding and credential ledger. `operation.shadow_sponsorship`
records route, canonical provider, prospective owner/epoch/grant and confirmation status
separately from actual payment. No contributed binding, gateway injection, response adapter,
subscription-token acceptance, refresh or automatic credential recovery is enabled.

## Current behavior

- `/concurro sponsor openai|anthropic` assigns only the verified caller. Explicit commands
  authorize future contributed calls, including authorized retries. GitHub/backoffice
  notices label current calls org-funded. Bare `sponsor` gives usage without assigning.
- Signed webhooks commit revision hints, receipt and the existing observation job together.
  Fresh paginated comments/events, ID-bound writer checks and the local registry revision
  fence confirm the issue projection. Accepted commands are ordered by source timestamp/ID,
  never webhook arrival. Human edits require the observed editor to be the consenting author;
  unknown editor evidence remains pending. Signed delegated envelopes keep their verified actor.
  Known gaps remain pending; GitHub pagination is not an atomic snapshot.
- Earlier explicit claims reserve their provider even without credentials. The first accepted
  start/label fills only absent providers covered by that initiator's selected-route credentials
  at its recorded source boundary. Metadata/source failures preserve that boundary for retry;
  they never record an empty once-only decision. Edited human START uses its effective
  update time and observed author/editor evidence before implicit fill; full explicit coverage
  needs no initiator grant. Signed delegated START uses its HMAC-bound actor. Org work may
  continue while bookkeeping waits.
- Pre-freeze edits/deletions retain source evidence and withdrawal tombstones. A change after
  the initiation boundary does not widen initiator fill. After adoption, processed sources
  remain immutable; use a new explicit command to change consent. A pre-enrollment withdrawal
  prevents only that actor's automatic reclaim, and never vetoes another starter.
- Each provider has its own monotonic epoch. Pending checkpoint transitions publish through
  the authenticated Exchange operator API with generation/epoch comparison and exact replay
  receipts. A lost acknowledgment replays the same transition. Withdrawal records only the
  caller/provider cutoff; permission loss publishes that user's task/provider cutoffs together.
  Other sponsors/providers, org-paid work, approvals and spent counters are preserved.
- Pending source/config/coverage evidence waits for new input rather than self-polling. Transient
  shadow I/O retries through the existing queue and exponential backoff with a persisted
  shadow failure count; permanent fence/config errors wait for reconciliation/operator input.
  Terminal tasks still process explicit consent/withdrawal receipts when woken.
- Scheduling acknowledges the exact projection observed, separately from checkpoint adoption
  and pending provider transitions. A permanent pending condition does not repeat on every poll;
  a new source revision still wakes reconciliation.
- Late prior evidence is recorded against the frozen initialization and charged call count.
  A still-effective prior claim changes future attribution through an epoch transition;
  later accepted explicit consent/withdrawal takes precedence. Charged records are never rebound.
- Old org checkpoints adopt only confirmed explicit grants. They never infer past credential
  availability or turn an old starter into a prospective personal payer. Org-era starter/label
  grants record `source_funding_mode: org`; future contributed cutover must clear these through
  ordinary epoch transitions and require explicit sponsorship, without rerunning fill.

## Configuration and migration

The trusted catalog is `engine/sponsorship.py:ROUTES`. Profile-to-route selection is explicit
in the target configuration, independent of harnesses and client headers:

```json
{
  "credential_routes": {
    "codex": "openai-api-key",
    "claude": "anthropic-api-key"
  }
}
```

For named profiles, use their profile names as keys. Only selected role profiles contribute
coverage; legacy plan reviewers reuse `policy.design_roster`. The catalog also declares
`chatgpt-subscription` and `claude-subscription`, but registration for those routes still
returns 404. Their local-CLI-token validation/storage/use is a later phase. Catalog membership
alone does not establish a deployed contributed inference backend.

Configure the service's private metadata client separately from its Exchange operator:

```json
{
  "vault_metadata_url": "http://concurro-vault:8090",
  "vault_service_token_path": "/etc/concurro/service/vault-service-token"
}
```

The two settings are optional together; without metadata or selected routes, implicit
initialization waits and org execution continues. Explicit pre-task projections still work.
Provision a dedicated random 32-byte token as 64 hex characters, separate from the vault
master key, Exchange operator token and GitHub credentials. The vault reads its owner-only
copy through `VAULT_SERVICE_TOKEN_FILE`; the service reads its own owner-only copy. Copies
must contain the same bytes and be owned by their respective runtime users (vault uid 65532,
service uid 65533). Compose mounts only the vault copy; the service settings directory is
already mounted. Provision these files before deploying the updated Compose configuration.
No token was provisioned or deployment performed by this implementation.

The new private endpoint is `GET /metadata/{user_id}?at=<ISO timestamp>`, authenticated only
by that service bearer. It returns route/version/opaque record ID, status, validation model
IDs and timestamps, never fingerprints, tokens or ciphertext. The service bearer is refused
by user credential endpoints before any GitHub request. API-key validation retains only
model-list evidence; missing model evidence displays unknown/missing coverage. Historical
retirement/model records preserve availability at initiation; unknown legacy history stays
unknown. Only selected grant evidence is copied into the task, not an owner's full inventory.

Historical unknown model evidence cannot be reconstructed by later token re-registration.
The phase-1 baseline allowed initialization to remain pending: later explicit claims are visible in the issue
projection but have no adopted task epochs yet. A later contributed cutover must reconcile
those explicit grants while preserving the unknown initializer; it must not rerun fill or
infer historical consent from today's inventory. Phase 1 did not implement that repair; [phase 3](phase-3.md#source-consent-and-cutover) now adopts confirmed explicit grants while retaining unknown historical availability.

Registry schema 6 adds issue sources/projections with additive migration. Every task ticked
by the daemon gains shadow bookkeeping (including pending initialization) and moves to wire
format 8, although payment remains org-funded. Standalone controllers without shadow fields
still write format 7. The new reader accepts both and retains older receipts/counters.
Exchange adds provider fences, receipts and cutoffs. Recovery is roll-forward only: preserve
these records through forward migrations and corrective releases. Backups support data
recovery, not deployment rollback. Target route changes still require normal configuration
adoption/resume for existing tasks.

## Backoffice API boundary

| Surface | Current implementation |
| ---- | ---- |
| `GET /api/repos/{owner}/{repo}/issues/{issue}/sponsorships` | User-visible issue read first, then daemon projection; works without checkpoint/stage. Returns owners, source revision/provenance, pending/confirmed status, missing coverage and org-funded notice. Epochs appear only after task adoption. |
| `POST /api/repos/{owner}/{repo}/issues/{issue}/commands` | Existing session/CSRF and `ServiceCommands` transport; same `{command, argument}` grammar as task commands. The daemon independently resolves the actor; self-withdraw does not require continuing write permission. |
| Target/overview lists | Provisional hints and confirmed intent issues join an `issues` array with sponsorship data; confirmed empty hints leave discovery. New sponsorship discovery polls facts/live tasks; existing ordinary-command watches remain. Enrolled tasks remain in `tasks`. Unenrolled issue records have no invented stage or budgets. Every GitHub issue read uses the viewer's token. |
| Private daemon `POST /sponsorships` | Verifies user identity and issue/repository visibility; reads Registry projections, never opens SQLite in the BFF. Coverage refresh reads stored metadata without advancing a task. Unknown issues request the existing targeted observation: a changed snapshot discovers missed explicit comments, but an empty read creates no durable sponsorship row or recurring poll. |

The frontend is a separate repository. This changeset supplies API/view/badge data and command
transport; it does not render a new badge or sponsorship control in that frontend. Read
failures mark sponsorship unavailable without hiding existing task lists.

## Integration with current main

The launcher discovers its immutable engine through Exchange and supplies that engine when
claiming a key. Shadow transitions preserve this handshake, queued commands behind a stop,
issue body/triage views and release-image configuration. Phase-0 CLI versions are unchanged;
main moved their pins from workflow environment variables to `engine/agent-clients.txt`.
The opt-in spike now checks that file and states the bound engine on key claims. Earlier
phase-0 source hashes and measurements remain historical evidence, not rebased test results.

## Verification and remaining boundaries

Tests exercise real Controller checkpoints, signed webhook ingestion, Registry observations,
worker acknowledgment and Exchange transactions with isolated fake GitHub/vault/gateway data.
Coverage includes source ordering, pre/post-boundary removal, deleted withdrawal tombstones,
permission loss, independent epochs, lost replies/restart, legacy org migration and API
visibility/CSRF. Full local CI and the exact reviewed file hashes are recorded with the
implementation review evidence.

Real-account checks remain deferred by the user's phase-0 instruction. The observation hop
remains a test fixture. Contributed admission, aggregate coverage enforcement, secret injection,
credential lifecycle events and durable provider-failure recovery remain later work in the
[implementation plan](implementation-plan.md). Stored cutoffs do not turn an org session into
personal work. This phase does not claim production subscription-backend support.
