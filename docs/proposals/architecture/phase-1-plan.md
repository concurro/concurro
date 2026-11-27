# Plan: phase 1, contracts and platform

**Status (2026-11-24): complete; shared infrastructure is in production.**
The steps below describe the original Phase 1 baseline, before service deployment.
See the [phase 1 progress record](../../rust-rewrite-progress.md) for exit-test evidence.

Phase 1 of the [architecture review](../architecture.md#migration) builds what every Rust
service stands on: the contract types and their generated schemas and Python models, the
event plumbing (outbox, forwarder, consumers), and the platform (a database and role per
service, NATS JetStream). It ships no service; GitHub ingress is phase 2. Everything stays
Python in production.

**Done when** the [exit test](#exit-test) passes: an event a Rust producer commits with
its state change reaches a Rust and a Python consumer through its outbox and the bus, each
using models generated from the same contract, and a duplicate delivery or a restart of
the forwarder or a consumer changes nothing. The event is a `github.issues` event, whose
action tags a variant with its own optional fields, so the whole generation chain is
proved with it.

## Scope

| In | Out |
| ---- | ---- |
| The Cargo workspace and its CI | Any service, ingress included (phase 2) |
| `concurro-contracts`: the envelope, identifiers and `github.issues`, with schemas, fixtures and Python models | The other contracts, added by the phase that first needs them |
| The events crates (`concurro-events`, `-events-postgres`, `-events-nats`): outbox, forwarder, consumers, deduplication, held events, parking, snapshot revisions | Ordered per-task consumption (phase 3) |
| `concurro-db`, `concurro-config`, `concurro-telemetry`, `concurro-testkit` | Blob storage (phase 3), OpenBao (phase 5) |
| The job queue decision, with a spike | Using the job queue in a service |
| Postgres and NATS for development and tests; the minimum-requirements doc | Production deployment of either |
| A parked-event command: list, show, replay, discard | Other operator calls |
| A Python consumer with the same rules, for the exit test | The Python service's own consumer (phase 2) |

## Decisions

These are taken here; each is cheap to revisit before code depends on it.

- **The workspace is at this repository's root,** `Cargo.toml` with members `crates/*`,
  beside the Python trees, which are untouched. The separate `concurro-relay` repository
  is only the model for its conventions.
- **Toolchain, dependencies and CI follow `concurro-relay`'s conventions:** a pinned
  toolchain (`rust-toolchain.toml`, 1.100.0 today, with clippy and rustfmt), edition
  2024, resolver 3, `publish = false`, every dependency pinned exactly (`=x.y.z`) in
  `[workspace.dependencies]` with `default-features = false` and explicit features, and a
  CI job that runs `cargo fmt --check` and `cargo nextest run --workspace --locked` with
  `cargo-nextest` installed at the relay's pinned version.
- **`concurro-relay`'s crates** come in as git dependencies pinned to relay release tags
  when a service first needs one (`provider-protocol` for the gateway, phase 5). Phase 1
  has none.
- **Crates are named `concurro-<name>`** in `crates/<name>`.
- **Each crate has one responsibility and an explicit public API.** Shared crates own
  reusable mechanisms; domain rules stay in their service's crates. A crate or module
  that passes about 1,000 lines is a prompt to reconsider its responsibilities, not a
  limit.
- **SQLx stays in storage crates.** Business code gets typed operations, or a
  `concurro_db::UnitOfWork`: an opaque handle on one open transaction that storage crates
  turn into their typed operations, so writes from several crates still commit together.
  - `concurro-db` is shared database infrastructure only: pools, migrations, transactions
    and savepoints, and the unit of work. It holds no service's queries and no generic
    CRUD layer.
  - A service's storage traits live in its core crate; their schema and SQL live in its
    storage crate.
  - One owner opens and commits or rolls back each unit of work; storage crates write
    through it and never commit it. An atomic operation called without one may open and
    commit its own transaction.
- **Database access is `sqlx`** (0.9.0, May 2026) with runtime-checked queries, so builds
  need no database.
- **Migrations:** each service embeds its own `sqlx::migrate!` and runs it at start.
  Library crates keep their tables in their own Postgres schema with their own migrations
  table (`events._sqlx_migrations` for `concurro-events-postgres`), so their version
  numbers never collide with a service's. The job queue manages its own schema.
- **NATS is `async-nats`** (0.52.0 or later; 0.53.0 is from November 2026).
- **Identifiers are `uuid`** 1.29 with the `v7` and `v5` features, behind newtypes in
  `concurro-contracts`. A GitHub event's ID is derived from its delivery ID and mapping
  revision, as the architecture specifies, as their UUIDv5
  ([contracts](../contracts.md#identifiers)).
- **Telemetry is `tracing`** with `tracing-subscriber` JSON logs to stdout; exporting
  traces is left until a service needs it.
- **Configuration** uses the `config` crate to load one TOML file per service, named by
  an environment variable, plus secrets read from files named in it. Typed settings
  are validated at start.
- **The job queue is `graphile_worker`** (0.14.2, November 2026), subject to the
  [spike](#job-queue). It's built on sqlx 0.9 and tokio 1.54, and enqueues inside an
  existing transaction (`WorkerUtils::with_executor`). `apalis-postgres` (1.1.0) doesn't
  document transactional enqueue, which jobs committed with a state change need.
- **Python models** use `pydantic` 2.14.1, the version `backoffice/` and `gateway/vault/`
  already pin, and the Python NATS client is `nats-py`. Their runtime pins, transitive
  ones included, are in `contracts/python/requirements.txt`. The generator,
  `datamodel-code-generator`, is pinned separately in
  `contracts/python/requirements-dev.txt`, since nothing runs it at runtime.
- **Tests that need Postgres or NATS use `testcontainers`** (0.28), so CI needs only
  Docker, which GitHub's runners have. A compose file runs the same images for local
  development.

## Layout

```text
Cargo.toml                    workspace, pinned dependencies
rust-toolchain.toml
crates/
  contracts/                  wire types; the `generate` binary
  events/                     envelope handling, outcomes, handler trait, consumer policy
  events-postgres/            outbox, deduplication, attempts, held, parked, revisions
  events-nats/                the forwarder's publisher, streams and JetStream consumers
  db/                         pool, migrations runner, unit of work
  config/                     settings loading and secret files
  telemetry/                  tracing setup
  testkit/                    Postgres and NATS containers, fixtures, a test producer
contracts/
  schemas/                    generated JSON Schema, one file per contract and version
  fixtures/                   examples per contract and version: valid, invalid, expected
  python/
    concurro_contracts/       generated Pydantic models
    consumer/                 the reference Python consumer
    requirements.txt          runtime pins
    requirements-dev.txt      generator pins
    tests/
deploy/dev/compose.platform.yaml    Postgres and NATS for local development
deploy/dev/postgres-setup.sql       a role and database per service
docs/platform.md              minimum requirements and setup
```

## Contracts crate

- **The envelope** is `Envelope<T>` with the
  [contracts' fields](../contracts.md#event-envelope). `kind` comes from the contract, so a
  producer can't mislabel a snapshot as a fact. `occurred_at` is the producer's
  transaction time.
- **A contract is a family with one schema per version,** and each value names its own
  `type` and subject. `github.issues` is one contract; a labeled issue's envelope has
  `type` `github.issues.labeled` and the subject `github.issues.labeled`, matching the
  [catalog](../contracts.md#github-ingress). A task event's subject carries its task ID.
  So the `Contract` trait gives the family, version and kind, and each value gives its
  `type`, subject, and, for a snapshot, its entity and revision.
- **A registry** lists every contract, which `generate` and the tests iterate.
- **Wire types are only wire types.** They never derive database traits and services never
  use them as domain state; services convert at their edges.
- **Unknown fields are accepted:** no `deny_unknown_fields` anywhere, since consumers
  ignore unknown fields.
- **Open enums** (documented as open in the contracts) carry an `Other(String)` variant on
  deserialization and are marked `x-concurro-open: true` in their schema. Closed enums have
  no fallback, so an unknown value fails and the event is parked.
- **Tagged variants carry the JSON Schema `discriminator` keyword,** added through
  `schemars`, so the Python generator emits a discriminated union rather than trying each
  variant.
- **`generate`** (`cargo run -p concurro-contracts --bin generate`) writes
  `contracts/schemas/<family>.v<version>.json` using JSON Schema 2020-12, set explicitly in
  the generator settings. Where serialization and deserialization differ it writes both
  (`.ser.json`, `.de.json`). It doesn't run Python; `contracts/python/generate.sh` runs the
  pinned `datamodel-code-generator` on the deserialization schemas (consumers deserialize)
  with pinned options: Pydantic v2, strict types, discriminated unions, no timestamp
  header.

## Fixtures and checks

- **Fixtures** live in `contracts/fixtures/<family>.v<version>/`: `valid/*.json`,
  `invalid/*.json`, and an `expected/<name>.json` for a valid fixture whose
  re-serialization differs from it. Together they cover each variant, a missing optional
  field and an explicit `null`, defaults, integer bounds (IDs above 2³¹), an unknown field
  (valid, and dropped on re-serialization), a string where an integer belongs (invalid),
  and an unknown value in a closed and an open enum.
- **Rust:** every valid fixture deserializes and re-serializes to its expected JSON (the
  fixture itself unless an `expected` file exists); every invalid one fails.
- **Python:** every valid fixture parses with `model_validate_json` in strict mode, and
  every invalid one fails. Strictness is a test, not an assumption: `"123"` for an integer
  must be refused.
- **Drift:** CI runs `generate` and `generate.sh` and fails on any difference from the
  committed files.
- **Compatibility:** CI compares each committed schema with its version at the merge base
  and allows, within a version, only changes known to be compatible: a new optional
  property, a new value in an open enum, and changes to descriptions and examples. Any
  other change fails, constraints such as bounds and patterns included, and needs a new
  version file. The checker has its own tests: pairs of schemas, each with the verdict it
  must give.

## Events crates

`concurro-events` holds what doesn't depend on storage or transport: the handler trait,
outcomes and the consumer's decisions. `concurro-events-postgres` holds the tables below
and their operations; `concurro-events-nats` publishes and consumes with `async-nats`.

### Outbox and forwarder

```sql
create table events.outbox (
  sequence     bigserial primary key,
  id           uuid not null,
  subject      text not null,
  type         text not null,
  version      integer not null,
  message      jsonb not null,
  created_at   timestamptz not null default now(),
  published_at timestamptz,
  retry_at     timestamptz,
  last_error   text,
  unique (id, version)
);
create index outbox_unpublished on events.outbox (sequence) where published_at is null;
```

- **`Outbox::append(&mut uow, &Envelope<T>)`** inserts in the caller's transaction and
  issues `NOTIFY`. It refuses a message larger than the configured maximum message size
  (which must not exceed the server's `max_payload`) or a subject outside the service's
  configured stream subjects, catching a bad event in the producer's transaction.
  `(id, version)` is unique, so a breaking change can append both versions of one event.
- **One forwarder per service** holds a Postgres advisory lock on a dedicated connection,
  outside the pool, and stops forwarding if that connection fails. It reads unpublished
  rows not waiting for a retry (`retry_at` empty or past) in `sequence` order, publishes
  each to JetStream with `Nats-Msg-Id: {id}:{version}`, waits for the acknowledgement,
  and marks the row published. It wakes on `NOTIFY` and polls as a fallback.
- **Order isn't guaranteed.** `sequence` is assigned at insert, not at commit, so rows can
  become visible out of order; nothing downstream relies on order
  ([architecture](../architecture.md#events-and-the-bus)).
- **A crash between publish and mark** republishes the row on restart; JetStream drops it
  inside its duplicate window, and consumer deduplication covers the rest.
- **A row that keeps failing** to publish, for example after the server's limits or the
  stream's subjects change, is retried with a backoff and doesn't hold up the rows behind
  it, since order isn't guaranteed anyway. It's never dropped: the event stays durable
  until a configuration fix lets it publish. The forwarder reports the age of its oldest
  unpublished row and that row's last error.
- **Published rows are deleted** after a retention period set in configuration.
- **`NOTIFY` serializes notifying commits** on a Postgres server. That's fine at our
  volume; polling alone is the fallback if it ever isn't.

### Streams

- Each producing service creates or updates its own stream at start, idempotently: its
  subjects (`github.>` for ingress, and so on), file storage, and a replica count,
  `max_age` and duplicate window from configuration.
- Each consuming service creates its own durable pull consumers at start, retrying until
  the stream exists: a filter subject, explicit acknowledgement, an acknowledgement wait,
  and `max_deliver` unlimited. Attempts are counted in Postgres instead (below), because
  JetStream counts every redelivery, a delayed `nak` included, and silently stops at
  `max_deliver` without a delivery the consumer could park.

### Consumers

- **Version first.** The consumer reads `type` and `version` from the message before
  decoding `data`. A version it doesn't read is acknowledged and skipped, so publishing
  two versions of one event during a breaking change parks nothing.
- **A handler** gets the decoded envelope and the open transaction as a unit of work:
  `async fn handle(&self, uow: &mut UnitOfWork, event: Envelope<T>) -> Outcome`. The
  events crates own the transaction; the handler writes through its service's typed
  operations.
- **One state change per event at a time.** Every transaction that changes an event's
  state (applying, holding, parking, re-running, replaying or discarding it) first takes
  `pg_advisory_xact_lock` on `(consumer, event_id)`, hashed to one `bigint`, so a
  collision only serializes two unrelated events. It's taken before any savepoint and held
  until commit, so a rollback to a savepoint never releases it. Under that lock the
  transaction checks `events.consumed` and `events.held` before it changes anything.
- **Applied once:** under the event's lock the consumer inserts `(consumer, event_id)`
  into `events.consumed`, runs the handler and commits, then acknowledges. A delivery that
  finds the event in `events.consumed` (applied, parked or discarded) or in `events.held`
  (a crash after holding, before acknowledging, or a republish) is acknowledged and
  skipped. A crash before the acknowledgement redelivers, and the row makes the redelivery
  a no-op. Two deliveries of one event at once (a redelivery after the acknowledgement
  wait) serialize on the lock: the second sees what the first committed.
- **Attempts are counted before the handler runs.** Each delivery, and each re-run of a
  released held event, first increments `(consumer, event_id)` in `events.attempts` in a
  transaction of its own and commits it. A delivery that finds the event in
  `events.consumed` is acknowledged as before; otherwise, one that finds the count at the
  configured limit parks the event instead of running it. So a handler that panics, runs
  out of memory or outlives the acknowledgement wait can't make the message loop forever,
  and an event already applied is never parked. The count measures failures only: a
  `Hold` clears it.

| Outcome | What happens |
| ---- | ---- |
| `Done` | Commit, then acknowledge |
| `Retry` | Roll back; record the error with the attempt; `nak` with the next backoff |
| `Hold(key)` | A prerequisite hasn't arrived. Roll back, confirm the hold under the key's lock, store the event in `events.held` and acknowledge it; it's never parked for waiting |
| `Reject` | Roll back, then park |

- **Held events** are re-run when the prerequisite is applied: a handler calls
  `Held::release(&mut uow, key)` in the transaction that applies it.
  - **Holding** discards everything the handler wrote, its `events.consumed` row included,
    by rolling back. In a new transaction the consumer takes the event's lock, then
    `pg_advisory_xact_lock` on `(consumer, key)` (always in that order), sets a savepoint
    and applies the event again from the `events.consumed` insert. If the prerequisite has
    arrived meanwhile, that run returns `Done` and commits as usual. If it returns `Hold`
    again, the consumer rolls back to the savepoint, keeping both locks, stores the message
    in `events.held` under `key`, deletes the event's attempt count and commits, then
    acknowledges. A `Hold` on a different key is confirmed again under that key; any other
    outcome is handled as usual. Handlers take no locks of their own. A deadlock Postgres
    detects between key locks rolls one transaction back, which counts as a `Retry`.
  - **No release is missed.** `Held::release` takes the key's lock and marks the key's
    held rows released. A release that commits before the confirming run takes it is
    seen by that run, since under Postgres's default READ COMMITTED isolation, which
    consumer transactions use, each statement sees what committed before it. A release
    that commits later finds the held row.
  - **Released rows are marked, not deleted.** The consumer runs each released event from
    the stored message as it would a delivery, under the event's lock: the
    `events.consumed` check, the attempt count and limit, and the outcomes above. The row
    is deleted in the transaction that applies, parks or skips the event. Released rows
    left by a crash run again at start. For a re-run, `Retry` leaves the row released with
    a backoff time before its next run, and `Hold` holds it again under its new key,
    confirmed the same way. There's no JetStream message to acknowledge or terminate.
  - **Held events are listable** with `<service> held list|show`, beside parked ones.
- **Parking**, under the event's lock, records the event in `events.consumed` as parked
  and writes the message to `events.parked`, in one transaction, then terminates it in
  JetStream. If the event is already in `events.consumed` (a delivery still running applied
  it, or an earlier parking crashed before terminating) or held, nothing is written and the
  message is acknowledged. The one exception is parking a released event's own re-run,
  which deletes its held row in the same transaction. So an event is never both held and
  parked, and has at most one open parking. A parked event counts as handled, so a later
  republish doesn't run it, until it's replayed or discarded. A message whose envelope
  can't be decoded at all is keyed by its `Nats-Msg-Id`, or its stream sequence if it has
  none.
- **Snapshots:** `Snapshots::apply(&mut uow, &envelope)` records the highest revision per
  `(consumer, type, entity)` in `events.revisions` and tells the handler whether the event
  is newer; older revisions are skipped but still marked consumed.
- **`events.consumed` is kept.** A copy of an event can arrive arbitrarily late: an
  unpublished outbox row is republished whenever its forwarder runs again, however long it
  was down. Deleting old rows needs a retirement rule that covers unpublished rows and
  replays; it waits until the table's size calls for it.

### Parked events

The events crates ship a command each service exposes:
`<service> parked list|show|replay|discard`. `events.parked` keeps a row per parking, as
history.

- **`replay`**, in one transaction under the event's lock, deletes the event's
  `events.consumed` row if it's still parked (otherwise it does nothing), deletes its
  attempt count, records who replayed it and when on the parked row, and stores the message
  in `events.held` as released. The consumer then re-runs it like any released event, with
  a fresh attempt budget; a delivery meanwhile finds the held row and is skipped. If it
  parks again, that's a new parked row.
- **`discard`**, under the event's lock, marks a parked event's `events.consumed` row
  discarded and records who discarded it and why; the event never runs.

This is the phase 1 part of the operations catalog's parked-event call.

### Fault injection

The events crates have a test-only feature with named failure points: after publishing
before marking, after committing before acknowledging, after holding before acknowledging,
and after parking before terminating. The exit test uses them to stop a forwarder or
consumer at exactly those points.

## Python consumer

`contracts/python/consumer/` is the reference for phase 2's Python consumer, with the same
rules: version first, the dedup record committed with the effect (SQLite in the test), the
acknowledgement after commit, attempts counted before the handler runs, and parking. It
uses `nats-py`, which is asyncio-only. The Python service is threaded, so in phase 2 it
runs the consumer on an asyncio loop in a thread of its own.

## Platform

- **Postgres:** `deploy/dev/compose.platform.yaml` runs the version the minimum
  requirements name. `deploy/dev/postgres-setup.sql` creates a role and database per
  service, each role owning only its database, and revokes `CONNECT` from `PUBLIC` on each
  database, which Postgres grants by default.
- **NATS:** the same compose file runs a single NATS server with JetStream on a persistent
  volume. Credentials per service are left until the first service exists; phase 1 uses
  one development account.
- **`docs/platform.md`** states the minimum requirements: the Postgres and NATS releases,
  JetStream on persistent storage, a database and role per service, connection settings,
  and backups. It names no host.

## Job queue

A spike decides between `graphile_worker` and a small queue of our own in
`concurro-events-postgres`. `graphile_worker` deletes a job when it completes, and a job
key only replaces a job that's still pending, so neither makes a job run once. Like an
event, a job runs once because its effect commits with a record of its stable ID. The
spike checks that pattern and the library around it, against a service's own database and
role:

1. A job enqueued with `with_executor` in a transaction that rolls back never runs.
2. A job key replaces a pending job with the same key, rather than adding a second.
3. A job whose effect committed with its stable ID, then crashed before completing, runs
   again and does nothing.
4. Retries back off and stop at a limit, and the job's last error is kept.
5. Its migrations install into a schema of the service's database, repeatably.
6. It runs on the workspace's pinned sqlx and tokio.

If any fails, the fallback is an `events.jobs` table with the same forwarder pattern: a
stable job ID as the primary key, a run-after time, attempts and the last error.

## Exit test

`concurro-testkit` starts Postgres and NATS. A test producer uses the events crates exactly
as a service would. The Rust consumer is built on them; the Python consumer
is the reference consumer above.

| # | Step | Expected |
| ---- | ---- | ---- |
| 1 | Commit a state row and a `github.issues.labeled` event from a fixture | Both consumers apply it once, with every field equal to the fixture |
| 2 | Roll back a transaction that appended an event | Nothing is published |
| 3 | Stop the forwarder after publishing, before marking; restart it within the duplicate window, after it, and after the stream's `max_age` | Within the window the republish is dropped; after it, the stream holds a second copy; after `max_age` the original has expired and the republish is accepted. Each consumer applies the event once in every case |
| 4 | Publish the same event again outside the duplicate window | Two messages on the stream; each consumer applies it once |
| 5 | Stop each consumer after committing, before acknowledging; restart | Redelivered, skipped by deduplication; applied once |
| 6 | Publish an event whose `data` fails to decode, one whose handler returns `Retry` every time, and one whose handler crashes the consumer every time | Each parked once by each consumer at the attempt limit; `parked list` shows them on the Rust side |
| 7 | Publish an event whose handler returns `Hold`, then its prerequisite; the same with the prerequisite committing while the hold is being decided; one held for more prerequisites, in turn, than the attempt limit; and one whose consumer stops after holding, before acknowledging | Held, then applied once after its prerequisites, in every case; never parked; `held list` is empty afterwards |
| 8 | Publish version 2 of an event alongside version 1 | Each consumer applies the version it reads; nothing is parked |
| 9 | Publish 1,000 events from concurrent transactions | Each consumer applies each exactly once |
| 10 | Replay a parked event while a copy of it is delivered; park an event while a slow delivery of it completes; park an event, at the attempt limit, while another delivery is confirming its hold | Each applied once; one parked row for the replayed event, none for the completed one; the held event is held, not parked, and applied once after its prerequisite |
| 11 | Connect as one service's role to another service's database | Refused |

## Work order

Each step is reviewable on its own and leaves CI green.

1. **Workspace and CI.** Root `Cargo.toml`, toolchain, an empty crate, and the `rust` CI
   job.
2. **Contracts.** Envelope, identifiers, the `Contract` trait and registry,
   `github.issues`, `generate`, committed schemas, and the drift check for schemas.
3. **Python models.** `contracts/python/` with its requirements, `generate.sh`, the
   generated models, and the drift check for them. This is where generator options are
   settled.
4. **Fixtures** with their Rust and Python tests, in a `contracts` CI job.
5. **Compatibility check** in CI.
6. **`concurro-db`, `concurro-config`, `concurro-telemetry`, `concurro-testkit`,** with a
   test that writes through two storage crates in one unit of work and rolls both back.
7. **Outbox and forwarder** with fault injection, tested against the stream directly: a
   rolled-back append publishes nothing, and a forwarder stopped between publish and mark
   leaves one message.
8. **Rust consumers:** version first, deduplication, outcomes, held events, parking,
   snapshot revisions, each with tests.
9. **Parked-event command.**
10. **Python consumer.**
11. **Job queue spike** and decision.
12. **Platform:** the compose file, the setup script and `docs/platform.md`.
13. **Exit test,** all eleven steps, in CI.

## Risks

- **Generator mismatches.** Even with `discriminator` set, `datamodel-code-generator` may
  not produce strict types exactly as the schemas intend. Step 3 finds out early; if it
  can't, the Python models are generated with a small post-processing step, tested by the
  same fixtures.
- **Schema modes.** `schemars` can describe a type differently for serialization and
  deserialization (defaults, open enums). Fixtures for both directions catch a mismatch.
- **Forwarder throughput.** Publishing one message at a time and waiting for each
  acknowledgement may be slow. Step 7 measures it; publishing a batch and waiting for all
  its acknowledgements is the fallback.
