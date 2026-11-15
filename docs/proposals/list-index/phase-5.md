# Phase 5 brief: live updates

**Implemented in this changeset; not deployed.** Base: service `0232334` and frontend
`1cabd26` (phases 1 to 4 merged and deployed). The brief below is kept as written; where
the implementation differs, this section says so.

## As built

- **What subscribes.** Decided at the start: every change kind is relayed, since the
  rows of `changes` for lists already existed. Task progress outdates the overview and
  that task's page; an issue, pull request or sponsorship change the overview and the
  target pages concerned; a runs change those targets' runs tab.
- **`tasks` is a local kind** next to `sponsorships` (`LOCAL_KINDS`), so it has a
  revision and coverage row like any kind and is resynchronized with it. It has no list.
- **`Registry.changed(kind, repository, issue)`** now carries the kind; `Registry.end`
  notes `tasks` for a live generation. `diagnostics.Store.changed(repository, issue)` is
  called after `submitted` and `finished`, and after `observed` only when the status moved
  or a runner was first assigned, since observations repeat.
- **Feed:** `ListIndex.changes` waits on a condition notified after every committed
  transaction and holds no lock while waiting. A cursor is refused (resynchronize) when it
  is missing, from another incarnation, older than the rows kept, or ahead of the last
  row. One answer carries at most 500 changes. `ListIndex.release` wakes waiters;
  `Daemon.stop` calls it before `self.http.shutdown()`.
- **Retention:** `changes` rows are kept one hour (`CHANGES_SECONDS`), pruned by the
  receipt check; receipts keep their six days.
- **Relay:** `backoffice/live.py`, started in the app's lifespan when `lists_token_path`
  is set. `ReadCache.outdate(..., exact=True)` is the operation that makes one snapshot due
  without settling. The relay only outdates snapshots for `tasks` changes: lists already
  follow their revisions.
- **`GET /api/events`** is an async stream; the relay thread hands events to each
  stream's queue on the event loop. Visibility is `Tasks.visible_targets`, cached per
  stream for a minute. An event names the target pages concerned (`targets`, by upstream),
  so the browser needs no mapping from repositories to targets.
- **Shutdown:** the backoffice now stops with `timeout_graceful_shutdown=5`. Without it an
  open stream would hold uvicorn's stop until the container is killed, skipping the
  cleanup of the read cache and logout state.
- **Frontend:** `Resource.reload` already ran one more read after a read in flight
  (phase 1). New: `Resource.watch`/`outdate` and `outdate(concerns)` in
  `cache.svelte.ts`, `changes.ts` (which reads a change outdates) and `events.ts` (the
  stream). The overview, target page, runs tab and task page each mark their resource as
  displayed.
- **Mock mode** has no stream; a command applies the same change through `events.ts`.
- **After code review** (two findings, both fixed with a test each):
  - A change arriving while a read is in flight is now always followed by one more read,
    displayed or not, so one during a page's first load is not overwritten by the answer
    already under way. Pages watch their resource themselves, through loading and error
    states, instead of `SavedData.svelte`, which mounts only after a first answer.
  - The service appends a `state` change when the states a repository reports move
    without a list changing: a history check that makes them `current`, or any read by
    the worker, such as an overdue item read that settles with unchanged content, or a
    rebuild that fails (the second review round added the successful reads). It is appended after the new state is reported and raises no revision. The
    overview, target page and runs tab reload on it, which clears a stale "checking for
    missed updates" note.
- **Tests:** service: 5 feed tests, 2 hook tests, the overdue read and the endpoint in
  `service/tests/test_list_index.py` (60 in all). Backoffice: 9 in
  `backoffice/tests/test_live.py`, one of them a real HTTP stream, and the client against
  the service's own feed in `test_list_freshness.py`. Frontend: 3 more in
  `tests/cache.test.mjs` and 2 in `tests/events.test.mjs`, including the browser-only
  disconnection.
- **Not done:** the "Done when" checks and the edge's flush setting need a deployment.
  Not verified against a real browser and edge: the stream was exercised over HTTP
  without a proxy, and the app only in fixture mode.

Implements phase 5 of the [proposal](../list-index.md).

## Outcome

An open overview or task page updates when Concurro makes progress on a task, without a
reload.

## Service

- **`tasks` kind:** a change names its repository and issue. Emitted after a successful
  record of:
  - a tick's outcome: after `Registry.end` commits for a live generation, through the
    registry's `changed` callback from phase 1 (extend it to carry the kind and issue);
  - a turn's submission, result and observed transitions: from `diagnostics.Store`
    (`submitted`, `finished`, `observed` for started, runner assigned, lost, cancelled)
    after its own write, since `Recorder` swallows failures.
- **Feed:** `POST /lists/changes` with `{incarnation, since}` on the `/lists` handler.
  Answer with later rows of `changes`; with none, wait on a condition for up to 25 seconds
  without an open transaction, then answer empty. `Daemon.stop` wakes waiters before
  `self.http.shutdown()`. A cursor from another incarnation or older than the retained
  changes gets `{resynchronize: true, incarnation, sequence}`.
- **Retention:** prune `changes` by age (a module constant); the feed's resynchronize
  answer covers a consumer that fell behind.
- **Lost local signals:** unchanged from phase 1, now also raising `tasks`: the
  resynchronization change appended at start and on overflow.

## Backoffice

- **Relay:** one background task holds the feed request, with a client timeout above 25
  seconds and below `ServiceCommands`' 60. It keeps the cursor in memory, repeats a lost
  request from the same cursor, and backs off while the service is unavailable.
- **On a `tasks` change:** mark that issue's saved snapshot due through a new `ReadCache`
  operation that does not start command settling (`invalidate` does), then relay.
- **On resynchronize** (answer or change), and at its own start without a cursor: mark
  every saved snapshot and list of the affected repositories due and relay a reload.
- **`GET /api/events`:** server-sent events for the signed-in session, carrying only
  changes for targets `visible_targets` returns for that user, re-evaluated when its
  cached access decisions expire. The edge needs `flush_interval -1` for this path, as
  the inference route has.

## Frontend

- One `EventSource` per app shell (`(app)/+layout.svelte`), reconnecting on error. Events
  missed while disconnected are not replayed: every time the stream opens, including the
  first, the displayed views refresh.
- A change for a view's task or target calls that view's `Resource.refresh`. Extend
  `Resource` (`src/lib/api/cache.svelte.ts`) so a refresh requested while a read is in
  flight runs once more when it finishes, instead of being dropped.
- First subscribers: the overview (tiles, "Needs you", activity) and an open task page.
- `SavedData.svelte`'s polling stays for refreshes and command settling.
- Mock mode: an in-browser event source driven by `runCommand`.

## Tests

- Service: each hook emits after its write and not when the write fails; a stale
  generation emits nothing; the feed's wait, timeout, shutdown wake-up, replay from a
  cursor and both resynchronize cases.
- Backoffice: a `tasks` change makes the next snapshot read refresh without settling
  behaviour; resynchronize marks everything due; a user receives no change for a target
  they cannot see; relay recovery after the service restarts.
- Frontend: a change during an in-flight read triggers exactly one more read;
  `cache.test.mjs` covers the `Resource` change; a browser-only disconnection while the
  relay stays healthy, with a last task change and no later event, shows that change after
  reconnecting.

## Deployment

Service, then backoffice, then the edge route's flush setting with the web release. No
App change.

## Done when

With a task running, the overview's activity feed and the task page's stage advance
without a reload, and stopping and starting the service leaves open pages consistent
after one reload signal.
