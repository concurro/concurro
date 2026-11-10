# Checkpoint-preserving resume

A writer's `/concurro resume` adopts the current configuration and instructions and
continues the saved stage. It needs no release compatibility certificate and does not
require replacement tasks because configuration changed.

## Preserved work

Completed intake, the plan, original design approvals, published commits, findings and
spent budgets remain recorded. The adoption receipt records the writer, command and
old/new execution identities separately from the original approval binding. Completed
design consensus keeps its original reviewer roster until a new assessment starts.

Resume uses current edit policy, configured limits, model assignments, CI requirements,
setup and role instructions. Increasing a configured allowance preserves prior spending.
It does not invent answers to questions, mark findings resolved or reset spent counters.
Normal execution still enforces budgets, publication recovery and review readiness.
An authorized reply to the unfinished planner's outstanding question continues planning
without repeating accepted intake or spending a plan revision. This includes operational
answers after resume adopts a new edit policy. The answer enters the planner and review
context; it does not approve the draft. Issue edits or other changed discussion still
trigger reassessment. Manual tasks require `/concurro start` after the reply.
Rejected or escalated judgments go through intake reassessment, rereading the issue and
spending a plan revision as retry does. Output corrections retain their assessment stage;
an exhausted revision stays waiting for explicit capacity. Finished tasks ignore resume.

Unfinished sessions whose execution settings changed restart with the current settings.
Retained results and pending publication continue through ordinary recovery. A changed
unfinished review team starts new judgments and archives superseded reports; changing
the PR team repeats its review without deleting findings. Restarted design sessions lose
first-pass eligibility. Previously accepted work is not automatically reassessed.

A proposed team must retain the owners of open or withdrawn PR findings and unresolved
design objections. Otherwise resume is consumed with a notice naming the records, leaving
settings and the pause reason unchanged. PR ownership follows the profile name; design
ownership includes its model and effort. Closed findings and resolved objections do not
restrict team changes.

## Configuration and recovery

The service reads the fork default branch when processing the command and freezes that
configuration commit. It saves the adoption receipt before updating the registry's
execution pin. If the reply or registry update is lost, the next tick recovers that exact
pin before dispatching work. Runner setup and caller workflows use the adopted commit;
old runner references retain their own cleanup identities.

Repeated reads of a consumed command do not reapply it. Uncertain writes retain their
existing reconciliation semantics; resume does not assume that an unknown write failed.
`/concurro invalidate` remains the explicit request to restart assessment.

## Rollback

A checkpoint with a resume receipt uses format **6**. Rollback needs an engine that can
read every format already saved; an older engine refuses format 6. Do not restore an
older checkpoint to erase spent calls or publication progress.
