# Proposal: service contracts

**Status: proposal, not implemented.**

Every contract between the services in the [architecture review](architecture.md), across
all migration phases: what crosses each boundary, who produces and consumes it, how it's
identified and deduplicated, and the phase that introduces it. The
[scenario traces](contract-scenarios.md) walk the critical flows through these contracts
and check them against the invariants.

## Target and bridge contracts

- **Target contracts** are what the Rust services implement and keep. They're designed
  for the end state: no Python interface shapes them. Today's code informs them only
  through the behavior each milestone ports (stages, authorization, recovery), never
  through its wire formats.
- **Bridge contracts** let a target service talk to Python code, or to something already
  deployed, during the migration. They keep today's shapes, with the smallest changes the
  migration needs. Each names the **adapter** that translates it to a target contract,
  in the Rust service that speaks it, and the phase that **retires** it, adapter
  included.

Contracts are written at one of two levels:

- **Full:** fields, types and rules. Everything phase 3's first milestone (M1) needs is
  full, bridges included.
- **Sketch:** the shape and the decisions that matter, settled in their phase's spike. A
  sketch can change; its catalog entry (producer, consumers, identity) shouldn't need to.

In phase 3 the task lifecycle is one Rust process, the task application (`concurro-tasks`,
[phase 3 plan](architecture/phase-3-plan.md)): intake, tasks, funding policy, attempts,
exchange integration and publication share one database and commit together. Between those
modules nothing is a wire contract: [fills](#fills), [grants](#grants), slot changes,
[read tokens](#read-tokens), [dispatches](#runner-dispatch),
[write admission](#write-admission) and [publication outcomes](#publication-outcomes) are
its records and the rules its modules keep. Wherever data crosses a process or trust
boundary, it's a contract: phase 1's Rust wire types and generated schemas, through a
transactional outbox and the bus for events. That includes the task application's events
and request API for the [backoffice API](#backoffice-api), a separate, lower-privilege
process. Its contracts are listed under [task application](#task-application).

## What the code inventory changed

Today's interfaces were inventoried from the code to find the bridges, the behavior to
preserve, and the guarantees today's design gives implicitly:

1. **Python never publishes onto the bus.** Until the phase 3 Cutover the Python service
   and list index only consume `github.*`; everything else Python offers is HTTP (the
   exchange, the vault, and until the Cutover the list API and delivery recovery).
2. **Webhooks only wake tasks today.** Ingress uses payloads to choose which tasks to
   wake (`service/ingress.py:224`), and the engine re-reads issues, comments, label
   events, PRs, checks and files from GitHub on every tick; the reconciler re-reads them
   after a gap (`service/reconciler.py:120`). The target needs an explicit rule for what
   events carry and what consumers read ([below](#events-and-reads)), and an explicit
   recovery for what the re-reads quietly guarantee ([coverage gaps](#coverage-gaps)).
3. **The runner protocol is a contract of its own.** The turn payload and result pass
   through the exchange opaquely (`exchange.py:1293`) between the engine that builds the
   prompt and the runner code in the engine bundle (`engine/turns.py:400`). The phase 3
   task application has to speak it to today's runner, which makes it a bridge.
4. **Live updates are part of the list contract.** The backoffice's server-sent events
   long-poll the list index's `POST /lists/changes` (`backoffice/live.py:127`), and its
   lists join sponsorship projections from `POST /sponsorships`
   (`backoffice/tasks.py:427`). Both retire with the Python service at the Cutover; the
   backoffice API's read model, fed by the task application's events, replaces them and
   serves its own change feed ([backoffice API](#backoffice-api)).
5. **Command edits are handled two ways.** Contribution commands apply once per comment
   (`id > command_id`, `engine/controller.py:1698`); standalone review commands apply
   once per revision (`command_receipts`). Requests key on the revision, so the comment
   channel needs one rule ([decisions](#decisions)).
6. **Python's grant is a consent slot, not an attempt.** A sponsorship slot's `grant_id`
   and `epoch` change only when consent changes; the exchange refuses a second grant ID at
   the same epoch (`exchange.py:778`). Per-attempt grants need a mapping
   ([Python exchange](#python-exchange)).
7. **Two mismatches in today's code,** which the exchange bridge fixes in M3, with
   personal credentials:
   - The engine treats `review_request` as an implicit provenance
     (`engine/sponsorship.py:23`); the exchange accepts only `task_start` and `label`
     (`exchange.py:671`), so a contributed standalone review is refused.
   - The engine can add `custody` to a frozen admission (`engine/admissions.py:63`); the
     exchange requires exactly twelve fields (`exchange.py:1210`). The service's metadata
     read drops `custody` (`service/shadow.py:86`), so it's never set today.
8. **GitHub user IDs are strings in some interfaces and integers in others** (vault
   events, metadata, bindings). Target contracts use integers; bridge adapters convert.

## Conventions

These apply to target contracts. Bridges keep their own.

### Identifiers

| ID | Form | Minted by | Notes |
| ---- | ---- | ---- | ---- |
| Issue key | `{repository_id, number}` | GitHub | Task lookup; PRs share the issue number space. A transferred issue gets a new key |
| Task ID | UUIDv7 | Task application, on the first request it applies for the issue | One task per issue key for its lifetime; the `<task>` in subjects |
| Request ID | UUIDv7 | Task application | |
| Source | `{channel, id, index}` | Channel | Comment: `{comment_id}:{revision digest}`; label, assignment, review request: the issue timeline event's ID ([enrollment channels](#enrollment-channels)); backoffice: its idempotency key |
| Attempt ID | UUIDv7 | Task application, at admission | One admitted model call; also its turn's ID |
| Consent revision | Integer per task | Task application | Increases with every consent change |
| Slot epoch | Integer per task and provider | Task application | Increases when the slot's payer changes |
| Grant ID | UUIDv7 | Task application | One per attempt |
| Session ID | UUIDv7 | Exchange | |
| Credential IDs | Entry, record, version | Vault today; the credentials owner from phase 4 | Opaque to consumers; existing IDs carry over unchanged |
| Relay ID | `relay-<32 hex>` | Verifier | |
| Event ID | UUIDv7, or UUIDv5 | Producer | UUIDv5 where the ID must be reproducible: a GitHub event's, of its delivery ID and mapping revision, so a re-mapping is a new event and a redelivery isn't |
| Delivery ID | GitHub GUID | GitHub | Ingress's deduplication key |

GitHub event IDs use UUIDv5 with the RFC 4122 OID namespace
(`6ba7b812-9dad-11d1-80b4-00c04fd430c8`) and the UTF-8 name
`<delivery_id>:<mapping_revision>`. For example, `delivery-1:1` produces
`669a0c7f-922d-5b0c-a579-4e19e2807e31`.

### People

Four roles stay distinct in every record and contract that names a person:

| Role | Who | Recorded as |
| ---- | ---- | ---- |
| Task author | The issue's author | `author {id, login}` on the task; their comments count toward the requirements |
| Command actor | Who made a request: a commenter, a labeler, or the signed-in user in the backoffice | The request's `actor {id, login}`, authorized at application |
| Operator | Whoever runs the task application's host commands | `operator`, never a GitHub user; host commands aren't requests and need no GitHub permission |
| Payer | Who pays for an attempt: the org, the initiator by an implicit fill, or an explicit sponsor | The grant's `purpose` and `payer`; the consent record's slots |

### Event envelope

Every event on the bus is a JSON object:

| Field | Type | Meaning |
| ---- | ---- | ---- |
| `id` | UUID | Event ID |
| `type` | string | `task.state`, `github.issue_comment.created`, ... |
| `version` | integer | Schema major version |
| `kind` | `snapshot` \| `fact` | How consumers apply it ([architecture](architecture.md#events-and-the-bus)) |
| `producer` | string | Publishing service |
| `occurred_at` | RFC 3339 UTC | Producer's transaction time; informational, never used for ordering |
| `entity` | string | The ID a snapshot's revision belongs to (task, notice, turn, report) |
| `revision` | integer | Snapshots only; strictly increasing per `entity` and `type` |
| `causation` | string | The request or event that caused it |
| `data` | object | Payload |

The NATS subject is the architecture's (`task.<task>.<event>` and so on). The message
ID is `{id}:{version}`, so a breaking change can publish both versions of one
event.

### APIs

These apply to APIs between processes. Modules of the task application call each other
in-process.

- **HTTP with JSON bodies** on each service's internal listener, under a version prefix
  (`/v1/...`).
- **One bearer per caller** in `Authorization`, scoped to the operations that caller
  needs. No shared tokens.
- **Every mutation is idempotent.** It carries a natural key (attempt ID, revocation job
  ID) or an `Idempotency-Key` header. The same key with the same body returns the
  recorded response, status included; the same key with a different body is
  `409 idempotency_conflict`. Recorded responses live as long as their entity can be
  retried. A caller that doesn't know the outcome retries with the same key, never a new
  one. A `503` is a transient precondition and isn't recorded: the same key and body are
  evaluated again. The [requests API](#requests) compares the actor and the command's
  contents, not its signed proof, which changes when it's signed again.
- **Concurrent edits** of a revisioned resource send `If-Match` with the revision they
  read; a stale one is `412`.
- **Errors** are always an HTTP response with `{"error": {"code", "message",
  "retryable"}}`, and a closed set of codes per API.
- **Times** are RFC 3339 UTC. **GitHub IDs** are integers. **Field names** are
  `snake_case`, except the backoffice API's, which are the frontend's `camelCase`.

### Versioning

- **Additive within a version:** new optional fields, and new enum values only where a
  field is documented as open.
- **Consumers ignore unknown fields.** An unknown value in a closed enum parks a fact
  and leaves a snapshot at its last understood revision; neither is dropped silently.
- **A breaking change is a new version** of the contract, which covers every `type` it
  publishes (`github.issues` covers `github.issues.labeled` and the other actions). Its
  events are published in both versions under the same event ID until every consumer has
  moved. A consumer reads one version.
- **Owned work survives a version switch.** Keep the old decoder or ship an audited
  conversion until retries, holds and parkings from that version are resolved. A consumer
  skips a fresh unsupported copy, but parks an unsupported event it already owns with
  its original bytes and version reason. An operator can discard that parking or replay
  it after a corrective binary restores the decoder. A readable newer-version copy
  with the same event ID cannot bypass that parking: the consumer skips it while the
  parking is open, during operator replay, and if the old bytes park again.
- **CI enforces it:** generated schemas are committed, and a change that isn't additive
  within a version fails the build.

### Events and reads

- **Events carry acts and what a consumer needs to route them:** who did what, when, and
  the fields that decide whether it's a request (a comment's body and author, a label's
  name, a review request's reviewer).
- **Decisions that need complete or current state read it when they're made,** with
  [read tokens](#read-tokens): writer permission (the collaborators API, never
  `author_association`), repository files and policy documents, CI gates for a commit,
  PR state, files and review threads, and issue timelines. Events say when to read again.
- **Read models are fed by events,** and read GitHub only to repair a
  [coverage gap](#coverage-gaps).

## Format

**Decided: JSON, with Rust types as the source** ([architecture](architecture.md#decided)).
Rust is the authoring language; the versioned JSON contract is what services depend on, so
Python consumers need neither Rust source nor a Rust toolchain.

- **Dedicated Rust wire types define each contract** (`serde`), separate from database
  models and service domain types, so refactoring a service can't change its protocol.
- **One direction: Rust → JSON Schema → Python.** `schemars` (1.2.3, October 2026)
  generates JSON Schema (draft 2020-12, set explicitly) into a committed `contracts/`
  directory, with one pinned generator configuration, and schemas for deserialization
  where they differ from serialization. Python's Pydantic models are generated from those
  schemas with `datamodel-code-generator`, pinned; nobody writes a second definition by
  hand.
- **Generated Python models are strict:** no coercion, so `"123"` isn't accepted as an
  integer. Unknown fields follow the [versioning](#versioning) rule.
- **Fixture tests across languages:** each contract has serialized fixtures that Rust
  round-trips and Python parses. Between them they cover tagged variants, a missing
  field versus `null`, defaults, integer bounds and unknown fields, so the schemas aren't
  the only evidence the two languages agree.
- **CI fails** on drift between the types and the committed schemas, and on a change that
  isn't additive within a version.
- **Schemas check shape, not behavior.** Consent, ordering, idempotency and withdrawal
  are guaranteed by these contracts and their [scenario](contract-scenarios.md) tests.
- **Why not protobuf:** JSON keeps outbox rows, parked events and the bus readable with
  standard tools, and Rust types as the source keep one definition without generating
  Rust. Protobuf's evolution rules are the additive rule above, so they wouldn't buy more.
- **Why not schemas first:** generating Rust from JSON Schema (`typify`, 0.10.1, November
  2026) is young, and hand-written types give us the tagged enums and newtypes we want.
  The committed schemas stay the language-neutral record.

Phase 1 proves the chain on one representative event, with a tagged variant and optional
fields, through the outbox and bus round trip.

## Target catalog

Phases are the [migration phases](architecture.md#migration). Within phase 3, the
milestones are M1 (GitHub-only intake slice), M2 (one contribution end to end), M3 (team
surfaces), the Cutover, and Owner use on real targets before the team is invited
([phase 3 plan](architecture/phase-3-plan.md)). Everything today's deployed product
offers is in place before the Cutover. Where a bridge stands in for a target contract
until it's built, the [bridge catalog](#bridge-catalog) says so.

### Configuration

| Contract | Kind | Producer → consumers | Phase | Level |
| ---- | ---- | ---- | ---- | ---- |
| `config.targets` | Snapshot | Task application → backoffice API; exchange in phase 5 | 3/M3 | Full ([below](#configuration-1)) |
| `funding.routes` | Snapshot | The credentials owner → gateway, backoffice API | 5 | Sketch: the route catalog, each route's provider, kind, custody and endpoint |

### GitHub ingress

All facts on `github.<event>.<action>`, identified by delivery ID and mapping revision
([fields](#github-events)).

| Contract | Consumers | Phase | Level |
| ---- | ---- | ---- | ---- |
| `github.issue_comment.{created,edited,deleted}` | Task application: comment channel (3/M1); mention channel (later) | 2 | Full |
| `github.issues.*`: `opened`, `edited`, `closed`, `reopened`, `labeled`, `unlabeled`, `assigned`, `unassigned`, `transferred`, `deleted` with their own fields, and any other action with the issue | Task application: issue state (3/M1), label channel (3/M3), assignment channel (later); Python list index until the Cutover | 2 | Full |
| `github.pull_request.*`, including `review_requested` and `review_request_removed` | Task application: PR state (3/M2), review-request channel (3/M3) | 2 | Full |
| `github.pull_request_review.*`, `github.pull_request_review_comment.*`, `github.pull_request_review_thread.*` | Task application (3/M2; mention channel later) | 2 | Full |
| `github.push`, `github.status`, `github.check_run.*`, `github.check_suite.*`, `github.workflow_run.*` | Task application: when to re-read gates (3/M2); backoffice API read model: runs (3/M3) | 2 | Full |
| `github.installation.*`, `github.installation_repositories.*` | Task application (targets): new, after phase 3; today's targets come from settings | Later | Sketch |
| `ingress.coverage.gap` | Python service until the Cutover; task application and backoffice API read model from 3/M3 | 2 | Full ([below](#coverage-gaps)) |
| `ingress.coverage.advanced` | Python list index until the Cutover; backoffice API read model (3/M3): everything up to `covered_until` is on the stream before it | 2 | Full ([phase 2 plan](architecture/phase-2-plan.md#coverage)) |
| `POST /v1/replays` | API: a consumer that lost events from the stream → ingress | 2 | Full ([phase 2 plan](architecture/phase-2-plan.md#replays)) |

Mentions and assignment aren't in today's deployed product; they're new channels,
planned after phase 3's Owner use ([mentions](mentions.md)). Nobody loses a workflow by
their wait.

### Task application

`concurro-tasks`'s contracts with other processes. Its events go through phase 1's
outbox onto the bus from the milestone that introduces each family: M1 publishes the
task, request and turn events it supports, and M3 adds the consent and index families.
Until 3/M3 the only consumer is M1's contract test consumer; 3/M3 adds the backoffice
API. The [records](#task-application-records) its modules share are not contracts.

| Contract | Kind | Producer → consumers | Phase | Level |
| ---- | ---- | ---- | ---- | ---- |
| `POST /v1/requests`, `GET /v1/requests/{id}` | API | Python backoffice → task application, forwarding a browser command it signed; the actor comes from the [proof](#command-proofs) | 3/M3 | Full ([below](#requests)) |
| `request.tasks.{applied,refused,superseded}` | Fact | → backoffice API read model | 3/M1 | Full ([below](#request-events)) |
| `request.tasks.{blocked,stopped}` | Fact | → backoffice API read model: a withdrawal's later [steps](#withdrawal-steps) | 3/M3 | Full |
| `request.source.{received,final}` | Fact | → backoffice API read model | 3/M1 | Full |
| `task.<task>.state` | Snapshot | → backoffice API read model | 3/M1 | Full ([below](#task-snapshots)) |
| `task.<task>.consent` | Snapshot | → backoffice API read model | 3/M3 | Full |
| `task.<task>.notice` | Snapshot per notice | → backoffice API read model | 3/M1 | Full |
| `task.<task>.report` | Snapshot per report | → backoffice API read model | 3/M1 | Full: kind, step, body, digest |
| `task.<task>.finding` | Snapshot per finding | → backoffice API read model | 3/M2 | Sketch: seat, origin profile, status, location, body, disposition |
| `turn.<turn>.{submitted,finished}`, `turn.<turn>.status` | Fact, snapshot | → backoffice API read model (diagnostics) | 3/M1, from the task application's polls; the exchange's in phase 5 | Full ([below](#turn-events)) |
| `consent.<repository_id>.<number>.{granted,replaced,withdrawn}` | Fact | → backoffice API read model: each user's own consent history, on issues with or without a task | 3/M3 | Full ([below](#consent-facts)) |
| `index.<repository_id>.{issue,pull_request,workflow_run}` | Snapshot | → backoffice API read model: the target index | 3/M3 | Full ([below](#target-index-and-baseline)) |
| `GET /v1/baseline` | API | Backoffice API → task application: every current snapshot and the durable history the views show, read as one consistent snapshot | 3/M3 | Full ([below](#target-index-and-baseline)) |
| `GET /v1/turns/{turn}/artifacts/{sha256}` | API | Backoffice API → task application: bytes for one PostgreSQL artifact referenced by the turn | 3/M3 | Full ([below](#blobs)) |
| `GET /v1/turns/{turn}/transcript` | API | Backoffice API → task application: ordered transcript entries from PostgreSQL | 3/M3 | Full ([below](#transcript-search)) |
| `funding.charge` | Fact | → backoffice API read model | 5 | Sketch: grant, usage, cost basis |
| [Delivery recovery](#delivery-recovery) | API | Ingress → task application | 3/M3; the Python service until the Cutover | Full |
| GitHub REST, with captured fixtures | Client | Task application → GitHub | 3/M1 | Full for each milestone's calls |
| [Python exchange](#python-exchange) operator API | Bridge | Task application → exchange | 3/M1; slots, provenances, `custody` and per-turn observations in 3/M3 | Full |
| [Turn payload v1](#turn-payload-v1) | Bridge | Task application → today's runner, through the exchange | 3/M1 | Full |
| [Vault metadata and events](#vault) | Bridge | Task application → vault | 3/M3 | Full |
| Operator commands | CLI | Operator → task application, on the host | 3/M1 | Full ([M1 plan](architecture/phase-3-m1-plan.md)) |

The task application holds these credentials, and no other Rust process does:

- **The App's private key,** for App JWTs, installation tokens and read tokens.
- **The reviewer account's user grant,** with which standalone reviews, mention replies
  and reactions are published (`service/user_grant.py`): a user-to-server token for the
  App, stored, refreshed and bound by the reviewer's numeric ID, so a refreshed token
  for another account is refused. Its refresh token is single-use, so it's never copied
  while two services could refresh it: through 3/M3 the task application uses an
  independent test reviewer account and grant, and at the Cutover, after the Python
  service has stopped, the production grant and its refresh configuration (client ID,
  client secret path) transfer to the task application. A published standalone review
  must be attributed to the configured numeric reviewer ID; publication reads the review
  back and checks its author, and the transfer is verified by a refresh.
- **The exchange's operator token** and, from 3/M3, **the vault's metadata bearer**.
- **The command-proof verification key** of the
  [Python backoffice](#python-backoffice-sessions).

### Task application records

Internal to the task application: each is owned by one module and changed only in its own
transactions, so none is a wire contract. Their rules are below.

| Record | Module | Phase |
| ---- | ---- | ---- |
| [Fills](#fills), the consent fold and the funding boundary | Funding policy | 3/M3 |
| [Grants](#grants), with selection and exclusions ([retries](#retries-and-exclusions)) | Funding policy | 3/M1 org grants; 3/M3 contributed |
| Slot changes and the slot adapter ([bridge](#python-exchange)) | Funding policy | 3/M3 |
| [Withdrawal steps](#withdrawal-steps) and cutoffs | Funding policy | 3/M3 |
| [Read tokens](#read-tokens) | Publication | 3/M1 |
| [Dispatches and ended sessions](#runner-dispatch) | Exchange integration, publication | 3/M1 |
| Writes, [admission](#write-admission) and [outcomes](#publication-outcomes) | Publication | 3/M1 |
| Attempts and charges | Attempts | 3/M1 |

### Exchange and runner

| Contract | Kind | Producer → consumers | Phase | Level |
| ---- | ---- | ---- | ---- | ---- |
| Operator API | API | Task application → exchange | 5 | Sketch ([below](#exchange-operator-api)) |
| Runner API | API | Launcher workflow → exchange | 5 | Sketch ([below](#runner-api)) |
| Runner protocol (turn spec and result) | Runner protocol | Task application ↔ runner, through the exchange | 5 | Sketch ([below](#runner-protocol)) |
| `session.<session>.binding` | Snapshot | → gateway, task application | 5 | Sketch: key hash, grant, credential version, models, token budget, expiry, status; a relay session is `revoked` only after the verifier confirms its release |
| `exchange.grant.released` | Fact | → task application | 5 | Sketch: grant ID and every session the exchange ever bound to it, published once it refuses new ones |
| Session claim | Signed token | Exchange → verifier, carried by the gateway | 5 | Sketch: claim ID, relay, record and version, route, models, session, expiry, call and token allowance, key ID; Ed25519 |

### Inference gateway

| Contract | Kind | Producer → consumers | Phase | Level |
| ---- | ---- | ---- | ---- | ---- |
| Inference endpoint | API | Runner harnesses → gateway | 5 | Sketch: native provider passthrough authenticated by the session key; route chosen by the binding, not by client headers |
| `inference.usage` | Fact | → task application, backoffice API read model | 5 | Sketch: binding, grant, turn, model, input and output tokens |
| `inference.failure` | Fact | → task application | 5 | Sketch: category ([failure categories](#retries-and-exclusions)), model, retry after, version |
| `inference.binding.closed` | Fact | → task application (spending stopped), exchange | 5 | Sketch: binding, reason, closed at |

### Relay verifier

Released by `concurro-relay` for phase 5.

| Contract | Kind | Producer → consumers | Phase | Level |
| ---- | ---- | ---- | ---- | ---- |
| Owner API | API | The credentials owner (the vault today) → verifier | 5 | Sketch ([below](#verifier-owner-api)) |
| Dispatch with a session claim | API | Gateway → verifier | 5 | Sketch: the claim replaces the session, turn and deadline the verifier trusts today |
| Release | API | Exchange → verifier | 5 | Sketch: revoke a claim or cancel one turn; persisted until the claim expires; acknowledged |

### Storage

| Contract | Kind | Producer → consumers | Phase | Level |
| ---- | ---- | ---- | ---- | ---- |
| Turn diagnostics and original archives | PostgreSQL search rows and artifacts, Hetzner S3 archive objects | Task application stores turn content (3/M1); backoffice API fetches it through the task application (3/M3); exchange accepts runner uploads (5) | 3/M1 | Full ([below](#blobs)) |
| KV path per credential version | OpenBao KV v2 | The credentials owner writes and destroys; gateway reads; migrator writes once | 5 | Sketch: `{secret, account}`, check-and-set 0, custom metadata `{entry, record, route}` |

### Backoffice

| Contract | Kind | Producer → consumers | Phase | Level |
| ---- | ---- | ---- | ---- | ---- |
| Backoffice API under `/api/v2` | API | Frontend → backoffice API | 3/M3 | Full for 3/M3 ([below](#backoffice-api)) |
| [Sessions and tiers](#python-backoffice-sessions) | API | Backoffice API → Python backoffice, on the platform network; read-scoped | 3/M3 | Full |
| Browser commands | API | Frontend → Python backoffice, with its CSRF check; forwarded as signed requests | 3/M3 | Full ([command proofs](#command-proofs)) |

### Operations

| Contract | Kind | Caller → service | Phase | Level |
| ---- | ---- | ---- | ---- | ---- |
| Parked events | API | Operator → every consumer | 1 | Sketch: list, inspect, replay or discard a parked event, by consumer and event ID |
| Operator views and recovery | CLI | Operator → task application | 3/M1 | Full ([M1 plan](architecture/phase-3-m1-plan.md)) |
| Reconciliation | CLI | Operator → task application | 3/M3 | Sketch: reconcile a task or a repository over a period, as for a [coverage gap](#coverage-gaps) |
| Cutovers and migrations | Runbook | Operator | 3, 5 | Sketch: the phase 3 Cutover (the window announced; outstanding tasks finished or stopped while Python still runs normally, unknown writes settled with `resolve` until Python is quiescent; the whole daemon stopped, its exit confirmed, and `A` set then; its final registry read through `Registry.inspect` (`service/registry.py:356-360`) and the exchange checked for no queued or running turns and no live sessions; sources dated strictly before `A` in the window checked against evidence specific to each (Python's reply, the writes it caused, records such as `approval.comment`, the issue's dashboard and labels), reissued only on positive evidence that Python missed them, and an ambiguous one left to its author's fresh decision with no automatic action; `S` and the exchange's generations and epochs recorded; the [Python archives imported](#python-archive-import); the reviewer grant transferred; the task application started; the backoffice baseline refreshed before switching the frontend; [cutover sources](#cutover-sources)). The reused-issue slot check (an exported epoch 2 replaced by epoch 3) is mandatory in the rehearsal, against a test exchange holding a historical slot; at the live Cutover it runs when the export holds a slot, and otherwise the operator confirms the export is empty and a first slot initializes normally. Also the phase 5 handover and secret migration, each with its checks |

## Bridge catalog

| Bridge | Between | Stands in for | Adapter | Retires |
| ---- | ---- | ---- | ---- | ---- |
| `github.*` and `ingress.*` consumers | Ingress → Python service, list index | Nothing: Python consumes target events through generated models | None | 3, at the Cutover |
| [Delivery recovery](#delivery-recovery), served by Python | Ingress → Python service | The task application's delivery recovery API | None: Python implements the target contract | 3, at the Cutover |
| [Python exchange](#python-exchange) operator API, capabilities and observations | Task application → Python exchange | Exchange operator API, grants on the exchange's side, `inference.*` | The exchange integration module (`concurro-exchange-v1`) | 5 |
| Sponsorship slot transitions | Task application → Python exchange | Nothing: target grants carry their decision | The funding policy's slot adapter | 5 |
| Turn polling | Task application → Python exchange | `turn.*` published by the exchange | The exchange integration module publishes `turn.*` | 5 |
| Runner API v1 (`engine`, `key`, `next`, `turns/{turn}`, `end`) | Launcher workflow → exchange | Runner API | Python exchange serves it; the Rust exchange serves it until no target's launcher is on v1 | When the last launcher moves |
| [Turn payload v1](#turn-payload-v1) | Task application → today's runner | Runner protocol | The exchange integration module | Phase 5, with the runner ([decisions](#decisions)) |
| [Vault metadata and events](#vault) | Task application → vault | The credentials owner's own store | The funding policy's vault adapter | 5 |
| Vault owner API (`gateway/vault/vault.py:1534`) | Python backoffice → vault | The [credentials API](#credentials) | None until phase 4 | 5 |
| Verifier owner and dispatch API, single bearer, `x-concurro-binding` | Vault, exchange forwarder → verifier | Verifier owner API, dispatch with a session claim, release | None | 5 |
| [Python backoffice sessions](#python-backoffice-sessions) | Backoffice API → Python backoffice | The backoffice API's own sign-in | None | 4 |
| Python backoffice `/api/*`, `/auth/*` | Frontend → Python backoffice | Backoffice API | None; the frontend calls `/api/v2` for tasks from 3/M3, and the Python task and list pages are off from the Cutover | 4 |

## Target contracts

### Configuration

Service settings stay files each service reads at start. The task application's settings
name its targets, and it publishes what other processes need from them as
`config.targets`, a snapshot with one revision per change, from 3/M3 for the backoffice
API (its lists and tier checks) and from phase 5 for the exchange:

| Field | Meaning |
| ---- | ---- |
| `targets` | Per target: `target_id`, `upstream` and `fork` (`{repository_id, full_name}`), `issues` (`upstream`, `fork` or `both`), `launcher {workflow, ref, version}` |
| `reviewer` | The reviewer account `{id, login}`, or absent |

- Publication admits writes and mints read tokens only for these repositories.
- Until phase 5 the task application sends a run's expected OIDC claims, built from the
  target's launcher, with each submit.
- A target's own configuration (`.concurro/config.*` at its `config_sha`) stays the
  user's file. The task application reads it when it decides, as it reads any repository
  file.
- Budgets (`max_model_calls` and the other limits) come from that file. The token budget
  and lease per attempt come from the task application's settings and go with the
  submit.

### GitHub events

Every `github.*` event's `data` holds:

| Field | Type | Meaning |
| ---- | ---- | ---- |
| `delivery_id` | string | `X-GitHub-Delivery` |
| `mapping_revision` | integer | Version of ingress's mapping that produced it |
| `received_at` | time | Ingress's receipt time |
| `installation_id` | integer | |
| `repository` | `{id, owner_id, full_name}` | |
| `sender` | Actor | Who acted; for an edit, the editor |

An Actor is `{id, login, type}`, with `type` one of `User`, `Bot`, `Organization`. Each
event type adds:

| Type | Fields |
| ---- | ---- |
| `issue_comment` | `issue {number, title, state, is_pull_request, user, labels, created_at, updated_at}`, `comment {id, body, user, author_association, created_at, updated_at, html_url}`, `previous_body` on `edited` |
| `issues` | `issue {number, title, body, state, state_reason, user, labels, assignees, created_at, updated_at, is_pull_request}`; `label {name}` on `labeled`/`unlabeled`; `assignee` on `assigned`/`unassigned`; `changes {title_from, body_from}` on `edited`; `new_issue {repository_id, number}` on `transferred` |
| `pull_request` | `pull_request {number, node_id, title, body, state, draft, merged, merge_commit_sha, user, head {ref, sha, repository_id, full_name}, base {ref, sha}, additions, deletions, updated_at}`; `requested_reviewer` or `requested_team {id, slug}` on review-request actions |
| `pull_request_review` | `review {id, state, body, user, commit_id, submitted_at}`, `pull_request {number, head_sha}` |
| `pull_request_review_comment` | `comment {id, body, user, author_association, path, line, in_reply_to_id, review_id, commit_id, created_at, updated_at}`, `pull_request {number}`, `previous_body` on `edited` |
| `pull_request_review_thread` | `thread {node_id, first_comment_id}`, `pull_request {number}` |
| `push` | `ref`, `before`, `after`, `forced` |
| `status` | `id`, `sha`, `context`, `state` |
| `check_run` | `id`, `name`, `status`, `conclusion`, `head_sha`, `app_id`, `check_suite_id`, `external_id`, `pull_requests` (numbers) |
| `check_suite` | `id`, `status`, `conclusion`, `head_sha`, `app_slug`, `latest_check_runs_count`, `pull_requests` |
| `workflow_run` | `id`, `run_attempt`, `workflow_id`, `name`, `path`, `event`, `status`, `conclusion`, `head_branch`, `head_sha`, `check_suite_id`, `created_at`, `run_started_at`, `updated_at`, `pull_requests` |

The field set is what routing, requests and read models need, which is at least what
today's ingress routes on (`service/ingress.py:224`), the sponsor hints and review-request
evidence it keeps, and the list index's projections (`service/github_reads.py:181`).
Ingress publishes them for every repository of the installation; filtering out
Concurro's own accounts is the consumer's decision.

### Delivery recovery

Served by whoever holds the App's private key: the Python service until the phase 3
Cutover, then the task application's publication module (built in 3/M3), on its listener
on the platform network. Ingress redelivers a delivery that has no receipt and no
successful or pending attempt ([phase 2 plan](architecture/phase-2-plan.md#recovery)).

| Call | Request | Response |
| ---- | ---- | ---- |
| `GET /v1/deliveries?cursor=` | | `{deliveries: [{id, guid, event, action, delivered_at, status, status_code, redelivery}], next_cursor}`, from GitHub's `app/hook/deliveries`; `status` tells a pending attempt from a failed one |
| `POST /v1/deliveries/{id}/redeliver` | | `202 {}`. Repeating it is harmless: a redelivery keeps its GUID, which ingress deduplicates |

### Coverage gaps

GitHub keeps deliveries for three days. When ingress can't recover a period, and once at
its first start, it publishes `ingress.coverage.gap {from, to, installation_id}`, a fact
([phase 2 plan](architecture/phase-2-plan.md#coverage)). Reconciliation doesn't depend
on knowing which issues are enrolled. For every target repository, the task application
lists what changed since `from`:

| Read | Recovers |
| ---- | ---- |
| `issues/comments?since=` | Commands, sponsorship, mentions and discussion on issues and PR conversations, enrolled or not |
| `pulls/comments?since=` | Mentions and commands in review comments |
| `issues?state=all&since=` and each listed issue's `issues/{n}/events` | Labels, assignments and review requests, through the [enrollment channels](#enrollment-channels). This relies on labeling, assigning and requesting a review updating the issue's `updated_at`, so `since` lists every issue that has one ([checked before 3/M3's label channel](#enrollment-channels)). The repository-wide `issues/events` listing isn't used: GitHub stops it after 300 pages of 100 (HTTP 422), with no `since` |
| `pulls?state=all&sort=updated`, back to `from`, and each PR's reviews | Review bodies with mentions; PR closes, merges and head changes |

Each comment becomes a request with its own source, so whatever arrived anyway is
deduplicated. Then every active task re-reads its decision inputs (PR head and merge
state, reviews, CI gates), as the reconciler does today, rewrites any surface whose
content changed, and republishes its snapshots. Gap handling is part of 3/M3; the
assignment and mention reads apply once those channels exist.

The task application also re-reads the [target index](#target-index-and-baseline) for the
affected repositories and republishes its snapshots. The backoffice API's read model marks
the period's index rows stale until those snapshots replace them. It holds no GitHub
credential, so it never reads GitHub itself.

Not recoverable: a comment created and deleted within the gap; intermediate revisions of
a comment edited more than once within it (the latest revision is seen, and an edit whose
author can't be established stays [unresolved](request-intake.md#authorization));
reactions.

### Enrollment channels

Label, assignment and review-request requests are created from the issue's events
timeline, not from deliveries. The label and review-request channels are part of 3/M3,
since label enrollment and standalone reviews are today's deployed workflows; the
assignment channel is new and comes later:

- A delivery wakes the channel for its issue. The channel reads `issues/{n}/events` and
  creates one request for each relevant timeline event (`labeled`/`unlabeled` for
  `concurro`, `assigned`/`unassigned` for the account, `review_requested`/
  `review_request_removed` for the account) that has no request yet.
- The source is the timeline event's ID, and `source_at` its `created_at`, so the funding
  boundary needs no later correction.
- If the delivery's action isn't in the timeline yet, the channel reads again with
  backoff; it never matches by nearest time. Reconciliation runs the same step, so a
  delivery and a reconciliation create one request between them.
- **Checked against the demo target's history (November 2026, read-only;
  [evidence](contracts/timeline-check.md)):**
  - Every `labeled`, `unlabeled`, `review_requested` and `review_request_removed` event
    has a numeric ID, its actor, `created_at`, and the label or requested reviewer (and
    the requester).
  - Repeated actions get distinct IDs: the same stage label added and removed several
    times (by Concurro's App), and a review request removed and re-added two seconds
    apart. The `concurro` label itself was only ever added once per issue there, so a
    person removing and re-adding it wasn't observed.
  - IDs are stable across reads, and `issues/{n}/timeline` returns the same IDs as
    `issues/{n}/events`.
  - The repository-wide `issues/events` listing carries each event's issue number, but
    is capped: on a larger repository it stops after 300 pages of 100 events, about
    22 months back there. Reconciliation lists issues by `since` and reads each one's
    events instead ([coverage gaps](#coverage-gaps)).
- **Not yet checked:** how soon after its webhook an event is listed, which needs a
  controlled label change on a test repository; that each of these actions updates the
  issue's `updated_at`, which reconciliation relies on; `assigned`/`unassigned` events,
  and a person removing and re-adding `concurro`, which that history doesn't contain.
  All are checked before 3/M3's label channel is built; the read-again-with-backoff rule
  doesn't depend on the delay being short.

### Requests

`POST /v1/requests`, called from 3/M3 by the Python backoffice, which forwards each browser
command it has checked and signed, with the browser action's ID as `Idempotency-Key`.
The backoffice API never calls it. The task application's own channels write the same record
in-process, in the transaction that records the event it came from, from 3/M1.

| Field | Type | Meaning |
| ---- | ---- | ---- |
| `issue` | `{repository_id, number}` | The task's issue, or the issue to enroll |
| `kind` | enum | `enroll`, `unenroll`, `start`, `approve`, `stop`, `retry`, `resume`, `extend`, `invalidate`, `sponsor`, `withdraw`, `funding`, `conversation` |
| `argument` | object | Per kind: `approve {plan_id}`, `extend {plan_revisions}`, `resume {description_only}`, `sponsor {provider?}`, `withdraw {provider}`, `funding {provider, mode}`; otherwise empty |
| `proof` | string | A [command proof](#command-proofs) the Python backoffice signed for this exact request; the actor comes from it |
| `source_at` | time | When the actor submitted it; bound by the proof, since it orders the request |
| `detail` | object | Channel data frozen with the request, such as `feedback` for `retry`; bound by the proof, since it changes the task's requirements |

The response is `202 {request_id, status, reason?}`; `GET /v1/requests/{id}` returns the
same with `task_id` and `applied_at` once known. The request carries no actor field: the
task application verifies the proof's signature, refuses with `403 proof_mismatch` a
proof whose bindings differ from the request, and takes the actor `{id, login}` from
it. Statuses and authorization are
[request intake's](request-intake.md#requests): the actor's permission is checked again
when the request is applied, whatever the backoffice checked. A channel's record adds its
channel, source, reply route and evidence (the delivery); a comment's `source_at` is its
creation or edit time. The operator's host commands aren't requests and have no actor.

**Retries keep one action ID.** The frontend gives each command the user makes an action
ID that survives its retries (a `concurro-backoffice` change: today's `actions.command`
sends none, `src/lib/api/live.ts:153`), and the Python backoffice keeps the original
`source_at` for each action ID. The action ID is the request's idempotency key, and a
repeat is matched on the actor and the command's contents (issue, kind, arguments,
`detail.feedback`, `source_at`), not on its proof: a retry after the proof has expired is
signed again with the same contents and returns the recorded response, applying nothing
twice. Different contents under the same action ID are `409 idempotency_conflict`.
Tested: a command that succeeds, whose response is lost, and whose retry after its proof
expired returns the original outcome.

**Edited comments** follow [the decision on edits](#decisions): each revision is its own
source, and a later revision supersedes an earlier one only while that one is pending.

#### Cutover sources

One rule decides which sources the task application turns into requests, whether they
arrive by live delivery, a re-mapping, a replay or a reconciliation read. The activation
time `A` is set once the Python daemon's exit is confirmed: its stop signal starts a
drain, and a tick already running finishes after the stop was requested
(`service/scheduler.py:94-100`). The original GitHub source's own time decides: a comment
revision's creation or edit time, a timeline event's creation time (`source_at`).

- **Before `A`, a source is history:** shown, and read for requirements, never a request
  and never part of payer selection. The consent fold, fills and the funding boundary
  use only sources dated at or after `A`, so a Python-era sponsor comment never makes
  its author pay for a task the task application runs; they sponsor again. That holds
  however late its delivery arrives: Python reads comments itself
  (`engine/controller.py:2411`), so it may have acted on a comment whose first delivery
  reaches the stream after it stopped.
- **At or after `A`, it's the task application's,** however it arrives, and it's never
  reissued.
- **Strictly before `A`,** after the exit, the operator lists the sources dated within the
  announced window, which the team is asked to keep quiet, `/concurro` comments and
  review requests for standalone reviews included. The final registry keeps only each
  issue's latest outcome and command cursor, which a refused `start` also advances
  (`engine/commands.py:175`), so each source is checked against evidence specific to
  it: Python's reply to it, the writes it caused (`mutations`), the issue's dashboard
  and labels, records such as `approval.comment` (`engine/commands.py:227-238`). One with
  positive evidence that Python missed it, such as a `start` on an issue with no
  registry row, is reissued by its author, whom the operator asks; one whose outcome
  stays ambiguous is left to its author's explicit fresh decision, and the task
  application takes no automatic action on it. None is applied implicitly.

The stream position is for transport only: the operator records the stream's
incarnation and `S`, the last sequence the Python service's consumer acknowledged, and
the task application's consumer starts at or before `S + 1`, with the source rule
filtering what it reads. A delivery forwarded out of order, or a coverage event at `S`,
doesn't change which sources count. An old `concurro` label found by a timeline read
after the Cutover enrolls nothing; a command made after `A` applies. The rehearsal posts
one command during the drain and one after `A` but before the task application starts,
and each acts exactly once.

### Request events

Published from 3/M1, for M1's contract test consumer and, from 3/M3, the backoffice
API's read model; publication reads the same outcomes in-process for replies.

| Type | Data |
| ---- | ---- |
| `request.tasks.{applied,refused,superseded}` | `request_id`, `task_id?`, `issue`, `kind`, `argument`, `actor`, `channel`, `source`, `source_at`, `reason?`, `reply` (route) |
| `request.tasks.{blocked,stopped}` | `request_id`, `task_id`, `provider`, `actor`: a withdrawal's later [steps](#withdrawal-steps) |
| `request.source.received` | `source`, `issue`, `channel`, `reply`, `acknowledge` (`reaction` for mentions) |
| `request.source.final` | `source`, `request_ids`, `failure?` (`interpreter_deadline`, `interpreter_unavailable`, `unresolved_edit`) |

Publication replies to a source once it's final and each of its requests has an outcome;
a final source with no requests gets its failure reply.

### Fills

A record of the funding policy, from 3/M3. Personal credentials are today's deployed
workflow, so they work before the Cutover; M1 and M2 run on org funding only, on the test
repository. After the Cutover, only sources dated at or after `A` take part in the fold,
fills and the boundary ([cutover sources](#cutover-sources)).

Whether the initiator's credentials fill an empty slot is decided once, as of the funding
boundary ([cross-cutting rules](README.md#cross-cutting-rules)). When the boundary is
ready to freeze, the funding policy module asks for a fill of `{task_id, issue
{repository, number}, target, boundary {kind, source, at}, initiator {id, login},
requirements}`. `target` is the upstream repository credentials are restricted by.
`requirements` lists every selected role member resolved to `{role, provider, profile,
route}`, a role possibly having several, as `engine/sponsorship.py:119` resolves them.
A job reads the initiator's credentials from the [vault](#vault) at `boundary.at` and
matches them as `listed_routes` does, a provider with route-era records keeping its
configured route (`engine/sponsorship.py:85-105`). The answer, `{provider: {eligible,
reason?}}`, is recorded once per task and boundary source. `eligible` is `yes`, `no` or
`unknown`; `unknown` (the credential history doesn't reach back far enough) leaves the
boundary pending, so nothing is charged. Each `yes` slot becomes active for the initiator
with implicit provenance; the others stay unassigned, and a credential added later never
fills them.

- **A fill applies only to the state it was asked for.** The job persists the candidate
  boundary, the version of every source it folded, the issue's source revision (raised by
  every new or changed sponsorship source as it's received, `service/registry.py:611-622`)
  and the consent revision. The transaction that applies the vault's answer compares all
  four, and recomputes the fill if any changed, as Python checks the source revision
  across its reads (`service/shadow.py:651`, `:1053-1058`). A withdrawal recorded while
  the vault read is outstanding makes its answer stale.
- **The funding mode comes from policy activations, not from the vault.** Vault history
  says whether a credential existed at the boundary, not whether the target was org or
  contributed then. The task application records an activation (configuration digest and
  funding mode) at its own clock whenever it reads the target's current configuration,
  at the fork's head, and finds it differs from the active one, never backdated
  (`service/registry.py:1182-1207`). Reading a pinned SHA, to recover or resume an older
  task, never activates a policy, as `service/worker.py:345-347` guards and
  `service/tests/test_sponsored_admissions.py:1088` tests. A source's mode
  is the activation that covers its time and has the exact digest of the task's pinned
  configuration (`:1209-1219`); with none, it's `unknown`. Each slot records `org`,
  `contributed` or `unknown` as `source_funding_mode` and keeps it, and implicit personal
  funding happens only under `contributed` (`exchange.py:669-673`). A delayed org-era
  label processed after the target switched to personal funding gets no grant until an
  explicit sponsor.

### Grants

A grant is the funding policy module's record of how one attempt is paid, keyed by the
attempt ID; it's internal, not a wire contract. In M1 and M2 every grant is `task_org`,
decided in the admission transaction with no credential selected. `task_contributed`
comes in 3/M3. `interpretation` comes with mentions, a new channel planned after phase 3.
A grant's request is one of three variants, tagged by `purpose`:

| Field | `task_contributed` | `task_org` | `interpretation` |
| ---- | ---- | ---- | ---- |
| `attempt_id`, `task_id`, `issue` | ✓ | ✓ | ✓ (`task_id` once one exists) |
| `role`, `profile`, `provider`, `models` | ✓ | ✓ | `role` is `interpreter` |
| `limits {tokens, lease_seconds}` | ✓ | ✓ | ✓ |
| `decision` | The frozen consent decision, below | `{consent_revision}` | |
| `payer {id, login}` | | | The commenter |
| `retry_of` | Optional ([retries](#retries-and-exclusions)) | Optional | |

A `task_contributed` decision holds `sponsor {id, login}`, `provenance`
(`explicit_comment`, `task_start`, `label`, `assignment`, `review_request`, `mention`),
`consent_revision`, `slot_epoch`, `boundary {kind, source, at}` and `evidence` (the
folded sources). `role` and `profile` filter credentials as
[ordered credentials](credential-list.md#filters-and-selection) define them.

The grant, with the same `purpose` tag:

| Field | Meaning |
| ---- | ---- |
| `grant_id`, `attempt_id`, `purpose` | |
| `status` | `issued` or `revoked` |
| `payer` | The sponsor or commenter; absent for `task_org` |
| `provider`, `slot_epoch` | `slot_epoch` for `task_contributed` only |
| `credential` | `{entry_id, record_id, version, route, custody}`, `custody` being `secret_store` or `relay`; the org route for `task_org` |
| `models`, `limits` | What the grant allows: models, and `{calls: 1, tokens, lease_seconds}`. Selection may lower the requested limits, never raise them |
| `issued_at`, `revoked_at?` | |

| Refusal | Purposes | When | The task does |
| ---- | ---- | ---- | ---- |
| `consent_cut_off` | `task_contributed` | The sponsor's withdrawal cutoff covers this slot epoch | Nothing more under that decision |
| `no_eligible_credential` | `task_contributed` | No eligible credential | `awaiting_credentials` |
| `no_eligible_credential` | `interpretation` | The commenter has no interpreter credential | Finalizes the source with `interpreter_unavailable`; exact mentions still work |
| `boundary_pending` | `task_contributed` | The decision's boundary isn't frozen | Waits |
| `attempt_cancelled` | All | The attempt was cancelled before a grant was issued | Nothing more for that attempt |

Selection follows the [architecture's grant cases](architecture.md#services): implicit
provenance fills from credentials eligible at `boundary.at`; a new attempt under existing
consent selects among those eligible now; a replay returns the original grant with its
current `status`. A contributed selection reads the vault, so it runs as a job and
records its grant on the attempt's row; a `stop` that cancels the attempt first marks
that row, and a selection committing after it records `attempt_cancelled`. The grant is
revised, never replaced, when it's revoked.

For a continuation, admission checks the [v1 journal rules](#retained-session-eligibility)
against the required archive revision and the requested scope, ordinal and prompt
before selecting a payer. A failed check blocks admission before any attempt, grant,
call reservation, charge or submit effect is created. On success pin the checked
revision for submission and replay.

The call is charged with the payer decision. An org grant is decided, and its call
charged, in the admission transaction ([M1](architecture/phase-3-m1-plan.md#attempts)).
A contributed attempt reserves its call at admission against `max_model_calls` and
protected capacity ([cross-cutting rules](README.md#cross-cutting-rules)); the grant makes
it a charge, and a refusal or cancellation releases it. A grant always covers one call.

### Retries and exclusions

Part of 3/M3, with personal credentials: ordered credentials and their automatic
fallback are today's behavior ([ordered credentials](credential-list.md)).
`retry_of {attempt_id, failure {category, model, retry_after?}}` names the failed attempt
a new one replaces. The attempts module sets it only when the failed turn's
`provider_requests` is `some` or `none` and its failure category is one of these; an
`unknown` outcome never leads to an automatic fallback. The funding policy records the
exclusion in the same transaction that records the next credential's selection, following
[ordered credentials](credential-list.md#automatic-fallback-and-recovery):

| Category | Excluded for this task | Until |
| ---- | ---- | ---- |
| `auth` | The credential version | It's replaced |
| `model_denied` | The version, for that model only | It's replaced |
| `quota_exhausted` | The version | `retry_after` if given, else it's replaced |
| `rate_limited` | The version | `retry_after` if given, else the funding policy's backoff |
| `unavailable` | The version | `retry_after` if given, else the funding policy's backoff; never labeled exhausted |

### Withdrawal steps

Part of 3/M3. A withdrawal records three steps, each reported to its author on GitHub and
published for the backoffice: "withdrawal recorded" when the `withdraw` request is
applied (`request.tasks.applied`); "new grants blocked" when the cutoff commits, in the
same transaction that would record a grant (`request.tasks.blocked`); and "spending
stopped" once every session under the revoked grants has ended
(`request.tasks.stopped`). From phase 5
that's each session the exchange lists in `exchange.grant.released` having its binding
closed (`inference.binding.closed`) and, for relay custody, its binding `revoked`. Before
phase 5 a time bound stands in ([Python exchange](#python-exchange)).

### Turn events

In phase 3 the task application records them from its polls of the Python exchange
([bridge](#python-exchange)) and publishes them, both from 3/M1: to M1's contract test
consumer, and from 3/M3 to the backoffice API's read model. From phase 5 the Rust
exchange publishes them itself, with the same fields. Blob fields carry [turn artifact references](#blobs).
A transcript's `blob` and `bytes` identify the original gzip by digest and compressed
size. Its turn link resolves the searchable snapshot as soon as ingestion commits.
Omit `transcript` if the archive is absent or unreadable. For an unreadable archive,
include `transcript_error` with the failed decoding step and, when bytes were
decoded from the payload, their digest and size. The turn outcome describes the
model result independently of this diagnostic.

| Type | Kind | Data |
| ---- | ---- | ---- |
| `turn.<turn>.submitted` | Fact | `turn_id`, `attempt_id`, `task_id`, `grant_id`, `stage`, `role`, `harness`, `model`, `effort`, `profile`, `session {scope, turn}`, `bundle_id`, `limits`, `spec` blob, `prompt` blob, `submitted_at` |
| `turn.<turn>.status` | Snapshot | `status` (`queued`, `running`, `finished`, `cancelled`), `created_at`, `started_at`, `finished_at`, `runner {session_id, run_id, run_attempt}` |
| `turn.<turn>.finished` | Fact | `outcome`, `reason`, `provider_requests`, `response`, `workspace` blob, `transcript {blob, bytes}?`, `transcript_error?`, `elapsed_seconds`, `provider_failure {category, model, retry_after}?`, `funding_ended` |

The `prompt` blob is the prompt as today's diagnostics reconstruct it from the request
and its feedback (`prompt(request, feedback)`, `service/diagnostics.py:145`): its
instructions, repository snapshot, schema and untrusted context, as the frontend shows
them (`backoffice/turns.py:135`). A turn whose prompt can't be rebuilt has none, as
today.

Two separate questions:

- **`outcome`** is whether the turn produced a usable result: `succeeded`, `failed` (it
  ended without one) or `unknown` (it may have produced one that was lost). `reason` is
  open and says why: `invalid_output`, `timed_out`, `terminated`, `lost`,
  `queue_expired`, `admission_unknown`, and so on.
- **`provider_requests`** is whether paid work may have happened: `none` only when the
  exchange's records show no provider request for the turn and none is still being
  recorded; `some` when at least one was made; `unknown` otherwise. Only `none` proves the
  turn cost nothing.

Neither authorizes a retry by itself: the task application keeps today's rules, charging
every attempt a call before dispatch and limiting uncertain attempts per operation.

The backoffice API's read model and the operator views take diagnostics from these, so
the diagnostics store goes.

### Task snapshots

`task.<task>.state`, revised on every task change. The task application writes it to its
outbox from 3/M1; the backoffice API's read model consumes it from 3/M3. Fields:

| Field | Meaning |
| ---- | ---- |
| `task_id`, `issue`, `kind` | `kind` is `contribution` (3/M1) or `standalone_review` (3/M3) |
| `author` | The task author: the issue's author `{id, login}` |
| `initialized` | False while a task is enrolled but waiting, such as for a manual `start` |
| `mode` | `manual` (3/M1), `autonomous` or `review` (3/M3) |
| `stage`, `stage_reason` | Absent until initialized. Values start from today's stages (`engine/vocab.py:6`), reviewed as each milestone ports them |
| `enrollment` | `{channels, first {channel, actor, at}}`, from 3/M3 |
| `plan` | `{id, approved}`, from 3/M2 |
| `budgets` | `{model_calls, fix_rounds, plan_revisions}`, each `{used, max}` |
| `attention` | Whether the task needs a human |
| `pull_request` | `{number, head_sha}`, once one exists, from 3/M2 |

`task.<task>.consent` (3/M3) holds `consent_revision`, `boundary {state, kind, source,
at}` (`state` is `pending` or `frozen`), `provider_funding` and `slots`: per provider,
`{status, payer, provenance, epoch}`. `status` is `unassigned`, `active` or `withdrawn`;
`payer` is `org`, the initiator by an implicit fill (`provenance` `task_start`,
`label` or `review_request`), or an explicit sponsor (`explicit_comment`), each with
their `{id, login}`. The backoffice API shows a non-writer only the slots where they're
the payer ([backoffice API](#backoffice-api)).

`task.<task>.notice`, one entity per notice: `notice_id`, `kind`, `state` (`open`,
`resolved`, `superseded`) and `data`, the kind's fields. Kinds start from today's notices
(`engine/actions.py:52`), reviewed as for stages.

`task.<task>.report`, one entity per report: `report_id`, `kind` (`intake` from 3/M1,
then plan, review and outcome reports from 3/M2), `step`, `body` and `digest`. Reports are
small; they travel in the event, and publishing them on GitHub stays an
[open question](architecture.md#open-questions).

### Consent facts

`consent.<repository_id>.<number>.{granted,replaced,withdrawn}`, from 3/M3: one fact per
change to one user's consent on one issue, published whether or not the issue has a
task, so sponsorship before enrollment is included. Data: `user {id, login}`, `issue`,
`task_id?`, `provider`, `source`, `provenance`, `at`, and for `replaced` the user or
source that replaced it. The backoffice API's "my sponsorships" projects these by the
viewer's numeric ID, whatever their current slot or writer status, as today's view keeps
historical own consent after writer loss (`backoffice/tasks.py:645-660`).

### Target index and baseline

The backoffice's lists keep today's selection (`service/list_index.py`): enrolled or
sponsored issues, and ones already held whose `concurro` label has since been removed
(`:449-455`); PRs linked to a task, found by a task branch's name (`task_pulls`), or
already held (`:483-488`, `:815-830`); and executor workflow runs (`:134-140`). A PR on a
task branch from before the cutover stays discoverable with no task linking it, though
`task_pulls` searches only the 100 most recently updated PRs
(`service/github_reads.py:298`): the cutover exports the PR numbers in Python's list
index to seed the first build, and a cold build with no seed pages task-branch discovery
through every PR. Making
them a browser of every issue, PR and run would be a product decision, not part of
phase 3. The task application keeps that index from `github.*` events, and from a GitHub
baseline read at its first start and after a [coverage gap](#coverage-gaps):

- **Rows** are `index.<repository_id>.{issue,pull_request,workflow_run}` snapshots, and a
  row that leaves the index (an issue deleted or transferred, as today) is published as a
  removal, never left stale.
- **Gap repair replaces a repository's index as a unit:** a full-membership snapshot of
  that repository's rows, so a row deleted during the gap disappears even though no
  event announced it. Known PRs are kept through a repair.
- **A rebuild doesn't overwrite what changed during its scan.** It records the
  repository's index revision before reading GitHub, as Python records versions before
  its reads (`service/list_index.py:815-830`). Before committing the replacement, every
  row changed since that revision keeps its newer state over the scanned one, and the
  replacement takes the next revision. Tested: a webhook arriving during the scan
  survives the replacement.
- **One ordering for rows and replacements.** Every index change carries the
  repository's index revision, a counter that rises with each row upsert, row removal and
  whole-repository replacement. A removed row is kept as a tombstone with its revision.
  The backoffice API applies a row change only if its revision is above both that row's
  and the repository's last replacement revision, so a delayed older upsert never
  revives a deleted row, and a newer row survives an older replacement that arrives
  after it. Tested: a deleted issue whose older upsert arrives after a rebuild stays
  deleted; a new row arriving before an older repository replacement survives.

`GET /v1/baseline` (3/M3, the backoffice API's bearer) returns, paged, everything the
views show:

- historical Python turns with repository, issue, harness session, recorded staffing
  associations and the serving assignment where known. The baseline distinguishes
  these from any independently evidenced per-turn dispatch and applies the source
  turn's diagnostics permissions. [Archive import](#python-archive-import)
- every current task, consent and index snapshot, index tombstones with their
  revisions, and each turn's latest status snapshot (status, runner session, run ID,
  start time) with its revision, so a turn running throughout a rebuild still shows its
  live run;
- the durable records behind the views' history: every [turn](#turn-events) submission
  and finish (with its prompt and transcript references), every
  [request outcome](#request-events) and every [consent fact](#consent-facts), so a
  rebuilt read model keeps diagnostics and a former payer's sponsorship history after the
  stream's retention (seven days in ingress's example settings) has dropped their events.

All its pages are read from one consistent database snapshot, so a change committed
while the backoffice API pages through it is on one side of the baseline, never split.
There's no outbox position to resume from: the producer's local insertion order isn't the
stream's order. Instead the backoffice API:

1. ensures its durable consumer is in place before taking the snapshot, retaining
   its position if it already exists and buffering arrivals during the refresh;
2. reads the baseline;
3. applies the messages buffered meanwhile and every later one, deduplicated by fact ID
   and by snapshot revision, so one that's also in the baseline changes nothing.

At cutover, refresh even a populated, caught-up read model after the archive import
commits and the task API is available. Finish loading the snapshot and applying the
buffered overlap before switching the frontend. Historical imports emit no live turn
events that could otherwise add those rows. In the rehearsal, read page one to pin
the snapshot, then commit a newer turn revision and deliver its event before reading
the final page. At the switch, the view must contain the imported turn from the
snapshot and the newer revision from the buffer.

Tested: an empty read model against a target with an inactive PR and a completed run
older than retention; a completed turn's prompt and a replaced pre-enrollment
sponsorship, also after retention; a running turn whose status doesn't change during the
rebuild; a write committed during pagination; an outbox publication delayed past the
baseline read.

### Runner dispatch

Records of the exchange integration and publication modules, internal to the task
application:

| Record | Data |
| ---- | ---- |
| Dispatch | `session_id`, `task_id`, `turn_id`, `target_id`, `repository_id` (the fork), `ref {name, sha}`, `workflow`, `launcher_version`, `inputs` (defined by the launcher version) |
| Session ended | `session_id`, `turn_id`, `repository_id`, `ref`, `reason` (`ended`, `cancelled`, `replaced` by a new session for the turn, or `turn_finished`) |

Publication creates the run ref and dispatches the workflow, and deletes the ref when the
session ends. A session's ending is recorded before anything else happens for that
session, so a dispatch or ref creation for an ended session, even a delayed one, is
settled as `skipped` without a write. A dispatch is never resent
([publication outcomes](#publication-outcomes)).

### Write admission

An internal rule of the publication module. It refuses any write outside these rules,
whatever decision asks for it. Every
write is to one of the task application's target repositories, and to the task's own
issue or fork.

| Surface | Account | GitHub operations | Admitted only when |
| ---- | ---- | ---- | ---- |
| `dashboard` | App | Create, then edit, one issue comment | The comment is the App's and carries the task's dashboard marker |
| `notice` | App | Create a comment; edit it to resolve or supersede | Editing only the App's own comments with that notice's marker |
| `reply` | App for comments, reviewer account for mentions | Create a comment on the source's issue, or reply in its review thread | The source is final |
| `reaction` | Reviewer account | Add `eyes` to a comment | The comment mentions the account |
| `stage_label` | App | Add or remove `concurro:stage:*` and `concurro:attention`; create those labels | Only those labels, only on the task's issue |
| `run_ref` | App | Create or delete `concurro/runs/<session>` on the fork | At the target's `config_sha`; never another ref |
| `dispatch` | App | `workflow_dispatch` of the target's launcher workflow at the run ref | The workflow is the target's launcher; the session hasn't ended |

M2's surfaces (branches, PRs, reviews, threads, checks, artifacts) add rows as they're
ported, from today's allow-list (`service/admission.py:18`).

### Publication outcomes

Internal records of the publication module, not events. Each write is recorded, then
marked `sending` just before its request goes out, then settled with an outcome:

| Field | Meaning |
| ---- | ---- |
| `surface` | `{task_id?, kind, key}`, `kind` being one of the write admission surfaces |
| `revision` | The snapshot revision written, for snapshot surfaces |
| `status` | `written`, `failed` or `skipped` (a newer snapshot replaced it, or a dispatch whose session the exchange replaced or whose turn ended) |
| `written_at` | When GitHub accepted it; an approval by time binds the latest plan written before its `source_at` |
| `failure` | `{class, http_status, message}`; `class` is `rejected`, `forbidden`, `not_found` or `not_admitted` |
| `result` | `{comment_id?, url?, commit_sha?, pr_number?, check_run_id?, run_id?}` |

- **A write never marked `sending` was never sent,** so after a crash it's sent.
- **A write marked `sending` with no response recorded is uncertain,** as is one answered
  `5xx`, `408` or `429`, and is settled by reading back. A readback answered `forbidden`
  or `not_found` settles it as `failed`; one that fails is retried with backoff, across
  restarts.
- **Labels and refs** are settled by what the readback shows, and a write found absent is
  sent again: adding a label is idempotent, and creating a ref that exists is refused.
- **Comment creates stay pending until their marker is seen,** each carrying a marker
  unique to that write, since a create can still be in flight when the readback finds
  nothing. The operator's `writes resolve` settles one: `--written <url>` adopts the
  comment; `--failed` stops waiting and sends nothing, and a late create carrying the
  marker is later adopted as written; `--resend` is an explicit override, recorded as the
  operator's, that can duplicate the comment if the first create still lands.
- **Dispatches are never resent,** since a run can be accepted before it's listed. If its
  run never appears, the exchange gives the queued turn a new session and the next poll a
  new dispatch.

The task never sees "unknown": a decision that waits on a write waits until it's
settled. The rules are [M1's](architecture/phase-3-m1-plan.md#publication).

### Read tokens

Publication mints read tokens in-process for the other modules: an installation token
limited to read permissions and one target repository, cached until 10 minutes before
expiry. Tokens are never logged or stored outside the process.

### Blobs

From 3/M1 the task application owns turn diagnostics. Specs, reconstructed prompts,
responses and workspace changes stay in its PostgreSQL database as content-addressed
artifacts (`{sha256, bytes, media_type}`). Specs omit endpoint credentials and tokens.
The application stores transcript entries as searchable rows and sends original
`.tar.gz` bundles to the existing private Hetzner S3-compatible object store.

#### Transcript search

New runner results and historical imports use the same ingestion path. For each
transcript or retained-session archive:

1. Take the original gzip bytes (base64-decoded for payload v1), record their SHA-256
   and length, and decompress the tar stream. Its SHA-256 identifies the transcript
   snapshot across different gzip encodings. Read the harness's transcript JSONL
   files, retaining member path, tar modification time, member ordinal and line number.
2. Keep each representable event as `jsonb` with its harness and event kind, including
   unfamiliar shapes. For a malformed line or valid JSON PostgreSQL cannot store
   (for example an escaped `\u0000`, an unpaired surrogate or an out-of-range number),
   keep the exact source line bytes as `bytea`, its position and a diagnostic, with
   SQL NULL in the JSON column. Check representability before insertion or isolate the
   conversion in a savepoint. Other database errors fail the transaction.
   Extract message text, tool names, arguments and results wherever decodable,
   including from fallback rows. Escape unrepresentable characters visibly in the
   search text, separating escapes from adjacent words for indexing. Use the formats in
   `backoffice/turns.py:170-250` without its clipping or item cap.
3. Key events by (tar digest, member ordinal, line number). For display, sort by
   member modification time, then path in Python string order, then archive ordinal
   for ties. Within each member preserve line order and the order of parts within
   a line, as in `backoffice/turns.py:170-195`. Index extracted text
   with PostgreSQL full-text search using the `simple` configuration and a GIN index
   on `tsvector`. Split large text into ordered parts of the same event as needed
   for indexing. Link the snapshot and original gzip digest to every source turn
   and retained session that supplied it, including the repository, issue, scope and
   session turn when known. A cumulative session snapshot can contain earlier turns'
   messages. The link identifies the snapshot observed at a turn.
4. Commit entries, searchable text, source links and the pending upload together.
   For a live result, the same transaction settles the turn and writes its single
   `finished` outbox fact. Historical imports commit diagnostics and provenance only.
   They emit no live turn events, including for sources with no turn ID. The backoffice
   loads verified historical turns from the baseline. Replay reuses the same records.

If a live result has no archive, record that absence. If base64 decoding, gzip
integrity validation or tar reading fails, discard any partial index and record
the archive error with the turn. When original bytes are available, queue them
for archival inspection by digest. Their verified upload does not make them
restorable. Commit the independently validated result and its finish fact even
when the archive is unreadable. Archive failure or replay never triggers a model
retry. A continuation requiring that revision reports it unavailable at the start
of admission, before selecting a payer or consuming call budget. It cannot fall
back to an older revision.

At ingestion, retain the root journal's status, scope, completed ordinal, request
digest and prior instructions and response schema when present. Admission uses
these fields to check the requested continuation. Readable transcripts remain
searchable even when their journal cannot serve that request. An eligible
revision can be restored from the pending upload bytes or the verified object.

At schema migration, set compression on the artifact payload, transcript JSON,
fallback source bytes and extracted text columns to LZ4 if `pg_settings.enumvals`
for `default_toast_compression` includes `lz4`, otherwise PGLZ.

SQL search is part of M1. The browser search UI can follow separately. From M3 the
backoffice API checks the turn's diagnostics tier and fetches its ordered entries
through the task application's internal `GET /v1/turns/{turn}/transcript`. Apply the
same viewer scope to matches before returning snippets or counts. Only a verified
turn link can expose a snapshot through the backoffice.

#### Original archive bundles

The task database keeps a pending upload record keyed by gzip SHA-256, with the
original bytes and byte count, independently of the worker job and its retry limit.
The worker writes to `<prefix>/sha256/<gzip-sha256>.tar.gz` in the configured
Hetzner bucket. After an uncertain PUT it reads the same key. Only a readback
matching both size and SHA-256 marks the object available and releases the pending
bytes in one transaction. A failed PUT or readback leaves search and session restore
available. Multiple gzip encodings of one tar retain separate keys and share indexed
events.

After the worker exhausts its attempts, the pending record retains the last error.
The host operator can inspect it with `effects pending` and use
`archives retry <gzip-sha256>` to requeue that upload. Concurrent or repeated retries
schedule at most one active job per digest. A verified digest is already complete.
Retrying an upload never creates a turn or repeats model work.

The task application holds the S3 credential. Session restore verifies the linked
gzip against its digest, using the pending bytes until the object is verified.
Backoffice diagnostics read PostgreSQL through the task application.
Keep rows and objects while a turn, session, report or import record
references them, including pending uploads during recovery. Retain database backups
and their referenced archive objects together. Phase 5's exchange accepts runner
uploads through its session API.

#### Python archive import

After the Python daemon exits, read its final diagnostics database and both archive
directories from the same deployment. The import command runs the normal archive
uploader against the task database and Hetzner, processing only archive jobs. It
can resume pending uploads and operator-requeued jobs with the controller, ingress
consumer and other workers stopped.

| Source | Reference preserved |
| ---- | ---- |
| `diagnostics_dir/blobs/<prefix>/<sha256>` | The original digest and each matching `diagnostics.sqlite3` turn ID, repository, issue and generation. The turn's `transcript` field holds the digest and `transcript_bytes` holds the size (`service/diagnostics.py:208-231`). |
| `state_dir/sessions/<repository directory>/<scope>.tar.gz` | The configured repository and session scope. The filename identifies the scope. The root journal can also identify a harness turn within it (`service/worker.py:412`, `engine/agents.py:80-94`, `engine/session_entry.py:83-96`). |

Inventory both directories and SQLite references, including files without a turn
row. Verify the diagnostics filename, SQLite digest and compressed size where known,
then pass each archive through the same [ingestion path](#transcript-search) as a
new result. Import IDs are stable across retries. Preserve each Python turn ID within
its source deployment, with its repository, issue, generation, requested
`session_scope` and requested `session_turn` from SQLite. Keep the turn linked to
the archive it returned. Preserve each retained session by deployment, repository
and scope.

The archive's root `turn.json` records the last completed harness turn. Store that
ordinal separately from the requested one when the journal is complete, its scope
matches and its `turn` is a nonnegative integer (including zero). A pending or
absent journal leaves the completed ordinal unknown. A failed request for turn 1
can return the journal for completed turn 0. Preserve both values and accept this
history on import and replay (`engine/tests/test_sessions.py:167-171`). Report
inconsistent evidence for the same fact, such as a journal scope that disagrees
with the retained filename. A journal alone supplies no Python turn ID.
Retained-only and unmatched sources keep their paths for operator lookup and stay outside viewer
results unless a verified turn link exists.

Keep every ID in `runner_sessions` as a staffing association. Python seeds the set
from the staffing group, so a warm turn can inherit IDs without dispatching anyone
(`engine/agents.py:433-439`, `:479-484`). Record a dispatch against a specific turn
only if a separate record identifies both that turn and the dispatch. Take the
serving session and full run ID (including attempt) from the recorded `runner`
assignment. Preserve staffing even when the assignment or archive is absent
(`service/diagnostics.py:182-205`). Imported history creates no runnable tasks.

An import is complete when every source file and recorded turn, session and run
link is accounted for, each source line has an event row or a diagnosed fallback
row, and the original gzip objects verify against the inventory. Report fallback
positions separately. Missing or corrupt archives and conflicting links still need
repair. Keep the source files read-only for reruns.

### Credentials

The API for users' credentials, called by the backoffice on behalf of a signed-in user.
Which process serves it, the task application or a credentials service of its own, is
decided in phase 4. Sketch, phase 4.

| Call | Purpose |
| ---- | ---- |
| `GET /v1/users/{user_id}/credentials` | Provider lists, each with its revision and ordered entries; supported types and roles |
| `POST /v1/users/{user_id}/credentials` | Add an entry: type, name, enabled, role and profile filters, and a secret |
| `PATCH /v1/users/{user_id}/credentials/{entry_id}` | Name, enabled, filters |
| `POST /v1/users/{user_id}/credentials/{entry_id}/versions` | Replace the secret with a new version |
| `DELETE /v1/users/{user_id}/credentials/{entry_id}` | Remove the entry and revoke its versions |
| `PUT /v1/users/{user_id}/lists/{provider}` | Set the exact order |
| `GET /v1/relay/registrations/{code}` | A pending relay registration: name, routes, expiry |
| `POST /v1/users/{user_id}/relays` | Approve a registration code for new entries, or move existing entries to it |
| `GET /v1/users/{user_id}/relays` | Relay status: relays, idle and active slots, last seen |

- **It verifies the user itself.** The backoffice forwards the user's GitHub token, and
  the API resolves it and checks it matches `user_id`, as the vault does today. A
  compromised backoffice can't change a user's credentials without their token.
- Every mutation takes `Idempotency-Key`, removal included, and `If-Match` with the entry
  or list revision.
- Entries, lists and relays have separate paths, so no route depends on declaration order.
- Relay status is shaped once, by the API's owner, with RFC 3339 times.

### Exchange operator API

Sketch, phase 5. The task application submits turns with their grant.

| Call | Purpose |
| ---- | ---- |
| `PUT /v1/turns/{turn_id}` | Submit: `task_id`, `grant_id`, `target_id`, `limits`, `spec` blob, `session_pin?`. Idempotent on the turn ID |
| `GET /v1/turns/{turn_id}?wait=` | Status and, once finished, the result; the same data as `turn.*` |
| `DELETE /v1/turns/{turn_id}` | Cancel; a later submit with that ID is refused |
| `DELETE /v1/sessions/{session_id}` | End a session |

The exchange admits a turn only under an `issued` grant whose models, limits and
credential match it. When a grant is revoked it refuses new sessions under it, ends the
existing ones, and publishes `exchange.grant.released` with every session it bound to
the grant.

### Runner API

Sketch, phase 5, served by the Rust exchange to launchers on version 2.

| Call | Auth | Purpose |
| ---- | ---- | ---- |
| `POST /runner/v2/sessions/{id}/token` | OIDC | Exchanges the job's OIDC token once for a runner token, the inference base URL and session key, and the engine bundle to run |
| `GET /runner/v2/sessions/{id}/turns/next?wait=` | Runner token | The next turn spec, or `204` |
| `POST /runner/v2/sessions/{id}/blobs` | Runner token | Upload a transcript, workspace or session archive; content-addressed |
| `PUT /runner/v2/sessions/{id}/turns/{turn}/result` | Runner token | The result, referencing uploaded blobs; idempotent |
| `POST /runner/v2/sessions/{id}/heartbeat` | Runner token | Liveness |
| `DELETE /runner/v2/sessions/{id}` | Runner token | End the session |

Each call has one kind of credential, and large content moves as blobs, not as base64
inside results. Each target's launcher version is in [its settings](#configuration-1),
and a contract test runs every supported launcher version against the exchange; v1
retires when no target is on it.

### Runner protocol

Sketch, phase 5.

- **Turn spec:** `turn_id`, `role`, `harness`, `model`, `effort`, `limits {timeout,
  context_bytes, retained_bytes}`, `session {scope, turn, archive blob?}`, `workspace
  {repository_id, base, head, refs}`, and `inputs`: instructions, charter, policy,
  requirements and context as blobs.
- **Result:** `outcome` and `reason` as in [turn events](#turn-events), `response`,
  `workspace_changes` blob, `session_archive` blob, `transcript` blob, timing.

### Verifier owner API

Sketch, part of `concurro-relay`'s phase 5 release, behind the owner credential.

| Call | Purpose |
| ---- | ---- |
| `GET /owner/v1/registrations/{code}` | A pending registration: name, routes, expiry |
| `POST /owner/v1/approvals` | `{code, user {id, login}, bindings: [{route, entry_id, record_id, version}]}` → `{relay_id}`; idempotent for identical bindings |
| `GET /owner/v1/relays?user_id=` | The user's relays with idle and active slots |

Bearer in `Authorization`; one request type per call; RFC 3339 times; every error an HTTP
response with one error shape.

### Backoffice API

A separate Rust process (a backend for the frontend), from 3/M3, serving the frontend under
`/api/v2`. It's lower-privilege than the task application: no App key, no exchange or
vault token, no GitHub credential at all. It owns a read model fed only by the task
application's events, `config.targets` and `github.*`, and has no command path. Browser
sign-in (GitHub OAuth), browser commands and the credentials pages stay with the Python
backoffice until phase 4; the backoffice API asks it, with the viewer token the edge
passes on, who a viewer is and what they may see ([sessions](#python-backoffice-sessions)).
It never receives the session cookie. The Python
backoffice's task and list pages are off from the Cutover.

- **The read model** starts from the task application's
  [baseline](#target-index-and-baseline) and follows its events, as that section
  describes.
- **Lists** come from the read model only, paged and filtered:
  `GET /api/v2/tasks?target=&stage=&kind=&cursor=`, and the same under a target. Today's
  fixed server limits (three pages of 100 issues) go. Nothing merges Python lists.
- **Tasks** by ID: `GET /api/v2/tasks/{taskId}`. A page addressed by issue resolves it
  with `GET /api/v2/issues/{owner}/{repo}/{number}`.
- **Viewer tiers** are checked on every read, from the session's tier for the target and
  issue, keeping today's rule (`backoffice/tasks.py:187-193`,
  [backoffice API surface](backoffice-api.md)):
  - **`writer`** (upstream write access, and both the upstream and the fork visible to
    the user) sees the full task: turns, prompts, transcripts, workspace changes,
    reports, requests and the whole consent record.
  - **`reader`** (the issue visible) sees a summary: the issue and its title, stage and
    reason, mode, the PR link, when it was updated, and their own consent. A reader who
    can see the upstream but not the fork gets only the summary.
  - Anyone else sees nothing of the target.
- **Turn content** comes from the task application's [diagnostics read APIs](#blobs).
  After checking the viewer's `writer` tier, the backoffice reads transcript entries
  from the turn and fetches other artifacts by digest.
- **My sponsorships** come from [consent facts](#consent-facts) by the viewer's ID.
- **Commands** aren't the backoffice API's: through phase 3 the browser posts them to
  the Python backoffice, which checks CSRF and the viewer's permission, signs a
  [command proof](#command-proofs) and forwards the request to the task application.
  The read model shows each request's outcome from the [request events](#request-events),
  naming its actor. A refused request changes no task state. Nothing is "may have
  posted".
- **Live updates:** `GET /api/v2/events` streams `{type, entity, revision}` from the read
  model, and the client refetches what changed. The read model's revision replaces
  today's content hash as the cache key.
- **Credentials** stay on the Python backoffice's pages until phase 4, then
  `/api/v2/me/credentials` and `/api/v2/me/relays`, mapped onto the
  [credentials API](#credentials).
- **Errors** use the shared error shape.

## Bridges

### Python exchange

Phase 3 uses today's operator API (`exchange.py:1580`) with today's operator token. Until
the Cutover both the Python service and the task application call it, for disjoint
targets (the task application only for its test repository); from the Cutover the task
application is its only caller, and the token is rotated so only it holds it. Each change
below names its caller and the milestone that needs it; everything the task application
needs for today's workflows lands before the Cutover.

Needed by 3/M1, for the task application's exchange integration module:

- **Turn tombstones.** A replayed attempt resubmits its stored body after a crash; without
  a tombstone, a replay after the exchange prunes a finished turn runs it again, and a
  cancel before the submit doesn't fence it. Today the exchange deletes finished turns
  after a day (`exchange.py:1461`), cancelled ones included (cancellation writes a
  `turns` row, `exchange.py:1423`), and a resubmit after that is admitted again. The
  bridge keeps a tombstone instead:
  - Deleting a turn and writing its tombstone (turn ID, and body digest if it was
    submitted) happen in one transaction.
  - Every admission checks tombstones, including the second check after the metadata
    lookup (`exchange.py:1305`), so cleanup and a submit can't interleave into a
    readmission.
  - A resubmit of a tombstoned turn with the same digest gets `410 turn_expired`; with a
    different digest, `409` as today. A turn cancelled before it was ever submitted keeps
    a tombstone without a digest, and any submit with its ID is refused.
  - `turn_expired` is distinct from the `410` for an unavailable credential: only the
    latter shows a first submit never ran.
  - Tombstones are kept until the Python exchange retires in phase 5.
- **Cancellation ends sessions.** `stop` after a submit whose reply was lost must not
  leave a session the task application never heard of. `DELETE turns/{id}` also ends
  every session allocated to the turn and returns their IDs, which the task application
  records as ended. Turn IDs are attempt IDs the task application mints, and it's the
  only caller, so the turn ID alone identifies what to cancel.

Both are compatible with the Python service while it still runs: it never resubmits a
pruned turn, and it ignores the ended sessions in a cancel's reply.

At the Cutover:

- **Listeners.** The operator API moves to its own listener on the platform network, and
  the public listener serves the runner API only, so Caddy's `/exchange/*` route no longer
  reaches operator calls. Its only caller is then the task application, on the platform
  network. It waits for the Cutover because the Python service reaches it through the
  edge until then. agentgateway (the `gateway` container) and the exchange are separate
  containers, and the exchange shares the gateway container's network namespace
  (`gateway/compose.yaml:41-46`). So the gateway container is attached to the platform
  network, and the exchange's operator listener binds the address its platform alias
  resolves to, as the vault's resolver listener does on its private network
  (`gateway/compose.private.yaml`).

Needed by 3/M3, for personal credentials and standalone reviews, which are today's
deployed workflows:

- **Observations** take an optional `turn` and then answer
  `pending` for that turn alone, with the per-turn query that exists
  (`inference.py:452`). Without it, `pending` is today's issue-wide answer, unchanged: it
  leaves out requests of running turns. The task application's retry across credentials
  needs the finished turn's own `provider_requests`.
- **Grants:** a contributed turn needs the slot row the task application's slot adapter
  posted; the exchange never takes it from the submitted `admission.grant`, so a submit
  can't run on consent the exchange never recorded.
- **Provenance:** `review_request` joins `task_start` and `label`, so a contributed
  standalone review isn't refused; `assignment` and `mention` join when those channels
  are built.
- **Admission:** `custody` is allowed as a thirteenth field, as the engine expects, so
  relay-custody credentials work.

Needed later:

- **Interpreter** (mentions, only if built before phase 5): an `interpreter:<provider>`
  role, and interpretation grants posted without a task slot, since they don't depend on
  task consent.

**Slots, not grants.** Part of 3/M3. Today's exchange holds one
sponsorship slot per task and provider, whose `grant_id` and `epoch` change only with
consent, and it accepts each change only as the next step from the slot it holds
(`exchange.py:773`). So the slot adapter needs every consent change, in order, which
neither grants nor a consent snapshot give it. The funding policy records each change
to a provider slot in the transaction that makes it:

| Field | Meaning |
| ---- | ---- |
| `task_id`, `issue {repository, number}`, `provider` | The slot; the exchange keys it by issue (`service/shadow.py:1103-1106`) |
| `consent_revision` | The task's consent revision this change made; orders the slot's changes |
| `previous_revision` | The consent revision of this slot's previous change; null for the first |
| `reason` | `initialize`, `sponsor`, `funding`, `withdraw` or `permission_loss` |
| `slot` | `{epoch, status, sponsor, provenance, source_funding_mode, funding}` after the change |
| `actor`, `cutoff` | For withdrawals and permission loss: whose consent ends, through which epoch |

- **Slots start at the funding boundary.** The first change is `initialize`, at epoch 1
  with the boundary's payer or epoch 0 without one; sponsorship before the boundary is
  folded into it, as today. Nothing is recorded for a slot before its boundary freezes.
  On an issue Python used, whose slot the exchange still holds at an exported epoch `e`,
  `initialize` is at epoch `e + 1` with `expected_epoch` `e`, as the exchange requires
  the replacement slot's epoch to follow the one it holds (`exchange.py:783-788`); its payer
  and consent come from the new boundary only.
- **The slot adapter, a job, applies a slot's changes strictly in order.** It holds a
  change until the one named by `previous_revision` has been acknowledged by the
  exchange. It sends each as today's transition (`exchange.py:692`): `expected_epoch`
  from its own record of the last acknowledged epoch (−1 before `initialize`, or the
  epoch exported at the Cutover for an issue Python used), never from the exchange's
  current state; the task's `generation`; and today's `transition` digest. It records the
  body before sending and the reply after.
- **A lost reply is retried with the same body;** the exchange returns its stored reply
  for that digest. The adapter never sends a slot's next change before the previous one
  is acknowledged.
- **A slot's Python `grant_id` is the SHA-256 hex of `(task_id, provider, slot_epoch)`.**
  Every grant under that slot epoch maps to it, so attempts, concurrent ones and
  credential replacements included, share one slot row.
- **Attempts wait for their slot.** A contributed attempt is submitted only once the change
  that set its slot epoch has been acknowledged. Org attempts need no slot.

**The exchange module** turns an attempt and its grant into today's submit request
(`exchange.py:1274`):

- `turn` is the attempt ID, `generation` the task's (below), `budget` the grant's token
  limit and `lease` its lease.
- **Generations.** The exchange keeps a generation per issue (`funding_generations`,
  `exchange.py:503-512`) and a slot epoch per issue and provider (`:764-772`), and refuses
  a lower generation or an unexpected epoch with `409`. A task on an issue the exchange
  has never seen has generation 1. For issues the Python service used, the operator
  exports both at the Cutover; a new task on such an issue takes the next generation, and
  its first slot transition expects the exported epoch `e` and sets epoch `e + 1`. Old
  consent isn't carried over: the new task's slots start from its own boundary. Tested
  with the actual personal-slot transition on a previously used issue, not only an
  org-funded intake, which sends no slot transition: mandatory in the rehearsal, against
  a test exchange holding a historical slot; at the live Cutover only when the export
  holds a slot, and otherwise by confirming the export is empty and a first slot
  initializes normally.
- For `task_org`: `funding` is `org` and every personal field is null, as the exchange
  requires (`exchange.py:306`).
- For `task_contributed` (from 3/M3): the binding's `funding`
  (`contributed`), `sponsorship_provider`, `sponsor` (as a decimal string), `epoch` (the
  slot epoch), `grant_id` (the slot's, as above), `route`, `credential` (the record ID)
  and `credential_version` come from the grant. `admission` is today's twelve fields plus
  `custody`: `credential.id` is today's digest of sponsor, route, record and version, and
  `grant` is the slot as the slot adapter posted it.
- It stores the exact body before the first call: the exchange keys a retry on the turn
  ID and the body's digest, so a rebuilt body would be refused.
- **Resubmitting is only for a lost first reply.** Once the exchange's admission reply is
  recorded, together with any `dispatch` it returned so a crash can't strand the runner
  dispatch, the module never resubmits that turn; it only polls. A poll that finds the
  turn gone (`404`), or a resubmit answered `410 turn_expired`, makes the attempt
  `unknown`, unless its terminal result is already recorded, which stands.
- It polls each turn and records [turn events](#turn-events), published from 3/M1. Today's
  `done` and `lost` statuses are `finished`. Of today's outcomes, `ok` is `succeeded`;
  `failed`, `terminated` and `queue_expired` are `failed`; `uncertain`, `lost`,
  `timed_out` and `admission_unknown` are `unknown`. Each is kept as the `reason`, and an
  invalid response as `invalid_output`.
- It takes `provider_requests` and `provider_failure` from the exchange's records for
  the turn, once the turn has finished. `none` needs both: the turn is contributed (the
  exchange records only contributed requests; org requests bypass it,
  `inference.py:90`), and that turn has no recorded request and none still awaiting its
  observation (`pending(turn=...)`, `inference.py:452`). Any recorded request is `some`.
  Everything else, every org turn included, is `unknown`.
- It records a [dispatch](#runner-dispatch) for a `dispatch` in a submit reply or a poll
  (a queued turn whose session was lost is dispatched again, `exchange.py:1416`), with
  today's launcher inputs (`service/gateway.py:284`), and a session ended when it ends,
  cancels or sees a session replaced. From 3/M3 it reads
  `GET capabilities` and holds contributed turns while their route isn't offered.

**Withdrawal steps before phase 5.** Part of 3/M3. Once the exchange
has acknowledged a withdraw transition, it refuses new sessions and relay requests under
that slot (`allowed()`, `exchange.py:515`), and every existing session ends by its
deadline: its creation plus its lease, which the verifier enforces on relay work by
itself. So the task application records "spending stopped" when the acknowledgement time
plus the longest `lease_seconds` among the slot's grants has passed, or earlier once
every turn under them has finished and none used relay custody. The withdrawal job
records both values when the acknowledgement arrives; an acknowledgement that only
arrives after a lost reply delays the report, never advances it. A relay release that
fails (`inference.py:350` ignores errors) can't make it report early.

### Turn payload v1

Today's runner takes `{request, feedback, spec, retained, timeout}`
(`engine/agents.py:130`) and returns `{outcome, response, workspace, retained,
transcript, termination_reason, elapsed_seconds}` (`engine/turns.py:400`). The exchange
module reads the session revision checked and pinned at admission from its pending
upload or verified Hetzner object and base64-encodes it for the v1 runner (`engine/turns.py:217-225`).
Persist the prepared submit body for retries. Returned archives go through
[transcript ingestion](#transcript-search).

#### Retained session eligibility

Check the retained journal against the requested v1 turn as the runner does
(`engine/session_entry.py:32-52`, `:63-73`).

- A next turn needs a complete journal for the same scope and the immediately
  preceding ordinal. Its instructions and response schema must still match.
- Exact replay of the journal's own ordinal requires the same scope, a complete
  journal, a non-null cached response and the same request digest. A null response
  cannot take the v1 cached-return path. Compute the digest from the full v1 prompt with
  its response schema using `engine/policy.py:107`. The runner returns the cached
  response on this path. Replaying an admitted attempt reuses its existing charge.
- A fresh turn zero can start without a journal. A continuation cannot use a
  missing, malformed or pending journal, an unreadable archive, a different scope,
  a skipped ordinal or a same-ordinal request with a different digest.
- For both continuation and exact replay, check the total size of restored regular
  files against the requested turn's `max_retained_bytes` before admission. Record
  this uncompressed size at ingestion. Equality passes and a null limit is unlimited.

### Vault

Part of 3/M3. The funding policy's vault adapter reads the vault's
service API until phase 5.

- **Its route.** The vault's metadata listener is reachable only on the `edge` network
  today, with no Caddy route; its optional private listener serves credential
  resolution only (`gateway/compose.private.yaml`). From 3/M3 the vault joins the
  platform network and its metadata listener binds the address its platform alias
  resolves to, as the resolver listener does, for the task application. Its other
  callers keep the edge address until they move.

- `GET /metadata/{user_id}?at=` gives a user's credentials and lists as of a time, with
  `history_complete`. Fills use `at` = the boundary; `history_complete` false makes the
  fill `unknown`.
- Only an entry's latest version is listed, and the exchange checks current metadata only
  for a turn it hasn't admitted (`exchange.py:1305`):
  - Replaying an admitted turn returns its stored admission, whatever has changed since.
  - A first submit whose version has since been replaced gets `410`. That attempt never
    ran, so the funding policy treats the version as unusable for new attempts on the
    task, like an `auth` failure.
  - Once the exchange has dropped an admitted turn's record (finished turns are kept for
    a day), a resubmit gets `410 turn_expired` from its tombstone, whether or not the
    credential is still valid. That doesn't show the attempt never ran: the attempt is
    recorded as `unknown`, with no automatic fallback.
- `GET /metadata/events?after=` pages credential events. The adapter stores its cursor
  with the events it applies, in one transaction, and applies them to its credential
  records.
- User IDs arrive as strings in metadata and integers in events; the adapter makes them
  integers.

### Python backoffice sessions

From 3/M3 until phase 4, browser sign-in stays with the Python backoffice. It answers the
backoffice API's questions about sessions, and it alone takes browser commands.

- **Its own listener on the platform network.** Today's server listens on every
  interface (`backoffice/__main__.py:19`), so the internal API is never a route on it.
- **Two credentials per sign-in.** The session cookie, which the command handler
  requires with its CSRF header (`backoffice/app.py:226-237`, `:502`), and a distinct
  viewer token, read-only, which answers identity and tier lookups and authorizes no
  command anywhere. The edge strips the session cookie from requests to `/api/v2/*`, so
  the backoffice API sees only the viewer token.
- **For a viewer token it answers the backoffice API** the user's ID and login, or
  `401`; and the viewer's tier for given targets and issues, and when it checked:
  `writer` needs upstream write access and both the upstream and the fork visible,
  `reader` the issue visible, else none. Each is evaluated with the session's GitHub
  token, as today's checks are (`backoffice/tasks.py:187-193`), and the token never
  reaches the backoffice API.
- **The backoffice API holds nothing that can command.** Its service credential to the
  Python backoffice is read-scoped, and the viewer token it's given authorizes no
  command: the Python backoffice's command handler requires the session cookie, which
  never reaches the backoffice API, and the requests API requires a proof only the
  command handler signs. It uses the answers to scope every read. Tested: the
  credentials actually delivered to the backoffice API authorize no command through the
  Python backoffice's command handler or the task application's requests API.
- **Browser commands** go to the Python backoffice, as today
  (`backoffice/app.py:502-510`), with its existing CSRF check and per-kind permission
  check. It signs each one and forwards it to the task application's
  [requests API](#requests) itself.
- Retires in phase 4, with the rest of the Python backoffice: sign-in, browser
  commands, and the credential writes and relay approvals that also use the viewer's
  token (`backoffice/credentials.py:96`), all move then. Moving sign-in alone wouldn't
  retire it.

#### Command proofs

Only the session authority can establish that a viewer issued a command: the Python
backoffice, which receives the browser's command POST, signs a short-lived proof for it,
and the task application verifies it:

- **Issued** for a command the browser posted, after the CSRF check and today's per-kind
  check with the session's GitHub token: write access for most kinds, issue visibility
  for `withdraw` (`service/actions.py:149-156`). Nothing else can ask for one.
- **Bound** to the user's numeric ID and login, the repository, issue, command kind and
  arguments, `detail.feedback`, `source_at`, the browser action's ID (the request's
  `Idempotency-Key`), and an expiry of a few minutes. Signed with a key only the Python
  backoffice holds; the task application has the public key.
- **Verified** by the task application: the signature, the expiry, and every binding
  against the request. The actor comes from the proof, and the same proof with any other
  request is `403 proof_mismatch`. A repeat of a recorded action is matched on the actor
  and the command's contents, not the proof, so a retry after the proof expired, signed
  again with the same contents, returns the recorded response ([requests](#requests)).
  Writer permission is still checked again when the request is applied.
- **Tested:** a request whose actor differs from the proof's, a proof reused for a
  different command (a `withdraw` proof for a `stop`, a `stop` proof for a `withdraw`),
  and a changed `detail.feedback` or `source_at` are refused and change nothing; the
  backoffice API, holding only a viewer token, can get no proof and submit no command.

## Decisions

1. **The runner.** M1 uses today's runner unchanged, on [payload v1](#turn-payload-v1).
   Its protocol moves to the target protocol and runner API v2 in phase 5, with the
   exchange. Porting its code to Rust is a separate, later step.
2. **Edited commands** follow the kind of the new revision:
   - A revision still pending is superseded by the next one, and stays recorded as
     `superseded`.
   - A sponsorship command (`sponsor`, `withdraw`, `funding`) by its author applies per
     revision, whatever an earlier revision said: an applied `start` edited by its author
     into `withdraw openai` withdraws. Consent an earlier revision gave stands until
     withdrawn, so `sponsor openai` edited into `sponsor anthropic` adds Anthropic while
     OpenAI stays sponsored. This is a behavior change: today's service ignores every
     later revision of a source it has adopted (`service/shadow.py:1197-1199`), and the
     edit would change nothing.
   - A standalone review command applies per revision, as today.
   - Any other command isn't applied if an earlier revision of the comment was; its
     request is refused as `already_applied`.
3. **Launchers** are versioned per target in [the settings](#configuration-1), every
   supported version is contract-tested, and v1 retires when no target is on it.

## Open items

1. **Timeline check.** The read-only part is done
   ([enrollment channels](#enrollment-channels)). Before 3/M3's label channel is built,
   with controlled changes on a test repository: measure the delay between a webhook and
   its event being listed; confirm each action updates the issue's `updated_at`; and
   confirm assignment events and a removed and re-added `concurro` label.
2. **Bridge tests.** A test drives today's exchange with the
   [Python exchange](#python-exchange) mappings. With 3/M1's exchange integration: org
   submits, the stored body, a resubmit after a lost first reply, the session-ending
   cancel, and turn tombstones (an unchanged resubmit after pruning, a changed-body
   resubmit, a turn cancelled before submission then pruned, and tombstones surviving a
   restart). With 3/M3's personal credentials: slot grants shared by attempts, ordered
   changes from the adapter's own record over HTTP (out of order, a lost reply, renewed
   consent, the cutoff), a replacement credential, both of today's mismatches, the
   `provider_requests` and withdrawal-expiry mappings, and concurrent adapter instances.
   The pre-boundary fold reaches the slot adapter already folded; folding itself belongs
   in the funding policy's tests. Interpretation admission comes with mentions.
