# Proposal: risk-driven routing

**Status: proposal, not implemented.** Implementation detail for each phase is staged in
[`risk-driven-routing/`](risk-driven-routing/).

Use Concurro's complexity and change-risk ratings to choose the models, reasoning effort
and review depth for each task.

## Today

- Intake, design reviewers and PR reviewers already rate `complexity` (how hard the
  change is to reason about and verify) and `change_risk` (the consequence of a
  plausible mistake), with a confidence. The ratings are advisory
  ([complexity-risk.md](../design/complexity-risk.md)).
- Each task uses fixed profiles (`{provider, model, effort}`) from its pinned config.
  Reviewer roles need at least two distinct provider/model pairs.
- PR findings are owned by the reviewer's profile name.

## Goals

- Spend more review and reasoning on risky or complex changes, and less on simple ones.
- Prefer review panels from different model lineages, as the operator configures.
- Make every decision deterministic, recorded and explainable.
- Keep approval bindings, finding ownership and spent budgets intact.
- Start small, and add each capability without rewriting the earlier ones.

## Non-goals

- Letting any agent choose models, effort or reviewers.
- Inferring model capability or lineage; both are operator declarations.
- Automatic calibration of the policy, task decomposition, and routing triage or the
  planner.
- Changing QC `risk: high` escalation.

## Design

### Overview

1. **Agents rate; config decides.** Agents rate against a rubric and never see the
   profile catalog.
2. **A complexity × consequence matrix** maps the ratings to a **class**.
3. **Seats** map each class to a profile. A seat is one reviewer position with an
   operator-declared ladder of profiles, normally from one lineage. The class also
   decides how many seats sit.
4. **Two classes per task:** a **design class** from intake (plan reviewers) and an
   **execution class** from design consensus (implementer and PR reviewers).
5. **Config is never modified.** Classes and assignments live in task state.

### Policy

```yaml
routing:
  version: 1
  classes: [routine, standard, elevated, critical]   # ordered, lowest first
  baseline: standard
  matrix:                                            # complexity → change_risk → class
    low:    {low: routine,  medium: standard, high: elevated}
    medium: {low: standard, medium: standard, high: elevated}
    high:   {low: elevated, medium: elevated, high: critical}
  fresh_design_round: [elevated, critical]
  lineage: {sonnet-low: anthropic, opus-medium: anthropic, astra-low: openai, ...}
  implementer: {routine: sonnet-low, standard: opus-medium, elevated: opus-high, critical: fable-high}
  plan_reviewers: {count: ..., seats: [...]}         # same shape as pr_reviewers
  pr_reviewers:
    count: {routine: 2, standard: 2, elevated: 2, critical: 2}
    seats:                                           # in order of preference
      - name: pr-anthropic
        profiles: {routine: sonnet-low, standard: opus-medium, elevated: opus-high, critical: fable-high}
      - name: pr-openai
        profiles: {routine: terra-low, standard: astra-low, elevated: astra-medium, critical: astra-high}
```

- The panel is the first `count[class]` seats, so seat order is how the operator
  expresses lineage preference (Opus + Astra rather than Sol + Astra).
- Each seat maps classes to its configured profiles, so providers don't need matching
  tiers.
- Every panel keeps distinct provider/model pairs. Lineage problems are warnings only.
- Lineage, harness and gateway route are separate: a new lineage such as GLM still needs
  its own route and credential coverage. The trusted route declares the canonical
  sponsorship provider; routing never infers one from lineage, harness or model name.
  Initial start/label fills absent providers only after explicit issue sponsors are
  reconciled and only where the initiator has selected-route coverage. A provider
  introduced later stays unassigned pending explicit sponsorship;
  coverage aggregates the selected profiles across independent provider-slot owners.

### From ratings to a class

1. `unknown` counts as `high`; look up the matrix cell.
2. Low confidence raises the class one step.
3. Incomplete or provisional evidence never goes below `baseline`.

Intake is provisional, so the design class is never below `baseline`.

### When classes are decided

| Hook | Decides |
| ---- | ---- |
| After intake | The design class, once. |
| After design consensus | The execution class. Later selections only raise it. |
| After an accepted implementation attempt | The implementer may raise its ratings, setting a floor. |
| After a PR round | The PR ratings may raise the execution class. |

A writer can override with `/concurro class <name>`; lowering is allowed for the
execution class only, and never if it would unseat a seat that owns open or withdrawn
findings.

### Resolved assignments

One function, `assignments(state, config)`, gives every consumer the effective seats and
profiles. Approval checks read only the design part, so an execution-class change never
invalidates an approval.

### Seats own findings

- **The seat owns** a finding and dispositions it; **the originating profile** is kept
  as permanent attribution. A different model in the same seat may disposition a
  finding.
- A report counts only for its seat's current profile. When the profile changes, the
  seat votes again.
- A seat that owns open or withdrawn findings is never unseated; one with only closed
  findings may be. Adding a seat never touches existing findings.
- Existing tasks and records keep today's meaning: seat name = profile name.

### Budget and limits

Admission and limits are unchanged. Per-class limits are deferred.

## Phasing

Each phase leaves the system consistent if later phases never ship.

| Phase | Adds | Staged notes |
| ---- | ---- | ---- |
| 0 | Rating rubric in the bundle; shadow selector that records but never acts | [phase-0-shadow.md](risk-driven-routing/phase-0-shadow.md) |
| 1 | Seat identity, with seat name = profile name; no behaviour change | [phase-1-seat-identity.md](risk-driven-routing/phase-1-seat-identity.md) |
| 2 | Execution class at consensus: implementer and PR seat profiles; new tasks only | [phase-2-execution-class.md](risk-driven-routing/phase-2-execution-class.md) |
| 3 | Implementer raise, and escalation after PR rounds | [phase-3-implementer-raise.md](risk-driven-routing/phase-3-implementer-raise.md) |
| 4 | Design class at intake | [later-phases.md](risk-driven-routing/later-phases.md) |
| 5 | Variable panel size, at least two | [later-phases.md](risk-driven-routing/later-phases.md) |
| 6 (optional) | One-reviewer PR panels | [later-phases.md](risk-driven-routing/later-phases.md) |

## Decisions before each phase

A phase's implementation brief is approved only once these are decided and recorded
here. A recorded deferral counts.

- **Phase 2:**
  - G1: may any class sit below `baseline`?
  - G2: one class for implementer and panel, or separate axes?
  - G3a: coverage of the selected profiles' routes before admission.
  - G4: the matrix and mappings to enforce, using the phase 0 shadow data.
- **Phases 3–5:** G4 for the policy each newly enforces.
- **Phase 6:** G6, evidence that one reviewer catches enough defects.
- **Whichever of routing phase 2 and per-user credentials enforcement ships second:**
  G3b, sponsor-aware coverage.
- **Deferred, no phase:** G3c (sponsor caps on class), G5 (per-class limits).

## Invariants

- Every panel has distinct provider/model pairs and at least two reviewers (until
  phase 6, for PR panels only).
- Approvals keep their binding; routing never changes design assignments after planning
  starts.
- A seat that owns open or withdrawn findings is never unseated.
- Spent budgets are never refunded or reset.
- Decisions are frozen with their evidence, and replay gives the same result.

## Risks

- **Drift to the top classes.** Mitigations: the rubric, shadow data, and a matrix with
  a single top cell.
- **Gaming** by under-rating. Mitigations: maximum aggregation, reviewer independence,
  the baseline floor, and an execution class that only rises.
- **Cost:** higher classes multiply spend; the task-wide call cap bounds it.
- **Lineage is a label,** not proof of independence.
