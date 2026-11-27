# Plan: phase 3, the task application

**Status (2026-11-24): M1–M3 implemented and cut over.** The task application is the
controller in production. Local evidence covers [M1](../../rust-phase-3-m1-evidence.md),
[M2](../../rust-phase-3-m2-evidence.md) and [M3](../../rust-phase-3-m3-evidence.md).
The plan below preserves the migration baseline; Python backoffice/exchange/vault wiring
has since been replaced by phases 4–5. Owner-use and team-readiness criteria are not a
claim that every workflow has been exercised live.

Phase 3 of the [architecture review](../architecture.md#migration) replaces the Python
service's task controller with a Rust task application, with Postgres as the authority,
and gives the backoffice a Rust API in front of it. Concurro is a multi-user team
product, and phase 3 keeps every workflow the team has today. The operator builds
and verifies it locally, takes over every target in one cutover, uses it on real
tasks, and then invites the team. The dedicated test repository was waived for
phases 1–3; local executable, database, broker and controlled external-service
fixtures provide implementation acceptance. They do not establish live publication
behavior.

Python and Rust never run tasks for the same targets at once: there is no per-issue
ownership, pilot list or task import. The Python runner, exchange, gateway and vault
stay, called through their APIs, and the Python backoffice keeps browser sign-in and the
credentials pages. The Python service's controller retires at the cutover.

## Milestones

| Milestone | Plan | Depends on | Done when |
| ---- | ---- | ---- | ---- |
| M1, GitHub-only intake | [M1 plan](phase-3-m1-plan.md) | Phase 2 | On the test repository, with org funding: `/concurro start` runs intake through the real exchange and runner, the result appears on GitHub, `stop` stops it, a refused command changes nothing, and a controller restart at any point duplicates no turn and no publication |
| M2, one contribution | [Below](#m2-one-contribution) | M1 | A representative issue goes from `start` to a ready PR, others reach each terminal stage, every task command works, and the operator can inspect a stuck task and recover from the named failures |
| M3, team workflows | [Below](#m3-team-workflows) | M2 | Every [team workflow](#team-workflows) runs on the task application and the backoffice API, and the [team readiness](#team-readiness) checks pass on the test repository |
| Cutover | [Below](#cutover) | M3 | The task application is the sole controller for every target; the frontend uses the backoffice API for tasks |
| M4, owner use | [Below](#m4-owner-use) | Cutover | The operator has completed representative tasks on real targets, and the team readiness checks pass again there |

M1 is an engineering milestone: it proves the lifecycle on GitHub alone. The cutover
waits until no team workflow regresses (M3), and the team is invited after M4.

M1's local implementation is accepted. The [M1 evidence](../../rust-phase-3-m1-evidence.md)
records the full 123 test checkpoint and the separately tested five regression
closure delta. No live target was exercised.
M2 is also accepted locally; its [evidence](../../rust-phase-3-m2-evidence.md)
records the frozen source, correction and test limits. M3 is also accepted locally;
its [evidence](../../rust-phase-3-m3-evidence.md) records scope and test limits.

## Team workflows

What the team can do with the Python service today, and where each continues. Nothing in
this table is dropped; [deferrals](#deferrals) lists what isn't carried over and why.

| Workflow today | Continues in |
| ---- | ---- |
| `/concurro start`, `stop` by a writer, with replies and the dashboard | M1 |
| Intake, plan, plan QC, `approve`, implementation, publication, review, gates and the terminal stages; clarification answered in comments | M2 |
| `retry`, `retry --plan-revisions N`, `resume`, `invalidate` | M2 |
| Call budgets, plan revision and fix round limits, the uncertain-attempt limit | M1, M2 |
| Personal credentials: the initiator's credentials filling a provider, `/concurro sponsor`, `withdraw`, `funding`, sponsorship before enrollment, ordered credentials and their fallback | M3 |
| Autonomous enrollment by the `concurro` label | M3 |
| Standalone PR reviews, requested by asking Concurro's reviewer | M3 |
| Backoffice sign-in, own credentials (and relay approvals where enabled) | M3: unchanged, Python backoffice and vault |
| Backoffice task lists, task pages, run diagnostics, sponsorships, live updates | M3: the backoffice API, from the task application's events |
| Backoffice commands, `withdraw` by any user who can see the issue | M3: the Python backoffice keeps the command POST and its CSRF check, and forwards each command as a signed request to the task application |
| Webhook delivery recovery and coverage gaps | M3 |

### Deferrals

Original scope decisions, reconciled with phases 4–5:

| Deferred | Why | Impact |
| ---- | ---- | ---- |
| Mentions and assignment channels | New features, not in the Python service today ([mentions](../mentions.md)) | None at the cutover; they're built on the task application afterwards |
| Relay-custody credentials | Implemented through phases 4–5 ([sponsor relay](../sponsor-relay.md)) | Pairing/connectivity and local joined Antigravity inference verified; deployed Actions/provider qualification remains open |
| Command comments posted on a user's behalf by the backoffice | A backoffice command becomes a request with the signed-in user as its actor ([request intake](../request-intake.md#backoffice)), so the delegated comment and its "may have posted" recovery go away | The browser receives a request receipt; the durable request records the actor. No GitHub command or reply comment is posted for this channel |
| Sign-in, credential writes and relay approvals in Rust | Completed in [Phase 4](phase-4-plan.md); native storage followed in Phase 5 | Accounts serves these flows; the Python backoffice is retired |

## Roles

The task application keeps four roles apart in its records, and never infers one from
another:

- **Task author:** the issue's author. Their text is part of the requirements; it grants
  no authority.
- **Actor:** whoever made a request, a GitHub user commenting or labeling, or a user
  signed in to the backoffice. Each request is authorized at application for its actor,
  by today's rule for its kind ([request intake](../request-intake.md#authorization)), and
  its outcome names them.
- **Operator:** whoever runs the task application's host commands. Operator actions are
  recorded as the operator's, never as a GitHub user's; they settle effects and cancel
  turns, and never make a request, consent or approve on a user's behalf.
- **Payer:** the org, the initiator filling a provider from their own credentials, or an
  explicit sponsor, decided per attempt from recorded consent and recorded with the
  attempt.

Viewers are signed-in backoffice users. What each sees follows from their own GitHub
access to the target, evaluated with their own token, as today.

## Design

### Processes

What runs at the cutover:

| Process | Separate because | Holds |
| ---- | ---- | ---- |
| Ingress (Rust, phase 2) | Public webhook endpoint; must accept deliveries while the task application restarts | The webhook secret, delivery receipts |
| Task application (Rust) | Sole controller and sole GitHub writer: the highest-privilege Rust process | Task state; the App's private key, the reviewer account's user grant (stored, refreshed and bound by numeric ID, as `service/user_grant.py` keeps it), the exchange's operator token, the vault's metadata bearer and the archive bucket credential |
| Backoffice API (Rust) | Faces browsers through the edge; must not hold the task application's authority | Its read model; a read-only bearer for the task baseline, turn artifacts and transcript entries; a read-scoped credential to the Python backoffice (session identity and tiers only). It submits no command |
| Backoffice (Python) | Signs users in with GitHub OAuth and keeps their tokens; takes the browser's commands | Sessions, the command signing key, the requests API bearer; its credentials and relay approval pages call the vault |
| Exchange (Python) and agentgateway, two containers; the exchange shares the gateway container's network namespace (`gateway/compose.yaml:22-46`) | Runners on the internet call them; they hold org provider keys and runner OIDC trust | Turns, sessions, sponsorship slots and per-issue generations |
| Vault (Python) | Personal credential material and its keyring | Credential records |
| Runner (GitHub Actions, in target forks) | Runs untrusted model output | Nothing durable |
| Postgres, NATS and S3-compatible object storage | Shared infrastructure | Service databases, event streams and a private bucket for original runner archives |

Who owns each durable change:

| Change | Owner |
| ---- | ---- |
| A webhook delivery received, a GitHub event published, coverage | Ingress |
| A request recorded or applied, consent, a stage change, an attempt admitted, its payer and its charge, an effect recorded or settled | Task application |
| The backoffice's read model | Backoffice API, from the task application's events |
| A session, a user's GitHub token | Python backoffice |
| A credential saved, replaced or deleted | Vault, through the Python backoffice |
| A turn queued, a session issued, a sponsorship slot, a turn result | Exchange, on the task application's calls, and the runner |
| A comment, label, ref, branch, PR, review, check run or dispatch | GitHub, written only by the task application |

### The task application

One deployable Rust application, `concurro-tasks`, runs the task lifecycle. Its modules
share one database and commit together, with no internal API or bus between them:

| Module | Owns |
| ---- | ---- |
| Intake | Channels from `github.*` events and the requests API, sources and requests, command parsing and authorization ([request intake](../request-intake.md)) |
| Tasks | Task state, stages and the decision function |
| Funding policy | Consent, the funding boundary, fills, credential selection and grants, sponsorship slots, withdrawal |
| Attempts | Call budgets, admission, the uncertain-attempt limit, the attempt record and its payer |
| Exchange | The Python exchange's operator API: submit, poll, cancel, end sessions, slot transitions |
| Publication | GitHub App and read tokens, write records, rendering, settlement, run refs and dispatch, delivery recovery for ingress |
| Operator | Host-only views and recovery commands |

- **Decide in one transaction, act in jobs.** The task logic is a function of the task's
  stored state and one new input (a request, a turn result, a read, a write outcome),
  returning the new state, its events and the effects it wants, with no I/O. It runs in
  one transaction, which also makes the payer decision, admits the attempt and reserves
  its call. Effects (a GitHub read or write, a vault read, a submit, a poll, a cancel) are
  `graphile_worker` jobs with stable keys, enqueued in that transaction; each records its
  outcome as a new input. A task's decisions run one at a time in a queue named for the
  task; effects run in their own queues, so a 45-second poll never delays a `stop`.
- **Every effect is recorded before it's sent.** An effect is `recorded`, then marked
  `sending` just before its request leaves, then settled with its outcome. A `recorded`
  effect was never sent and is safe to send. A `sending` effect with no outcome is
  uncertain. Publication reads back before deciding what to do. For managed labels and
  run refs it reconciles the latest desired state, including after a delayed HTTP
  completion. Dispatches are never resent.
- **Events for other processes** go through phase 1's transactional outbox and the bus:
  task, request and turn snapshots and facts for the backoffice API
  ([contracts](../contracts.md#target-catalog)). They're published in the transaction that
  makes the change. Nothing inside the application consumes them.
- **Crates,** as ingress is built: `concurro-tasks-core` (the modules' rules and types; no
  SQLx, NATS or HTTP), `concurro-tasks-postgres` (migrations and the transaction API) and
  `concurro-tasks` (the binary: the event consumer, the requests API, jobs, clients, the
  operator commands). Shared crates: `concurro-github`,
  `concurro-exchange-v1`, `concurro-vault-v1` (below), and phases 1 and 2's contracts,
  events, database, configuration, telemetry and test kit. A crate or module that passes
  about 1,000 lines is a prompt to reconsider its responsibilities, not a limit.
- **GitHub is called through a thin client of our own,** `concurro-github`: App JWTs
  (`jsonwebtoken` 12.0.0, whose default features sign nothing: `rust_crypto`, whose
  `hmac` 0.13 and `sha2` 0.11 match the workspace, or `aws_lc_rs`, chosen by M1's RS256
  spike), installation tokens with today's permission checks
  (`service/identity.py:134`), and typed requests and responses with only the fields
  Concurro uses, over the workspace's `reqwest`. Every call reports one of `ok`,
  `rejected`, `forbidden`, `not_found` or `uncertain`: no response after the request was
  sent, or one that doesn't say whether a write happened (`5xx`, `408`, `429`), as
  `service/settlement.py:64-88` classifies. Not `octocrab` (0.54.9): its models are
  GitHub's full payloads with closed enums, the reason ingress didn't use it, its errors
  don't separate an uncertain write from a refused one, and it pins `hmac` 0.12 and
  `sha2` 0.10 beside the workspace's 0.13 and 0.11.
- **Every GitHub shape is captured, not written by hand.** Each response type, and each
  request a write sends, has a fixture recorded from GitHub, anonymized, with the date
  and endpoint it came from. Phase 2's mapping shipped with hand-written raw fixtures
  that matched its own code and not GitHub's payloads.
- **Python is reached through bridge crates named for the bridge:**
  `concurro-exchange-v1` and `concurro-vault-v1`, the only crates that know their
  shapes, with fixtures captured from today's services. Each retires with its bridge in
  phase 5.
- **Diagnostics and archives.** From M1 the task application indexes new transcript
  messages, tool calls and results in PostgreSQL as it receives them. The historical
  importer uses the same path. Full source events and searchable text retain their
  turn and session links. Large PostgreSQL values use LZ4 where available and PGLZ
  otherwise. PostgreSQL retains the original gzip for restore until the uploader
  verifies it in the private archive bucket. An operator can retry an exhausted
  upload independently of the turn. An unreadable archive leaves a diagnostic on
  the completed turn and cannot supply a restore point. Readable transcripts stay
  searchable even when their journal cannot resume the requested turn. Admission
  checks that journal before reserving or charging a call. Specs, prompts, responses
  and workspace changes stay in PostgreSQL. From M3 the backoffice reads these diagnostics
  through the task application.
  [Storage contract](../contracts.md#blobs)
- **Reports** are stored in the task database and carried in the task's report events.
  Whether full reports are also published on GitHub stays an
  [open question](../architecture.md#open-questions) until M2, which publishes reports.

### The backoffice API

A separate Rust process, the backend for the frontend (`concurro-backoffice`), under
`/api/v2`:

- **Lower privilege.** It holds no App key, no exchange or vault token, and no user's
  GitHub token, and never receives the Python session cookie: at sign-in the Python
  backoffice also issues a read-only viewer token, which answers only identity and tier
  lookups, and the edge strips the session cookie from `/api/v2/*` requests. Its bearer
  to the task application allows baseline, transcript and turn artifact reads. It holds
  no S3 credentials and cannot authorize a command through either service.
- **Its read model** is fed by the task application's events, each snapshot kept at its
  highest revision, and serves task lists, task pages, run diagnostics, sponsorships and
  live updates. An empty or stale read model first creates its durable consumer, then
  loads the task application's baseline, read as one consistent database snapshot
  across its pages: every current task, consent and target index snapshot, and the
  durable records its views show (turn records with their latest status, prompt and
  transcript
  references, request outcomes, consent facts). It then applies the buffered and later
  events, deduplicated by fact ID and snapshot revision. So a PR, a run, a turn's prompt
  or a replaced sponsorship older than the stream's retention still appears
  ([contracts](../contracts.md#backoffice-api)).
- **Viewer scope:** the Python backoffice tells it, for a viewer token, who the user is
  and their tier on each target and issue, evaluated with the session's own GitHub token
  on an internal listener on the platform network. As today, full task views (turns,
  prompts, transcripts, workspace changes, reports) need upstream write access and
  visibility of both the upstream and the fork (`backoffice/tasks.py:187-193`); a reader
  gets a summary (issue, title, stage and reason, mode, PR link, updated time) and their
  own consent. Every turn and blob read goes through the same check, and blob URLs are
  never handed out.
- **Own consent** comes from the task application's consent facts per user and issue,
  on issues with no task too, so "my sponsorships" keeps a user's historical and
  pre-enrollment consent after a replacement or the loss of write access
  (`backoffice/tasks.py:645-660`).
- **Commands don't go through it.** Through phase 3 the browser's command POST stays on
  the Python backoffice, the session authority, with its CSRF check
  (`backoffice/app.py:502-510`) and today's per-kind check with the viewer's token
  (`service/actions.py:149-156`). It signs the command, bound to the user's numeric ID,
  the issue, the kind, its arguments, `detail.feedback`, `source_at`, the action ID and
  an expiry, and forwards it to the task application's `POST /v1/requests` itself. The
  backoffice API can neither obtain a signature nor submit a command. The task
  application verifies the signature, takes the actor from it, refuses a request that
  doesn't match it, and rechecks the actor's permission at application. A refused
  request changes no task state.
- **Retries survive a lost response.** The frontend gives each action an ID that its
  retries keep (today's `actions.command` sends none,
  `concurro-backoffice/src/lib/api/live.ts:153`), and the Python backoffice keeps the
  action's original `source_at`. The task application's idempotency is over the action
  ID, the actor and the command's contents, not the signature, so a retry re-signed
  after the first signature expired returns the recorded outcome.

### Python components

Each change is listed with its caller and why the release needs it.

| Component | Change | Caller, and why | Milestone |
| ---- | ---- | ---- | ---- |
| Exchange | Turn tombstones: a pruned or cancelled turn ID can never be admitted again ([bridge](../contracts.md#python-exchange)) | Task application: it replays an attempt's stored submit after a crash; without a tombstone, a replay after the exchange prunes a finished turn runs it twice, and a cancel before the submit doesn't fence it | M1 |
| Exchange | Turn retirement returns all retired session IDs on every acknowledged call and preserves terminal results | Task application: clean up a lost submit and prevent an old session from claiming the next same-group turn | M1 |
| Exchange | Per-turn observations | Task application: credential fallback decides from a turn's own unresolved requests | M3 |
| Exchange | The `review_request` provenance and `custody` as an admission field | Task application: standalone reviews paid by personal credentials, and the engine's payload | M3 |
| Exchange | The operator API on its own listener on the platform network: the gateway container joins the platform network, and the exchange binds its platform alias address | Task application, its only caller after the cutover; the edge stops reaching it. Done at the cutover because the Python service calls it through the edge until then | Cutover |
| Vault | Its metadata API on a listener bound to its platform-network alias address, as the resolver listener binds `credential-private` (`gateway/compose.private.yaml`); `GET /metadata/{user_id}?at=` already reports `history_complete` (`gateway/vault/vault.py:1086`) | Task application, for fills and selection: today the metadata listener is reachable only on the edge network, which the task application isn't on. Other callers keep the edge address until they move | M3 |
| Backoffice | A read-scoped session and tier API on its own platform-network listener ([bridge](../contracts.md#python-backoffice-sessions)); its command POST signs and forwards each command to the task application | Backoffice API: viewer scope without holding user tokens. Task application: commands as the signed-in user, with evidence that the user's session issued them | M3 |
| Backoffice | Task and list pages and delegated commands retire | The frontend calls the backoffice API for tasks | Cutover |

Funding no longer needs a separate exchange token, and the exchange needs no owner
check: the task application is the only controller after the cutover, and the test
repository's tasks never share an issue with the Python service's before it.

## M2, one contribution

The M2 implementation maps each stage to its Python behavior and records recovery
cases for uncertain publication and review submission.
The [M2 behavior and acceptance map](phase-3-m2-map.md) records those cases and
the retained continuation admission boundary.

**Outcome:** the stages after intake, every task command, and the operator's recovery
tools, enough for one contribution end to end. The trace it must make work, in manual
mode with org funding:

1. **`start` → intake** (M1) → `plan`.
2. **Plan:** the planner's turn (`Controller.plan`, `engine/controller.py:1768`) writes the
   plan; plan QC reviews it (`plan_qc`, revised within `max_plan_revisions`), and the task
   waits at `planned` with the plan on the dashboard. A `clarify` stage waits for the
   author's or a writer's answer.
3. **`approve`** by a writer binds the unchanged plan (`engine/commands.py:219`) and sets
   `implement`; the approver is the actor, whoever started the task.
4. **Implement:** the implementer's turn produces the change in the runner
   (`Controller.implement`, `:2045`).
5. **Publish:** the branch and a draft PR at the task's base, written as recorded commits
   so a lost reply is settled by the ref's SHA (`Controller.publish`, `:2098`;
   [scenario 19](../contract-scenarios.md#19-an-uncertain-branch-or-pr-write)).
6. **Review:** each reviewer's turn against the immutable change, findings published as
   one review per round with threads, fix rounds by the implementer within the task's
   budgets (`Controller.review`, `:2117`).
7. **Gates:** CI and current approvals observed, the PR marked ready once, and the "Ready
   for review" notice; Concurro never merges (`Controller.gates`, `:2204`).
8. **Terminal:** `observed_merged` when a person merges the PR (`:2261`), `closed` when
   it's closed unmerged, or `stopped`, `rejected`, `escalated` or `blocked` on the way.

Retained-turn regression fixtures cover these boundaries:

- Replay a complete journal with the same scope, ordinal and request digest but a
  null cached response. Verify refusal before any grant, call charge or dispatch.
- Use an otherwise eligible, highly compressible archive. A continuation and an exact
  replay one byte over the uncompressed limit must each be refused before funding.
  With the limit equal to the restored file size or explicitly `null`, both are eligible.

**Recovery the operator gets:** `tasks show` explains why a task is waiting and on what;
`effects pending` separates a `recorded` effect (never sent, safe to retry) from a
`sending` one (may have happened, settled by readback or by the operator from what
GitHub or the exchange shows); `retry` re-runs a failed stage under a new attempt and
`resume` adopts the current configuration, as today (`engine/commands.py:257`).

**Done when,** in the local GitHub/exchange fixtures after the live test repository waiver: the trace reaches a ready PR, and other issues
reach `observed_merged`, `closed`, `rejected`, `escalated`, `clarify` and `blocked`;
`retry`, `retry --plan-revisions N`, `resume` and `invalidate` behave as specified; the controller restarted
inside each stage duplicates no turn, commit, PR, review or comment; a lost reply on a
branch, PR or review write is settled without a second write; a stuck task's cause and
next step are readable in `tasks show`.

## M3, team workflows

Before implementing M3, map funding and permission changes to their Python behavior
and specify recovery cases for withdrawal, lost access and retried signed commands.

**Outcome:** the rest of the [team workflows](#team-workflows):

- **Personal credentials:** consent and the funding boundary, fills from the vault's
  metadata at the boundary, credential selection and fallback as today
  (`Shadow.preflight`, `service/shadow.py:514-591`), sponsorship slots on the exchange,
  withdrawal and its cutoff. A source's funding mode is the policy activation covering its
  time for the task's exact configuration, recorded at the task application's clock and
  never backdated, or `unknown` (`service/registry.py:1182-1219`); implicit personal
  funding needs `contributed`. A policy is activated only from a read of the target's
  current configuration, never from a pinned SHA read to recover or resume an older task
  (`service/worker.py:345-347`, tested at
  `service/tests/test_sponsored_admissions.py:1088`). A fill's answer applies only if the
  boundary candidate, the folded sources' versions, the issue's source revision and the
  consent revision it was asked under are all unchanged. The rules are the [consent
  scenarios](../contract-scenarios.md) and the payer rules in
  [contracts](../contracts.md#fills).
- **Autonomous enrollment** by the `concurro` label, with the labeler's authority
  rechecked before each attempt; the timeline checks of
  [open item 1](../contracts.md#open-items) come first.
- **Standalone PR reviews** by review request, published with a reviewer account's own
  grant and attributed to its configured numeric ID. M3 uses a separate test reviewer
  account and grant: the production grant's refresh token is single-use, so it's never
  held by two services that could both refresh it.
- **The backoffice API,** its baseline and read model, the Python backoffice's
  read-scoped session and tier API and its forwarding of commands, and the frontend's v2
  calls.
- **Historical diagnostics:** build and rehearse the restartable
  [Python archive import](../contracts.md#python-archive-import) before the cutover.
- **Delivery recovery** for ingress and **coverage gaps** (a strict re-read of every
  active task).

**Done when** the [team readiness](#team-readiness) checks pass on the test repository.

## Development before the cutover

Until the cutover, the Python service stays deployed for the real targets and the task
application runs only for the test repository. The split is static: the test repository
is in the task application's targets and not in the Python service's, and each ignores
events for repositories it doesn't serve. Both use the same GitHub App, ingress stream
and exchange. Nothing decides an owner per issue. The backoffice API runs beside the
Python backoffice on the test repository, through a frontend build that calls it.

## Cutover

A short maintenance window, after M3. There's no live task import and no rollback: a
fault after the cutover is fixed forward.

1. **Prepare.** The release passes M3, and this runbook has been rehearsed once on the
   test deployment with a task left mid-stage and an unsettled write. Its settings name
   every target, with the same upstream, fork, launcher and reviewer as the Python
   service's.
2. **Quiesce Python while it still runs.** Announce the window and ask the team to make
   no commands during it, so it stays quiet. The Python service has
   no admission pause (`service/control.py:39-48`), so it keeps handling commands while
   the operator,
   using its `status` command, finishes or `/concurro stop`s every task not in a
   terminal stage and waits for each stop to apply. Every write of unknown outcome is
   settled with `resolve`, which makes its task due again (`service/control.py:87`), so
   the operator waits until the task is quiet. Recorded sponsorship before enrollment
   (`issue_sponsor_sources`) is listed, so its sponsors can be told to sponsor again; it
   isn't imported.
3. **Stop the Python daemon,** its only shutdown path, which stops its consumer,
   reconciler, scheduler and control socket together. A stop starts a drain in which a
   running tick still finishes (`service/scheduler.py:94-100`), so `A`, the activation
   time, is set only once the daemon's exit is confirmed. Record `A`, the stream's
   incarnation and `S`, the last sequence its consumer acknowledged, from its receipt
   log (`service/bus.py`). Put the backoffice's command pages in maintenance.
4. **Verify the final state** from the stopped registry, opened read-only with
   `Registry.inspect` (`service/registry.py:356-360`), since the control socket is gone:
   every task terminal or stopped, and no write of unknown outcome. The exchange has no
   queued or running turn and no live session. Keep the registry and diagnostics files
   read-only, as the record of earlier tasks, and the service's settings.
5. **Settle the boundary.** List the `/concurro` comments, `concurro` label events and
   review requests dated within the announced window and strictly before `A`; a quiet
   window leaves none. The registry keeps only each issue's latest outcome and command
   cursor, and a refused `start` advances the cursor too (`engine/commands.py:175`), so
   it can't say what happened to each of two commands on one issue. Each listed source is
   checked against evidence specific to it: Python's reply to that comment, the writes
   it caused (`mutations`, `service/registry.py:97-100`), and the issue's dashboard and
   labels, and records such as `approval.comment` (`engine/commands.py:227-238`). One
   with positive evidence that Python missed it, such as a `start` on an issue with no
   registry row, is reissued by its author, whom the operator asks. One whose outcome
   stays ambiguous is left to its author's explicit fresh decision, told that it may
   have acted, and the task application takes no automatic action on it. None is
   applied implicitly. Sources at or after `A` are the
   task application's and are never reissued. The rehearsal includes two commands on
   the same issue in the window.
6. **Export the exchange's floors and Python's index.** For every issue the Python
   service used, the exchange's generation (`funding_generations`) and each provider's
   slot epoch are exported into the task application. A new task on such an issue starts
   at the next generation, and its first slot transition expects the recorded epoch and
   sets the recorded epoch plus one, with payer and consent from the new boundary only
   (`exchange.py:503-512`, `:764-773`). The PR numbers in Python's list index are
   exported too, and seed the first index build, since task-branch discovery searches
   only the 100 most recently updated PRs (`service/github_reads.py:298`).
   Run `python3 deploy/export_python_cutover.py EXCHANGE_DB LIST_INDEX_DB REGISTRY_DB
   MANIFEST --activation-at A --source-incarnation INCARNATION --source-sequence S`
   against the stopped databases, then `concurro-tasks imports cutover MANIFEST`
   before starting the service. The import is restartable and rejects changed
   floors or historical identities on replay. Empty floor and PR sets are valid.
7. **Import historical archives.** Run the [archive import](../contracts.md#python-archive-import)
   against the stopped Python service's diagnostics and retained session directories.
   The import command runs only the archive uploader with the task database and
   archive store credentials. Keep the controller, ingress consumer and other workers
   stopped through this step. Resume the import after interruption and requeue any
   exhausted uploads after repair. Verify each original gzip in the archive bucket, search its
   indexed text where present, and reconcile turn, harness session, runner and
   unmatched source references with the inventory. Preserve both the requested turn
   number and the journal's last completed turn, even when a failed turn returns
   its predecessor's journal. Distinguish inherited staffing from serving assignments.
   Imported
   rows reach the backoffice through its baseline and emit no live turn events.
   An empty inventory completes with zero files and references.
   Create the manifest with `deploy/export_python_archive.py` and queue it with
   `concurro-tasks imports python MANIFEST`.
8. **Move credentials and listeners.** Move the exchange's operator listener to the
   platform network and rotate the operator token so only the task application holds
   it. With Python stopped, transfer the production reviewer grant and its refresh
   configuration (client ID and secret) to the task application, which refreshes it from
   then on.
9. **Start the task application** as the sole controller. Its durable consumer starts at
   or before stream sequence `S + 1`; the source rule below decides what applies.
   Ingress's delivery recovery client points at it. With the import complete, refresh
   the backoffice API's [baseline](../contracts.md#target-index-and-baseline) even
   if its read model is populated and its consumer is caught up. Once the snapshot
   and buffered events have been applied, switch the frontend to the backoffice API
   for task views and the Python command POST to forwarding. The Python task pages
   and delegated command comments retire. In the rehearsal, pin the baseline on page
   one, then commit and deliver a newer turn revision before the final page. At the
   switch the writer must see both that revision and the imported transcript.
10. **Check.** A test issue on a real target runs intake; `consumer status` shows the
   position past `S`; a redelivery requested through ingress is answered; a writer's
   backoffice command applies as that writer; the reviewer grant refreshes and a
   standalone review is attributed to the reviewer's numeric ID. If the export holds a
   sponsorship slot, a personal-funding transition on that issue is accepted by the
   exchange; if it holds none, the export is verified empty and a first slot initializes
   normally. The epoch replacement case itself is rehearsed against a test exchange
   holding a historical slot.

**Which sources are the task application's** is decided by one rule, by the original
GitHub source's own time (a comment revision's creation or edit time, a timeline event's
creation time), the same for live delivery, remapping, replay and reconciliation: a
source dated before `A` is history, read as context and never a request; one dated at
or after `A` is the task application's. A source before `A` is also kept out of payer
selection: it's shown and read as requirements, but no consent fold, fill or boundary
uses it, so a Python-era sponsor comment never makes its author pay for a new task.
Stream positions don't decide it, since Python
reads comments directly (`engine/controller.py:2411`), the outbox forwarder can publish
out of receipt order, and the message at `S` may be a coverage event; `S` only says
where the consumer starts. The runbook is rehearsed with a delayed first delivery of a
comment Python already acted on (ignored), a command posted during the drain and one
posted after `A` but before the task application starts (each acts once), and a coverage
event at the checkpoint.

Retired at the cutover: the Python service (controller, reconciler, scheduler, list
index, delivery recovery, lists and actions APIs) and the Python backoffice's task
pages and delegated commands. Still running and called: the exchange, gateway, vault
and runner, and the Python backoffice for sign-in, credentials and command proofs until
phase 4.

## Team readiness

Verified by the operator alone, with distinct test GitHub identities for the roles: two
writers, a reader of the target, a user with no access, and a sponsor with personal
credentials. No teammate takes part. Each check confirms the visible outcome on GitHub
and in the backoffice, and that the task's records name the right author, actor and
payer:

- **Shared work:** writer A starts a task; writer B approves its plan and later stops it;
  the outcomes name B, and A remains the initiator.
- **Refusals:** the reader's and the outsider's commands, from a comment and from the
  backoffice, are refused with a reply naming the reason, and leave the task's state and
  revision unchanged. The credentials the backoffice API receives can't issue a
  command, through the Python backoffice or the task application; a forwarded request
  that doesn't match its signature, or reuses one for another command, `withdraw`
  included, is refused. A command whose response is lost, retried with the same action
  ID after its signature expired, returns its original outcome and applies once.
- **Viewer scope:** the reader sees the target's task summaries and only their own
  consent, and no prompt, transcript or workspace change; so does a writer who can't see
  the fork; the outsider sees no task of a target they can't see; a writer sees every
  sponsorship and the run diagnostics. A former writer whose sponsorship was replaced,
  and a sponsor on an issue never started, still find it in their sponsorships. An empty
  backoffice database, rebuilt while tasks change, shows an inactive PR, a run, a
  completed turn's prompt and a replaced sponsorship older than the stream's
  retention.
- **Payers:** the initiator's credentials fill a provider at `start`, and one added after
  doesn't; the sponsor's `/concurro sponsor` makes them the payer; their `withdraw` stops
  new spend, also while a vault read for a fill is outstanding, and a former writer can
  still withdraw; an org-era label processed after the target moved to personal funding
  issues no grant until someone sponsors; resuming a task pinned to an older
  configuration changes no funding policy; on an issue with a Python-era sponsor, a new
  task started by someone else isn't paid by that sponsor (on the test deployment always;
  on a real target only where such a sponsor comment exists, as with the reused-slot
  check).
- **Enrollment and reviews:** a writer's label enrolls an autonomous task and an
  outsider's doesn't; requesting Concurro's review on a PR runs a standalone review.
- **Operator:** a recovery command is recorded as the operator's and changes no request,
  consent or approval.
- **Recovery,** each without duplicate work or spend and explainable from the views: the
  task application restarted during a turn, during a GitHub write and between a submit
  and its reply; a lost reply from the exchange and from GitHub; a runner that never
  starts, and one that fails mid-turn, recovered with `retry`; a missed webhook,
  recovered from a coverage gap; a task blocked by its budget, recovered with `resume`
  after raising it.

There's no required number of runs or soak period: readiness is these checks holding on
the release the team will use.

## M4, owner use

**Outcome:** the operator uses the task application on real targets and fixes what they
find. **Done when** representative tasks are complete on real targets and the team
readiness checks pass again on a real target; a check that needs pre-cutover history,
such as a Python-era sponsor or a historical slot, runs there only where that history
exists, and always on the test deployment. Then the team is invited.

## Risks

- **The engine's behavior is larger than its stages.** The 19,000 engine lines carry
  rules no proposal lists. Each milestone plan inventories the engine code it ports, by
  file and line, and the operator's own use in M4 is where the rest shows up.
- **A missed behavior shows up on a real task.** The cutover makes the task application
  the only controller; a defect is fixed forward. The team waits until M4.
- **Bridges carry today's shapes.** The exchange's API, payload v1 and the vault's
  metadata are kept exactly; their fixtures are captured from today's services.

## Decisions for the operator

1. **Unfinished Python tasks at the cutover.** The plan stops any task not finished and
   lets a writer start it again. No task has run since sponsorship, so this should be
   empty; if one isn't, the operator decides whether to finish it on Python first.

## Documentation

Plans describe work to be done. When a milestone is running in production, what it built
moves into the documentation (`docs/`): the task application, the backoffice API, their
rules, the operator commands and how to recover. The plan's work order, exit test and
risks go, since the tests are the exit test. When phase 3 is done, its plans are removed
and the [contracts](../contracts.md) point at that documentation.
