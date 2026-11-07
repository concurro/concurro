# Independent design review through output corrections

Status: implemented locally, pending review, 2026-11-07. Not deployed.

## Purpose and scope

Allow unanimous independent cooperative approvals to skip a duplicate fresh round even
when a reviewer needed controller-directed output corrections. Preserve fresh review
following substantive negotiation, plan revisions or uncertain session continuity.

This change includes eligibility tracking, neutral correction instructions, regression
tests, documentation and a read-only audit baseline. It does not include parallel
execution, approval carry-forward, deadlock detection or new verdict fields.

## Implementation

`engine/design_eligibility.py` records the assessment's exact binding, reviewer, session
and input digest before dispatch. `Controller.quality_control()` copies the validated
eligibility decision into the existing `first_pass` vote field. `consensus_valid()` keeps
reading that field, so existing completed approvals remain valid. It now means independent
assessment, not literally one model turn.

Receipts live in `state["design_eligibility"]`, outside `operation`, and retain a fixed
reason when disqualified. Only the current receipt per reviewer is stored. Correction
feedback is engine-written; the bundle and provider schemas are unchanged.

Correction failures are not all cosmetic. They include missing evidence and contradictory
verdicts. A correction must produce a fully valid assessment, not train the reviewer to
change everything to satisfied. All current validation and unanimous approval requirements
remain in force.

The [pilot #19 baseline](../reports/design-review-baseline.md) records the measured
starting point and its limitations. It does not establish that fresh review after
substantive negotiation is redundant.

## Eligibility contract

Track an explicit controller-owned eligibility receipt for each reviewer assessment.
Bind it to the exact design approval binding, reviewer identity and retained session
identity. Use the existing binding and identity helpers; do not duplicate their contracts.

Initialize eligibility before dispatch only when clean provenance is positively established:

- The reviewer session is new: turn 0, no restarts and no earlier transcript.
- The context contains no own objections, including resolved ones.
- It contains no substantive planner responses or peer judgments.
- It contains no writer input beyond the requirements supplied as this assessment's baseline.

The baseline includes the plan, requirements, charter, repository evidence and intake
concerns. Empty response or objection collections do not count as discussion. Writer
input already incorporated into the baseline requirements does not itself disqualify a
new independent assessment. These checks establish eligibility once; they are not a
turn-count formula for reconstructing it later.

Store the receipt in its own durable per-reviewer record **outside `operation`**, and
write it in the same checkpoint save as the operation before contacting the runner.
`/concurro retry` removes `operation`, so the receipt must survive that reset. A missing
record on an already-started legacy assessment means ineligible; never reconstruct a
positive receipt from turn counts or archived correction counts.

| Event | Eligibility |
| ---- | ---- |
| Controller validation feedback; unchanged design and session | Preserve current value |
| Source inspection prompted by that feedback | Preserve current value; all evidence must still validate |
| Correction allowance exhausted, followed by writer retry with no substantive input | Preserve current value; retry resets only the allowance |
| Own objections supplied, planner responses or peer judgments introduced | Clear |
| New substantive input in a retained assessment | Clear; old eligibility cannot authorize changed inputs |
| Ordinary plan revision with the reviewer session retained | Invalidate old receipt; the existing transcript prevents a new eligible receipt |
| Requirements change, design reopen or explicit invalidation rotating the session | Invalidate old receipt; a new assessment may qualify only if all clean-provenance checks pass |
| New generation or binding alone | Invalidates old authorization; does not by itself prove or disprove clean provenance |
| Session restart or continuity cannot be established | Clear |
| Lost response or replay of the same operation | Recover the recorded value; do not initialize or debit twice |
| Missing legacy receipt | Require fresh review |

Within an assessment, eligibility is monotonic: true may become false, never the reverse.
Ordinary plan revisions retain plan-review sessions, so a reviewer that already assessed
the prior plan cannot regain eligibility by receiving a new binding. Requirements changes,
design reopens and explicit invalidation rotate the sessions under the current session
scope rules. A genuinely clean first assessment in those new sessions may qualify, as
it can today; do not impose a mandatory fresh round solely because it follows a reopen
or requirements change. Retained objections or planner responses still disqualify it.

This is a new assessment, not restoration of the old receipt. Old votes and receipts
cannot authorize it. Retry alone neither rotates the assessment into eligibility nor
clears a disqualification. A failure-driven session restart remains ineligible even at
turn 0, because its restart count records uncertain continuity.

Check disqualifying inputs before dispatch and record the loss durably. At vote time,
verify the binding and session again and copy the eligibility decision into the vote.
Consensus consumes that explicit receipt; it must not infer independence later from
mutable session counters. Replaying the vote yields the same decision.

An eligible vote authorizes nothing by itself. The report must pass the complete schema,
evidence, citation and objection checks. Implementation still requires unanimous current
approvals, no open objections and the existing approval binding. Once objections have
entered substantive negotiation within a retained assessment, resolving them does not
restore eligibility.

Keep the existing fresh gate after substantive negotiation. Fresh reviewers assess the
whole final design independently, without earlier votes or planner rebuttals. Do not
weaken their defect threshold or carry approvals across changed plans.

## Correction instructions

Add an engine-written `instruction` to `review_validation_feedback`, alongside the
specific validation code, field and detail. Suggested wording:

> Correct the report against its schema and the validation feedback. Reconsider the
> verdict if necessary; approval is never required. Preserve genuine concerns and
> unanswered questions, and report actionable objections when the evidence warrants
> them. This feedback concerns report validity, not a request to approve the design.

Use one shared engine constant for this path. Keep rejection details specific and retain
existing correction and total model-call budgets. Do not classify eligibility using an
error-code allowlist: the relevant property is what information entered the assessment,
not whether the validator named a formatting or evidence error.

Keep this wording in the controller's existing feedback payload. No bundle/schema change
is planned. If implementation discovers a contradictory skill instruction, handle that
explicitly rather than silently broadening this change.

## Regression matrix

| Scenario | Required result |
| ---- | ---- |
| Clean first-pass unanimous approval | Implementation proceeds without fresh calls |
| One or several validation corrections, same baseline | Valid unanimous approvals can skip fresh review |
| Contradictory approval or missing evidence | Neutral feedback is supplied; invalid output cannot authorize work |
| Correction produces a genuine revise verdict | Objection enters the normal revision loop; no shortcut |
| Correction exhaustion, escalation, writer retry | Allowance renews; eligibility and spent budget do not reset |
| Retry removes `operation` after correction exhaustion | Per-reviewer receipt survives save/reload with its prior eligibility |
| Retry with new substantive writer input | Old eligibility cannot authorize changed inputs; a rotated session must independently meet every clean-provenance check |
| Objection raised and later resolved in retained session | Fresh final review still required |
| Ordinary plan revision with retained session | All old votes invalidated; a new binding does not restore eligibility |
| Requirements change or design reopen with clean new session | New receipt may qualify; no unconditional extra fresh round |
| Rotated session with own objections (including resolved), planner responses, peer judgments or extra writer feedback | Ineligible despite turn 0 and a new binding |
| Generation or binding changes without a clean new session | No shortcut based only on the changed identifier |
| Session restart | Fresh final review required, even after subsequent clean approval |
| Corrections under another binding or reviewer | Cannot establish or restore this reviewer's eligibility |
| Lost response, reload and retained-result replay | Receipt and call accounting remain deterministic; no duplicate transition |
| False receipt followed by retry or replay | Remains false |
| Legacy in-progress review without receipt | Conservatively requires fresh review |
| Existing completed approval | Remains valid under its recorded contract |
| Explicit invalidation | Old receipts discarded; a new independent run uses new sessions |
| Checkpoint reload and history compaction | Active receipts survive without depending on full archived reports |

Run focused consensus, design-contract, correction/recovery and session tests, then the
engine suite. Run relevant service/exchange tests if their interfaces change; they are
not expected to change for this scope. Check bundle identity is unchanged, documentation
links and whitespace. Tests use fixtures, not live provider calls or pilot mutations.

## Read-only audit baseline

`engine/design_audit.py` reads only issue-artifact GET endpoints, freezes responses in a
local snapshot and emits derived metrics plus source commitments. Without `--fetch` it
reproduces the report offline. It verifies immutable archives using the production
report store, including nested report digests. It never publishes or invokes models.

The report separates runs and review phases, accepted and rejected outputs, validation
codes and authoritative objection dispositions. Its coverage is the audit's bounded
report index, not a claim of exhaustive task history. Missing or edited reports are
explicitly unavailable. It records exact artifact identities, response digests and body
hashes; raw report text stays in its original artifacts and the local capture.

Archived outputs are not exact model-call counts, and rejected outputs are not necessarily
correction calls. Unknown tokens, timing, engine identity and disagreement attribution
stay unknown. New objection IDs do not prove new defects. Compare the same pilot and
artifact population when evaluating a later change; predicted savings are not measurements.

See [baseline and reproduction instructions](../reports/design-review-baseline.md).

## Deferred follow-ups

- **[Parallel review dispatch](parallel-reviews.md):** timing evidence first, then shared
  turn coordination for design and PR reviews, adopting design reviews first. Apply results
  serially with per-reviewer state, atomic budget admission and current recovery guarantees;
  stronger crash recovery is deferred. It saves elapsed time, not calls.
- **Defects versus suggestions:** one consistent distinction across review phases.
  Current design `concerns` block approval; optional suggestions must not be put there
  without a deliberate schema and policy change.
- **Revision aids:** a plan diff may help reviewers, but automatic approval carry-forward
  needs evidence and remains deferred. Reviewers still confirm the whole current plan.
- **Convergence:** first expose objection, planner response and reviewer explanation
  together. Decide on deadlock detection from pilots with actual disputes.

## Delivery and deployment

Leave implementation uncommitted for review. Do not deploy, invalidate or retry live
pilots without authorization. The planned engine-only change should preserve bundle
identity, so it does not itself require task invalidation. That does not waive normal
CI, permission and coordinated engine/workflow release checks in `AGENTS.md`.

If bundle text or schemas do change, declare that before deployment: use an idle window
or obtain explicit authorization to invalidate affected tasks. Existing running reviews
must never be silently reclassified as eligible merely because a new engine was deployed.
