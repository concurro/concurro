# Phase 4 Slice 2 evidence

Implemented on `codex/phase-4-slice-2`, based on signed Slice 1 commit
`e38b15a493b1166543479899a89d27d482d5d7fc`. No deployment or live GitHub
write was made during implementation.

## Implemented

- Both existing browser command routes require the session cookie, CSRF header and
  UUID action ID. They validate the nine command kinds and arguments, use the
  current target snapshot and user bearer for visibility, and require writer
  access except for repository-visible withdrawal.
- An action ID retains its first source time for the session. Accounts signs the
  shared contract types with RS256 and forwards to the real task request API.
  Task proofs expire after 180 seconds without library leeway. Delivery errors
  distinguish proven-unsent requests from unknown outcomes.
- The shared claims, content and submission types have generated serialization
  and deserialization schemas in this working tree. The task verifier checks every binding and
  its existing apply-time actor check remains in place.

## Verification

| Check | Result |
| ---- | ---- |
| `cargo nextest run -p concurro-accounts -p concurro-tasks -p concurro-contracts -p concurro-accounts-postgres --locked --no-fail-fast` | Corrected tree: 59/59 passed; log `/tmp/concurro-phase4-slice2-corrected-nextest.log` |
| Narrow command integration after the consolidated review corrections | 1/1 passed |
| `cargo check -p concurro-accounts -p concurro-tasks --all-targets` | Passed |
| `cargo fmt --all --check` and `git diff --check` | Passed |
| `cargo run -p concurro-contracts --bin generate --locked` | Generated four command schemas |
| `python3 contracts/check_compat.py e38b15a493b1166543479899a89d27d482d5d7fc` | Passed |
| `cargo clippy -p concurro-accounts --all-targets --locked` | Corrected accounts code passed with no accounts warnings; existing dependency warnings remain. Log `/tmp/concurro-phase4-slice2-corrected-clippy.log`. A prior strict `-D warnings` check stopped in unchanged `concurro-jsonb` and `concurro-github-v1`. |

The real PostgreSQL/task-router integration covers writer stop attribution,
reader stop and outsider refusal, reader withdrawal, standalone review admission,
both browser routes and CSRF, invalid arguments and action IDs, an exact replay
after proof expiry, content conflict, response loss after durable admission and
retry, and a proven-unsent connection failure. It also rejects viewer/read
credentials at accounts commands and direct task requests, and checks tampered
Rust-signed proofs against the real task verifier.

The consolidated review correction tightened that integration: complete unsigned
submissions reach the task verifier with viewer, authority and task-read credentials,
with and without bearer headers, and receive `403 proof_invalid` without a new
action. An outsider's withdrawal is `404`; resume arguments are `422` and plain
resume records `description_only=false`. JSON body rejection follows the session
and CSRF checks (`401`, `403`, then `422`). The lost-response retry is checked
against its durable request ID, and the standalone review row identifies the
writer. The corrected narrow integration passed 1/1.

Before implementation, Lodestone searches located the task verifier, request
mapping, target authority and source-order storage. One similarity search covered
the 27 new or moved definitions; it found relevant existing `RequestCommand`,
`AppClient` and target-cache behavior, which this slice reuses. `rust-mcp` checked
the existing `jsonwebtoken` crate before adding the signer dependency.
