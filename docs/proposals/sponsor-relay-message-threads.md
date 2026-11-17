# Claude Message Threads over TLS-MPC

**Status (2026-11-17):** accepted after source and evidence review, including
compact mode as the default. The changes are undeployed. The frozen Sponsor
and Fable packages were not changed.

The native Claude adapter can prove a full `thread=create` request, then prove
only new CLI messages with `thread=continue` and the preceding authenticated
response ID. It replays the complete history as a new thread after a provider
`404 thread_not_found`. Compact mode is the default; set
`CONCURRO_CLAUDE_MESSAGE_THREADS=0` for full-context diagnostics.
The hosted fixture accepts `--claude-full-context` for the same opt-out.

## Matching rule

Continuation requires stable configuration, exact historical messages and an
exact authenticated assistant response, including block order, text,
thinking/signatures, tool IDs and tool input. The matcher ignores only
message/content-block cache metadata, Claude Code's observed cached-system
text representation, and `caller: {"type":"direct"}` on a direct tool-use
response block that Claude Code omits when resending it. Opaque tool input is
compared and replayed without recursive normalization. Programmatic callers
and substantive changes select a full create. Anthropic documents the
[Message Threads beta](https://platform.claude.com/docs/en/build-with-claude/message-threads)
and the [direct caller response field](https://platform.claude.com/docs/en/agents-and-tools/tool-use/programmatic-tool-calling).

The recursive-normalization defect found during acceptance review was fixed
after the live runs. Six focused Rust tests pass, including changes to
`cache_control`, `caller`, `role` and `content` inside tool input, replay
preservation, cached-system text and direct/programmatic callers. The
corrected source is in the [v4 source snapshot](../../../.artifacts/tls13-rust-port/message-threads-live-v4/source-snapshot.tar),
with the [test log](../../../.artifacts/tls13-rust-port/message-threads-live-v4/focused-nextest.log).
The live image predates this narrow matcher fix; no new provider call was made
after it.

## Validation

The five-file live task used the production agent entry, local OIDC/runner
claim, agentgateway, Exchange/Vault, hosted verifier, native TLS-MPC and frozen
Sponsor, with Claude Sonnet 4.6 at low effort. Each file held one nine-byte
topic line. Both passing five-file live runs had five ordered `Read` calls,
exact five summary steps and HTTP complete/end.

| Case | Provider attempts | Outcome |
| ---- | ---- | ---- |
| Synthetic full | 6 full, all 200 | Five-file task passed. |
| Synthetic compact | 1 create, 5 continue, all 200 | Five-file task passed. |
| Synthetic expiry | Create, continue 404, replay 200, continue 200 | Two-file task passed; the 404 stayed internal. |
| Live full, first run | 6 full, all 200 | Five-file task passed. |
| Live compact, first run | 7 creates, all 200 | Failed final steps; no continuation. Preserved. |
| Live compact, corrected run | 1 create, 5 continue, all 200 | Five-file task passed; every parent matched the preceding completed response ID. |
| Live full, warmed rerun | 7 full, all 200 | Five `Read` calls but empty final steps; failed assertion. Preserved. |

Each live run used one native session, one Sponsor session, one handshake and
one prefix bootstrap. The failed compact run exposed direct `caller` metadata
in the provider response that Claude Code omitted from its next request. A
one-file diagnostic recorded only the response block shape, not model text.

## Measured boundaries

Warm medians exclude the first provider request. Native first DATA is measured
from native request start through the first provider SSE data. Native
completion includes proof and provider response. Private-hop first event and
completion are measured at the local forwarder around agentgateway; Gateway
duration comes from its own request log. These are single-run measurements.

| Live case | Provider body | Proof call | Native first DATA | Native complete | Private-hop first event | Private-hop complete | Gateway duration |
| ---- | ----: | ----: | ----: | ----: | ----: | ----: | ----: |
| Full, passing | 134,922 B | 10.962 s | 9.923 s | 11.967 s | 9.927 s | 12.044 s | 12.057 s |
| Full, warmed final image | 135,159 B | 10.852 s | 9.938 s | 11.846 s | 9.942 s | 11.919 s | 11.929 s |
| Compact, passing | 764 B | 2.578 s | 1.693 s | 3.590 s | 1.697 s | 3.617 s | 3.625 s |

The passing compact run's warm provider-bound body was 99.43% smaller and its
proof call 76.48% shorter than the passing full run. Warm proof record counts
were 8 versus 18. The synthetic paired runs had 801 versus 84,555 B warm
bodies and 1.313 versus 6.406 s warm proof calls. The passing compact task
took 51.65 s end to end; the passing full control took 221.52 s including a
cold public-circuit build, so those wall times do not measure speedup. The
warmed full rerun took 111.32 s but failed the final-answer assertion.

All live provider usage records matched Exchange's raw input/output fields and
Gateway's normalized usage: Gateway input equals raw input plus cache creation
plus cache read, and output equals raw output. Across the passing full run,
raw input/cache creation/cache read/output were 8/34,553/170,711/783 tokens;
the passing compact run recorded 8/10,275/194,984/778. The first request's
cache state differed, while warm cache reads were similar. No billing savings
are established. Bootstrap handoff logs record traffic, but per-request proof
traffic bytes were not captured; proof record counts are not byte counts.

## Evidence

The durable [v4 evidence package](../../../.artifacts/tls13-rust-port/message-threads-live-v4/manifest.json)
contains sanitized per-attempt native journals, private-hop timings, joined
provider/Gateway/Exchange usage, bootstrap counts and recorded bootstrap
traffic, derived metrics, source and live-binary provenance, and test output.
The manifest hashes each file. The previous [v3 report](../../../.artifacts/tls13-rust-port/message-threads-live-v3/report.md)
and all original failed run directories remain intact. The v4 source snapshot
includes the post-review matcher fix; the live v6 image and binaries recorded
in provenance were built before that fix. No live result is claimed for the
post-review source.
