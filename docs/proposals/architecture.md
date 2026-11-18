# Proposal: architecture review

**Status: proposal, not implemented.**

Concurro was built quickly as an MVP: task state is stored on GitHub, inputs are re-read
from GitHub on every tick, and each component keeps its own SQLite files. New features
now spend much of their effort on keeping those stores consistent. This review describes
the current architecture, where it costs us, and a target built on PostgreSQL and an event
bus, split into services along domain and trust boundaries. The migration path lines up
with porting the services to Rust.

Line counts are non-test Python from `wc -l` at the time of writing, over the files
named. Per-tick read counts are REST and GraphQL calls inside `Controller.step` for an
idle task waiting on one CI ref, without caching; the worker's snapshot before it adds
more.

## Today

### Processes

| Process | Role | Stores |
| ---- | ---- | ---- |
| Edge proxy | TLS and routing for every public path | None |
| `service` | Webhook ingress, scheduler and workers running the engine, GitHub write gateway, list index, backoffice endpoints | `registry.db`, `list-index.sqlite3`, `diagnostics.sqlite3` and blobs, transcripts and grant files |
| `exchange` | Runner sessions, turn queue, inference attribution, forwarding personal and relay inference | `exchange.db`; reads agentgateway's `requests.db` |
| agentgateway | Inference proxy with per-key budgets | `agentgateway.db`, `requests.db` |
| `vault` | Encrypted provider credentials and relay approvals | `vault.db` and its keyring |
| `backoffice` | Signed-in API for the web app | `issue-reads.sqlite3`, `logout.db`; reads `diagnostics.sqlite3` |
| `web` | Static frontend | None |

Model turns and builds run on GitHub-hosted Actions jobs.

### Where task state lives

- **GitHub is the system of record.** Each task's state is a mutable "audit" issue
  artifact, with reports in immutable artifacts and a dashboard comment pointing at an
  audit revision (`engine/checkpoints.py`, `engine/report_store.py`). The service design
  lists task state, approvals and outboxes under "GitHub audit and immutable report
  artifacts; verified by the engine"
  ([service](../design/service.md#components-and-state)).
- **Inputs come from GitHub too.** Commands are `/concurro` comments above a
  `command_id` high-water mark; enrollment is re-derived from label events; backoffice
  commands are posted as signed App comments so the engine can read them back
  (`engine/delegation.py`).
- **Nine SQLite databases sit around it.** The registry alone has 21 tables, several of
  which can't be rebuilt from GitHub (write intents, funding policy receipts, sponsor
  source revisions, standalone-review enrollment).

### How a tick works

1. A webhook, timer, poll or observation queues the task in the registry.
2. The worker takes ownership (a generation and a lease) and snapshots the task's GitHub
   inputs: issue, all comments, label events, PR, reviews, threads and CI.
3. The engine loads and verifies the audit artifact, then re-reads what it needs: an idle
   task waiting on CI makes roughly 20–28 GitHub reads per tick
   (`Controller.step`, `engine/controller.py:2404`).
4. `Controller.save` writes the audit (read, compare, write, read back), then runs the
   conversation, notice and PR-review outboxes and republishes the dashboard
   (`engine/controller.py:408`).
5. Every GitHub write goes through the service's mutation gateway, which records an
   intent before sending and stops the task on an unknown outcome until an operator
   resolves it (`service/gateway.py:426`).

## Where it costs us

### Consistency machinery

Most of it exists because state lives on GitHub, which has no transactions, and because
the stores can't share one.

| Mechanism | Where | Approx. lines |
| ---- | ---- | ---- |
| Audit and report storage: verified loads, read-back writes, sticky uncertainty, overflow envelopes, history blocks, frozen legacy renderers | `engine/checkpoints.py`, `report_store.py`, `report_artifact*.py` and related | 3,400 |
| Outbox reconciliation: marker and exact-body recovery for comments, reviews, edits, check runs | `engine/conversation.py`, `reviews.py`, `pending_reviews.py`, `review_checks.py`, `actions.py` | 1,200 |
| GitHub write intents, settlement and operator recovery | `service/gateway.py`, `settlement.py`, `review_*.py`, registry methods | 900 |
| Input diffing: acknowledged and deferred snapshots, observation expectations | `service/registry.py`, `worker.py`, `observations.py` | 300 |
| Backoffice read model, rebuilt from GitHub and webhooks | `service/list_index.py` | 1,050 |
| Backoffice caching and freshness | `backoffice/read_cache.py`, `live.py`, `tasks.py`; frontend `cache.svelte.ts` and freshness banners | 1,000 |
| Sponsorship kept in three copies (checkpoint, registry, exchange) and fenced between them | `service/shadow.py` | Part of 1,340 |

That is about 8,000 lines of Concurro's 38,600 lines of Python, counted over whole
files, which also hold code that stays; the frontend's cache and freshness files add
about 220 lines of TypeScript. We estimate that about 5,000 of those Python lines are
recovery or compatibility code that exists only because of the storage model; rendering
projections, for example, would remain under any design. Tests follow the same shape:
the engine's storage, recovery and publication test files hold about 7,500 lines.

Not all of it disappears. Local transactions eliminate checkpoint storage, read-back
verification, input diffing and the fence between sponsorship copies. Coordination
remains where systems fail independently: GitHub publication, grant admission, credential
writes across funding and OpenBao, and runner dispatch. The target is judged by simpler
invariants and fewer uncertain states, not by lines deleted.

### Symptoms

- **Format sprawl.** Checkpoints are readable in formats 2 to 10, chosen implicitly on
  save from which keys are present (`engine/checkpoints.py:270`), alongside conversation
  event formats 1–6, review render formats 1–5 and a frozen v1 artifact renderer.
- **Size pressure.** The audit targets 60 KB, so the controller trims metadata before
  saving and spills overflow to an envelope (`engine/controller.py:382`).
- **Expensive reads.** Artifact listings use a page size of 1, one request per artifact,
  wherever no pointer is known (`engine/protocol.py:11`, `engine/github.py:172`). Every
  tick re-reads every comment.
- **Commands through comments.** The backoffice posts a GitHub comment to issue a
  command, then waits for an observation job to see it; a lost response leaves it
  answering "may or may not have been posted" (`backoffice/commands.py:14`).
- **Recovery in several copies.** Delivery-history repair exists in the registry and again
  in the list index. The exchange and the service each follow the vault's event feed with
  their own cursor. Sponsorship has a registry projection and checkpoint fields fenced
  against each other.
- **Polling between services.** Runners and the service long-poll the exchange, which
  serves both with 1-second sleep loops (`gateway/exchange/exchange.py:1049`, `:1370`);
  the exchange reconciles against the vault and agentgateway every 60 seconds.
- **Uneven durability.** The registry runs SQLite with WAL and full sync; `exchange.db`
  sets neither and mostly autocommits. Its `sessions` table stores live gateway keys and
  runner tokens in plaintext.
- **Funding logic in five places.** The vault stores credential order and filters; the
  service and engine select credentials and handle fallback; the exchange keeps its own
  sponsorship tables and re-checks admissions; agentgateway enforces token budgets.
  Sponsorship state exists in three copies.
- **One process, many domains.** `service` hosts ingress, scheduling, task logic, all
  GitHub writes, the list index, diagnostics and backoffice endpoints in one process with
  one registry.

### Why it matters now

Each new feature has to fit this model. The recent [request intake](request-intake.md)
and [mentions](mentions.md) drafts both ended up rebuilding a request table, an event log
and projections on top of SQLite and checkpoint artifacts. Those are the primitives a
database and a bus provide directly.

## Constraints

- **Services along real boundaries.** A service exists where it owns a domain or holds a
  credential others mustn't. No monolith, and no service split for its own sake.
- **Rust end state.** Every service is eventually written in Rust. Contracts between
  services must not depend on Python.
- **Roll-forward only.** Live tasks move by forward migration; nothing is rolled back
  ([AGENTS.md](../../AGENTS.md)).
- **Keep the trust boundaries that exist.** Provider secrets never reach runners; only
  the inference path can read personal secret material, which stays encrypted under a key
  held outside the database.
- **GitHub stays the user interface.** Issues, comments, reviews and PRs remain where
  people work with Concurro.

## Target architecture

### Principles

- **Postgres is the system of record** for every service's state.
- **GitHub is an input and a projection.** Webhooks are inputs; dashboards, notices,
  reviews, labels, PRs and artifacts are written from saved state and never read back as
  truth.
- **Commands, events and queries are kept apart (CQRS).** A command (a request) goes to
  the one service that owns the state, through its API, and is accepted or refused
  there. An event is a fact, published after the change commits, for any service that's
  interested. A query reads a model its owner built from events. Decisions that gate
  spending, such as a grant, are direct calls to their owner, answered from what it has
  committed; events report them afterwards.
- **Services talk through events and narrow APIs,** never through each other's
  databases.
- **A state change and the events it causes commit together** (a transactional outbox),
  so no cross-store recovery is needed inside one service.

### Services

| Service | Domain | Trust boundary | Owns |
| ---- | ---- | ---- | ---- |
| GitHub ingress | Webhook receipt: signature check, deduplication and delivery recovery; maps each delivery to a typed GitHub event | Public endpoint; holds the webhook secret only; asks the publisher for redeliveries | Delivery receipts, raw payloads, recovery coverage, the GitHub event models |
| Orchestrator | Tasks: requests, enrollment, stages, plans, findings, and who consented to pay (sponsorship, funding choices, the funding boundary); turns webhooks it cares about into requests; asks funding for a grant per attempt | Authority on task consent; reads repositories with read-only tokens the publisher issues; no GitHub write access | Tasks, requests, consent logs, task events |
| Funding | Whether and how consent is charged: users' credential entries, lists and filters, the route catalog, selection, fallback and recovery, call and token budgets, grants and charges | Payment authority; hands new secret material to the secret store on save but can't read it back | Credential metadata, grants, consent cutoffs, the charge ledger |
| GitHub publisher | Projects events onto GitHub: dashboard, notices, replies, reviews, threads, labels, check runs, branches and PRs; creates run branches and dispatches runner workflows for the exchange; lists and redelivers webhook deliveries for ingress | In the target, sole holder of the App's private key, which mints its write tokens and the orchestrator's read tokens, and of the reviewer account's token; the Python service keeps its existing App signing credentials until it retires in phase 4 | Write intents and their outcomes |
| Exchange | Runner sessions, turn queue, turn results, transcripts and turn diagnostics; publishes turn results as events | Runner OIDC; issues session keys bound to a grant | Sessions, turns |
| Inference gateway | Authenticates session keys, enforces token budgets, routes and forwards to providers or the relay verifier, records usage | Org provider secrets; reads personal secret material | Inference requests and their usage |
| Relay verifier | `concurro-relay`'s hosted verifier: verified inference through sponsors' relays | Public relay listener; relay TLS key and sponsor registrations; native MPC runtime | Relay registrations and sessions |
| Secret store (OpenBao) | Personal credential material, versioned and destroyable | Unseal key outside the database; a policy per client service | Encrypted material |
| Backoffice API | Signed-in users, their views and their requests | User sessions and OAuth tokens | Sessions, a read model fed by events |

- **Ingress publishes typed GitHub facts; consumers decide what they mean.** Ingress
  checks the signature, records the delivery ID so a redelivery publishes nothing new,
  and maps the payload to a typed GitHub event published to `github.<event>.<action>`.
  It answers GitHub once the receipt and its event are committed. Any service may
  subscribe.
  - **The GitHub event models belong to ingress.** They keep GitHub's meaning and carry
    only the fields Concurro uses (IDs, logins, author association, bodies, labels,
    timestamps), so consumers never parse webhook JSON. A delivery type without a model
    is recorded and not published.
  - **Ingress parses deliveries with `octocrab`'s webhook models** (0.54.9, October
    2026) and maps them to its own. `octocrab`'s types stay inside ingress: they're
    Rust-only, change with its 0.x releases and mirror GitHub's full payloads. The
    publisher uses the same crate's delivery listing and redelivery calls.
  - **Mapping is stateless.** Anything that needs another service's state (is the issue
    enrolled, is the author a writer, is this comment a command) is the consumer's
    decision.
  - **Raw payloads are kept** for a retention period, so a model that gains a field can
    be backfilled by re-mapping and republishing ([below](#events-and-the-bus)).
- **Channels turn GitHub events into requests.** The orchestrator's GitHub channels
  consume the events they care about (`/concurro` comments, labels, assignments, review
  requests, mentions) and send requests to the orchestrator's request API; the
  backoffice API calls the same API, as a future Slack adapter would. Channels may read
  the orchestrator's task context to interpret a request but never change it, and task
  logic only sees typed requests ([request intake](request-intake.md)).
- **The orchestrator decides who pays; funding decides whether and how.** Task consent
  (sponsorship requests, the consent log and the funding boundary) depends on the task's
  mode, enrollment and comments, so it stays with the task
  ([request intake](request-intake.md#applying-requests)). The orchestrator is the
  authority on who consented, and funding trusts its consent decisions: channels run in
  the orchestrator and authenticate actors there. A funding service that verified
  consent evidence itself could add protection, but this design doesn't claim it.
  - **Grants.** For each attempt the orchestrator sends funding a frozen consent
    decision (task, provider slot, sponsor, funding mode, consent revision, boundary and
    an evidence reference). Funding selects a credential and sets a budget:
    - **Implicit fill** (the initiator filling an empty slot) uses the initiator's
      credentials as they stood at the boundary, so a credential added later never
      makes an earlier `start` chargeable.
    - **A new attempt under existing consent** selects among the sponsor's credentials
      eligible now, so a replacement credential and same-sponsor fallback work
      ([ordered credentials](credential-list.md)).
    - **A replayed attempt** returns its original grant, credential version and current
      revoked status; a different request reusing the attempt is refused.
  - **Withdrawal.** The orchestrator commits a withdrawal with a revocation job, retried
    until acknowledged. Funding keeps a cutoff per task, provider slot and sponsor, as a
    counter that only increases and covers every earlier grant for that slot and
    sponsor; it checks the cutoff in the same transaction that inserts a grant, so a
    delayed request is refused and another provider's or a later sponsor's grant isn't.
    Grants already issued are revoked through `funding.*` events, and spending has
    stopped once the gateway publishes `inference.binding.closed` for each affected
    binding (and the exchange has released any relay session) or the binding has
    expired.
  - The orchestrator acts on `funding.*` events, such as a revoked grant or a credential
    that's usable again. The exchange binds sessions to grant IDs; the inference gateway
    reports usage as events, which funding charges. The route catalog is defined once, by
    funding, as a contract the gateway and the backoffice build from. A sponsor approves
    a relay's registration code in the backoffice; funding, as the vault does today,
    calls the verifier's owner endpoints, records a relay-custody credential version that
    selection treats like any other, and serves relay status to the backoffice
    ([sponsor relay](sponsor-relay.md)).
- **Secret material lives in OpenBao.** Each credential version is its own KV path, named
  by a version ID funding assigns. Funding writes and destroys versions; the inference
  gateway reads one exact version per request; each signs in with its own role and
  policy, and every read is audited. Static-key auto-unseal keeps the unseal key in a
  file outside the database, and OpenBao stores its data in its own database on the
  shared Postgres server. Both clients use it through a small interface (with the
  `vaultrs` client), so another store can replace it.
  - **New material goes backoffice API → funding → OpenBao;** the backoffice already
    proxies it to the vault today.
  - **Writes are idempotent across the two stores.** Funding records the version as
    pending, writes it with check-and-set so its path can be created only once, then
    marks it active. A retry writes the same path; funding settles a pending version by
    reading OpenBao's metadata, which its policy allows, never the material.
  - **The gateway can read any version,** as the exchange's resolver bearer can today; it
    reads only the version a session's grant names. Scoping reads per grant (a
    short-lived token or response wrapping issued at session bind) is a later
    hardening. Relay sponsors' tokens never enter OpenBao, so this access doesn't reach
    them; the verifier's own admission check protects them.
- **Our own inference gateway replaces agentgateway.** agentgateway authenticates session
  keys, blocks once a token budget is spent, routes on Concurro's headers and injects org
  secrets; personal and relay traffic pass through the exchange's own forwarder behind
  it, which for relay custody consumes a one-use request nonce, rechecks the exact relay
  record and turn authority, and forwards to the verifier (integrated and tested, not yet
  deployed). The gateway does all
  of it in one hop, parses usage with the Rust `provider-protocol` crate from
  `concurro-relay`, and can stop a stream when the budget runs out. Every route is native
  passthrough today, so no translation is lost.
- **How the gateway authorizes a request.** The exchange publishes each session binding:
  the key's hash, the grant, its credential version, allowed models and token budget.
  The gateway checks the key against it, enforces the budget on the hot path and
  publishes `inference.usage`; funding sets budgets and keeps the ledger.
  - **Bindings end.** Each binding has a hard expiry. Session end, grant revocation and
    credential-version revocation reach the gateway as events; it drops the binding and
    publishes `inference.binding.closed`, and a revocation is effective from that event.
    The gateway keeps revoked grants and versions on record and refuses a binding that
    arrives after their revocation. Usage before it is still recorded and charged, and
    the expiry bounds the exposure if an event is missed.
- **The gateway and the relay verifier stay separate processes.** The gateway faces
  runners, which run code we don't control; the verifier holds the relay TLS key and
  sponsor registrations, serves its own public listener and runs native MPC code. The
  relay keeps sponsors' credentials out of Concurro; merged, one gateway exploit would
  still reach sponsors' subscriptions through their relays, runner load could starve MPC
  bootstraps, and every gateway deploy would drop all relay connections.
  - **The verifier's private API gets separate credentials:** one for owner endpoints
    (registration approval and status), held only by funding (the vault holds it today);
    one for inference, held only by the gateway; and one for `/release`, held only by the
    exchange. Today one bearer covers all of them (`crates/hosted/src/main.rs` in
    `concurro-relay`).
  - **The verifier checks admission itself.** It trusts the session, turn and deadline
    its caller sends (`lease`). Today that caller is the exchange, which checks them
    first; with the gateway as the caller, a compromised gateway could invent a session
    and spend a sponsor's subscription. The exchange signs a claim per session: the relay
    record and version, route and models, session, expiry, and a call and token
    allowance. The verifier checks the signature with the exchange's public key and
    enforces the claim's scope, expiry and allowance. `/release` either cancels one turn,
    as today, or revokes a whole session claim; revoked claims and spent allowance are
    stored, survive a verifier restart and are kept until the claim expires (today's
    release list is per turn and in memory, `pool.rs`). The gateway doesn't become the
    verifier's caller without it. Today's arrangement is a separate one: the exchange's
    forwarder consumes its own one-use nonce and checks turn and session authority
    before it calls the verifier, so it deploys on its own schedule.
- **Splitting the orchestrator and publisher** separates task logic from GitHub write
  credentials, and isolates the remaining unknown-outcome handling in the one service
  that writes to GitHub. A lost projection write then delays display; it can't corrupt
  task state.
  - **One write per surface at a time.** The dashboard, each notice and each finding
    thread is a numbered snapshot of what it should show, so resolving a notice is a
    newer snapshot of it. The publisher settles an uncertain write before sending that
    surface's next one, and writes the newest snapshot, skipping intermediate ones. A
    promised reply or audit notice has its own publication record and is never
    skipped.
- **The backoffice read model replaces the list index.** It moves to the backoffice's own
  domain and is fed by task events, not rebuilt from webhooks and GitHub reads. The
  backoffice's GitHub caches go away with it.
- **Diagnostics** become turn records in the exchange, which already sees every turn,
  published as `turn.*` events for the backoffice read model.
- **Offline tools** such as retrospective collection read events and APIs; they aren't
  services.
- **Event plumbing is a shared library, not a service.** Every service links the same
  events crate, which runs the service's outbox forwarder and its subscriptions as
  background tasks in the service's own process, under its own database role. No
  separate component reads another service's outbox.

Tasks and requests:

```text
 GitHub ──webhooks──▶ Ingress ──github.*──▶ Orchestrator ──task events──▶ Publisher ──▶ GitHub
                                              ▲    │  ▲
                                     requests │    ▼  │ grants, funding events
 Backoffice API ──requests────────────────────┘   Funding
       │                                            ▲
       └─────────────── credentials ────────────────┘
```

Inference:

```text
 Runners ◀──turns──▶ Exchange ──session keys──▶ Inference gateway ──▶ Providers
    │                                            ▲   │   │   │
    └───────── inference, session key ───────────┘   │   │   └──▶ Relay verifier ──▶ Sponsors' relays
                                                     │   │
 Funding ◀────────────── usage events ───────────────┘   │
    │                                                    │ read exact version
    └──write, destroy──▶ OpenBao ◀───────────────────────┘
```

Events (`github.*`, `task.*`, `request.*`, `funding.*`, `turn.*`, usage) travel through
the bus ([below](#events-and-the-bus)); the backoffice read model follows task, request,
funding and turn events. Requests go to the orchestrator's request API, grant requests
and withdrawals from the orchestrator to funding's, and secret material through
OpenBao's. Not drawn: the orchestrator submits turns to the exchange and learns
results from `turn.*` events; the publisher dispatches runner workflows for the exchange
and redeliveries for ingress; funding calls the verifier's owner endpoints.

### Data

- **A database per service, on one shared Postgres server.** Each service connects as its
  own role to its own database. Postgres has no cross-database queries or transactions,
  so no service can read or join another's tables; cross-service views come from events
  (the backoffice read model).
- **Each service publishes through its own outbox.** A state change and its events commit
  in one transaction in the service's database; its forwarder moves outbox rows onto
  the bus in order.
- **OpenBao's unseal key stays outside Postgres,** so access to any database, OpenBao's
  storage included, never reaches credential material.
- **One server to start.** A service moves to its own server by changing its connection
  URL, if its load or failure isolation calls for it.
- **Secrets aren't stored in plaintext.** Exchange session keys and runner tokens are
  stored hashed or encrypted.
- **Large blobs** (reports, transcripts) live in content-addressed object storage
  referenced from Postgres, not in rows or artifacts.
- **Minimum requirements** to document: a supported PostgreSQL release, a NATS server
  with JetStream on persistent storage, an OpenBao server with its unseal key file,
  object storage for blobs, connection URLs per service, and backup of every database
  and the object store, OpenBao's database together with its unseal key.

### Events and the bus

- **NATS JetStream is the bus.** It starts as a single server with persistent storage
  and grows to a three-node cluster without changes to services. Rust services use the
  official `async-nats` client.
- **Subjects say what happened and who said it.** Ingress publishes
  `github.<event>.<action>`; the orchestrator `task.<task>.<event>`; the orchestrator and
  funding `request.<owner>.<outcome>`; funding `funding.<event>`; the exchange
  `turn.<turn>.<event>`; the inference gateway `inference.usage`. `<task>` is the task's
  subject-safe ID, not `owner/repo#n`, whose `.` separates NATS tokens. Each publishing
  service owns one stream over its subjects, retained long enough for a consumer to
  replay.
- **Broadcast between services, a queue within one.** Each consuming service has its
  own durable consumer, so every interested service sees every event it subscribes to.
  That service's instances share the consumer, so each event is handled once per
  service. Consumers filter by subject, for example `task.*.stage`.
- **At-least-once delivery, applied once.** The forwarder publishes with the event ID as
  the message ID, so JetStream drops a republish within its duplicate window. A consumer
  records the event ID in the same transaction that applies it and acks after commit; that
  check, not the window, is what makes a redelivery change nothing. An event that keeps
  failing is redelivered with backoff up to a limit, then parked for inspection.
- **A re-mapped GitHub event keeps its source.** A GitHub event's ID is its delivery ID
  plus its mapping revision, so a backfill reaches consumers as a new event, while
  requests stay keyed by their source: re-mapping updates read models and never repeats
  a command, a charge or a reply.
- **Consumers don't depend on arrival order.** Events are one of two kinds, and none is
  a partial update:
  - **State snapshots** carry an entity's complete state, ID and revision. A read model
    or projection, such as the dashboard, keeps the highest revision it has seen.
  - **Facts that call for an action** (a charge, a reply, a revocation) are each handled
    on their own, deduplicated by event ID, and never discarded as stale.

  A consumer holds an event that refers to something it hasn't seen until that arrives:
  the publisher holds request outcomes until their source is final, and the gateway
  checks late bindings against revocations it has recorded. The orchestrator orders
  requests by `source_at`, not by the bus. Within one stream, a consumer
  that needs a task's events in order can use JetStream's deterministic subject
  partitioning on the task ID.
- **Rust services reach the bus only through the shared events crate,** which keeps NATS
  out of service code. During the migration the Python service uses a NATS client with
  the generated models.

How one event moves from the orchestrator to the publisher:

```text
 ┌─ Orchestrator process ──────────────────┐   ┌─ Publisher process ─────────────────────┐
 │ handler ──one tx──▶ orchestrator db     │   │ consumer ──one tx──▶ publisher db       │
 │                     (state + outbox)    │   │      ▲               (state + event ID) │
 │ forwarder ◀── reads outbox              │   │      │ ack after commit                 │
 └───┼─────────────────────────────────────┘   └──────┼──────────────────────────────────┘
     │ publish, message ID = event ID                 │ durable consumer "publisher"
     └──────────────▶ JetStream: task.> stream ───────┘
```

- **Jobs inside a service** use a Postgres job queue. Maintained Rust options include
  `apalis-postgres` (1.1.0, November 2026) and `graphile_worker` (0.14.2, November 2026);
  `pgmq` is in alpha (0.34.0-alpha.7). Choosing one is part of phase 1.
- **Contracts are language-neutral.** Events and APIs are defined as versioned JSON
  Schema (or protobuf) in one place, so Python and Rust services interoperate during the
  migration and each event has one definition. The publishing service owns its events'
  schemas (ingress owns the GitHub event models); Rust types and Python models are
  generated from them.

### Code layout

| Repo | Contains |
| ---- | ---- |
| `concurro` | One Cargo workspace for every other Rust service: ingress, orchestrator, publisher, funding, exchange, inference gateway, backoffice API |
| `concurro-relay` | Everything that speaks the sponsor relay protocol: the Sponsor CLI (`relay`), `verifier`, `tls13`, `native`, `native-transport`, `protocol`, `provider-protocol`, `dev` and the hosted verifier service with its container build |

- **The relay stays its own product.** Sponsors install and audit it, and it keeps its own
  release page, changelog, issues and native build pipeline (`release-plz` and `dist`).
- **The relay protocol is versioned between the relay and the verifier.** Installed
  relays update slowly, so the verifier supports older relay versions; keeping both sides
  in one repo keeps that contract in one place.
- **`concurro` uses `provider-protocol` and the verifier's private API types** through git
  dependencies pinned to relay release tags. The API types are private to the hosted
  binary today and move into a library crate. A provider wire-format change the gateway
  needs is a relay release followed by a dependency bump.

### What goes away and what stays

| Goes away | Stays, smaller |
| ---- | ---- |
| Audit artifact storage, verification and format migrations | Rendering of dashboards, notices and reviews |
| Per-tick re-reads of comments, events and checkpoints | Webhook delivery recovery, in ingress and the publisher |
| Delegated command comments and "may have posted" | Unknown-outcome handling for GitHub writes, in the publisher only |
| Observation jobs, snapshot diffing, deferred inputs | Repository reads for policy and file checks |
| The list index and backoffice GitHub caches | Runner OIDC and session brokering |
| Three copies of sponsorship state, duplicated feed cursors, a route catalog defined four times | Funding's credential metadata and selection rules |
| The vault's encryption, keyring and storage code | Secret material, in OpenBao |
| agentgateway, its admin-API key sync and request-log import | Native passthrough forwarding, in our gateway |
| 1-second polling between services | |

## Migration

The architecture change and the Rust port are done together, one service at a time, so
no Python service is re-platformed only to be rewritten. Each phase leaves a working
deployment.

1. **Contracts and platform.** Event catalog and request API in the chosen format
   ([open question 1](#open-questions)); a database
   and role per service; NATS JetStream and its streams; the `concurro` Cargo workspace,
   pinned to `concurro-relay`'s crates; shared Rust crates for events (outbox forwarder
   and subscriptions), database access, configuration and telemetry; job queue choice.
   Done when an event a Rust service commits with its state change reaches a Rust and a
   Python consumer through its outbox and the bus, each using models generated from the
   same contract, and a duplicate delivery or a restart of the forwarder or a consumer
   changes nothing. Python only consumes events in every phase
   ([contracts](contracts.md#what-the-code-inventory-changed)).
2. **GitHub ingress in Rust.** It receives webhooks, recovers deliveries, and publishes
   typed GitHub events to `github.*`. The first models cover the fields today's service
   reads; the Python service, its list index included, consumes those events instead of
   webhooks, and performs redeliveries for ingress until the publisher exists. This
   proves the bus and the contract tooling with a small, well-bounded service (about 570
   lines today).
3. **Orchestrator, publisher, funding and the backoffice API in Rust, for new tasks,**
   with Postgres as the authority. This is the core of the change (about 19,000 engine
   lines plus the service's scheduler and gateway). It's built as milestones, each a
   working path end to end:
   1. **One task.** An issue enrolled by label and a writer's `start` go through intake,
      funding grants an attempt, the Python exchange runs it, the publisher posts the
      result and the backoffice shows it, with minimal versions of each service. A
      manual `start` alone can initialize a task; the label channel is included so
      enrollment is exercised from the start. Alongside it, contract tests feed
      schema-valid events through the real outbox, bus and publisher into a test
      repository, including duplicate delivery and an uncertain write. Done when
      replaying an attempt keeps its payer and grant, a turn submitted before funding
      records its sponsorship is refused, and when the wrong token reaches the exchange
      first for a task it's refused and the recorded owner can still proceed.
   2. **Failure paths.** A withdrawal during a run, a restart after an uncertain GitHub
      write, a missed webhook. Done when a crash during a withdrawal can't restore
      spending authority after funding has acknowledged the revocation, and two first
      inputs for one issue arriving across the pilot switch end with one owner.
   3. **The rest of a task's behavior** (stages, reviews, retries). Done when the new
      path matches a fixed baseline, a named release of the Python service, on every
      command and stage it supports, with their recovery and authorization behavior.

   Until milestone 3 is complete, only pilot tasks take the new path; ordinary new tasks
   switch after that.

   - **Ownership is claimed once per task.** The orchestrator's ownership table is the
     only record: an insert-if-absent claim returns the winner, and the orchestrator's
     channels, the Python service and the backoffice all call it for a task with no
     owner and act only if they won. The claim applies one rule: during the pilot, the
     new services own a task only if its issue or repository is on the pilot list;
     after the switch, every unowned task. The Python service's existing tasks are
     recorded before the pilot starts. The claim happens on the first routed input for
     the task, a sponsorship comment before enrollment included, and a recorded owner
     always wins over later changes to the rule. Claims are keyed by the issue
     (repository ID and issue number), all a task has before enrollment. Callers,
     the Python service included, claim synchronously and act on the owner returned.
   - **Existing tasks drain** on the Python service. An import tool is built only if some
     tasks can't drain; it would need `cutover`'s guarantees (a fresh generation, no
     write in flight or unresolved, `service/cutover.py`).
   - **Funding takes over selection, grants and the exchange's sponsorship
     transitions** for new tasks. Credential metadata and material stay in the vault
     until phase 5; funding reads them through the vault's metadata API and event feed,
     as the service does now.
   - **The Python exchange stays, with bridge changes:**
     - **Tokens.** One bearer covers every operator operation today
       (`gateway/exchange/exchange.py:1531`). The Python service keeps it for its
       draining tasks until phase 4. The orchestrator gets a token for running turns
       (submit, poll, cancel and end sessions), and funding one that can only change
       sponsorship. On the first operation for a task, a sponsorship change before any
       turn included, the exchange reads the task's owner from the orchestrator's
       ownership table, records it, and refuses the other token's operations on that
       task.
     - **Grants come from funding.** For new tasks, a contributed turn (paid with a
       user's credential, the initiator's own or a sponsor's) is accepted only once
       funding has recorded its sponsorship; the exchange never takes the grant from the
       submitted turn, as `current_grant` does today when it has no row (`:662`).
       Org-funded turns need no sponsorship record and still pass admission and budget
       checks.
     - **Provenance.** It accepts `assignment` and `mention` grant provenance (`:669`).
     - **Turn results.** The orchestrator polls them and publishes the same `turn.*`
       events the Rust exchange will, carrying what the service records in diagnostics
       today (submission, observed status and timing, the result and its transcript
       archive). The backoffice read model takes diagnostics from them. The polling goes
       in phase 5.

     The orchestrator sends it today's binding, which already carries funding, sponsor,
     grant, route, record and version; the publisher takes over runner workflow
     dispatch.
   - **The backoffice API serves new tasks** from its event-fed read model; the frontend
     calls it or the Python backoffice by the task's owner. Lists span both owners: the
     backoffice API combines the Python list API's entries for Python-owned tasks with
     its read model's, keeping one entry per issue by the ownership table, then sorts
     and filters as the lists do today. The Python list API returns whole lists, so no
     pagination spans the two. Live updates merge the same way: the backoffice API's
     change feed combines its read model's changes with the Python list API's
     `/lists/changes`.
4. **The Python service and backoffice retire** once the last task drains, with the list
   index, read caches and diagnostics store. Credential writes move to the Rust
   backoffice API, through funding to the vault until phase 5.
5. **Exchange, inference gateway and secrets in Rust.** The exchange moves to Postgres,
   records diagnostics as turn records and publishes `turn.*` itself. To hand over, the
   orchestrator stops submitting to the Python exchange, polls its outstanding turns
   until each is finished and its final events are published, then switches to the Rust
   exchange and retires the polling. Turn events are identified by turn ID and event
   kind, so the two producers can never publish one completion as two events. The gateway
   replaces agentgateway and the exchange's forwarder, and the relay verifier sits behind
   it, with the separate credentials and admission check of a `concurro-relay` release.
   Funding imports the credential metadata from the vault once. A one-time migrator
   holding the vault's keyring decrypts each usable version and writes it to OpenBao
   under a migration-only policy, keeping record and version IDs (relay-custody versions
   hold no material and move as metadata only); credential writes pause
   while it runs. Done when the handover publishes each outstanding turn's completion
   once, diagnostics come from turn records, every funding mode and inference route
   works through the gateway, and a revocation holds across a gateway and verifier
   restart. Only then are agentgateway, the vault (with the exchange's own sponsorship
   tables and vault feed) and the Python exchange retired, and the polling stopped.

Until phase 3 lands, the Python service gets fixes and the bridges above only. New
features such as [request intake](request-intake.md) and [mentions](mentions.md) are
designed against the target and built on the new orchestrator.

## Effect on open proposals

| Proposal | Effect |
| ---- | ---- |
| [Request intake](request-intake.md) | Becomes the orchestrator's request API, sponsorship included, with GitHub channels consuming `github.*` |
| [Mentions and assignment](mentions.md) | Orchestrator channels consuming `github.*`; the interpreter waits until exact mentions show what people ask for, then runs under a grant from funding, by an execution path that is still its open question (an exchange turn recovers a lost result) |
| [Backoffice API surface](backoffice-api.md) | Arrives in phase 3 for new tasks; commands become `POST /requests` to the orchestrator; views read the backoffice's event-fed model; diagnostics come from `turn.*` events |
| [Webhook-fed list index](list-index.md) | Replaced by the backoffice read model; retired with the Python service in phase 4 |
| [Per-user credentials](per-user-credentials.md), [ordered credentials](credential-list.md) | Rules unchanged; funding owns them, and secret material moves to OpenBao |
| [Sponsor relay](sponsor-relay.md) | The sponsor side is unaffected; funding takes over the vault's relay approvals, and the hosted verifier sits behind the inference gateway, with separate caller credentials and its own admission check |
| [Consolidated PR reviews](consolidated-pr-reviews.md), [risk-driven routing](risk-driven-routing.md) | Logic unchanged; publication goes through the publisher |
| [Retrospective tuning](retrospective-tuning.md) | Reads events and turn records instead of checkpoints and diagnostics files |
| [Issue artifact consolidation](issue-artifact-consolidation.md) | Complete; audit artifacts stay with tasks that drain on the Python service and aren't written for new tasks |

## Decided

- **The orchestrator owns task consent;** funding owns credentials, grants, budgets and
  charges, and trusts the orchestrator's consent decisions.
- **Existing tasks drain** on the Python service at phase 3; an import tool is built only
  if some can't.
- **One orchestrator.** Planning, implementation and review share task, approval and
  finding state, so review doesn't become its own service.
- **The publisher mints GitHub tokens.** In the target it alone holds the App's private
  key (the Python service keeps its signing credentials until phase 4), recovers
  webhook deliveries for ingress and issues the orchestrator read-only tokens scoped to
  one repository, with today's repository and permission checks
  (`service/identity.py:135`).

## Open questions

1. **Event format.** JSON Schema keeps events readable in outboxes and on the bus;
   protobuf gives stronger typing across languages. Which one? The
   [contracts](contracts.md#format) recommend JSON with Rust types as the source.
2. **Reports on GitHub.** Keep publishing full reports as issue artifacts (a public audit
   trail), or publish summaries and keep full reports in Concurro's own storage?
