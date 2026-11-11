# Proposals

**Not implemented.** These are design briefs for possible future work. They describe
intended behaviour, not current behaviour; for how Concurro works today, see
[`docs/design/`](../design/) and the code.

Each proposal records the current state it builds on, the proposed design, the invariants
it must keep, the phasing and open questions. Implementation detail that isn't needed to
understand the design is staged per phase in a directory named after the proposal: input
for that phase's implementation brief, re-checked against the code when the brief is
written. A proposal moves out of this directory once
it is implemented (its design notes go to `docs/design/`) or abandoned (it is deleted).

| Proposal | Kind | Summary |
| ---- | ---- | ---- |
| [Demo target policy](demo-target-policy.md) | Approved direction | Default-open text edits, explicit protections and configurable workload limits; rollout requires engine support and replacement tasks. |
| [Per-user provider credentials](per-user-credentials.md) | Epic | Each person's own provider credentials pay for the work they trigger. |
| [Risk-driven routing](risk-driven-routing.md) | Feature | Use complexity and change-risk estimates to choose models, effort and review depth. |
| [Consolidated PR reviews](consolidated-pr-reviews.md) | Feature | Post one GitHub review per round with attributed findings; later, group duplicates and handle contradictions. |
| [Backoffice API surface](backoffice-api.md) | Epic (inventory) | Endpoints for a GitHub-authenticated backoffice: own credentials, task overview, commands and run diagnostics. |

## Cross-cutting rules

These apply wherever the proposals meet:

- **One frozen admission record per admitted attempt.** The routing and profile identity
  with its assignment revision, the sponsor and credential version, and the attempt are
  fixed when the attempt is admitted (`RunnerExecutor.binding`, `sessions.prepare`,
  `design_batch.Design.reserve`). A later assignment or sponsor change never rebinds an
  admitted attempt.
  - **Replay** of that attempt preserves the record; it is never recomputed from current
    state.
  - **A new, charged retry** is a new attempt and gets a new record.
  - Nothing here recovers an old exchange result across ownership generations, which
    current recovery deliberately avoids (`Design.open`).
- **Approval identity and payment identity are separate.** The approval describes what was
  reviewed; the sponsor describes who paid for it.
- **Finding ownership is authoritative.** The owner is the reviewer **seat**: today a
  seat is a reviewer profile name; routing makes it a configured seat whose profile can
  change ([risk-driven routing](risk-driven-routing.md#seats-own-findings)). The
  originating profile stays recorded as immutable attribution. Neither routing nor
  consolidation may unseat an owner, or transfer a finding, while the finding is open or
  withdrawn. This holds at every assignment point, including after a design reopen, and
  for every effective panel.
- **Budget admission is central.** Every attempted model call, including consolidation
  and credential retries, passes budget admission (batch reservation or the
  next-attempt check against `max_model_calls`) and is charged
  (`Controller.charge_call`, which counts calls but doesn't enforce the cap).
- **Required and optional roles are distinct.** Credential preflight covers required
  roles only, so an optional role, such as the PR consolidator, never puts a task in
  `awaiting_credentials`. An optional call still passes budget admission; if admission
  fails, the deterministic path is used.
  - **Protected capacity:** an optional call is admitted only if
    `spent calls + optional next attempts + known required uncharged attempts <=
    max_model_calls`. "Known required" means work already pending, for example the
    current round's remaining reviewers and the next repair turn; attempts that
    `Design.reserve` has already charged are counted once, as spent. The check runs
    before every optional charge, retries included, and a failure takes the
    deterministic path. It needs a policy helper, not a new persistent reservation.
    Admission still reserves only next attempts, so unknown future repairs aren't
    protected.
- **Optional work fails open to the deterministic path.** A missing consolidator
  credential or an exhausted optional allowance falls back to deterministic publication.
  It never blocks the publication of reviews that were already accepted. This is only
  for an unavailable optional model: lost ownership or revoked execution authority still
  stops the work.
- **Reference evidence; don't copy it.** Sponsorship, routing and cluster records refer to
  immutable reports instead of duplicating them.
