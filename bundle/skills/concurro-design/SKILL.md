---
name: concurro-design
description: Plan a repository change before implementation, or revise a plan after review. Define architecture, edit scope, implementation steps and observable acceptance tests.
---

# concurro-design

## Design and scope

Settle architecture, decisions and risks from requirements, charter and source inventory;
provide implementation steps and observable acceptance tests. `files` identifies distinct
primary files, not every predicted edit. `scope.paths` covers the design and mechanical
follow-ons (callers, imports, tests), using distinct narrow directories/globs and no broader envelope.
Treat dependency changes as design decisions with risks; include their manifests and lockfiles
in scope. For an existing PR, include every accumulated changed file or escalate through questions.

Check `plan_output_contract` for effective edit policy and workload `limits`; null means no
configured cap. Do not invent file allowances or omit required work to fit a limit: ask for a
policy decision. Static file restrictions apply inside approved scope. If required code or docs
are forbidden, request a target-policy decision; a writer's prose cannot override file permissions.

`tests` describes observable acceptance criteria and useful commands. The implementer chooses
validation in the target environment; impose no check catalog. Put full design and validation
detail in `summary`, `steps` and `tests`.

## Revisions

Consider consolidated objections and relevant prior outcomes as evidence. Return one `responses`
entry per supplied open objection, with its exact ID and the change or reasoned disagreement;
use an empty array when none exist. Only the reviewer resolves an objection. Questions stop
implementation.

## Report and publication

Return the supplied [response contract](../../schemas/plan.json), within 20,000 JSON characters.

- `headline`: imperative change summary under 60 characters, used as the PR title; no `Plan:`
  prefix. Keep the plan summary about the design, without earlier verdicts or approval claims.
- `publication.changes`: concise changes since the previous version.
- `publication.review_focus`: points needing reviewer attention.

Each publication list has 1–5 non-empty bullets of at most 300 characters. These appear in the
chronological issue comment; the full plan belongs in its artifact. Do not repeat full steps
or tests in publication lists.
