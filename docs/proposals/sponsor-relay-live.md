# Live subscription directory-summary attempt

The later [five-file Codex compact run](sponsor-relay-codex-compact.md) passed
end to end on 2026-11-17. This page preserves the original 25-file attempts.

**Status:** Claude completed the 25-file model task on 2026-11-17; Codex did not
reach a model response. This is an attempted live qualification, not an accepted
end-to-end pass. The frozen Sponsor and Fable binaries were unchanged.

Future live runs default to a five-file smoke test (45 bytes of file content).
Use `--steps 25` for the sustained test (225 bytes), or `--steps N` for another
size. Each invocation generates its own isolated directory; repository files
are not included. The synthetic sustained cases still default to 25 files.
CLI instructions, tool schemas and conversation history add request context
beyond these small file contents. The results below are from the original
25-file runs, not the smaller smoke test.

The task used 25 local `file-NN.txt` files, each containing `topic-NN`. Both
CLIs used the production agent entry, local HTTP OIDC/runner claim,
agentgateway, Exchange/Vault and the hosted Rust verifier. The Sponsor used
its existing locally stored subscription credentials. Claude selected
`claude-sonnet-4-6` at low effort; Codex selected `gpt-6.1-terra` at low effort.

| Attempt | Provider responses | Outcome |
| ---- | ---- | ---- |
| Claude v1, 500k fixture budget | 15 HTTP 200 | Stopped after ten local budget 429s; preserved as a failed attempt. |
| Claude v2, 1.2m fixture budget | 26 HTTP 200 | Returned all 25 exact summary lines in `steps`; a post-run harness assertion expected editable filenames in `files` and prevented runner complete/end. |
| Codex v1, 1.2m fixture budget | 0 | Seven Gateway 503 attempts; hosted native WebSocket upgrade did not finish and Sponsor reported broken pipe. |

Claude v2 had one hosted native bootstrap and 26 Sponsor-proved requests over
the retained provider connection. Its 26 provider usage records matched
Exchange's raw input/output observations and Gateway's normalized usage per
request: 28 uncached input, 13,726 cache-creation input, 927,974 cache-read
input and 2,793 output tokens. Gateway reported 941,728 total input tokens,
equal to those three input components. These are provider subscription usage
fields, not an invoice or estimate of marginal price.

| Claude v2 latency | Chronological first | Warm median |
| ---- | ---- | ---- |
| Gateway-reported time to first token | 45.112 s | 10.166 s |
| Gateway request completion | 49.016 s | 12.168 s |

The Gateway measures those request-level times; they are not CLI-observed time
to first token. The elapsed interval from the CLI version stamp to the final
stdout write was approximately 359.5 s. Private-hop first-event timings were
only held in the harness process and were lost when its post-run assertion
failed. Exact Claude wire request-body sizes and independent tool-call events
were not retained. The final answer has 25 correct lines and the provider saw
26 requests, but the claimed one-file-per-tool-call behavior is not
independently proved in this live run. Each source file was 9 bytes.

Codex's bridge recorded one non-generating 36,058-byte warmup and six
52,992-byte generated attempts. All seven returned 503 before any provider
usage was observed. The hosted verifier reached one native bootstrap; its
provider WebSocket upgrade failed after the first native request proof. No
model response or usage was observed, so this record cannot establish billing.
The CLI's approximate version-stamp-to-stdout interval was 72.0 s. The
disposable Exchange DB retained an un-ended session and running turn after
failure; all task-owned processes and containers stopped.

No-generation probes using the existing relay token received HTTP 101 from the
real ChatGPT WebSocket endpoint with and without the current beta header, and
with the HTTP upgrade split across a 1 s or 13 s pause. A local native Codex
tool loop with a 1,781-byte dummy bearer also passed after allowing its larger
prefix circuit to finish. These observations rule out simple token expiry,
bearer length, the beta header, and incomplete-header delay as isolated causes.
The provider-facing failure was unresolved at this stage; the later
[compact run](sponsor-relay-codex-compact.md) records the diagnosed fix.

Claude v1 used 531,450 Gateway input tokens, including 495,003 cache reads and
36,430 cache creation tokens, before its test budget blocked further requests.
The corrected v2 model task succeeded, but the harness assertion prevented
HTTP runner complete/end. The local DB therefore retains an un-ended session
and running turn; no task-owned process or container remains. The harness now
checks the plan schema's `steps` field for this task.

Sanitized per-request records, failures, commands, source and binary identities,
and the original synthetic-evidence reference are in
`/Users/cylwit/Code/github.com/concurro/.artifacts/tls13-rust-port/live-cli-v1`.
