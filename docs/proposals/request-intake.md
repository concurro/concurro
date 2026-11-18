# Proposal: request intake

**Status: proposal, not implemented.**

Designed against the target in the [architecture review](architecture.md); build it on
the new orchestrator, not the current Python service.

Give every way of asking Concurro to act (a `/concurro` comment, the backoffice, a label,
an assignment, a review request, a [mention](mentions.md), later Slack) one shared path:
each **channel** authenticates the actor and sends a typed **request** to the
orchestrator, which owns every task, including who has consented to pay for it. GitHub
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
- Applying a request raises events, and those events drive every GitHub update
  (dashboard, notices, labels, artifacts, replies). No channel posts to GitHub to carry
  a request.

## Non-goals

- Changing what any command does, or who may use it.
- Removing the `/concurro` grammar or the label.

## Design

### Requests

The orchestrator keeps a `requests` table with these fields:

| Field | Meaning |
| ---- | ---- |
| `sequence` | Arrival order, assigned on insert |
| `task` | Task key; for enrollment, the issue or PR to enroll |
| `kind`, `argument` | A command kind (`commands.CommandKind`, sponsorship's `sponsor`, `withdraw` and `funding` included), `enroll` / `unenroll`, or conversation |
| `actor` | GitHub user ID and login, as the channel verified them |
| `channel` | `comment`, `backoffice`, `label`, `assignment`, `review_request`, `mention`, ... |
| `source`, `index` | The channel's reference (comment ID and revision digest, delivery ID, backoffice request ID) and the request's position among those its source produced |
| `source_at` | When the actor acted, by the channel's own clock (comment time, event time) |
| `evidence` | How the actor was verified: webhook delivery, user token check, the editor of an edited comment |
| `detail` | Channel data that must be frozen with the request, such as a mention's interpretation |
| `reply` | Where the channel wants the outcome delivered |
| `status`, `reason` | `pending`, `applied`, `refused` or `superseded` |

`(channel, source, index)` is unique, so redelivery, reconciliation and client retries
never create a second request.

### Sources

One comment or mention can produce several requests: "plan and implement this" is an
`enroll` and a `start`. The orchestrator's channels record each source revision in two
steps:

1. **Pending,** on receipt: its ID and digest, author and editor, and for a mention an
   interpretation attempt ID, saved before any model call.
2. **Final,** once the source is understood (parsed, or its interpretation recovered by
   attempt ID; see [mentions](mentions.md#interpreter)): the frozen interpretation and
   the requests it produces, saved in one transaction with those requests.

The publisher sends one reply per source once every request it produced has an outcome.

### Channels

A channel does three things and nothing else:

1. **Authenticate** the actor to a GitHub identity: the typed GitHub event ingress
   published from a verified delivery, including who edited an edited comment, or the
   forwarded sign-in token for the backoffice.
2. **Translate** its input into typed requests and send them to the orchestrator's
   request API, refusing early, with a reason, what isn't a valid request or comes from
   someone the kind doesn't allow.
3. **Route the outcome** back: a comment reply, an API response, a Slack thread.

Channels never change task state; they may read the orchestrator's task context (stage,
mode, latest plan) to interpret a request. GitHub channels run in the orchestrator and
consume ingress's `github.*` events; the backoffice API calls the request API directly. A
channel that needs a model (the mention interpreter) calls it before sending the request
and keeps the result in `detail`. An early refusal or acknowledgement is itself a
`request.*` event, so the publisher posts it like any other reply.

| Channel | Input | Replaces |
| ---- | ---- | ---- |
| `comment` | `/concurro` issue and PR comments | Comment parsing inside the tick |
| `backoffice` | `POST /requests` | Delegated command comments |
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
established, its source stays unresolved, and so does the task's payer.

### Applying requests

- **One transaction.** Storing a request queues its task's work. Applying it updates the
  task, the request's `status` and the highest applied `sequence` (which replaces
  `command_id`), and writes the resulting events to the outbox, all in one transaction.
- **Time-bound commands.** Sponsorship keeps its source-order rule, and an approval binds
  the plan current at `source_at`, so a slow channel can't approve a plan the actor
  didn't see. Ties are broken by `(source_at, source, index)`, as sponsorship orders
  facts today (`engine/sponsorship.py`).
- **Consent belongs to the task.** Sponsorship requests become facts in the task's
  append-only consent log, folded in source order as `sponsorship.project` does: a
  withdrawal never restores an earlier sponsor. Each change gives the task a new consent
  revision. The orchestrator is the authority on who consented to pay; funding trusts
  its consent decisions and decides whether and how they can be charged
  ([architecture](architecture.md#services)).
- **The funding boundary depends on the task's mode,** and is frozen the first time it's
  set, as `Shadow._initialize` does:

  | Mode | Boundary |
  | ---- | ---- |
  | Manual | The `start` request |
  | Autonomous | The first enrollment (label, assignment or mention) |
  | Standalone review | The first verified review request |

  Before freezing it, the orchestrator reads the issue's events timeline up to the
  candidate's time, always, since a delivery gap can't always be detected, and finalizes
  every source before it, mentions awaiting interpretation included. Only an event that
  sets the boundary in that mode can replace the candidate: an earlier label replaces an
  autonomous task's later enrollment, but never a manual task's `start`. Missing or
  conflicting evidence leaves the boundary pending, and nothing is charged.
- **Each attempt carries a frozen consent decision.** The orchestrator's grant request
  names the attempt and the decision: task, provider slot, sponsor, funding mode, consent
  revision, boundary and a reference to the sources folded. Replaying the attempt reuses
  it; a charged retry is a new attempt with a new decision
  ([cross-cutting rules](README.md#cross-cutting-rules)).
- **Withdrawal stops spending through funding.** Applying a `withdraw` commits the new
  consent revision with a revocation job that has a stable ID and names the task,
  provider slot and sponsor; the job retries until funding acknowledges, and a retry
  gets back the recorded status. The outcome is reported in three steps: "withdrawal
  recorded" when it applies, "new grants blocked" when funding acknowledges, and
  "spending stopped" once every affected gateway binding and relay session has closed
  or expired ([architecture](architecture.md#services)).
- **Feedback.** `retry` from a comment still uses writer comments as feedback. A channel
  that collects feedback text itself (the backoffice, Slack) stores it in the request's
  `detail`; applying the request adds it to the task's requirements snapshot and raises a
  feedback event, which the notices projection shows on the issue, since other writers
  need to see what changed the task's requirements.

### Ordering

The orchestrator takes every pending request for a task and applies them by class:

| Class | Kinds | Rule |
| ---- | ---- | ---- |
| Control | `stop`, `unenroll` | Applied first, even when they arrived after other pending requests |
| Commands | Everything else that acts | Applied in `source_at` order, after control requests |
| Conversation | Questions and other replies-only requests (the [responder](mentions.md#responder)) | Answered together, after commands |

- **Control cancels what it undoes.** A control request supersedes pending requests from
  before it that it would undo: a `stop` supersedes an earlier pending `start`, `retry`
  or `resume`. Superseded requests are marked `superseded` and reported. Requests made
  after the control request still apply, so "stop" then "start again" ends started.
- **Sponsorship requests are commands.** They're applied in source order through the
  consent fold, and no control request supersedes them.
- **One reply per source.** Outcomes of the requests from one comment or mention are
  combined into one reply, with conversation answers in it, in order (see
  [Sources](#sources)). Requests from other channels get their result through their own
  route.

### Events and projections

Applying a request publishes events: the request's own outcome (`request.*`: `applied`,
`refused` or `superseded`, with actor, channel and reply route), and whatever it caused,
such as a stage or consent change (`task.*`). The publisher
projects them onto GitHub:

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
- **Recoverable.** Events are saved with the state change; a failed projection is
  retried from the saved events without re-applying the request.
- **The backoffice reads its own model,** fed by `request.*`, `task.*` and `funding.*`
  events, instead of waiting for the GitHub writes a request causes to come back as
  webhooks.

### Backoffice

The backoffice calls `POST /requests` on the orchestrator with an idempotency key and
gets back the request ID and its status. A retry with the same key returns the same
request, which removes the "may or may not have been posted" case. Outcomes are read by
request ID or from the backoffice's read model. Nothing is posted to GitHub for the
request itself: the dashboard shows the change, and a notice appears only if the change
calls for one.

### Enrollment

Label, assignment and review-request events become `enroll` and `unenroll` requests, with
their channel as provenance (`label`, `assignment`, `review_request`, `mention`).

Missed webhooks are recovered by redelivery: ingress finds failed deliveries within
GitHub's three-day retention and the publisher redelivers them, and `(channel, source,
index)` keeps them from doubling. When the gap is longer, the orchestrator reconciles
from each issue's events timeline (`issues/{n}/events`, which records who labeled,
assigned or requested a review and when, as `Controller.enrollment` reads today) and
stores the difference as `enroll` or `unenroll` requests, with the reconciliation as
evidence. If the timeline can't establish who enrolled a task and when, its funding
boundary stays pending and nothing is charged. Ingress reports such a gap as an event, and
the orchestrator reconciles comments from the gap as well
([coverage gaps](contracts.md#coverage-gaps)).

## Invariants

- **Only the orchestrator changes task state,** consent included. Channels authenticate,
  translate and reply through their own route.
- **GitHub is updated only by projections** of saved events, including a mention's
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

Intake arrives with the new orchestrator (architecture phase 3), for tasks it owns. Tasks
the Python service already owns drain there on today's comment path. Roll-forward only:

- Delegated command comments stay readable for history; the backoffice stops posting them
  for new tasks once `POST /requests` is live.
- If tasks are ever imported, an imported task records its current `command_id` as the
  comment boundary, and its `review_requests` rows and `issue_sponsor_sources` become
  requests.

## Phasing

1. **Requests, and the comment, label and backoffice channels.** Commands go through
   requests; the backoffice uses `POST /requests`; labels enroll, so the architecture
   review's first milestone exercises enrollment.
2. **Enrollment channels.** Review requests move over; assignment arrives as a new
   channel.
3. **[Mentions](mentions.md).** Exact mentions; the interpreter waits until they show
   what people ask for.
4. **Further channels**, such as Slack or conditional starts.
