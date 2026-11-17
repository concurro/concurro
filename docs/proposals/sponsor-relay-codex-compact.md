# Codex compact continuations over TLS-MPC

**Status (2026-11-17):** accepted after source and evidence review for the
WebSocket fix and five-file compact-continuation test, within the limits below.
The changes are undeployed. The frozen Sponsor and Fable baselines were not
changed.

The five-file live task completed through the production agent entry, local
OIDC/runner claim, agentgateway, Exchange/Vault, hosted verifier and native
TLS-MPC. Codex `gpt-6.1-terra` returned the five expected ordered summary
steps. All seven provider requests returned Gateway HTTP 200: one
non-generating preflight, one initial generated request and five compact
tool-output continuations. The initial WebSocket upgrade returned HTTP 101;
the Concurro and Sponsor logs recorded one prepared prefix bootstrap each and
13 successful request proofs each. Exchange recorded seven requests and seven
observations, an ended session and a `done` turn. The runner released its
lease without lifecycle errors.

| Measurement | Initial generated request | Warm continuation median |
| ---- | ----: | ----: |
| Provider-bound body | 66,718 B | 3,696 B |
| Private-hop first event | 7.568 s | 0.637 s |
| Private-hop completion | 10.429 s | 2.074 s |
| Gateway duration | 10.445 s | 2.086 s |

The warm body was 94.46% smaller. The five continuations each carried a
`custom_tool_call_output` input. The full CLI task took 41.208 s. These are
single-run measurements, not a controlled speedup claim. The 13 Concurro
request-proof calls totalled 6.176 s, with a 374 ms median. Prefix bootstrap
metrics and per-call timings are preserved separately in the evidence.

Exchange raw usage and Gateway normalized usage matched on every request:
77,677 input and 347 output tokens in total. Gateway cache-read input was
63,872 tokens, already included in the input total. This record establishes
usage consistency, not billing savings. The bridge retained hashes of five
requested `previous_response_id` values. The native response handler checked
parent, response identity and event sequence on each successful turn, but
completed response IDs were not persisted, so cross-turn parent equality
cannot be independently replayed from the package. The final answer proves
five correct summaries; the tool-result IDs were empty in the retained
records, so one read per file is not independently established.

The [sanitized evidence manifest](../../../.artifacts/tls13-rust-port/codex-live-v20/manifest.json)
hashes request sizes and timings, usage joins, proof and bootstrap records,
closure state, source snapshots, exact candidate binaries and validation
logs. Raw input hashes link to the original private run without copying
credentials or prompt text. `cargo check`, `native.test_package`, 20/20
`nextest` tests and the checked-in prepared-helper Linux target build passed.
An initial sandboxed `nextest` run failed because loopback sockets were
denied; its log is retained, and the rerun passed with local socket access.

The live v20 packages were assembled manually from the candidate helper
source. The checked-in prepared-helper patch target was compiled separately.
A full cold source-to-image rebuild and macOS packager assembly have not run.
The harness stopped native sessions during teardown, so its ended Exchange
session and released lease do not establish a clean native TLS close.
