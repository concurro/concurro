# Phase 3 review record

**2026-11-13 — Claude Fable 5.2 medium: findings: [].**

After this review, the user clarified that recovery is roll-forward only. Current planning
and operational guidance now reflects that policy; runtime code is unchanged. The hashes
below identify the reviewed snapshot before this documentation-only clarification.

The final review covers [frozen admissions and provider fences](phase-3.md) against signed
phase-2 backend `e255130078e1bb585623803f33faaa10af1136ce`. CLI 2.1.301 used exact
`--model claude-fable-5-2 --effort medium --tools '' --strict-mcp-config --output-format json`;
session `554c39ee-f30f-465c-9761-017f10d9fa0b` was resumed for repairs. All three actual
results report `is_error: false` and canonical model `claude-fable-5-2`. The known
`unrecognized_model` warning is retained alongside actual model-usage evidence.

Fable read immutable source/deltas and evidence without tools; it did not execute tests or
recompute hashes. The worker and primary verified the actual source/log identities. This
record was added after the verdict, outside the **39-file** reviewed implementation manifest.

## Dispositions

| Finding | Repair |
| ---- | ---- |
| Primary P1 | A transient or unavailable initializer read stays pending; only genuinely unknown historical evidence can finalize unknown initialization. Outage→recovery preserves the uncovered starter's source-era grant and charges nothing while incomplete. |
| Primary P2 | Preserve existing task transcript scope across sponsor/epoch/token replacement. Staffing and admission identity remain exact; no unmeasured account-transfer fallback was added. |
| Fable F1 | A structured refusal identifies the exact unapplied stale-generation transition. Re-sign only that request under current ownership, checkpoint before retry and preserve epoch CAS plus committed peer receipt IDs. Real HTTP and grouped permission-loss regressions pass. |
| Fable F2 | Freeze fallible generation/admission inputs before journal mutation; prefreeze every batch member before starting any. Serial identity failure and second-member freeze failure leave attempts/uncertainty/debits unchanged. |
| Fable F3 | Reuse the existing transient predicate for uncharged preflight I/O. Status0/429/5xx waits with no suspension or charge; uncoded409 stays blocked. |

All supported findings were repaired; none were refuted or deferred. Requested dependency
sources closed the review's implementation gaps. The events token is explicitly the existing
private gateway principal, which also authorizes resolver access; metadata uses its separate
nonsecret principal. No new principal or user consent gate was introduced.

## Verification and limits

| Check | Result |
| ---- | ---- |
| Final full service suite | 494 tests passed on `round4`; focused admission suite has 18 scenarios. |
| Engine / gateway / backoffice / deploy | 1,179 / 126 / 44 / 13 tests passed in full `round3`; one existing Linux-only engine test skipped on macOS. These inputs were unchanged by F3. |
| Ruff / format / CI Pyright / compileall / whitespace | Passed on final `round4` source. Pyright retains the CI-configured extracted-module scope. |
| ShellCheck / bundle packaging | Passed in full `round3`, unchanged inputs. Isolated Python image-entrypoint/catalog packaging smoke passed. |
| Markdown | 41 local links and language-tagged fences passed; review-record links/whitespace checked separately after the verdict. |

These are **local CI-equivalent checks**, not remote release CI or a Docker release-image
build. No deployment, real provider call, subscription-backend/account validation or frontend
change occurred. Production Gateway remains org-only; injection/awaiting outcomes are phase4,
subscription acceptance phase5. Retirement-only event consumption and observed source-policy
receipts were brought forward as admission dependencies. Historical starts outside observed
policy intervals remain unknown; transcript account portability still awaits the user-deferred
live evidence.

Raw snapshots, negative attempts, repair deltas, CLI results/model usage and final verdict
remain under `/private/tmp/concurro-phase3-review`. A temporary overwrite of the first external
snapshot was recovered byte-for-byte against its original patch and manifest hashes; the
implementation/index were unaffected (`snapshot-recovery.json`). The final implementation
stayed frozen during its final review.

## Reviewed identities

| Artifact | SHA256 |
| ---- | ---- |
| Final39-file manifest | `d47236f260d2ff8ba8479f301eb78675930dd94941e67dd4de407667afe34072` |
| Implementation patch | `45e2c3fc90662bd578dffa085da54c2ff301987b038dae0efd558323f97b01c9` |
| Final review input | `311af531a994e3b4d25b68fa88e9b6bb31f1c09756691b0b767a48875538dcca` |
| Final raw Fable result | `c34b562b2adb961c80cc9a759220f8dff538d8bac7335a50459f975a5343754d` |
| Final current/reused check manifest | `726bd17e3b216ff0220128061cf812493029bba3bb4d2a2473a0a68687e9cc8f` |

| Reviewed file | SHA256 |
| ---- | ---- |
| `.github/workflows/ci.yml` | `791883d2352498ad5618aab520e982b1e5e177e90ceede1a322bbcbcd2d0c6f3` |
| `docs/proposals/per-user-credentials.md` | `8425740801174ffc0f9e5fec48a68fb22bb9905599e592726d0766e2196ee4e2` |
| `docs/proposals/per-user-credentials/implementation-plan.md` | `d8af1f39b78e3effa8cdcf6c189750af9b55dafde97562e2eafaa79bf230de93` |
| `docs/proposals/per-user-credentials/phase-1.md` | `588f9f287e700098bd729a904993395eb4fb1c490d51deab6b9cd8a780bf1ea1` |
| `docs/proposals/per-user-credentials/phase-3.md` | `c764ff7a69704f8dde47b7b7d42cced509fdfe547193026619500516f8cc0ca2` |
| `engine/admissions.py` | `bcf00b5e02d1a4b5d35439759b55a4d8ec93fd03c2bc7773449f25cca9c1c92d` |
| `engine/agents.py` | `403c0fa559a6d99711a207d392669c468236e713e8b0df19d47f59c2ec13116c` |
| `engine/attempt_result.py` | `914d8563357e72b9b77be946a7f8e826dae80cb945adcdaece42499d4ffedf9d` |
| `engine/checkpoints.py` | `2e4c73f95c3ff6e3252c60ddf3688b49775f320e14974322f9ae426852e88f1c` |
| `engine/controller.py` | `1be1a007935bd9c1229625d4bcf48b3e3dfc56c780837f4d15a5a5783f6d79c5` |
| `engine/credential_routes.py` | `dfd31bf39029de0ad20d1ebdceec4b46712d2677356c6f8d16f9575411aa3161` |
| `engine/credentials.py` | `94baff3a4d68e7651dc40fb1ee1ef6331f312016377d5ab544b8a91fb57ea4c2` |
| `engine/design_batch.py` | `a0ebafd602574d35f71b5d7e5fe77bcb88385f7b4689680b5017f7f775121958` |
| `engine/records.py` | `9410dd4d63f742a1bd1430fbb57d59e367f6f157755560b276d7976012fa4385` |
| `engine/relay.py` | `4a06ed5fac6ee60b05fd1edad26c6e316c00475ca13c315a004617d736ec50f4` |
| `engine/report_store.py` | `98b18ee4130ed30414ba83e1e889db3874277cf862bbd03b73d72780ae487f1b` |
| `engine/sponsorship.py` | `4d2215720d702615d42d862ad83e3693f5aeb09a9a331408d160c7c256aee354` |
| `engine/tests/fakes.py` | `b7deade0499ac25aaa3fde6ae4deea537e212de17385dc309d8f1cf2eb608bf7` |
| `engine/tests/test_admissions.py` | `d82269c5ba565d2d9382069a2656cd6325817397e294245849be56add541df03` |
| `engine/tests/test_large_changes.py` | `d8746fe83d2dae428d714eeb2df5e3f400fd73b7d6284671aca9b345fafc2cec` |
| `engine/tests/test_limits.py` | `854c1373b93a4e3ffba68d91638f9808a8f39eccc962d15c1f975837dbe02ac1` |
| `engine/tests/test_parallel_reviews.py` | `10b78b4a094c21f806c5f1a622acff67913e7913cde41a60910cd0a5b8daae84` |
| `engine/tests/test_resume.py` | `a44f693f4aea9649e6157d1d6e5f1f12447e6a39f394d37ffd931caea99c3ec2` |
| `engine/tests/test_storage_lifecycle.py` | `1a82e738f6abfd0f91e8df4324e15517efefb6f4f32cd6132b7600818e4a6c6e` |
| `gateway/README.md` | `ac1342fa29ae2c1f53a17faff9195bb1cf3aeb2190fd96fedddabc74e811ba04` |
| `gateway/compose.yaml` | `8d11175863d050f136700e337d65a972431b1dca835f281603885b109cb23975` |
| `gateway/exchange/Dockerfile` | `375b42d75a1f12a030e56daa83fed0ee19fd8fc2f9b2cc99ec7c8bb72dd190ad` |
| `gateway/exchange/exchange.py` | `6056988f81ea2b623bba2de664e98304c9d0cbccf56cd392dd1d0222806e3efb` |
| `gateway/tests/test_exchange_funding.py` | `5fa9b5ddf3d44455bde7b937e987be7501b620c495e32151ec2ed2fe38954bc1` |
| `service/__main__.py` | `2ea81665fbe5daf64b08443c1592cd39918d28f88aa518a749ce6624cb4b2a29` |
| `service/reconciler.py` | `d489bd94d74bee9e55610a627a952e300d29a7486cf8d1565052a365ab758d3a` |
| `service/registry.py` | `cd20991f8f7b3fbf26acf9558eb958470af8809ecb49cd6df554217d2f637fa6` |
| `service/shadow.py` | `06851205f407f89fcbf9e49227e02c58da555fd170ced5873e44ba982273e1b9` |
| `service/tests/goldens/registry_setup.json` | `20b6a5bebc623a6b100bff3b8bbe18d414b7fc752151ce101a8ed4bd227b8931` |
| `service/tests/test_pending_review_corrections.py` | `73f3f17e869e06d4c5b859bc06b02ec93664c9f9e831e0e8f9ffe86875afb978` |
| `service/tests/test_registry_setup.py` | `861c050946238d6ef8e0ea5f098adfc3f133c9fe1631861e0794ad081d105914` |
| `service/tests/test_shadow.py` | `60c7f078c2df6a4a72989efa073928d3583d9915fde87460f20768d7d1dec4a4` |
| `service/tests/test_sponsored_admissions.py` | `1f262ff824bacba457cbe8a7eea2194c3540b88bc3d97d63c64cac1fd3d47a14` |
| `service/worker.py` | `25b937aaf90371a44ccf5baf9d225bc595700ae6c755665a67c16e292d746c5d` |

| Gate log | SHA256 |
| ---- | ---- |
| ruff (round4) | `82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18` |
| format (round4) | `04989a5b23ca766c18f9efa8258977a1672c326efdc53b3c1f544e3fad4650b2` |
| pyright (round4) | `3ad2d843353727d542e7367b203d155ef69e68c7e533e1a57e7c5f8a2337fa84` |
| service (round4) | `2d6213ca072a235a9fbe4d7f299b8b8be9e53615dff6340ffc2d34b9722bb4a9` |
| compile (round4) | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| whitespace (round4) | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| engine (round3 reused) | `0511317ce67a21d0e7a91fa83e9404f97f931519b94123ea690bf93fbb6e5429` |
| gateway (round3 reused) | `3842d77544bbfee706ec88a56c9042a34a06da100a0a0e8570c327616ace96f2` |
| backoffice (round3 reused) | `cf1e5de81ef3eb9ab462cf205dc170946bf8ce07b23f9e8908a92925750ac56a` |
| deploy (round3 reused) | `d172ea210e5c2c8997f42b52ac96861f798192da2bffe8746df301019952ff40` |
| shellcheck (round3 reused) | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| bundle (round3 reused) | `87697cfdd7f3f08e0428cc892251892fa3b53065fe74fd0c264a13a71172fafa` |
