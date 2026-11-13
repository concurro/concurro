# Phase 2 review record

**2026-11-12 — Claude Fable 5.2 medium: `findings: []`.**

The final review covers [portal and vault lifecycle](phase-2.md) across the backend and
companion frontend. CLI 2.1.301 used `--model claude-fable-5-2 --effort medium --tools ''
--strict-mcp-config --output-format json`. The wrapper reports `is_error: false` and canonical
model `claude-fable-5-2`; session `f99d92e4-04d2-4de5-9cf0-327e66eace65`. The CLI's
`unrecognized_model` warning is retained; actual model usage confirms the requested model.

Four initial findings were repaired and one refuted; the resumed review explicitly reports no
findings. It read immutable source/delta/evidence with no tools, and did not execute tests or
recompute hashes. Worker and primary separately verified the actual identities. This record
was added after the verdict and is **outside** the 52-file reviewed manifest.

## Dispositions

| Finding | Final disposition |
| ---- | ---- |
| F1 | Skip only moved/gone per-issue HTTP 301/410 failures; other errors and transient outages remain visible. Existing sequential per-issue reads retain their documented scaling cost. |
| F2 | Refuted using actual model-config/policy sources and empty/complete legacy target tests: failures are caught KeyError/ValueError/TypeError, with no Blocked path. No speculative catch was added. |
| F3 | Failed late-pair journalling latches/persists unknown cleanup and best-effort revokes the known late access token. |
| F4 | Undecryptable jobs become terminal unknown under the exact claim fence; all receipt reads distinguish unavailable storage from no receipt. |
| F5 | Follow all Anthropic model cursor pages; incomplete/malformed/cyclic evidence stays unknown. The upstream page-size maximum is not a model-count cap. |

Primary acceptance also repaired issue-repository write capability independently of fork access,
and non-durable logout during refresh (including a late pair finishing during old-token revoke).
The portal preserves the returned cleanup result when no durable receipt exists. Tests cover
both permission directions, self-withdraw after writer loss, actual httpx 401 replay, configured
optional state startup, lifespan retry and revoke success/network failure.

## Verification and limits

| Check | Result |
| ---- | ---- |
| Engine / service / deploy | 1,175 / 476 / 13 tests in full `checks01`; one existing Linux-only engine test skipped on macOS. Sources unchanged by the repair. |
| Gateway / backoffice | Final 112 / 44 tests passed (`gateway-repair03`, `bff-repair14`). |
| Ruff / format / CI Pyright / compileall | Passed on repaired source. Pyright covers the CI-configured extracted engine modules, not broad strict typing of the BFF/vault. |
| Shellcheck / bundle packaging | Passed in full `checks01`; affected sources unchanged. |
| Frontend | Zero Svelte diagnostics; live and mock builds passed (`check07`, `live04`, `mock04`), unchanged by repair. |
| Browser | Mock provider isolation, disabled subscription forms and secret clearing; intercepted live-mode permission/withdraw/logout tests. No live account calls. |
| Visuals | `visual05` resolved issue/task screens; `visual06` resolved credentials/sponsorship rows in both themes. Premature skeleton captures and driver failures remain separate negative evidence. |
| Optional Compose / links / whitespace | Overlays rendered without containers; local Markdown links, source hashes and both repository whitespace checks passed. |

Raw packets, deltas, CLI results/model usage and `final-verdict.json` remain in
`/private/tmp/concurro-phase2-review`; full gates in `/private/tmp/concurro-phase2-checks01`,
final affected logs in `/private/tmp/concurro-phase2-*-repair*.log`. Screenshots/text are in
`/private/tmp/concurro-phase2-visual05` and `visual06`; live-mode intercepted evidence is
`/private/tmp/concurro-phase2-live-browser.json`. Earlier failed fixtures/runs are retained.

Actual inference remains org-funded. The optional private listener has no production resolver
consumer; container-alias startup is configured/rendered but unverified in a running deployment.
Contributed enforcement, subscription registration/refresh, automatic inference recovery and real
subscription-account checks remain later phases. Durable GitHub cleanup is optional; without its
new key/state configuration, local logout works with honest best-effort/unknown remote status.
Vault rotation needs retained old keys and a stopped-writer operation; rollback requires rewrapping
to the legacy key or restoring a consistent backup. See the runtime note for exact boundaries.
The reviewer also noted existing phase-1 per-actor permission metadata as possible later projection
minimization; it classified this as an observation outside this phase, with no remaining finding.
No phase-2 deployment, commit or push was performed.

| Artifact | SHA-256 |
| ---- | ---- |
| Final 52-file manifest | `f4575cda1e6e9d8a9e86d04f4b526c0f569881d8ff49b129dff8e99cc01a355c` |
| Final review input | `5087d5b948e756779d850bb37994fbcafcde77094de67829a5b89d94140b21f8` |
| Final raw CLI result | `90919835603793d660d3328110055265d268797f93670401e973cb2bacc50504` |

## Reviewed files

Backend base: `9c9c11a5d597487acdda6492bf8d4ab7dabc140d`.
Frontend base: `5822541b7aa412678ffbef01eda55888f41283ab`.

| File | SHA-256 |
| ---- | ---- |
| `backend/backoffice/README.md` | `2c80be09df8591e2b233cd76678a93c8614cea34ba20c4a1ff9645e3e6e4c61a` |
| `backend/backoffice/__main__.py` | `050715a50ce40ef9b06156affdb815fc869904acf7e93861a0ae56ce919ce57a` |
| `backend/backoffice/app.py` | `1c9cad30eb4289f7633015c05785e46c616fc0cf46e5b96455f64ef1391b53d5` |
| `backend/backoffice/auth.py` | `ad30081ddf65d10465324e94893520cf3f9a3bdf2d0f86a06536506eb59330f1` |
| `backend/backoffice/commands.py` | `414a5ffeda85dbd0ef273ab2897161338ab67e84630992881b1db5618db1ddf4` |
| `backend/backoffice/credentials.py` | `dc4a7132a21ed559a07753a7e9bbbc346a99639c79cb4d8d691ebd35806ab109` |
| `backend/backoffice/github.py` | `87501d21387f297fd7f2eda936ff0dc129dbcdd48e756ddfafef4a091cd12ac5` |
| `backend/backoffice/requirements.txt` | `bda8073271b4fcbd7d4586b7bf8d0e25cf26b328f71fda273f3cf1a3bc6736c2` |
| `backend/backoffice/revocations.py` | `ef0a1660896d0e53d396901f9dbad1883cf7b3fd3b9f024cd47b4fc1b84e4608` |
| `backend/backoffice/settings.py` | `1267431783bfc1cddc0dc61e8c3f0e61860b26b139ccbb4922aedc14ec414198` |
| `backend/backoffice/tasks.py` | `a7557079e82f3fd41a712a24e8ddf62989afc568d7935168fdac52802fdc7331` |
| `backend/backoffice/tests/test_auth_lifecycle.py` | `9bdc9e61463f67f1cc6087f93d149b25dfee81e30d6ca3234ee17bd964093c2e` |
| `backend/backoffice/tests/test_portal_lifecycle.py` | `a1567cb9e9416f2c5d3db09c5d32075e6f6ab14bfe325a9cf166946ccfce5636` |
| `backend/backoffice/tests/test_sponsorship_api.py` | `d3af7a614f8f58ffd0da219d4d65b624580dad1b9daca537a2c014267eec06fa` |
| `backend/docs/proposals/per-user-credentials.md` | `ec8aea5c29dfa351213d734052b56a348d2208249102349970d84d8c9f473b62` |
| `backend/docs/proposals/per-user-credentials/phase-2.md` | `92010943abcb026a809ecef1d0073ac38ab3e00da8d9a4b1cd9e2d6825d4bd3b` |
| `backend/gateway/README.md` | `a2824175133050b8fc9ab4be4a17ddbf8eab664ed4c73a97a01c413f8f765e1b` |
| `backend/gateway/compose.private.yaml` | `1df9efd30b4283d25644b76f0a3ca3b30fae7b25e8f0ba5183778faff46d85c3` |
| `backend/gateway/tests/test_vault.py` | `4dec6f3323d640ffc9dc0b8cb592b47108b7ebf97ba2a97cf082a1769b6504c7` |
| `backend/gateway/tests/test_vault_lifecycle.py` | `6a873ad43eebfdbc8d236c63b5d86a615a5eaf76af28d8827ecb75548dcc02eb` |
| `backend/gateway/tests/test_vault_metadata.py` | `22a056bdfaba1d74f7bb69d1f3b281c59bec71fbb4afcad190ba58f091461ae9` |
| `backend/gateway/tests/test_vault_resolver.py` | `f429d3d54ab3ee1e510f405b91ef783c72f69c9a5f8debf11d630c0da9dd920c` |
| `backend/gateway/tests/vault_fixtures.py` | `bd40c0277e57b30055a9b6af2d17969a67b965398778a964e5ffae3cfc0c9a7f` |
| `backend/gateway/vault/README.md` | `8761d4dbb34d1634072883c080051e204ea85de05bd7629b0a201d6bae74156f` |
| `backend/gateway/vault/vault.py` | `1628783399dbe244117c5531c360295afece9b0c2fd6fadffd379cda031b1809` |
| `backend/service/compose.portal.yaml` | `5ebd026a79edc39357480c02c9a7b6fa9d69daee56b4fcc4b481200be0c77e05` |
| `frontend/README.md` | `2da480ef531325c71e0913033ee2091032c69f36ae8b9a2e1ce15ab5b940f34d` |
| `frontend/src/lib/api/live.ts` | `f0b5cab3182a2bc34c133d970eb8ceca2d54bab2d8780ff47b9a4d68b61c30bf` |
| `frontend/src/lib/api/mock.ts` | `6df4b89e9833158cae4ae5f58c58406f36ee0c9aa95906425011faf64685737c` |
| `frontend/src/lib/api/session.svelte.ts` | `56801c967ada2cc4c2c437e7f131ecdffd433cb7229c1e9063bab1bb781658b7` |
| `frontend/src/lib/components/IssueIntents.svelte` | `b8829bfbd9c6202b8d2db595d1f06c36305349f1b5000493b02446f0db7af773` |
| `frontend/src/lib/components/ProviderSponsorship.svelte` | `88b4f3b7a70e458e1765a769368faab60677530f6f7559c991f37083f4cf9ac6` |
| `frontend/src/lib/components/Topbar.svelte` | `a6eb3f951ea4072859dbabfc72b5202d1603359cb8d891b4b6d2be39d17daa8d` |
| `frontend/src/lib/components/UserMenu.svelte` | `9028da89945c35058676a3c9d4759a7f6ad04eeb029ea8f88fc0968db4b13855` |
| `frontend/src/lib/components/task/CommandBar.svelte` | `23e0f0a8e354a83840ea2673cb185747b8c2634d5b6125bb47904ad88ec6388d` |
| `frontend/src/lib/format.ts` | `34d0976b1504785b729e01965c2f55cef7cc431f2dc4b70a4f07d80d77ab7f48` |
| `frontend/src/lib/meta.ts` | `7c7a905ca47c6d1d4b60ccb9ed1397c853b23a12389ae8959580bce98ed72bff` |
| `frontend/src/lib/mock/account.ts` | `95f2c499c44b2e21c9f792dea9b607baf67e1fec0cdabfa0035dba5a0b10e936` |
| `frontend/src/lib/mock/api.ts` | `ba48bfeedfe7d3324f62abbd044afcd832e606caf8a66a02af2e2d6a5de49d08` |
| `frontend/src/lib/mock/reference.ts` | `0d00ed58456487b8f85baf13dff2f320a165ab50816259236a7f3db24edad8fe` |
| `frontend/src/lib/mock/tasks.ts` | `f30052c607c2b717402c4e1029f4789c738fc0e4f106ad16a9b31852e111a610` |
| `frontend/src/lib/needs-you.ts` | `fdaf07f8c45eafdedfd158835878a1f1e1708640a19c361988d2b7d056861f7d` |
| `frontend/src/lib/state/store.svelte.ts` | `847772ba0ae04f03a8c0584d64c1fa98321730869a96f1a630beb9194207aafc` |
| `frontend/src/lib/types.ts` | `e1b9bfa7aad1b4c6ce22412b36719ddccac9314e5388a706d600ae8cdb78d7f6` |
| `frontend/src/routes/(app)/+page.svelte` | `14eca620b4370878d6ab30e79ba8cf2e9401b16508db192e8b53d26f21b297fa` |
| `frontend/src/routes/(app)/issues/[owner]/[repo]/[issue]/+page.svelte` | `20142948962dc8b4b768a63a779ed1b584a0596cdbe632dad29c9784727881f3` |
| `frontend/src/routes/(app)/issues/[owner]/[repo]/[issue]/+page.ts` | `d104c52207e7f80f2309b967a84ee2dd8582c24303290ad83415a300086a2ccf` |
| `frontend/src/routes/(app)/me/credentials/+page.svelte` | `e87bc0ca6782b3fe931b10fcea265c4c8e6d78293bc2b4e8e36862defa33ffed` |
| `frontend/src/routes/(app)/me/sponsorships/+page.svelte` | `cba8c22f3c6d9dc2d65d4d3548714f939bf44b359400694cdfe3e45f47c3f501` |
| `frontend/src/routes/(app)/targets/[owner]/[repo]/+page.svelte` | `0d46c7cdce9dd8ca864193615f97d39eb1e1670628ca5bcdbd32158391db68d3` |
| `frontend/src/routes/(app)/tasks/[owner]/[repo]/[issue]/+page.svelte` | `b82c6a4852994e402a4545875c37371146af54fbed61024bf8b716c2c8a1d1e5` |
| `frontend/src/routes/signin/+page.svelte` | `fa826b7b8d14d373d6ab7f5c80d2f497b862cf1db7d69a348209f7afaf9033fa` |
