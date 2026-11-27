# Plan: phase 2, GitHub ingress

**Status (2026-11-24): implemented and deployed.** See the
[implementation record](../../rust-rewrite-progress.md). The overlap and Python-consumer
steps below are completed migration history; current consumers are Rust services.

Phase 2 of the [architecture review](../architecture.md#migration) moves webhook receipt
out of the Python service into a Rust ingress service. Ingress verifies each delivery,
records it with its raw payload, and publishes a typed `github.*` event through its
outbox. The Python service, list index included, consumes those events instead of
webhooks, and serves ingress the delivery listing and redelivery calls that need the App's
private key. It's also the first production deployment of Postgres and NATS.

**Done when** the [exit test](#exit-test) passes and the service runs on events alone:
every delivery reaches the Python service and the list index through ingress and the bus,
with the routing decisions it would have made from the raw webhook; a failed delivery is
redelivered; events a consumer lost are replayed; and a period nothing can replay is
reconciled. The Python webhook path and its delivery scan are removed.

## Scope

| In | Out |
| ---- | ---- |
| Ingress: receipt, raw payloads, mapping, publishing, recovery, coverage, replays, re-mapping, an operator command | The orchestrator's channels and reconciliation (phase 3) |
| The `github.*` contracts the Python service and list index read, and the `ingress.*` contracts | `github.installation.*` (phase 3) |
| The Python service consuming `github.*` and `ingress.*` | Any change to how the Python service decides what a delivery means |
| The delivery recovery API, served by the Python service | The publisher's delivery recovery (phase 3) |
| Consumer continuity in the events crates | |
| Postgres, NATS and ingress in production, with backups | OpenBao, blob storage |
| The cutover from the Python webhook path to ingress | |

## Decisions

- **Ingress parses deliveries with its own input types, not `octocrab`'s webhook models.**
  `octocrab` (0.54.9) deserializes GitHub's full payloads into complete `Issue`,
  `Comment` and `Repository` models with closed action enums
  (`IssueCommentWebhookEventAction` has no fallback), so a new action or a changed field
  Concurro never reads fails the whole delivery; its own documentation calls webhook
  support beta. Ingress's input types are `serde` structs with only the fields its
  models carry: unknown fields are ignored, actions are read as strings, and only a
  missing or mistyped field it needs fails a delivery. This replaces the
  [architecture's](../architecture.md#services) choice; `octocrab` stays a candidate for
  the publisher's GitHub calls in phase 3.
- **HTTP is `axum`** (0.8.9) on the workspace's `tokio`, and signatures are checked with
  `hmac` (0.13.0) and `sha2`, compared in constant time.
- **The Python service keeps its routing.** Today's `Ingress.handle` verifies a delivery
  and then decides which tasks it wakes from the payload and the registry. The decision
  half moves into `route(delivery, event, payload)`, which both the HTTP path and the bus
  consumer call during the cutover. The consumer rebuilds the payload fields `route`
  reads from the typed event, so a routing difference can only come from a missing
  field, and the [parity test](#parity) finds it before the switch.
- **Deduplication on the Python side stays where it is.** The registry's `deliveries`
  table and the list index's receipts already key on the delivery ID and commit with
  their effects, so a redelivered or replayed event, or one that reaches the service both
  ways during the cutover, wakes nothing twice. The registry never prunes its receipts.
  The list index prunes its receipts after six days, but its rows are ordered by GitHub's
  `updated_at`, so an older delivery replayed after that changes nothing. The consumer
  adds the phase 1 attempt count and parking around them.
- **Re-mapping repairs little for the Python side.** Re-mapping gives a delivery a new
  event ID, but the Python service and list index key on the delivery ID and skip a
  delivery they've already recorded. A delivery that was `unmappable` reaches them for
  the first time, but one they routed with a field missing can't be routed again; the
  parity test is the protection.

## Crates

Each crate has one responsibility and an explicit public API; a crate or module that
passes about 1,000 lines is a prompt to reconsider its responsibilities, not a limit.

| Crate | Responsibility |
| ---- | ---- |
| `concurro-contracts` | Wire types and schema generation (phase 1), plus the `github.*` and `ingress.*` models |
| `concurro-ingress-core` | Input types, mapping, the receipt decision, recovery and coverage policy; typed storage and transport traits. No SQLx, NATS or HTTP |
| `concurro-ingress-postgres` | Migrations, SQL and row mapping; the storage traits' implementations as atomic operations |
| `concurro-ingress` | The binary: HTTP endpoints, background jobs, configuration and process wiring |
| Phase 1's shared crates | Database infrastructure, events, configuration, telemetry, test kit |

The recovery protocol is shared, not ingress's: any producer can serve a journal and any
consumer gets continuity and completeness. Phase 2 adds it to phase 1's crates:

| Shared crate | Adds |
| ---- | ---- |
| `concurro-events` | The recovery state machine (loss, replay, resolution), the replay request and answer types, and the completeness rules |
| `concurro-events-postgres` | Journal positions, retention and selection; publication barriers; durable replay progress; consumer positions and losses beside held and parked events |
| `concurro-events-nats` | Stream identity and generations, positions from publish acknowledgements, and acknowledgement floors from consumer info |
| `concurro-db` | Nothing new: pools, migrations, transactions and savepoints, `UnitOfWork` |

Ingress supplies the replay endpoint, GitHub coverage policy and its reconciliation
signals. The Python service gets the same protocol from phase 1's reference consumer
package.

- **Atomic operations, not repositories that commit on their own.** Recording a delivery
  and appending its event is one operation, `record_delivery`, which commits both or
  neither; advancing coverage with its event is another. Called on its own, an atomic
  operation opens and commits its transaction. Given an existing `UnitOfWork`, it writes
  through it and never commits: the unit's owner does.
- **SQLx stays in storage crates.** Business code receives typed operations or the
  phase 1 [unit of work](phase-1-plan.md#consumers), never a SQLx transaction.
- **Tests follow the split:** unit tests for mapping, receipt decisions and recovery and
  coverage policy against in-memory fakes of the storage traits; Postgres integration
  tests for the operations' atomicity and constraints; and the [exit test](#exit-test)'s
  smaller set of cross-service recovery tests.

## Ingress

### Receipt

`POST /webhook`, one route, routed from the edge's `/concurro/webhook`:

| Delivery | Response | Recorded |
| ---- | ---- | ---- |
| Bad or missing signature | `401` | Nothing |
| Body over 25 MB (GitHub's cap) | `413` | Nothing |
| All eight webhook slots stay occupied for 500 ms | `503` | Nothing; [recovery](#recovery) can redeliver while GitHub retains the failed attempt |
| `ping` | `200` | Nothing |
| Signed, no `X-GitHub-Delivery` | `400` | Nothing |
| Delivery ID already received | `200 duplicate` | Nothing new |
| Postgres unavailable or the commit fails | `503` | Nothing; GitHub records a failure, which [recovery](#recovery) redelivers |
| Signed, from an installation not in configuration | `202` | Receipt and payload, outcome `other_installation` |
| Signed, an event type without a model | `202` | Receipt and payload, outcome `unmodeled` |
| Signed, mapping fails (a field ingress needs is missing or mistyped) | `202` | Receipt and payload, outcome `unmappable` with the error |
| Signed and mapped | `202` | Receipt, payload and the event in the outbox, outcome `published` |

- The slot wait is bounded to 500 ms, then body reading to five seconds. Neither
  deadline cancels an admitted handler or its database commit.
- **The receipt, raw payload and event commit in one transaction** (`record_delivery`,
  with the lock order in [coverage](#coverage)), and ingress answers only after the
  commit. A delivery GitHub saw succeed is therefore
  always recorded; one that timed out on GitHub's side (10 seconds) but committed is
  recorded too, and recovery doesn't redeliver it.
- **Everything signed is answered `2xx`,** unmappable deliveries included: a failure
  status would only make recovery redeliver a payload that fails the same way. Those
  rows are surfaced by `ingress status` and repaired by [re-mapping](#re-mapping).
- **Accepted installations come from configuration,** as today, where the service refuses
  a delivery for a repository it doesn't serve. Within an accepted installation, ingress
  publishes for every repository; filtering is the consumers' decision.

```sql
create table deliveries (
  guid             text primary key,
  event            text not null,
  action           text,
  installation_id  bigint,
  repository_id    bigint,
  received_at      timestamptz not null,
  payload          bytea,           -- the raw body, zstd-compressed; null once pruned
  outcome          text not null,   -- published, unmodeled, unmappable, other_installation
  mapping_revision integer,         -- of the last mapping that published it
  error            text
);
create index deliveries_received on deliveries (received_at);
create index deliveries_outcome on deliveries (outcome) where outcome = 'unmappable';

create table coverage (
  id            boolean primary key default true check (id),
  covered_until timestamptz         -- null until the initial reconciliation is published
);
```

- **Receipts are kept;** payloads are pruned after a retention period from configuration
  (30 days to start), which bounds how far back [re-mapping](#re-mapping) reaches.

### Mapping

- **One module per event type** in `concurro-ingress-core`, each with its input type, its
  model in `concurro-contracts`, and a pure function from one to the other, as the
  [contract](../contracts.md#github-events) specifies. Mapping reads nothing but the
  payload and headers.
- **Every action of a modeled event type is published.** The action is an open enum: the
  actions the contracts name carry their own fields, and any other carries the fields
  common to its type. Today's routing and list index act on any action of most types (a
  PR task wakes on every `pull_request` action; an issue row takes the `updated_at` of any
  `issues` action), so leaving one out would change what the service does.
- **The subject** is `github.<event>.<action>`, or `github.<event>` for events without an
  action (`push`, `status`). The event ID is the UUIDv5 of the delivery ID and the mapping
  revision ([identifiers](../contracts.md#identifiers)); the kind is `fact`.
- **`MAPPING_REVISION`** is a constant in the crate, increased whenever the same payload
  would map to a different event. Each fixture's expected event is pinned with the
  revision that produced it, and an output that differs at the same revision fails CI.
- **Fixtures** for every event type and action the contracts list: a captured payload
  (anonymized) and its expected event. They also feed the [parity test](#parity).

### Publishing

- Ingress owns the `GITHUB` stream: subjects `github.>` and `ingress.>`, with `max_age`,
  replicas and duplicate window from configuration (seven days and two minutes to start).
- Publishing is phase 1's outbox and forwarder, with two additions in the shared events
  crates, since any producer's outbox can serve as a [journal](#replays):
  - **Positions.** When JetStream acknowledges a publish, the forwarder records on the
    row the stream's generation and the sequence the acknowledgement returns. A
    generation is an increasing number the producer assigns to each stream incarnation,
    identified by the stream's creation time as NATS reports it; the forwarder reads it
    at start and again whenever an acknowledged sequence isn't above the last one.
  - **Retention.** Published rows are kept for the replay retention (30 days to start)
    rather than phase 1's shorter default. Pruning records the newest pruned position.

### Recovery

A job in ingress, every five minutes (configurable), replaces the Python service's
delivery scan (`service/webhook_recovery.py`) with the same rules:

1. List the App's retained deliveries through the [delivery recovery
   API](../contracts.md#delivery-recovery), every page (GitHub keeps three days), and group
   attempts by GUID.
2. A GUID is **resolved** if ingress has its receipt or any attempt succeeded, and
   **pending** if any attempt is still in progress.
3. Otherwise every attempt failed: ingress asks for a redelivery of the latest attempt
   and counts the GUID as unresolved. A redelivery that arrives is deduplicated like any
   delivery, so asking twice is harmless.
4. Coverage advances to the scan's start if the scan ends with nothing pending or
   unresolved, and no `unmappable` delivery is waiting: an unmappable delivery is an
   input no consumer has, so coverage stops just before its receipt time until
   [re-mapping](#re-mapping) publishes it ([coverage](#coverage)).

- **Deliveries from before the switch are covered too.** A failed delivery the Python
  service never received is redelivered to the current endpoint, which is ingress; one
  it did receive succeeded and is left alone.
- **Only one instance runs recovery,** holding an advisory lock, like the forwarder.

### Coverage

`covered_until` is the time up to which every delivery GitHub made is recorded by ingress.
Two facts on the `GITHUB` stream report it:

- **`ingress.coverage.advanced {covered_until}`** when it advances. Times here are all
  Postgres's `clock_timestamp()`; the HTTP arrival time isn't used.
  - **Every delivery before `covered_until` is visible.** `record_delivery` first takes
    `ROW EXCLUSIVE` on `deliveries`, then reads `received_at`, and holds the lock until
    the receipt and its outbox row commit. Advancing takes `SHARE` on `deliveries`, which
    waits for every writer already holding the lock; a writer that hasn't taken it yet
    reads its `received_at` after the advance commits, so it's later than
    `covered_until`, which is the scan's start.
  - **Every message before the event is on the stream.** A barrier, in two short
    transactions that never wait while holding a lock:
    1. **Capture**, in the advance's transaction: it also takes `SHARE ROW EXCLUSIVE` on
       the outbox, which waits for every outbox writer (delivery events, coverage facts,
       a replay re-queueing rows) but still lets this transaction append, and stores
       `covered_until` with the highest outbox `sequence` as the pending barrier,
       replacing an older pending one. A gap advance appends its gap facts first, so
       they're under the barrier. Every row at or below it is committed, re-queued replay
       rows included, since they keep their sequence. The commit releases both locks, so
       the forwarder can mark rows published.
    2. **Finalize**, retried each forwarder pass: take `SHARE ROW EXCLUSIVE` on the
       outbox, so no replay can re-queue a row meanwhile; if no row at or below the
       barrier is unpublished, append the coverage event and clear the pending barrier;
       either way, commit at once. A delivery event, a gap or a replayed row that is
       still retrying holds the event back without holding a lock.

    `SHARE ROW EXCLUSIVE` conflicts with itself, so capture and finalization never hold
    it together, and capture takes `deliveries` before the outbox, as `record_delivery`
    does, so nothing deadlocks. The barrier is a `concurro-events-postgres` operation.
  - A consumer whose acknowledgement floor has reached the event's sequence has received
    everything up to `covered_until`; whether it applied it is its own question
    ([list index](#list-index)). Ingress publishes one per successful scan, idle periods
    included.
- **`ingress.coverage.gap {from, to, installation_id}`** for each accepted installation
  when coverage can't advance normally, with `covered_until` set to `to` in the same
  operation:
  - **Initial:** ingress starts with no coverage. Startup doesn't establish it: the Python
    service may hold an unresolved period of its own (`Recovery.poll` reconciles strictly
    when its coverage is missing or stale). The first scan that ends resolved publishes a
    gap from three days before ingress's first start to the scan's start, which makes
    the service run a strict reconciliation of every task, whatever the period.
  - **Expired:** `covered_until` is older than GitHub's three-day history, as after an
    outage of ingress or of the delivery recovery API, a delivery that never stopped
    failing, or an `unmappable` delivery not re-mapped in time. The gap runs from
    `covered_until` to the scan's start; consumers reconcile it, and a later re-mapping
    of the delivery still publishes it.
  - **Declared:** `concurro-ingress gap --from --to`, for a period the operator knows is
    incomplete, such as after restoring ingress's database from a backup, which loses the
    receipts and outbox rows written since.

Both are facts with UUIDv7 IDs.

### Replays

Coverage says what ingress recorded; it can't say what a consumer applied. A consumer that
loses events after they're published (its stream expired them before it read them, or
the stream was replaced) detects it with phase 1's [continuity](#consumer-continuity)
check and asks ingress to replay:

- **The outbox is the journal.** Every message ingress publishes, delivery events and
  coverage facts alike, is an outbox row with the position JetStream gave it. Selection is
  by position, never by a clock, so neither clock skew between hosts nor a row received
  long ago and published late can hide a message.
- **`POST /v1/replays {floor: {stream_created, sequence}, target_created}`** on ingress's
  internal listener, with a bearer per consumer, takes the consumer's acknowledgement
  floor and the target stream's exact creation string. A new target starts a fresh replay
  horizon; a retry for the same target resumes its durable cursor. It re-queues, in batches
  and exactly as first published (same
  message, same event ID), every row whose position is after it: a later sequence in the
  floor's generation, or any position in a later generation. A floor whose generation
  ingress doesn't know replays every retained row.
- **It answers `{replayed_through, unrecoverable}`.** `unrecoverable` is `{before}` when
  the floor is at or below the newest pruned position, or its generation is unknown and
  anything was ever pruned: messages before `before`, the creation time of the oldest
  retained row, may be missing. Otherwise it's `null`. The consumer handles it as a
  [coverage gap](#coverage-gaps).
- **Coverage facts come back with it,** so a lost `ingress.coverage.gap` is replayed and
  its reconciliation runs, and a lost `ingress.coverage.advanced` is replaced: the next
  one's barrier includes every re-queued row, gaps included.
- Repeating a replay is harmless, and it's recorded with its consumer and period for
  `ingress status`.

### Re-mapping

`concurro-ingress remap --from <time> --to <time> [--event <type>]` maps the stored
payloads in that period with the current mapping and appends what it publishes to the
outbox, in batches. An `unmappable` or `unmodeled` delivery that now maps becomes
`published`. Each event's ID includes the new mapping revision, so it's new to consumers
that key on event IDs; the Python service skips a delivery it has already recorded.

### Operator command

`concurro-ingress status` reports `covered_until`, unresolved and pending GUIDs from the
last scan, `unmappable` deliveries holding coverage and `unmodeled` counts since a time,
recent replays, and the age of the oldest unpublished outbox row. `concurro-ingress gap`
declares a gap ([coverage](#coverage)). `concurro-ingress parked ...` is phase 1's command,
for ingress's own consumers if it ever has any.

### Configuration

The phase 1 configuration file plus: the public and internal listen addresses, the
webhook secret file, the accepted installation IDs, the delivery recovery API's URL and
bearer file, the replay bearers, the recovery interval, payload retention, and the
stream's settings.

## Consumer continuity

A generic addition to phase 1's events crates and reference Python consumer, which every
consumer of every stream gets:

- **Each consumer records** in its own database its position: the stream's creation time
  and its acknowledgement floor (the stream sequence at or below which everything is
  acknowledged, from JetStream's consumer info). The first record is written when the
  durable consumer is created, with the floor just below the stream's first message, so
  a loss before anything is acknowledged still has a floor. It's updated about once a
  minute.
- **A loss** is a stream whose creation time changed (it was replaced), or whose first
  sequence is above the recorded floor plus one (messages expired before they were
  acknowledged). The consumer reports it, once, to its service's loss handler with the
  recorded position, and recreates its durable consumer from the stream's first message
  if it's gone. The loss stays open until the handler reports it resolved.
- **The producer defines recovery.** For ingress's stream it's a [replay](#replays). A
  service with no handler for a stream logs the loss and stops that consumer.

## Contracts

Phase 2 makes the `github.*` contracts it publishes and the `ingress.*` ones real; the
[contracts](../contracts.md) list them. Building them against the Python service's reads
found one gap, fixed in the contracts with this plan: `issue_comment`'s `issue` now
carries `created_at` and `updated_at`, which the list index needs to order an issue row
(`service/github_reads.py:181`). The plan also adds `ingress.coverage.advanced` and the
replay API.

Step 2 of the [work order](#work-order) re-checks against the contracts every field read
by `route`, the list index's projections (`projected`, `issue_fields`, `pull_fields`,
`run_fields`), observation expectations (`service/observations.py:8`) and sponsorship
hints (`engine/sponsorship.py:144`), before the models are written.

## Python service

### Routing

- `Ingress.handle` keeps signature checking, parsing and the HTTP replies, then calls
  `route(delivery, event, payload, list_index)`, which holds everything after: the
  repository and installation check, review requests, the list index offer, task keys,
  sponsorship hints and observations, and the registry receipt. `route` returns what
  happened (`accepted`, `duplicate`, `ignored`) instead of an HTTP status.
- **The HTTP path passes the list index,** so its offer is made and its errors swallowed
  as today. **The bus consumer passes none** and applies the list index itself, after
  `route`, so the registry still records a review request before the list index sees its
  PR.
- **The adapter** (`service/bus.py`) turns a decoded event into the payload `route` reads:
  the same keys, GitHub's nesting, and `"pull_request": {}` on an issue that's a PR. It
  builds only what the contracts carry; a key `route` reads that the adapter doesn't
  build is a parity failure, not a default.

### Consumer

- **One durable consumer, `service`,** on `github.>` and `ingress.>`, from phase 1's
  reference Python consumer: version first, attempts counted before the handler,
  parking and continuity. Its attempt, parking and continuity records are in their own
  SQLite file in the service's state directory; deduplication of deliveries is the
  registry's and list index's.
- **It runs on an asyncio loop in a thread of its own,** since `nats-py` is asyncio-only
  and the service is threaded, and calls `route` and the list index on that thread.
- **Outcomes:**

  | Case | Outcome |
  | ---- | ---- |
  | `route` and the list index both succeed | `Done` |
  | A registry error, or a list index storage error | `Retry`; both deduplicate by delivery ID, so the retry repeats nothing |
  | The list index can't project the delivery | The list index records the receipt and schedules a rebuild of that repository's list in one transaction, as `check` does today; then `Done` |
  | The adapter can't build a payload from the event | `Reject`, parked |

- **A loss** on the `GITHUB` stream calls ingress's replay API with the recorded
  position, and treats an `unrecoverable` part as a coverage gap. The loss is resolved once
  that reconciliation has finished and the acknowledgement floor has reached a coverage
  event created after the replay.
- **`python -m service parked list|show|replay|discard`** is the Python side of the
  parked-event command.
- **Settings:** the NATS URL and credentials file, ingress's replay URL and bearer file,
  and the consumer's attempt limit and backoff. With no NATS URL the consumer doesn't
  start, which is how the first release ships before the platform exists.

### List index

- **`apply(guid, event, repository, payload)`** is a new synchronous entry point for the
  bus path: it waits until the writer has applied the delivery and raises the writer's
  storage error. `offer` stays for the HTTP path until release 2.
- **Freshness comes from coverage events.** Today a kind is `current` only after a history
  check completed in this process and while its `verified` time is within ten minutes
  (`revisions`, `service/list_index.py:860`). On the bus path, the consumer records each
  `ingress.coverage.advanced` with its stream sequence, and the event is **applied** once
  all of these hold:
  - the consumer's acknowledgement floor has reached it, in the same stream generation
    (a coverage event from an earlier generation never counts after a replacement);
  - the consumer has no parked or held event at all, whatever its position or
    generation: parking terminates a message, which moves the floor without applying it,
    and a replayed copy of a parked event is acknowledged by deduplication while the
    event stays parked. One stops blocking once it's replayed and applied, or discarded
    by an operator;
  - the consumer has no open [loss](#consumer-continuity).

  Then every delivery up to its `covered_until` is applied, since ingress doesn't advance
  coverage past an `unmappable` one, so the list index sets `verified` to `covered_until`
  for every kind and marks itself checked for this process. That's what `check` did with
  a completed scan. A projection failure doesn't block it: its scheduled rebuild keeps
  that kind `rebuilding` until done. Idle periods are covered because ingress publishes
  one per scan; after a restart a kind is `catching-up` until the first one is applied.
- **What else `check` did moves:** a kind not verified within GitHub's history rebases
  when its `verified` is next set; receipt and change cleanup runs on its own timer, with
  the retry of failed reads (`deferred`). State-change notifications for the feed stay on
  every `verified` update.
- **`check`, `offer` and the payload fetches are removed** in release 2.

### Delivery recovery API

The [bridge](../contracts.md#delivery-recovery) on the service's internal listener,
beside `/actions` and `/lists`, with its own bearer file. The edge routes only
`/webhook` to the service, so it isn't public. `GET /v1/deliveries` pages through
`app/hook/deliveries` with today's cursor checks (`next_page`), and
`POST /v1/deliveries/{id}/redeliver` posts the attempt. Both use the App's JWT, as
`Recovery` does now.

### Coverage gaps

On `ingress.coverage.gap`, or an unrecoverable replay, the service does what a stale
coverage check does today: a strict reconciliation of every task
(`reconciler.poll(strict=True)`) and a new list index baseline (`rebaseline`). The
consumer records the gap's event ID in its SQLite file once both finish; a repeat after a
crash only reconciles again, which changes nothing.

### What else `Recovery` did

`Recovery.poll` also runs the policy observer and runner cleanup on its timer. Those move
to a timer of their own when the delivery scan is removed.

## Platform

- **Postgres and NATS** run as a `concurro-platform` compose project on `<host>`, on a
  private `platform` network, with persistent volumes. Each is reachable only from that
  network. Their minimum requirements are phase 1's `docs/platform.md`.
- **Roles and credentials:** an `ingress` Postgres role and database. NATS gets an account
  with a user per service: `ingress` may publish on `github.>` and `ingress.>` and manage
  its stream; `service` may only create and read its own durable consumers on it.
- **Ingress** joins the service's compose project (`ghcr.io/concurro/ingress`), on the
  edge network for Caddy and the platform network, with the same hardening as the service
  container (read-only root, no capabilities). Its internal listener (replays) is on the
  platform network only. CI builds and publishes its image with the others;
  `deploy/releases.py` and `update-concurro.sh` deploy it.
- **Backups:** nightly `pg_dump` of every database, kept with the host's other backups.
  Restoring ingress's database is followed by `concurro-ingress gap` from the backup's
  time to the restore. JetStream isn't backed up: a lost stream is a
  [continuity](#consumer-continuity) loss, which replays from the outbox and
  reconciliation cover.
- **Secrets:** the webhook secret moves to `/etc/concurro/ingress/webhook-secret`; the
  Postgres password, NATS credentials and replay bearers are files beside it. The service
  gets its NATS credentials, its replay bearer and the delivery recovery API's bearer; its
  webhook secret is removed in release 2.
- `gateway/README.md` and `docs/platform.md` describe the deployment, naming the host only
  as `<host>`.

## Cutover

No GitHub App setting changes: the App keeps delivering to `/concurro/webhook`, and the
edge's route decides who answers.

1. **Release 1 (Python):** `route`, the consumer, the adapter, the delivery recovery API,
   the list index's `apply` and coverage-based freshness, and gap and loss handling. The
   webhook path, the delivery scan and `check` are unchanged. The consumer starts once
   its settings name NATS.
2. **Platform and ingress:** deploy Postgres, NATS and ingress; ingress isn't routed yet.
   A signed test delivery to ingress directly, for a repository the service doesn't
   serve, appears on the bus and is ignored by the service's repository check.
3. **Switch:** the edge's `/webhook` route points at ingress. Deliveries already in
   flight finish on the service; later ones arrive through the bus. A delivery that
   reaches the service both ways is deduplicated by its delivery ID.
4. **Check:** request a redelivery of a recent delivery; ingress records it, the service
   logs it as a duplicate. Ingress's initial gap is published, and the service records
   its strict reconciliation as finished. `ingress status` shows coverage advancing.
5. **Release 2 (Python),** only after step 4's reconciliation is recorded: remove the
   webhook path and its secret, the delivery scan, `check` and `offer`. Until it ships,
   both the service and ingress may ask for the same redelivery, which is harmless.

Roll-forward only: a fault after the switch is fixed in ingress or the service, not by
pointing the route back.

## Exit test

Automated, against Postgres, NATS, ingress and the Python service, with GitHub's delivery
API faked behind the delivery recovery API:

| # | Step | Expected |
| ---- | ---- | ---- |
| 1 | Send every fixture delivery, signed | Each published once; the service's routing decisions match the [parity](#parity) record |
| 2 | Send a delivery with a bad signature, an oversized body, and from another installation | `401`, `413`, `202` with nothing published |
| 3 | Send a delivery whose mapping fails, then fix the mapping and `remap` | `202`, recorded `unmappable`; coverage stops before it and lists aren't `current`; after `remap`, published once with the new revision and coverage advances |
| 4 | Send the same delivery twice, and as a redelivery after its event was consumed | One event; the service wakes its tasks once |
| 5 | Stop Postgres, send a delivery, restart; run recovery with the fake listing showing it failed | `503`, then redelivered once and processed once |
| 6 | Stop NATS for ten minutes while deliveries arrive | All answered `202`; published after NATS returns; each processed once |
| 7 | Stop the service's consumer, send deliveries, restart it | Processed once each, list index rows included |
| 8 | Crash the service after the list index applies a delivery, before acknowledging; then make the list index's writer fail | Redelivered and applied once; the failure retries and applies once the writer recovers |
| 9 | Send a delivery the list index can't project | Acknowledged; a rebuild of that list is scheduled and runs |
| 10 | Start ingress for the first time; advance the clock past three days with recovery failing | One initial gap, then one expired gap; one strict reconciliation and list index rebaseline each |
| 11 | Stop the service's consumer past the stream's `max_age` (shortened for the test) while deliveries arrive; restart it | A loss; a replay; each delivery processed once, with nothing unrecoverable |
| 12 | Delete and recreate the `GITHUB` stream; then do it with the floor older than the outbox's retention; then do it before the service has acknowledged anything | A loss and a replay each time; the second also an unrecoverable part, handled as a gap |
| 13 | Hold an older delivery's outbox row unpublished until a newer event is acknowledged, publish it, then delete the stream before it's consumed | The replay includes the older row; processed once |
| 14 | Publish a coverage gap, then delete the stream before the service applies it | The replay includes the gap; one strict reconciliation runs |
| 15 | Park an event, then let a coverage event be acknowledged; then replace the stream, let the replay re-deliver the parked event and a new coverage event; then replay the parked event from the parked-event command | Lists stay `catching-up` past both coverage events, in either generation, until the parked event is replayed and applied; then `current` |
| 16 | Run ingress with its clock two minutes behind NATS's, then lose the stream | The replay includes every unconsumed message |
| 17 | Replay a gap and deliveries where the gap's publish keeps failing while the deliveries publish, with the forwarder, finalization and replays on separate connections | The forwarder marks the deliveries published; no coverage event until the gap is published; the loss stays open until the gap's reconciliation finishes |
| 18 | Pause a delivery writer just before it takes its lock, and another just after, while coverage advances | The first gets a `received_at` after `covered_until`; the advance waits for the second, which it covers |
| 19 | Leave the service idle for an hour; restart it with a backlog | Lists stay `current` while idle; after the restart they're `catching-up` until the backlog and the next coverage event are acknowledged, then `current` |
| 20 | Send a delivery to the service's HTTP path and the same delivery through ingress | Processed once (the cutover overlap) |

In production, after the switch: `ingress status` shows `covered_until` within one
recovery interval, no `unmappable` deliveries for a week, and the dashboard and lists of
active tasks behave as before.

### Parity

A test runs `route` twice per delivery, once on the raw payload and once on the adapter's
payload from ingress's mapped event, with a recording fake of the registry and list
index, and requires identical calls: task keys, receipts, sponsorship hints,
observations, review requests and list index projections. It runs on the committed
fixtures in CI, and once before the switch on a local corpus of the last three days'
real deliveries, fetched through the delivery listing and not committed.

## Work order

1. **Ingress crates' skeletons:** `concurro-ingress-core`, `-postgres` and the binary;
   configuration, the `axum` listeners, the signature check and `record_delivery` without
   mapping.
2. **Field check and models:** the field re-check above, then the `github.*` and
   `ingress.*` models in `concurro-contracts` with their schemas, Python models and
   fixtures.
3. **Mapping** per event type, `MAPPING_REVISION` and its pinning test.
4. **Publishing** through the outbox; the `GITHUB` stream.
5. **The shared recovery protocol** in the events crates and the reference Python
   consumer: journal positions and retention, consumer positions and losses, replay
   selection and completeness.
6. **Python `route` split,** with the existing ingress tests passing unchanged.
7. **Adapter and parity test** on the fixtures.
8. **Delivery recovery API** in the Python service.
9. **Recovery and coverage** in ingress, against a fake of that API.
10. **Replays, `remap` and `status`.**
11. **Python consumer:** the consumer thread, outcomes, the list index's `apply` and
    coverage-based freshness, gap and loss handling, and the parked-event command.
12. **Platform for production:** compose, roles, NATS accounts, backups, image build and
    deploy scripts, docs.
13. **Exit test** in CI.
14. **Release 1, platform, switch, release 2,** as in the [cutover](#cutover), each
    deployed with the user's authorization.

## Risks

- **A field the service reads isn't in a model.** After the switch it can't be repaired
  for the Python service by re-mapping. The field check, the parity test on real
  deliveries and the release 1 overlap (where the HTTP path still works if the switch is
  delayed) are the protection.
- **The edge's other routes.** Ingress is the first Rust container behind the shared
  Caddy; a route mistake affects only `/webhook`, and step 2 of the cutover tests ingress
  before it's routed.
- **First production Postgres and NATS.** Their failure stops new deliveries from being
  answered (Postgres) or consumed (NATS), not lost: GitHub records the failures, and
  recovery, replays or a gap cover them.
