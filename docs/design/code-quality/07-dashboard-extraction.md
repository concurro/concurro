# 07: Dashboard extraction

Part of the [code-quality programme](README.md).

## Goal

Move the issue-dashboard rendering out of `Controller` into its own module. This is about
380 lines of read-only rendering, mixed into the state machine at `controller.py:329-711`.
It is the lowest-risk, highest-yield step in breaking up the controller.

## Scope

- New `engine/dashboard.py`, next to the existing `engine/dashboard_runs.py`
- `engine/controller.py`
- Tests

## Changes

1. **Create a `Dashboard` class.** Give it `state`, `config`, `checkpoints` and the small
   set of controller facts the renderers read: `pr_url` and `waiting_revision()`. Pass
   those in as values or callables, not the whole controller.
2. **Move the renderers into it,** keeping their bodies:
   - `summary_body`, `summary_lines`, `activity_line`, `classification_row`;
   - `artifact_url`, which should use iteration 05's helper;
   - `current_step`, `step_row`, `action_line`, `progress_rows`, `diagnostics_table`,
     `previous_runs`;
   - any private helpers only they use.
   - `Controller.save()` (and anything else that renders) calls `Dashboard(...).body()`, or
     the moved method under its current name.
3. **Small cleanups inside the moved code:**
   - `activity_line`: in the `plan_qc` branch, `message` is built up to three times, each
     overwriting the last (about 393, 404, 415). Restructure it so each case assigns once,
     with identical results. Split per-stage helpers out behind a dispatch dict.
   - One `BATCH_STATUS` map replaces the two copies (about 412 and 636). One copy is
     lowercase and the other capitalized, so derive the capitalized form at its call site.
     Keep the "correcting" strings exactly as they are, since they differ.
   - `progress_rows`: compute `current_step()` once, instead of twice per step, and compute
     `dashboard_runs.boundaries(state)[-1]` once, instead of once per row and at 533, 541,
     649 and 679. Extract `_round_cells(...)` and `_pending_reviewer_status(provider)` to
     reduce the 7-level nesting.
   - `step_row` / `current_step`: use iteration 04's `Step` type instead of matching
     literal step names and magic indexes 2–5.
4. **Keep the controller's public surface working.** Anything tests call on the
   controller, for example `controller.action_line()`, either delegates to `Dashboard` or
   the tests are updated to call `Dashboard`. Prefer delegation for names used widely.

## Must not change

- **Dashboard body text, byte for byte,** for every stage and state shape.
- **Save behaviour:** when the dashboard is rendered relative to `save()`, and what is saved.

## Tests first

- **Golden snapshot tests, before moving anything.** Drive the existing pipeline fixtures
  to a representative set of states, render `summary_body()` for each, and store the
  bodies as fixtures. Cover at least:
  - intake, plan, design review in progress with mixed reviewer statuses, planned (manual);
  - implement, PR review, gates;
  - blocked with a bundle reason, escalated with a waiting revision, merged;
  - a task with a previous run (an invalidation boundary).
- The snapshot test must pass on the old code and on the new code unchanged.

## Done when

- None of the listed renderers is defined on `Controller`, except thin delegates.
- The golden snapshots pass unchanged.
- All suites and validation commands are green.

## Commit message

```text
refactor(engine): extract dashboard rendering from the controller
```
