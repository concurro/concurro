# Sponsorship: current operation and deployment readiness

The [main integration](main-integration.md) supersedes this review's App, visibility and
release-entrypoint guidance; earlier evidence below remains historical. Phases 0–6 are implemented. Baselines are signed backend
`abd891415407358527734ed84fe91ea840852f61` and portal
`3ad054413292c8e47242e5c5f88c273e530ab89a`; final review corrections require their own
committed release. Nothing in this guide deploys or retries a live task. Optional provider
refresh (5a) and user-deferred real-account acceptance remain separate.

## Production path and evidence

| Boundary | Current implementation | Local evidence |
| ---- | ---- | ---- |
| Identity and owner registration | `backoffice/app.py`, `auth.py`, `credentials.py` → vault owner API. GitHub session refresh/logout is separate from provider tokens. Candidate model-set probes use the owner's supplied token on fixed routes, then revision CAS commits one version. | Phase 2 auth/lifecycle tests; Phase 5 owner journeys, probe deadlines and replacement/revoke races. |
| Issue consent before a task | Ingress/observation jobs → `Shadow.confirm` and Registry projection → issue-addressed backoffice views/commands. Signed delegated comments bind stable actor/provider. Full source reconciliation precedes initialization. | Phase 1 ordering/editor/permission/replay tests and live-mode intercepted portal controls. |
| Initiation and admission | Current route catalog defines provider. Prior explicit sponsorship wins; first source-era starter fill covers only its own available routes. Whole required roster preflight → immutable UUID/digest/admission → checkpointed debit → Exchange. | Phase 3 lost-reply/fence/withdrawal/permission/cutover tests, charge history and retained-result recovery. |
| Inference and observation | Real key/active-turn start hook → native key/model/budget enforcement → exact-version private resolver → native header auth and narrow streaming hop. Native denial importer joins the server nonce to its original turn. | Phase 4 pinned-image/fake-provider streaming, spoof, cancellation, budget, shared WAL and TLS experiments. |
| Provider lifecycle | Vault replacement/revoke events → Exchange retirement, without payer rebinding. Task-local withdrawal differs from global token revoke; transfer is graceful for old admitted work. | Phase 3–5 real vault/Exchange fixtures; two fake accounts/provider and retained native-CLI transcripts. |
| Recovery and views | Each normal delivery-recovery timer polls service-principal metadata events. Registry cursor/cache/queue commit atomically; owned tick rechecks current consent, versions, required models, authority and budget. UI separates recorded charges, prospective grants and consumed/scheduled receipts. | Phase 6 real timer → TCP Store feed → Scheduler/Worker/Controller/Exchange, replay/restart/backoff/multi-route tests and loaded light/dark views. |

See [implementation plan](implementation-plan.md), phase notes and their review records for
exact historical artifacts. Synthetic providers establish local mechanics, not real backend
acceptance, authenticated account identity or cross-account portability. Missing usage stays
unknown. Unknown subscription errors retain existing handling; no API-key/OAuth taxonomy is inferred.

## Required deployment wiring

These are existing component contracts, not new consent/spending gates. The supplied gateway base Compose requires its master key and metadata bearer even for
org-only operation. Other optional features remain opt-in; an org-only deployment retains org credentials/accounting. Contributed
routes never fall back to them.

| Component/principal | Configuration and actual release path |
| ---- | ---- |
| Gateway/Exchange | Deploy current `gateway/agentgateway.yaml`, Exchange image and Compose together. They share a network namespace. Keep `EXCHANGE_PRIVATE_LISTEN=127.0.0.1:8081`, `EXCHANGE_NATIVE_LOG=/gateway-data/requests.db`, writable gateway `/data` and the same volume read-only at Exchange `/gateway-data`. The start hook also applies to org traffic; unavailable Exchange fails closed. No Caddy route or published port for 8081. |
| Vault metadata principal | Mandatory in base Compose: `VAULT_SERVICE_TOKEN_FILE=/run/secrets/vault-service-token`; file is 64 hex characters, owned by uid 65532 with owner-only access. Service gets its own owner-only copy for uid 65533 through `vault_service_token_path`; `vault_metadata_url=http://concurro-vault:8090`. This bearer cannot resolve material. |
| Exchange metadata reads | In the external gateway `exchange.env`, set `EXCHANGE_VAULT_METADATA_URL=http://concurro-vault:8090` and `EXCHANGE_VAULT_METADATA_TOKEN` to that metadata token's value. Do not put either token in GitHub, target config or source. |
| Gateway private principal | Provision a **different** 32-byte hex `vault-resolver-token` file for uid 65532; mount it via `VAULT_RESOLVER_TOKEN_FILE`. Set `EXCHANGE_VAULT_EVENTS_URL=http://concurro-vault-private:8091` and `EXCHANGE_VAULT_EVENTS_TOKEN` to this distinct gateway token. This existing principal serves events **and** exact `/resolve`; service/backoffice never receive it. Supply all four Exchange vault values together. |
| Private network | `deploy/host.sh` uses gateway base Compose plus `/etc/concurro/gateway/compose.host.yaml`. Merge `gateway/compose.private.yaml` into that existing host override, preserving existing state binds. Only gateway/Exchange and vault join `credential-private`; vault binds 8091 on its private alias. Merely retaining the optional file in Git does not activate it. |
| Vault persistence | Preserve owner-private `/data/vault.db` with WAL, `VAULT_MASTER_KEY_FILE` and any named keyring. Schema 3 migrates forward. Back up consistent SQLite state and escrow wrapping keys separately; do not recreate the store or delete volumes. Unknown historical validation/expiry remains unknown. |
| Service/task authority | Preserve Registry identity/state (schema 8), source policy intervals, pending transitions and checkpoints (format 9 on charged saves). `delegation_key_path` is needed for portal-posted signed commands. Service and backoffice must name matching target repositories and enrollment destination. Existing App callback/permission/subscription contracts still apply. |
| Backoffice/portal | Use `public_url=https://<host>/concurro`, matching Sign-in OAuth App callback `/concurro/auth/callback`, its client secret, separately provisioned `reader.app_id`/`reader.private_key_path`, `service_url=http://concurro-service:8787` and `vault_url=http://concurro-vault:8090`. Build portal in live mode at `/concurro`; it has no provider/privileged gateway secret. GitHub login proves identity; configured-upstream writer checks authorize new sponsorship. Reader App installation credentials are separate from the scope-free Sign-in identity. |
| Optional durable logout | Only when configuring both `auth_state_dir` and `auth_key_path`: provision uid-65533 host state/key, set container `auth_state_dir=/var/lib/concurro/backoffice-auth`, and merge `service/compose.portal.yaml` into `/etc/concurro/compose.service.host.yaml`. The helper includes this optional override on every service/web operation, preserving the state mount. Without it/configuration, base-only behavior and truthful best-effort cleanup remain. This is unrelated to provider refresh. |

Host overrides remain outside Git. Keep the nonsecret service override at the traversable
`/etc/concurro/compose.service.host.yaml`, not inside private uid-65533 settings. Render the **actual** base-plus-host Compose inputs with
`config --quiet` before release; inspect network/listener/mount identities without dumping
secret-valued environment output. The gateway helper always uses its existing host override;
the service override is used only when present. Preserve native request logs and Exchange
outbox/identity state together; lost logs remain unknown rather than reconstructed usage.
The isolated read-only WAL proof does not benchmark persistent-volume capacity/durability.

A target configures `provider_credential_routes` defaults with optional existing
`credential_routes` profile overrides through its normal reviewed configuration. Each issue
selects organization or personal funding per provider with `/concurro funding <provider> org|personal`;
legacy `funding_mode` initializes old policies without rebinding enrolled tasks. Models/roles
come from the existing roster, not a parallel vault catalog. API keys and subscription routes
remain distinct. See the [current contract](../../design/service.md#provider-funding-and-credential-routes).
Org-era starter/label grants remain historical at cutover; explicit provider grants can carry
forward. Unknown old source-policy/credential availability cannot create personal consent.
New providers require explicit grants. Existing admitted work keeps its original payer. Upgrade the gateway/Exchange before the
service when introducing provider funding commands; older Exchanges do not accept the new
`funding` transition reason. Updates and state migration are roll-forward only.

## Final review corrections

The release helper now discovers the optional nonsecret service overlay outside private
settings, so durable logout mounts survive service and web updates. Base gateway installs
must provision the documented vault metadata bearer as well as its master key. Portal
outages show unknown funding; canonical credential/budget pauses remain parked and visible
without relying on mock-only flags. Budget attention directs a writer to normal Retry,
not token replacement or automatic recovery. Live command receipts carry no mock label.

A missing native request row remains pending while its owner session is live, regardless
of unrelated later rows or log replacement. Existing explicit end, expiry, revocation and
authority-loss retirement transactions append a sanitized `native_gap` observation for an
otherwise unobserved request; startup reconciles already-ended owners too. This settles
missing usage/completion as unknown, without a new timeout, failure classification, payer
selection or debit. Late native evidence appends under the original request/turn/binding;
a trusted late denial still takes precedence over unknown. A log watermark cannot prove
a delayed writer finished, so it is deliberately not used as the settlement boundary.

Native-log availability is exposed separately from provider observations; corrupt/missing
logs reveal only `unavailable`, never paths or exception text. The existing independent
session reconciler can end an orphan owner without a successful result poll; ordinary
writer recovery then rechecks all admission conditions. Existing org SQL/lock boundaries
remain unchanged. Local tests use synthetic keys/providers. A current-source, UID-65532,
read-only/no-network vault startup was also checked over a retained dependency image;
a fresh offline image build lacked cached pip dependencies and is not release-build proof.

## What remains before deployment

1. Commit/sign review corrections, publish approved backend/portal releases and pass remote
   CI/image publication for the exact SHAs. Local checks and historical image experiments do
   not replace that release evidence. Once those SHAs exist, use the full-SHA commands in
   [the deployment guide](../../../gateway/README.md#releases); no deployment was authorized here.
2. Provision/verify the chosen external settings, distinct principal files, persistent mounts,
   private network and optional logout override above. Preserve state through forward migration.
   This review does not inspect private host configuration or establish what is already provisioned.
3. Check App grants and webhook subscriptions, approved upstream/fork workflow blobs/rules,
   target caller/config pins and `gateway.workflow_ref` against the intended launcher/engine.
   Finish active work or use explicit writer recovery for changed bundles/configuration;
   never rewrite approval hashes or retry a live task automatically for rollout.
4. Keep the explicitly deferred real checks visible: both subscription model probes/backends,
   actual ChatGPT account-header semantics, trustworthy token expiry when available and real
   cross-account transcript portability. Fake success proves none of them or provider approval.
   These are pending acceptance evidence, not a newly imposed provider-agreement gate.

Optional refresh is not required for the approved local-CLI login + token-paste/manual
replacement baseline. No custom provider OAuth app, token-export flag, automatic refresh,
new spend cap or organization fallback is part of this release.
