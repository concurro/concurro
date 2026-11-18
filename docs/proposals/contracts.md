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
  through the behavior that phase 3's parity test preserves (stages, authorization,
  recovery), never through its wire formats.
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

## What the code inventory changed

Today's interfaces were inventoried from the code to find the bridges, the behavior to
preserve, and the guarantees today's design gives implicitly:

1. **Python never publishes onto the bus.** In every phase the Python service and list
   index only consume `github.*`; everything else Python offers is HTTP (the exchange,
   the vault, the list API, delivery recovery).
2. **Webhooks only wake tasks today.** Ingress uses payloads to choose which tasks to
   wake (`service/ingress.py:224`), and the engine re-reads issues, comments, label
   events, PRs, checks and files from GitHub on every tick; the reconciler re-reads them
   after a gap (`service/reconciler.py:120`). The target needs an explicit rule for what
   events carry and what consumers read ([below](#events-and-reads)), and an explicit
   recovery for what the re-reads quietly guarantee ([coverage gaps](#coverage-gaps)).
3. **The runner protocol is a contract of its own.** The turn payload and result pass
   through the exchange opaquely (`exchange.py:1293`) between the engine that builds the
   prompt and the runner code in the engine bundle (`engine/turns.py:400`). Phase 3's
   Rust orchestrator has to speak it to today's runner, which makes it a bridge.
4. **Live updates are part of the list contract.** The backoffice's server-sent events
   long-poll the list index's `POST /lists/changes` (`backoffice/live.py:127`), and its
   lists join sponsorship projections from `POST /sponsorships`
   (`backoffice/tasks.py:427`).
5. **Command edits are handled two ways.** Contribution commands apply once per comment
   (`id > command_id`, `engine/controller.py:1698`); standalone review commands apply
   once per revision (`command_receipts`). Requests key on the revision, so the comment
   channel needs one rule ([decisions](#decisions)).
6. **Python's grant is a consent slot, not an attempt.** A sponsorship slot's `grant_id`
   and `epoch` change only when consent changes; the exchange refuses a second grant ID at
   the same epoch (`exchange.py:778`). Per-attempt grants need a mapping
   ([Python exchange](#python-exchange)).
7. **Two mismatches in today's code,** which the exchange bridge fixes:
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
| Task ID | UUIDv7 | Orchestrator, on an issue's first routed input | One task per issue key for its lifetime; the `<task>` in subjects |
| Request ID | UUIDv7 | Orchestrator | |
| Source | `{channel, id, index}` | Channel | Comment: `{comment_id}:{revision digest}`; label, assignment, review request: the issue timeline event's ID ([enrollment channels](#enrollment-channels)); backoffice: its idempotency key |
| Attempt ID | UUIDv7 | Orchestrator, before the grant | One admitted model call; also its turn's ID |
| Consent revision | Integer per task | Orchestrator | Increases with every consent change |
| Slot epoch | Integer per task and provider | Orchestrator | Increases when the slot's payer changes |
| Grant ID | UUIDv7 | Funding | One per attempt |
| Session ID | UUIDv7 | Exchange | |
| Credential IDs | Entry, record, version | Funding | Opaque to consumers; existing IDs carry over unchanged |
| Relay ID | `relay-<32 hex>` | Verifier | |
| Event ID | UUIDv7, or UUIDv5 | Producer | UUIDv5 where the ID must be reproducible: a GitHub event's, of its delivery ID and mapping revision, so a re-mapping is a new event and a redelivery isn't; and where two producers can publish the same event (turn events across the phase 5 handover), of `(entity, type)` for a fact and `(entity, type, revision)` for a snapshot |
| Delivery ID | GitHub GUID | GitHub | Ingress's deduplication key |

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
| `entity` | string | The ID a snapshot's revision belongs to (task, notice, turn, grant) |
| `revision` | integer | Snapshots only; strictly increasing per `entity` and `type` |
| `causation` | string | The request or event that caused it |
| `data` | object | Payload |

The NATS subject is the architecture's (`task.<task>.<event>` and so on). The message ID is
`{id}:{version}`, so a breaking change can publish both versions of one event.

### APIs

- **HTTP with JSON bodies** on each service's internal listener, under a version prefix
  (`/v1/...`).
- **One bearer per caller** in `Authorization`, scoped to the operations that caller
  needs. No shared tokens.
- **Every mutation is idempotent.** It carries a natural key (attempt ID, revocation job
  ID) or an `Idempotency-Key` header. The same key with the same body returns the
  recorded response, status included; the same key with a different body is
  `409 idempotency_conflict`. Recorded responses live as long as their entity can be
  retried. A caller that doesn't know the outcome retries with the same key, never a new
  one.
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
- **CI enforces it:** generated schemas are committed, and a change that isn't additive
  within a version fails the build.

### Events and reads

- **Events carry acts and what a consumer needs to route them:** who did what, when, and
  the fields that decide whether it's a request (a comment's body and author, a label's
  name, a review request's reviewer).
- **Decisions that need complete or current state read it when they're made,** with the
  read tokens the publisher issues: writer permission (the collaborators API, never
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

Phases are the [migration phases](architecture.md#migration); 3/M1 is phase 3's first
milestone. Where a bridge stands in for a target contract until it's built, the
[bridge catalog](#bridge-catalog) says so.

### Configuration

| Contract | Kind | Producer → consumers | Phase | Level |
| ---- | ---- | ---- | ---- | ---- |
| `config.targets` | Snapshot | Orchestrator → publisher, exchange, backoffice read model | 3/M1 | Full ([below](#configuration-1)) |
| `funding.routes` | Snapshot | Funding → gateway, backoffice read model | 5 | Sketch: the route catalog, each route's provider, kind, custody and endpoint |

### GitHub ingress

All facts on `github.<event>.<action>`, identified by delivery ID and mapping revision
([fields](#github-events)).

| Contract | Consumers | Phase | Level |
| ---- | ---- | ---- | ---- |
| `github.issue_comment.{created,edited,deleted}` | Comment and mention channels | 2 | Full |
| `github.issues.*`: `opened`, `edited`, `closed`, `reopened`, `labeled`, `unlabeled`, `assigned`, `unassigned`, `transferred`, `deleted` with their own fields, and any other action with the issue | Label and assignment channels, orchestrator, Python list index | 2 | Full |
| `github.pull_request.*`, including `review_requested` and `review_request_removed` | Review-request channel, orchestrator | 2 | Full |
| `github.pull_request_review.*`, `github.pull_request_review_comment.*`, `github.pull_request_review_thread.*` | Orchestrator, mention channel | 2 | Full |
| `github.push`, `github.status`, `github.check_run.*`, `github.check_suite.*`, `github.workflow_run.*` | Orchestrator (when to re-read gates), read model (runs) | 2 | Full |
| `github.installation.*`, `github.installation_repositories.*` | Orchestrator (targets) | 3 | Sketch |
| `ingress.coverage.gap` | Python service (2–4), orchestrator and backoffice read model (3) | 2 | Full ([below](#coverage-gaps)) |
| `ingress.coverage.advanced` | Python list index (2–4), backoffice read model (3): everything up to `covered_until` is on the stream before it | 2 | Full ([phase 2 plan](architecture/phase-2-plan.md#coverage)) |
| `POST /v1/replays` | API: a consumer that lost events from the stream → ingress | 2 | Full ([phase 2 plan](architecture/phase-2-plan.md#replays)) |

### Orchestrator

| Contract | Kind | Producer → consumers | Phase | Level |
| ---- | ---- | ---- | ---- | ---- |
| `POST /v1/requests`, `GET /v1/requests/{id}` | API | Backoffice API (later Slack) → orchestrator | 3/M1 | Full |
| `request.orchestrator.{applied,refused,superseded}` | Fact | → publisher (replies), read model | 3/M1 | Full |
| `request.source.{received,final}` | Fact | → publisher (reaction, one reply per source) | 3/M1 | Full |
| `task.<task>.state` | Snapshot | → publisher (dashboard, stage labels), read model | 3/M1 | Full (M1 fields) |
| `task.<task>.consent` | Snapshot | → publisher (dashboard), read model | 3/M1 | Full |
| `task.<task>.notice` | Snapshot per notice | → publisher, read model | 3/M1 | Full (M1 kinds) |
| `task.<task>.finding` | Snapshot per finding | → publisher (review threads), read model | 3/M3 | Sketch: seat, origin profile, status, location, body, disposition |
| `task.<task>.review` | Fact | → publisher | 3/M3 | Sketch: round, commit, body, finding IDs; reviewer account or App |
| `task.<task>.report` | Snapshot per report | → publisher, read model | 3/M3 | Sketch: kind, step, blob ref, digest; where it's published is an [open question](architecture.md#open-questions) |
| `task.<task>.branch` | Snapshot | → publisher | 3/M3 | Sketch: base, parent commit, tree edits blob, message. The publisher records the exact commit it will write (fixed author and committer times, so recreating it gives the same SHA) before writing, and settles an uncertain ref update only if the ref points at that SHA |
| `task.<task>.pull_request` | Snapshot | → publisher | 3/M3 | Sketch: title, body, head, base, draft or ready |
| `task.<task>.check` | Snapshot | → publisher | 3/M3 | Sketch: standalone review check, status, conclusion `neutral` |

### Funding

| Contract | Kind | Producer → consumers | Phase | Level |
| ---- | ---- | ---- | ---- | ---- |
| `POST /v1/fills` | API | Orchestrator → funding | 3/M1 | Full |
| `POST /v1/grants`, `GET /v1/grants/{attempt_id}` | API | Orchestrator → funding | 3/M1 | Full |
| `funding.grant` | Snapshot per grant | → orchestrator, exchange, gateway, read model | 3/M1 | Full |
| `POST /v1/withdrawals`, `GET /v1/withdrawals/{job_id}` | API | Orchestrator → funding | 3/M2 | Sketch: job ID, request ID, task, provider, sponsor, consent revision, slot epoch → recorded status |
| `request.funding.{blocked,stopped}` | Fact | → publisher, read model | 3/M2 | Sketch ([withdrawal steps](#withdrawal-steps)) |
| `funding.credential` | Snapshot per version | → orchestrator (`awaiting_credentials`), gateway, read model | 3/M2 | Sketch: version status, reason, expiry, usable-again; never material |
| `funding.charge` | Fact | → read model | 5 | Sketch: grant, usage, cost basis |
| Credentials and relay API | API | Backoffice API → funding | 4 | Sketch ([below](#credentials)) |

### Publisher

| Contract | Kind | Producer → consumers | Phase | Level |
| ---- | ---- | ---- | ---- | ---- |
| `POST /v1/tokens/read` | API | Orchestrator, backoffice API (gap repair) → publisher | 3/M1 | Full |
| Write admission | Rule | Publisher, for every surface | 3/M1 | Full for M1's surfaces ([below](#write-admission)) |
| `publication.outcome` | Fact | → orchestrator, read model | 3/M1 | Full |
| Delivery recovery API | API | Ingress → publisher | 2 | Full ([below](#delivery-recovery)) |

### Exchange and runner

| Contract | Kind | Producer → consumers | Phase | Level |
| ---- | ---- | ---- | ---- | ---- |
| `turn.<turn>.{submitted,finished}`, `turn.<turn>.status` | Fact, snapshot | → read model, funding | 3/M1 | Full |
| `runner.dispatch`, `runner.session.ended` | Fact | → publisher | 3/M1 | Full |
| Operator API | API | Orchestrator → exchange | 5 | Sketch ([below](#exchange-operator-api)) |
| Runner API | API | Launcher workflow → exchange | 5 | Sketch ([below](#runner-api)) |
| Runner protocol (turn spec and result) | Runner protocol | Orchestrator ↔ runner, through the exchange | 5 | Sketch ([below](#runner-protocol)) |
| `session.<session>.binding` | Snapshot | → gateway, funding | 5 | Sketch: key hash, grant, credential version, models, token budget, expiry, status; a relay session is `revoked` only after the verifier confirms its release |
| `exchange.grant.released` | Fact | → funding | 5 | Sketch: grant ID and every session the exchange ever bound to it, published once it refuses new ones |
| Session claim | Signed token | Exchange → verifier, carried by the gateway | 5 | Sketch: claim ID, relay, record and version, route, models, session, expiry, call and token allowance, key ID; Ed25519 |

### Inference gateway

| Contract | Kind | Producer → consumers | Phase | Level |
| ---- | ---- | ---- | ---- | ---- |
| Inference endpoint | API | Runner harnesses → gateway | 5 | Sketch: native provider passthrough authenticated by the session key; route chosen by the binding, not by client headers |
| `inference.usage` | Fact | → funding, read model | 5 | Sketch: binding, grant, turn, model, input and output tokens |
| `inference.failure` | Fact | → funding, orchestrator | 5 | Sketch: category ([failure categories](#retries-and-exclusions)), model, retry after, version |
| `inference.binding.closed` | Fact | → funding (spending stopped), exchange | 5 | Sketch: binding, reason, closed at |

### Relay verifier

Released by `concurro-relay` for phase 5.

| Contract | Kind | Producer → consumers | Phase | Level |
| ---- | ---- | ---- | ---- | ---- |
| Owner API | API | Funding → verifier | 5 | Sketch ([below](#verifier-owner-api)) |
| Dispatch with a session claim | API | Gateway → verifier | 5 | Sketch: the claim replaces the session, turn and deadline the verifier trusts today |
| Release | API | Exchange → verifier | 5 | Sketch: revoke a claim or cancel one turn; persisted until the claim expires; acknowledged |

### Storage

| Contract | Kind | Producer → consumers | Phase | Level |
| ---- | ---- | ---- | ---- | ---- |
| Blob store | Object storage | Orchestrator, exchange (5) write; backoffice API, orchestrator read | 3/M1 | Full ([below](#blobs)) |
| KV path per credential version | OpenBao KV v2 | Funding writes and destroys; gateway reads; migrator writes once | 5 | Sketch: `{secret, account}`, check-and-set 0, custom metadata `{entry, record, route}` |

### Backoffice

| Contract | Kind | Producer → consumers | Phase | Level |
| ---- | ---- | ---- | ---- | ---- |
| Backoffice API | API | Frontend → backoffice API | 3 | Sketch ([below](#backoffice-api)) |

### Operations

| Contract | Kind | Caller → service | Phase | Level |
| ---- | ---- | ---- | ---- | ---- |
| Parked events | API | Operator → every consumer | 1 | Sketch: list, inspect, replay or discard a parked event, by consumer and event ID |
| Reconciliation | API | Operator → orchestrator | 3 | Sketch: reconcile a task or a repository over a period, as for a [coverage gap](#coverage-gaps) |
| Ownership seeding and pilot list | API | Operator → orchestrator | 3/M1 | Full ([ownership](#ownership), [configuration](#configuration-1)) |
| Migrations and cutover | Runbook | Operator | 4, 5 | Sketch: the phase 4 retirement and phase 5 handover and secret migration, each with its checks |

## Bridge catalog

| Bridge | Between | Stands in for | Adapter | Retires |
| ---- | ---- | ---- | ---- | ---- |
| `github.*` and `ingress.*` consumers | Ingress → Python service, list index | Nothing: Python consumes target events through generated models | None | 4 |
| [Delivery recovery](#delivery-recovery), served by Python | Ingress → Python service | The publisher's delivery recovery API | None: Python implements the target contract | 3 |
| [Ownership](#ownership) | Channels, Python service, both backoffices, Python exchange → orchestrator | Nothing: after phase 4 every task is the orchestrator's | Orchestrator | 4; the exchange's lookup with the exchange in 5 |
| [Python exchange](#python-exchange) operator API, capabilities and observations | Orchestrator, funding → Python exchange | Exchange operator API, `funding.grant`, `inference.*` | Orchestrator's exchange adapter; funding's slot adapter | 5 |
| `task.<task>.slot.changed` | Orchestrator → funding | Nothing: target grants carry their decision | Funding's slot adapter | 5 |
| Turn polling | Orchestrator → Python exchange | `turn.*` and `runner.*` published by the exchange | Orchestrator's exchange adapter publishes them | 5 |
| Runner API v1 (`engine`, `key`, `next`, `turns/{turn}`, `end`) | Launcher workflow → exchange | Runner API | Python exchange serves it; the Rust exchange serves it until no target's launcher is on v1 | When the last launcher moves |
| [Turn payload v1](#turn-payload-v1) | Orchestrator → today's runner | Runner protocol | Orchestrator's payload adapter | Phase 5, with the runner ([decisions](#decisions)) |
| [Vault metadata and events](#vault) | Funding → vault | Funding's own credential store | Funding's vault adapter | 5 |
| Vault owner API (`gateway/vault/vault.py:1534`) | Python backoffice, then funding → vault | Funding's credentials API | Funding's vault adapter, from phase 4 | 5 |
| Verifier owner and dispatch API, single bearer, `x-concurro-binding` | Vault, exchange forwarder → verifier | Verifier owner API, dispatch with a session claim, release | None | 5 |
| [Python lists and sponsorships](#python-lists) | Backoffice API → Python service | Backoffice read model | Backoffice API's list adapter | 4 |
| Python backoffice `/api/*`, `/auth/*` | Frontend → Python backoffice | Backoffice API | Frontend routes by owner | 4 |

## Target contracts

### Configuration

Service settings stay files each service reads at start. What other services need from
the orchestrator's settings is published as `config.targets`, a snapshot with one revision
per change:

| Field | Meaning |
| ---- | ---- |
| `targets` | Per target: `target_id`, `upstream` and `fork` (`{repository_id, full_name}`), `issues` (`upstream`, `fork` or `both`), `launcher {workflow, ref, version}` |
| `reviewer` | The reviewer account `{id, login}`, or absent |
| `pilot` | `{repositories, issues}` while the pilot runs; absent after the switch |

- The publisher admits writes and issues read tokens only for these repositories.
- The exchange takes a run's expected OIDC claims from the target's launcher.
- A target's own configuration (`.concurro/config.*` at its `config_sha`) stays the
  user's file. The orchestrator reads it when it decides, as it reads any repository file;
  no other service does.
- Budgets (`max_model_calls` and the other limits) come from that file and belong to the
  orchestrator. The token budget and lease per attempt come from the orchestrator's
  settings and travel in the grant request ([grants](#grants)).

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

Served by whoever holds the App's private key: the publisher, and the Python service
until the publisher exists. Ingress redelivers a delivery that has no receipt and no
successful or pending attempt ([phase 2 plan](architecture/phase-2-plan.md#recovery)).

| Call | Request | Response |
| ---- | ---- | ---- |
| `GET /v1/deliveries?cursor=` | | `{deliveries: [{id, guid, event, action, delivered_at, status, status_code, redelivery}], next_cursor}`, from GitHub's `app/hook/deliveries`; `status` tells a pending attempt from a failed one |
| `POST /v1/deliveries/{id}/redeliver` | | `202 {}`. Repeating it is harmless: a redelivery keeps its GUID, which ingress deduplicates |

### Coverage gaps

GitHub keeps deliveries for three days. When ingress can't recover a period, and once at
its first start, it publishes `ingress.coverage.gap {from, to, installation_id}`, a fact
([phase 2 plan](architecture/phase-2-plan.md#coverage)). Reconciliation doesn't depend
on knowing which issues are enrolled. For every target repository, the orchestrator lists
what changed since `from`:

| Read | Recovers |
| ---- | ---- |
| `issues/comments?since=` | Commands, sponsorship, mentions and discussion on issues and PR conversations, enrolled or not |
| `pulls/comments?since=` | Mentions and commands in review comments |
| `issues?state=all&since=` and each listed issue's `issues/{n}/events` | Labels, assignments and review requests, through the [enrollment channels](#enrollment-channels). This relies on labeling, assigning and requesting a review updating the issue's `updated_at`, so `since` lists every issue that has one ([checked before M1](#enrollment-channels)). The repository-wide `issues/events` listing isn't used: GitHub stops it after 300 pages of 100 (HTTP 422), with no `since` |
| `pulls?state=all&sort=updated`, back to `from`, and each PR's reviews | Review bodies with mentions; PR closes, merges and head changes |

Each comment becomes a request with its own source, so whatever arrived anyway is
deduplicated. Then every task the orchestrator owns re-reads its decision inputs (PR
head and merge state, reviews, CI gates), as the reconciler does today, and republishes
its snapshots.

The backoffice read model marks the period's GitHub-derived rows (workflow runs, PR
rows) stale for the affected repositories and refreshes them with a read token. That's
its only GitHub read.

Not recoverable: a comment created and deleted within the gap; intermediate revisions of
a comment edited more than once within it (the latest revision is seen, and an edit whose
author can't be established stays [unresolved](request-intake.md#authorization));
reactions.

### Enrollment channels

Label, assignment and review-request requests are created from the issue's events
timeline, not from deliveries:

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
  All are checked before M1; the read-again-with-backoff rule doesn't depend on the delay
  being short.

### Requests

`POST /v1/requests`, with the backoffice's request ID as `Idempotency-Key`:

| Field | Type | Meaning |
| ---- | ---- | ---- |
| `issue` | `{repository_id, number}` | The task's issue, or the issue to enroll |
| `kind` | enum | `enroll`, `unenroll`, `start`, `approve`, `stop`, `retry`, `resume`, `extend`, `invalidate`, `sponsor`, `withdraw`, `funding`, `conversation` |
| `argument` | object | Per kind: `approve {plan_id}`, `extend {plan_revisions}`, `resume {description_only}`, `sponsor {provider?}`, `withdraw {provider}`, `funding {provider, mode}`; otherwise empty |
| `actor` | `{id, login}` | As the backoffice verified them |
| `source_at` | time | When the actor submitted it |
| `evidence` | `{kind: "user_token", checked_at}` | |
| `detail` | object | Channel data frozen with the request, such as `feedback` for `retry` |

The response is `202 {request_id, status, reason?}`; `GET /v1/requests/{id}` returns the
same with `task_id` and `applied_at` once known. Statuses and authorization are
[request intake's](request-intake.md#requests). The orchestrator's own channels write the
same record in-process, with their channel, source, reply route and evidence. A comment's
`source_at` is its creation or edit time.

**Edited comments** follow [the decision on edits](#decisions): each revision is its own
source, and a later revision supersedes an earlier one only while that one is pending.

### Request events

| Type | Data |
| ---- | ---- |
| `request.orchestrator.{applied,refused,superseded}` | `request_id`, `task_id?`, `issue`, `kind`, `argument`, `actor`, `channel`, `source`, `source_at`, `reason?`, `reply` (route) |
| `request.source.received` | `source`, `issue`, `channel`, `reply`, `acknowledge` (`reaction` for mentions) |
| `request.source.final` | `source`, `request_ids`, `failure?` (`interpreter_deadline`, `interpreter_unavailable`, `unresolved_edit`) |

The publisher replies to a source once it has `request.source.final` and an outcome for
each of its `request_ids`; a final source with no requests gets its failure reply.

### Fills

Whether the initiator's credentials fill an empty slot is decided once, as of the funding
boundary ([cross-cutting rules](README.md#cross-cutting-rules)). When it freezes the
boundary, the orchestrator calls `POST /v1/fills {task_id, boundary {kind, source, at},
initiator {id, login}, providers, roles}`. Funding answers `{fills: {provider: {eligible,
reason?}}}` from the initiator's credentials and filters at `boundary.at`, records it,
and returns the recorded answer for a repeat with the same task and boundary source.
`eligible` is `yes`, `no` or `unknown`; `unknown` (the credential history doesn't reach
back far enough) leaves the boundary pending, so nothing is charged. The orchestrator
makes each `yes` slot active for the initiator with implicit provenance; the others stay
unassigned, and a credential added later never fills them.

### Grants

`POST /v1/grants`; the attempt ID is the idempotency key. A request is one of three
variants, tagged by `purpose`:

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

The response is the grant, with the same `purpose` tag:

| Field | Meaning |
| ---- | ---- |
| `grant_id`, `attempt_id`, `purpose` | |
| `status` | `issued` or `revoked` |
| `payer` | The sponsor or commenter; absent for `task_org` |
| `provider`, `slot_epoch` | `slot_epoch` for `task_contributed` only |
| `credential` | `{entry_id, record_id, version, route, custody}`, `custody` being `secret_store` or `relay`; the org route for `task_org` |
| `models`, `limits` | What the grant allows: models, and `{calls: 1, tokens, lease_seconds}`. Funding may lower the requested limits, never raise them |
| `issued_at`, `revoked_at?` | |

| Refusal | Purposes | When | Orchestrator does |
| ---- | ---- | ---- | ---- |
| `consent_cut_off` | `task_contributed` | The sponsor's withdrawal cutoff covers this slot epoch | Nothing more under that decision |
| `no_eligible_credential` | `task_contributed` | No eligible credential | `awaiting_credentials` |
| `no_eligible_credential` | `interpretation` | The commenter has no interpreter credential | Finalizes the source with `interpreter_unavailable`; exact mentions still work |
| `boundary_pending` | `task_contributed` | The decision's boundary isn't frozen | Waits |

Selection follows the [architecture's grant cases](architecture.md#services): implicit
provenance fills from credentials eligible at `boundary.at`; a new attempt under existing
consent selects among those eligible now; a replay returns the original grant with its
current `status`. `GET /v1/grants/{attempt_id}` returns the recorded grant.
`funding.grant` is a snapshot per grant with the same fields, revised when it's revoked.

Call budgets aren't funding's: the orchestrator admits every attempt against the task's
`max_model_calls` and protected capacity
([cross-cutting rules](README.md#cross-cutting-rules)) before it asks for a grant. A
grant always covers one call.

### Retries and exclusions

`retry_of {attempt_id, failure {category, model, retry_after?}}` names the failed attempt
a new one replaces. The orchestrator sends it only when the failed turn's
`provider_requests` is `some` or `none` and its failure category is one of these; an
`unknown` outcome never leads to an automatic fallback. Funding records the exclusion in
the same transaction that selects the next credential, following
[ordered credentials](credential-list.md#automatic-fallback-and-recovery):

| Category | Excluded for this task | Until |
| ---- | ---- | ---- |
| `auth` | The credential version | It's replaced |
| `model_denied` | The version, for that model only | It's replaced |
| `quota_exhausted` | The version | `retry_after` if given, else it's replaced |
| `rate_limited` | The version | `retry_after` if given, else funding's backoff |
| `unavailable` | The version | `retry_after` if given, else funding's backoff; never labeled exhausted |

### Withdrawal steps

"Withdrawal recorded" is the orchestrator's `request.orchestrator.applied`. Funding
publishes "new grants blocked" (`request.funding.blocked`) when the cutoff commits, and
"spending stopped" (`request.funding.stopped`) once every session under the revoked
grants has ended: each one the exchange lists in `exchange.grant.released` has its
binding closed (`inference.binding.closed`) and, for relay custody, its binding
`revoked`. Before phase 5 a time bound stands in ([Python exchange](#python-exchange)).

### Turn events

Published by the exchange; until phase 5 the orchestrator publishes them from its polls
([bridge](#python-exchange)).

| Type | Kind | Data |
| ---- | ---- | ---- |
| `turn.<turn>.submitted` | Fact | `turn_id`, `attempt_id`, `task_id`, `grant_id`, `stage`, `role`, `harness`, `model`, `effort`, `profile`, `session {scope, turn}`, `bundle_id`, `limits`, `spec` blob, `submitted_at` |
| `turn.<turn>.status` | Snapshot | `status` (`queued`, `running`, `finished`, `cancelled`), `created_at`, `started_at`, `finished_at`, `runner {session_id, run_id, run_attempt}` |
| `turn.<turn>.finished` | Fact | `outcome`, `reason`, `provider_requests`, `response`, `workspace` blob, `transcript {blob, bytes}`, `elapsed_seconds`, `provider_failure {category, model, retry_after}?`, `funding_ended` |

Two separate questions:

- **`outcome`** is whether the turn produced a usable result: `succeeded`, `failed` (it
  ended without one) or `unknown` (it may have produced one that was lost). `reason` is
  open and says why: `invalid_output`, `timed_out`, `terminated`, `lost`,
  `queue_expired`, `admission_unknown`, and so on.
- **`provider_requests`** is whether paid work may have happened: `none` only when the
  exchange's records show no provider request for the turn and none is still being
  recorded; `some` when at least one was made; `unknown` otherwise. Only `none` proves the
  turn cost nothing.

Neither authorizes a retry by itself: the orchestrator keeps today's rules, charging every
attempt a call before dispatch and limiting uncertain attempts per operation.

The read model takes diagnostics from these events, so the diagnostics store goes.

### Task snapshots

`task.<task>.state`, revised on every task change. M1's fields:

| Field | Meaning |
| ---- | ---- |
| `task_id`, `issue`, `kind` | `kind` is `contribution` or `standalone_review` |
| `initialized` | False while a task is enrolled but waiting, such as for a manual `start` |
| `mode` | `manual`, `autonomous` or `review` |
| `stage`, `stage_reason` | Absent until initialized. Values start from today's stages (`engine/vocab.py:6`), which phase 3's parity test preserves, and are reviewed as M3 ports each one |
| `enrollment` | `{channels, first {channel, actor, at}}` |
| `plan` | `{id, approved}` |
| `budgets` | `{model_calls, fix_rounds, plan_revisions}`, each `{used, max}` |
| `attention` | Whether the task needs a human |
| `pull_request` | `{number, head_sha}`, once one exists |

`task.<task>.consent` holds `consent_revision`, `boundary {state, kind, source, at}`
(`state` is `pending` or `frozen`), `provider_funding` and `slots`: per provider,
`{status, sponsor, provenance, epoch}`, with `status` `unassigned`, `active` or
`withdrawn`.

`task.<task>.notice`, one entity per notice: `notice_id`, `kind`, `state` (`open`,
`resolved`, `superseded`) and `data`, the kind's fields. Kinds start from today's notices
(`engine/actions.py:52`), reviewed as for stages.

### Runner dispatch

| Type | Data |
| ---- | ---- |
| `runner.dispatch` | `session_id`, `task_id`, `target_id`, `repository_id` (the fork), `ref {name, sha}`, `workflow`, `launcher_version`, `inputs` (defined by the launcher version) |
| `runner.session.ended` | `session_id`, `repository_id`, `ref` |

The publisher creates the run ref and dispatches the workflow, and deletes the ref when
the session ends.

### Write admission

The publisher refuses any write outside these rules, whatever event asks for it. Every
write is to a repository in `config.targets`, and to the task's own issue or fork.

| Surface | Account | GitHub operations | Admitted only when |
| ---- | ---- | ---- | ---- |
| `dashboard` | App | Create, then edit, one issue comment | The comment is the App's and carries the task's dashboard marker |
| `notice` | App | Create a comment; edit it to resolve or supersede | Editing only the App's own comments with that notice's marker |
| `reply` | App for comments, reviewer account for mentions | Create a comment on the source's issue, or reply in its review thread | The source has `request.source.final` |
| `reaction` | Reviewer account | Add `eyes` to a comment | The comment mentions the account |
| `stage_label` | App | Add or remove `concurro:stage:*` and `concurro:attention`; create those labels | Only those labels, only on the task's issue |
| `run_ref` | App | Create or delete `concurro/runs/<session>` on the fork | At the target's `config_sha`; never another ref |
| `dispatch` | App | `workflow_dispatch` of the target's launcher workflow at the run ref | The workflow is the one in `config.targets` |

M3's surfaces (branches, PRs, reviews, threads, checks, artifacts) add rows as they're
ported, from today's allow-list (`service/admission.py:18`).

### Publication outcomes

`publication.outcome`, a fact per settled write:

| Field | Meaning |
| ---- | ---- |
| `surface` | `{task_id?, kind, key}`, `kind` being one of the write admission surfaces |
| `revision` | The snapshot revision written, for snapshot surfaces |
| `status` | `written`, `failed` or `skipped` (a newer snapshot replaced it) |
| `written_at` | When GitHub accepted it; an approval by time binds the latest plan written before its `source_at` |
| `failure` | `{class, http_status, message}`; `class` is `rejected`, `forbidden`, `not_found` or `not_admitted` |
| `result` | `{comment_id?, url?, commit_sha?, pr_number?, check_run_id?, run_id?}` |

An uncertain write is settled inside the publisher before an outcome is published, so
consumers never see "unknown".

### Read tokens

`POST /v1/tokens/read {repository_id}` returns `{token, expires_at, permissions}`, an
installation token limited to read permissions and that repository, after checking the
repository is in `config.targets`. Tokens are never logged or put on the bus.

### Blobs

Large content (turn specs, prompts, responses, workspaces, transcripts, reports) lives in
S3-compatible object storage, content-addressed.

- **Reference:** `{sha256, bytes, media_type}`. The key is `sha256/<hex>`.
- **Writes are create-only** (`If-None-Match: *`): a key's content never changes, and a
  repeat of the same write is harmless.
- **Access:** each service has its own storage credentials. M1: the orchestrator writes
  and reads; the backoffice API reads. From phase 5 the exchange writes what runners
  upload.
- **Secrets never go into blobs.** A turn spec's blob excludes credentials and tokens, as
  the diagnostics store's request record does today (`service/diagnostics.py:145`).
- **Lifecycle:** kept while a task, turn or report references them; a retention job
  deletes unreferenced blobs. Sketch until phase 4.

### Credentials

Funding's API for users' credentials, called by the backoffice API on behalf of a
signed-in user. Sketch, phase 4.

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

- **Funding verifies the user itself.** The backoffice API forwards the user's GitHub
  token, and funding resolves it and checks it matches `user_id`, as the vault does today.
  A compromised backoffice can't change a user's credentials without their token.
- Every mutation takes `Idempotency-Key`, removal included, and `If-Match` with the entry
  or list revision.
- Entries, lists and relays have separate paths, so no route depends on declaration order.
- Relay status is shaped once, by funding, with RFC 3339 times.

### Exchange operator API

Sketch, phase 5. The orchestrator submits turns; grants come from funding's events, not
from the caller.

| Call | Purpose |
| ---- | ---- |
| `PUT /v1/turns/{turn_id}` | Submit: `task_id`, `grant_id`, `target_id`, `limits`, `spec` blob, `session_pin?`. Idempotent on the turn ID |
| `GET /v1/turns/{turn_id}?wait=` | Status and, once finished, the result; the same data as `turn.*` |
| `DELETE /v1/turns/{turn_id}` | Cancel; a later submit with that ID is refused |
| `DELETE /v1/sessions/{session_id}` | End a session |

The exchange admits a turn only under an `issued` `funding.grant` whose models, limits and
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
inside results. Each target's launcher version is in `config.targets`, and a contract
test runs every supported launcher version against the exchange; v1 retires when no
target is on it.

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

Sketch, phase 3, for Rust-owned tasks; everything from phase 4.

- **Lists** are paged and filtered: `GET /api/tasks?target=&stage=&kind=&cursor=`, and the
  same under a target. Today's fixed server limits (three pages of 100 issues) go.
- **Tasks** by ID: `GET /api/tasks/{taskId}` with its turns, reports and requests. A page
  addressed by issue resolves it with `GET /api/issues/{owner}/{repo}/{number}`.
- **Requests** replace commands: `POST /api/requests` with `Idempotency-Key` returns
  `{requestId, status}`; `GET /api/requests/{id}` follows it. Nothing is "may have
  posted".
- **Live updates:** `GET /api/events` streams `{type, entity, revision}` from the read
  model, and the client refetches what changed. The read model's revision replaces
  today's content hash as the cache key.
- **Credentials** under `/api/me/credentials` and `/api/me/relays`, mapped onto funding's
  [credentials API](#credentials).
- **Errors** use the shared error shape.

In phase 3 the list and event endpoints also cover Python-owned tasks through the
[list adapter](#python-lists); the frontend sends task pages and commands for
Python-owned tasks to the Python backoffice.

## Bridges

### Ownership

During the migration both the orchestrator and the Python service create tasks, so one
record decides which owns each issue. After phase 4 every task is the orchestrator's,
and the claim becomes an internal step of creating a task.

| Call | Request | Response |
| ---- | ---- | ---- |
| `POST /v1/ownership/claims` | `{issue {repository_id, number}, claimant, input {kind, ref}}` | `{issue, owner, task_id?, generation?, decided_by, created}` |
| `GET /v1/ownership/{repository_id}/{number}` | | The same record, or `404` |
| `PUT /v1/ownership/{repository_id}/{number}` | `{owner: "python", generation}` | Operator seeding before the pilot; refused once a record exists |

- `claimant` is `orchestrator`, `python_service`, `python_backoffice` or `backoffice_api`;
  `input.kind` is the first routed input (`comment`, `label`, `assignment`,
  `review_request`, `mention`, `backoffice`).
- `owner` is `rust` or `python`. `decided_by` is `recorded` (an existing record),
  `pilot_list` or `switch`. `task_id` is set for `rust`; `generation`, the Python
  registry's ownership generation, only for seeded `python` records.
- The pilot list and the switch are the orchestrator's settings, published in
  `config.targets`; changing them never changes a recorded owner.
- Insert-if-absent: any later claim returns the stored record unchanged, with `created`
  false. Callers act on the returned owner, whatever they asked for.
- Seeding covers every issue the Python service holds state for, issues with only
  pre-enrollment sponsor sources included (`issue_sponsor_sources`).
- Each new record is published as `ownership.claimed`, a fact with the record's fields,
  so the backoffice API's list adapter knows every issue's owner without calling the
  orchestrator.

### Python exchange

Phases 3 and 4 use today's operator API (`exchange.py:1580`), with these changes in the
exchange:

- **Tokens:**
  - The orchestrator's token can only submit, poll and cancel turns, end sessions, and
    read capabilities and observations.
  - Funding's token can only post sponsorship transitions.
  - The Python service keeps today's token for its draining tasks.
  - On a task's first operation the exchange reads `GET /v1/ownership/{...}`, records
    the owner, and refuses the other side's token for that task. If the lookup fails, it
    answers `503` and records nothing.
- **Grants:** for a Rust-owned task, a contributed turn needs funding's active slot row;
  the exchange never takes it from the submitted `admission.grant`.
- **Provenance:** `assignment`, `mention` and `review_request` join `task_start` and
  `label`.
- **Admission:** `custody` is allowed as a thirteenth field, as the engine expects.
- **Turn tombstones.** Today the exchange deletes finished turns after a day
  (`exchange.py:1461`), cancelled ones included (cancellation writes a `turns` row,
  `exchange.py:1423`), and a resubmit after that is admitted again. The bridge keeps a
  tombstone instead:
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
  - Tombstones are kept until the Python submission path is retired, after the phase 5
    drain and every recovery job that could resubmit has finished.
- **Interpreter** (only if built before phase 5): an `interpreter:<provider>` role, and
  interpretation grants that funding posts without a task slot, since they don't depend
  on task consent.

**Slots, not grants.** Today's exchange holds one sponsorship slot per task and provider,
whose `grant_id` and `epoch` change only with consent, and it accepts each change only as
the next step from the slot it holds (`exchange.py:773`). So the slot adapter needs every
consent change, in order, which neither grants nor a consent snapshot give it. The
orchestrator publishes them as a bridge fact, `task.<task>.slot.changed`, one per change
to a provider slot, in the transaction that makes it:

| Field | Meaning |
| ---- | ---- |
| `task_id`, `provider` | The slot |
| `consent_revision` | The task's consent revision this change made; orders the slot's changes |
| `previous_revision` | The consent revision of this slot's previous change; null for the first |
| `reason` | `initialize`, `sponsor`, `funding`, `withdraw` or `permission_loss` |
| `slot` | `{epoch, status, sponsor, provenance, source_funding_mode, funding}` after the change |
| `actor`, `cutoff` | For withdrawals and permission loss: whose consent ends, through which epoch |

- **Slots start at the funding boundary.** The first change is `initialize`, at epoch 1
  with the boundary's payer or epoch 0 without one; sponsorship before the boundary is
  folded into it, as today. Nothing is published for a slot before its boundary freezes.
- **Funding's slot adapter applies a slot's changes strictly in order.** It holds a
  change until the one named by `previous_revision` has been acknowledged by the exchange.
  It sends each as today's transition (`exchange.py:692`): `expected_epoch` from its own
  record of the last acknowledged epoch (−1 before `initialize`), never from the
  exchange's state; `generation` 1; and today's `transition` digest. It records the body
  before sending and the reply after.
- **A lost reply is retried with the same body;** the exchange returns its stored reply
  for that digest. The adapter never sends a slot's next change before the previous one
  is acknowledged.
- **A slot's Python `grant_id` is the SHA-256 hex of `(task_id, provider, slot_epoch)`.**
  Every target grant under that slot epoch maps to it, so attempts, concurrent ones and
  credential replacements included, share one slot row.
- **Grants wait for their slot.** Funding answers a `task_contributed` grant only once the
  change that set its slot epoch has been acknowledged; until then it answers
  `503 slot_pending`, retryable. `task_org` and `interpretation` grants need no slot.

**The orchestrator's exchange adapter** turns a target grant into today's submit request
(`exchange.py:1274`):

- `turn` is the attempt ID, `generation` 1 (Rust-owned tasks have one ownership
  generation), `budget` the grant's token limit and `lease` its lease.
- For `task_contributed`: the binding's `funding` (`contributed`), `sponsorship_provider`,
  `sponsor` (as a decimal string), `epoch` (the slot epoch), `grant_id` (the slot's, as
  above), `route`, `credential` (the record ID) and `credential_version` come from the
  grant. `admission` is today's twelve fields plus `custody`: `credential.id` is today's
  digest of sponsor, route, record and version, and `grant` is the slot as the slot
  adapter posted it.
- For `task_org`: `funding` is `org` and every personal field is null, as the exchange
  requires (`exchange.py:306`).
- It stores the exact body before the first call: the exchange keys a retry on the turn
  ID and the body's digest, so a rebuilt body would be refused.
- **Resubmitting is only for a lost first reply.** Once the exchange's admission reply is
  recorded, together with any `dispatch` it returned so a crash can't strand the runner
  dispatch, the adapter never resubmits that turn; it only polls. A poll that finds the
  turn gone (`404`), or a resubmit answered `410 turn_expired`, makes the attempt
  `unknown`, unless the orchestrator has already recorded its terminal result, which
  stands.
- It polls each turn and publishes `turn.*`. Today's `done` and `lost` statuses are
  `finished`. Of today's outcomes, `ok` is `succeeded`; `failed`, `terminated` and
  `queue_expired` are `failed`; `uncertain`, `lost`, `timed_out` and `admission_unknown`
  are `unknown`. Each is kept as the `reason`, and an invalid response as
  `invalid_output`.
- It takes `provider_requests` and `provider_failure` from the exchange's records for
  the turn, once the turn has finished. `none` needs both: the turn is contributed (the
  exchange records only contributed requests; org requests bypass it,
  `inference.py:90`), and that turn has no recorded request and none still awaiting its
  observation (`pending(turn=...)`, `inference.py:452`). Any recorded request is `some`.
  Everything else, every org turn included, is `unknown`.
- It publishes `runner.dispatch` for the exchange's `dispatch` reply, with today's
  launcher inputs (`service/gateway.py:284`), and `runner.session.ended` when it ends a
  session. It reads `GET capabilities` and holds contributed turns while their route
  isn't offered.

**Withdrawal steps before phase 5.** Once the exchange has acknowledged a withdraw
transition, it refuses new sessions and relay requests under that slot (`allowed()`,
`exchange.py:515`), and every existing session ends by its deadline: its creation plus its
lease, which the verifier enforces on relay work by itself. So funding publishes "spending
stopped" when the acknowledgement time plus the longest `lease_seconds` among the slot's
grants has passed, or earlier once every turn under them has finished and none used relay
custody. The withdrawal job records both values when the acknowledgement arrives; an
acknowledgement that only arrives after a lost reply delays the report, never advances
it. A relay release that fails (`inference.py:350` ignores errors) can't make it report
early.

### Turn payload v1

Today's runner takes `{request, feedback, spec, retained, timeout}`
(`engine/agents.py:130`) and returns `{outcome, response, workspace, retained,
transcript, termination_reason, elapsed_seconds}` (`engine/turns.py:400`). The
orchestrator's payload adapter builds the first from its turn spec and maps the second
back, base64 archives included, until the runner moves to the target protocol.

### Vault

Funding's vault adapter reads the vault's service API until phase 5:

- `GET /metadata/{user_id}?at=` gives a user's credentials and lists as of a time, with
  `history_complete`. Fills use `at` = the boundary; `history_complete` false makes the
  fill `unknown`.
- Only an entry's latest version is listed, and the exchange checks current metadata only
  for a turn it hasn't admitted (`exchange.py:1305`):
  - Replaying an admitted turn returns its stored admission, whatever has changed since.
  - A first submit whose version has since been replaced gets `410`. That attempt never
    ran, so funding treats the version as unusable for new attempts on the task, like an
    `auth` failure.
  - Once the exchange has dropped an admitted turn's record (finished turns are kept for
    a day), a resubmit gets `410 turn_expired` from its tombstone, whether or not the
    credential is still valid. That doesn't show the attempt never ran: the orchestrator
    records it as `unknown`, with no automatic fallback.
- `GET /metadata/events?after=` pages credential events. The adapter stores its cursor
  with the events it applies, in one transaction, and maps them to `funding.credential`.
- User IDs arrive as strings in metadata and integers in events; the adapter makes them
  integers.

### Python lists

The backoffice API's list adapter merges the Python service's lists with its read model
for phase 3:

- **Issues:** `POST /lists {repository, kind: "issues"}` for Python-owned issues, joined
  with `POST /sponsorships {repository}` for their sponsorship projections, as the Python
  backoffice joins them (`backoffice/tasks.py:427`). The list calls use the backoffice
  API's own list token; `/sponsorships` is called with the viewer's own token, so its
  per-user scope (`own` for non-writers) stays the Python service's decision.
- **Owners** come from `ownership.claimed`; Rust-owned entries come from the read model.
- **Changes:** the adapter long-polls `POST /lists/changes`, drops changes for Rust-owned
  issues, and passes the rest into `/api/events`. A `resynchronize` reply, or a new
  `incarnation`, makes it reload the Python lists and send the frontend a reload.

## Decisions

1. **The runner.** M1 uses today's runner unchanged, on [payload v1](#turn-payload-v1).
   Its protocol moves to the target protocol and runner API v2 in phase 5, with the
   exchange. Porting its code to Rust is a separate, later step.
2. **Edited commands** follow the kind of the new revision:
   - A revision still pending is superseded by the next one, and stays recorded as
     `superseded`.
   - A sponsorship command (`sponsor`, `withdraw`, `funding`) applies per revision, as
     today, whatever an earlier revision said: an applied `start` edited by its author
     into `withdraw openai` withdraws.
   - A standalone review command applies per revision, as today.
   - Any other command isn't applied if an earlier revision of the comment was; its
     request is refused as `already_applied`.
3. **Launchers** are versioned per target in `config.targets`, every supported version is
   contract-tested, and v1 retires when no target is on it.

## Open items

1. **Timeline check.** The read-only part is done
   ([enrollment channels](#enrollment-channels)). Before M1, with controlled changes on a
   test repository: measure the delay between a webhook and its event being listed;
   confirm each action updates the issue's `updated_at`; and confirm assignment events and
   a removed and re-added `concurro` label.
2. **Bridge tests.** A test drives today's exchange with the
   [Python exchange](#python-exchange) mappings: slot grants shared by attempts, ordered
   changes from the adapter's own record over HTTP (out of order, a lost reply, renewed
   consent, the cutoff), a replacement credential, org grants, the stored body, and both
   of today's mismatches. It lands with the adapters. The pre-boundary fold reaches it
   already folded; folding itself belongs in the orchestrator's M1 tests. Still untested:
   concurrent adapter instances, the `provider_requests` and withdrawal-expiry mappings,
   and, once the bridge changes are built, interpretation admission and turn tombstones
   (an unchanged-credential resubmit after pruning, a changed-body resubmit, a turn
   cancelled before submission then pruned, and tombstones surviving a restart).
