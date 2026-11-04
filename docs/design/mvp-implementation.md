# Implementing the runner-only MVP

Status: proposed migration plan, 2026-11-04. Based on checkout `9ea1aa9` and the
current untracked runner proposal. This document authorizes no live action.

Implement [MVP architecture](mvp.md) using the [execution contracts](mvp-execution.md).
The deliverable is a supervised contribution loop with draft-to-ready automation once
CI passes and current agent reviews approve. Do not add preflight, CI execution/repair,
arbitrary issue trackers or a replacement build framework.

## Current code and destination

| Current area | Destination |
| ---- | ---- |
| `service/settings.py`, `worker.py` | Upstream/fork/issue-source routing; recorded config and base commits |
| `service/identity.py`, `gateway.py` | Per-repository permissions; task-bound writes across the pair |
| `registry.py`, `ingress.py`, `reconciler.py` | Explicit upstream PR mapping; CI wakes readiness ticks, never model turns |
| `engine/agents.py`, `relay.py`, `gateway/exchange/exchange.py` | Task/role/session-bound runner turns; no host executor or bench pins |
| `engine/runner.py`, `turns.py`, `agent_entry.py`, `agent.yml` | Native processes, target setup and heartbeat coverage; no execution-image build |
| `controller.py`, `policy.py`, `checks.py` | Direct develop/publish/review/handoff loop; retain pure publication policies |
| `local.py`, `workspace_entry.py`, `bench.py` | Remove after relocating retained publication validation |
| `bundle/` and example config | No profiles, named checks or local-review schema/skill; target instructions |
| `reviews.py`, `conversation.py`, `actions.py`, `checkpoints.py` | Reuse history/recovery; route issue artifacts and upstream reviews separately |
| `service/compose.yaml`, gateway deployment | Keep server containers and stores; remove only execution-image assumptions |

The largest changes are repository routing and the controller's publication path. They
are not solved by deleting modules. Retain the current proven gateway, OIDC verifier,
session journal and outboxes unless a specific interface change requires an edit.

## 1. Establish repository and publication contracts

- Add `upstream`, `fork`, `issues` to service target settings; keep one issue-to-target
  mapping. Reject overlapping/ambiguous enrollment. Record the actual issue repository
  in task, session and branch identity.
- Load config/charter/instructions/setup from one recorded fork `config_sha`; load code
  from upstream's `base_sha`. Keep configuration files out of the contribution tree.
- Separate issue, fork and PR readers/writers in the controller boundary. Update PR
  URLs, finding markers/links and recovery lookup assumptions, not just API prefixes.
- Extend the registry PR association to include PR repository as well as number. Preserve
  existing queue/ownership keys `(issue_repository, issue_number)` and mutation journals.
- Mint/verify the necessary token permissions per destination. Remove linked-branch
  calls; reserve a fork branch through normal Git refs and recover its issue-link comment.
- Retain startup clean-checkout and engine-pin resolution. Remove only image validation.

**Evidence:** upstream #N and fork #N coexist without shared branches, sessions or PR
events; foreign branch/PR writes are rejected; a lost PR/comment response recovers once.
Exercise permission unions for a repository used as both PR and issue destination.

## 2. Bind and execute native runner sessions

- Add task, role, ownership/config binding and session run ref to exchange records using
  the existing migration mechanism. Replace expected-claims equality as the staffing
  key with the stable task/role binding; session refs are unique, not staffing keys.
- Queue only model turns. Remove workbench operations/pins once callers are migrated.
  A fresh design-review phase always requests a fresh job/home.
- Add exact run-ref create/delete and workflow-dispatch gateway operations. Create at
  `config_sha`, bind signed OIDC claims and run attempt, and sweep ended session refs.
- Split runner startup into claim/heartbeat and serve, with `always()` cleanup. Keep
  setup, agent process lifetime and CLI timeouts inside existing job/session deadlines.
- Replace Docker invocation in `turns.execute` with native CLI processes; keep per-turn
  workspaces, bounded outputs, result delivery, transcript pruning and restoration.
  Make runtime paths explicit and inherit the target's toolchain environment.
- Update `agent.yml`: separate Concurro/config/code directories, optional target composite
  setup, pinned CLIs, read-only cache mode, no Docker build. Do not give agents GitHub
  write credentials. Preserve current native gateway client configuration. Point the
  fork caller at `agent.yml@deployed`, retaining the exact resolved engine-SHA check.

**Evidence:** another role/session's OIDC token is refused before binding or granting;
two issues cannot share a session; setup outlasts the heartbeat window; failure cleanup
ends it. A retained turn resumes in a fresh job, and a fresh approval sees no cooperative
history. Lost submit/result responses retain existing idempotency and attempt accounting.

## 3. Replace workbench and merge gates with contribution and handoff

- Simplify plan/develop schemas and prompts: remove named checks, executable validation
  catalogs and local-review contracts. Keep plan intent, scope, design reopens and PR
  review dispositions. Add bounded agent-reported validation text to the develop report.
- Reuse workspace extraction for returned changes. Move safe-path, size, cumulative
  scope and reviewer-context capacity checks into pure controller-side publication
  validation. Extend the edit representation with `content: null` deletions; carry them
  through validation, review context and explicit Git tree deletions. Renames are
  delete-plus-add, with both paths validated as one proposal.
- Replace `develop_locally` with a native develop turn and validated publication. The
  controller constructs the fork commit; do not accept arbitrary agent Git objects.
  Remove `local_evidence_valid` requirements from publishing and handoff.
- Open/recover one upstream draft PR immediately after a publishable candidate. Review
  without waiting for CI; revisions update the same branch/PR and invalidate old votes.
- Replace `gates`/merge automation with review completion and the narrow CI-readiness
  predicate. Mark the bound PR ready through an exact allow-listed GraphQL mutation;
  recover lost replies using `isDraft`. Retain one handoff notification per head.
- Keep check-run/check-suite events, add commit-status wake-ups and map all to the correct
  upstream PR. Poll through the existing reconciler as fallback. Read current CI only
  for readiness/display; do not launch model turns, fetch logs or rerun workflows.
- Keep issue/PR lifecycle and human-input observations. Reuse retry and clarification
  commands, with a bounded continuation after review completion. Respect manual draft
  changes and stop readiness observation when the PR closes or merges.
- Update dashboard wording and archive events. Preserve pending native reviews, finding
  resolution rules and durable blocks after publication failures.

**Evidence:** green CI plus current approvals marks ready exactly once; red, pending,
missing, all-skipped or old-head CI keeps the draft. Mixed success/skipped CI can pass.
Missed webhooks recover by polling; status-only CI works; a lost mutation response
does not duplicate the transition/notification. A CI event starts no model turn.
An independent review finding causes one bounded repair
and re-review. Wrong-head approval, protected-path changes and oversized cumulative
changes cannot publish/hand off. Persistent publication failure stays blocked without
automatic retries past the limit. Handoff and branch links remain duplicate-safe.
Exercise deletion and rename publication/recovery, including a rename whose protected
source makes the whole proposal fail. Deleted paths and lines stay in scope accounting.

## 4. Remove dead code and update operator material

- Remove `HostExecutor`, `host_agents`, `RemoteBench`, bench/execution Docker recipes,
  workbench modules and execution-image settings. Keep service/exchange Dockerfiles.
- Remove execution profiles; move surviving generic path/generated-file policies into
  target config and the small policy module. Do not retain a preset loader just to avoid
  deleting it. Keep lockfile summaries only where existing review context uses them.
- Remove old Actions-controller and host-pilot entry points from supported installation
  paths. Delete dead helpers/tests only after verifying their live consumers are gone.
- Update `bundle/manifest.json`, schemas/skills, packaging, design evaluation, examples,
  CI and gateway proof scripts for the native runner path. Keep offline fake-provider
  testing; replace container-specific tests with process/workflow contract tests.
- Update `README.md`, `docs/concurro.md`, `gateway/README.md`, `service/use_gateway.py`
  and settings examples together. Target setup is optional and cache warming is optional.
  Supply a stacks-core example and a small mixed-toolchain fixture, not a language matrix.

**Evidence:** imports, offline suites, bundle packaging, workflow lint and whitespace
checks pass. Search for removed settings, `local_review`, workbench and image dependencies;
explain intentional legacy readers rather than adding compatibility branches everywhere.
No supported command starts model/build work on the operator's Mac or service host.

## Migration and first live proof

Use a coordinated service/exchange/workflow update; there is no requirement to run both
executor architectures simultaneously. Keep intermediate changes on one implementation
branch until the fake end-to-end loop works. These are implementation slices, not four
mandatory deployments or approval ceremonies.

1. Stop new enrollment and let active turns finish or end them explicitly. Preserve audit,
   registry, exchange and retained-session stores; back up databases consistently before
   migration. Do not reset them to bypass unresolved writes.
2. With dispatch paused, install schema changes and the reviewed engine/workflow version
   on the existing host. Create or move Concurro's `deployed` branch to that same commit
   as an operator release step; it is not a task gateway write. Update the fork's
   configuration/caller to use that ref. Resume dispatch only when workflow and service
   commits match. Keep server secret locations and edge routes. No agent/execution image
   build or local-Mac service is needed.
3. Start with new pilot issues. Old tasks with obsolete bundle/workbench state remain
   readable and block for explicit reconciliation; do not promise automatic continuation.
   Cancel/end legacy unscoped runner sessions before enabling the new dispatch path.
4. Use a maintainer-approved test upstream and existing fork for the contribution proof.
   Enroll from each supported source, confirm a fork branch link, an upstream draft PR,
   native reviews and handoff. Green CI plus approvals marks ready; neither condition
   alone does. Nothing should create a branch in upstream or merge.
5. Exercise one review repair using a deterministic fixture, or authorized human feedback
   if the model finds nothing. Do not rely on a real model making a convenient mistake.
   Reuse the retained-session proof across two jobs and one setup-heartbeat failure case.
6. Confirm that CI only drives readiness, with webhook and polling recovery; it never
   starts a model or reruns a job. Confirm cache misses work. If a trusted cache exists,
   verify restore works and a job cannot upload.
   This is a focused hosted-runner proof, not a prerequisite cache-warming project.

Automatic migrations must preserve unknown mutation records and reject incompatible
state visibly. For rollback, stop writers/sessions first. Restore the previous engine
with a compatible saved database; never run an old binary against a new schema on the
assumption that it will work. Restore `deployed` to the same previous engine commit
before resuming dispatch. Keep already-published branches, PRs and audit artifacts.

## Completion criteria

- One public target can complete the entire loop without operator-machine execution.
- Fork and upstream identities remain distinct under retries and concurrent tasks.
- Native agents can build and experiment without a Concurro check catalog or image.
- Only the controller publishes validated changes; every review is tied to the head it
  reviewed; recovery cannot silently duplicate public output.
- A reviewed PR becomes ready when CI passes; otherwise the dashboard shows why it stays
  draft. The watcher uses no model quota and does not manage CI or merge policy.
- No preflight, required-check discovery, log ingestion, rerun automation, linked-branch
  API or automatic merge path remains in the supported MVP flow.

Use the existing engine/service/gateway suites for regression coverage and add tests for
changed contracts. Do not add a new benchmark, exhaustive mutation campaign or production
monitoring system as a condition of shipping this MVP.
