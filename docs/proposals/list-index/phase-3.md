# Phase 3 brief: issues

**Not implemented; depends on phase 2.** Implements phase 3 of the
[proposal](../list-index.md). Re-check against the code and the earlier phases as built.

## Outcome

Enrolled issues, and the sponsored issues shown beside them, are served from the
projection. The backoffice no longer lists issues from GitHub or reads sponsored issues
one by one.

## Service

- **Schema version 3:** `issues(repository, number, …)` with title, author, labels, state,
  created, updated, the local version and the removed marker.
- **From deliveries:** `issues` and `issue_comment` on issue repositories both carry the
  issue object; upsert from either. Skip payloads whose issue has a `pull_request` key.
  Only `issues.deleted` and `issues.transferred` set the removed marker;
  `issue_comment.deleted` is an ordinary update of the issue.
- **Stage labels arrive as two deliveries** about two seconds apart, added then removed
  (phase 0). Apply each as delivered; between them the row carries two stage labels and
  the backoffice's `stage_label` picks as it does today. Do not coalesce or reorder them.
- **Mapping change:** `issue_comment` on a pull request no longer raises `issues`.
- **Sponsorship membership:** a `sponsorships` signal names its issue from this phase. If
  that issue has no row, record a pending read of it. At start and after an overflow,
  compare `Registry.sponsor_issues` for each issue repository with the rows and record a
  pending read for each one missing.
- **Rebuild:** every `concurro`-labelled issue, open and closed, paginated to the end
  (extend `task_issues` in `service/github_reads.py`: no three-page cap), plus the
  registry's sponsorship issues by number. Rows the listing did not return, that the
  projection holds as enrolled and that did not change during the rebuild, are read by
  number. Gone (404, 410, moved, as `gone_issue` recognises) sets the removed marker.
- **`/lists` for `issues`** returns rows that carry the `concurro` label or are among the
  registry's sponsorship issues, not removed.

## Backoffice

- `Tasks.listed`: for `issues` in mode `rows`, take the issue list from the service
  instead of `Access.task_issues` and drop the `sponsored_issue` reads. Everything after
  that is unchanged: `summary`, the join with sponsorship projections from
  `/sponsorships`, the task-versus-intent split, the join with pull requests.
- The saved copy and the non-`current` states behave as for runs.

## List contract to hold

Open and closed, never pull requests, sponsored issues included without those GitHub
reports gone. **Changed:** every enrolled issue, not the newest 300; say so in
`backoffice/README.md`.

## Tests

- Parity with `Tasks.listed` over the existing fixtures in `test_sponsorship_api.py` and
  `test_backoffice.py`, including intents and the gone-issue cases (301, 410 omitted;
  403 and transient failures failing the rebuild, not the row).
- Label added then removed as separate deliveries, in order and reversed.
- Reordered and equal-timestamp issue snapshots; a late delivery after `deleted`.
- A sponsorship signal for an unknown issue; the same signal lost and repaired at start.
- Unlabelling: the row stays, leaves the list, and returns when relabelled.
- Pagination beyond three pages.

## Deployment

Service first. Each issue repository is rebuilt on first start (one page on the demo
target).

## Done when

The Issues tab and the overview load without a GitHub issue listing from the backoffice,
a stage change shows on the next load, and a newly sponsored unlabelled issue appears as
an intent without a backoffice read of that issue.
