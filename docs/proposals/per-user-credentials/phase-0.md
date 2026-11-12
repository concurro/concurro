# Phase 0: isolated gateway and harness evidence

**2026-11-12 — isolated experiments complete; live-account checks pending.**
Base: `0328670c200339fde8da28edd529de2bfb15ba0e`. This implements the local spike in the
[plan](implementation-plan.md#gateway-selection-and-empirical-phase-0), without changing
production behavior, deploying, reading provider credentials or logging into either CLI.
The user explicitly chose isolated experiments and deferred live-account checks.

## Selected mechanism

Use **native agentgateway virtual-key authentication + HTTP credential resolver + model
backend-auth expressions**, with a **narrow response-observation hop** inside the gateway
trust zone. Dynamic model resources and a full inference proxy are unnecessary for this
measured path. These are test fixtures, not the production vault/resolver integration.

The actual pinned gateway authenticates `concurro.session` metadata, passes that session
to the fake resolver, and injects its token/account into the selected backend. Policy-derived
route selection overrides forged client route headers before model selection. The response
hop observes real fake-upstream JSON/SSE bytes, snapshots the resolver's request/turn identity,
exports allow-listed categories/usage, and replaces credential-bearing error text with a
fixed message. Success SSE remains streaming; native key/model/token-budget checks stay
in agentgateway. Private correlation headers stop at the hop, not the provider.

**Pinned-version finding:** HTTP extAuthz metadata expressions using `response.body`
also register body access on the main provider response. The first candidate buffered
streams and consumed all 2,050 offered chunks despite client cancellation. The pinned
[source](https://github.com/agentgateway/agentgateway/blob/26f5aebe99a36a037fe54f8080a608aca5547e15/crates/agentgateway/src/http/ext_authz.rs#L1076)
notes this response-attribute registration problem. Reading **private resolver response
headers** instead removes the buffering side effect. They are consumed as metadata,
never copied wholesale into requests, responses, logs or the request database.

Native request logs alone distinguish HTTP status and usage but cannot distinguish the
two fixture 429 categories or an HTTP-200 stream error from an incomplete stream. Native
error responses can also expose synthetic provider details. The additional hop proved
structured classification and error sanitization without replacing gateway auth/budgets.
This does not establish that every possible native extension hook is unsuitable.

## What was measured

The [machine-readable evidence](phase-0-evidence.json) records source/config/schema hashes,
actual image identity, per-case request counts, scanner results and pending checks.
Raw logs/configs/transcripts/SQLite snapshots remain in `/private/tmp/concurro-phase0-final07`.
Earlier failed architecture probes have retained config/log/trace hashes in the evidence.
Those probes predate the frozen fixture source; their exact source revision was not saved.
Harmless fixture/setup failures remain in temporary recovery notes, not the result claims.

| Boundary | Result and limit |
| ---- | ---- |
| Actual image | v1.7.0, Linux arm64, digest `sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b`; 15/15 opt-in experiments pass. |
| Isolation | Two accounts per provider, parallel requests, duplicate/mixed-case account/plan/private headers, forged request identity and forbidden models. Body markers independently join each submitted request to the received credential account; no operator-poison backend request. Fake-provider credential inventory is independent of resolver account metadata; mismatched ChatGPT account metadata is rejected by that fake. |
| Denial | Missing/revoked/expired fake records and resolver outage produce no upstream call. Expiry/revocation decisions here belong to the fake vault, not new production code. |
| Native usage/budget | Native logs and the observer retain original request/turn on delayed completion. Three HTTP requests map to one actual Exchange turn and 15 reported fake tokens; engine call charging was not simulated. A four-token native budget permits the five-token first response, then blocks, including after reclaiming the same key. Budget denial is a native 429/log row; it bypasses the observation hop. Its durable attempt mapping remains integration work. |
| Typed observations | Both fake providers: auth, quota, rate with bounded retry-after, unknown 503, HTTP-200 error and truncated stream. Missing terminal usage is unknown, never zero; no raw provider message in exported observations. Codes are fixture taxonomy, not verified subscription-backend taxonomy. |
| Streaming/cancellation | First SSE event reaches the client while upstream completion is still withheld. Disconnect stops a bounded 32-MiB offered stream early; evidence records consumed chunks, exception type and elapsed time. The test does not infer a provider cancellation protocol. Synchronous writes provide backpressure without a full-response buffer. This is a bounded behavior check, not a throughput benchmark. |
| Existing Exchange lifecycle | Real admission/OIDC fixture signing/key minting, retirement, late-result rejection, failed deletion, reopen/reconcile and repeated end. Other-provider key continues. Provider/epoch selection is synthetic fixture association, not implemented sponsorship policy. |
| Existing gaps | Current `Exchange.key`/`next` do not consult vault status; an activation 403 is considered activated by today's `Gateway.activated` check. Resolver denial prevents provider traffic, but scheduling still occurs. Planned per-boundary checks remain phase 3 work. No production guard was added in phase 0. |
| Native harnesses | Workflow-pinned Codex 0.163.0 and Claude Code 2.1.301 use `agent_entry.command_for`, complete structured fresh turns, archive with `turns.pack/unpack`, and resume the same thread from fresh homes under another fake account, both directly and through the observer. History marker is present in resumed requests. This proves local CLI/transport behavior, not real-provider account portability. |
| Secret scans | Both Docker gateway log streams, request-log SQLite/WAL snapshots, native stdout/stderr/homes and sanitized traces are scanned for synthetic provider and session tokens. All experiment files except the intentional key-bearing Exchange DB and gateway config are scanned, including fresh homes/workspaces/temp files. No leaks in selected-path artifacts. A deliberately seeded marker proves the same scanner detects a leak. Scan conditions include `accessLog.remove: [error]`, `logging.level: info,agent_llm=error` and request-log `llm: metadata`; these must survive production integration, with `request_logs > 0` and zero payload rows required. |

## Reproduce

Use an isolated Python environment with the repository requirements, Docker, Node/npm and
the exact workflow CLI pins. The runner creates uniquely named disposable containers,
only loopback-published ephemeral ports and fresh CLI homes. It does not use deployment
Compose, env files or existing CLI login state. Choose a new output directory per run;
failed artifacts are not overwritten.

```bash
python3 -m venv /tmp/concurro-phase0-venv
/tmp/concurro-phase0-venv/bin/python -m pip install -r service/requirements.txt -r gateway/vault/requirements.txt -r backoffice/requirements.txt
npm install --prefix /tmp/concurro-phase0-clients @openai/codex@0.163.0 @anthropic-ai/claude-code@2.1.301
docker pull cr.agentgateway.dev/agentgateway:v1.7.0@sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b
curl --fail --silent --show-error https://raw.githubusercontent.com/agentgateway/agentgateway/v1.7.0/schema/config.json --output /tmp/concurro-phase0-schema.json
/tmp/concurro-phase0-venv/bin/python gateway/tests/sponsorship_spike.py --output /tmp/concurro-phase0-result --clients /tmp/concurro-phase0-clients/node_modules/.bin --schema /tmp/concurro-phase0-schema.json
```

On Docker Desktop, optionally pass `--docker-host unix:///path/to/docker.sock`.
The fixture uses `host.docker.internal` to reach loopback fake servers; native Linux
needs an equivalent host mapping/network arrangement. Linux runner parity was not measured.
The pinned schema SHA-256 is
`8d757c092bc30a5ed5fd38a07a47d72602941c8344203c2d1f86e02d6dbb2bd3`.
Generated configs retain production-equivalent hybrid SQLite storage but use disposable
container `/tmp` files and loopback-published admin ports for the experiment.

Offline regression checks:

```bash
python3 -m unittest discover -s gateway/tests -v
ruff check engine service gateway backoffice
ruff format --check engine service gateway backoffice
pyright --project pyrightconfig.json
```

Observed: 90 gateway tests pass, Ruff/format pass and Pyright reports zero errors/warnings.
The sandbox initially denied existing loopback-server tests; rerunning with local socket
access passed. No production settings, workflow pins or inference routes were changed.

## Remaining work and tradeoffs

- Real subscription tokens, backend authorization/account-header verification, real error
  codes and real account-to-account transcript behavior remain pending by user scope.
  A fake rejecting wrong account metadata does not prove ChatGPT enforces that header.
- The additional hop adds maintenance and latency; latency was not benchmarked. The
  fixture parser inspects bounded JSON/SSE events. Unsupported nonstream JSON returns
  sanitized unknown/502; malformed or oversized SSE remains opaque/unknown, while an
  unterminated final event is dropped. Offline tests cover these paths. It is not a production sanitizer for arbitrary
  malicious/oversized error bodies. Production parser/redaction coverage, service auth,
  durable/deduplicated observation delivery and deployment wiring need implementation.
- Vault expiry, provider-slot ownership and active-turn mapping are synthetic fixture
  inputs. Actual consent, writer checks, immutable payment binding and durable vault events
  remain later phases; the spike does not pretend to implement them.
- The fixture `cancelled` flag means a forwarding exception; its recorded exception type
  distinguishes pipe/reset from timeout and does not classify a provider failure.
- Refresh is not required for token-only delivery and was not implemented or tested.
  Log/DB scanning covers the exercised configurations and synthetic values, not arbitrary
  debug logging or every provider response format.

## Review dispositions

Initial Claude Fable 5.2 medium review returned 14 medium/low findings. The repair pass
keeps the same isolated scope:

| Findings | Disposition |
| ---- | ---- |
| F1, F7 | Independent body markers bind request to account; inspect duplicate header values and resolver IDs, join observer IDs, reject any poison-route call. |
| F2, F3 | Require real request rows and zero payload rows; scan both Docker streams and all fresh CLI files, preserve exactly the scanned log snapshots. |
| F4, F12 | Hash retained negative artifacts with explicit unfrozen-source provenance; runner emits per-case summary, artifact/source hashes, worktree status, exact pin checks and test durations. |
| F5, F13 | Unknown/502 for unsupported JSON, drop uninspected SSE tail; malformed/oversized/truncated offline tests. Opaque SSE sanitization remains explicitly unsupported. |
| F6, F8 | Verify actual gateway key deletion independently of resolver state; run overlapping old/new-turn requests and assert distinct trusted IDs. |
| F9–F11, F14 | Record termination cause, strengthen success/denial and CLI model/effort/beta/account controls, measure activation probe status, name log conditions and document budget-observation limit; add digest image pull. |

The follow-up accepted all 14 dispositions. Four additional low findings were repaired:
preserve failed-case manifests when scans are missing, hash WAL/SHM and native-error
artifacts, wait for nonstream observation append, and bind gate logs to frozen source.

Raw review input/results and immutable reviewed hashes are retained in
`/private/tmp/concurro-phase0-review`. Final source acceptance remains the primary agent's
responsibility; passing fixture checks do not establish real-backend support.
