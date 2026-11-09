# Proposal: risk-driven routing

**Status: proposal, not implemented.** Revised after Codex's reviews against the code,
including a redesign around a classification matrix and reviewer seats.

Use Concurro's complexity and change-risk ratings to choose, per task and stage, the
models, reasoning effort and review depth.

## Today

- **The estimate exists but is advisory.** Intake, design reviewers and PR reviewers rate
  `complexity` and `change_risk` (`unknown`/`low`/`medium`/`high`) with a confidence
  ([complexity-risk.md](../design/complexity-risk.md), `engine/classification.py`).
  - **Meaning:** complexity is reasoning and verification difficulty; change risk is the
    consequence of a plausible mistake.
  - **Aggregation** (`classification.reduce`): each dimension is the **maximum** of the
    known values and confidence is the **minimum**. A complete set replaces the previous
    estimate and may lower it; an incomplete set keeps the previous estimate as a floor.
    An incomplete set can still yield low/low, and an `unknown` disappears behind another
    reviewer's known value.
  - **Intake** is a single rating, and is `provisional` even when it is `complete`.
  - **Independence:** reviewers never see peer ratings.
  - **Tolerance:** missing or malformed ratings never invalidate a report.
    `report_core.core` removes only `classification` before core validation.
  - **The implementer doesn't rate.** `candidates.development` requires exactly
    `{summary, validation, edits, design_issue}`.
- **Static per task.**
  - **Profiles:** `.concurro/config.yaml` defines named profiles of exactly
    `{provider, model, effort}`, with `provider` in `model_config.HARNESSES`
    (`anthropic`, `openai`) and effort from that provider's vocabulary (`EFFORTS`).
  - **Assignments:** one profile per role. Each reviewer role needs **at least two**
    profiles with **distinct provider/model pairs**; effort doesn't count
    (`model_config._parse`).
  - **Pinning:** `Worker.load` pins each task to its configuration commit.
  - **Gateway routes:** `gateway/agentgateway.yaml` routes `gpt-*` and `claude-*` only.
- **Limits:** `RevisionBudget` adds writer-granted plan revisions to `max_plan_revisions`.
  `max_fix_rounds` caps both implementation attempts (`Controller.develop`) and PR repair
  rounds (`Controller.review`).
- **Approvals bind configuration.** `policy.approval_binding` includes `config_digest` and
  `design_roster(config)`; a changed effective config blocks (`_validate_authority`) or
  makes the approval stale. For named-profile configs the roster is the plan reviewers
  with full profiles, effort included. Legacy configs also bind the PR reviewers directly.
  `approval_binding`, `consensus_valid`, `Design` dispatch and `classification.candidates`
  all read `design_roster(config)`.
- **PR reviewer identity is the profile name**, in several places:
  - `state["reviews"]` is keyed by it, and `Controller.review` skips a reviewer that
    already has a report there;
  - `reviews.owned`/`context` key ownership by it, and only the owner dispositions;
  - outbox items carry it, and `reviews.report_for` uses it to find the report;
  - `finding_body` and `opening_matches` include it as attribution in thread
    verification;
  - `readiness.review_reasons` iterates `assigned(config, "pr_reviewers")`;
  - `classification.candidates` finds a reopened PR assessment by its source profile.
- **Sessions:** `sessions.prepare` retains transcripts for planning, design review and
  implementation, scoped by profile identity. PR reviews get no retained session.
- **Budget admission:** `design_batch.Design.reserve` reserves only the next attempts.

## Goals

- Spend more review and reasoning on risky or complex changes, and less on simple ones.
- Prefer review panels from **different model lineages**, as the operator configures.
- Make every decision **deterministic, recorded, frozen and explainable**.
- Keep approval bindings, finding ownership and spent budgets.
- Start small, and add each capability without rewriting the earlier ones.

## Non-goals

- Letting any agent choose models, effort or reviewers.
- Inferring model capability or lineage; both are operator declarations.
- Task decomposition, and automatic calibration of the policy from outcomes.
- Routing the triage agent or planner.
- Changing QC `risk: high` escalation.

## Design

### Overview

1. **Agents rate; config decides.** Agents rate complexity and change risk against a
   rubric. They are never shown the profile catalog.
2. **A complexity × consequence matrix** maps the ratings to a **class**.
3. **Seats** map each class to profiles. A seat is one reviewer position with an
   operator-declared ladder of profiles, normally from one lineage; lineage consistency
   is checked as advice only. The class also decides how many seats sit.
4. **Two classes per task:** a **design class** from intake (plan reviewers) and an
   **execution class** from design consensus (implementer and PR reviewers).
5. **Config is never modified.** Classes and assignments live in task state. The pinned
   routing policy is part of the config, so `config_digest` covers it unchanged, and
   routing never changes the `config` object that `approval_binding` and
   `_validate_authority` see.

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
  fresh_design_round: [elevated, critical]           # design classes that require it
  lineage:                                           # operator-declared, per profile
    sonnet-low: anthropic
    opus-medium: anthropic
    opus-high: anthropic
    fable-high: anthropic
    terra-low: openai
    astra-low: openai
    astra-medium: openai
    astra-high: openai
  implementer: {routine: sonnet-low, standard: opus-medium, elevated: opus-high, critical: fable-high}
  plan_reviewers:
    count: {routine: 2, standard: 2, elevated: 2, critical: 2}
    seats:                                           # in order of preference
      - name: plan-anthropic
        profiles: {routine: opus-medium, standard: opus-medium, elevated: opus-high, critical: fable-high}
      - name: plan-openai
        profiles: {routine: astra-low, standard: astra-low, elevated: astra-medium, critical: astra-high}
  pr_reviewers:
    count: {routine: 2, standard: 2, elevated: 2, critical: 2}
    seats:
      - name: pr-anthropic
        profiles: {routine: sonnet-low, standard: opus-medium, elevated: opus-high, critical: fable-high}
      - name: pr-openai
        profiles: {routine: terra-low, standard: astra-low, elevated: astra-medium, critical: astra-high}
```

- **Profiles** are the existing `models` entries; routing adds no profile fields.
- **Plan and PR seats are separate,** with role-qualified names. Their rosters, session
  lifecycles and assignment boundaries differ.
- **Capability ordering is an operator assertion.** Concurro can't infer that one model
  is stronger than another from names or effort.

**Validation** (the config is rejected otherwise):

- `classes` is non-empty and unique; `baseline` and every matrix value name a class.
- The matrix has all nine cells and is **monotonic**: for each step up along either axis,
  the class index doesn't decrease. This proves monotonic class selection, not
  increasing model capability.
- Seat names are unique across the routing section; every seat and the implementer map
  every class to a known profile.
- `count` is an integer from 2 to the number of seats for every class, and doesn't
  decrease along the class order.
- **Every effective panel** (the first `count[class]` seats, for every class) has
  distinct provider/model pairs.
- `fresh_design_round` lists known classes and is **upward-closed**: if a class requires
  a fresh design round, every higher class does too, so review depth stays monotonic.
- Routing requires a named-profile (`agents`) config; a legacy config with `routing` is
  rejected.

**Warnings only:**

- a seat whose profiles span more than one lineage;
- two seats on any effective panel with the same lineage;
- a class whose panel has no seat with a lineage different from that class's
  implementer;
- a routed profile with no declared lineage.

A missing lineage never counts as evidence of independence. Lineage is operator-declared
provenance, not proof of independent training. It is
separate from the **harness** (which CLI runs the model) and from the **route** (the
gateway backend and credential). A model such as GLM may run through the Claude harness
and still declare its own lineage; making it executable needs a gateway route,
authentication, model allowance and compatibility checks, and credential coverage is by
route, not lineage ([per-user credentials](per-user-credentials.md#3-coverage)).

### From ratings to a class

The displayed classification stays as it is. Routing uses a separate, conservative
**routing projection** of the current `classification.View`, including its `complete`
and `provisional` flags. `classification_snapshot` alone isn't enough: it keeps the last
complete estimate and can't describe current coverage.

1. Take the aggregate labels. `unknown` on either dimension counts as `high`.
2. Look up the matrix cell.
3. If confidence is `low`, raise the class one step, capped at the top class.
4. **Incomplete or provisional evidence never goes below the baseline:** if the set isn't
   complete, or is provisional, the class is at least `baseline`. Only a complete,
   non-provisional set can select a class below it.

Intake is always provisional, so the design class is never below `baseline`. Incomplete
low/low evidence selects **at least** `baseline`; missing reviewers don't by themselves
force high/high.

The decision record freezes the **input projection**: the labels, whether the previous
estimate contributed, the `complete` and `provisional` flags, and each adjustment
applied. Retrospective reconstruction uses that record, never a later snapshot.

### Design class and execution class

| Hook | Evidence | Decides |
| ---- | ---- | ---- |
| `Controller.finish_quality_control` | Intake | **Design class,** once. Plan-reviewer seat profiles and `fresh_design_round`. |
| `Controller.finish_consensus` (manual mode via `planned` and `CommandHandler.approve`) | The design-review set | **Execution class.** First selection: any class. Later (after a reopen): only up. |
| Accepted implementation attempt | The implementer's raise | **Execution class floor,** only up |
| End of `Controller.review`, before repair resets the reports | The PR-review set | **Execution class,** only up |

- **The design class** never changes once planning starts. `policy.consensus_valid`
  enforces `fresh_design_round` when the frozen design class requires it. The execution
  class never adds a design round; that needs an explicit reopen.
- **The execution class** is selected after valid consensus **even when ratings are
  incomplete**, using the routing projection. Consensus means completed votes, not
  complete ratings; routing adds no classification-completeness gate.
- **Human override:** `/concurro class <name>`, recorded with its author, applies at the
  next decision point. Raising follows the automatic rules. Lowering is an explicit
  exception, for the execution class only, and is **refused** if it would unseat a seat
  that owns open or withdrawn findings.
- **The frozen record:** each decision stores the policy version and digest, the class,
  the matrix cell, the projection's adjustments and references to the evidence.

### Resolved assignments

One state-aware function, `assignments(state, config)`, returns every role's effective
seats and profiles from the configured policy and the frozen class decisions. Every
consumer reads it through a **projection**:

- `approval_binding` and `consensus_valid`: the design projection only, so an
  execution-class change never changes the approval digest;
- dispatch: the assignment for its role;
- `classification.candidates` and `readiness.review_reasons`: the effective seats.

Without a `routing` section it returns exactly what today's configuration gives.

**Until design routing is enabled (phase 4),** the design projection is the static
design roster and today's fresh-review policy, even when execution routing is active.
`approval_binding` keeps its exact current representation.

**Each assignment carries a revision.** It is part of the frozen admission record of
every attempt (see the [cross-cutting rules](README.md#cross-cutting-rules)). Coverage
for a newly selected required profile is checked before admission; a profile change
never substitutes another credential or rebinds an admitted attempt.

### Seats own findings

- **Two identities per PR report and finding:**
  - **the seat** is the continuing review authority: it owns findings and dispositions
    them;
  - **the originating profile** is immutable attribution: who actually raised the
    finding, kept in the finding, its opening body and digest.

  A different model in the same seat may therefore disposition a finding. That is
  intended.
- **Reports are bound to the seat's assignment.** Each report records its seat, the
  assigned profile identity (effort included) and an assignment revision. When a seat's
  profile changes, its report is archived and the seat must vote again; an old approval
  never carries over.
- **One assignment-validity predicate** decides whether a report counts for the seat's
  current assignment, and every consumer uses it: dispatch, `classification.candidates`,
  round completion and `readiness.review_reasons`. `Controller.review` uses report counts
  and every stored verdict to complete a round, not only its initial lookup of which
  reviewers to call, so all of those uses go through the predicate.
- **Adding a seat** never touches existing findings. **Unseating** a seat that owns open
  or withdrawn findings is refused. There is no dynamic seat removal today, so this is a
  new rule, not an existing one.
- **Legacy records are read, not rewritten.** A report or finding without seat or
  assignment fields is interpreted under the task's pinned static configuration:
  - its seat, owner and origin are all the profile name, taken from where it is recorded
    today: a finding's `provider` field, the `state["reviews"]` key for a report
    (`Controller.record_pr_review` doesn't store `provider` in the report itself), or
    the outbox item's `provider` (`reviews.queue_review`) for an embedded reopen
    report. Identity is never inferred from the model name;
  - its assignment is that profile's static assignment, so it is valid without a new
    vote. The legacy branch of the assignment-validity predicate establishes
    assignment validity only; the existing head, verdict and readiness checks still
    apply separately;
  - format-3 outbox items, finding IDs, opening digests, pending renderings and receipts
    keep their original interpretation. `reviews.report_for` keeps its legacy lookup and
    `opening_matches` keeps verifying historical openings.

  No accepted review goes stale, and no historical publication data is rewritten just to
  fill in new fields.
- **Checkpoint compatibility:**
  - **Additive metadata** that an older engine can ignore without misreading anything
    (for example an assignment revision equal to the static assignment) may be written
    under the current checkpoint version.
  - **Authoritative seat semantics** that an older engine would misinterpret (an owner
    that differs from `provider`, a report bound to a non-static assignment) require a
    **new checkpoint wire version** first. `Store.validate_state` rejects unknown
    versions, but not unknown fields.
  - **Rollback boundary:** once a task has a checkpoint at the new version, it can't be
    served by an older engine.
- **New tasks only:** without `routing`, each configured profile is its own seat with
  seat name = profile name, so existing tasks and checkpoints are unchanged. Routed
  seats with new names apply to new tasks only. Migrating an existing task onto new
  seats would need an explicit, frozen mapping and recovery tests, and isn't proposed.
- **Classification:** `classification.candidates` binds a reopened PR assessment to its
  seat, not its source profile.
- **Sessions:** plan-reviewer transcripts stay scoped by profile identity, so a seat
  whose profile changes starts a fresh transcript. PR reviews have no retained session.

### The implementer's raise

- `develop.json` gains an optional `classification_raise: {complexity?, change_risk?,
  rationale}`, bounded like `classification`.
- **Tolerant:** `report_core.core` removes it before core validation, as it does
  `classification`, and `candidates.development` validates that projection instead of
  the raw report (today it checks the raw field set). Missing, null, malformed or
  unsupported values never invalidate an otherwise valid implementation report. The
  bundle schema and controller change together.
- **A floor, not a vote.** The implementer has seen review feedback, so its raise never
  enters the reviewers' aggregate. It sets an **execution-class floor**, bound to the
  accepted implementation attempt, recorded separately from the aggregate.
- **Componentwise:** each dimension counts only if it is higher than the current routing
  projection's value for that dimension, so a higher complexity can be accepted while a
  lower risk is ignored. An `unknown` current value already counts as `high`, so it
  can't be raised.
- **Computing the floor:** take the frozen input projection of the current execution
  decision, replace only the accepted higher dimensions, and rerun the same matrix and
  adjustments once. The result's maximum with the existing class and floor is the new
  effective class. The confidence uplift is never applied to an already adjusted class.
- **Durable with the attempt:** `Controller.develop` returns a validated candidate, and
  `Controller.implement` later persists it with its pending publication. The floor is
  persisted in that same checkpoint, keyed by the attempt, so it survives recovery
  across that boundary and replay applies it once. Rejected candidates and design-reopen
  responses don't contribute.
- **Effect:** the current attempt is frozen and isn't restarted. The new class applies to
  the next PR round and to the implementer's next attempt, after normal budget and
  credential admission.
- **Lifetime** (decided before phase 3):
  - **A design reopen** never lowers the effective execution class; the floor survives
    it. The redesign's fresh ratings can only raise the class further.
  - **A writer's `/concurro class`** that lowers the execution class explicitly
    supersedes every existing floor. Later evidence (a new raise, a PR round's ratings,
    consensus after a reopen) can raise it again.
  - **An explicit invalidation** (`/concurro invalidate`) clears the floors and the
    execution class, as it already clears the classification fallback; the next
    consensus selects afresh.

### Budget and limits

- **Admission is unchanged:** the next attempts are reserved, as `reserve` does today.
- **Limits** (`max_fix_rounds`, `max_plan_revisions`, `max_model_calls`) and writer grants
  stay as configured. Letting classes vary limits needs a formula over the configured
  value, the class value, grants and spent usage first.
- **Outcomes:** the classes are recorded in `record_outcome`.

### The rubric

Routing makes ratings consequential, and the rubric decides whether most work lands in
`standard` or drifts to the top. Phase 0 keeps the existing definitions and adds
concrete examples per level, grounded in reasoning difficulty and plausible
consequences. The prompts agents receive come from the bundle (`Bundle.instructions`),
so the rubric is a **bundle change**, even while the selector is shadow-only.

## Phasing

Each phase leaves the system consistent if later phases never ship.

**Activation boundary:** when a task starts, its checkpoint records which routing
capabilities are enabled for it, and that set never changes. Shadow observations are
stored separately from active decisions, as bounded records. Upgrading the engine
never turns a task's shadow observations into active decisions; `Worker.load` pins the
config, but an engine upgrade could otherwise change how it is interpreted.

| Phase | Adds | Behaviour change |
| ---- | ---- | ---- |
| 0 | The rubric in the bundle; the shadow selector records classes and panels at each hook, from current assessments, as bounded shadow observations; archived reports feed calibration | Prompts only |
| 1 | Seat identity with seat name = profile name and fixed assignments; owner and origin separated; reports bound to assignment identity; the assignment-validity predicate; the legacy-read rule; additive metadata only, no checkpoint version bump | None |
| 2 | Execution class at `finish_consensus`: implementer and PR seat profiles, `count` fixed. Introduces `assignments(state, config)` with the static design projection, assignment revisions in admission records, effective-panel validation, credential-coverage integration, the checkpoint version bump, and re-voting on a profile change. New tasks only | Yes |
| 3 | The implementer's raise, and escalation after PR rounds | Yes |
| 4 | Design class at intake: plan-reviewer seat profiles and `fresh_design_round` | Yes |
| 5 | Variable `count`, of at least two | Yes |
| 6 (optional) | One-reviewer PR panels | Yes |

**Phase 6** relaxes `_parse`'s two-reviewer minimum through an explicit, PR-only policy
exception; plan reviewers and static configs keep the minimum. It needs evidence that a
single reviewer catches enough defects, not only that the ratings are reliable.

## Invariants

- Every effective panel has distinct provider/model pairs and at least two reviewers
  (until phase 6, for PR panels only).
- A historical approval keeps its original binding. Routing never changes the design
  assignments after planning starts.
- A seat that owns open or withdrawn findings is never unseated.
- A report counts only for its seat's current assignment.
- Originating-profile attribution is immutable.
- Spent budgets are never refunded or reset.
- Decisions are frozen with their evidence references, and replay gives the same result.

## Risks

- **Drift to the top classes.** Mitigations: the rubric, shadow data, and a matrix with a
  single top cell.
- **Gaming:** a reviewer under-rates a change to reduce scrutiny. Mitigations: maximum
  aggregation, reviewer independence, the baseline floor for incomplete or provisional
  evidence, and an execution class that only rises.
- **Cost surprises:** higher classes multiply spend. The task-wide cap bounds this.
- **Lineage is a label.** Two lineages may share more than declared; the warnings only
  check what the operator wrote.

## Open questions

1. Should any class sit below `baseline`, or should routing only escalate?
2. Should `max_fix_rounds` be split into implementation attempts and PR repair rounds
   before classes vary limits?
3. How should routing combine with [per-user credentials](per-user-credentials.md)? Higher
   classes spend more of the sponsor's quota, so a sponsor may need to cap their class.
4. Later: should the two axes drive different things, complexity the implementer and
   consequence the review panel, instead of one class for both?
