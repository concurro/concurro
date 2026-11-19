# Plan: phase 3, M1, the first slice

**Status: plan, not started.** Part of [phase 3](phase-3-plan.md), whose design and
crates it uses.

M1 runs one task's intake end to end on the task application, through GitHub alone, with
org funding, on a dedicated test repository. It's an engineering milestone: the
backoffice, personal credentials and the other team workflows follow in M2 and M3,
before the [cutover](phase-3-plan.md#cutover). A writer comments
`/concurro start`; the task application records the request, initializes the task,
admits and charges the intake attempt, submits it to the Python exchange, polls it,
publishes the result on GitHub and shows it in the operator views. `stop` stops it, and
the controller can be restarted at any point without a second turn or publication.

**Done when** the [exit test](#exit-test) passes on the test repository.

## Scope

| In | Out |
| ---- | ---- |
| The comment channel; `start` and `stop`; `not_supported` replies for every other command | `approve`, `retry`, `resume`, `extend`, `invalidate` (M2); labels, review requests and the backoffice's requests (M3) |
| Org funding | Personal credentials, consent and sponsorship (M3) |
| The task, request and turn events, through the outbox | The backoffice API that consumes them (M3) |
| Initialization and the intake stage, its repairs and outcomes | Every later stage (M2) |
| Call budgets, the uncertain-attempt limit, the attempt record | Retries across credentials |
| The exchange adapter, payload v1, polling, cancel | |
| Turn content (spec, prompt, response, workspace, transcript) in the blob store | Reading it from the backoffice API (M3) |
| Publication: App and read tokens, the dashboard, notices, replies, stage labels, the run ref and dispatch, settlement | Branches, PRs, reviews and checks (M2); delivery recovery and gaps (cutover) |
| Operator views and recovery commands for what M1 does | |
| The exchange's tombstones and session-ending cancel | The operator listener move (cutover) |
| Recorded actors and operator actions | |

## The path

1. **Start.** Ingress publishes `github.issue_comment.created` with `/concurro start`.
   The comment channel records the source and one `start` request in the transaction
   that records the event's ID.
2. **Apply.** The task's decision queue applies it: the issue must be open and the
   author a writer, read with a read token. A task with no row gets one, with a UUIDv7.
   Applying it reads the target's configuration at the fork's head and pins it as the
   task's `config_sha` (`service/target_loader.py:34-36`).
3. **Initialize.** It resolves the upstream's configured base branch to the task's base
   SHA, reads the requirements and policy inputs, records them, sets stage `intake`, and
   records the dashboard, stage label and welcome notice writes.
4. **Admit.** In one transaction the intake attempt passes the call budget, its payer is
   decided (the org), it's charged a call and recorded with its inputs, and its submit
   effect is recorded.
5. **Submit.** The exchange adapter builds today's submit body, stores it, marks the
   effect `sending`, submits it, and records the reply. Its `dispatch` becomes a run ref
   and dispatch write.
6. **Poll.** The adapter polls the turn, recording each status change, until it
   finishes, then ends the session; the run ref is deleted.
7. **Result.** The intake report is validated and stored, or repaired. Its decision sets
   the next stage. The dashboard, stage label and notices are written; the operator
   views show the task, its attempts, turn and report.

## Task application

### Intake

- **Comment channel:** consumes `github.issue_comment.{created,edited,deleted}` for
  target repositories, ignoring pull requests (`engine/controller.py:2407`) and
  Concurro's own accounts. It parses `/concurro` commands with today's grammar
  (`engine/commands.py`) and records the source revision (`{comment_id}:{digest}`) and
  its requests. A comment with no command is ignored.
- **Requests** are stored and applied as
  [request intake](../request-intake.md#applying-requests) describes: unique by `(channel, source, index)`, `stop` before other commands, then by
  `(source_at, source, index)`. Writer permission is checked again at application with a
  read token and `GET collaborators/{login}/permission` (admin, maintain or write, as
  `engine/github.py:197`). Each request records its actor (numeric ID and login) and
  each outcome (`applied`, `refused`, `superseded`) names it; a refused request changes
  no task state. Each source gets one reply. Where today's service is silent (a non-writer's
  command, `engine/controller.py:1707`), M1 refuses with a reason and replies.
- **Edits** follow the [decision on edits](../contracts.md#decisions): an edited `start`
  that was already applied is refused as `already_applied`.
- **Closed issues:** applying a `start` reads the issue's current state; on a closed
  issue it's refused as `issue_closed` and nothing is initialized. `github.issues.closed`
  stops a task as today (`engine/controller.py:2327-2331`).
- **Not supported yet:** every other command (`approve`, `retry`, `resume`, `extend`,
  `invalidate` until M2; `sponsor`, `withdraw`, `funding` until M3) is recorded and
  refused as `not_supported`, so its author gets a reply instead of silence. M1 runs only
  on the test repository, so no team member meets this.
- **The consumer** is a durable JetStream consumer on ingress's stream, recording each
  event's ID in the transaction that applies it and acknowledging after commit, as phase
  1's events crate does. Until the cutover it starts from the stream's first message and
  ignores repositories that aren't its targets.

### Stages

M1 ports the stages up to the end of intake:

- **Initialization** (`engine/controller.py:938-1009`) works from two pinned SHAs. The
  fork's `config_sha` holds `.concurro/config.*`, `.concurro/charter.json` and the
  instruction overrides (`service/target_loader.py:53-73`), which with the task
  application's pinned engine release make the task's bundle and its identity
  (`engine/bundle.py:73`). The upstream base SHA, resolved from the configured base
  branch (`:940`), holds the policy documents and `.github/instructions` (`:587-589`).
- **The requirements snapshot** (`:926-936`) is the issue and the comments of its author
  or a writer, without commands, Concurro's comments and accounts, keeping the last
  twenty, and stopping at the `start` comment (`engine/commands.py:216`).
- **Intake** (`quality_control`, `:1454-1474`): the `triage` role's turn with
  `quality_context` (`:1407-1452`): `context()` (`:579-622`), policy documents and
  `.github/instructions`, and the editable files of the base tree. Every input is read
  with a read token when the decision is made and recorded with the attempt.
- **Its result** (`apply_quality_report`, `:1526-1590`): validated as `validate_qc`
  (`engine/policy.py:159`) against the task's charter, or repaired at most twice
  (`repair_intake`, `:1727-1752`), each repair a new charged attempt.
  `finish_quality_control` (`:1592-1612`) decides: `proceed` → `plan`, `clarify` →
  `clarify` with its "Input needed" notice, `reject` → `rejected`, `escalate` or high
  risk → `escalated`; concerns add the "Intake approved with concerns" notice
  (`engine/conversation.py:508-532`). The accepted report is stored with the task.
- **Waiting for M2.** A task that reaches `plan` records `stage_reason:
  awaiting_release` and runs nothing more. M2's release enqueues a decision for each
  waiting task.
- **`stop`** sets `stopped` and cancels every open attempt in the same decision, so no
  later effect or result of theirs acts. Each turn that may be live at the exchange gets
  a cancel, whatever its attempt's outcome: a submit sent or in flight, a lost reply, an
  attempt made `unknown` by a polling outage. A turn counts as live from its submit until
  a poll reports it finished or a cancel is acknowledged. The cancel tombstones a turn
  not yet admitted and ends every session allocated to it, so a session a lost reply
  would have named ends too; each ended session's run ref is deleted. Closing the issue
  cancels live turns the same way. A later `start` applies as today.

### Attempts

- **Admission is one transaction:** the call budget (`max_model_calls` from the task's
  configuration) is checked, the payer decided, the call charged and the attempt
  recorded with its ID, inputs, limits and submit effect. Today a call is charged once a
  credential is selected (`engine/controller.py:682-700`, `:772-779`); with org funding
  that's at admission. An exhausted budget sets `blocked` with today's reason and
  submits nothing.
- **Uncertainty:** an attempt whose outcome is `unknown` counts towards today's limit of
  two per operation (`MAX_UNCERTAIN`, `:84`), after which the task is `blocked`.
- **Replay:** after any crash, an attempt reuses everything recorded: its submit body and
  any reply. A new attempt is a new ID, and the attempt ID is the turn ID.
- **Only current attempts act.** A task has a current attempt per operation, or per
  reviewer seat where reviewers run in parallel (plan QC today, `engine/controller.py:1454`;
  M2's reviews). Effects check their attempt before acting, and every result names its
  attempt; a result for an attempt that isn't current for its operation or seat, such as
  one `stop` cancelled, is recorded and changes nothing. `stop` cancels every live
  attempt of the task. M1 has one operation at a time; the rule is built for M2's
  parallel ones.

### Exchange adapter

Today's operator API (`gateway/exchange/exchange.py:1580`), through
`concurro-exchange-v1`, with the operator token. Until the cutover it's reached through
the edge's `/exchange/*` route, as the Python service reaches it.

- **Submit** is `POST /exchange/operator/turns` with `{turn, expected, models, budget,
  lease, payload, pin, binding}`. `expected` holds the run's OIDC claims, built from the
  target's launcher as `service/github_reads.py:88-115` builds them. The payload is
  [payload v1](../contracts.md#turn-payload-v1), built as `Agents.prepare` does
  (`engine/agents.py:130-204`), with the runners' gateway URL, headers and optional Codex
  WebSocket URL from the task application's settings (`service/worker.py:203-207`). The
  binding is today's org binding with generation 1: M1's test issues are new, so the
  exchange holds no generation for them. An issue the Python service used starts from
  the generation exported at the [cutover](phase-3-plan.md#cutover).
- **A resubmit** of the stored body returns the existing turn (`exchange.py:1168-1180`),
  so a replay never queues a second one. After the exchange prunes it, the tombstone
  answers `410 turn_expired`, and the attempt is `unknown`.
- **`dispatch`** can come in a submit reply or a poll (a queued turn that lost its session,
  `exchange.py:1416`); either becomes one run ref and dispatch write per session.
- **Polls** wait at most 50 seconds (`exchange.py:368`). Ending a session is `POST
  sessions/{id}/end`.
- **Deadlines:** each attempt records its sessions and its deadline, the turn's timeout
  plus 30 minutes' queueing (`engine/agents.py:42`, `:436`). With
  `turn_timeout_seconds: null` (`engine/controller.py:225-227`) the deadline covers only
  queueing and is cleared once a poll reports the turn `running` (`:477-478`). Past the
  deadline the adapter cancels the turn and the attempt is `unknown` (`:516-519`). After
  more than 20 consecutive failed polls (`:49`) the attempt is `unknown` too, but the
  turn isn't cancelled, since a polling outage is no evidence the runner stopped
  (`:539-542`); it stays live for `stop`.
- **Turn content:** the submit spec and the prompt, and the response, workspace and
  transcript of a finished turn, are written to the [blob store](../contracts.md#blobs)
  before the turn's event references them; a write is create-only, so a replay repeats
  it harmlessly. The prompt is reconstructed as today (`prompt(request, feedback)`,
  `service/diagnostics.py:145-150`), with the instructions, repository snapshot, schema
  and untrusted context the backoffice shows (`backoffice/turns.py:135`).
- **Every reply,** whatever its status, is checked for `provider_failure` and `refusal`
  and acted on at once (`engine/agents.py:485-498`); a `budget_denied` failure pauses the
  task as today.

### Publication

- **Tokens:** installation tokens per repository with the permissions of
  `service/targets.py:53-76`, checked and revoked on a mismatch as
  `service/identity.py:134` does, cached until 10 minutes before expiry. Read tokens are
  the same, limited to read permissions.
- **Surfaces:** the dashboard, notices, replies, stage labels, the run ref and the
  dispatch, admitted as [write admission](../contracts.md#write-admission) lists them.
- **Rendering:** the dashboard from the task's state as `Dashboard.summary_body` renders
  it (`engine/dashboard.py:101-167`), with today's marker (`engine/checkpoints.py:20`)
  and no checkpoint pointer, since new tasks have no audit artifact; notices with
  today's texts and event markers (`engine/protocol.py:17`); replies from request
  outcomes.
- **One write per surface at a time,** newest snapshot first. A surface's next write
  waits until the previous one is settled, and a newer snapshot replaces an unsent older
  one; a reply is never skipped.
- **Write records:** each write is recorded, then marked `sending` just before its request
  goes out, then settled. A write never marked `sending` is sent after a crash. One
  marked `sending` with no response recorded is uncertain and settled by reading back.
  A readback that fails is retried with backoff, across restarts; one answered
  `forbidden` or `not_found` settles the write as `failed`.
- **Labels and refs** are settled by what the readback shows, and a write found absent is
  sent again: adding a label is idempotent, and creating a ref that exists is refused.
- **Comment creates** stay pending until their marker is seen, since a create can still be
  in flight when the readback finds nothing. Each carries a marker unique to that write.
  A pending create is read again with backoff and never resent on its own; only the
  operator's `writes resolve` settles it. A late create that carries the marker is
  adopted as written whenever it's found. The Python service
  likewise watches for its exact comment after an uncertain command post
  (`service/actions.py:162-172`).
- **Dispatches** are never resent, since a run can be accepted before it's listed. A
  dispatch whose run isn't listed yet is read again with backoff. If its run never
  appears, the exchange gives the queued turn a new session and the next poll a new
  `dispatch` (`exchange.py:1416`).
- **Sessions end in one place.** A session that ends, is replaced by the exchange or
  whose turn finished is recorded as ended before anything else happens for it. A
  dispatch or ref creation for an ended session is settled as `skipped` without a write,
  including a delayed job that resumes after the ref was deleted.
- **The run ref** is created at the task's `config_sha` and refused if it exists at
  another SHA, and deleted when its session ends, as `service/gateway.py:284-312` and
  `service/worker.py:268-300` do.

### Events

Through phase 1's outbox and the bus, in the transaction that makes each change, for the
backoffice API M3 builds ([contracts](../contracts.md#target-catalog)): the task's state,
notice and report snapshots, request outcomes and turn records. M1 proves them with a
contract test consumer; nothing in the task application consumes them.

### Operator views and recovery

Subcommands of `concurro-tasks`, run on the host against the task database, through the
same transaction API as the service. Each action is recorded as the operator's, with the
host user, and none makes a request, consent or approval on a GitHub user's behalf:

- **`tasks list`** and **`tasks show <repository>#<issue>`:** stage and reason, requests
  and their outcomes, attempts with their outcome and whether their turn may still be
  live, sessions, the report, and every write with its state.
- **`effects pending`:** every effect `sending` without an outcome or waiting on a
  readback, with its age and what settling it needs. `recorded` means never sent and safe
  to retry; `sending` means it may have happened.
- **`writes resolve <write> --written <url> | --failed | --resend`:** settles a pending
  comment create from what the operator saw on GitHub. `--written` adopts the comment.
  `--failed` stops waiting and sends nothing; a late create is still adopted when its
  marker appears. `--resend` is an override, recorded as such, that sends the create
  again and can duplicate the comment if the first one lands late.
- **`attempts cancel <attempt>`:** cancels a live turn at the exchange, as `stop` does for
  one attempt.
- **`consumer status`:** the stream sequence applied and any coverage gap ingress
  reported.

## Configuration

Settings files, read at start, as phases 1 and 2 do: targets (upstream, fork, issues,
launcher, reviewer), the engine release, per-attempt token limit and lease, the
exchange's operator URL and token, what payload v1 gives runners (the public gateway URL,
headers and the optional Codex WebSocket URL), the App ID and private key file, the
database URL, the NATS credentials and the blob store's credentials. In M1 the targets
are bootstrap settings; the architecture puts mutable target settings and funding policy
in the owning service's database behind an authenticated API
([migration](../architecture.md#migration)), which isn't part of M1.

## Exchange changes

The [bridge](../contracts.md#python-exchange)'s tombstones and session-ending cancel,
with their tests. Both are compatible with the Python service, which keeps running
meanwhile: it never resubmits a pruned turn, and its cancel gains the ended sessions in
the reply, which it ignores.

## Exit test

On the test repository, against today's runner and the Python exchange with its changes.
Each case that runs a task from the start uses its own fresh issue.

1. **The path.** `/concurro start` by a writer: one task, one intake turn under org
   funding, a dashboard, a stage label, the welcome notice and the intake outcome's
   notices on GitHub, and the task, attempt, turn and report in `tasks show`.
2. **Intake outcomes.** Captured reports for `proceed`, `clarify`, `reject` and
   `escalate` set their stages and notices; an invalid report is repaired twice, each
   repair charged, then fails as today.
3. **Requests.** A redelivered comment creates no second request; an edited `start` is
   refused as `already_applied`; `stop` supersedes a pending `start`; a non-writer's
   `start` is refused with a reply; a writer who loses access before application is
   refused; a `start` on a closed issue is refused and initializes nothing; `approve`
   and `sponsor` get `not_supported` replies. Every refusal leaves the task's state and
   revision unchanged, and its outcome names the actor; a `stop` by a second writer
   names that writer, not the initiator.
4. **Restart.** The controller is killed after admission, after a submit is `sending`
   and before its reply is recorded, during a poll, after the result before its writes,
   and between a write's `sending` and its response. After each restart: one exchange
   turn, one dispatch per session, one dashboard comment, one of each notice, and
   `effects pending` shows what was uncertain until it settled.
5. **Stop.** `stop` during the intake turn cancels it, ends the session and deletes the run
   ref. A `stop` before the submit, after a submit whose reply was lost, and after a
   polling outage leaves no turn or session running and no run ref; a late intake result
   changes nothing.
6. **Uncertain writes.** For each surface, a write whose response is lost is settled by
   reading back and never written twice. A readback that reaches GitHub before a comment
   create completes leaves it pending until the comment appears, once. A dispatch whose
   run is listed late isn't resent; a delayed dispatch job for an ended session creates
   no ref and no run. A write answered `5xx` is settled like a lost one.
7. **Polling.** A turn whose launcher never starts ends at its deadline as `unknown` and is
   cancelled. With `turn_timeout_seconds: null`, an unstarted turn ends at the queueing
   deadline and a running one runs on. Through a polling outage the attempt becomes
   `unknown` and the turn isn't cancelled. A running turn that spends its token budget
   pauses the task on the first reply that says so.
8. **Budgets.** At `max_model_calls`, nothing is submitted and the task is `blocked`. Two
   `unknown` attempts block the task.
9. **Inputs.** The submit body matches, field by field, the payload v1 body the Python
   service built for the same issue at the same SHAs, with the runners' gateway settings
   from the task application. Different policy documents at the fork's `config_sha` and
   the upstream base: intake reads the base's. The requirements include the issue
   author's comments, though they aren't a writer, and leave out a writer's reply after
   the `start`.
10. **Tombstones.** After the exchange prunes a finished turn, resubmitting its stored body
    gets `410 turn_expired` and the attempt is `unknown`, also after an exchange restart;
    a changed body gets `409`; a turn cancelled before submission can't be submitted.
11. **Fixtures and events.** Every GitHub read and write the task application makes
    matches a captured fixture; every event it publishes is schema-valid and reaches the
    test consumer once, across a restart and a duplicate delivery. Every blob a turn
    event references is in the store with its digest, and a captured turn's prompt blob
    matches the prompt the Python service's diagnostics record for the same turn.
12. **Separation.** Events for the real targets are ignored by the task application, and
    events for the test repository by the Python service.
13. **Operator.** `attempts cancel` and `writes resolve` are recorded as the operator's
    and change no request, consent or approval. After `--failed`, a first create that
    lands late is adopted as written, and no second comment is sent.

## Work order

1. **Spikes:** pick the object store server, and write and read a create-only blob
   through `concurro-blobs`' features; sign an RS256 App JWT; record GitHub fixtures for every call M1 makes,
   from the test repository; record the exchange's submit, poll and cancel bodies and a
   baseline intake payload from the Python service.
2. **Crates:** `concurro-github`, `concurro-blobs`, `concurro-exchange-v1`, and the task
   application's three crates with the effect records and job runner.
3. **Intake and requests,** then initialization and the intake stage, then attempts, the
   exchange adapter and polling.
4. **Publication:** tokens, admission, rendering, write records and settlement.
5. **Operator views and recovery commands.**
6. **Exchange changes,** deployed while the Python service keeps running.
7. **Platform:** object storage with a bucket and credentials for the task application;
   the `tasks` database and role, a NATS user and durable consumer, the
   service in a compose project on the platform network; the test repository in its
   targets only.
8. **The exit test.**

Steps 3 and 4 can be built in parallel once step 2 is done.

## Risks

- **Intake's inputs are wider than they look.** `context()` and `quality_context` read
  policy files, instructions, the base tree and prior outcomes; missing one changes the
  turn. Exit case 9's field-by-field comparison is the check.
- **The dashboard is rendered by new code.** The renderer gets its own fixtures and is
  allowed to differ where new tasks lack audit artifacts.
