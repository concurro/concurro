# Sustained synthetic CLI qualification

**Status:** local synthetic-provider qualification passed on 2026-11-17. The
signed baselines are Concurro `ee512123d7b2fa615051ef9ebb39c08f8fc1e69e`,
relay `3c580aa73733443729932922c77ac33faa2c616e` and backoffice
`f93a9cb50859d0e5bdad5a76f19ccd1a9955b334`. The frozen Sponsor binary was
unchanged.

One invocation of each pinned CLI read 25 synthetic files using real tools.
Each next synthetic provider response depended on a validated result from the
previous tool call. The production agent entry constructed the CLI command.
A signed local GitHub OIDC endpoint supplied the token to the real runner HTTP
client; Exchange then claimed, served, completed and ended the session over
HTTP. All model requests crossed the pinned agentgateway, Exchange/Vault,
Rust hosted verifier, frozen Sponsor and synthetic TLS provider. Codex retained
the WS→HTTP/SSE→WS bridge.

| Measure | Claude | Codex |
| ---- | ---- | ---- |
| Model requests / tool calls | 26 / 25 | 27 / 25 |
| Warmups / retries | 0 / 0 | 1 / 0 |
| Provider connections / native bootstraps | 1 / 1 | 1 / 1 |
| CLI wall time | 194.8 s | 59.2 s |
| First event, cold request | 30.1 s | 18.4 s |
| First event, warm median | 5.81 s | 1.37 s |
| Gateway completion, median | 6.55 s | 1.46 s |
| Provider request body bytes, total | 2,347,007 | 610,948 |
| Synthetic input / output tokens | 52 / 78 | 54 / 81 |

"First event" starts when Exchange begins forwarding the private hop and
stops when it writes the first SSE event toward agentgateway. It is not the
CLI-observed time to first token. The cold figures are the chronological first
request; warm medians exclude that request. Gateway completion is from the
agentgateway duration log.

All 53 provider usage records matched their agentgateway and Exchange records
per request. Each route resolved the exact Vault binding twice per request:
52 Claude and 54 Codex resolution calls. Claude used H2 streams 1–51 in order.
Codex recorded 27 bridge turns, including its detached warmup and 25 response
continuations. Both CLIs returned 25 correct one-line summaries; Exchange
leases and task-owned processes/containers were cleaned up. At teardown, both
Sponsor logs recorded a terminal peer EOF without TLS `close_notify`, followed
by link reconnect. No inference request failed or retried, and no subsequent
model request used the reconnected link.

The first Codex pilot failed before provider bind because its Python fixture
environment lacked PyYAML. A task-local provider wrapper supplied the pinned
dependencies; the two-file retry and 25-file run passed. The earlier Docker
wrapper left one hosted test container after the Claude pilot. A task-local
wrapper cleaned up subsequent runs. The existing one-tool Codex fixture also
passed after the test instrumentation change. Failed attempts and their logs
are retained in the local evidence package.

The complete per-request timings, input sizes, usage joins, commands, source
and binary identities, bridge records and failed attempts are under
`/Users/cylwit/Code/github.com/concurro/.artifacts/tls13-rust-port/sustained-cli-v1`.
The corrected reporting addendum is under
`/Users/cylwit/Code/github.com/concurro/.artifacts/tls13-rust-port/sustained-cli-reporting-v2`;
the original evidence and manifest are preserved.
The figures measure deterministic synthetic responses on a local network with
emulated Linux amd64 native code. Synthetic token counts are fixture values;
they do not estimate live model usage or price. No live provider or WAN profile
was exercised, and GitHub dispatch infrastructure was outside this run. No
direct-provider baseline was measured, so these totals do not isolate
cryptographic overhead.

The local stack is ready for a reviewed live pilot of the same 20–25-turn
directory task. That pilot needs an authorized live provider session, real
model IDs and limits, the selected owner/Vault approval, and a reachable
hosted verifier/CLI route. Publication or deployment of the local image needs
separate authorization if the pilot runs outside this workstation.

The subsequent [live subscription attempt](sponsor-relay-live.md) completed the
Claude model task but exposed a Codex provider WebSocket failure and a
post-run Claude harness assertion. Its evidence is separate from this accepted
synthetic qualification.
