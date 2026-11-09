# Routing phase 1: seat identity (staged notes)

Staged input for the phase 1 implementation brief of
[risk-driven routing](../risk-driven-routing.md). Not a brief: re-check every code fact
when the brief is written.

## Scope

Introduce seats with seat name = profile name and today's fixed assignments, so later
phases can change a seat's profile without changing who owns its findings. **No
behaviour change and no checkpoint version bump.**

## Code facts

PR reviewer identity is the profile name today:

- `state["reviews"]` is keyed by it, and `Controller.review` skips a reviewer that
  already has a report there. `Controller.record_pr_review` doesn't store `provider` in
  the report itself.
- `reviews.owned`/`context` key ownership by it, and only the owner dispositions.
- Outbox items carry it (`reviews.queue_review`), and `reviews.report_for` uses it to
  find the report.
- `finding_body` and `opening_matches` include it as attribution in thread verification.
- `readiness.review_reasons` iterates `assigned(config, "pr_reviewers")`.
- `classification.candidates` finds a reopened PR assessment by its source profile.
- `Controller.review` also uses report counts and every stored verdict to complete a
  round, not only its initial lookup of which reviewers to call.
- `sessions.prepare` scopes planning, design-review and implementation transcripts by
  profile identity; PR reviews get no retained session.
- `Store.validate_state` rejects unknown checkpoint versions, but not unknown fields.

## Two identities

- **The seat** is the continuing review authority: it owns findings and dispositions
  them.
- **The originating profile** is immutable attribution: who raised the finding, kept in
  the finding, its opening body and digest.

In phase 1 both are the profile name. Later, a different model in the same seat may
disposition a finding; that is intended.

## Reports bound to assignments

- Each report records its seat, assigned profile identity (effort included) and an
  assignment revision.
- **One assignment-validity predicate** decides whether a report counts for the seat's
  current assignment. Dispatch, `classification.candidates`, round completion in
  `Controller.review` and `readiness.review_reasons` all use it.
- `classification.candidates` binds a reopened PR assessment to its seat.

## Legacy records

Read, never rewritten. A report or finding without seat or assignment fields is
interpreted under the task's pinned static configuration:

- seat, owner and origin are the profile name, from where it is recorded today: a
  finding's `provider`, the `state["reviews"]` key for a report, or the outbox item's
  `provider` for an embedded reopen report. Never inferred from the model name;
- its assignment is the static one, so it stays valid without a new vote. The legacy
  branch of the predicate establishes assignment validity only; head, verdict and
  readiness checks still apply;
- format-3 outbox items, finding IDs, opening digests, pending renderings and receipts
  keep their interpretation; `report_for` keeps its legacy lookup and `opening_matches`
  keeps verifying historical openings.

No accepted review goes stale.

## Checkpoint compatibility

Phase 1 writes only **additive metadata** an older engine can ignore without misreading
anything (for example an assignment revision equal to the static assignment), under the
current checkpoint version. Anything an older engine would misinterpret waits for
[phase 2](phase-2-execution-class.md#checkpoint-version).

## Ownership amendments

The proposals README's cross-cutting ownership rule and
[consolidated reviews](../consolidated-pr-reviews.md) already name the seat as owner and
the originating profile as attribution.
