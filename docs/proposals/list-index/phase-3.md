# Phase 3 brief: issues

**Implemented in this changeset; not deployed.** Base: service `427c49f` (phases 1 and 2
merged and deployed). No frontend change was needed. The brief below is kept as written;
where the implementation differs, this section says so.

## As built

- **Row handling is shared.** `ListIndex.store` and `ListIndex.remove` hold the ordering
  rule, the read fence and absence for both runs and issues; `built` and `settled`
  publish a rebuild and an item read. Runs keep their membership and attempt-1 logic on
  top.
- **Schema version 3** adds `issues`; versions 1 and 2 migrate forward on open.
- **Which issues get a row.** One that carried the enrollment label when delivered, one
  the registry holds a sponsorship for, and one already held. Other issues of the
  repository are not stored. A held row keeps following its issue after the label goes,
  so relabelling restores it without a read.
- **Same shape as GitHub's.** Rows are GitHub issue objects reduced to the listed fields
  (`number`, `title`, `state`, `user.login`, `labels[].name`, timestamps), so the
  backoffice's `summary` and `stage_label` are unchanged and copies saved by earlier
  releases keep serving while the list is first built.
- **Registry signals name the issue** (`Registry.changed(repository, issue)`; the issue is
  None for a credential poll). A sponsorship signal raises `issues` as well as
  `sponsorships`, since sponsorship decides membership of the issue list.
- **Membership follows the registry at read time:** `/lists` for issues filters held rows
  by the enrollment label or `Registry.sponsor_issues`.
- **Shared reads:** `enrolled_issues`, `issue`, `issue_fields` and `gone` live in
  `service/github_reads.py`. The backoffice's legacy listing calls `enrolled_issues` with
  its three-page cap; the rebuild reads every page.
- **Backoffice:** `Tasks.listed` takes a held issue list from the service and reads no
  sponsored issue one by one. A sponsored issue that cannot be shown (not held yet, or
  the index is away with nothing saved) is left out instead of failing the page.
- **Deployment:** the phase 2 backoffice treats a held issue list as unusable, so the
  backoffice must not be older than the service. Both images come from this repository.
- **After code review** (four findings, all fixed with a test each):
  - Start and overflow resynchronization always raises a held issue list's revision, so a
    lost sponsorship removal cannot leave the backoffice with an issue the list dropped.
  - A rebuild reads every held issue its listing did not return, enrolled or not, since
    any of them can re-enter the list through a sponsorship without a new read.
  - The backoffice leaves out a held issue that is neither enrolled nor in the
    sponsorships it read, instead of showing it as an intake task.
  - Before the list's first build, sponsored issues saved one by one by the previous
    release are still served from their saved copies, without reading GitHub.
- **Tests:** 44 in `service/tests/test_list_index.py`, 32 in
  `backoffice/tests/test_list_freshness.py`.
- **Not done:** the "Done when" checks need a deployment.

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
