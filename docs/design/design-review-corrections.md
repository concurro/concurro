# Independent design review through output corrections

Status: agreed design and implementation plan, 2026-11-07. Not implemented.

## Purpose and scope

Allow unanimous independent cooperative approvals to skip a duplicate fresh round even
when a reviewer needed controller-directed output corrections. Preserve fresh review
following substantive negotiation, plan revisions or uncertain session continuity.

This change includes eligibility tracking, neutral correction instructions, regression
tests, documentation and a read-only audit baseline. It does not include parallel
execution, approval carry-forward, deadlock detection or new verdict fields.

## Current behavior

`Controller.quality_control()` records `first_pass` only when the cooperative session
has completed exactly one turn, has no restarts and has no own objections. Every output
correction increments the turn count. `policy.consensus_valid()` consequently requires
a fresh round after even a citation or schema correction.

Correction failures are not all cosmetic. They include missing evidence and contradictory
verdicts. A correction must produce a fully valid assessment, not train the reviewer to
change everything to satisfied. All current validation and unanimous approval requirements
remain in force.

Claude's analysis of pilot #19 reported 15 design-review calls, nine rejected outputs,
no objections and one fresh round triggered by corrections. Treat those numbers as inputs
to verify in the baseline, not general performance guarantees. That pilot does not establish
that fresh review after substantive negotiation is redundant.

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

## Implementation steps

1. **Define and persist the receipt.** Inspect `engine/sessions.py`, controller operation
   recovery and approval binding. Add the smallest per-reviewer record needed for binding,
   session identity, eligibility and a fixed disqualification reason, outside `operation`.
   Save it with operation admission, but preserve it when retry removes the operation.
   Store references and digests, not copies of plans, feedback or reports. Keep eligibility
   independent of correction-allowance resets and report-history compaction.
2. **Wire lifecycle transitions.** Update `Controller.quality_control()`, the model-call
   preparation/recovery path, plan revision, requirements changes, design reopen, session
   restart and explicit invalidation. Preserve eligibility through validation-only corrections. Do not merely replace
   `turn == 1` with a subtraction of repair counts. Review every path that rotates a
   session or changes the design binding.
3. **Record and consume votes.** Update the current `first_pass` receipt or replace it
   with clearly named eligibility metadata. Keep existing valid completed approvals
   readable and unchanged. Old in-progress reviews without the new provenance fall back
   to fresh review. Test `policy.consensus_valid()` and `approval_valid()` explicitly.
4. **Add neutral feedback.** Include the instruction on every design-review correction,
   including contradictory approval and missing-evidence failures. Preserve specific
   diagnostic details and the archive/replay ordering.
5. **Add the regressions below.** Update the test that currently requires a fresh round
   for every corrected report. Retain full rejection, stale-binding and objection tests.
6. **Update current documentation.** Amend `docs/design/mvp.md` and any affected operating
   guidance to distinguish validation correction from substantive negotiation. Mark this
   plan implemented only after acceptance; retain the enduring contract and remove
   completed implementation narrative when it no longer helps maintainers.

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

Produce a reproducible report from existing issue audits and immutable report artifacts,
without model calls, comments, task enrollment, retries or other remote writes. It can be
prepared alongside the eligibility change; it need not wait for parallel dispatch.

Record repository and issue, collection time, engine/bundle identity when available,
run/invalidation boundaries, and the artifact numbers/ranges and digests actually read.
Link to the source artifacts instead of copying report bodies. State missing artifacts
and incomplete coverage explicitly so later measurements can use the same population.

Report, separately per run and phase:

- Accepted assessments, rejected outputs and validation codes.
- Correction calls versus substantive plan revisions; do not count replay as a new call.
- Objections raised, resolved or still open and their linked planner/reviewer responses.
- Fresh-round outcomes, identifying genuinely new findings where evidence permits.
- Model calls and recorded timing/usage, with unavailable token data marked unavailable.

Do not infer a new defect solely from a new objection ID, or a deadlock solely from
unchanged wording. Classify ambiguous cases as unknown. Separate runner setup/queue time
from model time only when timestamps support it. Estimated savings are counterfactuals,
not measurements; do not claim that skipping a review guarantees the same final result.

## Deferred follow-ups

- **Parallel review dispatch:** submit durably recorded per-reviewer turns and apply
  results serially. The current single operation and correction slots are not safe for
  concurrent `ask()` calls. A separate change must handle per-reviewer recovery, atomic
  budget admission, stale results and task ownership. It saves elapsed time, not calls.
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
