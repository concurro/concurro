# Complexity and change-risk classification

Status: implemented. Intake, design reviewers and PR reviewers rate the work during their
normal calls. Classification adds no model calls, authorization gates or automatic routing.

## Meaning and schema

| Dimension | Low | Medium | High |
| ---- | ---- | ---- | ---- |
| Complexity | Local, familiar change with few interacting behaviors | Several interacting paths or meaningful compatibility constraints | Substantial coupling, concurrency, state transitions or architectural uncertainty |
| Change risk | Mistakes have limited, reversible consequences | Mistakes can disrupt a meaningful workflow or API | Mistakes can affect security, persistent data, consensus, funds or difficult recovery |

Complexity concerns reasoning/verification difficulty; change risk concerns the consequences
of a plausible mistake in this change, not the module's reputation. Line count, file count,
repair count and passing tests are context, not scoring formulas. Confidence describes
how well the inspected evidence supports the estimate. Missing evidence is not low risk.

New intake, design-review and PR-review schemas require:

```json
{
  "classification": {
    "complexity": "medium",
    "change_risk": "high",
    "confidence": "medium",
    "rationale": "The change coordinates several persistent-state paths; a missed invalidation could return data from the wrong state.",
    "unknowns": ["Recovery after interrupted writes has not yet been exercised."]
  }
}
```

The two dimensions accept `unknown`, `low`, `medium`, `high`; confidence accepts `low`,
`medium`, `high`. Rationale is bounded to 600 characters, unknowns to five entries of
200 characters, and normalized metadata to 4,096 JSON bytes. The controller attaches
schema version, source operation/attempt, agent identity and the applicable issue, plan
or head binding. Models do not supply trusted provenance.

## Separate from escalation

Top-level QC `risk` is an existing policy field for unresolved consequential uncertainty
requiring human judgment. Its `high` value still escalates or prevents design consensus.
`classification.change_risk` is descriptive: a well-specified high-impact change can proceed.
Neither field is copied into the other. Actual unanswered questions, charter conflicts
and unresolved decisions still follow their normal clarification/escalation paths.

Classification is removed from a copy before core report validation, size checks and
semantic call/outcome digests. An otherwise valid parsed response with absent, null or
malformed classification keeps its core verdict; it is unassessed rather than sent for a
paid correction. Legacy reports need not have a rating. Unparseable responses and hard
transport failures retain normal recovery behavior.

## Current estimate

`engine/classification.py` derives the view from accepted current reports and their bindings:

- Intake gives an explicitly provisional issue estimate.
- Design reviewers independently assess the current plan.
- PR reviewers independently assess the whole current base-to-head change, including repairs.

For a complete current reviewer set, each dimension is its maximum and confidence is its
minimum. Disagreement remains visible; there is no averaging or extra adjudication round.
Completeness requires every expected reviewer to supply valid, known dimensions. Rejected
core reports do not contribute; accepted revision requests can contribute despite not
authorizing implementation.

An incomplete round is provisional. It may raise the preceding estimate, but cannot lower
it until a complete set replaces it. If no previous estimate exists, show the available
dimensions and mark missing ones unknown. A new plan/head makes older evidence historical.
An explicit invalidation clears the previous-run fallback as well as the current votes.

Reviewers do not receive peer classifications or aggregate hints; fresh design review has
no prior votes. Planner and implementer feedback may retain assessments. Removing optional
ratings from feedback under context pressure does not change logical model-call identity.

## Storage and display

Full normalized ratings travel with their immutable report archives. The checkpoint keeps
current report annotations and a compact last-complete snapshot for provisional fallback.
Outcome records include the snapshot and its freshness so later calibration can compare
estimates with results. No independent observation ledger or replay protocol exists.

Current intake, design votes, PR reviews, reopened-review events and pending publications
are protected from rating trimming. Only archived historical ratings are shortened or
dropped under checkpoint pressure; their full archived reports stay unchanged. Compaction
matches the core-report digest after trimming/removal, while publication recovery still
uses full-body digests. See [report storage](issue-dashboard.md#report-archive).

The dashboard shows fixed labels, confidence, coverage, provisional status, disagreements
and report links. It does not render model-written rationale in its table. Full rationale
remains in the archive. Classification adds no PR comments, issue labels or notifications.

## Future routing

Automatic model/effort selection is not implemented. A future router could use a versioned
estimate with completeness, freshness and provenance to select a configured model profile
before dispatch. Unknown/provisional ratings must not silently mean “use the cheap model”.
Changing model/effort needs compatible session binding, and reviewer model diversity and
spent budgets still apply. Calibration and task decomposition are separate future work.
