# Routing phase 0: rubric and shadow selector (staged notes)

Staged input for the phase 0 implementation brief of
[risk-driven routing](../risk-driven-routing.md). Not a brief: re-check every code fact
when the brief is written.

## Scope

- The rating rubric in the bundle.
- The shadow selector: classes and panels computed at each hook and recorded, never
  acted on.
- An optional calibration summary on outcomes, and an operator-run calibration report.

No routing is enforced. Phase 0 depends on neither the credentials nor the
consolidated-reviews implementation.

## Code facts

- Intake, design reviewers and PR reviewers rate `complexity` and `change_risk`
  (`unknown`/`low`/`medium`/`high`) with a confidence
  ([complexity-risk.md](../../design/complexity-risk.md)). Reviewers never see peer
  ratings. Missing or malformed ratings never invalidate a report; routing keeps that
  tolerance and always has a deterministic fallback.
- QC's top-level `risk: high` escalation is separate from `change_risk`, and routing
  doesn't change it.
- `classification.reduce`: each dimension is the maximum of the known values, confidence
  the minimum. A complete set replaces the previous estimate and may lower it; an
  incomplete set keeps it as a floor. An incomplete set can still yield low/low, and an
  `unknown` disappears behind another reviewer's known value. Intake is `provisional`
  even when `complete`.
- `reduce`'s `disagreement` is one flag for either dimension, excludes unknowns, and
  isn't stored in the snapshot.
- `classification_snapshot` keeps the last complete estimate; it can't describe current
  coverage. The current `classification.View` carries `complete` and `provisional`.
- Agents receive prompts from the bundle (`Bundle.instructions`), so the rubric is a
  bundle change.
- `Controller.record_outcome` runs only for `merged`, `observed_merged`, `rejected`,
  `escalated`, `blocked` and `stopped` (several recoverable), keeps the last six
  deduplicated snapshots per task, and stores no repair rounds, revisions, finding
  counts, tokens or cost.
- `Controller.retrieve_outcomes` accepts only outcome version 1 and selects a fixed
  whitelist of lesson fields.
- Checkpoints are capped at 60,000 bytes; `report_store.packed` doesn't detach small
  numeric collections automatically.

## The rubric

Keep the existing definitions and add concrete examples per level, grounded in reasoning
difficulty and plausible consequences, drawn from pilot tasks. Intake, design-review and
PR-review prompts use the same rubric.

## Routing projection (computed in shadow)

1. Take the aggregate labels from the current `classification.View`. `unknown` on either
   dimension counts as `high`.
2. Look up the matrix cell.
3. If confidence is `low`, raise the class one step, capped at the top class.
4. If the set isn't complete, or is provisional, the class is at least `baseline`.

Incomplete low/low evidence selects at least `baseline`; missing reviewers don't by
themselves force high/high.

## Shadow observations

The canonical record, bounded per task, stored separately from any active decision. Each
holds:

- task and run identity, and the hook with its order of occurrence;
- the evidence binding and references to the assessments;
- the policy and configuration identity;
- the projected dimensions, confidence, coverage (complete, provisional, missing
  assessments) and adjustments, including whether the previous estimate contributed;
- a disagreement indicator per dimension;
- the raw class and the class after the floor and only-up rules; "baseline floor
  applied" only when the floor actually raised the result, separately from "evidence
  was incomplete";
- the actual assignments used.

Upgrading the engine never turns shadow observations into active decisions.

## Calibration summary on outcomes

- The outer outcome format stays at version 1, so lesson retrieval keeps working.
- An optional, separately versioned summary: consumed PR repair rounds (`state["round"]`,
  which writer retries can also increment), consumed plan revisions (`RevisionBudget`),
  unique finding IDs raised and recorded as fixed per seat (the reviewer-recorded status
  from `reviews.apply`, not verified correctness), and references to the shadow
  observations. It never enters the lesson whitelist.
- `record_outcome`'s equality check ignores the summary, as it ignores classification
  hints, so a metadata change never creates an extra outcome.
- Fix byte and cardinality bounds and truncation indicators, with a capacity test of six
  maximal summaries in one checkpoint.

## Calibration report

Operator-run, in `docs/reports/`, with reproducible machine-readable output and a source
manifest, like the [review-cost baseline](../../reports/design-review-baseline.md). Raw
captures stay outside the repository.

**Population:** tasks without an outcome (closed-unmerged, ready-for-review handoffs)
are covered from current-stage audit evidence or marked uncovered.

**Counting rules:**

- One selected endpoint per task and run for cumulative metrics; the six snapshots
  aren't runs, and summing them double-counts.
- Transitions between hooks use verified, ordered shadow observations.
- Truncated or unavailable history is labelled, never counted as zero.
- Whole-task associations are kept apart from work after a particular hook.
- Calls measure call volume, not tokens or cost.

**It shows:** the class distribution overall and per hook; per class, associations with
repair rounds, findings, call volume and outcome status (separating infrastructure or
schema failures from substantive repairs where possible); class changes between hooks
and their direction; disagreement per dimension and missing assessments; how often
evidence was incomplete and how often the floor raised the class.

**It can't show:** cost per class, escaped defects (nothing links a merge to a later
defect), or counterfactuals such as whether a cheaper model would have succeeded.

A dashboard may later link to the report, but isn't the calibration dataset.
