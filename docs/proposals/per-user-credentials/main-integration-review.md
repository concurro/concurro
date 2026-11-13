# Main integration review

This record covers the [current integration](main-integration.md), superseding earlier
release-readiness conclusions for these merged sources. Both pending merges remain uncommitted:

| Repository | Sponsorship parent | Main parent |
| ---- | ---- | ---- |
| Backend | `57a6442aef58c4006de34f7f0428747c0b7f379b` | `f7a3d70b24536f6ec32524f6dba4328f870e5bb7` |
| Portal | `8aa752710f697ab145d4e7b2e521f03f79adf32b` | `e3e42447742c32315c4379524ee0dfda5dd578b8` |

## Review identity

Fresh Claude CLI session `76332826-ba28-4ada-86cf-51129cd99ca4` used
`claude-fable-5-2`, effort `medium`, tools disabled and strict MCP configuration.
All three actual results report `is_error=false` and canonical model `claude-fable-5-2`.
The final result is `approve`, `findings=[]`, `read_complete=true`, with the exact R3
manifest and `END_OF_COMPLETE_MERGE_PACKET_20261113_R3` marker.

- Final 618-file source snapshot: `/private/tmp/concurro-main-merge-round3/manifest.json`,
  SHA-256 `4e00ae68639d1afe30a1244a8d4754bd79c01ea15803e5ba112ef8232939d6b4`.
- Final raw verdict: `fable-result.json` beside that manifest,
  SHA-256 `e4c71c48a8e70d28376eceb7b5fe852d2ef37b4386ce398f5631e61fce584a73`.
- R1 and R2 immutable packets/results remain in sibling `concurro-main-merge-round1/2`
  directories. R1 supplied 105 full bodies, relevant merge deltas and exact remaining-file
  hashes; R2 supplied changed bodies and previously omitted dependencies. R3 supplied the
  final definition/test/doc changes. Coverage paths are enumerated in the manifests.
  Hash-only files were not body-reviewed. CLI context compaction occurred; the final
  reviewer explicitly did not reread unchanged R2 bodies. This is an integration-seam
  review, not a new exhaustive body review of all 618 files.

The first upload approval was rejected as insufficient authorization for sensitive source.
The accepted retry cited the user's explicit Fable review request and enumerated nonsecret
inputs. Both repositories are public, but the packet includes **unpublished integration
source**, not exclusively published code. No credential files, private host configuration,
live account data or provider calls were included. Exact rejection/retry and this correction
are retained in R1 `approval-provenance.json`.

## Findings resolved

| Finding | Disposition |
| ---- | ---- |
| F1: numeric actor lookup rejected by production transport | Added the narrow read-only positive-ID endpoint; real HTTP Transport → confirmation/preflight tests retain upstream denial before charge. [GitHub documents this endpoint](https://docs.github.com/en/rest/users/users#get-a-user-using-their-id). |
| F2: foreign actor/cutoff through own-slot history | Shared redaction excludes foreign transitions, including when they carry the caller's slot. |
| F3: upstream-only writer command ignored by fork association filter | Human command wakeups precede that prefilter; controller upstream authorization remains authoritative. |
| F4: moved/deleted issue breaks discovery | Reused one exact 301/410/NotFound predicate. Outage swallowing was refuted: safe `CommandError` 503 and other transient failures remain visible. |
| F5: nonwriter read invokes privileged coverage/observations | Reads own evidence directly from Registry; canonical task slots preserve initializer-only grants without vault/Exchange calls. |
| F6: repeated no-prior-grant withdrawal comments | Reviewer proposal, explicitly outside the authorized defect scope; no new gate or changed withdrawal behavior. |
| F7: auth-state overlay replaces read-cache volume | Separate container auth path; existing host files remain in place. Forward settings-path correction is documented. Actual Compose rendering verifies both mounts. |
| F8 / primary: explicit refresh returns fresh cached summary | Task panel uses existing `Resource.refresh`; uncached issue view retains invalidation. Actual page reproduction changed one request to two in 103 ms, with phase content retained. |
| F9: conflicting release order | Combined two-SHA updater preferred; individual upgrades API first, first-install web selection explicitly distinguished. |
| R2-1: own automatic cutover receipt omitted | Matches provider plus caller-owned initialize grant; minimal own link preserves repeated redaction. Foreign grant/provider/actor negatives covered. |
| R2-2: misleading persisted-cache disclosure | Documents nonsecret sponsorship/ledger/admission identities separately from secret material and permission decisions. |

The primary's initial initializer-slot-loss candidate was refuted: production `Shadow.view`
already promoted task slots. Its real Registry/view/action regression now also protects the
raw nonwriter path. `Shadow.cutover` retires only implicit task-start/label grants;
explicit sponsor grants are ineligible. The portal history renderer consumes provider,
reason and optional cutoff, never actor/slot. These resolve the reviewer's static uncertainties.

## Local validation

| Evidence | Result and boundary |
| ---- | ---- |
| Initial merged full suites (`concurro-main-merge-checks01`) | Engine 1186, one existing skip; service 535; gateway 165; BFF 117; deploy 23. Ruff, format, Pyright, compile, ShellCheck and bundle checks pass. |
| Final affected suites (`checks03`) | Service 539; BFF 120; Ruff/format/Pyright/compile pass on final backend sources. The changed own-consent helper is covered by its service/BFF consumers; the full engine suite was not rerun after that helper change. |
| Deploy helper rerun | 23 pass; corrected overlay fixture and installed combined helper retain optional override arguments. Public Compose render confirms independent cache/auth mounts. |
| Portal (`frontend-gates02`, `visual03`) | Tests, Svelte check, live/mock builds pass; loaded light/dark views, current-plan approval during lazy loading, receipt convergence, own-only discovery and no-prior-grant withdrawal pass. |
| Exact-parent forward upgrades | Actual parent writers generate Registry 5/8, checkpoint 7/8/9 and persistent-cache fixtures; source-policy evidence, audit bindings and cooldowns survive. |
| Current native gateway (`runtime01`) | Four isolated production-config cases pass: four routes/spoof isolation, exact native-budget join/non-turn probes, denial/outage/revoke without fallback, unchanged org operation. |

Runtime uses `agentgateway:v1.7.0` digest
`60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b`.
Its 14 source identities and the portal's 94 visual-source identities match final sources.
All 107 manifest evidence hashes were checked. Historical native-CLI, TLS and WAL runs retain
their original provenance; they were not relabeled as current merge runs. Preparatory failures
and the pre-fix refresh reproduction remain under `/private/tmp/concurro-main-merge-*`.
Batched reuse searches cover 293 Python and 41 portal definitions plus repair deltas;
no material duplication remained. Both merge indexes and parent identities were left untouched.

## Deployment limits

Follow [current readiness](deployment-readiness.md) and [App migration](../../../backoffice/APP-MIGRATION.md):
sign both accepted merges, pass exact-release CI/image publication, then verify external
Sign-in/Reader App configuration/grants/callbacks, private principals/networks/mounts and
active-task bundle handling. Optional encrypted logout state needs the documented container
path update; existing host receipts are retained. These private settings were not inspected.

Own-consent discovery retains per-issue GitHub reads; unrestricted own-withdrawal retains
the acknowledged App-comment amplification risk. Cutover redaction tests use production-shaped
synthetic history rather than running a new full cutover-to-portal journey. No new restriction
was introduced. Live GitHub/App installation behavior and real subscription backend/model,
account-header, expiry and account portability checks remain pending by user direction.
Optional provider refresh remains separate. No deployment, push or live retry occurred;
recovery remains roll-forward only.
