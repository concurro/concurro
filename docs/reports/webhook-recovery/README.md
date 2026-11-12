# Webhook recovery and idle request traffic

Uncommitted implementation evidence; no deployment, App setting changes or pilot commands.

## Request measurement

Run `python3 service/tests/idle_traffic.py` to reproduce [traffic.json](traffic.json).
The fixture reaches actual handoff through the service scheduler, worker, controller and
fake GitHub, then observes one unchanged hour. Both head and test-merge CI are present.
The comparison restores the previous five-minute readiness timer and reconciler schedule;
it does not change read implementations, including the worker's fresh pre-tick snapshot
(comments and events, about two requests per tick). Thirteen-task totals scale measured per-task
reads while counting the repository enrollment listing once per interval. They are an
offline workload projection, not a reconstruction of production traffic.

| One idle hour, 13 handoff tasks | Previous scheduling | Webhook recovery |
| ---- | ---- | ---- |
| Repository REST requests | 8,462 | 0 |
| Repository GraphQL requests | 156 | 0 |
| Artifact reads (included above) | 312 | 0 |
| App delivery-list requests | 0 | 12 |
| Model calls | 0 | 0 |
| GitHub writes | 0 | 0 |

Bootstrap and event-driven work are excluded from the steady-state table. A separate
regression seeds thirteen durable idle outcomes in one registry and verifies that twelve
recovery checks dispatch no engine work, while a relevant event wakes its task.

The delivery checker reads all retained summary pages to avoid dropping delayed failures
or confusing an old failed attempt with a successful redelivery on another page. At 100
summaries per page, 150 retained deliveries cost 24 list GETs/hour; 1,000 cost 120.
Each failed eligible GUID adds a detail GET and, if still failed, a redelivery POST.
Pending deliveries are revisited; historical failures with a successful attempt cost no
redelivery requests. These requests do not invoke models.

## Regression coverage

- Receipt and all wakeups/jobs commit atomically; injected failures roll back both.
- Duplicates, restart recovery, CI-only backoff, absent CI senders, workflow runs,
  base pushes and deleted comments.
- Cursor pages, cross-page successful retries, pending and transport-failed deliveries,
  foreign destinations, lost redelivery replies, failed scans and retention gaps.
- Durable command observations use uncached snapshots and `registry.observe`. HTTP 200
  with stale contents retains its job. Edits/deletions, superseded creates, uncertain
  POSTs without an ID, backoff and arrivals during observation are covered.
- Registry migrations preserve queued work, delivery receipts and unresolved writes.

## Delegated-command merge boundary

The other branch owns signed command creation and authorization. This branch provides
`Ingress(..., command_comment=...)`, `Reconciler(..., command_comment=...)` and
`registry.request_observation(key, comment=..., action="created")`. Wire
`delegation.marked` into both predicates and use the durable request method after command
posting, including uncertain POSTs. Receipt success is not observation completion.

Tests here use the predicate seam and synthetic command bodies; they do not claim to test
the other branch's signatures or approval revocation end to end. The second branch merged
must include combined signed-command, edit/delete revocation, stale-list, lost-response,
restart and backoff regressions before the combined release is ready. The other worktree
has not been modified.

Integrated with the delegated-command branch: `service/__main__.py` wires `delegation.marked`
into both predicates, and `service/actions.py` calls `request_observation` with the exact
nonce-bearing body (and GitHub's comment ID when returned) after successful or uncertain posts.
`service/tests/test_delegated_commands.py` covers signed approvals, lost responses, stale lists,
restart, late/duplicate webhooks, retry backoff, approval edits/deletions, untracked starts and
refused posts through the real actions, ingress, observations, worker and engine.
Integrated validation: engine 1,168 (one skipped), service 420, gateway 84 and backoffice 20;
all passed, with ruff, Pyright, compileall, the bundle check and `git diff --check`.

## Operational changes

Verify Workflow runs and Push subscriptions before deployment. Recovery uses the App's
existing JWT credentials. There is no ordinary hourly state sweep. Full reconciliation
is for bootstrap, changed target scope, history gaps or explicit `reconcile-once`.
Local registry schema 5 is a rollback boundary; back up the stopped registry. The engine
bundle and task checkpoint format are unchanged.

## Validation

Full suites: engine 1,161 (one skipped), service 392, gateway 84 and backoffice 18;
all passed. Ruff check, format check, Pyright, compileall and `git diff --check` pass.
The traffic fixture reproduces the committed JSON exactly. No bundle resources changed.
The backoffice session separately ran the twelve observation regressions and confirmed
the stale-response integration fix; combined signed-command coverage remains a merge task.
