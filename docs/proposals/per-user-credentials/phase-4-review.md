# Phase 4 review record

**2026-11-13 — Claude Fable 5.2 medium: findings: [].**

The review covers [contributed inference and credential pauses](phase-4.md) against signed
backend phase3 `01cc4957d145f584951a240a0d7818b9f69866a8` and portal phase2
`3906e2bac517d1fecc0f5768c05a2e02eb890b09`. CLI 2.1.301 used exact
`--model claude-fable-5-2 --effort medium --tools '' --strict-mcp-config --output-format json`;
session `04685ecd-e9a6-4a1a-9243-5bb2402315bb` was resumed for repairs. Actual results report
`is_error: false` and canonical model `claude-fable-5-2`; the unrecognized-model warning is
retained with modelUsage. Fable read immutable source/deltas and synthetic evidence without
tools; it did not execute tests or independently recompute hashes. This record was added
after the verdict, outside the 39-file reviewed implementation manifest.

## Dispositions

| Finding | Final behavior |
| ---- | ---- |
| F1 / primary P1 | Exact ended admission becomes a typed provider failure. The failed handle stops; independently admitted peers drain under their own authority, including different owners/epochs or budgets on the same provider. No new admissions; ownership loss/stop/terminal verdict cancels all. |
| F2 / primary P2 | Missing coverage identifies provider, route, role, profile and model before charge. |
| F3 / primary P3 | Hop forces identity encoding and rejects unsupported encodings. |
| F4 / primary P4 | Separate source-labeled rows preserve provenance: native completion is neutral request observed, provider completion requires positive evidence. No usage aggregation/double counting. |
| F5 / primary P5 | Nonnull structural errors and SSE error events are sanitized; error:null success is retained. |
| F6 | API fixed upstreams include /v1; actual count_tokens and all four route paths checked. |
| F7 | Healthy in-flight requests do not block peers; terminal unresolved native evidence does. |
| F8 | Previously accepted successful results survive late failure; failure blocks later admission. Cancelled/revoked work is never revived. |
| F9 | Durable pause latch, highest cursor and acknowledged turns prevent replay/late-event re-pause after explicit retry. |
| F10 | Checkpoints retain cursor/latest failure, not the full durable outbox. |
| F11 | Org upstreams strip private headers and preserve native routing/budget. Trusted org selector avoids native model-specificity fallback. |
| F12 | Append/file-identity cursor, one lifecycle transaction per import and indexed task/turn reads replace repeated full scans. |
| F13 | Nonce consumed before vault I/O; resolve/claim/socket attach recheck exact authority. |
| F14 | Admitted deadline and existing session retirement govern upstream sockets; no arbitrary 60-second inference read limit. |
| F15 | Portal explicitly exposes unavailable evidence and unknown usage/completion. |
| F16 | Successful writer start/invalidate/revision grant/retry/resume acknowledge the provider pause through one helper, retaining exact-session cleanup intent. |
| F17 | Every drained failure contributes its turn/session/sequence; one writer retry acknowledges all. An older unseen row cannot be skipped by the cursor. |
| Reserve-time race | A trusted refusal while reserving a correction enters the same drain path without cancelling admitted peers or charging a replacement. |
| F18 | Turn cancellation and dead-session reconciliation interrupt only matching observer sockets, including live TLS reads. |
| F19 | Request-start reads the small admitted model column; task pending checks use indexed repository/issue fields. |
| F20 | Shared GitHub funding notices match configured organization or contributed funding. |
| Long-request repair | Private JSON hook responses explicitly close HTTP connections, avoiding measured stale pooled reuse after a 61-second request. This adds one local connection per hook; provider streaming lifetime is preserved. |
| Primary P6 | Exact failed-session cleanup intent is checkpointed before retirement; fail-before-send/lost-reply survives reload and writer retry. No new charge until confirmed end, then a distinct session/key is staffed. |

All supported findings were repaired; none were refuted or deferred. Dependency and runtime
evidence gaps were supplied in the follow-up. The UI deliberately retains distinct native and
provider rows instead of merging them. Existing lifecycle locks, checkpoint persistence,
Exchange retirement and the shared review coordinator remain authoritative.

The final reviewer explicitly retained evidence limits: the reserve-time race uses the real
Design adapter with fixture agents, and heartbeat socket retirement uses mocks. Actual
Exchange-to-executor draining and TLS turn cancellation are separately exercised. The older-row
service assertion is covered by the focused final log; its file identity is hash-bound rather
than guarded during that focused command. The earlier full service runtime inputs are unchanged.
Non-provider transient failures retain the existing whole-batch cleanup behavior.

## Verification and limits

| Check | Result |
| ---- | ---- |
| Engine / service / gateway / backoffice / deploy | 1,185 / 502 / 146 / 44 / 13 tests passed; one existing Linux-only engine test skipped on macOS. |
| Ruff / format / CI Pyright / compile / ShellCheck / bundle | Passed. Full 09 checks followed by affected gateway/static 12 on final repair; unchanged component results retained with source bindings. |
| Actual pinned gateway + fake upstream | 15 cases passed: all four routes/two owners, exact resolution/revoke, private header spoofing/redaction, native budget join/nonturn probes, stream/backpressure/cancellation, verified 61-second TLS response and subsequent request, pinned native CLIs and fake-account archive resume. |
| Shared native WAL | Actual read-only mount under Linux UID 65532 and production importer passed using an isolated tmpfs-backed named volume. |
| Portal | Check/build and six resolved light/dark screenshots passed, including unavailable evidence and native unknown/provider completion. |
| Reuse / links / whitespace | 177 Python definitions covered by original 150 plus the retirement and final repair supplements; five frontend definitions checked. Local links and both repository whitespace checks passed. |

These are local CI-equivalent checks and an isolated pinned-image experiment, not remote
release CI, a release-image build, deployment or real subscription validation. Live account,
ChatGPT account-header/backend and transcript-transfer checks remain user-deferred. Both
subscription response taxonomies stay unknown; subscription registration remains 404 until
phase 5 validators. No automatic credential refresh/recovery is enabled.

Native logging is asynchronous: terminal unresolved evidence waits through existing transient
recovery without a replacement charge; lost historical logs require operator repair. Private hooks use a fresh local HTTP connection per reply to avoid idle reuse; org
inference now shares the local authenticated start-hook availability dependency and fails
closed. Normal Docker-volume startup reported disk full; those failures are retained. The
tmpfs WAL proof establishes UID/read-only/live-WAL behavior, not persistent-volume capacity
or crash durability. Exchange reopen tests separately cover durable identity/cursor logic.
Production persistent configuration is unchanged. Recovery is roll-forward only.

Raw review results, failed attempts and repair packets remain under
`/private/tmp/concurro-phase4-review`; source freezes are `concurro-phase4-round1` through
`round7`, full/affected check logs `concurro-phase4-checks09`/`12`, and actual image evidence
`concurro-phase4-runtime20` (all under `/private/tmp`). Earlier sandbox-only local-socket failures, TLS fixture errors, the measured idle-reuse failure and an existing overloaded-server TCP reset remain separate from passing evidence. The final unchanged gateway rerun passed; unmeasured skip-delay diagnostics are not acceptance evidence. A mutable reuse inventory
was reconstructed from immutable round1 source and verified against original search batches;
no source snapshot was overwritten.

## Reviewed identities

| Artifact | SHA256 |
| ---- | ---- |
| Final 39-file manifest | `8f1f0f761a72198d2ea08449f330af7be37bae0de8f30c2890a1d2074f0e7f38` |
| Backend patch | `3aed0a335602964eabdab51304fd72e44afde8862bf66c9a051e0bb072703fe3` |
| Frontend patch | `eb905ca4bcea2cf0309b6c5a9838636be23a53db07bc86e7c37bfdc59cb08060` |
| Final review input | `b140ed6bb7d9af6a11637ba7ccb1b378f46c6e96904e2b8a0f904bde294ee89e` |
| Final raw Fable result | `0c38e20a8d568b25ffafafbb9ded6ecf385e56b5e59ec22febe38a4b17de5a9a` |
| Final current/reused gates | `619f9682807f4eda9790713340e4731241efbc0a6bc3ba2cb403b9fa135e271f` |
| Final evidence bindings | `2c2f32c6b390c8f6f15f570ce25e7a6468dda81197d2638c87ef59466e6829bd` |
| Actual image raw manifest | `e8e14209bd798c94bf237a56443172ee2e11f11ebae45dd3458aa652861b355a` |

| Reviewed file | SHA256 |
| ---- | ---- |
| `backend/backoffice/tasks.py` | `842da58c97824101b833a61143d1cbd237402141caa8dfd7f131f2b632d44096` |
| `backend/docs/proposals/per-user-credentials.md` | `1b89576e274d6e2f6a49af50e4dd10fab7c517b586c8d84a9d12b0ae4970dd90` |
| `backend/docs/proposals/per-user-credentials/implementation-plan.md` | `08da784757a880791dddd482f24311dc11c5a95d8092a60edddb5b240d4929c6` |
| `backend/docs/proposals/per-user-credentials/phase-4.md` | `effc7ef23170515356da7047ab52ed7e4d042fd51acf51fff58d2bf77390938c` |
| `backend/engine/admissions.py` | `1fc137a9ce235347c35d01b024f98937e5544238c9ca640fd56c2c35a7202d6c` |
| `backend/engine/agents.py` | `341cbf75a9b6a940bfeab5c38122c4edfbba128239b5796e71423b0f68fa797b` |
| `backend/engine/commands.py` | `3aee6286038b7c259ae35fe780395ba7dedc37744bb5db2e33b94d38787ebbc5` |
| `backend/engine/controller.py` | `4cbca207a26b92e93c001739d882601bfc3d08903369d1c3e9bc32dc47b433cb` |
| `backend/engine/dashboard.py` | `5d1ef82e7b66b26d64159d8a5963bd0c6f8cc9d366a6a5c6d78d1102daa48c32` |
| `backend/engine/policy.py` | `01000068870998ca5ebc0d464b521a7881485afb838a0280d20c2befaf6763e4` |
| `backend/engine/relay.py` | `9dc4a0f70fff2b29fccc896fb46ea2ca5c3bab893b3bc6b0e87f9df6fcb2a343` |
| `backend/engine/review_batch.py` | `22a9df4d07e4e796a914c346e54bbc32ee6960ca1b8aa863b8bbdbe2c3f4187a` |
| `backend/engine/tests/test_admissions.py` | `c65d68bd084caad678c963a406ec91f7e126aa2bf9baadbc837bba1ae2af3137` |
| `backend/engine/tests/test_provider_pauses.py` | `11b51e08d990c345d0d170f3ad6c7dd390ec1df2b134b385fe9b0f9c6770e993` |
| `backend/engine/tests/test_vocab.py` | `23f1cfd8e799c14019fd04ec1870661d350ad3bd8584002fe1db0dbcb4667fa8` |
| `backend/engine/vocab.py` | `f415d3b9b45ff518e4582e36d95f376275694544f5cec9913e62666de92b1958` |
| `backend/gateway/README.md` | `635af24a510468f16975f22a51d76da48429e4bfb17c42fed0bf137aa830ce9d` |
| `backend/gateway/agentgateway.yaml` | `9055723cecd62fbeec80556086da9e8a071456150afadf8c31688a2655b8f675` |
| `backend/gateway/compose.yaml` | `230b9128da315ce849c6bd35e2e1345f446b4bc58e47b497310f593b53217ea9` |
| `backend/gateway/exchange/Dockerfile` | `cb4628fb7dcfba59e20a39df1609e80b818f7f4a19dc0369b07b330e82f61e1e` |
| `backend/gateway/exchange/exchange.py` | `e4af812043fa204eda2c815450e0130acd4d0c3e1bcdcbbf72f1448445436b54` |
| `backend/gateway/exchange/inference.py` | `c4caebddff21eeda689616ac5e547aeebfc283063ef0ca5ad957bc41c06b8dc4` |
| `backend/gateway/tests/phase4_spike.py` | `a2d2a6202dd19324129f1891c89c4b4dc1fb3f9abd1bdbae613395c66242680d` |
| `backend/gateway/tests/test_exchange_funding.py` | `33af3083f972670155007ad51ee34fc9f4c3398cfec42185bc464fb0fd09893b` |
| `backend/gateway/tests/test_inference.py` | `84251a2b742400480de0944778c161ab6273f573a1a1b8292d653b031a7ea1e2` |
| `backend/service/registry.py` | `d50b6e638612190a8d8045a950f64edc13886a13c6a170a72aac5ddefd7a87f5` |
| `backend/service/shadow.py` | `20a5d18dcb042b062859e1560c24073f4c0090cf1cbaa4392dde781d71b935ca` |
| `backend/service/tests/test_sponsored_admissions.py` | `cfad0e9cb26465b6bf6b4430dc16776d42b56b1bff0027d9658ce25bf2f16cc0` |
| `backend/service/worker.py` | `aea8e93e585e2b5cb989a02daa48fb372bedb0f0c15066c0a7c34ecd8a1372f2` |
| `frontend/src/lib/components/IssueIntents.svelte` | `ffb139463f66f8a2ad89a76c832f062e571547d9aa5712a0e09a16ae135b5c2b` |
| `frontend/src/lib/components/ProviderSponsorship.svelte` | `6ca2bc205a41e98188724e4c20d98f66fd2a5f84f58e808988de3c50071cf9d9` |
| `frontend/src/lib/components/task/CommandBar.svelte` | `d72f2d4bda15ea3f7eb7c57e5ff0eee84b513bff872e009047d36b30eea80d45` |
| `frontend/src/lib/meta.ts` | `039c796e029a21bd02fe743c44979064769f5f47a9d7bb48c00e98b7d4975c38` |
| `frontend/src/lib/mock/account.ts` | `ce8a559062bc7f546a099d72756f9f5d5e9679e7987e759e042d06d8a66cb174` |
| `frontend/src/lib/state/store.svelte.ts` | `fdb005929c439f1fe1ab670c60de13b6456f77681fb3fe8fb0894ba5dce82db1` |
| `frontend/src/lib/types.ts` | `a3013f8c9e4e713cf0130e80273131720d2df571ce4756f1b8d63f193417b898` |
| `frontend/src/routes/(app)/me/credentials/+page.svelte` | `9bbf4093a16738ef3801d0f6ffd0f3a5efe66e404b1d9d6fde79c4e5fdaf5548` |
| `frontend/src/routes/(app)/me/sponsorships/+page.svelte` | `01537067f44a7fa1d65875f948ca51adce2df061800a6dfe15d12aa80737388d` |
| `frontend/src/routes/(app)/tasks/[owner]/[repo]/[issue]/+page.svelte` | `b26fe5bf005bd15f7892cf5ae6f793bad7c657aed58ff0702eb5c3b456db21ec` |
