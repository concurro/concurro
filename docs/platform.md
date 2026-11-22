# Platform requirements and Compose examples

Concurro uses PostgreSQL and NATS JetStream. The bundled Compose examples pin
PostgreSQL 18.7 and NATS Server 2.15.3. They illustrate a working setup; they do not
prescribe how operators provision databases, manage containers or schedule backups.

## Development example

Run `docker compose -f deploy/dev/compose.platform.yaml up -d`. PostgreSQL listens on
localhost port 5432 and NATS on localhost port 4222 by default. Set
`CONCURRO_POSTGRES_PORT` and `CONCURRO_NATS_PORT` if those ports are occupied. The
Compose project persists database and stream data in named volumes.
`postgres-setup.sql` runs only when the PostgreSQL volume is first initialized; its
passwords are for local development.

The setup creates a database and login role for each planned service. Each database
revokes `CONNECT` from `PUBLIC`; its role has access to that database. Tests use
disposable containers through `concurro-testkit`.

The Phase 4 accounts service has its own `accounts` database and login role. Its
settings are illustrated in `deploy/accounts.settings.example.toml`. Give uid 65533
read access to the settings directory and secret files. `database-url` contains only
its PostgreSQL connection URL; `logout-key` contains 64 hex characters for a new
AES-256 key. Keep the same viewer-authority bearer in accounts and the read API,
and the corresponding command-proof private/public PEM pair in accounts and tasks.
The accounts browser listener joins `edge`; its viewer listener binds the
`concurro-accounts-platform` alias on `platform`. The read API's
`viewer_authority_url` names that alias. Neither listener publishes a host port.

The cutover requires the accounts image, both matching key pairs and all three
service settings before the release helper starts the Rust overlay. Sign-in uses
the existing OAuth App and callback `https://<host>/concurro/auth/callback`.
Settle the retired Python backoffice's logout queue and remaining OAuth App
authorizations before removing its settings; see the
[Phase 4 cutover](../gateway/README.md#phase-4-accounts-cutover). Recovery is a
corrective release. The stopped Python controller and its state remain until the
phase 3 archive import is recorded complete.

## Service requirements

- Each service has its own PostgreSQL database and role, with a private password
  file. The role owns only its database. A service embeds and runs its migrations at
  startup. Shared library tables use a separate schema and migration history in the
  same database.
- NATS has JetStream enabled on persistent storage. A producing service owns a
  stream for its subjects. Set the stream's `max_age`, replica count and duplicate
  window in that service's configuration. The outbox cap counts the event body
  and `Nats-Msg-Id` header together and must not exceed NATS `max_payload`.
- Stream identity checks and producer replay cover JetStream recreation within
  the retained journal. Restoring a producer database requires a declared coverage
  gap for any period the restored backup may have lost.

## OpenBao

Phase 5 keeps personal credential material and routinely rotated operational secrets
in OpenBao ([S2](proposals/architecture/phase-5/s2-openbao.md)). Rust services reach it
through `concurro-secrets`. The example pins OpenBao 2.7.3; follow its patch releases,
since only the latest is fixed.

**Setup.** `deploy/openbao/openbao.hcl` is the server configuration:

- PostgreSQL storage;
- the static seal;
- a file audit device;
- self-init, which creates the `concurro` KV v2 mount, the policies, the `accounts`,
  `gateway` and `exchange` AppRoles and the `admin`, `admin-recovery` and `operator`
  users at the first boot of an empty database.

With the Compose example:

1. Create OpenBao's PostgreSQL database and role. The role owns only its database.
2. In `/etc/concurro/openbao/`:
   - Copy `openbao.hcl` there.
   - Create `seal.key` with `openssl rand -out seal.key 32`. Give it to uid 100 (the
     image's `openbao` user, which the server runs as) with mode `0400`.
   - Create `openbao.env` with `BAO_PG_CONNECTION_URL` and, for the first boot only,
     `INITIAL_ADMIN_PASSWORD`, `INITIAL_ADMIN_RECOVERY_PASSWORD` and
     `INITIAL_OPERATOR_PASSWORD`. Remove the passwords once OpenBao has initialized.
3. Create the audit log directory owned by the `openbao` user:
   `install -d -o 100 -g 1000 -m 0750 /var/log/concurro/openbao`. The image's entrypoint
   exits if it can't take ownership of the directory. Rotate the log with `SIGHUP` after
   moving the file.
4. Recreate PostgreSQL from `deploy/compose.platform.yaml` so it joins the
   `concurro-secrets` network, then start `deploy/compose.openbao.yaml`.
5. As `admin`:
   - Create the recovery keys:
     `bao operator rotate-keys -target=recovery -init -key-shares=1 -key-threshold=1`.
   - Issue each service's AppRole secret ID:
     `bao write -f auth/approle/role/<service>/secret-id`. Store each in a file only
     that service reads, and give the service the role ID
     (`bao read auth/approle/role/<service>/role-id`).

OpenBao listens only on `concurro-secrets`. A client service joins that network and
names `http://concurro-openbao:8200`. The operator and admins run the `bao` CLI inside the
OpenBao container, where `BAO_ADDR` is set:

```bash
docker compose -f deploy/compose.openbao.yaml exec openbao bao login -method=userpass username=operator
```

A value read with `@file` comes from inside the container, so pass values on standard
input with `=-` instead.

**Startup.** PostgreSQL, then OpenBao, which unseals itself with the seal key, then the
services. OpenBao waits for PostgreSQL. A service retries sign-in until OpenBao is
unsealed. Neither OpenBao's storage URL nor the seal key is kept in OpenBao.

**Rotating an operational secret.** Sign in as `operator` as above, then patch the field
from standard input, for example:

```bash
docker compose -f deploy/compose.openbao.yaml exec -T openbao bao kv patch -mount=concurro ops/gateway/providers chatgpt_access_token=- < token.txt
```

Readers pick it up within their refresh interval (60 seconds by default), or at once
after a provider refuses the old value. No restart is needed. The operator can't read
personal credential material.

**Administration.** Self-init runs once. Change policies, roles or users with an `admin`
identity and the `bao` CLI, and record each change with the deployment records. Keep
the `admin` and `admin-recovery` passwords offline and apart from each other.

If a service's secret ID leaks:

1. Issue a new one.
2. Destroy the old one with `bao write auth/approle/role/<service>/secret-id/destroy`
   and its value on standard input.
3. Revoke the AppRole tokens: `bao lease revoke -prefix auth/approle/login`.

OpenBao's lease IDs don't name the role, so this revokes every service's AppRole token.
Each service signs in again on its next request, the affected one with its new secret
ID.

**The phase 5 import.** The credential import uses a `migrator` AppRole, which exists
only during the import. As `admin`, before the import:

```bash
bao write auth/approle/role/migrator token_policies=migrator token_ttl=1h token_max_ttl=4h
```

Then issue its secret ID as for a service. Once the import's report is clean:

```bash
bao delete auth/approle/role/migrator
```

```bash
bao lease revoke -prefix auth/approle/login
```

Deleting the role stops new sign-ins, but tokens already issued keep working until
revoked. The second command revokes them, along with every other service's AppRole
token. Those services sign in again on their next request; the import runs in an idle
window, so nothing is interrupted.

**Recovery.** If both admin identities are lost:

1. Add `disable_unauthed_generate_root_endpoints = false` to the listener in
   `openbao.hcl`, and restart OpenBao.
2. Generate a root token with the recovery keys through the legacy
   `sys/generate-root/attempt` and `sys/generate-root/update` endpoints. The CLI's
   `operator generate-root` uses the authenticated endpoint instead.
3. Repair the identities, revoke the root token, remove the setting and restart.

**Backups.** OpenBao's database is in the PostgreSQL backup. Back up `seal.key` and the
recovery keys separately: a restore needs the database and the key, and either alone
decrypts nothing.

## Compose ingress example

This example uses the bundled Compose files and deployment helper. Other deployment
layouts can supply the same service configuration and connectivity.

Deploy release 1 first, using its full signed commit SHA. It keeps the Python
webhook and recovery jobs while adding the optional bus consumer. Install the
platform and ingress only after release 1 is running. In this example,
`deploy/host.sh` includes `service/compose.ingress.yaml` and starts
`deploy/compose.platform.yaml` when `/etc/concurro/ingress/settings.toml` exists.
The deployment user needs directory traversal to detect that file; the service
needs read access to its configuration and secrets.

Neither database nor NATS publishes a host port in the example. Ingress's public
listener is on `edge`; its replay listener binds the
`concurro-ingress-platform` network alias.

1. Create `/etc/concurro/platform/nats.conf` from `deploy/nats.conf.example`. Replace
   both password placeholders with hashes from `nats server passwd`; keep the clear
   passwords in separate owner-only JSON files
   `{"user":"ingress","password":"..."}` and
   `{"user":"service","password":"..."}` for their containers. The scoped users
   share the `CONCURRO` JetStream account. Prepare an owner-only PostgreSQL superuser
   password file at `/etc/concurro/platform/postgres-password` readable by the
   container's PostgreSQL user.
2. Start the platform with
   `sudo docker compose -f deploy/compose.platform.yaml up -d --wait`. Store the
   connection URL for ingress's database and role in
   `/etc/concurro/ingress/database-url`, outside the Compose environment.
3. Copy `deploy/ingress.settings.example.toml` to
   `/etc/concurro/ingress/settings.toml` and replace the installation ID. Put the
   App webhook secret, ingress NATS JSON, recovery bearer and service replay bearer
   beside it as private files readable by the image's ingress uid 65533.
   Give the service its NATS JSON and replay bearer
   under `/etc/concurro/service`, and add `bus_url`, `bus_credentials_path`,
   `bus_replay_url`, `bus_replay_bearer_path`, optional `bus_max_attempts`
   and `bus_backoff_seconds`, and
   `delivery_recovery_bearer_path` to its settings. Its recovery bearer and the
   ingress recovery bearer must match; likewise the two copies of the service replay
   bearer. In release 1, leave `bus_coverage_authoritative` false while the old HTTP
   route remains the edge target; its GitHub history check still verifies list
   freshness. Restart through the release helper with the full release 1 commit SHA.
4. Before switching the edge, send a signed delivery for a repository the service
   does not serve directly to ingress's `/webhook` from the private network. Check
   `concurro-ingress status`, then resend its GUID and confirm `200` with no second
   outbox event. Confirm the consumer recorded the initial gap by finding
   `ingress coverage gap reconciled` in service logs and checking its list state
   after strict reconciliation.
5. Check real delivery corpus parity through Rust ingress, then change the edge route
   to `concurro-ingress:8789`. In release 1, set `bus_coverage_authoritative` true
   in service settings and restart the service. Verify that the initial coverage gap
   was strictly reconciled, a new coverage fact makes lists current after backlog
   drain, and redelivering a GUID produces no second effect. Record these checks.
6. Only then prepare `/etc/concurro/service/settings.next.json` for release 2,
   copying the current settings and removing `webhook_secret_path` and
   `bus_coverage_authoritative` from the staged copy. Keep the
   bus URL, credentials, replay URL and bearer, and
   `delivery_recovery_bearer_path`. Keep the actual App webhook secret in ingress's
   settings; Rust ingress still verifies GitHub signatures. The release helper validates
   the staged settings with the new image before stopping the old service, then promotes
   them. Deploy release 2 through that helper, retaining the Rust ingress edge route.
   Release 2
   removes Python's `/webhook` intake, App delivery scan and overlap mode. The private
   App listing and redelivery deputy remains available to Rust ingress. The service
   performs one strict reconciliation to establish its active scope on first C2 start;
   later App, installation or target changes trigger another.

If all eight webhook slots remain busy for 500 ms, ingress returns `503` before
reading the queued request body and records no receipt. Recovery scans failed App
deliveries every five minutes by default, but GitHub retains them for only three
days. Clear sustained saturation within that window; if coverage expires, declare
the gap and strictly reconcile rather than assuming a later scan can recover it.

## Recovery

`deploy/backup-platform.sh` is an example backup helper for the bundled PostgreSQL
Compose service. Its archive includes roles and every database. It is not required
when using another backup mechanism or deployment layout.

JetStream is rebuilt from retained outbox rows after a stream loss. After restoring
an ingress database backup, declare the lost period with
`concurro-ingress gap --from TIME --to TIME`; use the backup's time as `from` and the
restore time as `to`. To inspect receipts and repair a mapped payload, run
`concurro-ingress status` or
`concurro-ingress remap --from TIME --to TIME [--event FAMILY]` inside the ingress
container. These times use RFC 3339.

## Deployment overrides

The example deployment helper reads `/etc/concurro/compose.platform.host.yaml`
and `/etc/concurro/compose.service.host.yaml` when present. These optional Compose
overrides allow operators to adapt the example without changing the bundled files.
Manual Compose commands must include the applicable override files too.

### Rust service NATS authentication

Ingress, tasks and the backoffice read API use `nats_credentials_file` for a private
JSON object containing `user` and `password`. Keep `nats_url` free of credentials;
URL-embedded credentials do not authenticate these clients. Omit the credentials file
only when connecting to an unauthenticated development broker.


## Backoffice read views

The target page loads current models and policy separately from task lists through
`GET /api/v2/targets/{owner}/{repo}/configuration`. The read API requires fresh
writer authority on that target, including fork visibility. It proxies the private
read-bearer endpoint in the task service, which resolves the fork's current default
branch and reads the validated configuration at that exact commit. The response
includes the source repository, branch and full SHA, normalized model assignments,
profiles, mode and allowlisted policy limits. MCP settings, arbitrary config keys
and credentials are excluded. These are current settings for new work; existing
tasks retain their pinned configuration. A configuration failure does not block
saved target task lists.

`GET /api/v2/operator` requires writer authority on at least one configured target.
It reports generic projection readiness, revision, received-but-unapplied event
count and last received event time. Task stage and turn-observation counts include
only tasks with a fresh issue-specific writer decision. Baseline loading returns
null task counts, not zero. Buffered events measure the read database, not broker
backlog; baseline readiness does not prove upstream service health.

The Operator page is read-only. Host actions, gateway health, deployment drift,
leases and live pending-request counts are not exposed. Pending requests occur in
the baseline but have no admission event stream, so they are not presented as a
live queue. Unknown terminal outcomes are separate from unfinished turns; streamed
finished facts take precedence over active status observations. A null finished field
is absent. Counts describe retained projection observations, including any source
event gaps, rather than a live Exchange queue. The page shows a timestamped snapshot and updates on reload. Authority
or storage failures return a visible unavailable error.

These additive endpoints require the task service before the read API and frontend.
They use existing read credentials and App read permissions, with no new settings
or schema migrations.
