# Phase 5 brief: live updates

**Not implemented; depends on phase 1, and on phases 2 to 4 for list events.** Implements
phase 5 of the [proposal](../list-index.md). Decide at the start which views subscribe
beyond task progress. Re-check against the code and the earlier phases as built.

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
