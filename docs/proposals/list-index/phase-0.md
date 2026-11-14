# Phase 0: evidence for the list index

**2026-11-14: workstation and host checks complete; measurements under load remain.** Base: service
`cf29ba9`, frontend `6b275ec`. Nothing was deployed and no App setting was changed. Every
GitHub request was a read, with two exceptions: the capture script's normal minting of a
read-scoped installation token, and one authorized rerun of a CI job. This records what the [proposal](../list-index.md) marked
*unverified*, what remains, and how to finish.

## Demo target, measured

Read with a user token from `concurro-demo-upstream/stacks-core` (issues, pull requests)
and `concurro-demo-fork/stacks-core` (task branches, executor runs).

| Quantity | Value | Consequence |
| ---- | ---- | ---- |
| Enrolled issues, open and closed | 22 (18 open, 4 closed) | An issue rebuild is one page. |
| Upstream pull requests in total | 22, of which 18 are task-branch pull requests, all open | The newest-100 window currently holds every task pull request; no by-number reads in a rebuild. |
| Task pull request heads | All 18 are cross-repository, head in the fork | Fork-head handling is the normal case, not an edge. |
| Executor runs on the fork | 357 in total; the newest 50 span about two days | The run list turns over quickly. |
| Reruns among the newest 50 runs | 0 | A runs rebuild is one request today. |

A full rebuild of this target is therefore about three GitHub requests, keeping the
existing combined GraphQL query for pull requests and rollups.

## Findings

| Question | Result | Basis |
| ---- | ---- | ---- |
| Do check events for fork heads list their pull request? | **No.** Both check runs on pull request 39's head carry an empty `pull_requests` list. | REST check runs for `5feac03…` on the upstream. Phase 4's association by indexed head SHA is required, as designed. |
| Where do those check runs live? | **On the upstream.** The same SHA has no check runs on the fork. | Same read against both repositories. Check deliveries for task pull requests arrive for the upstream repository, where the service's reader has checks access. |
| Is the rollup the same set as the REST check runs? | **Yes** for an unrerun head: the same two check run IDs, CheckRun entries only. | GraphQL `statusCheckRollup` against REST `check-runs` for pull request 39. |
| Does REST agree with GraphQL on line counts? | **Yes**: 741 additions, 36 deletions from both, same `updated_at`. | Pull request 39. |
| Who changes stage labels? | `concurro[bot]`, as two events about two seconds apart: the new stage label is added, then the old one removed. | Issue 34's events. Between the two an issue carries two stage labels; `stage_label` returns the first it finds. Phase 3 must apply both deliveries and tolerate the intermediate state exactly as today's list does. |
| Does every vault credential change reach the registry? | **Stored changes do, by polling; expiry does not.** The vault reports a credential as invalid once its known expiry passes without storing anything or emitting an event (`gateway/vault/vault.py`, `Store.metadata`). For stored changes: `Shadow.poll_credentials` consumes the vault's event feed from `Worker.observe_policies`, which runs at start and from every `Recovery.poll`. `Registry.credential_events` then rewrites every sponsorship document. Polling is scheduled, not guaranteed: an error or a long recovery pass delays it. | Code reading: `service/shadow.py`, `service/worker.py`, `service/registry.py`. |
| Is credential coverage in the sponsorship view registry state? | **No.** `Shadow.view` reads it from the vault on each `/sponsorships` call. | `service/shadow.py` `coverage`, `view`. |
| What do delivery-history entries carry? | `event`, `action`, `delivered_at`, `installation_id` and a numeric `repository_id`; a redelivery keeps its GUID. | GitHub's documentation, checked in review. The capture below confirms it against live entries. |

### Consequences for the briefs

- **Credential expiry (phase 1):** nothing signals it, so the credential coverage inside a
  saved sponsorship list can stay "available" after an expiry until another sponsorship
  signal or a refresh. No list view displays coverage today; the task and issue pages
  that do read it live. This is accepted as a documented limitation. A list view that
  starts displaying coverage must first add invalidation at the known expiry.
- **Sponsorship freshness (phase 1):** a saved sponsorship list would show credential
  coverage as of its last read. Two signals keep it current for stored changes: `credential_events`, emitted
  only when a batch contained events (it rewrites every document on every call, which
  would otherwise signal every five minutes), and the backoffice's own credential writes,
  which should outdate saved sponsorship lists directly rather than wait up to one
  recovery interval.
- **Rollup reads (phase 4):** keep the combined GraphQL listing for rebuilds; read single
  rollups only for check events and head changes.

## Host results

Captured on the live service at `cf29ba9` on 2026-11-14 with the script below, unchanged;
the output is [phase-0-host.json](phase-0-host.json). A supplemental capture and one
authorized rerun of `Demo Rust` on pull request 39 were run by Codex and are quoted here
from its report. No service was restarted and no App setting or grant was changed.

| Check | Observed | Reading |
| ---- | ---- | ---- |
| App subscriptions | All six applied event families | Sufficient for the projection. |
| Installation grants | Read permissions present for both roles | No expansion needed. The upstream installation reports no `push` or `workflow_run` subscription; the projection needs neither there. |
| Fork `workflow_run` deliveries | 37 each of requested, in progress and completed | Confirmed. Three sampled payloads carried every run field the list shows, at attempt 1. |
| Bot-sent stage-label deliveries | 5 of 5 sampled were sent by the bot and named a stage label | Confirmed in the sample. |
| Line counts in `synchronize` | 1 retained sample: payload and REST both 1300 additions, 92 deletions, same head | Confirmed for that sample; phase 4 keeps a parity test. |
| History entry fields | `event`, `delivered_at`, `repository_id` on 657 of 657; `action` on 578 | The 79 without `action` are `push` deliveries, which have none. |
| History scan | 657 delivery GUIDs in 2.3 seconds | One scan, not a worst case. |
| Applied deliveries | 555 over a window spanning three partial days (70, 332, 153) | Not a daily rate. All were acknowledged with 202, which says nothing about application. |
| Delivery detail size | 11 successful detail reads, plus six supplemental reads whose sizes were measured: 14.6 to 35.5 KB | The samples overlap, so these are not 17 distinct payloads. The measured six are far below the 1 MB client limit; that is not a bound for all payloads. |
| Check deliveries | 5 of 5 sampled carry `head_sha` and list no pull request (1 upstream, 4 fork) | Supports association by head SHA. |
| Rollup after a rerun | REST `filter=all` returned four check runs (both attempts); the rollup returned only the two of the latest attempt, during and after | Reading the rollup is required: assembling CI from every observed check run would be wrong. |

## Still open

- Payload shape and size for rerun attempts and for events not sampled.
- A working day of list reads and end-to-end staleness. The backoffice does not log
  successful GitHub reads today, so this needs the counting added in phase 1.
- Recovery under load, secondary-limit behaviour, and the projection's memory and
  concurrency once it exists.

### Capture script

[`phase-0-capture.py`](phase-0-capture.py) reads only: App metadata, the delivery history
the recovery job already scans, at most 15 delivery details, and one repository or pull
request read per sample. It mints a read-scoped installation token in the normal way,
which is a POST but changes no repository data or grant. Its output holds counts,
identifiers and numeric fields: no tokens and no payload text.

```bash
docker compose exec -T service python3 - /etc/concurro/service/settings.json < docs/proposals/list-index/phase-0-capture.py > phase-0-host.json
```

## Not established

- Live traffic against GitHub's secondary limits.
- Queue, publication and shutdown behaviour of the projection: these are proposed, not
  implemented.
