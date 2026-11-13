# Phase 5 implementation review

Reviewed 2026-11-13 against signed backend base
`74af3fa45af46215628c24883c913b6a0bd14df9` and companion portal base
`17f4ca2032dcb8fb363c2cb85cee23abaf676269`. Review ran on uncommitted Phase 5 work.
This provenance record was added **after** review; it is separate from the 30 reviewed files.

## Verdict and identity

Claude CLI used `--model claude-fable-5-2 --effort medium --tools '' --strict-mcp-config`
with immutable public source and synthetic evidence on stdin. Same session
`9670e085-18d0-4c23-8b11-b22675609435` reviewed three rounds. Final raw result has
`is_error: false`, actual `modelUsage` canonical model `claude-fable-5-2`,
`verdict: no_findings` and `findings: []`. The CLI's unrecognized-model warning did not
change the reported canonical model. The reviewer ran no tests/tools itself.

Final manifest SHA256: `c558f5aeb21144b68894c58e5ad454ce4b7c48ad8e6fa453be57a260145baaae`.

| Round | Input SHA256 | Raw result SHA256 | Outcome |
| ---- | ---- | ---- | ---- |
| 1 | `d500f1da3ef3560558ee1e4dc4eb8c054e659ffd74a6ad9feca0e8a511a8f26c` | `2758e67e0081d4686a669cdd7de604309ef7467753bad9bf9346df521d3eba0b` | F1–F4 |
| 2 | `710b8faef44cb45aba10e7e38d15c5e83ee35feb93edf9c14417b7914cb99f8b` | `ef6d975664953d04f61d3f07404d92bc8ece91f74a928ae846e899ac1894bb26` | F5–F6 |
| 3 | `d1bbd4e3e38cfaeac00dba555eaf214aa4ecfda1d78265cd1ed1951c9f2d225d` | `dba020fc0af5bcfdd433015177a64757bc0ab182d8e910314166b9d34bdccf24` | No findings |

Raw packets/results, invocations, dependency hashes and evidence manifests are under
`/private/tmp/concurro-phase5-review04/`. Exact final files/patches are under
`/private/tmp/concurro-phase5-round4/`; `round2-to-round4.patch` records the final bounded
repair delta. Earlier frozen inputs remain retained.

## Finding dispositions

| Finding | Resolution | Evidence |
| ---- | ---- | ---- |
| F1 / primary P1: vault release context | CI vault context now uses repository root, matching root-relative COPYs. | Actual vault/Exchange builds; offline UID 65532 imports and copied-source hashes. |
| F2: slow probes and uncertain saves | Shared per-model deadline interrupts connected headers/partial body reads; elapsed guards prevent late token send/commit. BFF timeout derives from the same constant; unknown saves refresh status. | Delayed-header/drip old-version/event preservation; effective HTTPX timeout; browser 503 with simulated remote commit/lost response. |
| F3: weak validation assertions | Claude beta/version/account-header absence and exact rejection/transient statuses are asserted. | Removing beta and converting 502 to 422 fail in-memory mutation controls. |
| F4: vacuous retained-memory proof | Marker-free retry/takeover prompts require archived content in upstream requests and returned answers for both CLIs. | Native lifecycle passes; omitting Codex archive fails with no rollout found. Claude has positive content assertions only. |
| F5: overstated connect bound | Chose the review's documentation option: DNS, multi-address TCP connect and TLS handshake may retain a worker beyond the interval/BFF timeout before socket capture. | Accurate source/doc wording; late-send/commit guards reviewed in source. Stalled connect/handshake is not empirically tested. |
| F6: deterministic mock refusal labelled unknown | Mock adapter normalizes local refusal to existing `ApiError(422)`; page has no mock special case. | Specific rejection toast/cleared inputs in both themes; direct missing-model/account contracts; live and mock builds. |

All six findings were supported; none was refuted. The real Store-to-native journey also
corrected a Phase 4 integration mismatch: versioned sealed account metadata is a map,
while the native hook expected a string. Exact-map extraction and malformed/missing-account
zero-upstream/no-fallback cases are covered. Schema 3's private owner/route mutation fence
is the small additional persistence change needed to prevent stale candidates reviving an
empty or revoked slot; it does not alter public retirement-event identities.

## Verification provenance

| Check | Result and source boundary |
| ---- | ---- |
| Initial full local CI-equivalent | Engine 1185 (one existing skip), service 503, gateway 156, BFF 45, deploy 13; Ruff/format/Pyright/compile/Shellcheck/bundle pass in `checks01`. |
| Repaired components | Gateway 158, BFF 46, engine admission/isolated packaging 4, compile/static and frontend checks/build pass in `checks02`; corrected standalone Controller journey and static checks pass in `final-gates03`. |
| Final discovery/frontend | Full service 503 passes with corrected import bootstrap; frontend check, live build and mock build pass in `checks03`. Only the later doc duplicate-sentence removal differs from its source manifest. |
| Final native lifecycle | `runtime09`: 3 cases pass, real BFF/public-private vault/Exchange plus pinned gateway and native CLIs, fake providers/two accounts each. Includes replacement/resume/transfer/revoke and malformed account isolation. Vault docstring later changed; executable logic is identical. |
| Existing inference regression | `inference-regression01`: all 15 cases pass after parser extraction, including org budgets/routing, spoofing, streaming/cancellation, native exact-turn denial, live read-only WAL and 61.013s TLS delay. Later catalog timeout constant does not change inference logic; this is not relabelled as a later-source run. |
| Images | Root-context local arm64 builds pass. `image-smoke02.json` binds Exchange/protocol/catalog; final `image-smoke03.json` binds vault's final docstring/source under UID 65532 with networking disabled. |
| Portal | `visual04` binds resolved light/dark forms and live 503/status refresh to source hashes; `visual07` binds specific mock refusal, cleared inputs and direct ApiError contracts. |
| Reuse | Initial 92 Python definitions, 5 TypeScript/Svelte groups, repair 23 definitions and final mock adapter were searched. Existing parser/catalog, storage/events, BFF auth, listener/admission/native fixtures were reused. |

Paths above abbreviate `/private/tmp/concurro-phase5-…`. Check/result manifests contain
commands, worktree identities and raw log hashes; runtime manifests bind image/config/source
and artifact hashes. These are local checks, not remote release CI or a deployment.
Ruff/format/Pyright and gateway runs precede the final vault **docstring-only** edit;
the final image smoke binds those final bytes. Engine/deploy full-suite results remain
historical; they were not repeated for the added unused timeout constant/workflow context.

Pinned gateway is v1.7.0 image SHA256
`60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b`, source
`26f5aebe99a36a037fe54f8080a608aca5547e15`. Native CLI pins remain Codex 0.163.0 and
Claude 2.1.301; `cli-help.json` records login/setup help and installed binary hashes.
Python image remains 3.13.16 digest
`73aa5faee130c60da030e105ec971ccf57473fbd22ce73bd6fc9ba99a40b838d`.

Useful negative evidence is retained: runtime01/02 fixture setup/admission failures,
runtime03 real account-map mismatch, runtime05 fixture method typo, runtime08 synthetic
repeated-digit bearer collision inside the pinned native executable, and the expected
missing-archive/mutation controls. Unique synthetic bearers removed that collision without
weakening the scanner. Scans cover owner/private/candidate tokens, gateway logs and vault
storage; Exchange's intentional virtual-key store, explicit fixture config and signing key
are excluded. Setup-script/import/screenshot-timing failures are retained separately in tmp.

## Limits and deferred work

Real subscription backend/probe acceptance, authenticated account-header semantics, token
expiry and real cross-account transcript portability remain pending by user scope. Fake
success establishes none of those and does not imply provider approval. Supplied account
identity stays unverified; expiry stays unknown. Unknown subscription 401 retains existing
session/error behavior; no category/stop is invented. Malformed legacy account refusal has
no immediate resolver observation and relies on native completion/unknown evidence.

Connection establishment may outlive the connected-read deadline as described for F5.
Direct candidate transport ignores proxy environment variables; egress-proxied operation is
unvalidated. UI conservatively labels even definite vault 502 as unknown and refreshes status;
the browser's indeterminate case exercises 503. Scheduler-level automatic wake, optional
refresh, real private-listener deployment, persistent volumes and production-sized migration
are outside this acceptance. Manual native turns prove transport/lifecycle; the separate real
Controller journey proves charge/pause/writer retry. No deployment or live provider call occurred.

## Reviewed source hashes

### Backend

| File | SHA256 |
| ---- | ---- |
| `.github/workflows/ci.yml` | `85d47f6693e01cc1083b13acc0a44b4c130142780d99d6c5a5dc34590e1b1641` |
| `backoffice/README.md` | `2a8c4c26a79c79bcf3c36d79cad2940d2184402e4ddbac652641c65bd09c3038` |
| `backoffice/app.py` | `6e9dac128bc73fbb5ff6a07a6e25723cba51d08869372d8ed2f9c62bad2d498b` |
| `backoffice/credentials.py` | `2fb16b8426ee288c275f6f57e017393f105dff8c259f7f08c4a22bf7f7fa3ba7` |
| `backoffice/tests/test_portal_lifecycle.py` | `3bed2e84a8e022fe32499ddbc6b14973197650fd975c3583f253f95ba0194ab8` |
| `docs/proposals/per-user-credentials.md` | `698b8014ad290ff4ebe2d803f68b0276f54e550c257785697b71e210261f0ad3` |
| `docs/proposals/per-user-credentials/implementation-plan.md` | `f3deb2539aa4a60adb8f3e04b3c23f2ccc0ad4c56c63362c11a71567d10623df` |
| `docs/proposals/per-user-credentials/phase-5.md` | `f9d7a53ef6f230f8069248dcca2df69317aa091758e8f5a9656810f1b57e7b4e` |
| `engine/credential_routes.py` | `8716daaf46da5d582b9caccc51befa3fd8b90ee59a7b63d1f307fa58ca55b17d` |
| `engine/tests/test_admissions.py` | `ef63ecf3396a91cb94593aea5a67d2c9c4cc8c1db324d3c28871b8539d8066b5` |
| `gateway/README.md` | `42ad1aa14acdf90bcb39cee27aa000ff280c55f8075b7b32baff317f7dd48b49` |
| `gateway/compose.yaml` | `cb5ce62fcf5bf84ff60c57d11ec8ad4b34ac025b479bd0da4268f5cc2a741682` |
| `gateway/exchange/Dockerfile` | `bb9a93fb34dc575409b9a8200c9d6c3545f2a548d2dca2699ae61e402fb06c31` |
| `gateway/exchange/inference.py` | `bc057128f810087e1c29c48e8f13ea63bbd3b49dd75855d4e01cf1b17fb56b62` |
| `gateway/provider_protocol.py` | `407581a03dda1b146d15ed0d8f45bbe4456c31d77fdf342ce745cbba56d0b023` |
| `gateway/tests/phase4_spike.py` | `86151f8bdf9df2e54fb6929db3dc01e96c05beeca731adef98ca8ce2f98309f2` |
| `gateway/tests/phase5_spike.py` | `97b7ae4b70e072465f26b1ec77402f75f34a86029c1698cba781fd236ace8546` |
| `gateway/tests/test_subscription_validation.py` | `cfffffdf4884c94294480d95b48b11625730f5fb68a1bcb388b5bf33ca4b5fdc` |
| `gateway/tests/test_vault_lifecycle.py` | `7dcca78008d911fe439a7865a6e14bb4e94727c87928801e67e1c31153f7540d` |
| `gateway/tests/test_vault_metadata.py` | `b83df2978cbe5ef2daa5a3f6f7347d023718737aae77b98f0961c8a484f20150` |
| `gateway/vault/Dockerfile` | `6ddd2929c2a191f82fc487d08132a357ec1334d025e1da92a0b14ee1d3ea6d07` |
| `gateway/vault/README.md` | `dadecb2f37bb0a95f83c52a9c3151d63e1ae29af7b045b32a660baed5127627a` |
| `gateway/vault/vault.py` | `427ee84f3513739b733a625d97e4b449c2803b7a32b4528b20f9d561d419f94b` |
| `service/tests/test_subscription_journey.py` | `f75c6c69eb537753648057d6b948f686250b73f08796bd455c11463558f6fc08` |

### Frontend

| File | SHA256 |
| ---- | ---- |
| `src/lib/api/live.ts` | `477b72b0747fe5ccbb2545c75990eb7dd712685aa701995a25fac200a1b56160` |
| `src/lib/api/mock.ts` | `49c19f872acc6d241123182fd1739de46267b06c037e211172b4818eb9dae621` |
| `src/lib/mock/account.ts` | `a57257f3b1fffcb45c2a402662fb40b1dbe7f6c81cccfae33357070e5474e7bb` |
| `src/lib/state/store.svelte.ts` | `047c6dca8bb5c18b6326cccbda80ea2f4ab90f4082f7b0aec4c2651e6f1eeea9` |
| `src/lib/types.ts` | `8be5f20c1599651a2d96637c16b49653e57ff989f86a2e8142e0d63a5a1ffbac` |
| `src/routes/(app)/me/credentials/+page.svelte` | `fdf7b501c3dd5b4da8e9fd21a11a4d3d557024c157eabb056e1f4bdf6b9e20f1` |
