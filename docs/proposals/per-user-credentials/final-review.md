# Final sponsorship review — 2026-11-13

Phases 0–6 are implemented on signed backend `abd891415407358527734ed84fe91ea840852f61`
and portal `3ad054413292c8e47242e5c5f88c273e530ab89a`. This record covers final review
corrections, not deployment. See [current operation and deployment readiness](deployment-readiness.md)
for the actual configuration, mounts, principal separation and remaining release steps.

## Review and source identity

Fresh Fable `claude-fable-5-2`, effort `medium`, tools disabled and strict MCP configuration,
reviewed complete production Python (122 files), frontend source (89 files), and selected
consent/admission/vault/gateway/timer/CLI/deployment producers and consumers. Follow-ups reused
session `bc43109d-e0ac-45bb-b67e-966f64b515c2`; actual JSON reports `is_error=false` and the
canonical requested model. CLI compaction occurred during the final follow-up.

| Review | Frozen manifest SHA256 | Verdict |
| ---- | ---- | ---- |
| Fresh full-source R1 | `f59d57053012eedf7980e505442ff4c1b71a8d794b80ba6fe373575356aad8f9` | Six supported findings |
| Repair R2 | `8a3b03d09f3a6cfafa9a8107b2126acc2ac5dc5c7341f69f50da67a59d609327` | `no_findings`, `findings=[]` |
| Final R3 | `b49967a987f44da136c3d020dd416c5bc4d2e99c600c00ae7344cd1bcc0492a5` | `no_findings`, `findings=[]` |

The final manifest enumerates all 274 supplied paths:
`/private/tmp/concurro-sponsorship-final-round4/manifest.json`.
The initial full bodies and later definition-level coverage are recorded in
`/private/tmp/concurro-sponsorship-final-reviewe/round2-body-coverage.json`.
Computed remainder comparisons prove the four partially re-supplied production files have
no other changes from R1; every excluded definition is unique in its file. The original
recovery-test imports were supplied in R1 and remain byte-identical. Comparisons are in
`/private/tmp/concurro-final-source-comparison04.json` and `concurro-final-definition-counts05.json`.

Final immutable input: `round3-input.md`, SHA256
`e183b918df24a8c6be5f80e520b87b4c634b9291d1abc458b3005c0cc48c1e51`.
Raw final verdict: `round3-result.json`, SHA256
`56a56a583e9839428313f40b7dc6f6964c53061128eb9dadcc840a948c51fc64`; both live under
`/private/tmp/concurro-sponsorship-final-reviewe/`. Invocation, model usage, all prior raw
results, exact evidence hashes and negative experiments remain there or at their linked paths.
This post-verdict record is separate from the reviewed implementation manifest.

## Resolved findings

| Finding | Result |
| ---- | ---- |
| F1: service overlay hidden by private settings permissions | Helper discovers optional `/etc/concurro/compose.service.host.yaml`; service/web mount retention, protected-parent and absent-override tests pass. |
| F2: orphan request evidence could block recovery forever | Existing retirement transactions and startup settle unobserved ended owners as `native_gap/unknown`; live owners remain pending. Resolver rejection records sanitized unknown, and native-log availability is separate from provider evidence. |
| F3: mandatory metadata bearer omitted from installation | Current guide provisions master key and metadata bearer required by base Compose, including org-only operation. |
| F4: outage falsely implied org funding | Unavailable projection reports unknown funding. |
| F5: live receipts labeled mock | Mock badge uses the existing mode selector. |
| F6: live paused stages absent from attention/filter/action paths | Canonical stages drive parked views and sponsor attention; budget recovery points to writer Retry, credential recovery to replacement. |

Primary acceptance also tightened the real orphan recovery regression: actual Worker cleanup
ends the original owner; writer Retry produces exactly one new charged attempt (`calls=2`,
`served=2`), new turn/session identities and an unchanged first binding. The next trusted
synthetic auth failure pauses normally. A displaced stage-badge doc comment was corrected.

## Verification and limits

Full affected suites passed: service 530, gateway 165, backoffice 47, deploy 16; Ruff,
format, Pyright, compile, ShellCheck and bundle checks passed. Unchanged engine evidence
(1,185 tests, one existing skip) was reused by exact source comparison. Final test-only
tightening passed all 27 recovery tests and Ruff/format. Portal check and live/mock builds
passed; Svelte check reran after the comment-only move. Six loaded light/dark screenshots
and live-mode intercepted assertions cover canonical pauses, parked membership, budget
versus credential actions, unknown funding and live/mock receipts. Build/visual evidence
keeps its original identities; rendered code did not change afterward.

Current-source pinned gateway matrix passed four isolated cases covering all four routes,
spoof isolation, per-request resolution, native budget attribution, revoke/outage refusal and
org behavior. Image: `cr.agentgateway.dev/agentgateway:v1.7.0@sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b`.
Native CLI, streaming, TLS and shared-WAL evidence remains historical with its original
source/config/pin identities. It was not relabeled as a final-source rerun. Current-source
vault startup over a retained dependency image passed with synthetic owner-private keys,
UID 65532, read-only mounts and no network; fresh offline dependency build lacked cached
pip packages. Compose evidence renders synthetic inputs, not private host provisioning.

A missing observation stays pending until durable owner end; unrelated later logs are not
proof that a delayed writer finished. A late native denial can arrive after an unknown gap
and a replacement charge. It retains the original request/turn/payer, can block later
admissions, and never rewrites charges or an accepted result. No timeout or failure category
was invented. Existing full-history reads and polling costs remain unbenchmarked at scale.

Review limits remain explicit: Fable ran no tools or hash computations; the lockfile, three
static mock datasets, historical phase notes/reviews, bundle resources, Pyright configuration
and unprovided test bodies are outside body review. Native gateway internals and published
release state were not source-verified. Live-mode browser requests were intercepted locally.
Real subscription probes/backends, authenticated account-header/expiry semantics, cross-account
portability and provider taxonomy remain user-deferred. Optional refresh is separate.
No live credentials, provider calls, task retries, deployment, push or commit occurred here.
