# Accounts service: Phase 4 Slice 1

The Rust `accounts` binary now implements browser sign-in, in-memory sessions, logout and
the private viewer authority. It is local implementation work; the Python backoffice
still owns the live routes until the later Phase 4 slices and cutover.

Set `CONCURRO_ACCOUNTS_CONFIG` to a TOML file based on
[`deploy/accounts.settings.example.toml`](../../deploy/accounts.settings.example.toml).
The process has separate browser and internal listeners. Route only `/auth/login`,
`/auth/callback`, `/auth/logout`, `/auth/logout-status`, `/api/me` and `/api/health` to
the browser listener for this slice. Only `/internal/v1/viewer` is on the internal
listener. Do not expose the internal listener at the edge.

The sign-in client must be the existing Sign-in **OAuth App**, with callback URL
`<public_url>/auth/callback`. Sign-in requests no scopes and uses one-use state and
S256 PKCE. Sessions and user tokens stay in process memory; a restart signs users
out. The distinct viewer cookie is read-only. GitHub `401` invalidates the session.
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
