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
resume those checkpoints. Comment/PR bodies and human-readable artifact views use separate
60,000 UTF-8 byte allowances; immutable data parts now use 256 KiB (see consolidation below).
PR titles use 256 characters. These are publication capacities, not limits on accepted model prose. Existing model-response, extraction and transport bounds remain.

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

## Completed-response retention follow-up

Issue #35's audit revision 127 and completed Sol call 23 were replayed offline through
`Controller.complete`, with GitHub writes disabled. The response digest matched its
session completion receipt (`48cbd46560a847a17731cf08145f3791796620f9d949efa1616ebc88d33b3e4c`).
With the active reviewer selected, the pre-cleanup checkpoint measured 56,409 bytes and
discarded the response; `ff8339a` measured 43,429 bytes and retained it (44,094 bytes
after completion). The cleanup's detached plan already removes #35's immediate size pressure.
This checks retention, not acceptance of the review or live recovery.

A longer synthetic history still reproduced the same false "uncertain twice" block
on `ff8339a`. Completed responses now survive the former 56,000-byte reserve, and large
quality/design/review histories and report indexes reuse format-7 artifact storage.
Existing report references remain reusable inside those collections. Regression tests
cover serial reload, parallel completion, a crash before applying votes, unchanged spent
calls and complete history round trips. Legacy checkpoints already missing a response
still follow the existing uncertain-outcome recovery path.

Retention-only validation: engine 1,009 tests (one skipped), service 347 and gateway 58 passed;
Ruff lint/format, Pyright, compileall and whitespace checks passed. The bundle identity
is unchanged. The three new regression tests fail against the pre-fix implementation.
The 48-call capacity tests now retain all timing receipts through the detached report
index; the existing metadata-only no-rewrite and legacy-format tests pass unchanged.
No pilot state was changed, resumed or deployed.


## Fixed-position history blocks

Large detached lists now use eight-entry blocks and an ordered manifest of their digests,
artifact numbers and entry counts. Eight is storage granularity, not a task limit.
Appending rewrites the tail block and manifest; updating old timing, classification or
other metadata rewrites its block and manifest. Canonical report bodies remain shared.
A separate metadata index was avoided because existing records can change without a
stable entry ID; it would introduce a second growing structure and synchronization.

`REPORT_INDEX` and its trimming are removed. Publication/reload tests retain every entry
through 48 calls, including all new timing receipts. A 600-entry test proves the dashboard
can shorten its view without removing index entries. The read-only design audit now uses
the checkpoint reference-restoration helper, so it can read detached indexes and histories.
Older checkpoints may already have lost index entries; this does not reconstruct them.

Block records use the already-unreleased checkpoint format 7, with no format 8.
Existing whole-list artifacts remain readable and reusable. If their creation was
interrupted, recovery finishes the original encoding under its existing digest marker.
New tests cover unchanged saves, stable block boundaries after old metadata grows,
missing/reordered blocks, malformed layouts before child fetches, cold reload, and lost
block or multipart-manifest replies. Existing response-retention, spent-call, approval,
current-rating and crash-before-application tests remain in place.

### Measured artifact traffic

[Raw results](history-traffic.json) compare the same workload with block encoding enabled
and disabled. Four collections share identical synthetic entries, exercising cross-field
and canonical-report reuse. Each entry carries short metadata, timing and a unique report.
There is one real checkpoint save per append, one cold reload and one unchanged save,
using fake GitHub only. The disabled-block results reproduce the pre-block implementation's
artifact counts and bytes exactly. These are synthetic storage measurements, not pilot
traffic predictions.

| Entries per collection | Encoding | New data artifacts | Data-body bytes | Data GETs | Peak audit bytes |
| ---- | ---- | ---- | ---- | ---- | ---- |
| 48 | Whole snapshots | 135 | 1,504,421 | 186 | 46,655 |
| 48 | Blocks | 133 | 432,461 | 188 | 46,561 |
| 192 | Whole snapshots | 1,299 | 25,340,710 | 1,502 | 46,655 |
| 192 | Blocks | 565 | 2,232,542 | 782 | 46,561 |

Bytes count complete immutable artifact bodies, including canonical evidence, manifests,
fences and links; mutable audit versions are excluded. GETs include write confirmation
and the cold reload. Neither encoding writes new data on the final unchanged save.
Blocks can increase cold-read counts slightly on shorter histories; the existing verified
cache still avoids repeated data reads across warm ticks. Manifests contain references,
but grow and are rewritten: this reduces history duplication without eliminating all
quadratic growth in reference metadata. No old immutable artifacts are deleted.

Reproduce from the repository root:

```sh
python3 engine/tests/storage_traffic.py --entries 48 192 --whole-snapshots --snapshot-manifests --chunk-characters 16000
python3 engine/tests/storage_traffic.py --entries 48 192 --snapshot-manifests --chunk-characters 16000
```

Final validation: engine 1,021 tests (one platform skip), service 347 and gateway 58
passed. Ruff check/format, Pyright, compileall, bundle comparison and `git diff --check`
passed. The bundle remains `837b6a126a52294a556a8e5a507cb50425b04ec97f3d5e79aba1d66a29574b45`.
The offline #35 completion replay still retains the response (43,429 bytes before,
44,094 after). The initial block tests demonstrated the missing layout/traffic behavior
before implementation; the audit-reader test also failed before its hydration fix.
All changes remain uncommitted; no deployment, pilot command or live task mutation occurred.

### Review follow-up: inline-only pressure relief

Timing and classification relief now use the actual packed layout. Detached timings,
ratings, snapshots and assessment links remain intact; classifications carried inline
beside individual report references remain eligible under the existing protection rules.
A 59,500-byte checkpoint regression verifies completed-response retention, unchanged
external metadata, no new data artifacts, and an exact reload. Inline relief is also
covered, including a shared classification value whose detached copy must not change.
Block detection and publication use one record encoder, with an alternate-indentation
replay test. Existing artifact bytes and measured traffic remain unchanged.

Follow-up validation: engine 1,025 tests (one platform skip), service 347 and gateway 58
passed, together with Ruff check/format, Pyright, compileall, bundle comparison and
`git diff --check`. The four new tests cover the pressure and encoding cases above;
the pre-fix run reproduced both detached-metadata failures. The traffic workload's
artifacts, bytes, reads and audit peaks match the recorded results exactly.


## Artifact consolidation

New immutable data parts use a 256 KiB **complete-body** allowance, independent of comment
and view sizes. Larger values span parts. This is an operating choice tested against the
service's existing 16 MB artifact-response allowance, not a claimed GitHub maximum.
Large parts use plain-text fences, avoiding JSON syntax-highlighting expansion. Tests cover
a ten-artifact page with HTML escaping and an immutable artifact's version response without
raising transport limits. This is offline validation, not a new live GitHub size probe.

The mutable audit retains its 60,000-byte target because GitHub returns repeated versions
with rendered HTML. If packed metadata exceeds that target, a verified immutable envelope
holds it and the audit stores a small pointer. Completed responses, findings, approvals and
spent budgets survive a cold reload. Publication still prepares recovery data before branch
or PR writes; metadata above the audit target no longer causes a capacity refusal by itself.

Growing lists now keep their directory and unfinished tail in the mutable audit. Only full
eight-entry blocks become immutable artifacts; report bodies in the tail remain references.
There is no new manifest or partial-block artifact per append. An older entry changing
rewrites its block. Existing whole-list and manifest encodings remain readable and reusable;
interrupted writes retain their original part boundaries and exact bodies. These layouts
share the unreleased format-7 boundary; no format 8 or bundle change is introduced.

### Traffic comparison

[Raw results](artifact-consolidation-traffic.json) use the same synthetic four-collection
workload as above, including cross-collection reuse. Before means 16,000-character parts
and immutable manifests; after means larger parts and the mutable directory/tail. GETs
include write confirmation and one cold reload. Unchanged saves create no new artifacts.

| Reports per collection | Report size | Encoding | Data artifacts | Data-body bytes | Data GETs | Peak audit bytes |
| ---- | ---- | ---- | ---- | ---- | ---- | ---- |
| 48 | About 3 KB | Before | 133 | 432,461 | 188 | 46,561 |
| 48 | About 3 KB | After | 54 | 213,727 | 108 | 49,124 |
| 192 | About 3 KB | Before | 565 | 2,232,542 | 782 | 46,561 |
| 192 | About 3 KB | After | 216 | 855,789 | 432 | 49,124 |
| 48 | About 96 KiB | Before | 424 | 5,211,767 | 767 | 58,432 |
| 48 | About 96 KiB | After | 54 | 4,778,719 | 108 | 49,124 |

The 54 artifacts are **48 canonical reports plus six history blocks**, all on GitHub.
The workload also has one mutable audit artifact. It does not publish the additional
human-readable report archives that a live issue would have; these counts are not total
pilot artifact counts. Existing artifacts are neither deleted nor retrospectively consolidated.

The tradeoff is larger individual reads and slightly larger mutable versions because the
short tail and directory live there. The directory still grows; if it eventually needs the
overflow envelope, changing that envelope writes a new immutable metadata snapshot. Canonical
reports and closed blocks are still reused. Preflight can leave unused immutable data after
an abandoned publication, as before. This reduces unnecessary artifacts without promising
constant storage or a single artifact for a whole task.

Reproduce:

```sh
python3 engine/tests/storage_traffic.py --entries 48 192 --snapshot-manifests --chunk-characters 16000
python3 engine/tests/storage_traffic.py --entries 48 192
python3 engine/tests/storage_traffic.py --entries 48 --report-characters 98304 --snapshot-manifests --chunk-characters 16000
python3 engine/tests/storage_traffic.py --entries 48 --report-characters 98304
```

Validation: engine 1,025 → 1,041 (one platform skip), service 347 → 348, gateway 58 → 58;
all passed. The initial regressions reproduced multipart proliferation and oversized-metadata
refusal; the directory test separately reproduced per-append artifact churn before its fix.
Coverage includes exact legacy and lost-response recovery, Unicode/fence byte accounting,
compact overflow with plan binding, malformed/tampered/missing metadata, directory and tail
integrity, publication ordering, cold reload and unchanged saves. The full-call-budget tests
retain complete responses, active ratings and timing. The offline #35 replay remains
43,429 bytes before and 44,094 after, with the response retained and one save.

Ruff check/format, Pyright, compileall, bundle verification and whitespace checks pass.
The bundle is still `837b6a126a52294a556a8e5a507cb50425b04ec97f3d5e79aba1d66a29574b45`.
Changes remain uncommitted. No deployment, pilot resume/retry or live artifact mutation occurred.

### Baseline acceptance review

Reviewed the pending implementation after proposal commit `f56d635`, against Claude's
reviews supplied in the conversation. No blocking correctness defect found.

| Scope | Review status |
| ---- | ---- |
| Completed-response retention, history blocks and removal of `REPORT_INDEX` | Claude signed off, including the subsequent inline-only metadata relief and shared record encoder. Those fixes remain present. |
| 256 KiB complete-body parts, mutable history directories/tails and immutable audit overflow | Requested implementation, reviewed here with full validation. The supplied conversation does not contain an explicit Claude sign-off on this later code; approval of the proposal is separate. |
| Comment encoding, response-budget packing, combined readable/canonical artifacts, dashboard witnesses and legacy-plan replacement | Proposal only; none is implemented by this baseline. |

Validation rerun on the pending baseline:

| Check | Result |
| ---- | ---- |
| Engine | 1,041 tests, passed; one Linux orphan-adoption test skipped on macOS |
| Service | 348 tests, passed |
| Gateway | 58 tests, passed |
| Ruff check / format check | Passed; 233 Python files formatted |
| Pyright | Zero errors or warnings |
| Compileall / whitespace | Passed |
| Bundle | Every resource hash matches HEAD; bundle identity unchanged |
| Traffic reproduction | All six before/after cases match the recorded artifact counts, bytes, GETs and audit peaks exactly |
| Offline #35 replay | Completed response retained; 43,429 → 44,094 audit bytes, one save invocation |

The #35 replay uses captured inputs and a mocked save; the regression suites separately
exercise real checkpoint saves against fake GitHub, cold reloads and lost-write recovery.
No live provider, GitHub mutation, deployment or pilot command was used. Linux CI remains
the check for the platform-specific skip. The interrupted lodestone search returned no
results; this review used the source, diffs and tests directly.

Remaining tradeoffs are unchanged: separate readable archives still exist, discovery can
scan collections, and overflow-envelope changes create immutable snapshots. The newer
format-7 layouts require readers from this baseline; an earlier format-7 implementation
is not a sufficient rollback target merely because its version number matches.

## Phase 1: compact audit encoding and recovery

Implemented on baseline `064a3b5`, pending Claude review. New audit JSON uses a versioned
HTML comment and escapes `>` after JSON serialization. Readers retain fenced formats and
exact legacy migration proofs. Audit and plan GETs request one version; historical pages
are fetched only for an older pointer, legacy migration or an unbound plan's origin.

Dashboard publication now reads back the comment. Confirmed/pending pointer receipts ride
in the next ordinary checkpoint; they introduce no dedicated audit save. A crash before
that save can recover the candidate from the newest durable revision. An old-pointer
readback retains the pending candidate. Recovery can update visible content under that same
pending pointer before advancing to another. Freshness compares rendered content without
its validated pointer, so receipt-only saves do not cause an idle rewrite loop.

An unbound plan whose origin is outside the readable window is left untouched. A replacement
intent is checkpointed before creating a view from authoritative task state. Recovery binds
that one replacement after a lost creation/binding reply; no planning or approval repeats.

### Offline traffic

[Results](audit-phase1-traffic.json) compare identical inputs against committed baseline
`064a3b5`: 48 entries in each of four histories, one plan and a dashboard, followed by ten
cold idle save cycles. Responses model the measured 30-version default and 100-version
window. Byte counts use JSON plus CommonMark rendering **without GitHub's syntax highlighting
or sanitization**; they are comparative fixture measurements, not live transport predictions.

| Measurement | Baseline | Phase 1 |
| ---- | ---- | ---- |
| Artifacts: reports / history / audit / plan | 48 / 6 / 1 / 1 | 48 / 6 / 1 / 1 |
| GETs (including collection pages) | 785 | 835 |
| Collection GETs | 63 | 63 |
| POSTs | 57 | 57 |
| PATCHes | 105 | 97 |
| Uploaded JSON bytes | 1,238,702 | 1,261,099 |
| Modeled response bytes | 144,705,875 | 20,739,758 |
| Peak audit body bytes | 49,590 | 49,851 |

The tradeoff is extra dashboard readbacks and receipt bytes, with much smaller audit reads
and fewer redundant dashboard PATCHes. Artifact counts are unchanged in this phase. There
are no combined report views, new part-size rules or reduced discovery scans yet. The
traffic run injects no interruption; separate regression tests cover lost writes and prove
one replacement plan, with the unverifiable original intentionally retained.

```bash
python3 engine/tests/audit_traffic.py --entries 48
```

For the baseline, copy this script into an archive of `064a3b5` and run the same command.
No live GitHub requests, deployments or pilot commands are used.

Phase 1's regressions cover hostile comment syntax and literal escapes, pagination and
concurrent changes, 100+ revisions with confirmed/pending witnesses, failed readback,
crash before receipt persistence, idle freshness, overflow receipts, and lost replacement
creation/binding replies. Existing tests retain legacy migration and provenance checks.
Every checkpoint save now writes format 7. After deployment, **every task that saves**
requires this phase's reader for rollback, not only tasks using large reports or detached
layouts. The baseline's format-7 reader alone cannot read the new comment encoding and receipts.

### Phase 1 validation

| Check | Baseline | Phase 1 |
| ---- | ---- | ---- |
| Engine suite | 1,041 | 1,059, passing; one Linux-only test skipped on macOS |
| Service suite | 348 | 348, passing |
| Gateway suite | 58 | 58, passing |
| Ruff 0.17.4 check / format check | Pass | Pass |
| Pyright 1.1.408 | Pass | Zero errors and warnings |
| Compileall / whitespace / changed-doc local links | Pass | Pass |

The initial nine phase-1 regression cases were written before implementation; eight failed
against the baseline. The final module has 18 cases. Existing contract tests now expect
format 7 and comment encoding; view-repair assertions exclude only publication witnesses,
retaining exact task state and approvals. The capacity test no longer requires metadata
loss when compact overflow avoids trimming. Dashboard goldens and bundle resources are unchanged.

Bundle identity remains `837b6a126a52294a556a8e5a507cb50425b04ec97f3d5e79aba1d66a29574b45`.
The recorded deployed ref is `7e39bc4e954744c65afc1b1ed2b4f8ee78e16ca6`; this implementation
extends the unreleased format-7 baseline. No live deployment state was changed or probed.
