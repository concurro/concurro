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
- **Modules that can transact together are one service.** A process boundary is drawn
  where a trust boundary or an independent lifecycle requires one, not between parts of
  one decision. Task logic, consent, budgets and GitHub publication commit in one
  database, with no internal API or bus between them.
- **One listener per trust boundary.** A service whose callers sit on different sides of
  a trust boundary, such as runners on the internet and services inside, serves each on
  its own listener, on only the network those callers use. The edge network carries only
  what the edge routes, so what can reach an API follows from the network, not from a
  path rule in a proxy.
- **A state change and the events it causes commit together** (a transactional outbox),
  so no cross-store recovery is needed inside one service.

### Services

| Service | Domain | Trust boundary | Owns |
| ---- | ---- | ---- | ---- |
| GitHub ingress | Webhook receipt: signature check, deduplication and delivery recovery; maps each delivery to a typed GitHub event | Public endpoint; holds the webhook secret only; asks the task application for redeliveries | Delivery receipts, raw payloads, recovery coverage, the GitHub event models |
| Task application | Tasks, as modules of one service: intake (channels, requests, enrollment), task state and stages (plans, findings, reviews), consent and funding policy (who pays, the funding boundary, credential selection and grants), attempts (call and token budgets, admission, charges), the exchange integration, GitHub publication (dashboard, notices, replies, reviews, threads, labels, check runs, branches, PRs, run refs and dispatch; webhook redeliveries for ingress), and the task views | Sole controller of tasks and sole writer to GitHub; holds the App's private key and the reviewer account's token | Tasks, requests, consent logs, attempts, grants, write records and their outcomes |
| Accounts service | Signed-in users and what they do as themselves: GitHub sign-in and sessions, viewer authority for the backoffice API, browser commands signed for the task application, and credential management (entries, lists, filters, relay approvals, the route catalog). From phase 4 ([plan](architecture/phase-4-plan.md)); the Python backoffice until then, with credentials kept by the Python vault until phase 5 | Holds users' GitHub tokens and the command-signing key; no App key with write permissions. Hands new secret material to the secret store on save but can't read it back | Sessions (in memory), credential metadata from phase 5 |
| Exchange | Runner sessions, turn queue, turn results, transcripts and turn diagnostics; publishes turn results as events | Runner OIDC; issues session keys bound to a grant | Sessions, turns |
| Inference gateway | Authenticates session keys, enforces token budgets, routes and forwards to providers or the relay verifier, records usage | Org provider secrets; reads personal secret material | Inference requests and their usage |
| Relay verifier | `concurro-relay`'s hosted verifier: verified inference through sponsors' relays | Public relay listener; relay TLS key and sponsor registrations; native MPC runtime | Relay registrations and sessions |
| Secret store (OpenBao) | Personal credential material and routinely rotated operational secrets | Unseal key and service bootstrap identities outside the database; a least-privilege policy per client service | Encrypted material |
| Backoffice API | Signed-in users' views | No App key, exchange or vault token, no user token and no command path; asks the accounts service who a viewer is | A read model fed by the task application's events |

- **Ingress publishes typed GitHub facts; consumers decide what they mean.** Ingress
  checks the signature, records the delivery ID so a redelivery publishes nothing new,
  and maps the payload to a typed GitHub event published to `github.<event>.<action>`.
  It answers GitHub once the receipt and its event are committed. Any service may
  subscribe.
  - **The GitHub event models belong to ingress.** They keep GitHub's meaning and carry
    only the fields Concurro uses (IDs, logins, author association, bodies, labels,
    timestamps), so consumers never parse webhook JSON. A delivery type without a model
    is recorded and not published.
  - **Ingress parses deliveries with its own input types,** `serde` structs with only
    the fields its models carry, so a new action or a change to a field Concurro doesn't
    read never fails a delivery. `octocrab`'s webhook models parse GitHub's full payloads
    with closed action enums and are documented as beta
    ([phase 2 plan](architecture/phase-2-plan.md#decisions)).
  - **Mapping is stateless.** Anything that needs another service's state (is the issue
    enrolled, is the author a writer, is this comment a command) is the consumer's
    decision.
  - **Raw payloads are kept** for a retention period, so a model that gains a field can
    be backfilled by re-mapping and republishing ([below](#events-and-the-bus)).
- **The task application's modules** are named for what they own: intake, tasks
  (the orchestrator's role), funding policy, attempts, the exchange integration,
  publication (the publisher's role) and views. Below, *the orchestrator*, *funding* and
  *the publisher* name those modules; *funding* always means the task application's
  funding module. Credential management, which funding's selection reads, belongs to the
  accounts service from phase 4.
- **Channels turn GitHub events into requests.** The intake module's GitHub channels
  consume the events they care about (`/concurro` comments, labels, assignments, review
  requests, mentions) and record requests in the task application's database; the
  accounts service submits signed browser requests to the task application, as a future
  Slack adapter would. Channels may read a task's context to interpret a request but never
  change it, and task logic only sees typed requests ([request intake](request-intake.md)).
- **The orchestrator decides who pays; funding decides whether and how,** in the same
  transaction that admits the attempt and charges its call. Task consent (sponsorship
  requests, the consent log and the funding boundary) depends on the task's mode,
  enrollment and comments, so it stays with the task
  ([request intake](request-intake.md#applying-requests)); channels authenticate actors
  in the same application. Personal credentials work before phase 3's cutover
  ([phase 3](architecture/phase-3-plan.md#m3-team-workflows)), since the team uses them
  today.
  - **Grants.** For each attempt the orchestrator freezes a consent decision (task,
    provider slot, sponsor, funding mode, consent revision, boundary and an evidence
    reference). Funding selects a credential and sets a budget:
    - **Implicit fill** (the initiator filling an empty slot) uses the initiator's
      credentials as they stood at the boundary, so a credential added later never
      makes an earlier `start` chargeable.
    - **A new attempt under existing consent** selects among the sponsor's credentials
      eligible now, so a replacement credential and same-sponsor fallback work
      ([ordered credentials](credential-list.md)).
    - **A replayed attempt** returns its original grant, credential version and current
      revoked status; a different request reusing the attempt is refused.
  - **Withdrawal.** A withdrawal commits with its cutoff: funding keeps a cutoff per
    task, provider slot and sponsor, as a counter that only increases and covers every
    earlier grant for that slot and sponsor, and checks it in the same transaction that
    inserts a grant, so a delayed attempt is refused and another provider's or a later
    sponsor's grant isn't. Grants already issued are revoked at the exchange by a job
    retried until acknowledged, and spending has stopped once the gateway publishes
    `inference.binding.closed` for each affected binding (and the exchange has released
    any relay session) or the binding has expired.
  - A revoked grant or a credential that's usable again is a new input to the task's
    decisions. The exchange binds sessions to grant IDs; the inference gateway reports
    usage as events, which funding charges. The route catalog is defined once, by the
    accounts service, as a contract the gateway and the backoffice build from. A sponsor
    approves a relay's registration code in the backoffice; the accounts service, as the
    vault does today, calls the verifier's owner endpoints, records a relay-custody
    credential version that selection treats like any other, and serves relay status
    ([sponsor relay](sponsor-relay.md)).
- **Secret material lives in OpenBao.** Each credential version is its own KV path, named
  by a version ID the accounts service assigns. Operational secrets live under separate,
  per-service paths. The accounts service writes and destroys credential versions; the
  inference gateway reads
  one exact credential version per request. Their policies grant no access to other
  services' operational paths; each service reads only its own operational path under
  a separate policy. Each client signs in with its own role and policy, and every read
  is audited. Static-key auto-unseal keeps the unseal key in a file outside the
  database, and OpenBao stores its data in its own database on the shared Postgres
  server. Clients use it through a small interface (with the `vaultrs` client), so
  another store can replace it.
  - **New material goes browser → accounts service → OpenBao;** in phase 4 the accounts
    service proxies it to the vault, as the Python backoffice did.
  - **Writes are idempotent across the two stores.** The accounts service records the
    version as pending, writes it with check-and-set so its path can be created only
    once, then marks it active. A retry writes the same path; it settles a pending
    version by reading OpenBao's metadata, which its policy allows, never the material.
  - **The gateway can read any credential version,** as the exchange's resolver bearer
    can today; it reads only the version a session's grant names. Its policy cannot
    read operational-secret paths. Scoping credential reads per grant (a
    short-lived token or response wrapping issued at session bind) is a later
    hardening. Relay sponsors' tokens never enter OpenBao, so this access doesn't reach
    them; the verifier's own admission check protects them.
- **Our own inference gateway replaces agentgateway.** agentgateway authenticates session
  keys, blocks once a token budget is spent, routes on Concurro's headers and injects org
  secrets; personal and relay traffic pass through the exchange's own forwarder behind
  it, which for relay custody consumes a one-use request nonce, rechecks the exact relay
  record and turn authority, and forwards to the verifier (deployed with the hosted relay
  overlay). The gateway does all
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
    (registration approval and status), held only by the accounts service (the vault
    holds it today);
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
- **Publication is recorded, then settled.** Task decisions record the writes they want
  in their own transaction; jobs send them. Each write is recorded, marked `sending`
  just before it leaves, and settled with its outcome, so a crash or a lost reply leaves
  a write the publisher can name and settle by reading back. A lost projection write
  delays display; it can't corrupt task state.
  - **One write per surface at a time.** The dashboard, each notice and each finding
    thread is a numbered snapshot of what it should show, so resolving a notice is a
    newer snapshot of it. The publisher settles an uncertain write before sending that
    surface's next one, and writes the newest snapshot, skipping intermediate ones. A
    promised reply or audit notice has its own publication record and is never
    skipped. For managed labels and run refs, keep reconciling the latest desired
    state while an earlier HTTP attempt could still land, including after session end.
- **The backoffice read model replaces the list index.** The backoffice API builds it from
  the task application's events, not from webhooks and GitHub reads, and shows each
  signed-in user what their own access allows. The backoffice's GitHub caches go away
  with the list index.
- **Diagnostics** become turn records: today the task application records each turn's
  submission, observed status and result from its polls and publishes them for the
  backoffice read model, and in phase 5 the exchange records them itself.
- **Offline tools** such as retrospective collection read events and APIs; they aren't
  services.
- **Event plumbing is a shared library, not a service.** Every service links the same
  events crate, which runs the service's outbox forwarder and its subscriptions as
  background tasks in the service's own process, under its own database role. No
  separate component reads another service's outbox.

Tasks and requests:

```text
 GitHub ──webhooks──▶ Ingress ──github.*──▶ ┌─ Task application ───────────────┐ ──writes──▶ GitHub
                                            │ intake · tasks · funding policy  │
                                            │ attempts · exchange · publication│ ──turns───▶ Exchange
                                            └──▲──────────────┬────────────────┘
                                 signed requests│              │events, baseline
                                            ┌──┴──────────┐ ┌─▼─────────────────┐
 Browser ──commands, sign-in, credentials──▶│ Accounts    │◀│ Backoffice API    │◀── Browser (views)
                                            │ service     │ └───────────────────┘
                                            └──────┬──────┘  viewer token: identity, tiers
                                                   ▼
                                     Vault (until phase 5)
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

Events between services (`github.*`, the task application's task, request and turn
events for the backoffice API, usage, binding changes) travel through the bus
([below](#events-and-the-bus)); inside the task application, modules share one
transaction. Secret material goes through OpenBao's API. Not drawn: the task application
polls the exchange for turn results until phase 5, dispatches runner workflows for the
exchange and performs redeliveries for ingress; the accounts service calls the
verifier's owner endpoints from phase 5.

### Data

- **A database per service, on one shared Postgres server.** Each service connects as its
  own role to its own database. Postgres has no cross-database queries or transactions,
  so no service can read or join another's tables; cross-service views come from events
  or the owner's API (the task views).
- **Each service publishes through its own outbox.** A state change and its events commit
  in one transaction in the service's database; its forwarder moves outbox rows onto
  the bus.
- **OpenBao's unseal key stays outside Postgres,** so access to any database, OpenBao's
  storage included, never reaches credential material.
- **One server to start.** A service moves to its own server by changing its connection
  URL, if its load or failure isolation calls for it.
- **Secrets aren't stored in plaintext.** Exchange session keys and runner tokens are
  stored hashed or encrypted.
- **Turn diagnostics** live in the task application's PostgreSQL database. From M1 it
  extracts and indexes transcript events at ingestion, linked to their turns and
  sessions. Specs, prompts, responses and workspaces also live in the task database.
  The task application archives original `.tar.gz` bundles in private S3-compatible object storage
  for recovery and session restore. The backoffice reads diagnostics through the
  task application.
- **Minimum requirements** include a supported PostgreSQL release, persistent NATS
  JetStream, a private S3-compatible archive bucket from phase 3, and OpenBao with its
  unseal key from phase 5. Back up each database and retain the archive objects its
  backups reference. Keep OpenBao's unseal key with its database backup.

### Events and the bus

- **NATS JetStream is the bus.** It starts as a single server with persistent storage
  and grows to a three-node cluster without changes to services. Rust services use the
  official `async-nats` client.
- **Subjects say what happened and who said it.** Ingress publishes
  `github.<event>.<action>`. The task application publishes, for the backoffice API,
  `task.<task>.<event>`, `request.tasks.<outcome>`,
  `consent.<repository_id>.<number>.<event>`, `index.<repository_id>.<kind>` and, until
  phase 5, `turn.<turn>.<event>`. The exchange publishes `turn.<turn>.<event>` from
  phase 5, and the inference gateway `inference.usage`. A service publishes only what
  another process consumes; nothing inside the task application consumes its own
  events. A subject that names a task uses its subject-safe ID, not `owner/repo#n`,
  whose `.` separates NATS tokens. Each publishing service owns one stream over its
  subjects, retained long enough for a consumer to replay; a consumer that falls behind
  retention reloads from its producer's baseline, as the backoffice API does.
- **Broadcast between services, a queue within one.** Each consuming service has its
  own durable consumer, so every interested service sees every event it subscribes to.
  That service's instances share the consumer, so each event is handled once per
  service. Consumers filter by subject, for example `github.issue_comment.*`.
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
  the gateway checks late bindings against revocations it has recorded. The task
  application orders requests by `source_at`, not by the bus.
- **Rust services reach the bus only through the shared events crate,** which keeps NATS
  out of service code. Until the cutover the Python service consumes `github.*` with a
  NATS client and the generated models.

How one GitHub event moves from ingress to the task application:

```text
 ┌─ Ingress process ───────────────────────┐   ┌─ Task application process ──────────────┐
 │ handler ──one tx──▶ ingress db          │   │ consumer ──one tx──▶ tasks db           │
 │                     (receipt + outbox)  │   │      ▲               (state + event ID) │
 │ forwarder ◀── reads outbox              │   │      │ ack after commit                 │
 └───┼─────────────────────────────────────┘   └──────┼──────────────────────────────────┘
     │ publish, message ID = event ID                 │ durable consumer "tasks"
     └──────────────▶ JetStream: GITHUB stream ───────┘
```

- **Jobs inside a service** use a Postgres job queue. Maintained Rust options include
  `apalis-postgres` (1.1.0, November 2026) and `graphile_worker` (0.14.2, November 2026);
  `pgmq` is in alpha (0.34.0-alpha.7). Choosing one is part of phase 1.
- **Contracts are language-neutral.** Events and APIs are versioned JSON, each with one
  definition, so Python and Rust services interoperate during the migration. The
  publishing service owns its contracts (ingress owns the GitHub event models), written as
  Rust wire types; JSON Schema and the Python models are generated from them
  ([contracts](contracts.md#format)).

### Code layout

| Repo | Contains |
| ---- | ---- |
| `concurro` | One Cargo workspace for every other Rust service: ingress, the task application, exchange, inference gateway, backoffice API |
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
| Per-tick re-reads of comments, events and checkpoints | Webhook delivery recovery, in ingress and the task application |
| Delegated command comments and "may have posted" | Unknown-outcome handling for GitHub writes, in the task application's publication only |
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

1. **Contracts and platform.** The [contracts](contracts.md) as Rust wire types with
   their generated schemas and Python models; a database and role per service; NATS
   JetStream and its streams; the `concurro` Cargo workspace, pinned to
   `concurro-relay`'s crates; shared Rust crates for events (outbox forwarder
   and subscriptions), database access, configuration and telemetry; job queue choice.
   Done when an event a Rust service commits with its state change reaches a Rust and a
   Python consumer through its outbox and the bus, each using models generated from the
   same contract, and a duplicate delivery or a restart of the forwarder or a consumer
   changes nothing. Python only consumes events in every phase
   ([contracts](contracts.md#what-the-code-inventory-changed)). The event is a
   representative one, with a tagged variant and optional fields, so the whole generation
   chain is proved with it. [Implementation plan](architecture/phase-1-plan.md).
2. **GitHub ingress in Rust.** It receives webhooks, recovers deliveries, and publishes
   typed GitHub events to `github.*`. The first models cover the fields today's service
   reads; the Python service, its list index included, consumes those events instead of
   webhooks, and performs redeliveries for ingress until the task application's cutover. This
   proves the bus and the contract tooling with a small, well-bounded service (about 570
   lines today). [Implementation plan](architecture/phase-2-plan.md).
3. **The task application in Rust, and a single cutover,** with Postgres as the
   authority. This is the core of the change (about 19,000 engine lines plus the
   service's scheduler and gateway): one application for intake, task state and stages,
   consent and funding policy, attempts, the exchange integration and GitHub
   publication, with the backoffice API, a separate lower-privilege Rust process, serving
   the frontend's task views and commands from the task application's events and
   requests API. Every workflow the team has today continues. The rollout is private:
   it's built and verified on a dedicated test repository with distinct test identities,
   takes over every target in one maintenance window, and is used by the operator on
   real tasks before the team is invited. [Implementation plan](architecture/phase-3-plan.md).
   1. **GitHub-only intake.** On the test repository, with org funding, `/concurro start`
      runs intake through the real exchange and runner, the result appears on GitHub,
      `stop` stops it, a refused command changes nothing, and restarting the controller
      at any point duplicates no turn and no publication.
   2. **One contribution.** A representative issue goes from `start` through planning,
      approval, implementation, review and gates to a ready PR, and others to each
      terminal stage; every task command works; the operator can inspect a stuck task and
      recover from the named failures.
   3. **Team workflows.** Personal credentials and sponsorship, label enrollment,
      standalone reviews, the backoffice API with viewer-scoped views, backoffice commands
      as the signed-in user, delivery recovery and coverage gaps. Done when the team readiness
      checks pass on the test repository: distinct identities for writers, a reader, an
      outsider and a sponsor, with refused commands leaving task state unchanged.
   4. **The cutover.** Python task admission stops, in-flight work is settled or
      stopped, and the task application starts as the sole controller from the stream
      position the Python service stopped at. There's no task import and no rollback.
   5. **Owner use.** The operator runs real tasks and fixes what they find; the team
      readiness checks pass again on a real target before the team is invited.

   - **One controller at a time.** Before the cutover the Python service and the task
     application serve disjoint targets by configuration; after it, only the task
     application runs tasks. Nothing decides an owner per issue.
   - **The Python exchange, gateway, vault and runner stay,** called through their
     existing APIs. The exchange gains turn tombstones and a cancel that ends the turn's
     sessions, and its operator API moves to the platform network at the cutover
     ([bridge](contracts.md#python-exchange)).
   - **The Python backoffice keeps sign-in and the credentials pages,** and answers the
     backoffice API's read-scoped session and tier questions on an internal listener; it
     keeps the browser's command POST, signing each command and forwarding it to the task
     application. Its task pages retire at the cutover.
4. **Sign-in, commands and credentials in Rust.** The Python backoffice retires. A new
   Rust accounts service takes GitHub sign-in and sessions, the viewer authority the
   backoffice API asks, browser command signing, and credential management, which it
   forwards to the vault with the user's token until phase 5. The backoffice API keeps
   its privileges, and the frontend keeps its routes.
   [Implementation plan](architecture/phase-4-plan.md).
   - **Credential management is the accounts service's.** Credential writes act as the
     signed-in user, and only the process holding users' tokens can make them without
     widening the vault or the task application; no decision needs credential edits and
     grants in one transaction. The task application's funding module keeps consent,
     selection, grants, budgets and accounting.
   - **Sessions stay in memory,** as today: a restart signs users out, and the cutover
     signs everyone out once. Logout revocations keep their durable retries, since the
     sign-in token is also the user's owner identity to the vault.
5. **Exchange, inference gateway and secrets in Rust.** The exchange moves to Postgres,
   records diagnostics as turn records and publishes `turn.*` itself. To hand over, the
   task application stops submitting to the Python exchange, polls its outstanding turns
   until each is finished and recorded, then switches to the Rust exchange and retires
   the polling. Turn records are identified by turn ID, so a completion seen by both the
   polling and the new exchange is recorded once. The gateway
   replaces agentgateway and the exchange's forwarder, and the relay verifier sits behind
   it, with the separate credentials and admission check of a `concurro-relay` release.
   The accounts service imports the credential metadata from the vault once and owns it
   from then on: the task application's funding module reads it at each boundary and
   attempt through the same contract it reads from the vault, and consumes
   `credential.<user_id>.changed` facts to revoke grants on retired versions
   ([phase 4 plan](architecture/phase-4-plan.md#phase-5-credential-metadata-in-the-accounts-service)).
   A one-time migrator
   holding the vault's keyring decrypts each usable version and writes it to OpenBao
   under a migration-only policy, keeping record and version IDs (relay-custody versions
   hold no material and move as metadata only); credential writes pause
   while it runs. Routinely rotated operational secrets also move from host files to
   service-specific OpenBao paths; each service reads only the credentials its role needs.
   Bootstrap identities remain outside OpenBao and must avoid a circular startup dependency.
   Done when routine secret rotation no longer requires editing host files, the handover
   publishes each outstanding turn's completion once, diagnostics come from turn records,
   every funding mode and inference route
   works through the gateway, and a revocation holds across a gateway and verifier
   restart. Only then are agentgateway, the vault (with the exchange's own sponsorship
   tables and vault feed) and the Python exchange retired, and the polling stopped.

Until the cutover, the Python service gets fixes only, and none are expected, since it
runs no tasks. New features such as [request intake](request-intake.md) and
[mentions](mentions.md) are designed against the target and built in the task
application.

Mutable target settings and funding policy belong in their owning
services' databases, changed through authenticated APIs with validation and audit.
File settings remain for bootstrap. A future `concurro` CLI should call the owning
service APIs for setup and operator actions once those APIs are stable. An
installation-specific migration script need not be a product command.

## Effect on open proposals

| Proposal | Effect |
| ---- | ---- |
| [Request intake](request-intake.md) | Becomes the task application's intake module, sponsorship included, with GitHub channels consuming `github.*` |
| [Mentions and assignment](mentions.md) | Task application channels consuming `github.*`; the interpreter waits until exact mentions show what people ask for, then runs under a grant from funding, by an execution path that is still its open question (an exchange turn recovers a lost result) |
| [Backoffice API surface](backoffice-api.md) | Arrives in phase 3 (M3), before the cutover: the backoffice API serves views from its read model of the task application's events, and commands become requests as the signed-in user; sign-in, commands and credentials move to the accounts service in phase 4 |
| [Webhook-fed list index](list-index.md) | Replaced by the backoffice API's read model; retired with the Python service at phase 3's cutover |
| [Per-user credentials](per-user-credentials.md), [ordered credentials](credential-list.md) | Rules unchanged; the task application applies them from phase 3's M3, and secret material moves to OpenBao |
| [Sponsor relay](sponsor-relay.md) | The sponsor side is unaffected; the accounts service takes over the backoffice's relay approval pages in phase 4 and the vault's verifier calls in phase 5, and the hosted verifier sits behind the inference gateway, with separate caller credentials and its own admission check |
| [Consolidated PR reviews](consolidated-pr-reviews.md), [risk-driven routing](risk-driven-routing.md) | Logic unchanged; publication goes through the task application's write records |
| [Retrospective tuning](retrospective-tuning.md) | Reads events and turn records instead of checkpoints and diagnostics files |
| [Issue artifact consolidation](issue-artifact-consolidation.md) | Complete; audit artifacts stay on the issues of tasks the Python service ran and aren't written for new tasks |

## Decided

- **One task application.** Intake, task state and stages, consent and funding policy,
  attempts, the exchange integration and GitHub publication are modules of one service
  with one database, so the payer decision, attempt admission and call charge commit
  together. Planning, implementation and review share task, approval and finding state,
  so review doesn't become its own service either.
- **A single cutover, no coexistence.** The task application is built on a test
  repository, then becomes the sole controller in one maintenance window. Python tasks
  aren't imported: any unfinished one is settled or stopped, and can be started again.
- **The task application mints GitHub tokens.** It alone holds the App's private key
  after the cutover, recovers webhook deliveries for ingress, and reads repositories
  with read-only tokens scoped to one repository, with today's repository and permission
  checks (`service/identity.py:135`).
- **No team workflow regresses at the cutover.** Personal credentials, label enrollment,
  standalone reviews and the backoffice's task views and commands work on the task
  application and the backoffice API before it; a deferral is recorded with its reason
  and impact ([phase 3](architecture/phase-3-plan.md#deferrals)).
- **A separate, lower-privilege backoffice API.** It holds no App key and no user token,
  and reads the task application's events. It submits no command: the session authority
  (the Python backoffice in phase 3, the accounts service from phase 4) takes the
  browser's commands with its CSRF check, signs each for the signed-in user and forwards
  it, so the backoffice API can't act as a user.
- **A separate accounts service for what users do as themselves.** Sign-in, sessions,
  command signing and credential management share one trust boundary, acting with the
  user's own token, and so one process; it's apart from the backoffice API, which
  serves the largest surface with no user authority, and from the task application,
  which holds the App key and parses the most untrusted input
  ([phase 4](architecture/phase-4-plan.md#processes-and-authority)).
- **Contracts are JSON, written as Rust types.** Dedicated wire types generate JSON Schema,
  which generates strict Python models, with fixture tests in both languages
  ([contracts](contracts.md#format)). JSON keeps outboxes, parked events and the bus
  readable; protobuf's evolution rules wouldn't add to the additive rule.

## Open questions

1. **Reports on GitHub.** Keep publishing full reports as issue artifacts (a public audit
   trail), or publish summaries and keep full reports in Concurro's own storage?

## Later hardening

Not planned in any phase; each would be its own proposal.

- **Runners on a private network.** Runner jobs join a tailnet with the Tailscale GitHub
  action, authenticated by GitHub OIDC (workload identity federation, ephemeral tagged
  nodes), and the exchange's runner API and the inference gateway stop listening on the
  internet. Their pre-authentication code is then out of reach of anyone else. It doesn't
  make an authenticated runner less dangerous, and the runner tag's ACL must reach those
  two listeners and nothing else. Best done with phase 5, when both are rebuilt.
