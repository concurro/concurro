# Design-review baseline: pilot 19

Collected 2026-11-07 from [pilot #19](https://github.com/concurro-demo-upstream/stacks-core/issues/19),
using GET requests only. This baseline measures output-correction overhead before
[correction-aware review eligibility](../design/design-review-corrections.md).

## Coverage

The frozen [audit artifact](https://github.com/concurro-demo-upstream/stacks-core/issues/19/artifacts/1)
is version 113. The [machine-readable report](design-review-baseline-19.json) records its
body hash, bundle identity, invalidation boundary and exact identities and digests for
all 38 artifacts read. All 15 indexed design-review archives and their referenced report
parts verified successfully. No raw report bodies are duplicated here.

Coverage is this audit's report index. The index is bounded, so this method is not a
claim of exhaustive history for arbitrary tasks. No other pilot was sampled, and no
current pilot was changed. The engine commit is not recorded by this dataset; the bundle
identity is not an engine SHA.

## Observations

| Run / round / phase | Accepted outputs | Rejected outputs | Total outputs | Open objection dispositions |
| ---- | ---- | ---- | ---- | ---- |
| 1 / 1 / cooperative | 2 | 8 | 10 | 0 |
| 1 / 1 / fresh | 2 | 1 | 3 | 0 |
| 2 / 1 / cooperative | 2 | 0 | 2 | 0 |
| Total | 6 | 9 | 15 | 0 |

All six accepted reviews approved. The nine rejections comprise seven `review_evidence`
and two `review_shape` diagnostics. The second run follows an explicit writer invalidation;
there were zero substantive plan revisions. Round numbers in the generated report are
relative to the run, preserving the original durable labels alongside them.

The first run's fresh phase produced no objections in either accepted review:
[Opus](https://github.com/concurro-demo-upstream/stacks-core/issues/19/artifacts/35) and
[Sol](https://github.com/concurro-demo-upstream/stacks-core/issues/19/artifacts/37).
That supports investigating redundant review after corrections; it does not show that
fresh review after substantive negotiation is unnecessary.

These are **archived output counts**, not a reconstruction of every provider attempt.
The audit records 24 total model calls across all stages, but does not provide enough
per-attempt data here to equate every rejected output with a correction call. Replay is
not counted again. Tokens, model duration, setup/queue time and dollar savings are
unavailable. No five-minute or ten-minute saving is claimed from this dataset.

There are no disputes here from which to assess deadlock, repeated fresh findings or
planner/reviewer convergence. Further baselines should include pilots with substantive
objections and inspect their linked explanations; new IDs alone do not prove new defects.

## Reproduction

From the repository root, capture an issue's current audit and its indexed design reports:

```bash
python3 engine/design_audit.py \
  --repository concurro-demo-upstream/stacks-core --issue 19 \
  --snapshot /tmp/concurro-design-review-19 --fetch \
  --output /tmp/concurro-design-review-19.json
```

The transport permits only artifact GETs using the existing `gh` login. It caches each
response once; it never invokes models or changes GitHub. Reproduce the same capture
offline by repeating the command without `--fetch`:

```bash
python3 engine/design_audit.py \
  --repository concurro-demo-upstream/stacks-core --issue 19 \
  --snapshot /tmp/concurro-design-review-19 \
  --output /tmp/concurro-design-review-19-replay.json
```

Use a new snapshot directory for a new observation. A later capture may see a newer audit
version; compare the source manifest before treating it as the same baseline. Missing,
edited or unverifiable reports are marked unavailable, never treated as clean approvals.
Snapshot collection time and source commitments are retained in the derived JSON; raw API
responses remain in the local snapshot, outside the repository.

## Timing in new captures

The timing-only change adds optional exchange observations to the current operation and
existing report-index entries; the index still retains at most 40 reports. It adds no
independent timing history. Failed attempts without reports, legacy responses and receipts
dropped under checkpoint pressure may have no measurements. Their durations remain unknown.

`design_audit.py` output version 2 includes each indexed report's observed timing and
admission-to-claim/execution durations, plus coverage counts per round and the target config
identity when available. Admission-to-claim includes staffing and setup; execution includes
tools and transcript handling. These are not pure queue or model-inference measurements.
The old frozen JSON remains historical evidence and is not rewritten to invent timing.

Capture a new serial baseline after an authorized deployment, using the commands above.
Keep the pilot/artifact population and model configuration explicit in comparisons. No
serial timing baseline or live speedup has been measured by this implementation change.
