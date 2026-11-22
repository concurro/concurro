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
The internal listener serves `/internal/v1/viewer` and, with the native credential
store configured, `/v1/credentials/{user_id}`. Keep that listener on the platform
network at cutover.

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

With `[credentials] store = "vault"` (the default) or no `[credentials]` section,
credential and relay requests use the signed-in user's GitHub bearer against
`vault_url`. The vault owns verification, list revisions and `mutation_id`
idempotency; accounts keeps no credential secrets. Writes require the same
session cookie and `X-Concurro-Request: 1` header as commands. Vault `401`
invalidates the browser session. Entry and relay responses retain the existing
frontend shape, and vault `409`, `410` and `422` messages pass through.
An unreachable vault returns `503` for credential routes; `/api/me/relays`
shows `service: unavailable`. Set optional `relay_server` and
`relay_download_url` to display Sponsor setup commands and a download link.
Relay approval and device replacement have a 60-second request deadline.

## Native credential store

A `[credentials]` section, with `[events]`, adds accounts' own credential store. It keeps
the vault's entries, provider lists, policy history and versions in the accounts schema,
and each version's material in OpenBao at `personal/<record_id>`, created with `cas=0`
under the `accounts` AppRole, which can't read it back. With `store = "native"` the
browser routes use it instead of the vault, with the same responses, revision checks
and `mutation_id` replay. Settlement, relay reservation settlement and expiry run only
with `store = "native"`, so a vault import in progress is left alone.

- **Writes.** A new secret becomes a `pending` version, is created in OpenBao, then
  activates with the entry, list and policy changes in one transaction. A replace whose
  entry changed meanwhile is superseded and answers `409`. Only activated versions are
  ever listed. A version left pending for two minutes is settled: activated if OpenBao
  holds it, otherwise failed, so a retry with the same `mutation_id` writes it again.
  The replay journal holds no secret, so a retry under the same `mutation_id` answers
  with the first write's result even if its secret differs; the vault answered `409`.
  The frontend uses a fresh ID for each action and reuses it only for retries.
- **Material is destroyed** by the `destroy_credential_material` job once a version is
  replaced, removed, superseded or expired, retried until OpenBao confirms.
- **Expiry.** Hourly, a valid version past its known expiry becomes `invalid` with
  `status_reason = expired`.
- **Change facts.** Each committed change appends `credential.<user_id>.changed` to the
  outbox, which a forwarder publishes to the `ACCOUNTS` stream.
- **Metadata.** `GET /v1/credentials/{user_id}?at=` answers as the vault's
  `/metadata/{user_id}?at=`, plus `relay_id`, to the bearer in `metadata_bearer_file`.
- **Relays** go through the verifier's owner API v1 (`[credentials.verifier]`): pending
  registrations, approval and device replacement, and status. An approval reserves each
  route's version, asks the verifier, then commits relay-custody versions. A reservation
  left by a crash is retried with the identical approval after two minutes, and released
  if the verifier refuses it. Without a verifier, status reports `unconfigured`.
- **Last use.** A durable consumer (`accounts-usage`) of `inference.usage` on the
  `EXCHANGE` stream advances each version's `last_used`; redelivered or older facts
  change nothing.

```toml
[credentials]
store = "vault"
metadata_bearer_file = "/etc/concurro/accounts/credential-metadata-bearer"

[credentials.openbao]
url = "http://concurro-openbao:8200"
role_id = "ACCOUNTS_ROLE_ID"
secret_id_file = "/etc/concurro/accounts/openbao-secret-id"

[credentials.verifier]
url = "http://concurro-verifier-private:7450"
owner_token_file = "/etc/concurro/accounts/verifier-owner-token"

[events]
nats_url = "nats://concurro-nats:4222"
```

### Vault import

The import copies a stopped vault into the native store. With vault writes paused,
copy its database, then record the live vault's metadata answers for every owner at
every policy revision, version creation and retirement, and now:

```bash
accounts capture-vault --vault-url http://concurro-vault:8090 --service-token-file <vault service token file> --vault-db <copy> --out capture.json
```

Then stop the vault and import, with a `migrator` AppRole created as the
[platform guide](../../docs/platform.md#openbao) describes:

```bash
accounts import-vault --vault-db <copy> --master-key <vault master key file> --capture capture.json --migrator-role-id <role ID> --migrator-secret-id-file <secret ID file>
```

Pass `--keyring <file>` too if the vault runs with `VAULT_KEYRING_FILE`. The import
refuses a copy with a relay approval in progress, or native tables that already hold
another import. It writes all metadata in one transaction, then copies each version's
material, checking it against its fingerprint and reading it back; a path already in
OpenBao is compared instead. A mismatch stops the import and names the `record_id`.
Rerunning resumes, and adds nothing once finished. The report lists counts per table
and every recorded read the native store answers differently, and fails unless there
are none.
