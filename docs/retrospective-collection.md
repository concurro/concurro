# Retrospective collection

**Status (2026-11-24): legacy phase-0 collector, not a collector for the Rust stack.**
Snapshot replay remains useful offline. Fresh collection below requires Python-era service
settings, diagnostics and exchange interfaces; adapting it to Rust task records and
archives remains deferred. Do not recreate retired services just to run these commands.

`service/retro.py` gathers what happened in a target's tasks into per-task dossiers and
cross-task metrics. It is phase 0 of [retrospective tuning](proposals/retrospective-tuning.md):
operators read the output to judge whether there is enough signal for an analyst. It makes
no model calls and writes nothing of its own outside its two output directories. Reading the
exchange's task observations lets the exchange first import pending native observations into
its own database, as the service's reads of the same observations do.

## Running a collection

Run it where the service's settings, App key and diagnostics directory are available, for
example inside the service container, with both directories on a mounted volume. It doesn't
use the daemon's control socket, so the daemon may be running or stopped.

```bash
python3 -m service --settings /absolute/path/to/settings.json retro-collect OWNER/FORK \
  --since 2026-11-01 --snapshot SNAPSHOT --output OUTPUT
```

`OWNER/FORK` names a configured target by its fork or upstream. `--until` defaults to now.
Both directories must be new. The snapshot holds every read; rebuild the output from it
anywhere, without credentials or network access:

```bash
python3 -m service.retro --snapshot SNAPSHOT --output NEW_OUTPUT
```

## Sources

| Source | Read through | Content |
| ---- | ---- | ---- |
| GitHub | The App's read installation tokens | Enrolled issues, task artifacts, issue comments, the task PR's commits, reviews and comments, and the fork's configuration at each task configuration commit |
| Run diagnostics | `diagnostics_dir`, read-only | Allow-listed turn fields only |
| Exchange | The operator channel's task observations | Allow-listed inference fields, including token usage |

Diagnostics rows and exchange observations keep identifiers, timings, sizes, token counts and
values from fixed code sets: stage, provider, effort, status, outcome, termination reason,
observation source and category. Any other value becomes `other`. Profile and model names are
kept only when one of the task's configuration commits declares them; this is checked before
anything is saved, so an undeclared name never reaches the snapshot. A legacy configuration
declares its harnesses as profiles, with their models and the QC model, and its turns, which
carry no agent record, take their harness as profile. Requests, prompts, feedback, specs,
responses, workspaces and transcripts are never read. Turns recorded for operator evaluations
are excluded. The exchange's report of requests still awaiting an observation, and of native
observation availability, is kept.

## Population

A run covers enrolled contribution tasks, found by their current `concurro` label, that
either reached a terminal stage (merged, observed merged or closed) inside the window, or are
currently suspended or judged (blocked, stopped, awaiting credentials, budget exhausted,
rejected or escalated), whenever they stopped. The terminal time is the PR's merge or close time, or the issue's close time for a
task closed without a PR. `run.json` lists every enrolled issue with the reason it was
included or excluded.

## Output

`run.json` holds the snapshot's source metadata, the population, metrics and limitations.
`dossiers/` holds one file per included task:

- `task`: stage, reason, call and revision counts, the observation cutoff, and when every
  PR reviewer last approved the final head.
- `segments`: the configurations the task ran under, in order, with roles and profiles read
  from the fork at each configuration commit. `configuration_history` says whether they are
  complete.
- `observations`: indexed reports with correction codes, the finding ledger with each
  finding's recorded dispositions, turns with token usage, the PR and its human activity,
  and human comments on the issue.
- `candidates`: human commits and reviews on the PR, and findings from different reviewers
  on overlapping lines of the same head. These need interpretation; the collector doesn't
  classify them.

Segments come from every saved version of the task's audit artifact, not only the latest
state: `/concurro invalidate` drops the current resume receipt, so the latest state can't show
an earlier resume. A segment is a bundle and configuration digest. A new one starts where a
saved version first shows a different one, at the time of the resume or invalidate comment
that caused it. The history is incomplete when its pages don't match the verified current audit, as
checkpoint recovery requires, when any version from the current one down to the first is
missing, or when a version is unreadable or written by another account. Reading every version makes snapshots grow with a task's number of saves.

Each report, finding and turn names the segment it ran under and how that was decided:

| Method | Evidence |
| ---- | ---- |
| `only` | The complete history has one segment |
| `turn` | A report linked to a recorded turn takes that turn's segment |
| `execution` | In a complete history, exactly one segment fits the record's profile, model, effort and bundle, among segments not resumed after the record existed |
| `unknown` | Several segments remain, or the configuration history is incomplete; candidates are listed |

A finding takes the segment of the review that raised it, identified by the verified review
report's finding IDs. A classification's configuration binding is not used: it records the
approval configuration, which resume preserves.

Metrics group turns, reports, correction codes and findings by profile. Sums report how
many records they cover: missing diagnostics, usage or timing are unknown, not zero. Token
usage counts the first observation with usage for each observed inference request. Observed
totals include every observed request and may undercount. Complete-turn totals count a turn
only when every observed request has usage, the exchange reported no request awaiting an
observation for the task, and native observations were not unavailable.
