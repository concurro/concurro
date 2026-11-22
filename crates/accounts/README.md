# Accounts service

The Rust `accounts` binary implements browser sign-in, in-memory sessions, logout,
browser commands, credential and relay routes, and the private viewer authority.
Phase 4 deployment is prepared locally; the Python backoffice owns live routes until cutover.

CI builds `ghcr.io/concurro/accounts:<service SHA>` from this crate's Dockerfile.
The Rust Compose overlay starts accounts with an edge listener and a separate
platform listener. The image serves `/api/health` for its container healthcheck.

Set `CONCURRO_ACCOUNTS_CONFIG` to a TOML file based on
[`deploy/accounts.settings.example.toml`](../../deploy/accounts.settings.example.toml).
The process has separate browser and internal listeners. The browser listener serves
`/auth/login`, `/auth/callback`, `/auth/logout`, `/auth/logout-status`, `/api/me`,
`/api/health`, both existing `/api/repos/.../commands` and
`/api/tasks/.../commands` routes, and `/api/me/credentials` and `/api/me/relays`.
Only `/internal/v1/viewer` is on the internal
listener. Keep that listener on the platform network at cutover.

The sign-in client must be the existing Sign-in **OAuth App**, with callback URL
`<public_url>/auth/callback`. Sign-in requests no scopes and uses one-use state and
S256 PKCE. Sessions and user tokens stay in process memory; a restart signs users
out. The distinct viewer cookie is read-only. GitHub `401` invalidates the session.
For local fake OAuth tests, `oauth_authorize_url` and `oauth_token_url` may override
the GitHub defaults; production settings omit both.
Sign-in failures redirect with a URL-encoded explanation. `/api/me` always returns
string `name` and `avatarUrl` fields, falling back to the login and an empty URL.
The read API's `viewer_authority_url` points to the internal listener at cutover;
its bearer must match `viewer_authority_bearer_file`.

The accounts database stores only anonymous logout receipts and AES-256-GCM sealed
revocation jobs. `logout_key_file` contains exactly 64 hex characters. Keep the same
key across restarts until all pending receipts have completed. Logout ends local
authority first, then tries remote revocation. A failed attempt remains pending for
Graphile Worker retry; the receipt reports `revoked`, `pending`, `unknown` or
`unavailable`. A database outage permits one remote attempt without a receipt.
Completed jobs erase their ciphertext.

The task application's private `GET /v1/targets` supplies target identities and
their revision from one durable row. Accounts refreshes that snapshot each minute;
viewer lookups return `503` until a usable snapshot is available, then use the
last snapshot if a refresh fails. The read-only
Backoffice App checks upstream writer permission, while the user's plain bearer
checks repository and issue visibility.

Each command requires the session cookie, `X-Concurro-Request: 1` and a UUID
`Idempotency-Key`. All commands except `withdraw` require writer access;
`withdraw` requires visibility of both target repositories. Accounts signs the
confirmed actor and exact command as a 180-second RS256 proof, then forwards it to
the private task API. The first source time stays with the session action ID, so
a retry can receive the recorded outcome with a fresh proof. The task application
keeps its apply-time actor check. Set `command_proof_private_key_file` to the RSA
PEM paired with the task application's `command_proof_public_key_file`.

Credential and relay requests use the signed-in user's GitHub bearer against
`vault_url`. The vault owns verification, list revisions and `mutation_id`
idempotency; accounts keeps no credential secrets. Writes require the same
session cookie and `X-Concurro-Request: 1` header as commands. Vault `401`
invalidates the browser session. Entry and relay responses retain the existing
frontend shape, and vault `409`, `410` and `422` messages pass through.
An unreachable vault returns `503` for credential routes; `/api/me/relays`
shows `service: unavailable`. Set optional `relay_server` and
`relay_download_url` to display Sponsor setup commands and a download link.
Relay approval and device replacement have a 60-second request deadline.
