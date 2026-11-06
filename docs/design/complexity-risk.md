# Evolving complexity and change-risk classification

Status: accepted revision 2, implemented for review, 2026-11-06. Uncommitted; not deployed.

Classify the issue at intake, reassess the proposed work during design review, and
reassess the actual change during every PR review. Keep the estimates, their evidence
and their evolution visible. Later, model routing can use this information to choose
among the target's named model/effort profiles.

This first version adds observations, not another gate. It adds no model calls,
line budgets, required review rounds, approval thresholds or automatic task splitting.
Implementation is described in [the implementation plan](complexity-risk-implementation.md).

## Two dimensions, not one score

| Dimension | Low | Medium | High |
| ---- | ---- | ---- | ---- |
| Complexity | Local, familiar change with few interacting behaviors | Several interacting paths, meaningful state or compatibility constraints | Substantial coupling, concurrency, state transitions or architectural uncertainty |
| Change risk | A mistake has limited, readily reversible consequences | A mistake can disrupt a meaningful workflow, API or service | A mistake can affect security, persistent data, consensus, funds or difficult recovery |

Complexity describes reasoning and verification difficulty. Change risk describes
credible failure consequences and exposure, informed by the actual change and its
safeguards. Neither is the severity of an individual finding or a measure of remaining
work. Fixing a finding does not automatically lower either rating; a sound mitigation
or a narrower design can lower risk when the reviewer explains why.

Use `unknown` when evidence cannot support a rating. Missing assessment is never low
risk. Confidence is separate: how well does the inspected evidence support the estimate?

Examples are illustrative, not rules: consolidating several hex representations might
be medium complexity and low risk; changing a small authentication condition might be
low complexity and high risk. Line count, file count, test results and repair count are
context, never automatic scoring formulas. A small parser change can be harder than a
large mechanical edit.

## Report contract

Require the same non-null `classification` object in new intake, design-review and
PR-review schemas. Use `unknown` for insufficient evidence, rather than omitting the
assessment. The object has this shape:

```json
{
  "complexity": "medium",
  "change_risk": "medium",
  "confidence": "medium",
  "rationale": "Several parser states and partial-progress paths interact. Incorrect framing could lose response data, but the change is confined to the HTTP decoder.",
  "unknowns": [
    "Behavior of all incremental callers has not yet been checked."
  ]
}
```

Complexity and change risk use `unknown`, `low`, `medium`, `high`; confidence uses `low`,
`medium`, `high`. The rationale explains both dimensions using the issue, plan, code
or validation evidence. No new exact-line citation requirement or mandatory checklist.
An unknown is informational; it triggers clarification only when the existing workflow
independently determines that missing intent prevents safe work.

The rationale is at most 600 characters; include at most five unknowns, each at most
200 characters. Normalize to at most 4,096 serialized JSON bytes. Truncate excess text
with a bounded diagnostic; invalid labels or shape make the assessment unavailable.
These are metadata bounds, not constraints on the work being proposed.

Reuse the report's controller-owned stage, role/profile, actual provider/model/effort,
operation identity and requirements/base/plan/head binding. Add missing provenance to
new reports only; models never supply it. Keep schema version with the normalized
assessment. No separate observation ledger or model-identity structure is needed.
The first implementation collects estimates in the three existing assessment stages;
it does not add planner or implementer self-rating requirements.

## Evolution and disagreement

- Intake supplies the initial, explicitly provisional issue estimate.
- Design reviewers assess the current plan independently. Their assessments refine
  the issue estimate without changing the plan's approved content.
- PR reviewers assess the entire current base-to-head change, including accumulated
  repairs. Their assessments become the current PR estimate and the issue's latest view.
- A new plan, head, input revision or bundle invalidation makes affected observations
  historical. Preserve them for comparison; never present them as fresh evidence.

Use one deterministic reducer. For a complete current review set, take the highest
complexity and highest change risk among its reviewers, separately. Preserve each assessment
and flag disagreements; do not average ordinal labels or start a disagreement-resolution
round. Overall confidence is the lowest confidence among the contributing assessments.

An assessment set is complete only when every expected reviewer for the applicable
round/phase has a valid assessment, with neither dimension unknown. Otherwise mark it
provisional and show missing/unknown coverage. During an incomplete round, retain the
previous estimate as explicitly provisional and raise it if a current reviewer reports
a higher value. Do not lower it until a complete current set replaces it. If there is
no earlier estimate, show the known dimensions provisionally and unknown for the rest.

A completed new review set may lower an earlier rating: classification is not a permanent
high-water mark. Keep the old rating and rationale reachable. Cooperative and fresh
review phases use the engine's existing final-round rules; the classifier neither
adds a fresh round nor turns partial approval into a complete review.

Only validated core reports contribute. A malformed or rejected plan/review cannot
replace the estimate. Accepted reports requesting revision can contribute—their
assessment is evidence even though their verdict does not authorize implementation.

Reviewers are not shown peer classifications or the aggregate before producing their
own assessment. Fresh reviewers receive no previous classification votes. Retained
cooperative sessions may naturally retain their own prior estimate. The controller
combines observations after the reports arrive.

## Relationship to existing policy

The current top-level QC `risk` field is already operational: `finish_quality_control`
escalates `high`, and `consensus_valid` excludes it. Do not reuse that field for this
new descriptive classification or silently remove those checks.

Use `classification.change_risk` for consequences if the change is wrong. In this
release, update the shared instructions and the design-review skill so the top-level
QC `risk` explicitly concerns unresolved uncertainty requiring a human decision:

- Replace “Escalate high risk” with “Escalate consequential uncertainty that requires
  human judgment. Set the top-level QC risk to high in that case. A high classification
  change_risk alone is not a reason to escalate.”
- Replace the approval condition “no … high risk” with “no unanswered questions,
  concerns, non-goal conflicts or unresolved decisions requiring human judgment.”
- Rate the consequences of a plausible mistake in this change, not the module's reputation.

The existing high-QC-risk checks and explicit escalate verdict stay intact. This is an
intentional clarification of model-facing policy, not just a field rename: high impact
with an adequately specified and reviewable design can proceed, while missing authority,
intent or consequential decisions still escalate. Neither risk field is copied into the
other. A later policy change may remove the legacy field in favor of explicit escalation.

Classification disagreement, missing metadata, high complexity and high change risk do
not independently block work. Actual findings, charter conflicts and unsafe or unclear
requirements still follow the existing process. CI and exact-head approval requirements
are unchanged.

## Storage, recovery and presentation

Keep normalized classification inside the existing accepted intake report, design votes
and PR reviews. Compute the current view from those reports and their existing bindings.
Reuse operation consumption and write-once report archives for recovery and history;
do not create separate observation IDs, replay handling or a parallel history store.

The only additional task-level state is a small last-complete snapshot: complexity,
change risk, confidence, the stage and revision they describe, and source-report references.
It supports the provisional fallback when current reports are incomplete or replaced.
Use stable source identities until archive receipts supply links. Include this snapshot
in `record_outcome`, with freshness relative to the outcome's plan/head, so calibration
survives report compaction. No new outcome events or model calls are required.

Add one dashboard row, for example:

```text
Complexity: medium · Change risk: high · Confidence: medium
PR head abc1234 · 2/2 assessed · reviewers disagree
```

Before completion, say `provisional` and distinguish the last reviewed head from the
current head. Link to existing report archives for rationale and history. Keep the small
classification fields during normal report compaction; under checkpoint pressure,
release archived advisory text first, then optional assessments or snapshots before
rejecting a fitting core report. Pending publication bodies are never changed; retired report text already lives
in its archive. No new classification text in PR review comments, notification comments,
labels, GitHub permissions or separate assessment service in v1.

New report archives naturally include classification as part of their JSON. Existing PR
review rendering stays unchanged and ignores classification; no new review format is
needed. Never backfill old reports, queued bodies, approval digests or timestamps.
Missing historical classification means unassessed; the old QC risk cannot reconstruct it.

Separate advisory metadata before validation: remove only `classification` from a copy
before the exact-field and size checks in `validate_qc`, `validate_design_report` and
`validate_pr_review`, and before downstream core-report capacity checks. Validate the
core normally. Normalize and attach classification only to accepted reports, before their
first durable recording or publication. Rechecks use the same core projection. No other
unknown fields are silently removed.

New schemas require classification; old reports may omit it. A new parseable response
that nevertheless omits it, returns null or supplies invalid metadata keeps its valid core
verdict and gets an unavailable assessment with a bounded diagnostic. It must not trigger
a paid correction solely for classification. Reserve room for bounded metadata, dropping
excess advisory text or the assessment if necessary to preserve an otherwise fitting core
report. Hard transport limits, unparseable responses and provider failures keep their
existing recovery paths; this guarantee starts after a response can be parsed.

## Later: model and effort routing

Expose a versioned snapshot containing both dimensions, confidence, completeness,
binding and provenance. A future router can map that snapshot and the next role to
existing named model profiles. No routing changes happen in this increment.

Triage must have a configured starting profile: its own future classification cannot
select the call that produced it. Unknown or provisional ratings must not automatically
select a cheaper profile. Freeze and record each selection before dispatch. A provider,
model or effort change requires a correctly bound new session, not reuse of a transcript
under a different identity. Reviewer model diversity and budgets continue to apply.

Start by comparing estimates with real outcomes: review findings, repair rounds,
validation coverage and model usage. These outcomes inform calibration; a clean pilot
is not proof of low risk. Task decomposition remains separate future work, triggered by
reasoned scope assessment rather than a resurrected line threshold.
