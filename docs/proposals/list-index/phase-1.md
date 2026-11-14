# Phase 1 brief: change signals

**Implemented in this changeset; not deployed.** Base: service `cf29ba9`, frontend
`6b275ec`. Implements phase 1 of the [proposal](../list-index.md), informed by
[phase 0](phase-0.md). The brief below is kept as written; where the implementation
differs, this section says so.

## As built

- **Names:** the store is `ListIndex` in `service/list_index.py`, and the endpoint handler
  `Lists` lives in the same module. `Actions` already has a `projection` argument for
  sponsorship views, so "projection" was avoided in code.
- **One connection:** the index uses a single SQLite connection under a lock for its
  writer thread and its readers, as the backoffice's read cache does. Reads are two small
  queries.
- **State is derived, not stored:** `coverage` holds the baseline and verified time;
  `current` and `catching-up` are computed from them on each read.
- **Registry signals only on change:** each writer of a sponsorship document compares the
  stored text with the new one (`Registry.noted`), so the per-tick rewrites in `grant`,
  `end` and `credential_events` signal nothing when the document is unchanged. A
  terminal outcome in `end` always signals.
- **Refresh without the index** outdates the target's saved lists in the read cache
  instead of answering 503, so the menu action works in both modes.
- **Unavailable index in the frontend** needs no new note: the reason arrives as the
  existing `cache.error` and shows in `SavedData.svelte`'s alert. Only `catching-up` has
  a new line.
- **Tests:** 12 in `service/tests/test_list_index.py`, one in `test_settings.py`, 18 in
  `backoffice/tests/test_list_freshness.py`, including the backoffice client against the
  service's real listener. The registry test exercises the commit callback and
  `credential_events`; the other writers are covered by the mechanism they share, not
  one by one.
- **After code review** (eight findings, all fixed with a test each):
  - A read that comes back incomplete is retried like a failed one and never replaces a
    complete saved copy. Before, a failed sponsorship or runs read saved against a
    revision would have been kept until that revision moved.
  - `ListIndex.check` rebaselines a kind it last verified, or began and never verified,
    longer ago than GitHub keeps history, whatever the registry's coverage says.
  - With the index enabled and unavailable, a disabled or failing read cache answers 503
    instead of reading GitHub.
  - A credential poll that consumed events signals every repository with sponsorships,
    even when no document changed; an empty poll stays silent.
  - The backoffice validates each index entry it uses; a malformed one, or one in a mode
    it does not serve (`rows`), is treated as unavailable.
  - Nothing is `current` until a check has completed in the running process.
  - Coverage of a repository removed from the configuration is dropped at start.
  - The frontend's `Resource.reload` guarantees one more read after a read in flight;
    "Refresh from GitHub" uses it for the page, its runs and the overview.
- **Tests now:** 16 in `service/tests/test_list_index.py`, 24 in
  `backoffice/tests/test_list_freshness.py`, 10 frontend.
- **Not done:** the "Done when" checks need a deployment. The refresh menu was checked in
  the mock app in both themes.

## Outcome

With the index enabled, saved lists are re-read from GitHub when the service reports a
change, not when they turn 60 seconds old. Every kind is in mode `signals`: the service
holds no list rows yet. A writer can force a re-read. The backoffice shows when the
service is unsure it has seen everything.

## Out of scope

Rows, `/lists`, rebuilds from GitHub, authoritative reads, pending reads, delivery-detail
fetches (all phase 2 on); the `tasks` kind and the change feed (phase 5).

## Service

### `service/projection.py` (new)

A `Projection` owning `list-index.sqlite3` in `settings.state_dir`: one writer thread, WAL,
readers on their own connections. Schema version 1 creates `meta` (version, incarnation),
`repositories`, `revisions`, `receipts`, `coverage` and `changes` as the proposal's Store
section defines them. `pending` and row tables come in phase 2.

| Operation | Behaviour |
| ---- | ---- |
| `offer(guid, event, repository)` | Called from ingress. Puts onto a bounded queue without waiting; on a full queue sets the overflow flag. Never raises. |
| `signal(repository, kind)` | Local signals (sponsorships). Same queue and overflow rule. |
| Writer, per delivery | One transaction: receipt, revision, change. A GUID that has a receipt is skipped. |
| Writer, on overflow flag or at start | One transaction: raise `sponsorships` for every repository and append a repository-wide resynchronization change. |
| `check(groups, started)` | The receipt check below. |
| `rebaseline(repository, kind)` | Set the baseline to now, then raise the revision. Used by refresh, by the replay threshold and when recovery falls back to full reconciliation. |
| `revisions(repositories)` | Per repository and kind: `{revision, mode: "signals", state, verified}`. |
| `close()` | Stop the writer, close connections. |

- **Kinds from events:** `issues`, `issue_comment` → `issues`; `pull_request`, `check_run`,
  `check_suite` → `pulls`; `workflow_run` → `runs`. In this phase `issue_comment` is not
  split into issue and pull-request comments: no payload field is read at all.
- **Receipt check:** for each GUID group from `Recovery.deliveries`, take the newest
  attempt. If its event is applied, its `repository_id` maps to a configured repository,
  its `delivered_at` is after the kind's baseline and it has no receipt, record the receipt
  and raise the revision. More than 20 such deliveries for one kind: `rebaseline` instead.
  A check that completes advances `verified` to `started` for every kind.
- **State:** `current` while `verified` is younger than the freshness allowance (a module
  constant, initially 600 seconds); otherwise `catching-up`. At start every kind is
  `catching-up` until the first check completes. A kind whose `verified` is three days old
  or absent is rebaselined at start.
- **Repository IDs:** read once per configured repository at start through its reader
  (`repos/{repository}` → `id`) and stored in `repositories`.

### Wiring

- **`Ingress`** (`service/ingress.py`): new optional `projection`. In `handle`, after the
  installation check and before `self.keys`, call `offer` inside `try/except Exception`
  that logs the exception type. Nothing else in `handle` changes.
- **`Handler.do_POST`:** paths under `/lists/` go to `self.server.lists` before the webhook
  branch, whether or not `actions` is configured. Body limit as `ACTION_LIMIT`.
- **`service/lists.py`** (new): `Lists(projection, token)` with `handle(path, authorization, body)`
  → `(status, payload)`. Constant-time bearer comparison (`hmac.compare_digest`); 401
  without it; 503 when `lists_token_path` is unset. Endpoints: `/lists/revisions`,
  `/lists/refresh` (calls `rebaseline` for each kind of the repository).
- **`Recovery`** (`service/webhook_recovery.py`): new optional `projection`. In `poll`,
  call `projection.check(groups, started)` after `groups = self.deliveries()`, and
  `rebaseline` every repository and kind on the full-reconciliation branch. Both inside
  their own `try/except` so a projection failure cannot stop redelivery or the registry's
  `webhook_check`.
- **`Registry`** (`service/registry.py`): new optional `changed` callback. Writers of
  `issue_sponsorships` or of a terminal outcome note the affected repository during the
  transaction; `transaction()` invokes the callback after `COMMIT` and after releasing
  `self.guard`, inside `try/except`. Writers: `receive` and `request_observation` (hints),
  `confirm_sponsorship`, `record_shadow`, `credential_events` (only when the batch held
  events; see phase 0), `grant`, `end`, `recover`. The callback is `projection.signal(repository, "sponsorships")`.
- **`Daemon`** (`service/__main__.py`): construct the projection after the readers, pass it
  to `Ingress`, `Recovery` and the registry, attach `Lists` to the HTTP server. In `stop`,
  close it after `self.http.shutdown()` and before `self.registry.close()`.
- **Settings** (`service/settings.py`): optional `lists_token_path`, validated with the
  existing `secret()` helper and added to the `identities` uniqueness check; 32 bytes as
  hex, read with `private.read`.

## Backoffice

- **Settings** (`backoffice/settings.py`): optional `lists_token_path`, read with
  `private.read`, same format.
- **`ServiceCommands`** (`backoffice/commands.py`): `list_revisions(repositories)` and
  `list_refresh(repository)`, sending the token, with a short timeout of their own. Any
  non-200, transport error or malformed body raises one new exception, `IndexUnavailable`.
  It must never surface as a 401 to the browser.
- **`ReadCache`** (`backoffice/read_cache.py`): forward migration adding a `revision`
  column to `snapshots`. `read` gains a way to say how freshness is decided:
  - by age (today, and the only rule when the index is disabled);
  - by revision: due when the stored revision differs **or** the entry was invalidated
    locally (a command, a credential write), subject to the existing retry delays; never
    because of age. The revision passed in is the one stored with the result, so it must
    be the one read **before** the load;
  - saved only: never schedule a refresh; return saved data flagged with the supplied
    reason; raise when nothing is saved.
  Local invalidation keeps rejecting a refresh that began before it (the existing
  epoch), and command invalidation keeps its settling behaviour, under all three.
- **Counting list reads:** `Tasks.listing` logs one line per GitHub list read it performs
  (kind and repository, no content), since successful reads are not logged today and the
  acceptance checks below count them.
- **`Tasks`** (`backoffice/tasks.py`): one `list_revisions` call per request for the
  repositories it will read, kept on the instance. `Tasks.listing` maps its names to kinds
  (`issues` and `issue:<n>` → `issues`; `sponsorships:<user>` → `sponsorships`; `pulls`;
  `runs`) and picks the freshness rule per the proposal's Backoffice behaviour table.
  `freshness()` adds `state` and `verified` (the least favourable among the kinds read).
- **Credential writes** (`backoffice/app.py`, `put_credential`, `revoke_credential`):
  outdate every saved `list:sponsorships:` entry, since credential coverage is read from
  the vault and the registry learns of the change only at its next poll.
- **Known limitation, to document:** a credential expiring is not a signal (phase 0), so
  coverage inside a saved sponsorship list can be stale until another sponsorship signal
  or a refresh. No list view shows coverage.
- **Refresh:** `POST /api/targets/{owner}/{repo}/refresh`, behind the existing `writer`
  dependency and `find_target`; calls `list_refresh` for each of the target's
  repositories; 503 with a plain message on `IndexUnavailable`.

## Frontend

- `ReadFreshness` (`src/lib/types.ts`) gains optional `state` and `verified`.
- `SavedData.svelte` shows a note for `catching-up` ("Concurro is checking for missed
  updates; last verified …") and for an unavailable index, without reloading in a loop.
- Target page: an overflow menu (`DropdownMenu` from `bits-ui`, as `UserMenu.svelte` uses)
  with "Refresh from GitHub", shown to writers; on success clear the cached reads and
  reload the page's resources; on 503 a toast.
- `actions.refreshTarget` in `live.ts` and a mock equivalent in `mock.ts`.

## Tests

- **Ingress parity:** with a projection that raises, blocks or has a full queue, every
  case in `service/tests` for `Ingress.handle` returns the same status and enqueues the
  same wakeups as without one.
- **Projection:** receipt, revision and change commit together; duplicate GUID skipped;
  overflow raises local revisions and appends the resynchronization change; restart keeps
  revisions and incarnation; a recreated file changes the incarnation.
- **Receipt check:** a delivery acknowledged but never applied is repaired; deliveries
  before the baseline are ignored; the 21st missing delivery rebaselines; `verified`
  advances only to the scan's start; a failing check leaves the kind `catching-up`.
- **Registry signals:** each listed writer signals after commit; a raising callback leaves
  the transaction committed and the registry usable; `credential_events` with an empty
  batch does not signal.
- **Lists endpoint:** wrong or missing token, unset setting, both endpoints, and that an
  unknown path still reaches webhook handling.
- **Read cache:** the three freshness rules, the revision captured before a load that a
  delivery overtakes, a local invalidation making an entry due while the service revision
  is unchanged and discarding a refresh that began before it, migration of an existing
  database, command settling unchanged.
- **Backoffice:** every row of the behaviour table that applies to mode `signals`;
  `IndexUnavailable` never becomes a 401; refresh as writer, as non-writer and with the
  service down; credential writes outdating sponsorship lists.
- **Frontend:** `npm run check`, `npm test`, `npm run build`; the refresh action and both
  notes in mock mode, light and dark.

## Documentation

`docs/design/service.md` (projection, receipt check, list endpoints), `backoffice/README.md`
(list loading, read cache, settings example, the refresh endpoint),
`gateway/README.md` (the token file), the frontend `README.md`.

## Deployment

- **Before deploying:** create one token (`openssl rand -hex 32`) and install the same
  value as an owner-only file in `/etc/concurro/service` and `/etc/concurro/backoffice`,
  each owned by that container's user.
- **Order:** the service first, with `lists_token_path` set. The backoffice keeps today's
  behaviour until its own `lists_token_path` is set.
- **No App permission or subscription change.** The projection file is created on first
  start; there is nothing to back up or migrate. The read cache migrates forward on open.

## Done when

- Viewing an idle target for ten minutes logs no list reads, and a test with a counting
  reader shows none across repeated requests at an unchanged revision.
- A label change on GitHub appears on the next page load without waiting a minute.
- Stopping the service shows saved lists with the unavailable note, and no list reads.
- Killing the service between a delivery's 202 and its application is repaired by the
  next recovery scan.
