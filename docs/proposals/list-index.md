# Proposal: webhook-fed list index

**Status: phase 1 implemented and not deployed; later phases are proposals.** Revised after four Codex reviews and a design
discussion, against service `main` at `cf29ba9` and frontend `main` at `6b275ec`. [Phase 0](list-index/phase-0.md)
records what was verified live and what is still open; the per-phase briefs are in
[`list-index/`](list-index/).

Keep the backoffice's list data (enrolled issues and their sponsorships, task pull requests
with CI, executor runs) current from the webhooks the service already receives, so list
content stops being read from GitHub while a page loads. The index is a persistent read
model in the CQRS sense: GitHub and the existing task machinery stay authoritative, and a
separate projection, owned by the service and eventually consistent with them, answers
list queries over the service's internal listener. The plan runs through payload-fed rows
for every list (phase 4) and then live updates (phase 5).

## Decisions

- **Scope of the copy:** the index holds the fields list views display, plus the metadata
  needed to keep those rows correct. Detail views (checkpoints, reports, review threads,
  job logs) keep reading GitHub.
- **Go through phase 4.** Phase 1 is a stepping stone, not a stopping point.
- **A persistent projection.** Rows, revisions and recovery progress live in a
  service-owned SQLite file and survive restarts. A restart resumes and repairs; it does
  not rebuild.
- **One process per database.** The service is the projection's only reader and writer;
  the backoffice asks the service.
- **Eventually consistent, with a defined way to converge.** The projection may lag. Every
  delivery GitHub recorded is either applied or covered by a rebuild, checked against
  GitHub's delivery history at the existing recovery interval.
- **Private interface.** The index is reachable only on the service's internal listener.
  Browsers talk to the backoffice API alone, which makes every Concurro-bound request.
  The list endpoints also require a shared backoffice token, because the same listener
  serves the public webhook path and one wrong edge rule would otherwise expose them.
- **No timed re-reads.** With the index enabled, a list is re-read from GitHub when a
  webhook or the service says it changed, when the index lost coverage, or when a writer
  asks. Nothing re-reads on a timer.
- **Refresh needs the service.** "Refresh from GitHub" is unavailable while the service is
  unreachable.
- **No automatic detection of silent gaps** (missing subscription, disabled webhook) for now.
- **Live updates are planned** (phase 5), starting with Concurro's own task progress; what
  else updates live is scoped when that phase starts.
- **CI means check runs.** The targets report CI through Actions and checks, so commit
  statuses stay out of the list's CI state.

## Today

- **List reads** (`backoffice/tasks.py`, `Tasks.listed`, `Tasks.target_runs`):
  - Enrolled issues: `Access.task_issues`, REST, `concurro`-labelled, open and closed, up
    to three pages of 100 by most recent update (pull requests in those pages are
    discarded), per issue repository.
  - Sponsorships: the service's `/sponsorships`, saved per user. `Actions.sponsorships`
    verifies the user with GitHub on every call; writers get `Shadow.view`, which adds
    credential coverage read from the vault to the registry's projection. A sponsored
    issue missing from the labelled listing is read by number (`Tasks.sponsored_issue`),
    and omitted when GitHub reports it moved or deleted (`gone_issue`).
  - Pull requests: `Access.pull_requests`, one GraphQL query for the upstream's 100 most
    recently updated pull requests of any kind, with line counts and up to 100 check
    contexts of the head commit. `Tasks.pulls_by_issue` keeps those whose head branch
    matches `TASK_BRANCH`, whether or not the registry knows them; per issue number an
    open one wins, otherwise the most recently updated. A REST fallback returns rows
    without counts or checks.
  - Executor runs: `Runs.for_target` (`backoffice/runs.py`), the 50 most recent
    `workflow_dispatch` runs of the fork's executor workflow (`EXECUTOR_WORKFLOW`), one
    row per run. An attributed rerun shows its first attempt, read separately and omitted
    with a message when unreadable; an unattributed run shows the listed attempt.
- **Saved lists** (`backoffice/read_cache.py`, `Tasks.listing`): each list is saved per
  repository. The first request after a saved list turns 60 seconds old (`FRESH_SECONDS`)
  gets the saved list and triggers a background refresh. `ReadCache.read` has no way to
  serve saved data without that age rule, and with the cache disabled every request reads
  GitHub. The frontend reloads while a refresh or a command's effect is pending
  (`SavedData.svelte`). A command posted through the backoffice outdates its issue
  repository's lists.
- **GitHub's guidance**
  ([best practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)):
  subscribe to webhooks instead of polling, and make requests serially instead of
  concurrently. The one-second pause it recommends applies to mutating requests. The saved
  lists are age-based polling and a target's lists are read concurrently, so both run
  against that guidance. Actual request volume has not been measured.
- **Webhooks** (`service/ingress.py`): the [deployment guide](../../gateway/README.md)
  requires subscriptions to issues, issue comments, pull requests and their reviews, check
  runs, check suites, workflow runs, statuses and pushes, and the live App
  has them ([phase 0](list-index/phase-0.md)). `Ingress.handle` runs, in order: signature, ping and event allow-list,
  delivery ID, payload and installation validation, task-key extraction (`Ingress.keys`),
  then `Registry.receive`, which records the receipt and enqueues wakeups. No payload is
  stored. A delivery that maps to no task returns 202 before the receipt, which covers
  most events sent by a Concurro identity (stage labels change this way; its marked
  command comments are admitted), display-label changes (`DISPLAY_LABELS`) and issues
  that are neither enrolled nor active. GitHub expects an answer within ten seconds.
- **Missed deliveries** (`service/webhook_recovery.py`): every 300 seconds by default,
  `Recovery.poll` asks GitHub to redeliver failed deliveries from its three-day history.
  Any 2xx or 3xx attempt counts as delivered, including 202 "ignored". A full
  `Reconciler.poll` runs only when coverage for the current scope is absent or at least
  three days old, and visits open enrolled issues, active tasks and known sponsorship
  candidates.
- **Service interface:** the backoffice calls the service's listener for `/actions` and
  `/sponsorships` (`backoffice/commands.py`) with the user's token. `Handler.do_POST`
  treats every other POST as a webhook delivery, so an unknown path answers 401 "invalid
  signature". The edge forwards only `/concurro/webhook` to that listener
  (`gateway/concurro.caddy`). The registry has an exclusive writer and the backoffice
  never opens it.
- **Permissions:** the service's readers are scoped per repository role
  (`service/targets.py` `permissions`): issues on issue repositories, pull requests and
  checks on the upstream, Actions on the fork.

## Proposed design

### Store

A new SQLite file, `list-index.sqlite3`, in the service's state directory (already mounted
for the service), opened by the service alone: one writer thread, readers on their own
connections. Its schema carries a version and moves forward by migration.

- `meta`: schema version and an `incarnation`, a random identifier set when the file is
  created, so revisions and cursors from a lost or recreated file never match.
- `rows` per kind (`issues`, `pulls`, `runs`), added by the phase that starts applying the
  kind: the displayed fields, the GitHub `updated_at` they were built from, a local
  version that rises with every applied change, and a removed marker with its timestamp
  so an older delivery cannot bring a removed item back.
- `revisions(repository, kind, counter)`: a kind's revision is the incarnation and this
  counter. Kinds are `issues`, `sponsorships`, `pulls`, `runs` and, in phase 5, `tasks`.
- `receipts(guid, delivered, outcome)`: every delivery the projection has dealt with,
  kept for longer than GitHub's three-day delivery history.
- `pending(repository, kind, item, requested)`: authoritative reads that are owed and not
  yet published.
- `repositories(id, name)`: GitHub's numeric repository IDs for the configured
  repositories, read once per repository at start; delivery-history entries name
  repositories only by that ID.
- `coverage(repository, kind, mode, state, rebuilt, verified)`: whether the service only
  reports changes for the kind (`signals`) or holds its list (`rows`); its state
  (`current`, `catching-up`, `rebuilding`, `failed`); when its last rebuild began; and up
  to when its deliveries have been verified against GitHub's history.
- `changes(sequence, repository, kind, item, changed)`: the recent changes, in order, for
  the phase 5 feed. A change without a kind is a repository-wide resynchronization.

### Applying deliveries

- **Where:** after installation validation in `Ingress.handle` and before `Ingress.keys`,
  so events from Concurro's identities and display-label changes are seen.
- **Isolation:** inside its own exception boundary the hook copies the delivery GUID and
  the few payload fields the projection needs and offers them to a bounded in-memory
  queue without waiting. The hook cannot change a delivery's status, delay it measurably,
  alter its receipt in `Registry.receive`, or change which tasks wake.
- **One transaction per delivery:** the writer thread updates the affected rows, records
  the receipt and any authoritative read the delivery makes necessary (`pending`), raises
  the kind's revision and appends the change together. A delivery that affects no row
  still gets its receipt.
- **Ordering:** GitHub may deliver out of order. A snapshot older than the stored row is
  dropped and a newer one replaces it. Equal timestamps with different content, and any
  event that cannot be ordered, require an **authoritative read** of that one item.
- **Replay:** a delivery with a receipt is skipped. Applying a row twice has no effect.
- **Queue overflow** sets a flag instead of queueing anything; the writer handles it as a
  lost local signal (below) and the receipt check finds the dropped deliveries.

### Convergence

"The webhook returned 202" does not mean the projection has the delivery: the queue can be
full, the process can stop with events queued, a transaction can fail. No durable inbox
sits in the delivery path, because GitHub's own delivery history already is one. Repair
works from it:

- **Receipt check:** `Recovery.poll` is scheduled every 300 seconds and already pages
  through the App's whole delivery history. History entries carry the event, action,
  delivery time, installation and numeric repository ID, and a redelivery keeps its GUID.
  For each delivery of an applied event on a configured repository, delivered after that
  kind's baseline and lacking a receipt, the projection repairs it: in mode `signals` from
  the history entry alone, by recording the receipt and raising the revision; in mode
  `rows` by fetching the delivery's payload from GitHub (as `Recovery.redeliver` does) and
  applying it. This is independent of the
  delivery's HTTP outcome, so it also covers deliveries the existing recovery counts as
  delivered.
- **Replay gives way to a rebuild** when a kind has more than a small number of missing
  deliveries (proposed: 20), or, in mode `rows`, when one cannot be replayed: its detail exceeds the App
  client's 1 MB response limit (`APP_LIMIT`; ingress accepts 25 MB), cannot be fetched, or
  fails to apply.
- **Baseline:** a rebuild accounts for the items it actually reconciles, as of the moment
  it began; that moment is the kind's baseline, and receipts are compared only for later
  deliveries. In mode `signals` there are no rows, so the equivalent of a rebuild is to
  set the baseline and then raise the revision, which makes the backoffice read the list
  again on its next view. The backoffice saves a list against the revision it saw before
  reading GitHub, so a delivery that lands during its read makes the list due again.
- **Pending reads** are durable. They resume after a restart, and publishing one clears it
  and updates the row, the revision and the change in one transaction. Requests for the
  same item are merged keeping the oldest request time, and a read clears only the
  requests made before it began.
- **When a kind is `current`:** its verified watermark is younger than the freshness
  allowance (initially two recovery intervals, ten minutes, to be set from phase 0's
  measurements; a warning threshold, not an expected latency), it has no failed rebuild, and
  none of its pending reads is older than that allowance. A scan that leaves no delivery
  unaccounted for advances the watermark to the time that scan started. The interval is
  when recovery is scheduled, not a deadline for it: a scan can be preceded by a full
  reconciliation and can fail, and the watermark then simply ages out of `current`.
- **At start,** a kind verified within the last three days is `catching-up`: its retained
  rows are served with that status until a scan completes. A kind never built, or not
  verified for three days or more, is `rebuilding`, because history can no longer prove
  what was missed.
- **Lost local signals:** `sponsorships`, and `tasks` in phase 5, originate in the service
  and have no delivery history. At every start, and on queue overflow, the writer raises
  those revisions and appends a repository-wide resynchronization change in one
  transaction. On that change the backoffice treats every saved list and task snapshot of
  the repository as due. From phase 3 the writer also compares the registry's sponsorship
  issues (`Registry.sponsor_issues`) with the projection's rows and records a pending read
  for each one missing. That is a local comparison, not a rebuild.

### Reads and publication

Rebuilds and authoritative item reads run outside the writer thread, one GitHub request at
a time, through the reader of the repository's role (rollups through the upstream reader).

- **A read result is a snapshot like any other.** It is applied under the ordering rule,
  so it never replaces a newer delivery. On a tie it wins only if the row's local version
  is unchanged since the read began; otherwise the item is read again.
- **Absent is not failed.** GitHub answering that an item is gone (404, 410, moved)
  produces a removed marker and does not fail a rebuild; any other error does.
- **A rebuild reconciles every row it keeps.** Rows its listings did not return, and that
  did not change during the rebuild, are read by number: issues the projection holds as
  enrolled, and retained pull requests outside the upstream's newest 100.
- **Run membership is separate from run history.** The displayed list is the runs of the
  latest listing plus runs that deliveries created since, newest 50. A rebuild replaces
  that membership with its listing plus the runs deliveries created while it ran, so a run
  deleted on GitHub leaves the list. Attempts
  already seen stay stored.
- **A rebuild is complete** when every page and item read either succeeded with full
  fields or reported absence. A REST fallback without counts, a failed listing or a
  truncated read leaves the kind `failed`, to be retried at the next recovery interval.
- **When a rebuild runs:** first use, history unable to cover a gap, too many missing
  deliveries, `Recovery.poll` falling back to full reconciliation, and an explicit refresh.
- **What a rebuild reads:**
  - Issues: every `concurro`-labelled issue, open and closed, paginated to the end, plus
    the registry's sponsorship issues by number.
  - Pull requests: the upstream's 100 most recently updated with `updatedAt`, filtered by
    `TASK_BRANCH`, plus the pull request the registry links to each task (`Registry.link`
    keeps one per issue and never replaces it) by number, each with its rollup.
  - Runs: the fork's 50 most recent executor runs, plus the first attempt of each rerun.
- **What a rebuild cannot discover:** a task pull request the projection has never seen,
  that the registry does not link and that lies outside the upstream's newest 100. That
  can happen after a history gap or a lost file, and it stays unknown until a delivery
  mentions it. Run attempts other than the first and latest are likewise not restored.

### Interface

Endpoints on the service's listener, dispatched in `Handler.do_POST` before webhook
handling and independent of the delegation key, not forwarded by the edge, called through
dedicated `ServiceCommands` methods that carry no user token and none of the
command-posting uncertainty handling:

- `POST /lists/revisions` with `{repositories}` → per repository and kind:
  `{revision, mode, state, verified}`.
- `POST /lists` with `{repository, kind}` → `{revision, state, verified, items}`, for
  kinds in mode `rows`.
- `POST /lists/refresh` with `{repository}` → raises its kinds' revisions and, in mode
  `rows`, rebuilds them.

**Credential:** each request carries a bearer token that both processes read with
`private.read` from a file named by a new optional setting, `lists_token_path`, added to
both settings loaders (which reject unknown fields today). The format is the vault service
token's: 32 bytes as hex. The service compares in constant time. Setting it is the
operator's switch from today's behaviour to the index; it does not detect the service's
version, which the per-kind `mode` reports instead.

### Backoffice behaviour

Any failure of a list call (unreachable, a 401 from an older service's webhook handling or
from a wrong token, a 5xx, a malformed answer) is "index unavailable". It is never passed
to the browser as an authentication failure.

| Situation | Behaviour |
| ---- | ---- |
| `lists_token_path` unset | Today's behaviour, including the 60-second rule. |
| Kind in mode `signals` | The saved list is fresh while its revision matches; a different revision makes it due and the background refresh reads GitHub once. A state other than `current` is shown with the list. |
| Kind in mode `rows`, `current` | Served from `/lists` and saved. The backoffice no longer reads this kind from GitHub, with or without the read cache. |
| Kind in mode `rows`, `catching-up` | Served from `/lists`, marked as catching up with its `verified` time. Until phase 5 an open page keeps that content and status until its next request. |
| Kind in mode `rows`, `rebuilding` or `failed` | Whatever `/lists` holds, else the saved copy, marked as being rebuilt or as failed, with its `verified` time. |
| Index unavailable | The saved copy with a warning that Concurro's index is unavailable. No age-based refresh. |
| Nothing to show in either of the two rows above | The list request fails with an explanation and a retry. |
| Kind the service does not report | Unsupported: the saved copy, as when the index is unavailable. Timed reads apply only with `lists_token_path` unset. |
| Explicit refresh | `/lists/refresh`; unavailable while the index is. |

`ReadCache` gains what this needs: freshness by revision instead of age, and serving saved
data without scheduling a refresh.

### List contracts

Rows the index returns must match today's lists except where marked **changed**.

- **Issues:** `concurro`-labelled issues, open and closed, never pull requests, plus the
  registry's sponsorship issues (facts, a live task or an unconfirmed hint, as
  `Registry.sponsor_issues` selects), without those GitHub reports gone. The
  task-versus-intent split stays in `Tasks.listed`. **Changed:** every enrolled issue, not
  the newest 300.
- **Pull requests:** task-branch pull requests, open, closed or merged; per issue number
  an open one wins, otherwise the most recently updated. **Changed:** a task's linked
  pull request stays listed after leaving the upstream's newest 100, as does any other
  task-branch pull request the projection has seen (subject to "What a rebuild cannot
  discover").
- **Runs:** the newest 50 executor runs, one row per run, attributed or not. An attributed
  rerun shows attempt 1 and an unattributed one its latest; other attempts the projection
  saw are kept but not shown. Attribution precedence (`served`) and the empty
  result without diagnostics are unchanged.
- **CI:** `ci()` over the check runs among the first 100 contexts of the head commit's
  rollup, as today.
- **Sponsorship content** is not indexed. It keeps coming from `/sponsorships`, per user;
  the index only says when to re-read it.

### Phase 1: change signals

- **Service:** every kind is in mode `signals`, where no payload is read, so a missing
  delivery is repaired from its history entry alone. Mapping: `issues` and `issue_comment`
  → `issues` (comments on pull requests are told apart only from phase 3); `pull_request`, `check_run`, `check_suite` →
  `pulls`; `workflow_run` → `runs`.
- **Sponsorship signals:** the registry reports the issues whose sponsorship projection or
  terminal outcome it changed, after its transaction has committed and its lock is
  released, through the same isolated queue; index code never runs inside
  `Registry.transaction`. The writers known today are `receive` and `request_observation`
  (hints), `confirm_sponsorship`, `record_shadow`, `credential_events`, `grant`, `end` and
  `recover`. Stored credential changes reach `credential_events` by polling. A credential
  expiring is not stored and signals nothing, so the vault-derived coverage in a saved
  sponsorship list can be stale until another sponsorship signal or a refresh; no list
  view displays coverage, and the pages that do read it live.
- **Backoffice:** a list request reads its repositories' revisions in one call and
  `Tasks.listing` uses them as the table above describes. The per-user sponsorship list
  follows the `sponsorships` revision.
- **Refresh action:** the target page gets "Refresh from GitHub" in an overflow (`…`)
  menu: `POST /api/targets/{owner}/{repo}/refresh`, writers only.
- **Receipts, baselines, the receipt check and the state** start here. In mode `signals`,
  applying a delivery means its receipt and the revision, and a "rebuild" means a new
  baseline and a raised revision. The backoffice shows a state other than `current` from
  this phase on.
- **Result:** a change on GitHub reaches the next view in seconds, and viewing an idle
  target reads no lists.

### Phase 2: executor runs

- Introduces mode `rows`: row rebuilds, pending reads, the read-publication rule and
  absence handling, with runs as the first kind.
- A `workflow_run` payload carries the whole run (id, attempt, status, conclusion, head
  branch, event, workflow path, timestamps); the service keeps the latest attempt and attempt
  1 of each executor `workflow_dispatch` run. A rerun that arrives without its attempt 1
  gets a pending read of exactly that attempt; an earlier attempt that happens to be known
  never stands in for it, and an attributed row is omitted with a message while attempt 1
  cannot be read, as today; an unattributed run still shows its latest attempt. A conflict is settled by reading that exact run attempt.
- `Tasks.target_runs` takes runs from `/lists` and applies the unchanged attribution
  (`Diagnostics.for_sessions`, `served`).

### Phase 3: issues

- `issues` and `issue_comment` payloads both carry the issue; the service updates its row
  from either. Only `issues.deleted` and `issues.transferred` remove it.
- A sponsorship signal for an issue without a row triggers an authoritative read of that
  issue, replacing `Tasks.sponsored_issue`.

### Phase 4: pull requests and CI

- Rows from `pull_request` deliveries on the upstream whose head branch is a task branch:
  number, title, state, draft, head ref and SHA, line counts.
- **CI is read, not assembled.** After a rerun GitHub's rollup holds only the latest
  attempt's check runs, while every attempt's remain listed ([phase 0](list-index/phase-0.md)).
  The service reads a pull request's rollup, with today's
  GraphQL projection, when the pull request is first seen, when its head changes, and
  when a `check_run` or `check_suite` delivery names the head SHA of an indexed pull
  request. The association uses the indexed head alone, not the payload's pull-request
  list (empty for forks) or the registry. A signal arriving during a rollup read schedules
  one more read after it, and a result is kept only if the head it was read for is still
  the head.
- After this phase list content is not read from GitHub while a page loads. Visibility and
  writer checks and target configuration still contact GitHub behind their existing
  caches, and `/sponsorships` verifies the user on each call it receives.

### Phase 5: live updates

- **Changes:** the `changes` table, pruned by age. A `tasks` change names its repository
  and issue.
- **Feed:** `POST /lists/changes` with `{incarnation, since}` answers with later changes,
  holding the request for up to 25 seconds (under `ServiceCommands`' 60-second timeout)
  without holding a database lock, and returning at once on shutdown. A lost response is
  repeated by asking again from the same cursor. A cursor from another incarnation, or
  older than the retained changes, gets a resynchronize answer.
- **Relay:** the backoffice keeps one such request open and relays changes to browsers
  over server-sent events, filtered to the targets each session may see. On a
  resynchronize answer, on a repository-wide resynchronization change (for that
  repository), and when the backoffice itself starts without a cursor, it treats every
  saved task snapshot and list as due and tells connected browsers to reload their views.
- **First item, task progress:** the service appends a `tasks` change after a successful
  record of:
  - a tick's outcome (`MutationGateway.release` → `Registry.end`, live generations only);
  - a turn's submission, result and observed transitions (`Store.submitted`,
    `Store.finished`, `Store.observed`: started, runner assigned, lost or cancelled),
    emitted by the store after its write, because `Recorder` swallows failures.
- **Backoffice:** a `tasks` change marks that issue's saved snapshot due without the
  command-settling behaviour of `ReadCache.invalidate`. The next read may return the
  saved snapshot marked as refreshing; the page completes through the existing reload.
- **Frontend:** the overview and an open task page reload on a change for them. A change
  that arrives while a read is in flight is kept and triggers one more read
  (`Resource.refresh` drops it today). `SavedData.svelte`'s polling stays.
- **Shutdown:** `Daemon.stop` wakes held feed requests and stops the index's threads
  before it closes credentials and the registry.
- What else subscribes (lists from GitHub events, run status) is decided at the start of
  this phase.

### Shared code

The list queries in `backoffice/github.py` (`task_issues`, `pull_requests`) and the runs
listing in `backoffice/runs.py` move to `service/github_reads.py`, extended with the
pagination and fields builds need. The backoffice keeps calling them for kinds in mode
`signals` and with the index disabled.

## Invariants

- **Access is decided as today.** The index holds repository data only. Visibility and
  writer checks stay in `Tasks.target_access`, per request.
- **Task scheduling is untouched.** Index work cannot change a delivery's status, timing,
  receipt or wakeups, and never runs inside a registry transaction.
- **Rows, receipt, pending reads, revision and change are committed together.**
- **Never show uncertain data as current.** A kind is `current` only while its deliveries
  are verified against GitHub's history within the freshness allowance and no read it owes
  is overdue; otherwise it says so.
- **A read never overwrites a newer delivery.**
- **One process per database.**
- **No secrets.** Rows hold what the list endpoints already return to writers.

## What can still go stale

| Cause | Repaired by |
| ---- | ---- |
| Failed delivery within GitHub's three-day history | Redelivery |
| Delivery acknowledged but not applied (full queue, crash, failed transaction) | The receipt check at the next recovery scan |
| Delivery applied but its follow-up read unfinished (crash, GitHub error) | The pending read resuming; the kind leaves `current` if it stays overdue |
| Missing delivery too large or otherwise impossible to replay | Rebuild of that kind |
| Service down for less than three days | The receipt check after start; rows are served as `catching-up` meanwhile |
| Service down for three days or more, or a lost projection file | Rebuild |
| A change GitHub never recorded as a delivery (payload over its cap, an event the projection does not apply) | A later snapshot of that same item, the next rebuild or an explicit refresh. A change to another item does not repair it. |
| A task pull request never seen, unlinked and outside the upstream's newest 100 | A later delivery that mentions it; a rebuild cannot find it |
| Missing subscription or disabled webhook | An explicit refresh, each time; nothing detects it (deferred) |
| Revoked installation, inaccessible repository, outdated target configuration | Not by a refresh; rebuilds fail and the state shows `failed` |
| A rebuild that keeps failing | Retried each recovery interval; the state shows `failed` with its `verified` time |
| A lost service-originated signal (`sponsorships`, `tasks`) | The resynchronization change at the next start or queue overflow; otherwise the next signal for it or a refresh |
| A credential expiring, for the coverage inside a saved sponsorship list | The next sponsorship signal or an explicit refresh; nothing signals expiry |
| A sponsorship comment on an unlabelled issue the service never learned of | Nothing while it stays unknown and unlabelled; the service cannot discover it today either |

## Phasing

| Phase | Delivers | Size |
| ---- | ---- | ---- |
| 0 | Evidence. **Done** except measurements under load: see [phase 0](list-index/phase-0.md). | Small |
| 1 | Change signals. **Implemented, not deployed:** see the [phase 1 record](list-index/phase-1.md). | Medium to large |
| 2 | Mode `rows`: row rebuilds, pending reads, the read-publication rule, absence handling, run membership; executor runs; `/lists`. | Medium |
| 3 | Issues: rows from payloads, removal, sponsorship hydration. | Medium |
| 4 | Pull requests from payloads, linked pull requests in rebuilds, CI by rollup read. | Medium |
| 5 | Live updates: `changes` feed, relay with resynchronization, frontend subscription, task progress. | Medium |

- **Deployment order:** the service ships each phase first. The backoffice is switched to
  the index once, by setting `lists_token_path`; after that each kind follows the mode
  the service reports. The same token value is provisioned in each process's private
  settings directory (`/etc/concurro/service`, `/etc/concurro/backoffice`).
- **Migrations** are forward only: the projection's schema version, the two optional
  settings and the read cache's revision column.
- **Each phase updates** the service design notes, the backoffice API and cache
  documentation, the settings examples, and the frontend's live and mock contracts.
- **Tests per phase** cover: ingress parity and timing when the projection fails or its
  queue is full, a delivery acknowledged but lost across a restart and repaired by the
  receipt check, a receipt whose follow-up read was interrupted, an older read finishing
  while a newer request for the same item is pending, an unreplayable delivery, the switch
  from replay to rebuild, the freshness allowance expiring, the three-day boundary at
  start, a deleted run leaving the list, a run discovered during a rebuild staying in it,
  a lost sponsorship signal repaired at start, a rerun first seen at a later attempt,
  reordered and tied updates, a delivery and an item read racing a rebuild, absence, rerun
  attempts, a check event during a rollup read, projection recreation,
  registry signals not affecting registry transactions, the token, every row of the
  backoffice behaviour table, feed resynchronization, and a change arriving during a
  frontend read.
