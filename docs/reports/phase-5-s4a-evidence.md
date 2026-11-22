# Phase 5 S4a evidence

S4a implements the private exchange operator workflow. This candidate is local,
unstaged and uncommitted; no service or runner was deployed. The preexisting edit to
`docs/proposals/architecture/phase-5/s4-exchange.md` is the approved work order and
was preserved; the S4b retirement acceptance line was added during review correction.

## Delivered

| S4a requirement | Evidence |
| ---- | ---- |
| Shared wire families | `concurro-contracts` defines operator, admission, runner v2 and event types. Generated JSON Schemas, Python models and representative fixtures cover the HTTP handoff and `exchange.turn.status`, `exchange.turn.finished`, `inference.usage`. The operator grant is the documented exchange projection, with numeric payer ID and exact purpose/custody values. |
| HTTP clients | Operator submit, observe, cancel and revoke, plus admission request, probe, finish and session status, round-trip fixture servers. `submit_bytes` preserves the exact canonical body supplied by the task application. |
| Durable operator flow | The real Axum API uses a private bearer, PostgreSQL, `concurro-db` unit of work and the shared transactional outbox. Grant IDs are immutable; a replay returns the original observation; changed canonical bodies conflict. A session dispatch is allocated for the first queued turn, and FIFO advances after cancellation. |
| Retirement and revocation | Cancel-before-submit, revoke-before-submit, stable tombstone replay, complete retired-session sets and terminal results survive pruning and restart. Revocation fences org and contributed turns. Pending relay release returns `502 release_pending`; it is never reported as acknowledged. |
| Events | State transitions append status snapshots in the same database transaction. The binary forwards committed exchange events to the `EXCHANGE` stream and prunes terminal live rows after one day. |

## Verification

| Command | Result |
| ---- | ---- |
| `cargo run -p concurro-contracts --bin generate --offline` | Passed; regenerated contract registry and schemas. |
| `bash contracts/python/generate.sh` | Passed; regenerated Python models. |
| `PYTHONPATH=contracts/python contracts/python/.venv/bin/python -m unittest contracts.python.tests.test_exchange_api contracts.python.tests.test_fixtures` | 6 passed. |
| `cargo nextest run -p concurro-exchange -p concurro-exchange-client -p concurro-contracts --offline` | 13 passed with disposable PostgreSQL and real HTTP router; run `5190b592-f87c-4128-a9f3-57eac5bfbd93`. Docker socket required an escalated local run after the initial sandbox refusal. |
| `cargo fmt --all --check` | Passed. |
| `cargo check -p concurro-exchange --locked` | Passed after final binary wiring. |
| `cargo clippy -p concurro-contracts -p concurro-exchange-client -p concurro-exchange-postgres -p concurro-exchange --all-targets --offline` | Passed; only existing dependency warnings in `concurro-jsonb` and `concurro-tasks-core`. Log: `/tmp/concurro-s4a-clippy-final.log`. |
| `cargo clippy -p concurro-exchange --all-targets --offline` | Passed after final pruner wiring; same existing dependency warnings. Log: `/tmp/concurro-s4a-clippy-binary.log`. |
| `git diff --check` | Passed. |

All 115 new handwritten Rust/Python definitions were passed together to Lodestone
`similar_code_search` after semantic reuse searches. The complete input is
`/tmp/concurro-s4a-definitions.json`. Existing `concurro-db`, shared event outbox,
and `concurro-tasks-core::python_digest` were reused; no conflicting duplicate API
was found. Generated Python classes were excluded from the handwritten-definition
list.

## Remaining S4 work

S4b supplies the OIDC runner service, key handshake, blobs and real execution. S4c
supplies the admission server, request accounting and settlement. S4d supplies relay
claim/release delivery and contributed funding integration. The current operator API
does not claim a pending relay verifier release is durable. No S7 traffic cutover is
included here.

## Consolidated review correction

The operator now keeps one live session per staffing group, promotes the oldest
eligible turn on submit, cancel or revoke, and never reuses a session across grants.
An expired unbound assignment is replaced before a newer submit can take its place.
The staffing digest uses raw submitted claims and binding while normalizing lease to
the Python float representation; explicit optional `null` is preserved. Grant IDs
are encoded as one URL path segment, and CI checks the generated API registry for
drift. The S4b exit criterion now covers late retirement of a reused session.

Focused correction proof: `cargo nextest run -p concurro-exchange --test operator
--offline` passed 8/8 with disposable PostgreSQL (run
`f0d96cf6-5a3d-48eb-b74a-1f89d24554f2`). After adding the revoke-promotion
assertion, its focused nextest selection passed 1/1 (run
`68320e4a-83b9-4d58-bff2-d8c62c8fee59`). `cargo check -p concurro-exchange
-p concurro-exchange-postgres -p concurro-exchange-client --offline` and
`cargo fmt --all --check` passed. The updated 115-definition Lodestone call found
no competing implementation. `cargo clippy -p concurro-exchange --all-targets
--offline` passed after the correction, with only prior dependency warnings (log:
`/tmp/concurro-s4a-clippy-correction.log`). `git diff --check` passed.
