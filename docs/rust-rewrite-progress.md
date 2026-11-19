# Rust rewrite progress

This record tracks local evidence for phases 1 and 2. The committed plans in
`docs/proposals/architecture/` remain the acceptance criteria.

## Release packaging

C1 combines the phase 1 foundation with Rust ingress and the Python bridge, including
all accepted corrective changes. It retains the Python webhook and delivery recovery
for the two-release cutover. C2 removes those legacy paths after the rollout prerequisites
in [the platform guide](platform.md) are satisfied.

This C2 release retires the legacy paths. The sections below retain historical
implementation-test evidence and the corrected release verification. Both release
trees are verified separately before signing. No push, deployment or live
GitHub write is part of this packaging operation.

## Checkpoints

| Checkpoint | State | Evidence |
| ---- | ---- | ---- |
| A: contracts and database foundation | Accepted by coordinator | Checked Rust/Python decoders, generated schema compatibility, migration isolation, typed outbox admission |
| B: phase 1 complete | Accepted by coordinator after final bounded verification | JetStream, Rust and Python consumers, operator command, queue spike, and recovery tests; full all-features suite 40/40 |
| C1: ingress and service bridge | Accepted; corrected release includes phase 1 | [20-case local evidence](rust-rewrite-c1-evidence.md); R1–R3 closed in independent source review |
| C2: legacy retirement | Accepted by Opus and coordinator after final verification | Python webhook intake, App delivery scan and overlap mode removed; private App deputy, list maintenance and bus coverage retained |

## Environment

- Rust 1.100.0 and cargo-nextest 0.9.145 are installed.
- Docker Desktop is reachable through the approved local test escalation.
- Release verification runs container tests sequentially. A two-test parallel run
  hit a NATS container startup timeout before application code; its log is retained.

## Phase 1 exit cases

| Case | Test | Result |
| ---- | ---- | ---- |
| 1–2 | `state_and_outbox_commit_together`, `transport_consumer_applies_once` | Rust and Python consumers apply the same fixture once; rollback publishes nothing |
| 3–4 | `stopped_after_publish_republishes_with_same_id`, `transport_consumer_applies_once` | Stream duplicate window and expiry behavior pass; both consumers deduplicate an outside-window copy |
| 5–6 | `transport_consumer_applies_once`, `rust_transport_retry_parks_at_limit`, `python_transport_failure_matrix`, `malformed_without_header_parks_both_consumers_once`, `nul_escape_uses_authoritative_wire_bytes`, consumer unit tests | Commit-before-ack faults, retries, park limits, and crashed-handler attempts pass; headerless malformed delivery parks once in each consumer, and escaped NUL bytes survive hold, release, retry, and park |
| 7 | `prerequisite_commits_during_hold_decision`, `prerequisite_chain_outlives_attempt_limit`, `hold_confirmation_beats_duplicate_parking`, transport fault and Python matrix tests | Rust hold/release races and after-hold-before-ack pass; Python transport hold/release passes |
| 8 | `versions_of_one_event_publish_as_distinct_messages`, `versioned_messages_reach_both_consumers` | v1 and v2 with one ID survive the duplicate window and reach both consumers; each applies v1 once, skips v2, and parks nothing |
| 9 | `thousand_concurrent_events_apply_once` | 1,000 concurrent producer commits apply once in both consumers |
| 10 | `slow_success_beats_attempt_limit`, `operator_replay_and_discard`, `hold_confirmation_beats_duplicate_parking`, `parked_replay_serializes_with_delivery` | Slow success and hold beat parking at `max_attempts=1`; replay and delivery are observed waiting on the same event lock |
| 11 | `service_database_roles_are_isolated` | The committed setup script runs; a service role is refused by a peer database |

## Phase 2 exit cases

| Case | Test | Result |
| ---- | ---- | ---- |
| 1–20 | [Local C1 evidence](rust-rewrite-c1-evidence.md) | Direct or composed local assertions; production corpus and cutover remain pending |

## Phase 2 C1 local checks

- Independent source review: R1–R3 closed; no new source findings. Coordinator
  acceptance passed two final live Python tests and three Rust recovery regressions.
- `cargo nextest run --workspace --all-features --locked -j 2`: 64 passed.
  The later replay-horizon, gap-barrier and receipt-lock edits passed focused nextest runs.
- Python contracts: 20 passed. Python service: 771 passed across 57 isolated test files,
  including signed ingress, PostgreSQL/NATS outages, remap, replay and lost-ack recovery.
- `cargo clippy --workspace --all-targets --all-features --locked -- -D warnings`,
  Rust formatting, Ruff check/format and `git diff --check`: pass.

## Phase 2 C2 local checks

- Independent Opus verification closed all four findings with no new regressions.
- Coordinator final acceptance: 177 tests passed across scope/deputy, settings,
  list index, credential recovery, private HTTP, live bus, deployment and backoffice.

- Python service: 740 passed across 57 isolated test files, including live NATS and
  Rust ingress integration. Python contracts: 20 passed; deployment: 23 passed;
  backoffice/list integration: 33 passed.
- Ruff check and format, plus `git diff --check`: pass. Rust source is unchanged.
- After coordinator review, independent list maintenance and the C1-to-C2 settings
  transition were added. Focused reruns: list index 53, daemon maintenance 4,
  settings 10 and live bus integration 9 passed; the full 740-case suite above
  predates these focused changes.
- After independent review, active App/installation/target scope bootstrap was restored
  on the maintenance path, recording completion after strict reconciliation and list
  rebaseline. Scope A→B→A, aged coverage, real credential feed, and removed-target
  tests pass: daemon/deputy 5, list index 55, credential recovery 30, idle traffic 2,
  delegated commands 13. The full suite was not repeated after these focused changes.
- Release 1 at the C1 SHA, real delivery corpus parity, edge switch, initial strict
  gap reconciliation and duplicate GUID check remain operational prerequisites;
  none has been performed by this patch run. No deployment or live GitHub write.

## Corrective pass final checks

The frozen executable-source tree has identity
`8506aba7945239aa5422cbfd134c327001e70e6ce59945c07bce0fd1c3363c5c`.
The contract generators reproduced the same 849-file identity. This section was
added after the suites as documentation-only evidence.

- Rust: `cargo nextest run --workspace --all-features --locked -j 2` passed
  74/74; the separate `concurro-events-nats` fault-injection suite passed 13/13.
- Python per-file CI suites: engine 1,271, service 748, gateway 187,
  backoffice 172, deploy 26; 2,404 run across 190 files, with 2,403 passing.
  One engine test requires Linux orphan adoption and was skipped on macOS.
  The skipped test passed separately in a Linux container with a read-only
  source mount. Python contracts: 27 passed.
- Rust formatting and all-targets/all-features Clippy, Ruff check and format,
  Pyright, `compileall`, ShellCheck, charter bundle verification, and
  `git diff --check` passed.
- Schema compatibility passed against current HEAD's 26 committed schemas.
  The main merge base has no schema baseline; checker fixture tests cover its
  compatibility rules. Rust and Python contract generation caused no drift.
- The 1,000-event cross-language probe passed under concurrent service-test
  load with 1,287 deliveries and 1,000 distinct effects. Its earlier
  delivery-count failure is preserved in the local diagnostic logs.

The original three-commit chain is superseded by the corrected C1 and C2
release pair. Production prerequisites remain separate from local acceptance. Real GitHub delivery parity,
the controlled rollout timeline, edge switch, strict gap reconciliation,
duplicate GUID check, and deployment-only checks remain pending.

## Narrow parking and webhook follow-up

Opus 5.5 High and the coordinator accepted the six-file follow-up delta without
findings. The tested tree has identity
`b7ed68b92e22b4e9fafe28a877a683606809c187202139d0c3a0d9c9d6c9f929`;
this evidence paragraph was added after the tests.

- Rust and Python upgrade regressions now show an owned old-version retry
  parking after a decoder upgrade. A readable newer copy with the same event ID
  skips before, during and after operator replay and re-parking; the old bytes
  and version reason remain visible, and its handler never runs.
- Webhook ingress waits at most 500 ms for one of eight shared permits and
  returns `503` without reading a queued body when saturated. The five-second
  body-read deadline and already-admitted handlers remain independent. The
  concurrency test covers saturation, permit recovery and a later success.
- Final-tree reruns: ingress 6/6, Postgres consumer 20/20, Python contracts
  27/27, and live NATS service bus 9/9; no skips or failures. Rust formatting,
  scoped all-targets/all-features Clippy, Ruff check and `git diff --check`
  passed. The earlier full Rust/Python matrices above were not repeated after
  this narrow change.

## Commands and results

- `cargo --version`: 1.98.0 using the default toolchain before workspace pinning;
  `rust-toolchain.toml` pins 1.100.0 for this workspace.
- `cargo nextest --version`: 0.9.145.
- `docker info --format '{{.ServerVersion}}'`: permission denied at Docker socket
  under the default sandbox; local test escalation reaches Docker Desktop 29.9.3.
- `cargo check --workspace --all-targets --locked`: pass.
- `cargo fmt --all --check`: pass.
- `cargo clippy --workspace --all-targets --all-features -- -D warnings`: pass.
- `cargo nextest run --workspace --all-features` with local Docker escalation:
  40 passed, 0 skipped. Includes named fault injection, the final bounded
  regressions, and 1,000 concurrent events applied by Rust and Python consumers
  against Postgres 18.7 and NATS 2.15.3.
- `cargo nextest run -p concurro-events-nats --features fault-injection --locked`
  with local Docker escalation: 10 passed, including publish/mark, all three
  consumer acknowledgement fault points, and Rust/Python transport failure paths.
- `PYTHONPATH=contracts/python contracts/python/.venv/bin/python -m unittest discover -s contracts/python/tests -q`:
  16 passed, including strict fixtures, nanosecond timestamps, cross-process
  serialization, and Python consumer recovery rules.
- `python3 contracts/check_compat.py b192a5dfc8fc82335b0ac889f23a08cd96039eca`:
  pass; no schema existed at the base, so the checker fixture tests provide its
  current behavior evidence.
- `ruff check .` and `ruff format --check .`: pass.
- `git diff --check`: pass.

## Checkpoint A snapshot

The foundation is the Cargo workspace, `contracts/`, the `contracts`, `db`,
`config`, `telemetry`, `events`, `events-postgres`, and `testkit` crates, and the Rust
and contract CI jobs. The shared `UnitOfWork` owns one SQLx transaction; typed storage
operations borrow its connection and cannot commit it. Library migrations use detached
connections, so cancellation cannot return a changed `search_path` to the pool.
Outbox rows preserve the exact admitted wire bytes for the forwarder.

The phase 1 forwarder, NATS transport, consumer state machine, Python reference
consumer, operator command, and local platform development assets are implemented.
The phase 2 Rust ingress and Python service bridge are implemented and locally tested.
C2 legacy retirement is locally accepted. Production rollout remains pending.

## Corrected C1 release verification

- Workspace build, formatting and all-targets/all-features Clippy passed.
- Rust workspace: 74/74; separate event fault-injection suite: 13/13.
- Python: engine 1,271, service 774, gateway 187, backoffice 172, deploy 26;
  2,430 tests total. The one macOS skip passed separately in a Linux container.
- Python contracts: 27/27. Ruff, compilation, ShellCheck, bundle verification and
  schema compatibility against the original C1 passed.
- No push, deployment, real-delivery parity check or live GitHub write was performed.

## Corrected C2 release verification

- Workspace build passed. Rust crates, contracts and Cargo manifests are identical
  to corrected C1, so its 74 workspace and 13 fault-injection passes apply here.
- Fresh Python run: engine 1,271, service 748, gateway 187, backoffice 172, deploy 26;
  2,404 tests total. The one macOS skip passed separately in a Linux container.
- Python contracts: 27/27. Formatting, Ruff, Pyright, compilation, ShellCheck, bundle
  verification and schema compatibility against the original C2 passed.
- Executable source matches the accepted corrective tree; only release documentation
  changed during C2 packaging. Production rollout checks remain pending.
