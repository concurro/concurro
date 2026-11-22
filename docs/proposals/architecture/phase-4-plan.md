# Phase 4 plan: sign-in, commands and credentials in Rust

**Status: Slices 1–4 implemented locally; cutover not started.** Phase 4 of the [migration](../architecture.md#migration).
It builds on phase 3 as deployed: the task application is the sole controller, the
backoffice read API serves every view under `/api/v2`, and the Python backoffice is left
with sign-in, browser commands and the credentials pages.

## Outcome

- The Python backoffice is retired: its process, image, settings, secrets and edge route.
- GitHub sign-in, sessions, viewer authority and browser command signing move to a new
  Rust process, the **accounts service**.
- Credential management (entries, lists, relay approvals) moves to the accounts service,
  which forwards to the Python vault as the backoffice does today. The vault stays until
  phase 5.
- The task application keeps phase 3's consolidation: its funding module still owns
  consent, credential selection, grants, budgets and accounting. Nothing is split out of
  it.
- The backoffice read API keeps its privileges: no user token, no command or secret path.
- The frontend doesn't change: the accounts service serves every route it calls today.
- GitHub stays the task-control interface. Model configuration stays in each fork's
  `.concurro/config.yaml`.

Out of scope, unchanged: OpenBao, the exchange, the inference gateway and the relay
verifier (phase 5); mentions and assignment ([phase 3 deferrals](phase-3-plan.md#deferrals)).

## What phase 3 left

The frontend calls the Python backoffice for 16 routes, plus GitHub's redirect to the
sign-in callback (`concurro-backoffice/src/lib/api/live.ts`). Every other call goes to
`/api/v2`.

| Group | Python routes the frontend calls | Python code |
| ---- | ---- | ---- |
| Sign-in | `GET /auth/login`, `GET /auth/callback`, `POST /auth/logout`, `GET /auth/logout-status`, `GET /api/me` | `backoffice/app.py:318-415`, `auth.py`, `revocations.py` |
| Commands | `POST /api/repos/{o}/{r}/issues/{n}/commands`, `POST /api/tasks/{o}/{r}/{n}/commands` | `app.py:521-572`, `commands.py:66-108`, `proofs.py` |
| Credentials | `GET`/`POST /api/me/credentials`, `PATCH /api/me/credentials/{id}`, `PUT …/{id}/secret`, `PUT …/order/{provider}`, `DELETE …/{id}?revision=` | `app.py:584-650`, `credentials.py` |
| Relays | `GET /api/me/relays`, `POST /api/me/credentials/relay`, `POST …/relay/pending`, `PUT …/relay/device` | same |

Its internal listener answers the read API's viewer lookups (`backoffice/internal.py`).
The rest of `app.py` (overview, target, task, section, review, report, turn, run,
sponsorship, issue and live-event routes, target refresh, static serving) has had no
caller since the cutover; several of its reads depend on the stopped Python service.

## Decisions

### Processes and authority

Three Rust processes face the browser or serve it, each holding only what its job needs:

| | Backoffice read API (unchanged) | Accounts service (new) | Task application |
| ---- | ---- | ---- | ---- |
| Serves | `/api/v2/*`: views, live updates, transcripts, artifacts | `/auth/*`, `/api/*` outside `/api/v2`; viewer authority on the platform network | Requests API, reads, target identities |
| User GitHub tokens | None | Holds them, in memory, per session | None |
| Browser credential | Viewer token only (the edge strips the session cookie) | Session cookie with the CSRF header | None |
| GitHub App credentials | None | The read-only Backoffice App, for writer checks | The Concurro App's private key |
| Command authority | None | Signs command proofs (private key) | Verifies them (public key) and rechecks the actor at application |
| Credential metadata | None | Through the vault, as the signed-in user | Reads it from the vault's metadata API for selection |
| Secret material | None | Passes new material to the vault on save; no route reads it back | None |
| State | Read model in its own database | Sessions in memory; encrypted logout revocation jobs in its own database | Task database |

**The read API and the accounts service are separate processes.** The read API parses
every task event and serves the largest surface; merging it with the process that holds
user tokens and the signing key would give that surface command and credential
authority. The read API's settings change only to name the accounts service as its
viewer authority, and it gains no credential.

**Credential management is a module of the accounts service, not the task
application.** Credential writes act as the signed-in user, and that decides it:

- The vault authenticates each owner call by resolving the user's own GitHub token
  (`gateway/vault/vault.py:1547-1557`), and the vault stays until phase 5. Only the
  process that holds user tokens can call it without a vault change. Moving the module
  into the task application would mean either handing it users' tokens, or adding a vault
  credential that can act as any user.
- New secret material passes through whichever process the browser posts to, and that is
  the accounts service in every option. Placing the owner elsewhere adds a second process
  that sees plaintext and isolates nothing.
- The task application parses the most untrusted input (webhooks, issue and PR text,
  model output, runner results) and holds the App key. Keeping new provider secrets out
  of it limits what a compromise there captures.
- No decision needs credential edits and grants in one transaction. Funding reads
  metadata at the boundary and at each attempt (`crates/tasks/src/funding.rs:311-357`),
  and the exchange rechecks current metadata at admission; phase 3 runs this way today.

The task application's funding module keeps consent, selection, grants, budgets and
accounting. Phase 4 moves only what the Python backoffice did. In phase 5 the accounts
service takes over the vault's metadata and its writes to the secret store; how
credential changes then reach the funding module is set out
[below](#phase-5-credential-metadata-in-the-accounts-service).

### Sessions

- **In memory, as today.** A restart signs everyone out, as the Python backoffice's does
  (`backoffice/auth.py:1-4`). The accounts service's database holds only logout
  revocation jobs.
- **Three cookies, today's names:**
  - `concurro_login`: the pending sign-in. `HttpOnly`, `SameSite=Lax`, 10 minutes.
  - `concurro_session`: the session ID. `HttpOnly`, `SameSite=Strict`.
  - `concurro_viewer`: a distinct read-only token. `HttpOnly`, `SameSite=Strict`.

  All are `Secure` when the public URL is HTTPS, with `Path` set to the base path. Keeping
  the names leaves the edge's `/api/v2` rule unchanged (`gateway/concurro.caddy:40-53`).
- **Sign-in is GitHub's web flow on the Sign-in OAuth App, with no scopes,** as today
  (`backoffice/README.md:7`). It uses a one-use `state` and S256 PKCE, both bound to the
  starting browser by the login cookie. The account comes from `GET /user` with the new
  token, never from the browser.
- **Lifetime:** GitHub's `expires_in` when it supplies one, otherwise eight hours
  (`auth.py:24`). The session ends at that time.
- **No token refresh.** OAuth App tokens don't expire or refresh. The Python code refreshes
  only when GitHub returns refresh material (`auth.py:157-215`), which the documented
  Sign-in OAuth App never does. *Assumption, checked in slice 1:* the deployed sign-in
  client is an OAuth App. If it is a GitHub App's user token, the session simply ends at
  the token's expiry and the user signs in again.
- **Revocation:** a GitHub `401` on any call made with a session's token ends that session.
  This covers a user revoking the app at GitHub. The browser is then sent to sign in, by
  the next `/api/*` call, or by the read API's next viewer lookup.
- **Logout revokes the token durably, as today** (`backoffice/revocations.py`). The
  sign-in token is also the user's owner identity to the vault, so an unrevoked token keeps
  that authority until GitHub drops it.
  1. The session ends locally first.
  2. In one transaction, the token is sealed with AES-256-GCM under the accounts service's
     logout key and recorded as a revocation job with an opaque receipt ID.
  3. One revocation attempt runs inline (`DELETE /applications/{client_id}/token`, with
     the Python code's 404/422 confirmation, `auth.py:341-361`).
  4. Failures retry with backoff through the shared job queue (`graphile_worker`) until
     GitHub confirms. Jobs survive restarts, and a revoked job's ciphertext is cleared.
  5. The browser gets the `concurro_logout` receipt cookie and
     `remote: revoked | pending`. `GET /auth/logout-status` reports
     `none | pending | revoked | unknown` for that receipt, as today; `unknown` means
     the job could not be decrypted.
  6. If the database is unavailable at logout, the service makes one inline attempt and
     reports `revoked` or `unavailable`, as the Python code does without its state
     (`app.py:388-390`).

  **Cost:** one small storage crate with two tables, a job handler and a key file. That
  is proportionate to the token's authority, so retries stay.

### Viewer authority

The read API's lookup keeps its contract, now served by the accounts service on its
platform listener ([contract](../contracts.md#viewer-authority)). The tier rules are
today's (`backoffice/internal.py:20-59`):

- **`writer`:** the upstream and the fork are both visible to the user's token, and the
  Backoffice App reports write, maintain or admin permission for the user on the upstream.
- **`reader`:** the issue is visible to the user's token.
- **`none`:** anything else.

Decisions are cached for 60 seconds, as today (`backoffice/github.py:21-24`).

### Commands

- **The browser routes and body are unchanged.** The accounts service ports the handler
  (`backoffice/app.py:521-572`) step by step:
  1. Check the CSRF header and the session.
  2. Check the command is known and its argument valid (`backoffice/tasks.py:140-157`).
  3. Check the kind's permission: `writer` for every kind except `withdraw`, which needs
     the target's repositories visible to the user (`tasks.py:187-193`).
  4. Keep each action's original `source_at` in the session.
  5. Sign the proof and post the request to the task application's `POST /v1/requests`.
- **The command proof contract doesn't change.** Its signer moves to the accounts service,
  with a new key pair; the task application keeps verifying it and rechecking the actor's
  permission when it applies the request.
- **Proof types are shared.** `CommandContent`, `CommandClaims` and the actor move from
  `crates/tasks/src/api.rs:51-89` into `concurro-contracts`, so the signer and the
  verifier use one definition.

### Credentials and relays

- **The browser routes and response shapes are unchanged.** The accounts service ports
  `backoffice/credentials.py`: forwarding to the vault's owner routes with the session's
  token, and mapping the vault's responses to the frontend's types.
  - The vault keeps its revision checks (`409` stale, `410` removed) and its `mutation_id`
    idempotency (`vault.py:524-568`). The accounts service passes both through.
  - Relay setup details (`relay_server`, `relay_download_url` and the derived
    `concurro-relay` commands) come from its settings (`backoffice/settings.py:78-97`).
- **Relay approval is ported as it works today, against the deployed verifier.** The
  vault calls the verifier's private API (`VAULT_VERIFIER_URL` on the
  `concurro-credential-private` network, `gateway/compose.relay.yaml`). The accounts
  service only calls the vault, so it needs no verifier credential and no place on that
  network.
  - Approval and device replacement keep the Python code's 60-second timeout, since the
    vault waits for the verifier (`backoffice/credentials.py:109, 158`).
  - The Sponsor approval link the verifier shows is the frontend's
    `/concurro/credentials/relay` page (`CONCURRO_RELAY_VERIFICATION_URL`), which
    redirects to the credentials page. Phase 4 changes neither.
- **Dropped, since the current frontend never calls them:**
  - The two `410` routes for clients older than entry lists (`app.py:624-627, 644-650`).
  - `PUT /api/me/credentials/{id}/relay` (`app.py:639-642`), which replaces one relay
    entry. Replacing a device's entries with one new code, which the frontend does call,
    covers it.

### Target identities

The accounts service needs each configured target's upstream, fork, issue repository and
repository ID to check tiers and build command proofs.

- **New task application route:** `GET /v1/targets` returns the current `config.targets`
  contract from the task database.
- **No bearer.** It returns only public repository identities, which the task application
  already publishes to every bus subscriber, and it is reachable only on the platform
  network.
- **Why not the bus:** `config.targets` is published only when it changes, so a stateless
  subscriber would find nothing once the last snapshot outlived the stream's 30-day
  retention.
- **Refresh:** the accounts service refreshes the list every minute, and when a request
  names a repository it doesn't know.

### Libraries

| Need | Choice |
| ---- | ---- |
| HTTP server and cookies | `axum` (workspace), `axum-extra` 0.12.6 with `cookie` |
| OAuth code flow, state and PKCE | `oauth2` 5.0.0, default features off, with a small async HTTP adapter over the workspace `reqwest`. *Untested:* the adapter's fit, checked first in slice 1 |
| Command proof signing | `jsonwebtoken` (workspace, already the verifier) with an RS256 `EncodingKey` |
| Session and viewer tokens | `rand` 0.10.4: 32 random bytes, URL-safe base64 |
| Logout revocation jobs | A storage crate `concurro-accounts-postgres` on `concurro-db` (SQL and migrations there); `graphile_worker` (workspace) for retries; `aes-gcm` 0.11.1 (RustCrypto) to seal tokens, keyed by a 32-byte key file |
| Backoffice App installation tokens and writer checks | `concurro-github-v1`'s `AppClient` (`writer_permission`, `lib.rs:395`). One addition: a read with a plain user bearer, since `user_read` takes a refreshable `UserGrant` (`lib.rs:506`) |
| Settings, secrets, telemetry | `concurro-config`, `concurro-telemetry` |

The session table is a small in-house map, not `tower-sessions`. A session must be found
by its viewer token as well as by its ID, and that second index doesn't fit a
session-store crate.

## Parity matrix

Status, as of main `9a30c5a` and frontend `285309a`:

- **Working:** the feature exists and is deployed.
- **Uncalled:** the route exists, but no frontend code calls it.
- **Stub:** existing, incomplete behavior.
- **Excluded:** intentionally not provided.

Tests are named by slice ([below](#slices)): R is a Rust integration test, B a browser
test, P a live check on this installation after the cutover.

| Flow | Today | Status | Rust owner | Contract | Migration action | Acceptance test |
| ---- | ---- | ---- | ---- | ---- | ---- | ---- |
| Sign in | Python `GET /auth/login`, `GET /auth/callback` | Working | Accounts | [Accounts browser API](../contracts.md#accounts-service) | Port; same callback URL, same OAuth App | B1: sign in as a writer, land on `next` |
| Sign-in refused or stale | Redirect to `/signin?error=` | Working | Accounts | Same | Port | R1: wrong `state`, missing login cookie, expired login and a refused code each redirect with an error and create no session |
| Open-redirect guard | `next` must start with a single `/` | Working | Accounts | Same | Port | R1: `//evil` and absolute URLs land on `/` |
| Current user | `GET /api/me` | Working | Accounts | Same | Port | B1: user menu shows the login |
| Session expiry | Session ends at token expiry or after 8 hours | Working | Accounts | Same | Port | R1: with an advanced clock, `/api/me` answers `401` and the viewer lookup answers `401` |
| Revoked GitHub token | A GitHub `401` ends the session | Working | Accounts | Same | Port | R1: GitHub answers `401` to a tier check; the session ends and the read API answers `viewer_expired` |
| Token refresh | Only when GitHub supplies refresh material | Unused (OAuth App) | — | — | Not ported; the session ends at expiry | Slice 1 confirms the OAuth App |
| Restart | Signs everyone out; pending logout revocations resume | Working | Accounts | — | Same behavior | R1: a pending revocation completes after a restart |
| Sign out | `POST /auth/logout`, remote revoke with a durable retry queue (`revocations.py`) | Working | Accounts | Same | Port, retries included | B2: sign out, reach the sign-in page with the notice, and the old session cookie gets `401` from `/api/me`. R1: with GitHub down the receipt is `pending`, then `revoked` once GitHub answers. R3: the old viewer token gets `401` from the viewer authority, and from the read API on `/api/v2/targets` and a populated task |
| Logout status | `GET /auth/logout-status`, by receipt cookie | Working | Accounts | Same | Port | B2; R1: an undecryptable job reports `unknown`; no response or log contains the token |
| CSRF | `x-concurro-request: 1` on writes, `SameSite=Strict` session cookie | Working | Accounts | Same | Port | R2: each write route without the header answers `403` and changes nothing (no task request, no vault call) |
| Viewer authorization of reads | Python internal listener | Working | Accounts (internal), read API (enforces) | [Viewer authority](../contracts.md#viewer-authority) | Port; repoint the read API | R3: writer, reader and outsider each get the right tier through the real read API router; B3: a reader sees summaries only |
| Read API holds no command authority | Viewer token only | Working | Read API | Same | Kept | R3: the viewer token and the read API's bearer are refused by the accounts command routes and by `POST /v1/requests` |
| Writer commands (start, approve, retry/extend, resume, stop, invalidate, funding) | Python signs, task application verifies | Working | Accounts signs; task application decides | [Command proofs](../contracts.md#command-proofs) | Port; new key pair | B4: a writer stops a task; R4: a reader's `stop` is `404`, as today, with no request |
| Withdraw by a non-writer | Target repositories visible | Working | Same | Same | Port | R4: a reader withdraws their own consent; an outsider is `404` |
| Sponsor command | Accepted, no UI sends it | Working (API only) | Same | Same | Port as is | R4 |
| Standalone review commands | Same route, PR number | Working | Same | Same | Port | R4: a writer's `retry` on a review PR reaches the task application |
| Command retry after a lost response | `Idempotency-Key`, `source_at` kept per action | Working | Same | Same | Port | R4: two posts with one key give one request and the same answer, including after the proof expires |
| Task application unavailable | `503` nothing recorded, `502` unknown | Working | Accounts | Same | Port | R4: connection refused gives `503`; a dropped response gives `502`; a retry with the same key gives the recorded outcome |
| Credential list | `GET /api/me/credentials` | Working | Accounts → vault | [Credentials](../contracts.md#credentials) | Port | R5 against the fake vault; B5 |
| Add, edit, enable/disable, replace, reorder, delete | Python → vault with the user's token | Working | Same | Same | Port | B5: each flow on the credentials page; R5: stale revision `409` and removed `410` reach the browser as "reload" |
| Secrets never returned | Vault owner routes return metadata only | Working | Vault | Same | Kept | R5: no response or log line contains the submitted secret; a malformed body echoes nothing |
| Vault unavailable | `503` with a fixed message | Working | Accounts | Same | Port | R5; B5: the page shows the error and keeps the form |
| Relay code lookup, approval, device replacement | Python → vault → verifier | Working, verifier deployed | Accounts → vault | Same | Port | R5: forwarded bodies and timeouts match today's, and a verifier refusal passes through; P3: approve a Sponsor code |
| Relay status and setup | `GET /api/me/relays`, polled every 30 seconds | Working | Same | Same | Port | B5: the setup panel shows the configured command; P3: the paired device shows as connected |
| Sponsorships (mine, per issue) | `GET /api/v2/me/sponsorships`, `/api/v2/issues/…` | Working (read API) | Read API | Unchanged | None | Covered by phase 3 |
| Sponsor device usage on the credentials page | Built from issue stubs without projections | Stub: shows "Unavailable" | — | — | Not changed by phase 4 ([gaps](#existing-gaps)) | — |
| Model configuration | `GET /api/v2/targets/{o}/{r}/configuration` | Working, read-only (`9a30c5a`) | Read API, task application | Unchanged | None | Covered by `9a30c5a` |
| Model configuration writes | Edit the fork's `.concurro/config.yaml` | Excluded | — | — | GitHub stays the control interface | — |
| Operator page | `GET /api/v2/operator` | Working, read-only (`9a30c5a`) | Read API | Unchanged | None | Covered by `9a30c5a` |
| Operator host actions | Task application CLI | Excluded from the browser | — | — | Unchanged | — |
| Live updates | `GET /api/v2/events` | Working (read API) | Read API | Unchanged | None | B3: still updates after the switch |
| Python read routes (overview, target, runs, task, sections, review, report, turns, turn runs, issue view, sponsorships), `/api/events`, target refresh | Python | Uncalled | — | — | Removed | — |
| Single relay entry replacement (`PUT /api/me/credentials/{id}/relay`) | Python → vault | Uncalled | — | — | Removed; device replacement covers it | — |
| Static frontend from the backoffice (`static_dir`), `/api/openapi.json` | Python | Uncalled in deployment | — | — | Removed; the web container serves the app | — |

### Deferrals

None. Every working flow is ported or kept where it already is.

### Existing gaps

These are not phase 4 work, but are recorded so they aren't mistaken for regressions:

- **Sponsor device usage.** The credentials page's device usage reads issue stubs from
  `/api/v2/me/sponsorships` that carry no sponsorship projections, so usage shows
  "Unavailable" (`SponsorDevices.svelte:29`). This is a read-side fix.
- **Stale frontend comments.** Doc comments in `src/lib/types.ts:699-775` name Python read
  routes that nothing calls, and the frontend README names `/api/events`.

## Slices

Four slices, each ending with a complete, testable change. Tests use local `axum` fakes for
GitHub, the vault and the task application where the real one isn't the subject, as the
task runtime tests do (`crates/tasks/tests/runtime.rs`).

### Slice 1: accounts service, sign-in and viewer authority

**Builds:**

- **The crate.** `crates/accounts` (binary `accounts`, image `ghcr.io/concurro/accounts`)
  with modules `settings`, `sessions`, `oauth`, `github` (tiers), `targets`, `browser`
  (the edge router) and `internal` (the platform router).
- **Its routes:** `/auth/login`, `/auth/callback`, `/auth/logout`,
  `/auth/logout-status`, `/api/me`, `/api/health`, and `GET /internal/v1/viewer`.
- **Logout revocation:** `crates/accounts-postgres` with its migration (receipts and
  sealed revocation jobs), and the `graphile_worker` job that retries them.
- **Supporting changes elsewhere:**
  - The task application's `GET /v1/targets`.
  - The plain-bearer user read in `concurro-github-v1`.
  - The read API's settings comment and README naming the accounts service.
- **Spike first:** the `oauth2` adapter against the workspace `reqwest`.

**Tests:**

- **R1, sessions:** the sign-in errors, the open-redirect guard, expiry with an advanced
  clock, and revocation by a GitHub `401`. A refreshed or replayed callback creates no
  second session.
- **R1, logout, with PostgreSQL:**
  - Logout clears both cookies and revokes once.
  - With GitHub down, the receipt reads `pending`; after GitHub recovers and the service
    restarts, the job completes and the receipt reads `revoked`.
  - A job sealed under another key reads `unknown`.
  - With the database down, logout makes one attempt and reports `revoked` or
    `unavailable`.
  - No row, response or log line contains the token after a revocation completes.
- **R3, viewer authority:** run the real read API router with the real accounts internal
  router and a fake GitHub that has a writer, a reader and an outsider.
  - The writer sees a full task, the reader a summary and their own consent, and the
    outsider nothing.
  - The internal route refuses a missing or wrong bearer (`401`), an unknown viewer token
    (`401`) and an unconfigured target (`404`).
  - It isn't served on the browser listener.
  - After logout, the old viewer token gets `401` from the internal route, and the read
    API answers `401` on `/api/v2/targets` (with a configured target) and on a populated
    `/api/v2/tasks/{id}`. An empty task list may still answer `200`; that is the read
    API's existing behavior and isn't changed here.
  - The viewer token and the read API's credentials authorize no command (with slice 2).
- **Dependency outages:** with GitHub unavailable, sign-in redirects with an error and a
  tier lookup answers `503`, not `none`. With the task application's targets unavailable
  at startup, the service answers `503` until it has a target list.

### Slice 2: commands

**Builds:**

- The two command routes, with today's argument rules and per-kind permission.
- Per-session `source_at`, proof signing, and forwarding to `POST /v1/requests`.
- The proof types moved to `concurro-contracts`, with their generated schema.

**Tests:**

- **R2, CSRF:** every write route needs the header.
- **R4, against the real task application router with its database:**
  - A writer's `stop` is recorded with the user as actor.
  - A reader's `stop` is `404` (today's answer) and records nothing; their `withdraw` is
    recorded.
  - An outsider gets `404`.
  - A standalone review command reaches the review.
  - A repeated `Idempotency-Key` returns the recorded outcome after the proof expired.
  - The task application down gives `503`; a dropped response gives `502`, then the
    recorded outcome on retry.
  - The existing proof-binding tests (`crates/tasks/src/api.rs:754-800`) run against a
    proof the Rust signer made, replacing the Python fixture.

### Slice 3: credentials and relays

**Builds:**

- The credential and relay routes, with today's mapping to the frontend's types.
- Relay setup details from settings.

**Tests:**

- **R5, against a fake vault that speaks the vault's owner contract:**
  - **Fixtures first:** request and response fixtures are recorded from
    `backoffice/credentials.py`'s tests before the Python code is deleted.
  - Each route forwards the session's token and the exact body.
  - Each response maps as the Python code mapped it, checked against those fixtures.
  - `409`, `410` and `422` pass through with the vault's message, including a refusal
    the vault relays from the verifier.
  - Relay approval and device replacement wait up to 60 seconds, as today.
  - A vault outage gives `503`.
  - A malformed body echoes no field, and no log line contains a submitted secret.
- **Untested here:** the real vault isn't exercised, because it resolves users at
  `https://api.github.com/user`, which can't be pointed at a fake without a Python change.
  P2 covers it on the test deployment.

### Slice 4: browser tests, deployment and retirement

**Builds:**

- **Browser tests:** a Playwright suite (below) in `concurro-backoffice`. The frontend's
  own code doesn't change.
- **Deployment:**
  - CI builds the `accounts` image, and `deploy/` releases and deploys it.
  - The accounts service's database and role, created like the other services'
    (`deploy/dev/postgres-setup.sql`), and its logout key file.
  - Compose services for `accounts`, with an edge listener and a platform listener.
  - The edge routes `/api/*` and `/auth/*` to it.
  - Settings examples and the platform guide.
  - The task application settings name the new public key.
- **Retirement:** the Python backoffice and its deployment pieces
  ([below](#retirement)), with documentation updated in the same change.

**Browser tests (B1–B5),** with Playwright against the frontend's production build, the
real accounts service, a fake GitHub that signs in a chosen test identity, the fake vault
and a stubbed read API serving fixed `/api/v2` fixtures:

- **B1:** sign in and land on `next`; the user menu shows the login.
- **B2:** sign out and see the notice with the receipt's status; a reload stays signed
  out.
- **B3:** a reader opens a task and sees the summary only; a live change still reaches the
  page.
- **B4:** a writer stops a task, and the task application fake receives a valid proof; a
  `503` shows the "nothing was recorded" message.
- **B5:**
  - Add, rename, disable, reorder, replace and delete a credential.
  - A stale revision shows "reload".
  - A vault outage keeps the form open with the error.
  - The Sponsor setup panel shows the configured command.

**Live checks on this installation (P1–P3),** with the operator's own account, run once
after the cutover. The reader and outsider cases are covered by R3, R4 and B3 with fake
identities. Signing in additional real identities is optional qualification, not a
cutover dependency.

- **P1:** sign in, and confirm targets, tasks, turns and transcripts load as before.
- **P2:** add, reorder and delete a throwaway credential against the real vault, and
  confirm the task application's next funding read sees the change.
- **P3:**
  - Run one browser command on an issue of a configured target and confirm the GitHub
    reply names the user.
  - Start a Sponsor device with the setup panel's command, approve its code from the
    verifier's approval link, and confirm the device shows as connected.
  - Sign out and confirm the notice reads `revoked`.

## Contracts

The exact contracts are in [contracts](../contracts.md):

- [Accounts service](../contracts.md#accounts-service): the browser API, settings and
  listeners.
- [Viewer authority](../contracts.md#viewer-authority): unchanged, new server.
- [Command proofs](../contracts.md#command-proofs): unchanged, new signer.
- [Credentials](../contracts.md#credentials): the browser routes in phase 4, and the
  metadata contract the accounts service takes over in phase 5.
- [Target identities](../contracts.md#target-identities): `GET /v1/targets`.
- [Vault](../contracts.md#vault): the owner routes' caller changes; nothing else.

## Cutover

One deployment window, roll-forward only. The installation's user signs in again
afterwards; no session, viewer token or in-flight browser action carries over.

1. **Release.** CI publishes the release with the `accounts` image. No frontend release
   is needed.
2. **Prepare accounts settings** in the accounts service's own settings directory, without
   touching the running services:
   - Its database, role and database URL file, and a new logout key.
   - The Sign-in OAuth App's client ID and secret. They are the same app with the same
     callback URL, `https://<host>/concurro/auth/callback`, so no GitHub change is needed.
   - The Backoffice App's ID and private key.
   - A new command proof key pair: the private key here, the public key for the task
     application.
   - A new viewer-authority bearer, shared with the read API.
   - The vault URL, the task application URL, and the relay settings carried over from the
     backoffice's settings.
3. **Finish the historical display migration while the Python backoffice still runs.**
   The exporter runs inside the backoffice container with its settings and a valid
   sign-in token, so it must run before step 4 revokes tokens and before step 6 removes
   the container ([historical display migration](../../reports/backoffice-parity.md#historical-display-migration)).
   - Export with `deploy/export_historical_display.py` from a pinned pre-retirement
     release, the last one that still contains `backoffice/`.
   - Import with `concurro-tasks imports display`.
   - Check source coverage against the inventory.
   - Keep the private export as migration evidence.

   Skip this step if the migration is already recorded complete.
4. **Settle the Python backoffice's sign-in tokens while it still runs.** A logout
   receipt covers only that browser's sign-out, so one `revoked` notice proves nothing
   about other tokens.
   - Sign out in every browser that is still signed in.
   - Check the whole revocation queue: count the `logout_jobs` rows with
     `state = 'pending'` in the backoffice's `logout.db`, opened read-only. Read only the
     count; the rows hold sealed tokens. The backoffice retries due jobs every second
     while it runs.
   - Tokens no queue covers stay valid: sessions that were never signed out, sessions
     lost to an earlier restart, and jobs that stay pending. Each affected user revokes
     the Sign-in OAuth App's authorization in their GitHub settings, under Authorized
     OAuth Apps. That revokes every token the app issued them, and GitHub asks them to
     authorize the app again at their next sign-in. On this installation that is one
     action by its only user.
   - Nothing is transferred: once the queue's count is zero, or the authorization has
     been revoked, `logout.db` and its key can go in step 6.
5. **Switch,** in one deployment:
   - Start `accounts`.
   - Restart the task application with the new public key.
   - Restart the read API with the accounts service as its viewer authority and the new
     bearer.
   - Install the edge route sending `/api/*` and `/auth/*` to `accounts`.

   Between the task application's restart and the edge reload, Python-signed commands are
   refused with `403 proof_invalid` and change nothing. A browser action retried after the
   switch meets a `401` and is issued again after sign-in.
6. **Stop and remove the Python backoffice** (container, compose entries, image reference
   and volume, as listed [below](#retirement)), then archive and remove its settings
   directory and its logout database.
7. **Sign in and verify:** run P1–P3.

**What carries over:**

- **Credential records, lists, revisions, relay intents and records:** untouched in the
  vault, under the same user IDs and entry IDs.
- **Task state, consent and request history:** untouched in the task database. Commands
  already recorded keep their outcomes.
- **The old proof key and logout key:** retired. Both are deleted with the backoffice
  settings, after step 4.
- **Historical displays:** imported by step 3. The task database keeps them, migration
  `0019`'s tables included, and the Rust importer (`concurro-tasks imports display`)
  stays.

If the accounts service misbehaves after the switch, the remedy is a corrective release.
The Python backoffice is not restarted.

## Retirement

**Removed from the repository:**

- `backoffice/` with its tests, `Dockerfile`, `README.md`, `APP-MIGRATION.md` and
  `requirements.txt`.
- **Code outside `backoffice/` that imports it:**
  - `service/tests/test_diagnostics.py`: remove
    `test_native_schema_is_visible_in_the_backoffice_panel` and its
    `backoffice.turns` import. It checks the retired panel's view; the recorder's own
    tests in the file stay.
  - `deploy/export_historical_display.py`: retired. Its instructions in
    [the parity report](../../reports/backoffice-parity.md#historical-display-migration)
    are marked as needing the pinned pre-retirement release.
  - `gateway/tests/phase5_spike.py`: retired. It is an opt-in qualification of the old
    portal → vault → exchange path, and its evidence is recorded in
    [per-user credentials phase 5 review](../per-user-credentials/phase-5-review.md).
    `gateway/tests/hosted_relay_spike.py` subclasses that retired fixture and is
    retired with it.
    The vault and exchange keep their own suites (`gateway/tests/test_vault*.py`,
    `test_exchange*.py`), and its run instructions in
    [phase 5](../per-user-credentials/phase-5.md) are marked retired.
- In CI, the `backoffice-api` image build and the `backoffice` lint, compile and test
  steps (`.github/workflows/ci.yml`).
- In `service/compose.yaml`, the `backoffice` service and the `backoffice-data` volume.
- The `backoffice` override in `service/compose.rust.yaml`.
- `service/compose.portal.yaml`.
- The Python routes in `gateway/concurro.caddy`.
- References in `gateway/README.md`, `docs/platform.md` and `deploy/` (`releases.py`,
  `host.sh`, `deploy.sh` and their tests).

**Removed from the deployment:**

- **Container and storage:** the `concurro-backoffice` container and its image reference,
  the `backoffice-data` volume, and `/var/lib/concurro/backoffice`.
- **Settings and secrets:** the backoffice settings file and secrets — the command proof
  private key, the viewer bearer, the logout key, the list index token and the read cache
  setting.
- **Routes:** the edge routes `/api/events` and `@backoffice` to `concurro-backoffice:8788`,
  and the internal listener `:8791`.

**Every Python route removed:** all of `backoffice/app.py` and `backoffice/internal.py`:

- Sign-in: `/auth/login`, `/auth/callback`, `/auth/logout`, `/auth/logout-status`.
- `/api/health`, `/api/me`, `/api/me/sponsorships`.
- `/api/repos/{o}/{r}/issues/{n}` and `…/sponsorships`, `/api/events`, `/api/overview`.
- `/api/targets/{o}/{r}`, `…/refresh` and `…/runs`.
- `/api/tasks/{o}/{r}/{n}`, `…/sections/{s}`, `…/reports/{id}`, `…/turns` and
  `…/turns/{id}/runs`.
- `/api/reviews/{o}/{r}/{pr}`.
- Both command routes, and every `/api/me/credentials*` and `/api/me/relays` route.
- `/internal/v1/viewer`.
- The static fallback.

**Bridges that remain until phase 5:**

- **The vault:**
  - Its owner routes, called only by the accounts service with the user's token.
  - Its metadata routes, called by the task application and the exchange.
  - Its resolver, called by the exchange.
  - Its calls to the verifier's owner endpoints, with the shared verifier bearer.
- **The exchange:** its operator API and turn polling, used by the task application.

**Not phase 4's:** the stopped Python service container and its state. It stays until the
operator records the phase 3 archive import complete; nothing in phase 4 uses it.

## Untested assumptions

- **The sign-in client is an OAuth App** (no expiring tokens). Checked in slice 1 against
  the deployment's settings.
- **`oauth2` 5.0.0 works with the workspace `reqwest` 0.13** through its async client
  trait. Checked by slice 1's spike; if it doesn't, the protocol is three documented
  requests, implemented directly.
- **The real vault and verifier are exercised only by P2 and P3,** with the operator's
  own account. The Rust tests use a fake vault built from recorded fixtures, and fake
  identities for the reader and outsider cases.
- **GitHub's token revocation endpoint** behaves as the Python code relies on
  (`backoffice/auth.py:341-361`). Covered by a fake in R1, and observed once at the
  cutover's sign-out check.

## Phase 5: credential metadata in the accounts service

Phase 5 plans its own steps. This section fixes the ownership and contracts phase 4
commits to ([contracts](../contracts.md#credentials)).

**Ownership:**

- **The accounts service** keeps durable credential metadata in its own database:
  entries, lists, filters, the policy history that as-of reads need, version records and
  relay records. It writes and destroys credential versions in OpenBao, under a policy
  that can create a version's path and read its metadata, but not read back its material.
  It holds the verifier's owner credential (registration lookup, approval, relay status)
  and no inference or release credential: those stay with the gateway and the exchange
  ([architecture](../architecture.md#services)).
- **The task application's funding module** keeps consent, selection, exclusions, grants,
  budgets and accounting, unchanged.

**How credential changes reach the task application.** The temporary vault interfaces
are replaced one for one:

| Need | Until phase 5: Python vault | Phase 5 target |
| ---- | ---- | ---- |
| Eligibility at a consent boundary (implicit fill) | `GET /metadata/{user_id}?at=` with `history_complete` | `GET /v1/credentials/{user_id}?at=` on the accounts service's platform listener, same fields |
| Selection for a new attempt, in list order with filters and exclusions | `GET /metadata/{user_id}` | The same route without `at` |
| Replay of an admitted attempt | The attempt's stored grant and version; no read | Unchanged |
| Revocation of a replaced, removed, unusable or expired version | The exchange rechecks current metadata before admitting a turn (`410`) and follows the vault's resolver events | `credential.<user_id>.changed` facts from the accounts service's outbox |

- **Selection keeps reading the owner directly.** It doesn't use a local projection, so a
  credential saved a moment ago is eligible at the next attempt, and boundary reads keep
  `history_complete`. The task application's adapter (`crates/vault-v1`, with
  `Metadata::select`) changes only its base URL, path and bearer.
- **The task application consumes revocations** with the shared consumer. In one
  transaction per event, the funding module revises each grant on a revoked version to
  `revoked` and records the version as unusable for that task's exclusions. A replay
  then returns the original grant with its current status, as the grant rules require.
- **The gateway consumes the same facts.** It drops bindings on a revoked version and
  refuses one that arrives later.
- **Revocation is eventual.** A selection made just before a revocation commits, or
  before its event is delivered, can still be used briefly. Consent and eventual
  revocation are preserved within the repository's accepted operational tolerance; the
  phase 5 plan sets the mechanism and its bound.
- **The fact mirrors the vault's credential event feed** (`gateway/vault/vault.py:472-481`),
  so the import carries the vault's sequence.
- **The import is one-time.** The accounts service imports the vault's metadata, and the
  migrator writes material to OpenBao, keeping record and version IDs. Credential writes
  pause while it runs; the task application switches its metadata URL afterwards.
- **Shared infrastructure only:** `concurro-db`, the outbox and forwarder in
  `concurro-events-postgres`, `concurro-events-nats` and `graphile_worker`. No new
  service is split from the task application.
