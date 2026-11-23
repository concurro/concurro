# Phase 5 S4d evidence

Base: signed local `c63e37f5e6babb074949a4847c1dcfddaeead0bd`. All S4d
changes are unstaged and uncommitted. No push, deployment, live credential,
paid provider call or GitHub write occurred.

## Working path

| Area | Evidence |
| ---- | ---- |
| Task selection | The selected accounts metadata entry, exact record/version, custody and relay ID are frozen with the attempt. Rust mode projects them into the immutable v2 grant with the payer's verified login. Python mode retains its original frozen admission shape. Rust mode refuses legacy metadata that has no grant identity before charge. |
| Version and consent fences | Accounts `credential.changed` facts cross disposable JetStream to task storage. A transactional version marker and grant insertion use the same lock, so a retired version cannot be inserted later. Existing retirement effects cover submitted contribution and standalone turns. A withdrawn provider transition waits for Rust exchange revocation and any verifier release acknowledgement before its slot is acknowledged. |
| Personal inference | The gateway reads only the admitted `personal/<record_id>` OpenBao version. A real admission router and PostgreSQL ledger recorded a bad version's exact grant/record/version failure, then a replacement version succeeded against a fake provider. ChatGPT account material is read from the same version. |
| Relay | Exchange signs one Ed25519 JWS per session, retains it and its release state in PostgreSQL, and retries S1 releases from a durable ledger after restart. The gateway forwards the same request ID and claim to S1's hosted API, maps its shared refusal codes, and never retries `request_reused` with a fresh ID. An exchange-produced claim reached an actual S1 hosted verifier; after its release and verifier restart, S1 refused the same claim. |

## Checks

| Command | Result |
| ---- | ---- |
| `cargo nextest run -p concurro-exchange -p concurro-exchange-postgres -p concurro-inference-gateway -p concurro-tasks -p concurro-tasks-postgres --no-fail-fast` | 112/113 passed, including personal OpenBao, relay gateway, NATS credential fact, database marker and slow runtime restart. The one failure was a hand-written Rust conversion fixture still locating S4d fields inside the Python admission object. Corrected fixture then passed alone. Log: `/private/tmp/concurro-s4d-final-nextest-corrected.log`. |
| `cargo nextest run -p concurro-tasks --lib rust_exchange::tests::org_task_submit_and_terminal_fact_use_existing_read_model` | 1/1 passed on the corrected fixture, with real PostgreSQL, operator and retained blob. |
| `cargo nextest run -p concurro-tasks --test initialization --no-fail-fast` | 5/5 passed after gating Rust-only selection fields; includes Python oracle parity, withdrawal and fallback. |
| `cargo nextest run -p concurro-inference-gateway --test personal --test relay` | 2/2 passed on the final gateway tree: real OpenBao personal version replacement and one hosted `request_reused` refusal without a second dispatch. |
| `CONCURRO_S1_HOSTED_BIN=/private/tmp/concurro-s1-target/debug/concurro-relay-hosted cargo nextest run -p concurro-exchange --test operator relay_claim_is_signed_once_and_release_is_durable` | 1/1 passed, with real PostgreSQL, actual S1 process, release and restart. S1 was built from a disposable copy of its current `c1ea48b` checkout using `cargo build --manifest-path /private/tmp/concurro-s1-source/Cargo.toml -p concurro-relay-hosted --bin concurro-relay-hosted`. Its own `release_before_dispatch_is_durable_and_expires` test also passed. |
| `cargo check --workspace --offline`; `cargo clippy -p concurro-exchange -p concurro-exchange-postgres -p concurro-inference-gateway -p concurro-tasks -p concurro-tasks-postgres --all-targets --offline`; `cargo fmt --all -- --check`; `git diff --check` | Passed. Workspace and Clippy logs: `/private/tmp/concurro-s4d-workspace-check-final.log`, `/private/tmp/concurro-s4d-clippy.log`. Clippy retains nonfatal workspace warnings, including a new argument-count warning for relay claim issuance. |

The first broad run stopped at two Python-mode tests because Rust selection fields
changed the strict legacy admission body; its log is
`/private/tmp/concurro-s4d-final-nextest.log`. Both passed after the mode split.
Disposable Docker and loopback tests required the authorized local test sandbox;
the default sandbox refused Docker access. The final 27 new definitions were sent
in one Lodestone `similar_code_search` call; their source batch is
`/private/tmp/concurro-s4d-definitions.json`. The search found existing
transaction, effect, runner and metadata patterns reused by this slice.

## Remaining scope

The corrected local S4d workflow is ready for narrow acceptance. S7 deployment, live
credential configuration, real paid provider qualification and cutover remain
separate. The S1 process test proves exchange claim acceptance and release across
restart without a connected Sponsor; the separate gateway test proves its hosted
dispatch and refusal mapping with a fake S1 endpoint. No live relay or paid
provider call was made.

## Consolidated correction

The Opus 5.5 high-effort review and coordinator acceptance pass found four
material wiring gaps. The corrected gateway distinguishes absent personal
material from unavailable storage or local configuration, treats S1 claim
refusal and unsupported paths as non-credential failures, and maps an offline
Sponsor to temporary `unavailable` fallback. Contributed task and standalone
usage now passes the exact grant identity guard. Fractional finite `Retry-After`
seconds round up for a bounded temporary quota exclusion. The release sender's
single batch is exercised against both a fake verifier and the actual S1 process,
including a durable 204 whose exchange acknowledgement is lost before restart.

| Focused corrected-tree check | Result |
| ---- | ---- |
| `cargo nextest run -p concurro-tasks --test initialization rust_provider_failure_reselects_a_different_credential_version` | 1/1: real exchange admission and provider-failure status went through the task handler; the next metadata selection used a different record/version. The task's first frozen v2 submit is seeded directly in this test; its production v1-to-v2 conversion is covered by the task lib test. |
| `cargo nextest run -p concurro-tasks --lib rust_exchange::tests::org_task_submit_and_terminal_fact_use_existing_read_model` | 1/1: task and standalone contributed usage rows recorded; a submitted relay grant's withdrawal stayed `sending` on pending release, then acknowledged after the actual release sender's 204. |
| `cargo nextest run -p concurro-tasks-postgres --lib retired_version_cannot_insert_a_later_grant` | 1/1: version marker excludes the old version; a fractional quota hint keeps the replacement exclusion temporary. |
| `cargo nextest run -p concurro-exchange --test operator relay_claim_is_signed_once_and_release_is_durable` | 1/1: fake verifier received the release twice after an unrecorded first 204, while the exchange restarted before acknowledgement. The same test passed 1/1 with `CONCURRO_S1_HOSTED_BIN=/private/tmp/concurro-s1-target/debug/concurro-relay-hosted`, exercising S1's real release journal and restart refusal. |
| `cargo nextest run -p concurro-inference-gateway --lib --test personal --test relay` | 7/7: local/relay failure categories, unsupported path, success with 3 input and 2 output tokens, offline Sponsor, and one-ID request reuse. |

The corrected final changed-component suite (the five Rust packages listed
above, with `--no-fail-fast`) ran 115 tests: 114 passed, including the
132-second runtime restart; one unrelated planner test failed before setup
because its disposable
PostgreSQL container stream ended unexpectedly (`UnexpectedEof`). That exact
`planner_result_is_atomic_with_plan_report_and_task_snapshot` test then passed
1/1 alone. The combined corrected-tree result is 115/115 by case, with the
parallel Docker startup failure retained in
`/private/tmp/concurro-s4d-corrected-nextest.log`. Workspace check, changed
component Clippy, `cargo fmt --all -- --check`, and `git diff --check` passed.
Logs: `/private/tmp/concurro-s4d-corrected-workspace-check-final.log` and
`/private/tmp/concurro-s4d-corrected-clippy.log` (existing nonfatal warnings).

The corrected definition batch (32 Rust definitions in one Lodestone similarity
call) is `/private/tmp/concurro-s4d-corrected-definitions.json`. The approved
implementation keeps the PostgreSQL periodic release worker; its one-second
pause, file-backed release bearer and requirement to retain settings while
releases remain pending are documented. Version removal retires submitted
turns through existing cancelled/lost handling; it does not promise automatic
replacement. Trusted provider failure does select the next eligible version
under the existing stage retry.
