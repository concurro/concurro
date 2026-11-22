# S2: OpenBao and the shared secrets client

Part of the [phase 5 plan](../phase-5-plan.md). OpenBao deploys before the cutover. Its
first users are the operator, storing org provider credentials, and its own checks.
Accounts, the exchange and the gateway use it from S7.

## Server

- **Release.** OpenBao 2.7.x, pinned to an exact patch (2.7.3 at planning time).
  OpenBao fixes only its latest release, so upgrades follow its patch releases.
- **Storage: PostgreSQL.** OpenBao gets its own database and role on the shared server.
  - `ha_enabled = false`.
  - `max_connect_retries = 0`, so it waits for PostgreSQL at boot.
  - The connection URL comes from a file or environment variable, never from OpenBao
    itself.
  - OpenBao's docs mark PostgreSQL production-ready and recommend it to new users over
    Raft. Single-node Raft would add snapshot operations for no gain here.
- **Seal: static.** The key is a 32-byte AES-256-GCM key in a root-owned file outside
  PostgreSQL, with a key ID.
  - A restart unseals without anyone stepping in.
  - Anyone holding both the key file and the database can decrypt everything, so they
    are stored and backed up separately.
  - Rotation uses `previous_key` and `previous_key_id`.
  - Chaining the key to a TPM or `systemd-creds` is later hardening.
- **Declarative self-init** (`initialize` stanza) creates the mounts, auth methods,
  policies and audit device at first boot, then revokes the root token. The
  configuration file is the record of the setup.
- **Audit.** A file audit device in the configuration file's `audit` stanza, with
  rotation handled by the host. Values are HMAC'd. If the device can't write, OpenBao
  refuses requests, so a full disk shows up as an outage rather than unaudited access.
- **Network.** One listener on a private network that only OpenBao's clients join:
  accounts, the exchange, the gateway, and the operator's access path. It has no edge
  route.

## Layout and policies

One KV v2 mount, `concurro`, without a mount-wide `cas_required`.

- **Create-only comes from the `accounts` policy.** A write to an existing path is an
  `update`, and that policy grants only `create`. Accounts also always sends `cas=0`.
- **Requiring CAS mount-wide would break routine rotation:** it makes the operator's
  `bao kv patch` name the current version
  ([S0](../../../rust-phase-5-s0-evidence.md#2-openbao)).

| Path | Holds |
| ---- | ---- |
| `personal/<record_id>` | One personal credential version: `{secret, account?}`. `record_id` is the vault's `sha256(user/route/version)`, unique per version. There is no custom metadata: accounts' database maps the path to its entry, route and version, and adding metadata would need a metadata-write capability |
| `ops/gateway/providers` | Org provider credentials: `openai_api_key`, `claude_oauth_token`, `chatgpt_access_token`, `chatgpt_account_id` |
| `ops/<service>/<name>` | A service's other operational secrets ([below](#operational-secrets)) |

| Policy | Capabilities |
| ---- | ---- |
| `accounts` | `create` on `data/personal/*`, `read` on `metadata/personal/*`, `update` on `destroy/personal/*`, `delete` on `metadata/personal/*`; `read` on `data/ops/accounts/*` |
| `gateway` | `read` on `data/personal/*`, `data/ops/gateway/*` |
| `exchange` | `read` on `data/ops/exchange/*` |
| `migrator` | `create` and `read` on `data/personal/*`, `read` on `metadata/personal/*`. Used only by S7's import, and revoked afterwards |
| `operator` | `create`, `update`, `patch` and `read` on `data/ops/*`, `read` on `metadata/*`. No access to `data/personal/*` |
| `admin` | Policies, auth methods and AppRole roles and secret IDs, audit configuration, mounts' configuration; token revocation. No access to `data/personal/*` |

- **Accounts can't read material back.** `create` without `read` refuses any data read.
  An existing path (any version, deleted or destroyed) makes a write an `update`,
  which this policy refuses even before `cas=0` is checked.
- **The gateway can read any personal version.** It reads only the version its
  admission names; per-grant OpenBao authorization is later hardening.

## Service identity

- **Services sign in with AppRole.** Each service has its own role.
  - The role ID is in its settings.
  - The secret ID is a file, its one piece of OpenBao bootstrap material.
  - Tokens are renewable, with a one-hour TTL. The client renews them, and signs in
    again after a failure.
- **The operator signs in with userpass** on the `operator` policy for routine secret
  rotation. Its password is kept with the installation's other operator credentials.
- **Administration uses two `admin` userpass identities,** both created by self-init with
  the `admin` policy. Their passwords are kept offline and apart from each other. They
  are used only for:
  - changing policies or AppRoles;
  - issuing a replacement secret ID for a service;
  - revoking the `migrator` role after S7's import;
  - recovering from a lost operator password.
- **Self-init runs once.** Later changes are made with the `admin` identity through the
  `bao` CLI, and recorded with the deployment records; editing the `initialize` stanza
  doesn't apply them.
- **Recovery keys are the break-glass** ([S0](../../../rust-phase-5-s0-evidence.md#2-openbao)).
  - Self-init creates none. Right after first boot, `admin` creates them with
    `bao operator rotate-keys -target=recovery`. They're stored offline with the seal
    key's backup, apart from the database.
  - **To recover** when both `admin` identities are lost:
    1. Set `disable_unauthed_generate_root_endpoints = false` on the listener and
       restart.
    2. Generate a root token through the legacy `sys/generate-root` endpoint with the
       recovery keys.
    3. Repair the identities.
    4. Revoke the token, remove the setting and restart.

    The endpoint stays disabled the rest of the time.
- **Initial passwords come from the environment.** Self-init reads the `admin` and
  `operator` passwords with `eval_source = "env"` and `require_present = true`, so
  they're never written into the configuration file. Whether to change them at first
  sign-in is an operational choice.

## Shared client: `concurro-secrets`

A focused crate on `vaultrs` 0.8, used by accounts, the exchange and the gateway:

- AppRole sign-in, token renewal, and signing in again.
- `read(path)`, with an in-memory cache and a refresh interval (60 seconds by default),
  plus `refresh(path)` for a caller that has just had a credential refused.
- `create(path, value)` with `cas=0`.
- `destroy(path)`, removing every version and then the metadata.
- `metadata(path)`.
- **A `SecretRef` settings type:** `openbao:ops/gateway/providers#chatgpt_access_token`,
  or a file path, so the same setting reads either source.
- **Errors never contain secret values.** The values are wrapped in `secrecy` types.

## Operational secrets

| Secret | Where | Rotation |
| ---- | ---- | ---- |
| Org provider credentials | `ops/gateway/providers` | `bao kv patch -mount=concurro ops/gateway/providers chatgpt_access_token=@file`. The gateway picks it up within 60 seconds, or at once after a provider `401`. No restart |
| The exchange's claim signing key and key ID | `ops/exchange/claim-signing` | Write a new key under a new ID. The verifier holds both public keys until the old claims expire |
| Verifier `release` credential | `ops/exchange/verifier-release` | Write it, and update the verifier's token file |
| Verifier `inference` credential | `ops/gateway/verifier-inference` | Same |
| Verifier `owner` credential | `ops/accounts/verifier-owner` | Same |
| The exchange's token key | `ops/exchange/token-key` | Write a new key while idle; live sessions' runner tokens and session keys stop working ([exchange](s4-exchange.md#runner-api-v2)) |
| Gateway → exchange admission bearer | `ops/exchange/admission-bearer` and `ops/gateway/admission-bearer` | Write both. Each side reloads within 60 seconds; requests in between get a `401` and are retried |

**These stay in files:**

- OpenBao's seal key and storage URL.
- Each service's AppRole secret ID.
- Database URLs and NATS credentials.
- The GitHub App key, the webhook secret, and accounts' OAuth client secret and logout
  key.
- The task application's bearers for the exchange's operator API and accounts'
  metadata API.

They are either bootstrap material or rarely rotated, and are read by services that
otherwise need no OpenBao identity. Moving any of them later is a settings change
through `SecretRef`.

## Startup order

PostgreSQL → OpenBao, which unseals itself → services. A service retries sign-in with
backoff until OpenBao's `sys/health` reports it unsealed. There is no cycle:

- OpenBao's own storage URL and seal key are files.
- No service needs OpenBao to reach its database.

## Backups

- OpenBao's database is backed up with the other databases.
- The seal key file is backed up separately. A restore needs both.
- The self-init configuration is kept with the deployment records.

## Acceptance

- After a cold restart of PostgreSQL and then OpenBao, OpenBao unseals and serves
  without anyone stepping in.
- With the `accounts` policy:
  - a `cas=0` create succeeds;
  - a second write to the same path is refused;
  - a data read is refused;
  - metadata reads, `destroy` and the metadata delete succeed.
- With the `gateway` policy, a personal read succeeds, and writes and destroys are
  refused.
- The `operator` and `admin` policies can't read `data/personal/*`.
- The operator's `bao kv patch` of `ops/gateway/providers` succeeds without naming a
  version, and accounts' overwrite of an existing personal path is refused.
- The `admin` identity revokes the `migrator` role and issues a replacement secret ID
  for a service; the `operator` identity can do neither.
- A recovery key created by `admin` yields a root token through the temporarily enabled
  legacy endpoint, which is disabled again afterwards.
- An operator patch of `ops/gateway/providers` is seen by a `concurro-secrets` reader
  within its refresh interval, and at once after `refresh`.
- An audit entry records each request with HMAC'd values.
- The client's errors and logs contain no secret values, checked with a known secret in
  the tests.
