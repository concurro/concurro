# Review: `/concurro resume`

This review covers the uncommitted resume change, on top of `f8174ad`. It was done by
reading the code; the tests weren't run.

## Fix before shipping

**1. Resuming an escalated or rejected task skips reassessment.**

- **What retry does:** for these stages, `/concurro retry` sends the task back through
  intake with `revise_plan("intake", …)`. That uses one plan revision and re-reads the issue,
  so edits made to answer the escalation are taken into account.
- **What resume does:** `bundle_resume.adopt` sets `stage = retry_stage` and clears the
  reason.
- **Result:** a resume overrides the judgment with no new assessment, and the issue edits are
  ignored.
- **Fix:** after adopting the new settings, send escalated and rejected tasks through the
  same intake reassessment as retry, including re-reading the requirements and using a
  revision. Alternatively, refuse and point to `/concurro retry`.

**2. A changed reviewer team can leave open findings or objections that no reviewer can answer.**

- **Status:** found by reading the code; not reproduced.
- **What happens:** `reviews.owned` matches findings by `provider`. `adopt` resets the
  reviews when the PR team changes, but never checks who owns the open findings.
  - A removed PR reviewer's open or withdrawn findings have no reviewer left to address
    them.
  - Every open finding blocks the merge, so the task can never reach handoff.
  - The same applies to unresolved design objections when the plan-reviewer team changes
    before consensus is complete.
- **Not affected:** keeping a profile name and changing only its model or effort leaves
  ownership intact.
- **Fix:** refuse to resume while a reviewer being removed owns open or withdrawn findings,
  or unresolved objections, and list them in the refusal. This matches the
  routing-proposal rule that a reviewer who owns open findings is never removed.

**3. Check whether resume is refused on finished tasks.**

- **The gap:** nothing in `resume` or `adopt` refuses `merged`, `observed_merged` or
  `closed` tasks, so a finished task could get a "Task resumed" event.
- **Fix:** if commands aren't already filtered for these stages earlier, refuse them here.

Add a test for each of the three.

## Checked and sound

- **Approvals:** completed design approvals keep their original binding. The reviewer list is
  pinned in the resume record, and the stored `config_digest` and `charter_id` don't change.
  New assessments bind to the current configuration and instructions.
- **Edit policy:** candidate paths are still checked against the current edit policy at
  publication.
- **Budgets:** spent budgets, findings and publication progress are preserved.
- **Interrupted resume:** the resume record is saved before the registry's config pin is
  updated, and a lost update is recovered before any work is dispatched.
- **Unfinished sessions:** these restart without regaining first-pass eligibility. A
  completed but unconsumed model answer is applied under its original response contract.
- **Rollback:** the format-6 limit is documented.

## Round 2

Items 1–3 are fixed and tested. One low item remains.

**4. The notice after an exhausted resume says the resume wasn't applied.**

- **Where:** `CommandHandler.resume` calls `bundle_resume.adopt` before `reassess_judgment`.
  When the revision allowance is exhausted, `refuse_revision_command` posts "Plan revision
  command not applied".
- **What actually happens:** the new settings are adopted. The resume record is saved and
  the registry config pin is updated. Only the reassessment waits for revision capacity.
- **Why it matters:** the writer is told nothing happened, but the task now runs on the new
  settings.
- **Fix:** in this case, post a notice saying the settings were adopted and that
  reassessment needs more revisions, with `/concurro retry --plan-revisions 1` as the next
  step. Update `test_exhausted_judgment_keeps_waiting_revision_and_spending` to check the
  notice.
