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

Run the script from the original commit to reproduce the baseline (copy only this measurement
script there); run it from this worktree for the cleanup result. No provider credentials or
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

The integrated cleanup-only export passed 1,001 engine tests with one platform skip, plus
lint, formatting and type checks. Service validation passed 347 tests.
