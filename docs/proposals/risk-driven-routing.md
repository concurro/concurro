# Proposal: risk-driven routing

**Status: proposal, not implemented.** Revised after two Codex reviews against the code.

Use Concurro's complexity and change-risk estimates to choose, per task and stage, the
models, reasoning effort and review depth.

## Today

- **The estimate exists but is advisory.** Intake, design reviewers and PR reviewers rate
  `complexity` and `change_risk` (`unknown`/`low`/`medium`/`high`) with a confidence
  ([complexity-risk.md](../design/complexity-risk.md), `engine/classification.py`).
  - **Aggregation:** for a complete set, each dimension is the **maximum** and confidence
    is the **minimum**. The aggregate is **not a ratchet**: an incomplete set keeps the
    previous estimate as a floor, but a complete set replaces it and may lower it.
  - **Intake** is a single rating, and is `provisional` even when it is `complete`.
  - **Tolerance:** missing or malformed ratings never invalidate a report
    (`normalize`/`fit`).
  - **Storage:** `record_outcome` stores the snapshot, and classification archives keep
    full ratings. That's enough for shadow analysis.
- **Static per task.**
  - **The config:** `.concurro/config.yaml` fixes one profile (provider, model, effort) per
    role. Every reviewer in a reviewer role must have a **distinct provider/model pair**
    (`model_config._parse`); effort doesn't count towards distinctness.
  - **Pinning:** `Worker.load` pins each task to its configuration commit, so editing the
    repository config affects new tasks only.
- **Limits aren't all static.**
  - `RevisionBudget` adds writer-granted plan revisions (`/concurro retry
    --plan-revisions N`) on top of `max_plan_revisions`.
  - `max_fix_rounds` caps **both** implementation attempts in `Controller.develop` and PR
    repair rounds in `Controller.review`.
- **Approvals bind configuration.** `policy.approval_binding` includes `config_digest` and
  `design_roster(config)`. A changed effective config either blocks
  (`_validate_authority`) or makes the approval stale.
  - **Named-profile configs** (`agents`): the roster is the plan reviewers with their full
    profile, **effort included**. PR reviewers aren't in the roster, but their named
    assignments are covered indirectly by `config_digest`.
  - **Legacy configs:** the roster is the deduplicated planner, implementer, QC and
    reviewers, so PR reviewers are bound directly.
- **One reader, many consumers.** `approval_binding`, `consensus_valid`, `Design` dispatch
  (`design_batch`) and `classification.candidates` all derive the roster from
  `design_roster(config)`.
- **PR findings are owned by profile name** (`reviews.owned`/`context`). `Controller.review`
  calls only the reviewers currently assigned.
- **Cost:** model calls are counted per task (`state["calls"]`). Tokens are budgeted per
  gateway session key, and per-turn token and cost figures aren't recorded.
- **Budget admission:** `design_batch.Design.reserve` reserves only the **next attempts**
  for the roster, not a phase's worst case.

## Goals

- Spend more review and reasoning on risky or complex changes, and less on simple ones.
- Make every decision **deterministic, recorded, frozen and explainable**: a pinned policy
  applied to recorded evidence.
- Keep reviewer diversity, approval bindings, finding ownership and spent budgets.

## Non-goals

- Task decomposition, and automatic calibration of the policy from outcomes.
- Letting a model pick its own model, effort or reviewers.
- Changing QC `risk: high` escalation.

## Design

### Three separate layers

1. **Configured policy:** a versioned `routing` section in the pinned config, covered by
   `config_digest` like everything else in the config. It never changes during a task.
2. **The design tier:** selects the plan-reviewer roster that the approval binds, and
   whether a fresh design round is required. It is decided **once, before planning**, and
   is never changed afterwards.
3. **The execution tier:** selects work that the design approval doesn't bind
   (implementer profile and effort, PR reviewers). It is first selected after design
   consensus, and afterwards only escalates. **Neither tier is applied by changing the
   `config` object** that `approval_binding` and `_validate_authority` see.

### Resolved assignments

Storing a decision doesn't make anything use it. One state-aware function,
`assignments(state, config)`, returns the effective roster for every role: the configured
roster, overridden by the frozen tier decisions. It replaces `design_roster(config)` in
**every** consumer: dispatch, `approval_binding`, `consensus_valid`,
`classification.candidates` and `readiness`. With no routing decision recorded, it
returns exactly what `design_roster` and the configured agents return today.

Consumers read **projections**, not the whole object. `approval_binding` and
`consensus_valid` read the design assignments only, so an execution-tier change never
changes the approval digest.

Routing is supported only for named-profile (`agents`) configs. A legacy config with a
`routing` section is rejected, so legacy approval bindings are never affected.

### Policy

```yaml
routing:
  version: 1
  default: standard
  tiers:
    light:
      implementer: sol61-medium
    standard: {}                 # agents as configured
    elevated:
      implementer: opus55-max
      plan_reviewers: [opus55-high, sol61-high, third-model-high]   # distinct provider/models
      pr_reviewers: [opus55-high, sol61-high, third-model-high]
      fresh_design_round: required
  select:                        # first match wins
    - when: {change_risk: high}
      tier: elevated
    - when: {complexity: high}
      tier: elevated
    - when: {complexity: low, change_risk: low, confidence: [medium, high], complete: true, provisional: false}
      tier: light
```

- **Validation:** every tier is validated like today's config: distinct provider/model
  pairs for every reviewer. **The effective roster is validated too:** an escalation that
  keeps an owner (see below) and adds a tier's reviewers must still have distinct
  provider/model pairs. If keeping the owners makes the requested tier invalid, for
  example an owner's medium-effort profile plus the same model at high effort, the
  transition is refused and recorded; a model is never duplicated and an owner is never
  removed to make it fit.
- **Limits are out of scope at first.** Tiers vary agents and effort only;
  `max_fix_rounds`, `max_plan_revisions` and `max_model_calls` stay as configured, with
  writer grants (`RevisionBudget`) unchanged. A later phase that lets tiers vary limits
  must first define the effective limit as a formula over the configured value, the tier
  value, writer grants and spent usage, with boundary examples (a tier change after part
  of the limit is spent, a grant before and after a tier change).
- **`fresh_design_round: required`** belongs to the design tier only, so it is always
  known before the approval it governs. `policy.consensus_valid` enforces it when the
  frozen design decision carries it. The execution tier never adds it: demanding another
  design round after consensus needs an explicit reopen.
- **Fallback:** missing or malformed ratings give the deterministic fallback, `default`.
  They never invalidate an otherwise valid report.

### Decision points

| Hook | Evidence | Decides |
| ---- | ---- | ---- |
| `Controller.finish_quality_control` (after intake) | Intake: complete but provisional | **Design tier**, once: `default` or higher. |
| `Controller.finish_consensus` (after design approval; manual mode via `planned` and `CommandHandler.approve`) | Complete design-review set | **Execution tier.** The first selection may be any tier, including `light`; later ones (after a design reopen) only escalate. |
| End of `Controller.review`, before repair resets the reports | Current PR-review set | **Execution tier, additive escalation only** |

- **Design tier:** chosen once from intake and never changed. Because intake is
  provisional, it can't be `light`.
- **Execution tier:** its first selection, at the first `finish_consensus`, may be any
  tier the evidence allows. After that it only moves **up**, including when
  `finish_consensus` runs again after a design reopen.
- **Owners are kept at every assignment point.** Only the **first-ever** PR-reviewer
  assignment, before any PR finding exists, starts from a clean slate. Every later
  assignment, at a PR round or at `finish_consensus` after a reopen
  (`Controller.invalidate_design`/`reopen_design` leave earlier findings withdrawn with
  their owners), keeps every reviewer that owns an open or withdrawn finding;
  `readiness.review_reasons` would otherwise block forever.
- **PR-round escalation is additive:** it may add a reviewer or raise the implementer's
  effort, and never replaces or removes an owner. Replacing a reviewer needs an
  ownership handoff protocol, which is out of scope here.
- **The frozen record:** each decision stores the policy version and digest, the tier, the
  rule that matched, and **references to the evidence**: the classification sources and
  the binding. These are captured before dispatch, so later trimming, changed
  completeness or a replay can't change the outcome.
- **Human override:** a writer can set a tier with `/concurro tier <name>`, recorded with
  its author, which applies at the next decision point.
  - **Raising** follows the same rules as an automatic escalation.
  - **Lowering** is an explicit, recorded exception to upward-only, allowed for the
    execution tier only. It still keeps every owner and must give a valid effective
    roster. The design tier is never lowered once planning starts.

### Budget

- **The admission rule stays as it is:** reserve the next attempts, as `reserve` does
  today.
- **A stricter worst-case reservation per phase** is optional, and would need a separate
  design.
- **Outcomes:** the tier is recorded in `record_outcome`.

### Classification becomes consequential

Routing gives ratings consequences: they select reviewers, and can lead to budget blocks.
The rule is therefore narrower than "never a gate": **missing ratings never invalidate a
valid report, and routing always has a deterministic fallback.** Gaming is addressed
under [risks](#risks).

## Phasing

1. **Pure selector, in shadow mode:**
   - **Live:** at each hook, compute the decision from the current accepted assessments,
     record it with its evidence references, and show it on the dashboard. Nothing
     consumes it.
   - **Retrospective:** run the selector over archived reports and `record_outcome`
     snapshots for calibration. `record_outcome` records only selected terminal and
     blocked states, so it can't feed live decisions.
   - Token and cost telemetry would improve calibration, but isn't a prerequisite.
2. **Freeze the design tier before planning,** at `finish_quality_control` (`standard` or
   `elevated`). This phase introduces `assignments(state, config)` in every consumer.
3. **Execution tier at `finish_consensus`:** implementer and PR reviewers. The first
   selection precedes any PR finding; selections after a design reopen keep owners and
   only escalate. This phase enables `light`.
4. **Additive PR-round escalation,** once ownership semantics with
   [consolidated reviews](consolidated-pr-reviews.md) are settled. Parallel PR dispatch
   would cut latency, but isn't needed for correctness.

## Invariants

- Every reviewer in every tier, and in every effective roster, has a distinct
  provider/model pair.
- A historical approval keeps its original binding. Routing never changes the design
  roster after planning starts, and every consumer reads the same resolved assignments.
- No PR reviewer that owns outstanding findings is removed or replaced, at any
  assignment point.
- Spent budgets are never refunded or reset.
- Decisions are frozen with their evidence references, and replay gives the same result.

## Risks

- **Gaming:** a reviewer under-rates a change to reduce scrutiny. Mitigations:
  - the max aggregation;
  - reviewer independence;
  - a design tier that never lowers, and an execution tier that only escalates after
    its first selection;
  - `light` only from complete, non-provisional sets;
  - shadow data before enforcement.
- **Cost surprises:** an elevated tier can multiply spend. The task-wide cap bounds this.
- **The `max_fix_rounds` coupling:** if tiers later vary limits, lowering it also
  reduces implementation attempts. Consider separate limits first.

## Open questions

1. Should `light` exist at all, or should routing only escalate above the baseline?
2. Should `max_fix_rounds` be split into implementation attempts and PR repair rounds
   before tiers vary it?
3. How should routing combine with [per-user credentials](per-user-credentials.md)?
   Elevated tiers spend more of the sponsor's quota, so a sponsor may need to cap their
   tier.
