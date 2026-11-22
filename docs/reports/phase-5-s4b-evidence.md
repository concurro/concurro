# Phase 5 S4b evidence

S4b is a local, unstaged and uncommitted candidate on approved S3 commit
`13150eecd39d88ff823e44b75c0301b67e241462` (which follows signed S4a).
Nothing was deployed or sent to GitHub.

## Delivered

| Requirement | Evidence |
| ---- | ---- |
| OIDC claim and key | The runner listener verifies GitHub RS256/JWKS, issuer, audience, time, repository, owner, workflow, actor, run ref, environment and attempt. PostgreSQL stores used JTI values and first-run binding. Runner and session keys derive from `concurro-secrets`; only hashes are stored. A new JTI from the same run returns the same keys after a store/server restart. |
| Runner lifecycle | Authenticated heartbeat, bounded next poll, result replay/conflict, cancellation, end and stale-session reconciliation use the real router and PostgreSQL. An unbound session is replaced; a stale running turn becomes lost. Cancelling historical T1 leaves a later active T2 on its reused session. |
| Artifacts | The runner uploads digest-checked, create-only objects and bounded result JSON. PostgreSQL records per-session upload links, so separate sessions can reference identical bytes. Downloads require a turn reference. |
| Launcher and engine | The workflow claims once with OIDC before fetching its engine commit and hands the claim to `engine/runner.py`. The engine uses v2 heartbeat, poll, blobs, result and end while retaining its native turn behavior and bounded acknowledgement retries. |
| Events | Each runner transition commits its status snapshot with the state change. Settled `exchange.turn.finished` facts wait for S4c request accounting; S4b does not emit a zero-usage placeholder. |

## Verification

| Command | Result |
| ---- | ---- |
| `cargo nextest run -p concurro-exchange -p concurro-exchange-postgres --locked --offline` | 14/14 passed after S3 integration, run `02a751c1-f452-4296-aa30-676e84088b46`; includes S3's accounts metadata grant test, disposable PostgreSQL, signed fake GitHub OIDC, real HTTP router and Python engine with fake inference. |
| `cargo nextest run -p concurro-exchange --test runner -E 'test(python_engine_completes_canned_turn_with_fake_inference)' --offline` | 1/1 passed, run `57793f9f-ea9a-4db3-9ec8-57801c03eb86`, after adding retained input download/unpack and output upload to the real engine case. |
| `PYTHONPATH=engine:engine/tests contracts/python/.venv/bin/python -m unittest engine.tests.test_turns engine.tests.test_runner_termination` | 91/91 passed; `/tmp/concurro-s4b-engine-tests-final.log`. |
| `PYTHONPATH=engine:engine/tests:gateway/tests contracts/python/.venv/bin/python -m unittest engine.tests.test_launcher` | 4/4 passed; `/tmp/concurro-s4b-launcher.log`. |
| `PYTHONPATH=engine:engine/tests contracts/python/.venv/bin/python -m unittest engine.tests.test_turns.RunnerLoopTests` | 4/4 passed after the five-attempt regression was added. |
| `cargo clippy -p concurro-exchange -p concurro-exchange-postgres --all-targets --offline` | Passed; only preexisting warnings in `concurro-jsonb` and `concurro-tasks-core`. |
| `cargo fmt --all --check`; `git diff --check` | Passed. |

All 81 new or changed handwritten definitions were passed together to Lodestone
`similar_code_search`; the exact input is `/tmp/concurro-s4b-definitions.json`.
Existing S0 OIDC and Python exchange behavior, the shared unit of work/outbox, task
archive paths and engine turn helpers informed the implementation. The new storage
rows follow the existing `FromRow` pattern. No reusable equivalent was displaced.

## Remaining scope

The inference fixture accepts the derived key but does not perform exchange
admission or report usage. S4c must implement the real gateway admission and
settlement path, then publish one `exchange.turn.finished` fact with settled usage.
The artifact test uses an in-memory object store; S7 must wire and validate its
configured S3 bucket and public runner route. S4d retains relay claims/releases
and contributed funding. No live OAuth, provider call or release dispatch occurred.
S4c integration must also exercise S3's `run_producer` and `run_subscription`
against real NATS.

## Approved S3 integration

Before fast-forwarding `main`, S4b's tracked patch and six untracked files were
backed up separately under `/tmp/concurro-s4b-before-s3*` and saved in a local
stash. Main then fast-forwarded from S4a to S3. The S4b stash was **applied**, not
dropped. All 11 tracked files outside the expected Cargo overlaps and all six
untracked files match the backup byte for byte. The merged manifest retains S3's
`concurro-vault-v1` development dependency and S4b's runner dependencies. The
merged lockfile passes `cargo check --locked --offline -p concurro-exchange` and
the 14-test suite above. No conflict markers or unmerged paths remain; all S4b
files were returned to the unstaged working tree. The stash and backups remain
available until acceptance.
