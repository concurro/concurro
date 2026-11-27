# Proposals

**Status reconciled against the implementation on 2026-11-27.** This directory contains
implemented design records as well as unfinished proposals. The table distinguishes
shipped scope from remaining work; a retained brief is not evidence that its feature is
still pending. Historical phase notes and test reports retain their original baselines.

Implementation, deployment and live qualification are separate. In particular, the
Phase 5 cutover and connection checks are complete. Local Antigravity provider turns and
refresh checks also passed; deployed Actions/provider qualification and final legacy-source
retirement remain open. See the
[Phase 5 closeout](architecture/phase-5-plan.md#implementation-status).

| Proposal | Status | Implemented / remaining |
| ---- | ---- | ---- |
| [Architecture review](architecture.md) | Partly complete | Phases 1–4 complete; Phase 5 implementation and cutover complete, deployed Actions/provider qualification and source retirement pending. |
| [Service contracts](contracts.md) | Partly implemented | Current Rust APIs/events have generated types and tests. Explicit later contracts and mention interpretation remain deferred; [scenario traces](contract-scenarios.md) are acceptance designs, not a test-pass ledger. |
| [Per-user provider credentials](per-user-credentials.md) | Implemented | Task funding and accounts/OpenBao support OpenAI, Anthropic and Google. Server-stored personal-token refresh remains deferred; Antigravity Sponsor renewal delegates to agy. |
| [Ordered personal credentials](credential-list.md) | Implemented | CRUD, ordering, role/profile filters and same-payer fallback. Phase 5 paid fallback still needs live qualification. |
| [Sponsor-held credentials through a relay](sponsor-relay.md) | Implemented; deployment qualification remains | Claude, ChatGPT and Antigravity routes use protocol 6 on main. Local joined Antigravity inference and refresh passed; v0.1.2/protocol 5 is the earlier deployed baseline. |
| [Request intake](request-intake.md) | Partly implemented | Durable requests from exact comments, browser commands, labels and review requests. Assignment, mentions and additional channels remain deferred. |
| [Mentions and assignment](mentions.md) | Deferred | Neither assignment enrollment nor exact/free-text mentions is implemented. The shared intake foundation is ready. |
| [Risk-driven routing](risk-driven-routing.md) | Deferred | Descriptive complexity/risk ratings exist; automatic model, effort and review-depth selection does not. |
| [Consolidated PR reviews](consolidated-pr-reviews.md) | Deferred | Reviews still publish per reviewer turn. Round-wide consolidation, deduplication and contradiction handling remain future work. |
| [Retrospective tuning](retrospective-tuning.md) | Partly implemented | Legacy phase-0 collector and snapshot replay exist. Rust-source collection, analysis, tuning PRs and scheduled evaluation remain deferred. |
| [Backoffice API surface](backoffice-api.md) | Partly implemented; inventory superseded | Core auth, credentials, task views/commands, diagnostics, SSE, configuration visibility and read-only Operator shipped. Preferences and the broader operator-write inventory are not implemented. |
| [Webhook-fed list index](list-index.md) | Implemented; storage replaced | Rust task/index and backoffice projections replace the original Python/SQLite implementation. |
| [Demo target policy](demo-target-policy.md) | Implementation complete | Engine and Rust policy support exist; individual target configuration is an installation rollout, not an outstanding code feature. |
| [Issue artifact consolidation](issue-artifact-consolidation.md) | Complete historical implementation | Engine artifact consolidation shipped. Rust task recovery now uses PostgreSQL; retained artifact notes describe the original engine storage contract. |

## Implementation records

- [Phase 1](architecture/phase-1-plan.md): contracts, shared database/event crates and platform.
- [Phase 2](architecture/phase-2-plan.md): Rust ingress, recovery and webhook cutover.
- [Phase 3](architecture/phase-3-plan.md): task application, funding, publication and read API.
- [Phase 4](architecture/phase-4-plan.md): accounts service and Python backoffice retirement.
- [Phase 5](architecture/phase-5-plan.md): exchange, gateway, native credentials and OpenBao;
  remaining qualification and retirement are listed separately from completed slices.
- [Antigravity](../antigravity.md): Google profiles, Sponsor login renewal and shared TLS
  KeyUpdate transport; [joined-test evidence](../reports/antigravity-joined-evidence.md)
  records fresh/resumed provider turns and the remaining qualification limits.

## Deferred feature work

The rewrite did not implicitly implement the feature backlog. Assignment and exact
mentions can build on the existing request ledger; the model interpreter and per-task
mode/approval choices need their own implementation brief. Routing, consolidated reviews
and retrospective tuning likewise need their proposals rechecked against Rust interfaces
before implementation. No Rust rewrite of the retained Python engine/runner is scheduled.

## Cross-cutting rules

These apply wherever the proposals meet. Rules for deferred features are design
constraints, not claims that those features already run:

- **One frozen admission record per admitted attempt.** The routing and profile identity
  with its assignment revision, canonical provider slot, consenting sponsor, slot epoch
  and credential version, and the attempt are
  fixed in the task application's admitted attempt and immutable exchange grant.
  Only the selected slot enters its binding; another
  provider's transfer/withdrawal never changes that session. A later change never rebinds an
  admitted attempt.
  - **Replay** of that attempt preserves the record; it is never recomputed from current
    state.
  - **A new, charged retry** is a new attempt and gets a new record.
- **Provider grants are source-bound and task-local.** Confirm explicit issue-comment
  sponsors before initial start/label fills uncovered slots from the initiator's
  available route credentials. Existing explicit sponsors keep priority even with missing
  credentials. Persist pre-enrollment intents; reconcile source order, not webhook arrival,
  before charging. Fill uses the start/label event boundary: later removals cannot make
  the initiator pay more. An earlier withdrawal prevents only that actor's auto-
  reassignment (even without a previous grant), not another initiator's own grant.
  Afterwards withdrawal/failure/retry/relabel never select another sponsor automatically.
  [Ordered personal credentials](credential-list.md) implements fallback between the same
  sponsor's eligible credentials, preserving that provider grant and funding choice.
  `sponsor <provider>` assigns the caller; self `withdraw <provider>` retires only their
  selected-provider work. Independent epochs/tombstones survive; required batch coverage
  spans all owners, optional missing coverage takes the deterministic path.
- **Funding-mode changes preserve consent.** Org-era starter/label grants are historical
  attribution, not permission to start charging that person when contributed funding is
  enabled. Explicit sponsor commands authorize future contributed calls; show that scope
  alongside org-funded status. Existing attempts keep their recorded mode and payer.
- **Provider pauses preserve unrelated admitted work.** Stop new admissions and drain
  unaffected reviewers while their authority remains valid; then suspend. Task-authority
  loss or work invalidation still follows whole-batch cancellation.
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
  through the task application's funding module; exchange admission also enforces the
  submitted grant's allowance.
- **Required and optional roles are distinct.** Credential preflight covers required
  roles only, so an optional role, such as the PR consolidator, never puts a task in
  `awaiting_credentials`. An optional call still passes budget admission; if admission
  fails, the deterministic path is used.
  - **Protected capacity:** an optional call is admitted only if
    `spent calls + optional next attempts + known required uncharged attempts <=
    max_model_calls`. "Known required" means work already pending, for example the
    current round's remaining reviewers and the next repair turn; attempts that
    the task application has already charged are counted once, as spent. The check runs
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
