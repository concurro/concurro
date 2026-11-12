# Review: per-user credentials implementation plan

Independent review by **Claude Fable 5.2, medium effort**, of
[implementation-plan.md](implementation-plan.md) on 2026-11-12
against the [epic](../per-user-credentials.md), the
[cross-cutting rules](../README.md#cross-cutting-rules) and the tree at
`0f0bd17dd91e7cba47327c8e907021b66e7f31c8`.


**Current verdict: ready to implement.** Claude Fable 5.2 at medium effort explicitly
approved the final repaired text on 2026-11-12 with no remaining design findings.
Codex agrees. The final readiness section below records the reviewed snapshot and limits.

**Method.** Source was read, not run: no tests, spikes, logins or provider calls were
executed. Provider pages were fetched on the review date; "published" below means the
page says it, not that Concurro has exercised it. This review covered the earlier custom-OAuth proposal. Its source/accounting findings
remain useful; the provider-flow and gating dispositions below are historical and
superseded where noted. The focused follow-up below covers the user-directed token-paste revision.

## Initiator-fill steering after the provider-slot amendment

The user clarified that initial start/label may assign the initiator for available
credentials, while earlier explicit issue-comment sponsorship takes priority. This
supersedes the previous amendment's “no implicit funding from start/label” rule below.
The current plan adds confirmed issue-level intents before enrollment, source-order
reconciliation, provenance and provisional/confirmed backoffice badges. It reuses existing
registry receipts/observations/recovery jobs, adding narrow sponsorship source/projection
records rather than a separate queue. Explicit sponsors with missing credentials retain
priority. A pre-enrollment withdrawal prevents that person's auto-reassignment, not a
different initiator's own first grant. After initialization, withdrawal/failure never
trigger automatic reallocation.

Local source inspection covered `Ingress.comment`, `Registry.receive` and observation
methods, `Reconciler.snapshot`/`observe_pending`/`candidates`, `Recovery.poll`,
`Worker.run`/`consumed`, `Controller.step`/`initialize`, `CommandHandler.start`,
`Actions.act`, `UserAccess.task_issues` and `Tasks.task_view`. Their current limits and
required extensions are recorded in the plan. No runtime tests or provider calls were
made. This steering revision was subsequently reviewed and repaired in the follow-ups
below; the final readiness verdict supersedes the earlier review status.

## Provider-slot amendment after the CLI-token review (earlier steering)

The user subsequently requested different explicitly consenting sponsors for OpenAI and
Anthropic on the same task. The latest plan replaces the reviewed single-payer baseline
with route-derived provider slots, independent epochs, caller-only `sponsor <provider>`,
self `withdraw <provider>` and no implicit funding from start/label/retry. Withdrawal is
task/provider/caller-scoped, including that caller's older epochs; another sponsor's work
is untouched. Writer loss affects all that user's grants on the task, not other users or
other tasks. Required batches preflight coverage across all owners before charging.

Earlier findings 4, 7, 11, 13 and 14 retain their source facts but their single-sponsor
schema/command/migration dispositions are superseded. The earlier readiness and acceptance
are historical, not a review of provider slots. CLI token paste, route validation,
manual replacement and optional refresh remain as previously reviewed. **Claude has not
yet reviewed this provider-slot amendment.** Documentation checks are repeated below;
no runtime changes, tests, logins or provider calls were made.

## User-directed revision after review

The user clarified: “We should do exactly as we've done today -- use the device-login
flow from each CLI and have the user provide their token”. The plan now reuses local CLI
login and the existing authenticated BFF PUT/vault ownership path for both providers,
keeping today's gateway backends and headers. Manual token replacement completes the
initial subscription deliverable; refresh is optional only with usable supplied material
and demonstrated CLI-grant behavior.

This supersedes findings 1 and 3, the SIWC lifetime/refresh/error assumptions in 10 and 15,
the provider-agreement disposition in 16, and the earlier subscription readiness/gating
conclusions below. The custom registration/helper, GitHub helper device flow, public vault
import route, public SIWC Responses migration and agreement prerequisites are removed.
Provider terms remain the operator's/users' responsibility; no provider approval is claimed.
The core identity, budget, isolation, revocation and crash-recovery findings are preserved.

Codex checked local CLI help only: `codex login --device-auth` exists; Claude has
`setup-token` and `auth login --claudeai`, without a shown device-auth flag. No login,
credential-file read or provider call occurred. Claude subsequently reviewed the amended
plan and supplied route/source evidence; see the focused follow-up below. The following
original findings and acceptance describe the earlier revision only.

## Findings and dispositions (earlier revision)

| # | Finding | Evidence | Disposition |
| ---- | ---- | ---- | ---- |
| 1 | The plan didn't say plainly which parts of the user's goal (subscription sponsorship) it can deliver. PR 5 bundled both providers and required "an applicable Claude integration", so it could never complete. | Provider pages below | Added a readiness table; split PR 5 into 5a (lifecycle, fake provider), 5b (ChatGPT, gated on OpenAI) and 5c (Claude, unscheduled). |
| 2 | `Exchange.submit` was to call a service endpoint for the writer decision. The service is the Exchange's only operator, so this is self-vouching, plus a call cycle around the lifecycle lock; the controller preflight already made the same GitHub check. | `service/worker.py:Worker._operator`, `executor`; `gateway/exchange/exchange.py:Exchange.locked`, `submit` | One writer decision per admission in the controller; denial is written to the Exchange as a task/epoch revocation. Added an authority table. Epic §3 wording aligned. |
| 3 | The local-connect helper was to present "the user's GitHub token" to the vault, but the BFF never releases that token and the vault has no edge route. | `backoffice/auth.py` module docstring; `gateway/compose.yaml` header comment; `gateway/vault/vault.py:Identity.resolve` | Proposed GitHub device flow for the helper (reuses `Identity.resolve`) plus one import-only edge route; import code as the alternative. Both listed as App/edge changes to approve. |
| 4 | Adding the enroller's user ID would block every existing autonomous task: `refresh_autonomous` compares the whole saved record with `Controller.enrollment()`. | `engine/controller.py:Controller.enrollment`, `refresh_autonomous` | Comparison and in-place upgrade rule specified; acceptance test added to PR 1. |
| 5 | A new persisted "exchange turn UUID" duplicated an existing one. `begin_attempt` already persists a per-attempt UUID before dispatch and `RunnerExecutor.submit` already accepts an identifier; the serial and batch callers just don't pass it. | `Controller.begin_attempt`; `engine/agents.py:RunnerExecutor.submit`, `run`; `engine/review_batch.py:run` | Turn ID is the attempt UUID. Clarified that replay happens only inside `RunnerExecutor.admit`; after a crash the engine doesn't resubmit, so no double charge by construction. |
| 6 | `Controller.ask` selects the credential once, outside its two-attempt loop; the restart after `SessionFailure` is a new charged attempt and would have reused a stale selection. A credential failure would also have consumed that automatic restart. | `Controller.ask`; `Agents.decode_result`; `Design.failed` | Selection moves inside the loop. Trusted category returns with the turn result and raises a dedicated failure ahead of `SessionFailure`. |
| 7 | Ledger re-keying and a separate Exchange admission record were heavier than needed. | `Controller.charge_call` keys by `credential.id`; the binding is stored, digested and hashed into staffing (`Exchange.submit`, `staffing`) | Contributed `Credential.id` is the identity digest, so `charge_call` is unchanged; the versioned binding is the Exchange's frozen admission. Selection is per session, not per active turn. |
| 8 | `attempt_result.restore` compares bindings exactly; adding payment identity unconditionally would strand receipts written before the change. | `engine/attempt_result.py:binding`, `restore` | Admission digest included only when the operation has one. |
| 9 | Secret-returning resolution was "private" without saying how. The vault has one listener on the shared `edge` network and only user-token auth. | `gateway/compose.yaml` (`vault` service); `vault.create_app` | Separate resolver listener on a gateway-only network; service metadata endpoint called out as new. |
| 10 | Refresh had no trigger other than use, and nothing said tokens are resolved per request. Published lifetimes (one-hour access, 30-day refresh) are shorter than a turn lease and an idle pause. | `engine/agents.py:DEFAULT_LEASE_SECONDS`; OpenAI token reference | Vault refreshes on demand and on a schedule; resolver resolves per request. |
| 11 | Replacement, rotation, revocation, permission loss and takeover were described in prose with overlapping terms. | `vault.Store.put`, `revoke`; `Exchange.retire` | One table of event, version effect and effect on admitted work. |
| 12 | `awaiting_credentials` had no implementation home. | `engine/vocab.py:SUSPENDED_STAGES`; `Controller.suspend`; `CommandHandler.retry` | New suspended stage reusing those paths; rollback boundary noted. |
| 13 | Sponsor-transfer fence needed a call on every ownership change and had no creation rule. | Generations change per acquisition (`Worker.executor` task binding) | Fence compares epoch and sponsor, tracks generation monotonically, and is created on first use. |
| 14 | Reference drift: `agent_entry.command` is `command_for`; `/concurro sponsor` touch points omitted `backoffice/tasks.py:COMMANDS`; test list omitted `test_delegation.py`, `service/tests/test_actions.py`; manual tasks were assumed to retain start evidence. | Named files; `state["command_id"]` is overwritten by later commands | Corrected. Existing manual tasks need an explicit takeover. |
| 15 | Provider claims not on the cited pages: the sign-in page doesn't mention Codex's client ID; ChatGPT lifetimes, error codes and the absence of a published quota or reset time were missing. | OpenAI sign-in, token-reference, errors-and-recovery pages | Client-ID rule restated as Concurro's own; added a published-versus-assumed table and an error-code mapping. No Concurro cap added. |
| 16 | The Claude constraint was understated as "no published contract". | Anthropic legal-and-compliance, "Authentication and credential use" | Quoted the published exclusion of collecting, storing or intermediating plan credentials; route marked unavailable pending agreement. |

Checked and left as written: checkpoint format numbering (`report_store.FORMAT` 7,
`bundle_resume.FORMAT` 6); the `engine` binding field in the original checkout's
uncommitted work (read-only check of its `session_binding`); agentgateway image pin;
budget, optional-role and approval-identity rules; GitHub refresh and `offline_access`.

## Residual validation (earlier revision; superseded provider flow)

These can't be settled from the repository and are not claimed:

- **OpenAI applicability.** Whether a remotely hosted multi-user deployment may use the
  documented flow, and whether vault import counts as the documented credential
  transfer. The docs cover open-source and locally hosted apps and point hosted apps to
  an interest form.
- **Claude subscription.** No published path for a vault-held grant. Needs an agreement
  or a separately approved runner-login design.
- **Gateway mechanism.** Whether v1.7.0 `llm` models can take a CEL-derived backend key
  from external-authorization or API-key metadata, and whether trusted streaming
  observations are available. The documentation shows routing-mode examples only.
- **Harness behavior.** Pinned Codex emitting the restricted public Responses shape;
  resuming a retained transcript under another account for each harness; provider
  behavior when a token expires mid-stream.
- **Refresh semantics.** `earliest_refresh_at` and refresh-token single-use are inferred,
  not stated.
- **Host sessions.** Remaining `kind="host"` call sites should be confirmed when
  implementing the resolver.

## Readiness (earlier revision; superseded)

PRs 0-4, 5a and 6 are specified well enough to write implementation briefs, with PR 4's
mechanism decided by PR 0's evidence. PR 5b is specified technically but blocked on an
external confirmation; 5c is not implementable as planned. The plan therefore does not
yet deliver multi-user subscription sponsorship end to end, and neither subscription
route should be described as supported.

## Consolidated acceptance (earlier revision only)

Codex checked the repaired plan against the source and provider references, then clarified
implementation details:

- `Controller.begin_attempt` stores `uuid.uuid4().hex`; the Exchange's `UUID`,
  `SESSION_PATH` and `OPERATOR_PATH` require hyphens. Normalize the same UUID at the
  transport boundary and preserve checkpoint/receipt identities.
- Historical admissions remain in versioned audit artifacts (`Checkpoints.save`/`history`),
  even when a new attempt replaces the live operation fields.
- Trusted `rate_limited` outcomes also bypass `SessionFailure`'s automatic restart,
  consistent with the recovery table.
- An expired in-flight refresh lease is an ambiguous provider outcome, not permission
  to replay a potentially rotated token.

These are documentation repairs with explicit acceptance cases, not executed runtime
tests. Provider applicability and empirical gateway/harness validation remain open as
listed above.

Validation: the four changed Markdown files passed whitespace, table separator, tagged
fence and relative-link/anchor checks (25 references). No runtime files changed.

## Checks for the user-directed revision

Changed documents only: implementation plan, epic, proposal index and this review record.
Whitespace, table separators, tagged fences and relative file/anchor checks are rerun for
this revision. No runtime tests or provider experiments are claimed. Remaining technical
unknowns are the chosen gateway mechanism, current-route model/account validation, exact
local Codex token-retrieval instructions, account-transfer transcript behavior and any
optional refresh/revoke support of the supplied CLI tokens. They are not agreement gates.

## Focused Claude follow-up on CLI tokens

Claude Fable 5.2 at medium effort reviewed the amended implementation plan, the existing
`gateway/agentgateway.yaml`, `backoffice/credentials.py` and the observed local CLI help.
Its verdict was **approve with changes**. The follow-up intentionally did not repeat the
unchanged admission review or inspect runtime credentials. Codex applied its ten focused
repairs and checked consistency with the epic and index:

- Validation uses the candidate token/account, never a model entry injecting operator
  credentials; runtime route selection cannot fall through to an operator backend.
- Rejected candidates and transient validation outages preserve the current credential;
  quota/rate pauses preserve ciphertext rather than demanding replacement.
- Resolver-side expiry produces a trusted credential outcome without a provider call;
  tests include expiry/auth rejection during a running turn.
- Subscription route registration ships with its compatible inference validator; the
  API-key validator never receives CLI tokens. Account metadata is forwarded through
  both `SecretBody` and `Vault.put`, with status/UI mappings updated together.
- Subscription coverage uses the configured-route inference probe. Optional refresh
  remains optional in rollback; obsolete SIWC flow wording is removed.

Claude agreed that pasted CLI tokens and manual replacement form the complete initial
subscription design, with no custom OAuth/helper or provider-approval prerequisite.
The gateway/account-validation and transcript experiments are implementation work, not
executed evidence. No runtime tests, logins or provider-token calls were performed.

## Provider-slot amendment checks

The epic, implementation plan and cross-cutting rules now agree on explicit per-provider
consent, caller-only signed commands, monotonic slot tombstones, scoped retirement and
legacy migration without inferred grants. Minimal backoffice/routing references are
aligned. Whitespace, table/fence format, relative links/anchors and unchanged-index checks
are rerun for the amendment. Its concurrency/crash/withdrawal cases are implementation
acceptance requirements, not executed test results.

## Latest amendment checks

Six proposal documents are aligned on explicit-first initialization, immutable grant
provenance, issue-before-task visibility, known-incomplete-history retry, non-retroactive
admission and scoped provider withdrawal. Earlier review conclusions are historical; the focused Claude review below covers this
amendment. Whitespace, table/fence format, relative
links/anchors and unchanged-index checks are rerun before handoff.

## Claude review of provider slots and pre-enrollment sponsorship

Claude Fable 5.2 at medium effort reviewed the amended plan, epic, cross-cutting rules
and backoffice proposal. It requested six repairs; Codex applied them and checked the
resulting document consistency:

- Late-discovered earlier claims use the ordinary provider epoch/Exchange transition,
  never an in-place payer rewrite. Later confirmed changes supersede stale evidence;
  only unorderable conflicting evidence needs reconciliation.
- Preflight accepts both explicit-comment grants and recorded one-time starter/label
  grants.
- Initial fill is evaluated at the initiating event. A withdrawal/edit/deletion after
  that boundary cannot unexpectedly make the initiator pay; applied source revisions
  are recorded so they are not applied twice.
- Pre-enrollment self-withdraw checks identity, not continuing writer permission.
- A pre-enrollment withdrawal records the caller/provider opt-out even without an older
  grant; it does not veto another person's initial sponsorship.
- Issue projections have revisions; task slot epochs begin at checkpoint adoption
  (1 for adopted grants, 0 otherwise) and then increase monotonically.

The reviewer found no cross-provider binding leak or epoch-reset issue in the steady-state
transition design. This was a documentation consistency review, not a live webhook,
gateway or concurrency test. Implementation acceptance still includes source-order races,
missed deliveries, lifecycle failures, two sponsors and provider-isolated attribution.

Final amendment checks passed: six Markdown documents, 41 relative file/anchor references,
table/fence formatting and whitespace. No runtime files changed; changes remain uncommitted.


## Final readiness review — 2026-11-12

The final review used `claude-fable-5-2`, medium effort, in reviewer session
`e8d3696a-c1f3-4d8a-af93-274e9d461ff4`. Its first pass confirmed the six preceding
repairs and requested three further fixes. Codex resolved them:

- **Release dependencies:** land service credential metadata first; PR 1 owns the trusted
  provider/route catalog. PRs 1–3 ship together before initialized-task commands are
  consumed. Earlier ingestion retains hints without consuming commands or grants.
- **Funding-mode consent:** explicit sponsor commands consent to future contributed
  calls. Org-funded starter grants never silently become personal payers at cutover;
  clear them through normal epoch transitions and require explicit sponsorship.
- **Late withdrawal:** a confirmed pre-boundary opt-out retires the initiator's implicit
  grant through the ordinary withdrawal transition, once, without refund or rebinding.

Codex also found that the current review-batch exception path cancels all live members.
The plan now requires a provider pause to drain unaffected admitted work, save valid
results and then pause, without new charges. Whole-batch cancellation remains appropriate
for lost task authority, stop, invalidated work or a terminal verdict.

Fable reviewed the repaired plan, epic and cross-cutting rules, the unchanged backoffice
and routing proposals from its first pass, and the relevant `review_batch.py` and
`design_batch.py` source excerpts. It returned **`ready_to_implement`**, with **no findings**:
“No unresolved design findings. I agree the final text is clear and executable as an
implementation plan.” Codex's consolidated acceptance agrees with that verdict.

Carry these non-blocking implementation notes into PR 4's brief: include
`engine/review_batch.py` and `engine/design_batch.py`; handle pauses from mid-batch reserve
preflight and revocation-retired turns as well as decoded failures; after draining, the
pause takes precedence over the scheduler's no-pending-work error. The epic's remaining
org-pool question is resolved for this implementation by the plan's explicit per-target
funding mode; it does not leave the payer policy undecided.

### Reviewed snapshot

These SHA-256 hashes identify the five normative documents accepted by the final review.
This review record is excluded because it records the verdict afterwards.

| Document | SHA-256 |
| ---- | ---- |
| `docs/proposals/per-user-credentials/implementation-plan.md` | `47f80149861a8941e56b67568c3cb4e9e8986f5d7cc41b3b9c5e67fab67e4da4` |
| `docs/proposals/per-user-credentials.md` | `eb6bf5c3a9ed22fa65c8859c9c89df7460a6e3a135663839d3676d1f90c42ae2` |
| `docs/proposals/README.md` | `9c11b8f73d0391247803d70e7750206140ff7d08050f0825ba94696960e9533a` |
| `docs/proposals/backoffice-api.md` | `436df4397866e33aca4ba4d942986b6f29182fa4dbec3104ce8e918b6333ce8a` |
| `docs/proposals/risk-driven-routing.md` | `8f4a4a8f9e930fb6134b06524ccf00254a32af7d3f4b71619776876b1c9bd7a1` |

Readiness covers the design, integration responsibilities, sequencing and acceptance
criteria. Gateway injection, account-header validation, exact CLI token retrieval,
transcript portability, streaming classification and concurrency/crash behavior still
require the planned phase-0 experiments and implementation tests. The final reviewer
used the supplied text and source excerpts, without tools or provider calls; no runtime
validation is implied.

Final document validation: six Markdown files; relative file/anchor references, tagged
fences, table separators and whitespace checked. Changes are documentation only and
remain uncommitted.
