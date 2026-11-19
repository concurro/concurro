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

## Compose ingress example

This example uses the bundled Compose files and deployment helper. Other deployment
layouts can supply the same service configuration and connectivity.

Release 1 keeps the Python webhook and recovery jobs while adding the optional bus
consumer. In this example, `deploy/host.sh` includes `service/compose.ingress.yaml`
and starts `deploy/compose.platform.yaml` when `/etc/concurro/ingress/settings.toml`
exists. The deployment user needs directory traversal to detect that file; the
service needs read access to its configuration and secrets.

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
   bearer. Leave `bus_coverage_authoritative` false while the old HTTP route remains
   the edge target; its GitHub history check still verifies list freshness. Restart
   through the release helper, which pulls the exact commit's images.
4. Before switching the edge, send a signed delivery for a repository the service
   does not serve directly to ingress's `/webhook` from the private network. Check
   `concurro-ingress status`, then resend its GUID and confirm `200` with no second
   outbox event. Confirm the consumer recorded the initial gap by finding
   `ingress coverage gap reconciled` in service logs and checking its list state
   after strict reconciliation.
5. Change the edge route to `concurro-ingress:8789`, set
   `bus_coverage_authoritative` true in service settings, and restart the service.
   Verify a new coverage fact makes lists current after the backlog drains. The
   Python webhook route stays available through the first release. Remove it only
   in release 2 after the cutover checks.

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
