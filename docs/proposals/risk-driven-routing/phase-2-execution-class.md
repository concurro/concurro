# Routing phase 2: execution class (staged notes)

Staged input for the phase 2 implementation brief of
[risk-driven routing](../risk-driven-routing.md). Not a brief: re-check every code fact
when the brief is written.

## Scope

At `finish_consensus`, select the execution class and apply it to the implementer
profile and the PR seats' profiles. Seat `count` stays fixed. New tasks only.

## Decisions needed first

- **G1:** whether any class may sit below `baseline`. May be settled conservatively at
  first by prohibiting it.
- **G2:** one class for both implementer and panel, or separate axes. "One class
  initially; separate axes in a later policy version for new tasks" settles it.
- **G3a:** routing coverage on existing routes (below).
- **G4:** the matrix, the implementer and PR seat mappings, and the compatibility
  contract with consolidated reviews (below).

## Code facts

- Profiles are exactly `{provider, model, effort}`, with `provider` in
  `model_config.HARNESSES` (`anthropic`, `openai`) and effort from `EFFORTS`.
- `model_config._parse` requires each reviewer role to have at least two profiles with
  distinct provider/model pairs; effort doesn't count.
- `policy.approval_binding` includes `config_digest` (every config field except
  `engine`) and `design_roster(config)`; `approval_binding`, `consensus_valid`, `Design`
  dispatch and `classification.candidates` all read `design_roster`. A changed effective
  config blocks (`_validate_authority`) or makes the approval stale.
- For named-profile configs the design roster is the plan reviewers with full profiles,
  effort included; PR reviewers are covered only through `config_digest`. Legacy configs
  bind the PR reviewers in the roster directly.
- `Worker.load` pins the config per task, but an engine upgrade can change how it is
  interpreted.
- `gateway/agentgateway.yaml` routes only `gpt-*` and `claude-*`.
- `Controller.review` counts `state["round"]` against `max_fix_rounds`.
- `design_batch.Design.reserve` reserves only the next attempts.

## Config validation

- Seats reference the existing `models` profiles; routing adds no profile fields, and
  lineage is a separate per-profile mapping.
- Plan and PR seats are separate, with role-qualified names: their rosters, session
  lifecycles and assignment boundaries differ.
- Capability ordering within a seat is an operator assertion; it can't be inferred from
  names or effort.

Rejected unless:

- `classes` is non-empty and unique; `baseline` and every matrix value name a class.
- The matrix has all nine cells and is monotonic: one step up along either axis never
  lowers the class index. (This proves monotonic class selection, not increasing model
  capability.)
- Seat names are unique; every seat and the implementer map every class to a known
  profile.
- `count` is an integer from 2 to the number of seats per class, and doesn't decrease
  along the class order.
- Every effective panel (the first `count[class]` seats, per class) has distinct
  provider/model pairs.
- `fresh_design_round` lists known classes and is upward-closed.
- The config uses named profiles (`agents`); legacy configs with `routing` are rejected.

Warnings only: a seat whose profiles span lineages; two seats on a panel with the same
lineage; a panel with no lineage different from its implementer's; a routed profile
without a declared lineage. A missing lineage never counts as independence.

## Lineage, harness and route

Three separate things. A model such as GLM may run through the Claude harness and declare
its own lineage, but making it executable needs a gateway route, authentication, model
allowance and compatibility checks. Credential coverage is by route, not lineage.

## Resolved assignments

- `assignments(state, config)` returns every role's effective seats and profiles from the
  policy and the frozen class decisions. Without `routing`, it returns exactly today's
  configuration.
- Consumers read projections: `approval_binding` and `consensus_valid` the design
  projection only; dispatch its role's assignment; `classification.candidates` and
  `readiness.review_reasons` the effective seats.
- Until [phase 4](later-phases.md), the design projection is the static design roster
  and today's fresh-review policy. `approval_binding` keeps its exact representation.
- Classes and assignments live in task state; the `config` object is never modified.

## Decision records

Each decision stores the policy version and digest, the class, the matrix cell, and the
frozen input projection: labels, whether the previous estimate contributed, `complete`
and `provisional`, and each adjustment. Reconstruction uses that record, never a later
snapshot. The execution class is selected after valid consensus even when ratings are
incomplete; routing adds no classification-completeness gate. Active classes are
recorded in `record_outcome`.

## Activation boundary

When a task starts, its checkpoint records which routing capabilities are enabled, and
that set never changes. Shadow observations from phase 0 never become active decisions.

## Admission and coverage (G3a)

- Each assignment carries a revision, which joins the frozen admission record of every
  attempt.
- Coverage for a newly selected required profile is checked before admission.
- A profile change never substitutes another credential or rebinds an admitted attempt.

## Re-voting

When a seat's profile changes, its report is archived and the seat votes again; an old
approval never carries over. Plan-reviewer transcripts stay scoped by profile, so a
changed seat starts a fresh transcript.

## Checkpoint version

Authoritative seat semantics an older engine would misinterpret (an owner that differs
from `provider`, a report bound to a non-static assignment) require a new checkpoint
wire version first. Once a task has a checkpoint at the new version, an older engine
can't serve it.

## Consolidated reviews compatibility

The round identity includes the assignment revision. An assignment change starts a new
review generation without incrementing `state["round"]`; sealed segments stay
publishable, and a new generation doesn't by itself allow a publication-mode switch.
Full consolidated publication needn't ship first, but must agree with this.

## Human override

`/concurro class <name>`, recorded with its author, applies at the next decision point.
Lowering is allowed for the execution class only, and is refused if it would unseat a
seat that owns open or withdrawn findings.

## Migration

Routed seats with new names apply to new tasks only. Migrating an existing task onto new
seats would need a frozen mapping and recovery tests, and isn't proposed.
