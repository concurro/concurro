# Phase 4 brief: pull requests and CI

**Implemented in this changeset; not deployed.** Base: service `6441488` (phases 1 to 3
merged and deployed). No frontend change was needed. The brief below is kept as written;
where the implementation differs, this section says so.

## As built

- **Schema version 4** adds `pulls`, shaped like the other row tables so `store`, `remove`
  and the read fence are shared, and `pull_checks`: the check runs with the commit they
  were read for. Versions 1 to 3 migrate forward on open.
- **Rows are in the backoffice's existing shape** (`number`, `title`, `open`, `draft`,
  `ref`, `sha`, `additions`, `deletions`, `runs`, plus `updated_at`), so
  `Tasks.pulls_by_issue` only gained the `rows` source and copies saved by earlier
  releases keep serving while the list is first built. `/lists` orders them most
  recently updated first, which the selection relies on.
- **Checks apart from the row's ordering.** A check changes no pull request timestamp, so
  check runs are not part of the ordered document. They are listed only while their
  commit is the row's head; a push therefore shows `none` until the new head is read.
- **Rollup reads** are the item reads of this kind: owed for a new row, a changed head and
  a `check_run` or `check_suite` on a held head, coalesced per pull request. The same read
  returns the row, so it also corrects line counts and settles ties.
- **Shared code:** `TASK_BRANCH`, `PULL_REQUESTS`, `graphql_pull`, `pull_fields`,
  `task_pulls` and `pull_request` live in `service/github_reads.py`. `Access.pull_requests`
  keeps its REST fallback for the index-disabled mode only.
- **Linked pull requests:** `Registry.linked_pulls(repository)` gives a rebuild the numbers
  recorded for tasks.
- **Deployment:** as in phase 3, the backoffice must not be older than the service: the
  phase 3 backoffice treats a held pull request list as unusable. This replaces "Service
  first" below. Both images come from this repository.
- **After code review** (two findings, both fixed with a test each):
  - A read that itself gives a row its head (a rebuild discovering a pull request or a
    changed head, or an item read finding a new head) owes one more rollup read. A check
    delivered during such a read matched no held head, got a receipt and owed nothing, so
    its result would otherwise be lost. A rebuild therefore reads each newly discovered
    pull request a second time.
  - A GraphQL answer carrying errors is refused whole, for the listing and for one pull
    request, so a partial answer never replaces held check runs. Absence is taken only
    from an answer without the pull request whose every error is `NOT_FOUND`.
- **Tests:** 52 in `service/tests/test_list_index.py`, 33 in
  `backoffice/tests/test_list_freshness.py`.
- **Not done:** the "Done when" checks need a deployment. Check deliveries for an
  upstream's other pull requests each cost a receipt and one lookup; more than 20 missed
  at once for the kind cause a rebuild, whose cost under a busy upstream is unmeasured.

Implements phase 4 of the [proposal](../list-index.md). Phase 0 confirmed line counts in
one `synchronize` payload and that a rerun changes which check runs the rollup holds.

## Outcome

Task pull requests and their CI state are served from the projection. After this phase no
list content is read from GitHub while a page loads.

## Service

- **Schema version 4:** `pulls(repository, number, …)` with title, state, draft, head ref,
  head SHA, additions, deletions, updated, the CI check runs as last read, the local
  version and the removed marker.
- **From deliveries:** `pull_request` on an upstream whose head ref matches `TASK_BRANCH`
  (move the pattern from `backoffice/tasks.py` to a module both sides import). All 18 task
  pull requests on the demo target have their head in the fork (phase 0); the head
  repository plays no part in matching.
- **Line counts:** taken from the payload. Phase 0 saw one matching sample, so the rollup
  read that every head change already triggers also returns the counts and corrects them.
- **CI is read, not assembled.** After a rerun the rollup holds only the latest attempt's
  check runs while REST keeps every attempt's (phase 0). Record a pending rollup read when a pull request row is
  created, when its head SHA changes, and when a `check_run` or `check_suite` delivery's
  `head_sha` equals the head of a row. Those deliveries arrive for the upstream and list
  no pull requests (phase 0), so nothing else can associate them. A signal during a read
  leaves a newer pending request, which the publication rule preserves. A result is
  stored only if the row's head is still the SHA that was read.
- **Rollup read:** the existing GraphQL projection (`PULL_REQUESTS` in
  `backoffice/github.py`, moved to `service/github_reads.py`) restricted to one pull
  request: CheckRun entries among the first 100 contexts, status and conclusion.
- **Rebuild:** the combined listing of the upstream's newest 100 with `updatedAt` and
  rollups (one request, as today), plus the pull request `Registry.link` records for each
  task, and every retained row the listing did not return, by number with its rollup.
  The REST fallback without counts or checks fails the rebuild.
- **`/lists` for `pulls`** returns non-removed rows with their check runs.

## Backoffice

- `Tasks.pulls_by_issue`: for `pulls` in mode `rows`, take rows from the service. The
  selection is unchanged: per issue number an open pull request wins, otherwise the most
  recently updated; `ci()` over the check runs; `ready` from open and not draft.
- `Access.pull_requests` and `Access.task_issues` remain only for the index-disabled mode.

## List contract to hold

Open, closed and merged task-branch pull requests. **Changed:** a task pull request the
projection has seen stays listed after leaving the upstream's newest 100. A pull request
never seen, unlinked and outside that window cannot be discovered; document both in
`backoffice/README.md`.

## Tests

- Parity with `pulls_by_issue` over the existing fixtures: open wins, then most recent;
  draft; merged; counts absent.
- A check event before its pull request row exists; a check event during a rollup read;
  a head change during a rollup read (result discarded, new read pending).
- A check event for a SHA that is no row's head (receipt only, no read).
- Reordered and equal-timestamp pull request snapshots.
- A rebuild rereading a retained row outside the newest 100, and the REST fallback
  failing it.
- A rerun: check events for the new attempt lead to a rollup read that replaces the
  earlier attempt's check runs.

## Deployment

Service first. The upstream's pull requests are rebuilt on first start (one request on
the demo target). After release, confirm in the backoffice log that the overview and
target pages issue no GitHub list reads.

## Done when

The overview and both list tabs load with no GitHub list request from the backoffice, a
push to a task branch updates the head and resets CI on the next load, and a completed
check updates the badge without a manual refresh.
