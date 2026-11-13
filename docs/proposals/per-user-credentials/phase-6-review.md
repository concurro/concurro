# Phase 6 implementation review

Reviewed 2026-11-13 against signed backend base
`bb44888654286bf23682905a34068ed37e815327` and portal base
`e72b36921339be66f3a3d2c1df8e2bf9586c1d87`. This record was added **after** review,
separately from the 29 reviewed implementation files. See [Phase 6](phase-6.md) for current behavior.

## Verdict and provenance

Claude CLI used `--model claude-fable-5-2 --effort medium --tools '' --strict-mcp-config`
with immutable public source and nonsecret synthetic evidence on stdin. Same session
`1d0a9b93-3ba4-4317-bcd4-590512d8e3d0` reviewed three rounds. Final raw result has
`is_error: false`, canonical `modelUsage` model `claude-fable-5-2`, `verdict: no_findings`
and `findings: []`. The unrecognized-model warning did not change its actual model identity.
The reviewer ran no tools/tests; local source, test and artifact hashes provide that evidence.

Final manifest SHA256: `77613f95313bd7af07131f403f17597091ba3e0b2c93ae1dcba4c87c5c6fa3ea`.

| Round | Input SHA256 | Raw result SHA256 | Outcome |
| ---- | ---- | ---- | ---- |
| 1 | `ae5b63d15476cb862df35e5d85606a6261c271ebab4bf135ac5ca5950065f8dd` | `a33f903540faac139b3dedf01c4b1b30f6f14c26f770c867d8cd0c9ad9a598f9` | F1–F3 |
| 2 | `7214b4f2a015fe05f642dfa2b4f1bf98cf72c1bef8ae0b11cd6b58799d0a0450` | `73374356febae262ff8335e7031243bda69f3c9d9e90e8c058a6f4dbd6357a1b` | No findings |
| 3 | `1ddcfa6d1657d13dbd5dec919dd366b9627f8a72327c88d77a2c550810f77ef6` | `716d5e46702b2b905a42f46994c076ddf95baadaabe6865b554eb06c2ac2cbf5` | No findings |

Raw input/results, invocations, dependency hashes and evidence manifests are under
`/private/tmp/concurro-phase6-review03/`. Exact final files/patches are under
`/private/tmp/concurro-phase6-round3/`; `round2-to-round3.patch` captures the final bounded
repair. Earlier immutable snapshots and useful failures remain retained.

## Dispositions

| Finding | Resolution and evidence |
| ---- | ---- |
| F1: production feed trigger missing | Healthy delivery checks now call the existing policy observer every timer pass. Actual `Recovery.poll` → Worker → TCP Metadata → encrypted Store replacement page → Registry → charged dispatch test proves the real trigger; no direct feed-poll shortcut. |
| F2: actual retired vocabulary rejected | Service parser matches the vault's exact event/status vocabulary. Real replacement, revoke and destroyed legacy snapshot pages pass over TCP; retired v1 cannot strand validated v2. Full Store lifecycle confirms lock/transaction ordering. |
| F3: consumed receipt shown as scheduled; status lost | Same intent preserves status/consumption without another checkpoint write. UI requires an unconsumed actual delivery for scheduled wording; consumed/missing delivery shows waiting. Live/mock browser checks include both themes and issue badges. |
| Confirmed observer-coupling gap | Expected `NotFound` is isolated per target, so a removed fork/missing charter cannot suppress the feed and App delivery scan. Negative timer test reproduces the old failure; full service proves recovery plus arbitrary-error propagation. |
| Suggested uncertainty cap | Refuted, with no policy change: `Controller.ask` marks trusted terminal failures `failed`. Three consecutive auth failures then success retain one operation, four debits/attempts and uncertainty 0. A separate preexisting-uncertainty case still blocks without a debit and retains ordinary writer recovery. |

All three reported findings were supported and fixed. The expected-NotFound gap was
confirmed and repaired before the final verdict; unrelated failures are not swallowed.
Missing targets now warn individually at startup as well as during polling, while their
own task loads still fail. The final copy repair adds explicit spacing after last-check status.

Initial full gates also found and repaired a genuine normal-authority regression: intended-stage
checking must not freeze the ordinary stage across autonomous/manual approval changes.
Schema 8 SQL golden expectations and narrow Worker test doubles changed without weakening
lock/transaction/failure assertions. An explicit empty precharge failure snapshot now survives
reinspection instead of importing a previously acknowledged failure. No uncertainty,
correction, sponsorship, approval or spent-call allowance is reset.

## Verification

| Check | Final result and source boundary |
| ---- | ---- |
| Full engine | 1185, one existing skip; `checks03`, unchanged engine sources in the final manifest. |
| Full service | 529; exact final source, Ruff/format/Pyright/compile pass in `checks06`. Includes 26 real scheduler/Controller/Registry/Exchange recovery cases and production timer/real vault-feed integration. |
| Full gateway / BFF | 160 / 46 in `checks05`; no gateway/BFF source changed afterwards. |
| Deploy / packaging checks | 13 deploy tests, Shellcheck and bundle pass in `checks03`; those component sources are unchanged. |
| Frontend | `frontend-gates04`: check, live build and mock build pass on exact companion source. `visual08`: eight loaded light/dark captures, consumed/scheduled status and spacing, provider-bound self-withdrawal and mock no-charge/history/stage-mutation assertions. |
| Reuse | Initial 76 Python definitions and 4 TypeScript groups, repair superset and final two definitions searched with Lodestone. Existing queue/transactions, metadata auth, lifecycle ledger, authority/preflight, DeliveryAPI, listeners, SQL trace and admission fixtures reused. |

Paths abbreviate `/private/tmp/concurro-phase6-…`. Check manifests record commands, exact
worktree/source identities and raw log hashes. Review evidence manifests bind those checks
and browser artifacts; standalone targeted logs are supplementary to the source-bound suites.
These are local CI-equivalent checks, not remote CI, release image builds or a deployment.
Native gateway/CLI paths are unchanged; prior Phase 4/5 transport evidence is historical,
not relabelled as a Phase 6 run.

Preserved negatives include `checks01` authority/schema/double failures, `checks02` localhost
socket denials from a mistakenly sandboxed invocation, `negative-prior-pause01` stale failure
reimport, `negative-review01` missing timer/status proof (and a disproven uncertainty-test
assumption), `negative-feed01` rejected retired pages and `negative-target01` suppressed App
scan. Browser setup/timing failures remain separately in tmp.

## Limits and tradeoffs

The existing timer defaults to 300 seconds; each pass also reads current target policy.
The Registry transaction scans task projections/latest-version evidence rather than adding a
second scheduling index. Poll/read cost and production-sized migration are not benchmarked.
The service and vault must both provide the metadata-feed contract; no new secret/configuration
or resolver privilege is introduced. Registry schema 8 upgrades forward, retaining prior state.
Recovery is roll-forward only.

Legacy snapshot-only credentials lack a validation receipt and cannot establish automatic
readiness until re-registration. The last check status may remain visible next to a newly
scheduled receipt. Dedicated new cases do not exercise every authority-invalidating/replan
branch or review-peer uncertainty path; those retain existing fail-closed/accounting behavior
as reviewed in source. Arbitrary policy/Registry failures remain failures.

Real subscription backend/account/header/expiry and cross-account transcript portability checks
remain pending by user scope. Synthetic feed, OIDC and provider replies establish none of those.
Portal transport/rendering uses synthetic intercepted APIs and mock state, not a live deployment.
Optional refresh 5a is not implemented; no token refresh, provider taxonomy, organization fallback,
automatic consent restoration, live provider call or deployment occurred.

## Reviewed source hashes

### Backend

| File | SHA256 |
| ---- | ---- |
| `backoffice/README.md` | `1b63a7a269f277da04632205625df470f695959949515f0e3c51e86995de0992` |
| `docs/design/service.md` | `be5e452aa698853e9667c05758227f5cb8cee7ef721dea3e03c780aa792c5670` |
| `docs/proposals/per-user-credentials.md` | `688c2c64bb175a5e6de59974871b0e0a05add0a481e192167dd2d8186cdc0cf0` |
| `docs/proposals/per-user-credentials/implementation-plan.md` | `1f5e4ad53350db79f304f5b2a83c0fa810a4eb021286d6e7f9d1006670a9a19d` |
| `docs/proposals/per-user-credentials/phase-5.md` | `71e2a48d68dc555fab2aace4b569d3b9e9b237aa3b0c2c17a438b73a106b5c7a` |
| `docs/proposals/per-user-credentials/phase-6.md` | `7ef287475d0a1cbf0dd9aa324fc9cf9dd5f123f49ea75953b959354ba9887245` |
| `engine/commands.py` | `6434481f248414b9ca97472cb2eee881c92f1bae5ad540c42ae036d08b2a35dd` |
| `engine/controller.py` | `8904a3759a4ffe96eb891eb17456799c9f58e5076119b6e10be212c275bd439a` |
| `engine/records.py` | `deda7ad15a5fdc5f856929c10a247bd33bddea33262e0ba06c5c60413579d8ca` |
| `engine/sponsorship.py` | `aad406da3db4ac7e562a3621a5b6c36e43d37d29aa32a9ff96a4d6f27968c56d` |
| `gateway/README.md` | `37e390d08e4d0624e9d7b802de3a86e5bdb8f76bdeb2260cb8d52c4a17702e70` |
| `gateway/tests/test_vault_resolver.py` | `c039751f17f5130b903e18c856807e39e0f7795de730851980175971effd5ea6` |
| `gateway/vault/README.md` | `efc1726a44f857ea76b9910e5cf02493cba9bd388b7e04602773a27cd0b10c9e` |
| `gateway/vault/vault.py` | `e8dd4fae1d00088b447827c912de87dcbcbfc7a3876a8b6397412993f60f2d14` |
| `service/credential_recovery.py` | `0bf73053bc1f822cd991d4879680a39dcee883df3589aecf784c5755606ab161` |
| `service/registry.py` | `568f30743f89a1dd9fafb33cdf739cc7f97b72eea10786ad69da634cd8329ac2` |
| `service/shadow.py` | `baef789ddd1b1805ef0f266bbae8397be2d7c9ad8402f6d400cc5edb0f9e7be6` |
| `service/tests/goldens/registry_setup.json` | `feeb321438242eacbdc42a7c3aa574b0dabc5e13b47be077f182c48b733205f5` |
| `service/tests/test_credential_recovery.py` | `ae0425d30f9dc458ab74f2924a85c036595f6d9f7a17753d9d7c457e321bf469` |
| `service/tests/test_pending_review_corrections.py` | `8cc69f1acc73347e3ae20e02b551a2ea482ee77dbae27e47e99803c2dffe6669` |
| `service/tests/test_registry_setup.py` | `7321075eeb069c88f6f154d28eb2926dd9b589bcc841020c14155c643be1b198` |
| `service/tests/test_worker_structure.py` | `b73591d82bad7ff46061e2ae8bd33127d65f8acb2b9c8d7b9dc098cd0f84a7c4` |
| `service/webhook_recovery.py` | `9d55d5f13d16cf1bedc0cfa7d56d01b7fb2d8131ec97a89cd7bfe39b42022eb5` |
| `service/worker.py` | `34b2ffa9b032f350137578c09b394f21b616a3ae68ff7ea6ad39ea59eeb737ca` |

### Frontend

| File | SHA256 |
| ---- | ---- |
| `src/lib/components/IssueIntents.svelte` | `71f74899730fbbfef6c43e87ba3453f06c06be5158fc53be396dc6f1e7b704f4` |
| `src/lib/components/ProviderSponsorship.svelte` | `d4cc693e4d2f06c763e13e69f98e6e8e3eeb8ec841b853131234ff38a13d1792` |
| `src/lib/state/store.svelte.ts` | `1e448b50f2b2fa7bb7ab26c3ef411f3243a0308aa9749124d790e4644ec1bba4` |
| `src/lib/types.ts` | `005033b77ead8bf5a27470f4f0f8e4dce2cf1587fbe906a139ea94a94d373d12` |
| `src/routes/(app)/me/credentials/+page.svelte` | `cf154945caa2f3ca8bfc02353914bc7756ec15104fc39c4e33b4a51689595ca0` |
