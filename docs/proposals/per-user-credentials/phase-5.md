# Phase 5: pasted subscription credentials

This records the Phase 5 boundary. [Phase 6](phase-6.md) now adds eligible automatic
credential recovery; the manual-only recovery statements below describe the earlier release.

Both subscription owner PUTs are enabled through the existing authenticated portal/BFF/vault
path. API-key behavior remains compatible. Production contributed routing and pauses stay on
the Phase 4 authority path; organization-funded tasks keep org credentials.

## Registration and evidence

The portal instructs users to run local Codex `login --device-auth` and use their existing
local access-token retrieval procedure; login has no token-export flag. Claude
`setup-token` prints a token after browser login; `auth login --claudeai` is browser login,
not a device-code flag. Help was rechecked without login or credential reads against runner
pins Codex 0.163.0 and Claude 2.1.301. See the official [Codex authentication guidance](https://developers.openai.com/codex/auth/)
and [Claude authentication guidance](https://code.claude.com/docs/en/authentication).
No custom OAuth client, import helper, password, refresh token or CLI credential file is used.

`PUT /api/me/credentials/{route}` accepts `secret` and, for `chatgpt_subscription`,
`account_id`. The vault authenticates the GitHub owner and stores the candidate without
provider requests or model selection. The UI says Save and shows the update time. Task
configuration selects the model; authentication and model-access errors are handled during
execution. Saving a replacement retires the previous version even if the new token does not
work. Empty or header-unsafe input is rejected locally without echoing secrets.

New credentials emit `stored` events and have no validation timestamp or model coverage.
Historical fields/events remain readable. The service and Exchange accept both event types;
model coverage is no longer an admission or automatic-recovery requirement. The existing
owner/route revision fence still prevents a concurrent stale write from undoing a replacement
or revocation. Save outcomes lost in transit remain unknown until refreshed.

ChatGPT account-header material is encrypted with the exact token/version. Public owner
metadata exposes only an opaque supplied-account hint. Saving does not authenticate that header; `accountVerified` remains false. No unsigned JWT
claims, guessed lifetimes or unrelated OAuth scopes are accepted as evidence. Pasted-token
expiry remains unknown. Existing trusted exact-version expiry, when available independently,
continues to deny resolution and retire that version.

## Mutation and runtime boundaries

Vault schema 3 adds only `credential_fences(user_id, route, revision)`. Before saving, PUT
captures this private revision. The replacement transaction checks it, then atomically
advances revision/version, destroys old material and emits existing lifecycle events.
Every DELETE advances the fence, including an empty or already-revoked slot, so a candidate
in flight cannot revive it. Stale completion returns sanitized 409. Public retirement events
remain idempotent; there are no synthetic version-zero records/events. Schema 2 migrates
forward, preserving ciphertext, history and existing known/unknown evidence.

The real portal-to-vault journey exposed a Phase 4 integration defect: the native resolver
hook expected a string account while the existing sealed record returned a map. The hook
now extracts `chatgpt_account_id` from that exact version's map and refuses missing or
malformed values, preserving legacy string fixtures and API-key behavior. Failed isolated
runs remain in the evidence rather than being relabelled successful.

Replacement/revoke use existing exact-version events, Exchange retirement and task preflight.
They do not transfer sponsorship, reset budgets/approvals, wake a task or retry inference.
Known resolver expiry/revoke produces trusted `auth`; unverified subscription-backend errors
remain sanitized `unknown`. A writer corrects coverage and explicitly retries. Retained
transcripts keep their original task scope across replacement/transfer; admitted old work
keeps its frozen identity. Optional refresh and automatic recovery remain phases 5a and 6.

## Verification and operation

Isolated tests cover both providers/two accounts, saves without provider calls or model selection,
atomic replacement, stale writes, revoke during saving including empty slots, forward
migration, private-principal isolation and redaction. The real Controller/Registry/Exchange
journey tests running-turn trusted expiry, one charged failure, no replacement auto-wake,
explicit writer retry/new version and preserved grants. The retired opt-in `phase5_spike.py` ran the
actual pinned gateway and native CLIs against fake providers, with actual BFF/public-private
vault storage, replacement, retained resume, graceful account transfer and isolated revocation.
Its manually admitted native turns do not independently prove Controller budget accounting.

Live subscription execution, authenticated account-header semantics, token expiry and
cross-account transcript portability remain **pending** by the user's live-account deferral.
Fake-provider protocol success is not provider approval or evidence of those live capabilities.
Provider-specific inference behavior is verified separately from credential storage.

Credential saving has no provider transport; the vault image no longer contains the inference
protocol/catalog. Follow the [save-only rollout order](../../../gateway/README.md#save-only-credential-rollout):
upgrade service and Exchange consumers before permitting new vault writes, then update the
backoffice API and frontend. Retain the existing private-listener configuration. No new secret or model-catalog configuration is
needed. Back up state consistently with WAL and retain wrapping keys before the forward
migration. Recovery is roll-forward only; preserve revisions, versions and retirement events.
No deployment, provider login, real provider call or live task retry was performed here.

Local acceptance (2026-11-13): engine 1185 tests (one existing skip), service 503,
gateway 156, BFF 45 and deploy 13 pass; final affected suites add deadline/API-key
CAS and timeout coverage (gateway 158, BFF 46), with the Controller journey passing standalone; Ruff, format, Pyright, compile, Shellcheck,
bundle validation and frontend check/build pass. The three Phase 5 native cases pass
against agentgateway v1.7.0 image digest
`60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b`
and runner pins above. The fifteen existing Phase 4 native regression cases are rerun
against the extracted protocol; their source-bound manifests distinguish these new runs
from historical Phase 0/4 evidence. Raw isolated evidence is retained under
`/private/tmp/concurro-phase5-runtime09` and
`/private/tmp/concurro-phase5-inference-regression01`.

The following historical reproduction requires a pinned pre-retirement checkout;
`gateway/tests/phase5_spike.py` is absent from the current tree. Use that
checkout with its pinned CLI bin directory and isolated Docker:

```bash
python gateway/tests/phase5_spike.py --output /tmp/NEW_PHASE5_RUN --clients /path/to/pinned/node_modules/.bin
python gateway/tests/phase4_spike.py --output /tmp/NEW_REGRESSION_RUN --clients /path/to/pinned/node_modules/.bin
```

Each output directory must be absent. Runners retain image/config/source/artifact hashes,
raw fake-provider journals, native outputs and secret-scan results, including failures.

Review repairs also validate the actual root-context vault/Exchange image builds and
network-disabled imports under UID 65532. Marker-free native retry/takeover prompts assert
both retained upstream content and returned answers; omitting the archive fails as expected.
The scan includes synthetic owner/private bearers and all candidate tokens. An earlier run
correctly found repeated-digit test bearer markers inside the pinned native executable;
unique synthetic markers remove that collision without weakening the scanner.
