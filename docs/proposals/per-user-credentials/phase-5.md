# Phase 5: pasted subscription credentials

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

`PUT /api/me/credentials/{route}` accepts `secret`, subscription `models` and, for
`chatgpt_subscription`, `account_id`. The vault independently authenticates the stable GitHub
owner. Default models come from existing target credential requirements plus prior validated
coverage, with owner edits allowed. One distinct-model probe per candidate uses provider
capacity and is disclosed before submit; these are not task attempts. There is no second
operator-maintained model catalog.

Both fixed upstream routes, response parser and probe construction share
`gateway/provider_protocol.py` and the trusted route catalog. Validation uses only the
candidate token/account, no operator secret or API-key model-list endpoint. Minimal streaming
Responses/Messages probes must finish successfully for every selected model before a version
is published. Redirects are refused. Candidate rejection returns a fixed 422; transient,
malformed or incomplete validation returns a fixed 502. Neither validation path changes
the prior version. A shared ten-second per-model deadline interrupts connected header/body
reads, including partial lines. System DNS, multi-address TCP connect and TLS handshake
may outlive that interval before the socket is captured. Deadline checks prevent sending
a late provider request/token or committing its candidate after connection returns; those
uninterruptible connection operations may retain a worker longer than the BFF timeout.
The BFF derives its larger timeout from the same constant. Indeterminate save outcomes
refresh status and remain labelled unknown rather than promising the old version survived.
Successful replacement stores only the models actually probed.

ChatGPT account-header material is encrypted with the exact token/version. Public owner
metadata exposes only an opaque supplied-account hint. Successful inference does not prove
the backend authenticated that header; `accountVerified` remains false. No unsigned JWT
claims, guessed lifetimes or unrelated OAuth scopes are accepted as evidence. Pasted-token
expiry remains unknown. Existing trusted exact-version expiry, when available independently,
continues to deny resolution and retire that version.

## Mutation and runtime boundaries

Vault schema 3 adds only `credential_fences(user_id, route, revision)`. Before probing, PUT
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

Isolated tests cover both providers/two accounts, model-set atomicity, invalid/transient
replacement, overlapping candidates, revoke during validation including empty slots, forward
migration, private-principal isolation and redaction. The real Controller/Registry/Exchange
journey tests running-turn trusted expiry, one charged failure, no replacement auto-wake,
explicit writer retry/new version and preserved grants. The opt-in `phase5_spike.py` runs the
actual pinned gateway and native CLIs against fake providers, with actual BFF/public-private
vault storage, replacement, retained resume, graceful account transfer and isolated revocation.
Its manually admitted native turns do not independently prove Controller budget accounting.

Real subscription validation, authenticated account-header semantics, token expiry and
cross-account transcript portability remain **pending** by the user's live-account deferral.
Fake-provider protocol success is not provider approval or evidence of those live capabilities.
The probe bodies/header combination are implemented on the configured routes but must still
be checked against dedicated real accounts before claiming that acceptance.

Candidate HTTPS transport is direct and does not use proxy environment variables;
an egress-proxied deployment is unvalidated. Deploy coordinated vault and Exchange images
containing the shared protocol/catalog, with
the existing private-listener configuration. No new secret or model-catalog configuration is
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

Reproduce from the repository with the pinned installed CLI bin directory and isolated Docker:

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
