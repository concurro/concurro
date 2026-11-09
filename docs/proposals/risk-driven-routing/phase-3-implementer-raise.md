# Routing phase 3: implementer raise and PR-round escalation (staged notes)

Staged input for the phase 3 implementation brief of
[risk-driven routing](../risk-driven-routing.md). Not a brief: re-check every code fact
when the brief is written.

## Scope

- The implementer may raise the ratings, setting an execution-class floor.
- At the end of `Controller.review`, before repair resets the reports, the PR-review set
  may raise the execution class.

Both only raise. **G4 for this phase:** approve the raise normalization, floor lifecycle
and escalation boundaries below.

## Code facts

- `candidates.development` requires exactly `{summary, validation, edits,
  design_issue}`.
- `report_core.core` removes only `classification` before core validation.
- `Controller.develop` returns a validated candidate; `Controller.implement` later
  persists it with its pending publication.
- `CommandHandler.invalidate_task` removes `classification_snapshot` and resets current
  assessments.

## The raise

- `develop.json` gains an optional `classification_raise: {complexity?, change_risk?,
  rationale}`, bounded like `classification`.
- **Tolerant:** `report_core.core` removes it before core validation, and
  `candidates.development` validates that projection. Missing, null, malformed or
  unsupported values never invalidate an otherwise valid report. Schema and controller
  change together.
- **A floor, not a vote:** the implementer has seen review feedback, so its raise never
  enters the reviewers' aggregate.
- **Componentwise:** each dimension counts only if higher than the current routing
  projection's value. An `unknown` current value already counts as `high`.
- **Computing the floor:** take the frozen input projection of the current execution
  decision, replace only the accepted higher dimensions, rerun the matrix and
  adjustments once, and take the maximum with the existing class and floor. The
  confidence uplift is never applied twice.
- **Durable with the attempt:** persisted in the checkpoint where `Controller.implement`
  persists the candidate, keyed by the attempt, so replay applies it once. Rejected
  candidates and design-reopen responses don't contribute.
- **Effect:** the current attempt isn't restarted. The new class applies to the next PR
  round and the implementer's next attempt, after normal budget and credential
  admission.

## Lifetime

- A design reopen never lowers the effective execution class; the floor survives, and
  fresh ratings can only raise it.
- A writer's `/concurro class` that lowers the execution class supersedes every floor.
  Later evidence can raise it again.
- `/concurro invalidate` clears floors and the execution class, as it already clears the
  classification fallback.

## PR-round escalation

Owners are kept at every assignment point, including `finish_consensus` after a reopen.
An escalation whose effective panel would duplicate a provider/model pair is refused and
recorded; a model is never duplicated and an owner is never unseated to make it fit.
