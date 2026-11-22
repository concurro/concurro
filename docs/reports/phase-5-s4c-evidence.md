# Phase 5 S4b/S4c integration evidence

Base: `13150eecd39d88ff823e44b75c0301b67e241462` (approved S3). The 19-path
S4b baseline remains staged; `git diff --cached --binary` has SHA-256
`bca056168352821cae4c4e54c55bdb2b3082a0c221e6c0f5ed7300355ec63c73`,
identical to `/tmp/concurro-s4b-staged-on-s3.patch`. S4c and all consolidated
review corrections are unstaged. No commit,
push, deployment, live provider call or GitHub write was made.

## Working path

| Area | Evidence |
| ---- | ---- |
| Exchange | The private admission router and PostgreSQL request ledger validate the session key, grant, model and spent budget. Finishes append one `inference.usage`, set the first trusted provider failure, and append `exchange.turn.finished` with its terminal observation after all requests settle or reach the five-minute deadline. Repeated finishes and late reports preserve the first result. Historical session refusals remain visible after replacement. |
| Gateway | Org route comes from the frozen grant. Server credentials override caller authority while a denylist retains native request and provider response metadata. JSON and SSE are parsed incrementally with an 8 MiB per-event bound; structured failures are sanitized. A request-wide monitor closes stalled headers, streams and backpressure after revocation, deadline or a prolonged exchange outage. Transport uncertainty records possible dispatch. Finish reports retry for up to five minutes. |
| Task application | Rust mode verifies the exact retained object before first send or replay, freezes the v2 org body and route, then uses the existing effect, runner dispatch, settlement, archive and read-model paths. Status revisions fence late snapshots. A terminal fact applies after the operator row is pruned; the five-minute operator readback remains for lost facts while live rows exist. Deferred successful stages retain provider request evidence and usage. Org route and usage storage also cover standalone reviewer grants. |
| Engine runner | Idempotent v2 blob downloads/uploads retry without native re-execution. Exhausted transfers stop the session with an unacknowledged receipt and keep the turn directory; they do not fabricate a native FAILED result. |
| Real runner | The integration harness uses a disposable PostgreSQL database, real operator/admission/runner/gateway HTTP routers, fake GitHub OIDC/JWKS and provider, and the real Python v2 engine runner. A canned intake turn advances the task to Plan, records one request's tokens, and indexes its transcript and retained archive. A verified workspace blob is projected into publication input; the canned intake turn itself does not produce a workspace. |
| Transport | `run_producer` and `run_subscription` delivered a committed event through disposable JetStream and PostgreSQL, with a durable consumer effect. The integrated task test applies the exchange fact handler directly to isolate result and reconciliation behavior. |

## Pre-review checks

| Command | Result |
| ---- | ---- |
| `cargo nextest run -p concurro-exchange -p concurro-exchange-postgres -p concurro-inference-gateway -p concurro-tasks-postgres -p concurro-tasks -p concurro-events-nats --offline --no-fail-fast` | 117/117 passed; one existing runtime restart test took 148 seconds. Log: `/tmp/concurro-s4c-nextest-final.log`. |
| `cargo nextest run -p concurro-tasks --lib rust_exchange::tests::org_task_submit_and_terminal_fact_use_existing_read_model --offline` | 1/1 passed after the final missing-object, retained/archive, replay, standalone-route and Rust-only readback assertions. Log: `/tmp/concurro-s4c-org-final-5.log`. |
| `cargo nextest run -p concurro-inference-gateway --test org --offline` | 1/1 passed after the OAuth route assertions. Log: `/tmp/concurro-s4c-gateway-final.log`. |
| `cargo nextest run -p concurro-exchange-client --test fixtures --offline` | 2/2 passed, including the terminal operator observation fixture. Log: `/tmp/concurro-s4c-fixtures.log`. |
| `PYTHONPATH=engine:engine/tests:gateway/tests uv run --no-project --with PyYAML==6.0.3 --python 3.15 python -m unittest -q engine.tests.test_launcher engine.tests.test_turns` | 50/50 passed. Log: `/tmp/concurro-s4c-python-final.log`. |
| `cargo check --workspace --offline`, `cargo fmt --all -- --check`, `git diff --check` | Passed on the first review candidate. Workspace check log: `/tmp/concurro-s4c-workspace-check-final.log`. |
| `cargo clippy -p concurro-exchange -p concurro-inference-gateway -p concurro-tasks -p concurro-events-nats --all-targets --offline` | Passed with existing workspace warnings. `-D warnings` stops on the pre-existing `concurro-jsonb` range warning. Log: `/tmp/concurro-s4c-clippy-nonfatal.log`. |
| `cargo run -p concurro-contracts --bin generate --offline` | Passed; a repeat left the four changed schema files byte-identical. |

The first broad suite could not compile two Python-mode test initializers after
`ExchangeContext` gained its Rust option; both now set `rust: None`. A malformed
Responses SSE test literal caused the next run to stop at 29/30 completed tests;
the fixture was corrected before the 117/117 run. A plain sandbox run could not
access Docker or bind the Python loopback fixture; the approved local test runs
used the permitted escalated sandbox. No production failure was inferred from
those environment refusals.

The initial 67 S4c definitions and 27 correction definitions were submitted
together to Lodestone `similar_code_search`; the final batch is
`/tmp/concurro-s4bc-corrected-definitions.json`. Existing archive, effect,
outbox, event and provider-observation patterns were reused where they fit.

## Consolidated review correction

The corrected tree also covers a live idle cross-grant session, refusal retention
through unbound replacement, pruning before task fact consumption, deferred
develop-stage usage, Responses metadata and >8 MiB aggregate streaming, HTTP 200
structured failure sanitization, stalled headers and backpressured revocation,
and uncertain post-send transport. The original sandboxed broad attempt failed
to access disposable Docker sockets (`/tmp/concurro-s4bc-corrected-nextest.log`);
the same suite passed with the authorized local test permissions.

| Corrected-tree check | Result |
| ---- | ---- |
| `cargo nextest run -p concurro-exchange -p concurro-exchange-postgres -p concurro-inference-gateway -p concurro-tasks-postgres -p concurro-tasks -p concurro-events-nats -p concurro-exchange-client --offline --no-fail-fast` | 123/123 passed; `/tmp/concurro-s4bc-corrected-nextest-privileged.log`. |
| `cargo nextest run -p concurro-contracts --offline` | 6/6 passed; `/tmp/concurro-s4bc-corrected-contracts-final.log`. |
| `PYTHONPATH=engine:engine/tests:gateway/tests uv run --offline --no-project --with PyYAML==6.0.3 --python 3.15 python -m unittest -q engine.tests.test_launcher engine.tests.test_turns` | 52/52 passed; `/tmp/concurro-s4bc-corrected-python.log`. Strengthened receipt assertions passed in focused rerun `/tmp/concurro-s4bc-corrected-python-focused.log`. |
| `bash contracts/python/generate.sh`; `PYTHONPATH=contracts/python contracts/python/.venv/bin/python -m unittest -q contracts.python.tests.test_exchange_api contracts.python.tests.test_fixtures` | Generated Python bindings for the updated fact and usage schemas; 6/6 passed; `/tmp/concurro-s4bc-corrected-python-contracts.log`. |
| `cargo check --workspace --offline`; `cargo fmt --all -- --check`; `git diff --check` | Passed; check log `/tmp/concurro-s4bc-corrected-workspace-check.log`. |

The final `provider_error` classification order, structured failure assertion,
and stalled-header client disconnect guard passed the focused real-router gateway
regression (1/1, `/tmp/concurro-s4bc-corrected-gateway-disconnect-final.log`).
The initial disconnect assertion counted unrelated unknown requests and failed;
the corrected test binds the exact durable request ID. Workspace check and
format/diff checks passed afterward; last check log:
`/tmp/concurro-s4bc-corrected-workspace-check-final2.log`.

The final CI compatibility check initially rejected the required terminal
`observation` field in the v1 fact. It is now optional on the wire, always
populated by this producer; consumers use operator readback only for old facts
without it. Both old and new fact fixtures round-trip in Rust and Python.
`python3 contracts/check_compat.py HEAD` passed, as did Rust contract tests
(6/6, `/tmp/concurro-s4bc-compat-rust-contracts.log`) and Python contract tests
(6/6, `/tmp/concurro-s4bc-compat-python-contracts.log`). The gateway now keeps
its monitor alive through final buffered JSON handoff; a chunked JSON provider
response passed the focused real-router test (1/1,
`/tmp/concurro-s4bc-compat-gateway.log`). The pruned-fact task regression passed
(1/1, `/tmp/concurro-s4bc-compat-task.log`), followed by workspace check,
format and diff checks (`/tmp/concurro-s4bc-compat-workspace.log`).

## Remaining scope

S4d still owns personal credential versions, relay claims/releases, credential
fact selection and fallback, and full task grant-revocation effects. The gateway
currently serves org HTTP routes; provider token refresh on a 401 and contributed
or relay routes remain in the later gateway work. The full path was proved through
real HTTP routers and the real runner with direct task event application; a
single process-level test spanning the real NATS producer and task consumers was
not added. Publication workspace projection was checked with a verified fixture
blob; a real development turn producing a workspace is not covered by the canned
intake turn. S7 deployment and live qualification remain separate.
