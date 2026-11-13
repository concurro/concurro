# Phase 2: portal and vault lifecycle

Implemented on backend phase-1 base `9c9c11a5d597487acdda6492bf8d4ab7dabc140d`
and frontend base `5822541b7aa412678ffbef01eda55888f41283ab`. Current inference, attempt
bindings and accounting remain org-funded. This phase adds the personal-record lifecycle and
portal; contributed admission/enforcement, response observations and automatic recovery remain
later work. Real subscription-account acceptance remains pending by user direction.

## Current surfaces

- API-key add/replace validates with the provider before committing; reject/outage preserves the
  previous version. Revoke destroys stored material locally, not the provider's own key. Metadata,
  expiry evidence, version, events and private exact-record resolution are documented in the
  [vault guide](../../../gateway/vault/README.md). No private resolver consumer is enabled.
- The separate frontend renders OpenAI/Anthropic prospective owners, pending source/task adoption
  and configured-route coverage; caller-only provider sponsor/withdraw uses existing signed
  command transport. Pre-task issues have their own view, overview/target list and consent actions.
  My sponsorships uses stable GitHub IDs and includes visible historical consent. Writer loss does
  not remove self-withdrawal. Sponsor enablement follows the issue repository’s write capability,
  separately from the configured fork’s permissions. No arbitrary caps or new consent gate are introduced.
- Personal credential forms support API keys only, clear secret inputs after every attempt/close,
  and show known/unknown expiry. Subscription user PUTs and BFF route mappings remain unavailable;
  privately sealed token/account schema does not prove a CLI/backend/account is supported.
- GitHub sessions refresh only when GitHub actually supplies a usable rotating pair and lifetimes.
  Refresh is single-flight per session; logout fences a late pair. An ambiguous one-use refresh
  requires fresh sign-in rather than replay. Sessions remain memory-only; restart signs users out.
- Logout requires the write header, ends local authorization first and attempts single-access-token
  remote revocation. Optional encrypted durable retry survives restart, uses leases/backoff and
  exposes anonymous pending/revoked/unknown status. It never deletes the user's whole App grant.
  An interrupted in-flight refresh, failed late-pair journal or undecryptable cleanup job
  leaves an unknown remote outcome; known late access tokens still get best-effort revoke. No claim is made that
  revoking one access token invalidates every refresh token in the App authorization.

GitHub protocol follows the official [refresh fields and rotation behavior](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/refreshing-user-access-tokens)
and [single-token revocation/check endpoints](https://docs.github.com/en/rest/apps/oauth-applications#delete-an-app-token).
It does not force offline scopes or App setting changes and does not imply provider-token refresh.

## Configuration and release boundary

The supplied current gateway base Compose requires `vault-service-token` alongside its
master key; provision both before starting it, including org-only deployments. The original
Phase 2 no-new-secret code boundary is historical. Private resolver activation is
optional as described above. Durable logout retry is enabled only when both `auth_state_dir` and
`auth_key_path` are configured; without them local logout still works and makes a best-effort
remote call, reporting unavailable on failure or unknown if a refresh was in flight at logout.
The portal preserves that result when there is no durable status receipt. Provision an owner-only external directory and
32-byte hex key for uid 65533. For the release helper, merge `service/compose.portal.yaml`
into `/etc/concurro/compose.service.host.yaml` (read when present by both service/web
updates); merely retaining the optional overlay in the checkout does not activate it. Set:

```json
{"auth_state_dir":"/var/lib/concurro/backoffice","auth_key_path":"/etc/concurro/backoffice/auth-key"}
```

The SQLite queue contains encrypted GitHub access tokens, never provider keys. Back it up with
its separate key; startup checks key continuity and refuses a wrong key. Drain pending jobs before
changing that key; key rotation for this optional queue is an operational replacement after drain,
not vault wrapping-key rotation. Restart preserves queued jobs but marks unresolved in-memory
refresh cleanup unknown. Recovery is roll-forward only: retain the queue/key and resume
cleanup with a corrective release. Vault schema and key-rotation recovery are described in
its guide. No checkpoint/Exchange schema changes are introduced by phase 2; phase-1
checkpoint 8/schema 6 remain unchanged.

Deploy compatible BFF and frontend releases together when authorized. The frontend's live and
mock modes share the same API shapes. No deployment, real credentials or provider calls were
used for local verification. Phase-1 historical-metadata/pending initialization limitations remain;
adding a key now cannot reconstruct historical availability or silently turn an org-era starter
grant into personal-payment consent.

## Verification

Tests use actual vault SQLite/envelope transactions, actual separate TCP listeners with Host/
forwarded-header spoof attempts, synthetic GitHub/provider transports and real BFF-to-vault
requests. They cover two-owner isolation, replacement/revoke/events/restart, legacy AAD, key
rewrap transaction atomicity, expiry, secret-free errors/metadata/events, refresh concurrency/logout races,
revocation retry leases/key continuity, pre-task visibility and stable actor filtering. Frontend
checks/builds and light/dark browser evidence are recorded with final review provenance.

Fake validation proves local behavior only. Subscription selected-model/account probes and
live transfer/refresh/revoke evidence remain deferred; no phase-0 historical run is relabelled.

List views still read projected, unlabelled issue intents individually through the user's
transport. Moved/gone (HTTP 301/410) entries are skipped; permission and transient failures
remain visible. Large intent sets therefore retain a sequential per-issue read cost; no
new pagination restriction or parallel observation framework is introduced here.
