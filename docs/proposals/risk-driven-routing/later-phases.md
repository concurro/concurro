# Routing phases 4–6 and deferred work (staged notes)

Staged input for the later implementation briefs of
[risk-driven routing](../risk-driven-routing.md). Not briefs: re-check every code fact
when a brief is written.

## Phase 4: design class at intake

- Set once at `Controller.finish_quality_control`, from intake. It selects the
  plan-reviewer seats' profiles and whether a fresh design round is required.
- Intake is always provisional, so the design class is never below `baseline`.
- It never changes once planning starts. `policy.consensus_valid` enforces
  `fresh_design_round` when the frozen design class requires it; the execution class
  never adds a design round, which needs an explicit reopen.
- This replaces phase 2's static design projection; `approval_binding` reads the design
  projection only.
- **G4:** approve the intake-selected plan-reviewer mappings and the fresh-review policy.

## Phase 5: variable count

- `count` varies by class, at least two, non-decreasing along the class order.
- Adding a seat never touches existing findings.
- **G4:** approve the count mappings and their budget and coverage implications.

## Phase 6 (optional): one-reviewer PR panels

- `model_config._parse` applies the two-reviewer minimum to both reviewer roles. Phase 6
  adds an explicit, PR-only policy exception; plan reviewers and static configs keep the
  minimum.
- It changes what the unanimity gate means for opted-in classes.
- **G6:** a dedicated review-effectiveness evaluation showing one reviewer catches enough
  defects; reliable ratings alone aren't enough.

## Deferred, with no phase

- **Per-class limits (G5):** `max_fix_rounds` caps both implementation attempts
  (`Controller.develop`) and PR repair rounds (`Controller.review`), and
  `RevisionBudget` adds writer grants to `max_plan_revisions`. Varying limits by class
  first needs a decision on splitting `max_fix_rounds`, and a formula over the
  configured value, the class value, grants and spent usage, with boundary examples.
- **Sponsor integration (G3b):** sponsor-aware coverage once routing and per-user
  credentials enforcement both run; blocks whichever ships second.
- **Sponsor caps on class (G3c):** may be deferred. If adopted, insufficient
  authorization waits or refuses; it never silently selects a weaker class.
- **Ownership handoff:** a seat owning open or withdrawn findings can't be removed until
  they close; a seat with only closed findings can. Handing open findings to another
  seat isn't proposed.
- **Separate axes:** complexity driving the implementer and consequence the panel, as a
  later policy version for new tasks.
