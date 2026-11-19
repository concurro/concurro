# Platform minimum requirements

The Rust services use PostgreSQL 18.7 and NATS Server 2.15.3 with JetStream.

## Development

Run `docker compose -f deploy/dev/compose.platform.yaml up -d`. PostgreSQL listens on
localhost port 5432 and NATS on localhost port 4222 by default. Set
`CONCURRO_POSTGRES_PORT` and `CONCURRO_NATS_PORT` if those ports are occupied. The
Compose project persists
database and stream data in named volumes. `postgres-setup.sql` runs only when the
PostgreSQL volume is first initialized; its passwords are for local development.

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
- Keep PostgreSQL database backups. Back up every service database nightly and
  test restoration. Stream identity checks and producer replay cover JetStream
  recreation. A producer restore needs a declared
  coverage gap for any period its backup may have lost.

## Production ingress

Deploy release 1 first, using its final signed commit SHA recorded in the release
packaging evidence. Release 1 keeps the Python webhook and recovery jobs while adding the bus
consumer. Install the platform and ingress only after release 1 is running. The host's
`deploy/host.sh` includes `service/compose.ingress.yaml` and starts
`deploy/compose.platform.yaml` when `/etc/concurro/ingress/settings.toml` exists.
Neither database nor NATS publishes a host port. Ingress's public listener is on
`edge`; its replay listener binds the `concurro-ingress-platform` network alias.

1. Create `/etc/concurro/platform/nats.conf` from `deploy/nats.conf.example`. Replace
   both password placeholders with hashes from `nats server passwd`; keep the clear
   passwords in separate owner-only JSON files
   `{"user":"ingress","password":"..."}` and
   `{"user":"service","password":"..."}` for their containers. The scoped users
   share the `CONCURRO` JetStream account. Prepare an owner-only PostgreSQL superuser
   password file at `/etc/concurro/platform/postgres-password` readable by the
   container's PostgreSQL user.
2. Start the platform with
   `sudo docker compose -f deploy/compose.platform.yaml up -d --wait`. Create an
   `ingress` login role and database owned by it. Store its connection URL in
   `/etc/concurro/ingress/database-url`; the role needs no other database. Keep the
   PostgreSQL password in that file, outside the Compose environment.
3. Copy `deploy/ingress.settings.example.toml` to
   `/etc/concurro/ingress/settings.toml` and replace the installation ID. Put the
   App webhook secret, ingress NATS JSON, recovery bearer and service replay bearer
   beside it as private files readable by ingress uid 65533, including directory
   traversal. Give the service its NATS JSON and replay bearer
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

Run `deploy/backup-platform.sh` nightly on `<host>` and keep its full PostgreSQL
archive with the host's other backups. It includes roles and every database. JetStream
is rebuilt from retained outbox rows after a stream loss. After restoring an ingress
database backup, declare the lost period with
`concurro-ingress gap --from TIME --to TIME`; use the backup's time as `from` and the
restore time as `to`. To inspect receipts and repair a mapped payload, run
`concurro-ingress status` or
`concurro-ingress remap --from TIME --to TIME [--event FAMILY]` inside the ingress
container. These times use RFC 3339.
