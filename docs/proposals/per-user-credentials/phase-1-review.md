# Phase 1 review record

**2026-11-12 — Claude Fable 5.2 medium: `no_findings`, `findings: []`.**

The static review covered [org-funded shadow sponsorship](phase-1.md) and its private
metadata prerequisite. Claude CLI 2.1.301 used
`--model claude-fable-5-2 --effort medium --tools '' --strict-mcp-config`.
The final wrapper reports `is_error: false` and canonical model `claude-fable-5-2`.
Session: `39380182-bf7b-4ddd-adf4-05108f950e16`.

Nine initial findings and two follow-up residuals were repaired; the third verdict has
no findings. The reviewer read immutable source/deltas/evidence without tools; it did not
run tests or recompute hashes. The worker and primary verified the actual source and gate
hashes separately. This provenance record was added after the verdict and is **outside**
the 42-file reviewed manifest below.

## Dispositions

| Finding | Final repair |
| ---- | ---- |
| F1 | Permanent pending conditions stop self-retrying; transient shadow I/O uses shared backoff and the existing queue. Terminal consent processing remains enabled. |
| F2 | Verified edits from non-command text can complete source history. |
| F3 | Removing an old comment cannot withdraw a newer grant from the same actor. |
| F4 | Permission loss uses its observation boundary, excluding deletion sentinels; delayed pre-loss claims cannot revive consent. |
| F5 | Empty discovery creates no intent row; confirmed empty hints leave discovery; terminal history remains directly readable. |
| F6 | Successful reconciliation restores confirmed status without requiring an epoch change. |
| F7 | Bare usage replies use the existing writer authority. |
| F8 | Tests assert reloaded charged evidence and check the BFF after command POSTs. |
| F9 | Rollback instructions disclose that the daemon moves every ticked task to checkpoint format 8. |
| R2-1 | Scheduling acknowledges the exact observed projection/status separately from adopted consent and pending transitions. |
| R2-2 | Only facts/tasks join new recurring discovery; provisional hints remain API-visible until confirmation. Successful pure discovery does not create a recurring task; ordinary-command and legacy watches remain. |

Additional acceptance repairs cover unknown legacy model evidence, a source hint arriving
during metadata I/O, caller/editor identity for human sponsor and START edits, and the real
signed-ingress → observation → Worker/Scheduler terminal-withdrawal path. Counterexamples
and exact pending receipts are tested; no finding was left unresolved.

## Verification

| Check | Result and source evidence |
| ---- | ---- |
| Engine | 1,175 tests; one existing Linux-orphan-adoption test skipped on macOS. Full `checks06`. |
| Service | 476 tests passed on final source. `checks10`. |
| Gateway / backoffice / deploy | 103 / 25 / 13 passed. Full `checks06`; those component sources are unchanged. |
| Ruff / format / Pyright / compileall / shellcheck / bundle | Passed on final source in `checks10`. |
| Pinned gateway/native CLI compatibility | 15 isolated cases passed in `concurro-phase1-rebase-spike01`; gateway/fixture dependencies are unchanged. |
| Local Markdown links / fence tags / whitespace | Passed. |

Raw inputs, exact deltas, source/gate manifests, CLI JSON/stderr and
`final-verdict.json` are retained in `/private/tmp/concurro-phase1-review`.
Full checks are in `/private/tmp/concurro-phase1-checks06`; final affected checks are in
`/private/tmp/concurro-phase1-checks10`. Failed counterexamples and intermediate runs remain
separate. The final six changes from full checks06 are service source/tests and this phase's
runtime note; the affected service/static checks cover them.

| Artifact | SHA-256 |
| ---- | ---- |
| Final 42-file manifest | `37eab92576f671b821fa17b8b54622ed8302be4036bb51afb1635f89f6ef276e` |
| Final review input | `934d2c90ff6c727f779b13fa7ce376cdd8b265b77c2aa6100da2f5ce133f0041` |
| Final raw CLI result | `552292373b163df794c478cc49bba620a92a6042c25397aeca85468d01031cd1` |

Actual payment remains org-funded. Rendered frontend controls, contributed enforcement,
subscription token acceptance/lifecycle and real-account checks remain later work.
Historical unknown model evidence can leave initialization pending indefinitely; later
explicit claims may remain only in the issue projection without adopted epochs. Future
cutover must reconcile explicit grants without inventing past availability or rerunning
initializer fill.

Successful label-only/stateless discovery watches leave background polling once unlabeled;
later commands arrive through webhooks or targeted observation. Bound tasks and ordinary
command watches retain their existing recovery. A withdrawal-only intent remains a durable
fact/watch by policy. Marker-shaped comments may retain an ordinary watch but cannot grant
sponsorship without the existing identity/signature checks.

No phase-1 deployment or commit was performed.

## Reviewed files

Base: signed phase-0 commit `b6526d69cf97a78455d78d3b6ba04ee0722e2d10`, rebased onto
main `a4263cb9d10923f5c091b39e021be1ed7f7b3ed0`. Earlier phase-0 evidence remains historical.

| File | SHA-256 |
| ---- | ---- |
| `backoffice/README.md` | `69892cda316c24863477b41bb78bcb9156685deda0e2cdecf28dad4ea07b8b56` |
| `backoffice/app.py` | `770b6bd603cddc63492f87e01bbbe4f81f242a18490898bd6ce4cf85abe43fda` |
| `backoffice/commands.py` | `78a1df8699d0c322761a8b8b547946572e5e6e9614838bebd828b17ef2146623` |
| `backoffice/tasks.py` | `20ea3bc0aa83cf85c852b28744efc105f6c84da77e635c5d2a93c08d11d42e7e` |
| `backoffice/tests/test_sponsorship_api.py` | `0aa237cc0460146f17eb5f8a6edea6e6c28c613cba13bd1773522fcc2b678c96` |
| `docs/design/service.md` | `4dc6d8c16e770b16af2c3661735369f1aba10bb20ccb179a9a2e445624c81076` |
| `docs/proposals/per-user-credentials.md` | `45704d9c26c928890ee8a9c43bef6949c264015bbb1502d04c15f58658bbeb78` |
| `docs/proposals/per-user-credentials/implementation-plan.md` | `d8987c007665aed2747ed4f0b86ad83a1fc65093e1626ddfc3e2e9c3a09923a3` |
| `docs/proposals/per-user-credentials/phase-1.md` | `be026eb8b397e186b8330d919831ad7d41dae477ee467e04e42f13f51b2ec7c8` |
| `engine/checkpoints.py` | `10deb3a14f4a211f72d89cf43e2ac871442d20719df667c9807f517f711c8e25` |
| `engine/commands.py` | `5ce29b954dd93495f23a74610b6471003d136d34553975971d8c9cb0f90d6e3a` |
| `engine/controller.py` | `84cd207fc5c732ef6858dee3d5f1da4cb771ca5b8449274c024541f831875a1c` |
| `engine/delegation.py` | `9c0bbdf277ae6556c2f7382f8c2bf2af2135fe7daa921fa37d00fc87b1cc8935` |
| `engine/records.py` | `c822fa6a0db4d7261ed3d53c28201f9041df2a9decab759787c6047cc1907f18` |
| `engine/relay.py` | `99ced8669b2c1a48ee7f2eda45dc666ddb991a3cfc2a6c2a218bef0b805047fa` |
| `engine/report_store.py` | `2fe103a903940d5fc917eb0ab1480e900426fa613ffd435c8894f8c8783ba484` |
| `engine/sponsorship.py` | `120945c747da49d1aaa5e302a868d5dda85cc9a15db95d8b04fcf0ed1f8293f2` |
| `engine/tests/test_delegation.py` | `6af677e63e6e127b601827c9c3c8b6e6aee1b1bb08aaa0cc571dc08e92f493ff` |
| `engine/tests/test_large_changes.py` | `1237ee5a91c49c7a36b91835967c80ea7a1a25ef2937bf9d17e7af0834eebe14` |
| `engine/tests/test_parallel_reviews.py` | `0fe44915d9d55f2062e2594478384733e6e8d184253817927f929d9a1b3a5e13` |
| `engine/tests/test_storage_lifecycle.py` | `de595170b4d08d93cd8df6c5b2e5c7411a5fb8a138f13020d78df4b2aa964f1b` |
| `gateway/README.md` | `027d6314dafd378a9b6ca484d441dfa0ac3524799afe2f55216fe7f39ef56775` |
| `gateway/compose.yaml` | `4fe9ec7b7110db2c53428b3e82ddaee422d5cf94971407215f4f6e0c6dcfcb8c` |
| `gateway/exchange/exchange.py` | `81baec24fa8f9300db6a9ad71a0df6b412a91f68c72e785c714beb3a4c75cf0d` |
| `gateway/tests/sponsorship_spike.py` | `53b8bf508d7002b1316e7f725a08569a56de9d3567e4453acde0a3bd799e8bfe` |
| `gateway/tests/test_exchange_sponsorship.py` | `1a85678da8e038e8cd13ff1a116e8673be269e814261118c677933280038c458` |
| `gateway/tests/test_vault_metadata.py` | `12e256594baa73d3738d0f92359b6cc92992e03b0a2c9c185e464e567d6924a7` |
| `gateway/vault/vault.py` | `e95c5594e8bdb558c19e572214bbeb0c26a6453e89dfb0cad78b982114a9b1c0` |
| `service/__main__.py` | `099db0072d163b717664c303f0e3e3e183b2ac41e3dbf37bccd7eaad3368dc7f` |
| `service/actions.py` | `b79825cc02ec4da11ccc1f82c64ef707f3f7598a6baf2d4678045b57e1812ae8` |
| `service/ingress.py` | `de103518172922ff8112f073bcc5dcc23234394bb3e482b8a052a4c67ca01885` |
| `service/reconciler.py` | `e6a1096da19e8dd48678c1a8d7d959a3ae7e0894bd204200c4ecb4eee8fbcbaa` |
| `service/registry.py` | `582e91fb015ad0c15a5dd521a1b40876a51024b7c09198259f745b793d097d32` |
| `service/settings.py` | `986aca764f374c6d5dd8bd7d1e05d4f8a6ce6bc4288def6236113fe7cd734f45` |
| `service/shadow.py` | `2d929e3e2c4ebb334100c6cc2854b5121759b11b2ef177b483826e4df591e243` |
| `service/tests/goldens/registry_setup.json` | `38658b664f411039c6a446b65937fc24ee39d0f110762058c23577e7893cef8d` |
| `service/tests/test_pending_review_corrections.py` | `24bbc7dbdbfeac0ac2c170df5d55a4e7c241615c8b3588fecd5cc2d26f2558a4` |
| `service/tests/test_registry_setup.py` | `996b0de8c51837f8e76eafe71fb49972dbcc03085a4c0663586c930297d7c20e` |
| `service/tests/test_shadow.py` | `6eabf7f91347b927c12104d02edf4c90f36fede9502fcf8542c92b74776db8b9` |
| `service/tests/test_shadow_api.py` | `389f9c8789f1269e329729b48e8b6e66bf5493a5403fd812ab417bab65c429f2` |
| `service/tests/test_worker_structure.py` | `eb3592e67c8c088eba49e3e708cf45b8dc201421333e77c888ed207fa454ac9b` |
| `service/worker.py` | `f077782c9f50de193c89c980ffa1d0fa4ceb1d4ddfb7b78e18dc9e9e761a4bdf` |
