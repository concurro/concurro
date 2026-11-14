# Phase 2 brief: rows, rebuilds and executor runs

**Implemented in this changeset; not deployed.** Base: service `6ec87c1`, frontend
`782db86` (phase 1 merged). The brief below is kept as written; where the implementation
differs, this section says so.

## As built

- **Schema version 2** adds `pending`, `runs`, `run_members` and three coverage columns
  (`built`, `wanted`, `failed`); a version 1 file migrates forward on open.
- **Which lists are rows:** `ListIndex.start(rows=…)` names them. The daemon passes each
  target's fork for `runs`; every other kind stays in `signals`.
- **One saved shape for runs.** The backoffice now saves the raw listing (each run with
  its first attempt) in both modes and attributes on every request (`Runs.listing`,
  `Runs.attribute`). Attribution reads only local diagnostics, and saving it froze it at
  the time of the read. In legacy mode this reads attempt 1 of every listed rerun, not only
  attributed ones.
- **The backoffice still decides by revision.** For a held list its "read" is `/lists`
  instead of GitHub, through the same saved-copy and revision rule as phase 1, so an
  unchanged revision costs no request to the service beyond the one revisions call.
- **Absent attempts are remembered** as a row without a document, so an attempt GitHub no
  longer has is not asked for on every later delivery of that run.
- **Failed reads wait for the next recovery scan** (`ListIndex.deferred`), not a timer.
- **A failed listing in legacy mode** now raises into the read cache, which keeps the last
  good copy and retries after a minute; `Tasks.target_runs` turns a cold failure into the
  existing "could not be loaded" message.
- **Found by the end-to-end test:** the listener dispatched only paths under `/lists/`, so
  the bare `/lists` fell through to webhook handling. Fixed.
- **Deployment order differs from the brief:** the phase 1 backoffice treats `mode: rows`
  as unusable, so with a phase 2 service it would show the saved runs with an
  "unavailable" note. Deploy the backoffice first, or both together.
- **After code review** (five findings, all fixed with a test each):
  - A rebuild settles only the rebuild requests made before it began; one made while it
    ran is still owed afterwards.
  - A delivery replayed from history is not applied when a rebuild published during the
    payload fetch already covers it, so it cannot bring back a run that rebuild removed.
  - An attempt GitHub no longer has loses its stored document under the same fence as a
    read. A run whose latest attempt is gone leaves the list; no older attempt stands in.
    The marker keeps the removed snapshot's timestamp, and a later delivery for it, of
    whatever age, is not applied but answered with a read of that attempt.
  - Saved copies of the unchanged lists keep their scope. The reshaped runs list has its
    own key (`runs:attempts`), and while it has no copy and the index is unavailable, a
    copy saved by phase 1 is still served.
  - A delivery payload without what its event must carry (the run with its identity,
    trigger and workflow path) is refused, in ingress and in replay, and so leads to a
    rebuild instead of a receipt.
- **Tests:** 32 in `service/tests/test_list_index.py`, 27 in
  `backoffice/tests/test_list_freshness.py`.
- **Not done:** the "Done when" checks need a deployment. The new status notes were not
  checked in a browser; the mock data has no index states.

## Outcome

The service holds the executor-run list. The target page's Actions tab is served from the
projection, including after a service restart, and reads GitHub only through the
service's rebuilds and item reads. This phase builds the machinery that phases 3 and 4
reuse.

## Machinery introduced here

- **Mode `rows`** per kind in `coverage`, with states `current`, `catching-up`,
  `rebuilding` and `failed`. `runs` switches to it; other kinds stay in `signals`.
- **Schema version 2:** `pending`, `runs(fork, run, attempt, …)` and run membership.
  Forward migration from version 1.
- **Delivery payloads:** `offer` now carries the projected fields of applied events, and
  the receipt check fetches a missing delivery's detail (`app/hook/deliveries/{id}`,
  as `Recovery.redeliver` does) to apply it. A detail that cannot be read or applied, or
  more than 20 missing deliveries, triggers a rebuild.
- **Rebuilds and item reads** on a worker thread, one GitHub request at a time, through
  `readers[repository]`. Publication follows the proposal's "Reads and publication":
  a result is a snapshot under the ordering rule, ties go to the read only if the row's
  local version is unchanged, absence is a removed marker, and a failed read leaves
  `failed` for the next recovery interval.
- **Pending reads:** recorded in the delivery's transaction, resumed at start, merged per
  item keeping the oldest request time, cleared only for requests made before the read
  began. An overdue pending read takes the kind out of `current`.
- **`POST /lists`** with `{repository, kind}` → `{revision, state, verified, items}`.
- **Shared GitHub reads:** move the runs listing out of `Runs.for_target` into
  `service/github_reads.py`, with the attempt read beside it, so the rebuild and the
  backoffice's legacy path call the same functions.

## Runs

- **From deliveries:** `workflow_run` on a fork whose workflow path is
  `EXECUTOR_WORKFLOW` and whose event is `workflow_dispatch`: the membership of
  `Runs.for_target`'s listing, which includes unattributed runs on any branch. The head
  branch and session decide attribution only, as `served` does today; they never exclude
  a run. Store the delivered attempt; keep the latest attempt and attempt 1.
- **Attempt 1:** a rerun seen without it gets a pending read of
  `actions/runs/{run}/attempts/1`. No other attempt stands in for it.
- **Membership:** the runs of the latest listing plus runs created by deliveries since,
  newest 50. A rebuild replaces membership with its listing plus the runs deliveries
  created while it ran.
- **Rebuild:** the listing of 50, plus attempt 1 of each listed rerun (none on the demo
  target today, per phase 0).
- **Conflicts:** `workflow_run` carries `updated_at`; a tie with different content is
  settled by reading that exact run attempt.

## Backoffice

- `ServiceCommands.list_rows(repository, kind)`.
- `Tasks.target_runs`: for a kind in mode `rows`, take the listing from the service and
  run the unchanged attribution (`served`, `Diagnostics.for_sessions`, `Runs.attributed`)
  over it. An attributed rerun shows attempt 1 and is omitted with the existing message
  while attempt 1 is missing; an unattributed run shows its latest attempt. Save the
  result in the read cache as the fallback copy. Without diagnostics the result stays
  empty, as today.
- Apply the remaining rows of the behaviour table: `catching-up`, `rebuilding`, `failed`,
  and "nothing to show".
- Refresh now rebuilds kinds in mode `rows`.

## Frontend

`SavedData.svelte` gains notes for `rebuilding` and `failed`. No other change: the runs
response keeps its shape.

## Tests

- Attribution parity: the existing `test_activity.py` run cases pass against rows.
- An executor run dispatched on a branch that is no runner session is listed, unattributed.
- A delivery and an item read racing a rebuild; a run created during a rebuild staying in
  the list; a deleted run leaving it at the next rebuild.
- A rerun first seen at attempt 3: pending read of attempt 1, row omitted when attributed
  until it lands, shown at its latest attempt when unattributed.
- A receipt whose pending read was interrupted by a restart; an older read finishing
  while a newer request for the same item is pending.
- An unreadable delivery detail leading to a rebuild; the replay threshold.
- Schema migration from version 1 with data present.
- Every `rows` row of the backoffice behaviour table.

## Deployment

Service first. No setting changes. Each fork's runs are rebuilt on first start (one
request per fork on the demo target).

## Done when

The Actions tab loads with the service's rows and no GitHub request from the backoffice,
a run started on GitHub appears on the next load, and a service restart serves the same
rows as `catching-up` and then `current`.
