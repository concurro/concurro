# Phase 5 S2 evidence

[S2](proposals/architecture/phase-5/s2-openbao.md) of the
[phase 5 plan](proposals/architecture/phase-5-plan.md): OpenBao and the shared secrets
client. This is local implementation evidence. Nothing was deployed, and no live
credential was used.

## Delivered

| Item | Where |
| ---- | ---- |
| Shared client | `crates/secrets` (`concurro-secrets`), on `vaultrs` 0.8.0 and `secrecy` 0.10.3 |
| Server configuration | `deploy/openbao/openbao.hcl`: PostgreSQL storage, static seal, file audit, and self-init of the mount, policies, AppRoles and users |
| Compose example | `deploy/compose.openbao.yaml`; `deploy/compose.platform.yaml` adds the `concurro-secrets` network, which PostgreSQL also joins |
| Test container | `concurro-testkit`'s `OpenBaoContainer`: OpenBao 2.7.3 and its PostgreSQL on a private network, started from a given configuration file |
| Operator guide | [Platform guide](platform.md#openbao): setup, startup, rotation, administration, recovery, backups |

### `concurro-secrets`

- **`OpenBao`:**
  - signs in with an AppRole, reading the secret ID file on each sign-in;
  - renews its token at two thirds of the lease, and signs in again when renewal fails
    or grants less than a minute;
  - retries a refused request once after signing in again.
- **Operations:**
  - `read`, served from memory within the refresh interval (60 seconds by default);
  - `refresh`;
  - `create` with `cas=0`;
  - `metadata`;
  - `destroy`, which removes every version and then the metadata, and treats an absent
    path as already destroyed.
- **`SecretRef`:** `openbao:<path>#<field>` or a file path, parsed from settings.
  **`Secrets`** resolves both kinds and caches file values for the same interval.
- **No secret values in errors or debug output.** Values are `SecretString`, and errors
  carry only the path, field and HTTP status, never response bodies.

## Tests

`cargo nextest run -p concurro-secrets`: 12 of 12 passed. The eight OpenBao tests start
OpenBao 2.7.3 from `deploy/openbao/openbao.hcl` itself, so they exercise the deployed
policies.

| Test | Shows |
| ---- | ---- |
| `accounts_writes_once_and_cannot_read_back` | Accounts creates once with `cas=0`. A second create is refused, and its error contains no value. Accounts can't read data but reads metadata. The gateway reads the exact version and can neither create nor destroy. The operator and the recovery admin can't read personal material. Accounts' destroy removes the path, after which the gateway's read is not found, and a second destroy succeeds. The audit log has HMAC'd entries and no plaintext |
| `operator_rotation_reaches_readers` | The operator writes and patches `ops/gateway/providers` without naming a version. A `Secrets` reader serves the old value within the interval, the new one at once on `refresh`, and the next one after the interval. A missing field is reported as such |
| `admin_manages_roles_and_operator_cannot` | Both admin identities create and delete a role and issue a replacement secret ID. The operator is refused both |
| `the_import_cleanup_revokes_migrator_tokens` | A `migrator` token, from a role created with a one-hour TTL, writes and reads back personal material. After the role is deleted, that token still reads. After `sys/leases/revoke-prefix/auth/approle/login` it is refused, and its secret ID can't sign in again |
| `an_expired_token_signs_in_again` | With a 3-second token on a 4-second maximum, a read after 5 seconds signs in again and succeeds, through the renewal path |
| `a_revoked_token_is_replaced_by_signing_in_again` | After admin revokes every AppRole sign-in (`sys/leases/revoke-prefix/auth/approle/login`), the client's next request is refused, signs in again once and succeeds. The operator can't revoke |
| `a_cold_restart_unseals_without_help` | After stopping OpenBao and PostgreSQL and starting PostgreSQL then OpenBao, OpenBao unseals and a stored version is read |
| `a_recovery_key_yields_root_only_while_the_legacy_endpoint_is_enabled` | With the deployed configuration, the legacy root generation endpoint doesn't serve. With `disable_unauthed_generate_root_endpoints = false` added, a recovery key created by `admin` yields a `root` token, which is then revoked |
| Unit tests (4) | `SecretRef` parsing, file refresh on interval and on request, OpenBao references needing a client, no values in debug output |

**Checked, not tested:**

- **"Logs contain no secret values"** was checked statically. The crate logs no values.
  `vaultrs` and `rustify` record only error text, which carries no content, and request
  and response lengths and status.
- **Removing the `INITIAL_*` passwords** after initialization was checked by hand in the
  independent review: a restart without them still unseals and serves.

The other checks passed:

- `cargo clippy -p concurro-secrets -p concurro-testkit --all-targets -D warnings`;
- `cargo fmt --check`;
- `cargo check --workspace --tests`;
- `git diff --check`.

## Rollout notes

- **PostgreSQL restarts once to join `concurro-secrets`.** That happens at the next
  release deploy where `deploy/host.sh` runs the platform Compose file, which is only when
  `/etc/concurro/ingress/settings.toml` exists. Otherwise it happens at the platform
  guide's setup step 4. Services reconnect. An installation that overrides the platform
  file must add the same network to its override. Nothing else in `deploy/` is affected:
  the backup's `pg_dumpall` already includes OpenBao's database.
- **OpenBao itself starts only when the operator follows the platform guide.** No
  service uses it until S7.
- **The image runs as uid 100,** its `openbao` user. So the seal key belongs to uid 100,
  and the audit log directory must be created owned by it. The entrypoint exits if it
  can't take ownership, and the platform guide says so.
- **The `bao` CLI runs inside the container,** with `BAO_ADDR` set in the Compose
  service. Values go in on standard input (`=-`), since `@file` reads inside the
  container.

## Codex review

Codex found that deleting the `migrator` role left its issued token able to read
personal material, for the role's default 32-day TTL. The import cleanup now deletes the
role and revokes AppRole tokens. The role is created with a one-hour TTL, and the new
test shows both steps are needed.

## Independent review

A fresh-eyes review found one defect, now fixed: the guide's audit log directory, created
root-owned, stopped OpenBao starting. It also found five gaps, now closed:

- the CLI address and access path;
- the condition under which PostgreSQL restarts;
- an admin policy that couldn't revoke service tokens (it can now revoke AppRole
  sign-ins, which cover every service, since lease IDs don't name the role);
- the brief's operator policy row;
- a test for recovering after a refused request.
