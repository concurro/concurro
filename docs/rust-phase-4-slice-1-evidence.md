# Phase 4 Slice 1 local implementation evidence

Implemented on `codex/phase-4-slice-1` from `bb433ab`. No deployment, GitHub
write, permission change or live task retry occurred during implementation.

## Implemented

- `concurro-accounts` has separate browser and internal routers, state and S256 PKCE
  OAuth login, in-memory session and viewer credentials, issue-specific GitHub tiers,
  target snapshot caching and local-first logout.
- `concurro-accounts-postgres` persists anonymous receipts and AES-256-GCM sealed
  revocation jobs. Graphile Worker retries pending jobs after a restart and erases
  ciphertext on completion.
- The task API serves `GET /v1/targets` from one `tasks.config_targets` row.
  `concurro-github-v1` supports plain-bearer user reads. The read API router is
  exported for integration tests; deployed routing is unchanged.
- Settings and current-behavior documentation cover the local service. Python
  remains the live browser authority.

## Consolidated review correction

The Opus review prompted one pass: a shared HTTP client adds User-Agent for OAuth,
GitHub and targets; a failed refresh retains the last target snapshot; expired
pending logins, sessions and viewer indexes are pruned; `/api/me` returns string
name and avatar fields; sign-in redirects carry encoded, human-readable errors;
and startup uses the shared JSON telemetry subscriber at `info`. The local
settings example binds its internal listener to loopback. The sponsorship timing
test now takes its before timestamp from PostgreSQL's clock.

Lodestone semantic searches before correction located the existing GitHub request
headers, telemetry initializer, session behavior and database-time queries. A
single `similar_code_search` checked all 73 added or touched functions together;
no duplicate implementation was adopted. `rust-mcp` supplied the OAuth error type
variants used to classify refused, incomplete and unavailable exchanges.

## Checks

| Check | Result |
| ---- | ---- |
| OAuth adapter against fake GitHub | Passed: shared User-Agent, JSON response request, body credentials, state and S256 verifier |
| `cargo nextest run --offline -p concurro-accounts --test authority_flow` | Passed with real PostgreSQL, real accounts and read API routers, fake GitHub and task endpoints |
| `cargo nextest run --offline -p concurro-accounts --lib` | Passed session pruning test |
| `cargo nextest run --offline -p concurro-tasks --test target_configuration` | Passed both target tests, including same-row revision and identities |
| `cargo nextest run --offline -p concurro-tasks --test sponsorship -E 'test(funding_policy_uses_local_activation_time_and_exact_config_digest)'` | Passed after using PostgreSQL time |
| Final changed-component suite (`concurro-accounts`, `concurro-backoffice-api`, `concurro-tasks`) | 60/60 passed; log: `/private/tmp/concurro-phase4-nextest-corrected.log` |
| `cargo check --offline -p concurro-accounts -p concurro-tasks` | Passed |
| `cargo clippy --offline -p concurro-accounts --all-targets` | Passed; no warnings in accounts or accounts-postgres |
| `cargo fmt --all --check`; `git diff --check` | Passed |

The authority test covers writer, reader and outsider task views; actual own-consent
redaction; missing service bearer, viewer and target; browser listener isolation;
post-logout rejection by both routers; GitHub and target outages, including stale
target fallback; remote `401` invalidation and read API `viewer_expired`; callback
replay, missing and expired login cookies, refused code, expiry and redirect guard;
pending, revoked and database-outage logout states; ciphertext secrecy; and a
Graphile Worker recovery using a new logout instance over the same database.

## Limits for review

- The command-authority cross-check depends on Slice 2's command routes and proof.
- The deployed sign-in client's OAuth App setting was not read or changed during
  this local implementation; confirm it before cutover.
- This slice has no edge cutover, browser commands, credential routes or Python
  retirement. Those remain in later slices.
