# Proposal: request intake

**Status: proposal, not implemented.**

Designed against the target in the [architecture review](architecture.md); build it as
the intake module of phase 3's
[task application](architecture/phase-3-plan.md#the-task-application), not in the
current Python service.

Give every way of asking Concurro to act (a `/concurro` comment, the backoffice, a label,
an assignment, a review request, a [mention](mentions.md), later Slack) one shared path:
each **channel** authenticates the actor and records a typed **request** for the task
application, which owns every task, including who has consented to pay for it. GitHub
comments become one channel among several instead of the transport every command has to
pass through.

## Today

- **Comments are the command log.** Each tick reads every issue comment
  (`Controller.step`), substitutes verified delegated comments
  (`delegation.effective`), and applies `/concurro` commands above the task's
  `command_id` high-water mark, re-checking the writer (`Controller.commands`).
- **The backoffice goes through GitHub.** `service/actions.py` verifies the writer, then
  posts an App comment with a signed marker naming the writer and command
  (`engine/delegation.py`). The backoffice learns only whether the comment was posted, and
  a lost response leaves it unsure (`backoffice/commands.py:UNKNOWN`).
- **Some inputs already live in the registry.** Review requests are committed with their
  delivery receipt to `review_requests` and are never reconstructed from PR history.
  Sponsorship claims are tracked by source comment in `issue_sponsor_sources`.
- **Enrollment is re-derived from GitHub.** `Controller.enrollment` reads the issue's
  label events on each tick.

## Goals

- One request model shared by every channel.
- Channels independent of each other: adding Slack or a mention interpreter doesn't touch
  the comment path or task logic.
- The backoffice submits requests directly, gets a request ID and can follow its outcome.
- Applying a request records the GitHub writes it causes (dashboard, notices, labels,
  artifacts, replies), and those records drive every GitHub update. No channel posts to
  GitHub to carry a request.

## Non-goals

- Changing what any command does, or who may use it.
- Removing the `/concurro` grammar or the label.

## Design

### Requests

The task application keeps a `requests` table with these fields:

| Field | Meaning |
| ---- | ---- |
| `sequence` | Arrival order, assigned on insert |
| `task` | Task key; for enrollment, the issue or PR to enroll |
| `kind`, `argument` | A command kind (`commands.CommandKind`, sponsorship's `sponsor`, `withdraw` and `funding` included), `enroll` / `unenroll`, or conversation |
| `actor` | GitHub user ID and login, as the channel verified them |
| `channel` | `comment`, `backoffice`, `label`, `assignment`, `review_request`, `mention`, ... |
| `source`, `index` | The channel's reference (a comment webhook uses comment ID, delivery ID and body digest; other channels use their event or action ID) and the request's position among those its source produced |
| `source_at` | When the actor acted, by the channel's own clock (comment time, event time) |
| `evidence` | How the actor was verified: webhook delivery, a [command proof](contracts.md#command-proofs) for the backoffice, the editor of an edited comment |
| `detail` | Channel data that must be frozen with the request, such as a mention's interpretation |
| `reply` | Where the channel wants the outcome delivered |
| `status`, `reason` | `pending`, `applied`, `refused` or `superseded` |

`(channel, source, index)` is unique, so redelivery, reconciliation and client retries
never create a second request.

### Sources

One comment or mention can produce several requests: "plan and implement this" is an
`enroll` and a `start`. The channels record each source revision in two steps:

1. **Pending,** on receipt: its ID and digest, author and editor, and for a mention an
   interpretation attempt ID, saved before any model call.
2. **Final,** once the source is understood (parsed, or its interpretation recovered by
   attempt ID; see [mentions](mentions.md#interpreter)): the frozen interpretation and
   the requests it produces, saved in one transaction with those requests.

Publication sends one reply per source once every request it produced has an outcome.

### Channels

A channel does three things and nothing else:

1. **Authenticate** the actor to a GitHub identity: the typed GitHub event ingress
   published from a verified delivery, including who edited an edited comment, or, for
   the backoffice, a command proof the Python backoffice signed with the session's
   GitHub token in hand.
2. **Translate** its input into typed requests and record them, refusing early, with a
   reason, what isn't a valid request or comes from someone the kind doesn't allow.
3. **Route the outcome** back: a comment reply, an API response, a Slack thread.

Channels never change task state; they may read the task's context (stage, mode, latest
plan) to interpret a request. GitHub channels run in the task application's intake module
and consume ingress's `github.*` events, recording each source and its requests in the
transaction that records the event, so there's no request API between a channel and the
task logic. The backoffice channel is the Python backoffice through phase 3: it takes the
browser's command POST with its CSRF check, signs a command proof for the signed-in user
and that exact request, and calls the task application's `POST /v1/requests` itself; the
task application takes the actor from the proof and authorizes it at application, like
any other request. The backoffice API, a separate lower-privilege process, never
receives the session cookie (the edge strips it) and holds only a read-only viewer token,
so it has no command path. Host-only operator
commands are recorded with actor `operator`, never as a GitHub user. A channel that needs
a model (the mention interpreter) calls it before recording the request and keeps the
result in `detail`. An early refusal or acknowledgement is recorded as the request's
outcome, so publication replies to it like any other.

| Channel | Input | Replaces |
| ---- | ---- | ---- |
| `comment` | `/concurro` issue and PR comments | Comment parsing inside the tick |
| `backoffice` | The Python backoffice's `POST /requests`, forwarding a browser command it signed | Delegated command comments |
| `review_request` | `review_requested` / `review_request_removed` | The `review_requests` table |
| `label` | `labeled` / `unlabeled` for `concurro` | `Controller.enrollment` event reads |
| `assignment` | `assigned` / `unassigned` for the account | New ([mentions and assignment](mentions.md)) |
| `mention` | `@account` in comments and reviews | New ([mentions and assignment](mentions.md)) |

### Authorization

Each kind keeps today's rule, checked by the channel and again at application, so a
writer whose access was removed after submitting is refused then.

| Kinds | Who may |
| ---- | ---- |
| Task commands, `enroll`, `unenroll`, `sponsor`, `funding`, conversation | A writer of the upstream (`Controller.commands`) |
| `withdraw` | Any signed-in user who can see the issue; it withdraws only their own consent (`service/actions.py`) |

An edited comment counts for `sponsor`, `withdraw`, `funding` or a paid interpretation
only when its author made the edit, as sponsorship checks editor IDs today
(`service/shadow.py`); otherwise the edit is refused and nobody is charged. Webhook
`edited` deliveries record the editor. GitHub's comment edit history (`userContentEdits`)
helps recover missed edits but isn't proof: its editor and diff can be missing, and
revision text can be deleted. When an edit that would decide consent can't be
established, its source stays unresolved: a funding boundary still pending stays
pending, and consent already applied stands.

### Applying requests

- **One transaction.** Storing a request queues its task's work. Applying it updates the
  task, the request's `status` and the highest applied `sequence` (which replaces
  `command_id`), and records the writes and effects it causes, all in one transaction.
- **Time-bound commands.** Sponsorship keeps its source-order rule, and an approval binds
  the plan current at `source_at`, so a slow channel can't approve a plan the actor
  didn't see. The M1 comment decision queue processes `stop` first, then orders by
  `(source_at, numeric comment ID, C-collated source, index)`. Repeated deliveries of
  one comment content revision share a source; deletion is terminal, and competing
  edits at one timestamp make pending commands ambiguous. Sponsorship retains its
  source-order fold (`engine/sponsorship.py`).
- **Consent belongs to the task.** Sponsorship requests become facts in the task's
  append-only consent log, folded in source order as `sponsorship.project` does: a
  withdrawal never restores an earlier sponsor. Each change gives the task a new consent
  revision. The task's consent is the authority on who consented to pay; the funding
  policy decides, in the same transaction that admits an attempt, whether and how it's
  charged ([architecture](architecture.md#services)).
- **The funding boundary depends on the task's mode,** and is frozen the first time it's
  set, as `Shadow._initialize` does:

  | Mode | Boundary |
  | ---- | ---- |
  | Manual | The `start` request |
  | Autonomous | The first enrollment (label, assignment or mention) |
  | Standalone review | The first verified review request |

  Before freezing it, the task application reads the issue's events timeline up to the
  candidate's time, always, since a delivery gap can't always be detected, and finalizes
  every source before it, mentions awaiting interpretation included. Only an event that
  sets the boundary in that mode can replace the candidate: an earlier label replaces an
  autonomous task's later enrollment, but never a manual task's `start`. Missing or
  conflicting evidence leaves the boundary pending, and nothing is charged.
- **Each attempt carries a frozen consent decision.** The attempt's record names the
  decision: task, provider slot, sponsor, funding mode, consent revision, boundary and a
  reference to the sources folded. Replaying the attempt reuses it; a charged retry is a
  new attempt with a new decision ([cross-cutting rules](README.md#cross-cutting-rules)).
- **Withdrawal stops spending in one transaction.** Applying a `withdraw` commits the new
  consent revision and the cutoff for the task, provider slot and sponsor, which refuses
  any later attempt under it, and records the exchange transition that fences new
  sessions. The outcome is reported in two steps: "withdrawal recorded, new attempts
  blocked" when it applies, and "spending stopped" once every affected session has
  closed or expired ([architecture](architecture.md#services)).
- **Feedback.** `retry` from a comment still uses writer comments as feedback. A channel
  that collects feedback text itself (the backoffice, Slack) stores it in the request's
  `detail`; applying the request adds it to the task's requirements snapshot and raises a
  feedback event, which the notices projection shows on the issue, since other writers
  need to see what changed the task's requirements.

### Ordering

The task application takes every pending request for a task and applies them by class:

| Class | Kinds | Rule |
| ---- | ---- | ---- |
| Control | `stop`, `unenroll` | Applied first, even when they arrived after other pending requests |
| Commands | Everything else that acts | Applied in `source_at` order, after control requests |
| Conversation | Questions and other replies-only requests (the [responder](mentions.md#responder)) | Answered together, after commands |

- **Applied controls survive the queue.** When a control applies, commit its outcome,
  state change and greatest applied order key `(source_at, numeric source ID, source, index)` for that
  issue and control kind together. Keep the key across task restarts and new starts.
  Before applying a conflicting request, compare its key in the same serialized
  decision. A `stop` supersedes every earlier `start`, `retry` or `resume`, including
  one delivered after the stop committed. Record and report the superseded outcome
  without changing the task. A newer request still applies if eligible. A refused
  control never advances the key.
- **Applied lifecycle commands also survive the queue.** A delayed `stop` older than an
  already applied `start` is superseded. Pending controls still run before pending
  commands; the comparison applies only against a command already committed.
- **Sponsorship requests are commands.** They're applied in source order through the
  consent fold, and no control request supersedes them.
- **One reply per source.** Outcomes of the requests from one comment or mention are
  combined into one reply, with conversation answers in it, in order (see
  [Sources](#sources)). Requests from other channels get their result through their own
  route.

### Events and projections

Applying a request records the request's own outcome (`applied`, `refused` or
`superseded`, with actor, channel and reply route) and whatever it caused, such as a
stage or consent change, with the GitHub writes they need. Publication writes them:

| Projection | Surface | Today |
| ---- | ---- | ---- |
| Dashboard | The issue's status comment; shows the latest request outcomes | `Checkpoints.publish_dashboard` on save |
| Notices | Issue comments that need a human ([notice kinds](../design/issue-dashboard.md#new-comments)) | `actions.transition` and its outbox |
| Stage labels | `concurro:stage:*`, `concurro:attention` | `Controller.sync_stage` |
| Artifacts | Plan and report artifacts | `Controller.checkpoint` |
| Check run | A standalone review's check | `engine/review_checks.py` |
| Comment replies | One reply per source to comment and mention requests | Command replies |

- **A request isn't a reason for a comment.** Its outcome shows on the dashboard. A
  comment appears only when the resulting state is one of the existing notice kinds, or
  as the reply to a request that was itself a comment.
- **Recoverable.** Writes are saved with the state change; a failed write is retried from
  its record without re-applying the request.
- **The backoffice API reads its own model,** fed by the task application's `request.*`
  and `task.*` events through its outbox and the bus, instead of waiting for the GitHub
  writes a request causes to come back as webhooks.

### Backoffice

Phase 3, M3, before the cutover, since backoffice commands are an existing team
workflow. Through phase 3 the browser posts a command to the Python backoffice, as
today, which checks CSRF and the user for the kind (a writer, or issue visibility for
`withdraw`) with the session's GitHub token, signs a short-lived
[command proof](contracts.md#command-proofs) bound to the user, issue, command,
arguments, `detail.feedback`, `source_at` and the browser action's ID, and calls the
task application's `POST /v1/requests` with it. The backoffice API, which only reads and
sees only a viewer token, can neither obtain a proof nor submit a request. The task
application verifies the proof, takes the actor from it, and returns the request ID and
its status. A refused request changes nothing but its own record. Each browser action
carries an ID that survives its retries, the request's idempotency key, and the Python
backoffice keeps its original `source_at`; a retry, even after the proof expired and is
signed again, is matched on the actor and the command's contents and returns the same
request ([requests](contracts.md#requests)), which removes the "may or may not have been
posted" case.
Outcomes are read by request ID. Nothing is posted to GitHub for the request itself: the
dashboard shows the change, and a notice appears only if the change calls for one.

### Enrollment

Label, assignment and review-request events become `enroll` and `unenroll` requests, with
their channel as provenance (`label`, `assignment`, `review_request`, `mention`). Labels
and review requests arrive in phase 3's M3, before the cutover; assignment and mentions
are new and follow it ([phasing](#phasing)).

Missed webhooks are recovered by redelivery: ingress finds failed deliveries within
GitHub's three-day retention and the task application's delivery recovery API
redelivers them, and `(channel, source, index)` keeps them from doubling. When the gap is
longer, the task application reconciles
from each issue's events timeline (`issues/{n}/events`, which records who labeled,
assigned or requested a review and when, as `Controller.enrollment` reads today) and
stores the difference as `enroll` or `unenroll` requests, with the reconciliation as
evidence. If the timeline can't establish who enrolled a task and when, its funding
boundary stays pending and nothing is charged. Ingress reports such a gap as an event, and
the task application reconciles comments from the gap as well
([coverage gaps](contracts.md#coverage-gaps)).

## Invariants

- **Only the task logic changes task state,** consent included. Channels authenticate,
  translate and reply through their own route.
- **GitHub is updated only by recorded writes** of saved state, including a mention's
  acknowledgement reaction and early refusals.
- **One request per `(channel, source, index)`.**
- **Each kind's authorization is checked at application,** whatever the channel checked
  earlier.
- **Requests are frozen.** A request's interpretation and evidence are stored with it and
  never recomputed on replay.
- **Every applied, refused or superseded request is recorded** and shown on the
  dashboard.
- **Approval and payment identity are unchanged.** Approvals bind a plan by `source_at`;
  sponsorship keeps its source-order and initiator rules, the funding boundary is frozen
  once set, each attempt carries a frozen consent decision, and a withdrawn consent
  revision is never granted again.
- **The `/concurro` grammar and the label keep working** without the backoffice, an
  account or a model.

## Migration

Intake arrives with the task application (architecture phase 3) and takes over every
target at its single [cutover](architecture/phase-3-plan.md#cutover). No task is
imported. Roll-forward only:

- A source becomes a request only if it's the task application's by the
  [cutover rule](contracts.md#cutover-sources): GitHub dates the original source (a
  comment revision, a timeline event) at or after the activation time `A`, set once the
  Python daemon's exit is confirmed. This holds for live delivery, re-mapping, replay and
  reconciliation alike, however late a delivery arrives. A `/concurro` comment or
  `concurro` label from before `A` is history, shown and read for requirements, never a
  request and never part of payer selection; one Python missed strictly before `A`
  (a standalone review request included) is reissued by its author, whom the operator
  asks. A source at or after `A` is never reissued.
  An issue whose Python task finished is a new task if a writer starts it again, at the
  exchange generation after the old one's.
- Delegated command comments stay readable as history. From the cutover, the Python
  backoffice forwards browser commands as signed requests to the task application's
  `POST /v1/requests`, and posts no delegated comments.

## Phasing

Every channel Concurro has today works before the cutover; only channels that are new
follow it.

1. **Requests and the comment channel,** with `start` and `stop` (phase 3, M1). Until M2
   and M3, every other command gets a `not_supported` reply on the test repository.
2. **`approve`, `retry` and `resume`** (phase 3, M2).
3. **Phase 3, M3, before the cutover:** labels and autonomous enrollment; sponsorship
   commands (`sponsor`, `withdraw`, `funding`) with personal credentials; review
   requests for standalone reviews; and the backoffice channel.
4. **After the cutover: assignment and [mentions](mentions.md).** Both are new; the Python
   service has neither, so deferring them drops no existing workflow. Until they land,
   assigning the account or mentioning it does nothing, as today. Exact mentions come
   first; the interpreter waits until they show what people ask for.
5. **Further channels**, such as Slack or conditional starts.
