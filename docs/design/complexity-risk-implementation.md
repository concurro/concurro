# Complexity and change-risk implementation plan

Status: revision 2 implemented for review, 2026-11-06. Uncommitted; not deployed.

Implements [the design](complexity-risk.md). Ship an observable classification first;
leave automatic model/effort selection for a later change based on pilot evidence.

## 1. Shared contract and core-report projection

Add `engine/classification.py` with small typed records for an assessment, last-complete
snapshot and aggregate view. Follow `model_config.py` dataclass/serialization conventions;
reuse existing model identities and report bindings. No separate observation ledger.

Provide one shared projection that separates `classification` from the core report without
mutating the input. Apply it before exact-field and size checks in `validate_qc`,
`Controller.validate_design_report` and `validate_pr_review`. Design evidence validation
and stored-vote rechecks must see the same core. Review/checkpoint capacity calculations
must also distinguish core content from optional stored metadata. Strip no other fields.

Normalize metadata separately: `complexity` and `change_risk` are unknown/low/medium/high;
confidence is low/medium/high. Limit rationale to 600 characters, unknowns to five entries
of 200 characters, and the normalized JSON to 4,096 bytes. Clip excess text with a bounded
diagnostic; malformed labels or structure produce an unavailable assessment. Reserve
metadata room, but drop advisory text or the assessment if it alone would exceed remaining
checkpoint/report capacity. A valid fitting core must not acquire a repair or block.

Compose one versioned schema fragment through `Bundle.schema`, register it in the bundle
manifest and identity calculation, and emit self-contained provider schemas. New schemas
require a non-null classification object with all fields required; insufficient evidence
uses unknown. Historical reports may omit it. Missing/null classification in a new parsed
response is diagnostic metadata failure, not a core-verdict failure or a paid correction.
Hard transport and parsing limits remain unchanged.

Tests: all levels and unknowns, invalid/missing/null metadata, clipping and byte bounds,
unchanged core-field rejection, core reports just below their size limit with oversized
metadata, stored-vote validation, provider schemas and old reports without classification.

## 2. Derive estimates from existing reports

Normalize and attach classification in `quality_control` and `record_pr_review` only after
core validation and before the accepted report is persisted or queued. Use the existing
operation-consumption transaction. Resolve actual profile/provider/model/effort through
`model_config.resolve` for newly recorded reports, including legacy configurations.
Do not rewrite historical reports or pending outbox content.

Implement a pure reducer over current intake, design votes and PR reviews. Use their
existing requirements/base/plan/head/input-revision bindings and cooperative/fresh rules.
For a complete assessment set, take each dimension's maximum and confidence's minimum;
show disagreements. Missing or unknown assessments make the view provisional. Incomplete
sets may raise the previous estimate but cannot lower it; a complete newer set can.
Do not mix historical and current evidence without marking the fallback provisional.

Keep only a compact last-complete snapshot at task level: three labels, stage/revision
binding and source-report references. Resolve links through existing archive receipts;
do not generate new timestamps or observations on replay. Add the snapshot and its
freshness to existing `record_outcome` entries. Preserve small current classification
fields during compaction; use existing archives for retired rationales and history.
Classification never enters semantic plan or approval bindings.

Tests: order-independent reduction, incomplete coverage, disagreement, stale bindings,
new heads and design reopens, lowering after complete reassessment, retry/replay stability,
lost archive responses, compaction and terminal outcomes. Verify model calls, approval
bindings and readiness stay unchanged.

## 3. Guidance and dashboard

Update shared and assessment-role instructions with the rubric and the distinction between
`classification.change_risk` and top-level QC `risk`. Replace blanket “Escalate high risk”
and “no … high risk” wording as specified in the design. High change risk alone is not a
reason to escalate; unresolved decisions needing human judgment still are. Preserve the
policy code's high-QC-risk veto. This prompt-policy clarification is part of this release.

Ask for concise evidence, not a line-count prediction or a checklist score. Do not feed peer
ratings or the aggregate into independent/fresh assessment contexts. Planner/implementer
feedback may carry ratings because those roles do not produce assessments. When attaching
classification to retrieved outcomes, omit that metadata from assessment-role context so
calibration history does not become an unintended rating hint.

Add one compact dashboard row with scope, confidence, provisional status, disagreement and
source-report links. Existing JSON report archives expose the full assessment. Leave PR
review bodies, finding comments and notifications unchanged: no new renderer or format
version is needed. Existing archives and outboxes must still reproduce their original bytes.

Tests: high change risk with low QC risk may proceed/approve; high QC risk still escalates
or invalidates consensus; real clarify/reject/escalate decisions retain their behavior.
Also test hostile rationale text, dashboard fallback, reviewer-context independence,
unchanged PR review goldens, pending publication recovery and archive visibility.

## 4. Pilot and acceptance

Run engine policy, consensus, model-profile, publication, rendering and pipeline tests,
plus bundle/schema and whitespace checks. Run service/exchange tests if request/session
serialization changes.

Use two end-to-end fixtures: a simple change and a stateful change with a repair round.
Show intake, design, initial PR and repaired-head estimates. Classification must add no
model calls, gates, retries or notifications, and must not alter agent selection. Include
missing/malformed metadata and a high-change-risk approval in these fixtures.

Use fresh live pilots only after review, commit and deployment approval. This is a bundle
change: active model work follows the established invalidation policy, while watching
ready PRs stays unaffected. No silent adoption by active tasks.

Done means the operator can see what was estimated, by which model and effort, against
which revision, why it changed and where reviewers disagreed. Automatic routing, operator
overrides, classification-based approval rules and sub-issue creation remain follow-ups.

## Implementation notes

The shared schema fragment is composed by `Bundle.schema`; provider contracts stay
self-contained. Classifications are omitted from semantic operation identity and removed
from optional feedback if context space is tight, so metadata compaction cannot repeat a
model call or reject an otherwise fitting request. Checkpoint pressure releases already
archived advisory evidence before core reports; pending publications remain byte-stable.
Classified reports' publication receipts retain a separate core digest for later text
compaction after a rating is trimmed or removed. Full source/body hashes still govern
publication recovery, and older receipts retain their full-digest match. Archive-link
resolution updates an existing outcome snapshot without adding an outcome.

The regression suite covers simple and finding/repair lifecycles, provenance for named and
legacy model selections, malformed metadata, stale bindings, lost responses, byte limits,
context independence, compaction and unchanged native PR reviews. Live model calibration
remains a post-review pilot; no deployment is part of this change.
