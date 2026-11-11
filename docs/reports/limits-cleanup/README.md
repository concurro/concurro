# Limits cleanup

The cleanup removes fixed work-shape ceilings from model schemas, validators, configuration
lists, pagination, revision grants, report parts and queued events. Spend and execution
budgets remain operator choices. Native snapshot operations remain; the unused repository
MCP server and its private quotas are removed.

Large plans, candidate descriptions, target authority and queued events reuse verified
report artifacts. Views shorten raw text before rendering, preserving engine-generated disclosures, fences
and headings. Compact PR bodies retain controller-written workflow/policy notices and
dependency summaries. Canonical evidence survives reload, including full rejected drafts and
design, quality and outcome history. Large outcome collections also use report artifacts. This requires
checkpoint wire format 7 when these additional fields are detached. Older engines must not
resume those checkpoints. Publication bodies consistently use a conservative 60,000 UTF-8
byte allowance; PR titles use 256 characters. These are publication capacities, not limits
on accepted model prose. Existing model-response, extraction and transport bounds remain.

`turn_timeout_seconds` accepts a positive integer or `null`, defaulting to 1200 seconds for
intake, planning and design review. `max_retained_bytes` similarly controls uncompressed
transcripts, defaulting to 64 MiB. Compressed archive and exchange capacities are independent.
No new per-file or list-count settings are introduced. See [configuration](../../concurro.md).

## Context measurement before changing accounting

Target: `concurro-demo-fork/stacks-core`, commit
`e296939a775a9934f4da1a1f11d566b938aa8c25`, the remote of the local
`concurro-demo/fork` checkout. Its current configuration has no obsolete execution settings;
no target migration is needed. The older local checkout was not used as authority.

The fixture uses the pinned target's actual config, charter, role instructions and policy
documents. A synthetic documentation task proceeds through intake, planning, both design
reviewers, implementation, PR reviews and one repair. All model answers and GitHub writes
are fake. These are reproducible prompt-size measurements, **not live task telemetry**.
CLI-added framing, tool results and retained conversation history are outside the measurement.

| Engine state | Controller context | Complete constructed prompt | Minimum headroom at 350,000 bytes |
| ---- | ---- | ---- | ---- |
| Before cleanup (`7e39bc4e954744c65afc1b1ed2b4f8ee78e16ca6`) | 52,183–57,577 | 65,482–72,185 | 277,815 |
| Cleanup, original accounting | 52,183–57,577 | 65,453–72,065 | 277,935 |

The actual target's authority fits comfortably for this fixture. It does **not** prove that
all existing tasks fit. Synthetic contexts padded to exactly 350,000 bytes exceed that
budget by roughly 13–15 KB once instructions, schemas and prompt structure are included.
Changing the setting's coverage must therefore be reviewed separately and documented as a
behavior change. The configured budget has not been raised silently.

[Before cleanup](context-before.json) and [after cleanup](context-cleanup.json) include each
stage, profile, size and authority-file SHA-256. `measure_context.py` uses the production
controller, bundle and prompt builder, including repair feedback indexes.

To reproduce, fetch the paths listed in the JSON's `files` map from the pinned target commit
into a temporary directory, preserving their relative paths, then run with Python 3.12 and
the service requirements installed:

```sh
python docs/reports/limits-cleanup/measure_context.py \
  --target /tmp/concurro-limits-target \
  --output /tmp/context-measurement.json
```

To reproduce the baseline, copy the limits-cleanup commit's version of the measurement script
into the original commit checkout. The combined version uses the test helper in
`engine/tests/support.py`; run it from this worktree for expanded accounting. No provider credentials or
live inference are required. Prompt sizes may change with future bundle instructions; the
report records the bundle identity rather than assuming a permanent number.

## Validation

Regression coverage crosses the removed limits: more than 32 criteria, 20 findings,
10 policy documents, 32 globs, 128 artifact parts, eight pending events and 100 pagination
pages. It covers long Unicode plans, descriptions and paths; large authority files;
uncapped named model/reviewer lists; positive/null timeout and transcript settings; and
exact save/load recovery. Structural invalidity, independent reviews and operator-selected
minima remain tested. Oversized view tests parse the generated Markdown, and history tests
verify full text and repeated outcomes survive restart. Shared constants define publication
capacity, transcript retention and turn timeout defaults. The legacy exchange `capacity`
field remains a readiness signal: zero pauses claims; positive values allow one active turn
per session. No blanket schema lint or additional policy gate was introduced.

## Integration with local main

The branch now includes local `main` commit
`0f828d72de1b18d1318f979431f61ca92a05834f`. Its prompt headings, role descriptions,
self-contained evidence rules and response-schema classification rubric are preserved.
The cleanup removes prose/count caps within that new structure; it does not restore the
old duplicated guidance. The earlier measurements above remain historical evidence.

The same pinned target and offline lifecycle were remeasured after integration:

| Engine state | Complete constructed prompt | Minimum headroom at 350,000 bytes |
| ---- | ---- | ---- |
| Local main, before cleanup | 63,998–69,785 | 280,215 |
| Cleanup, original accounting | 63,994–69,685 | 280,315 |

[Local-main baseline](context-main-before.json) and [integrated cleanup](context-main-cleanup.json)
record the new bundle identities and unchanged target file hashes. The headline prompt
uses 160 characters as a soft preview target; longer output remains accepted and retained.

## Separately reviewable expanded accounting

The limits cleanup and expanded context accounting are separate commits, each with its own
tests and measurements. Review the cleanup first, then accounting; the combined changes
replace per-resource instruction ceilings with one complete-prompt budget.

Admission now shares the runner's prompt builder. The JSON contains instructions (including
target authority), schema, model-visible context, repository metadata, authorization and
feedback indexes. Workspace and feedback paths are relative to the runner's working directory,
so temporary path names do not change its measured size. Complete feedback remains in verified
files; its prompt index is counted. The prompt and runner share the same directory-layout
constants, with a regression test that relocates both directories. Subsequent tool reads, CLI framing and retained history
are outside this setting. Retained sessions are budgeted against the full constructed prompt
even when the harness sends an incremental update.

[Expanded-accounting measurements](context-expanded.json) use the same target and fixture.
No target setting was changed.

| Stage | Largest prompt before cleanup | Largest prompt with cleanup and expanded accounting |
| ---- | ---- | ---- |
| Intake | 65,482 | 65,437 |
| Planning | 67,499 | 67,258 |
| Design review | 68,616 | 68,357 |
| Implementation / repair | 72,185 | 72,049 |
| PR review | 67,726 | 67,668 |

All measured lifecycle prompts fit the existing 350,000-byte budget, leaving at least
277,951 bytes. The padded near-limit cases exceed it by 13,254–14,912 bytes. A regression test
proves that a context which formerly fit is refused before charging or dispatching a call,
and that the exact full-prompt boundary is accepted. The diagnostic names the stage and
reports the measured size. Operators with near-capacity stages must deliberately adjust
`max_context_bytes` or reduce supplied context when adopting this accounting change.

After integrating local main, [expanded accounting](context-main-expanded.json) measures
63,978–69,669 bytes, leaving at least 280,331 bytes at the unchanged target budget. Padded
near-limit contexts still exceed it by 10,900–13,477 bytes, so the accounting change remains
separately reviewable.

## Final checks

After integrating local main, the combined engine suite passed 1,006 tests with one platform
skip, and the service suite passed 347. The unchanged gateway suite previously passed 58. Ruff lint/format checks and the repository's configured
Pyright checks passed. The cleanup-only index export independently passed all 1,001 engine tests with one platform
skip, plus lint, formatting and type checks. All tests used fake
providers and GitHub transports; nothing was deployed or published.

Integration also passed 81 focused prompt, schema, target-policy, dashboard and accounting
tests. Four dashboard snapshots were reconciled with main's numeric evidence-link ordering;
the final full-suite results above include those corrections. The later soft headline-guidance
change was remeasured and passed the same 81 focused tests; full suites were not repeated.

```sh
PYTHONPATH=engine:engine/tests python -m unittest discover -s engine/tests
PYTHONPATH=engine:engine/tests:service/tests python -m unittest discover -s service/tests
PYTHONPATH=gateway python -m unittest discover -s gateway/tests
ruff check engine service gateway docs/reports/limits-cleanup
ruff format --check engine service gateway docs/reports/limits-cleanup
pyright
```

Review rendering format 5 applies consistent byte accounting to new records. Previously
queued formats retain their original rendering so retries can match already-published
Unicode bodies exactly. This is independent of checkpoint wire format 7, which permits
the additional artifact-backed state fields.
