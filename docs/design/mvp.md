# Concurro MVP: a reviewed draft PR

Status: proposed implementation design, 2026-11-04. Records the simplified MVP agreed
with the operator; no implementation or deployment is implied.

Concurro takes an issue, plans a contribution, implements it, obtains independent
agent reviews, and hands a PR to maintainers. It opens the PR as a draft and marks it
ready for review when CI passes and all configured agent reviewers approve the current
head. It observes CI; it does not run it or decide whether GitHub will allow a merge.

Read this document first, then the [execution contracts](mvp-execution.md) and
[implementation plan](mvp-implementation.md). These three documents govern the new
MVP where earlier designs disagree. The previous [runner proposal](runner-roles.md)
is historical. Existing service recovery, report archives, comment formatting and PR
finding behavior remain useful foundations, not invitations to rebuild them.

## What v1 delivers

- All model work runs on ephemeral GitHub-hosted runners, with native CLI tools and a
  shell. No agent or build process runs on the operator's Mac or the controller host.
- The operator supplies an existing upstream/fork pair. Task branches and runner refs
  live only in the fork; draft PRs target upstream.
- Issues come from upstream, the fork, or both. No separate tracker integration.
- Target-owned setup and instructions accommodate Rust, Node, .NET or mixed projects.
  Concurro does not define their builds, tests, packages or dependency installation.
- Planning, independent reviews, bounded revisions, retained conversations and visible
  history remain. Removing local checks does not remove agent collaboration.
- Provider credentials and GitHub write credentials stay central. Agent jobs receive
  only their limited gateway/session credentials and GitHub's job credentials.
- PRs start as drafts. Concurro marks them ready when CI and its reviewers are satisfied,
  but never submits GitHub APPROVE reviews or merges. Human review remains separate.

## Deployment

Reuse the existing small Linux-host deployment: controller service, exchange,
agentgateway and the shared HTTPS edge. Keep their current containers and persistent
stores. Removing execution images does not mean removing server containers.

| Component | Responsibility |
| ---- | ---- |
| Controller service | Enrollment, planning/review state, dispatch, publication, CI observation and readiness |
| Mutation gateway (inside controller service) | All GitHub writes, task ownership and recovery after uncertain writes |
| Exchange | Issued sessions, OIDC binding, turns, results and session-key lifecycle |
| agentgateway | Native provider protocols, central provider credentials and usage controls |
| GitHub-hosted jobs | Agent tools, source workspaces, optional builds and exploratory tests |
| GitHub/maintainers | Project CI, workflow approvals, required checks, readiness and merge decisions |

There is no host-executor fallback, Docker workbench, self-hosted execution runner or
new scheduler/database product. Keep the existing single service instance, SQLite
registries and restartable controller ticks.

## Target and issue identity

The operator's service settings select the repositories and enrollment sources:

```json
{
  "targets": [
    {
      "upstream": "stacks-network/stacks-core",
      "fork": "cylewitruk/stacks-core-autobot",
      "issues": "both"
    }
  ]
}
```

`issues` is exactly `upstream`, `fork` or `both`; it never changes the publication
destination. Require an existing compatible fork and App access to both repositories.
Do not create or synchronize forks. Reject overlapping enrollment configurations that
would route one issue to different targets.

A task keeps the existing `(issue_repository, issue_number)` key and records its target
pair. Thus upstream #42 and fork #42 are different tasks. Use an opaque task-derived
branch suffix rather than the issue number alone. Persist the fork branch and upstream
PR identity; never infer them from another repository's issue number.

The issue's own repository holds its dashboard, audit, plan and report archives.
Upstream holds the PR and native review threads. No linked-development-branch API is
used. A normal, recoverable issue comment links the fork branch and later the PR.

## Sources and target setup

Record two independent commits when enrolling a task:

- `base_sha`: upstream code approved for the task.
- `config_sha`: the fork commit supplying `.concurro/` and the runner caller workflow.

The fork's configuration files do not enter the candidate tree or upstream PR. Read
configuration and setup from `config_sha` for the task's lifetime. New default-branch
configuration applies to new tasks; changing an existing task's configuration requires
an explicit replan/reconciliation, not a silent mid-turn switch. Preserve engine/bundle
identity checks when deploying new engine code. Bundle drift blocks model work, not
closure or CI/readiness observation. A writer uses `/concurro invalidate` to adopt
the current bundle and restart assessment; `/concurro retry` retains the existing
bundle binding. Invalidation preserves the PR and spent budgets and records the change.

Upstream advancing alone does not restart a task. An explicit base change goes through
the existing replan path and invalidates affected approvals and reviews.

The target supplies its charter, [named model profiles and agent assignments](../concurro.md#models-and-agents), path policies and
resource limits, plus these optional files:

| File | Purpose |
| ---- | ---- |
| `.concurro/setup/action.yml` | Install job-wide tools and restore download caches |
| `.concurro/instructions/shared.md` | Project-wide agent guidance |
| `.concurro/instructions/planner.md` | Planning guidance |
| `.concurro/instructions/design-reviewer.md` | Design-review guidance |
| `.concurro/instructions/implementer.md` | Implementation and suggested validation |
| `.concurro/instructions/reviewer.md` | Code-review guidance |

The shared file applies to every role, including intake. These are bounded prompt
inputs below the engine's charter/role rules and above issue/code content. This is
instruction precedence, not a guarantee against prompt injection. Upstream contribution
documents remain available as reference material.

Setup may call scripts or existing Actions. Install checkout-local dependencies inside
the candidate workspace when needed, using the agent's tools; do not assume the setup
checkout's `node_modules` or build directory exists there. No named-check API, execution
profiles, package-scoping engine or generated CI workflows remain.

## Contribution flow

1. **Enroll and plan.** Keep existing writer enrollment, charter assessment, clarification,
   plan artifact and independent design review. Unanimous approvals on each reviewer's
   first session turn authorize the plan directly: those contexts are already fresh.
   Reviews that resume after revisions or output corrections still need a separate fresh
   approval round. Manual mode also requires writer approval. Directory scopes such as `changelog.d/`
   include their descendants. Rejected planner output is linked from the dashboard's
   plan artifact with its rejection reason, separate from the approved plan. The planner
   gets two automatic correction attempts within the existing model-call budget; persistent
   errors escalate for `/concurro retry`. Oversized rejected output is explicitly excerpted.
   Correcting output does not spend a semantic design-revision round.
   Intake may proceed with advisory concerns, carried explicitly to the planner and
   reviewers and posted once as a concise issue comment. Unanswered questions and charter
   conflicts still prevent proceed; design-review approval still requires no blocking
   concerns. The comment is informational, not an additional approval gate.
   Invalid intake reports also receive two automatic corrections, with the rejection reason
   and bounded output in a write-once report archive linked from the dashboard. Genuine
   clarification, rejection and escalation decisions still stop for human input. A writer's
   retry renews the correction allowance without resetting spent model calls. Transport
   failures retain their own recovery path. Rejected output never authorizes planning.
   Reviewers receive the complete substantive plan, including its summary; publication
   hints and peer verdicts are omitted. New objections use an empty ID until the controller
   assigns one. Output-correction feedback identifies objection ID and concern/disposition
   mismatches by field, without discarding the reviewer's concern. Incorrect citation lines
   are corrected only when the exact quote uniquely matches in the same pinned file.
   Each reviewer and design binding has a bounded correction allowance, renewed by an
   explicit writer retry. Repeated identical failures still escalate; retries preserve
   the task's total model-call budget and correction history.
2. **Implement.** A role job edits the approved code with native tools. It may build,
   install dependencies and test hypotheses. It returns changes plus an explanation and
   clearly labeled, agent-reported validation. Meaningful scope growth can reopen design.
3. **Publish.** The controller validates the changes and publishes them on the fork branch.
   Create/recover the branch when implementation begins and post its link once; open the
   upstream draft PR after the first publishable candidate. Do not wait for CI.
4. **Review and revise.** Independent providers review the exact published head. Native
   COMMENT reviews and finding threads remain. Open findings feed another bounded
   implementer turn, followed by publication and re-review. There is no local-review stage.
5. **Hand off and watch readiness.** Once every configured reviewer has a current
   approving report, findings are addressed and publication is complete, record review
   completion. If CI is already green, mark the PR ready and post the handoff. Otherwise
   keep it draft, show the CI state and link the PR; a small controller watcher marks it
   ready when CI becomes green. No runner or model waits for CI.

CI readiness uses GitHub's current PR check/status results, not a custom required-check
catalog. Missing results, pending work, failures or workflow-approval waits do not count
as a pass. The [execution contract](mvp-execution.md#reviews-handoff-and-readiness) defines
the small predicate. This is not a claim that every branch-protection rule is satisfied.
Agent-reported commands and outcomes remain useful evidence, not verified test receipts.

After code review completes, stop autonomous model turns. CI webhooks and the existing
reconciler can advance readiness, but never start repair agents or rerun jobs. Existing
authorized issue commands can request another bounded round; maintainers can supply CI
failures as task feedback. PR closure or merge stops readiness observation. No automatic
PR-comment repair protocol is required for this MVP.

## What remains enforceable

Retain session authentication, centralized write authorization, bounded exports, protected
paths, approved scope and file limits, exact-head review freshness, time/spend limits and duplicate-safe
publication. These protect what Concurro may do; they do not certify the candidate's
behavior. Do not add exact-file completion requirements or new language-specific rules.

An agent can compromise its own job, spend its available session allowance and submit
misleading output. A reviewer executing candidate code accepts that same risk. Separate
jobs prevent direct memory/filesystem sharing, not malicious influence through source.
Human review and the project's CI remain necessary. Fork PRs are not a universal secrets
barrier: operators must avoid privileged workflows that execute candidate code with secrets.

Plans have no changed-line budget. Reviewers judge whether the work is coherent and
reviewable; a large diff alone does not require revision. Diff statistics remain visible.
Older plans may retain `max_changed_lines` for historical integrity, but it is ignored
without rewriting the plan or its approval digest. Transport and review-context bounds
still apply. The schema/instruction update changes the bundle; existing bundle-reconciliation
rules remain, so deploy after active pilots finish or reconcile them explicitly.

## Deliberately deferred

| Capability | When to reconsider |
| ---- | ---- |
| Parent/sub-issue decomposition | A task contains meaningful, independently reviewable slices; not merely a large line count |
| Preflight and protection-rule discovery | Targets need more than observing the PR's reported CI |
| CI logs, reruns, automatic repair | Maintainers repeatedly spend time relaying failures |
| Merge automation | Explicitly requested after the contribution loop is proven |
| Mandatory warming, shared build caches | Measurements show cold setup is a material bottleneck |
| Separate trackers/project boards | A target needs more than upstream/fork issues |
| Fork creation/sync and linked branches | Operators request those administration features |
| More runner platforms/private-source delivery | A target needs capabilities beyond the first public Linux pilot |

Optional existing default-branch cache producers work now; no warming service or workflow
is required. The initial live proof uses public repositories and Linux GitHub-hosted
runners. Different languages do not require a Concurro adapter.

### Future: parent and sub-issue planning

A parent issue would hold the overall objective, architecture, dependencies and integration
criteria. Each sub-issue would carry its slice's detailed plan, implementation and review.
The planner or reviewers could propose this when the work has separable outcomes; a large
but cohesive change could remain one task. Size is a signal to consider decomposition,
not an automatic rejection threshold. Creation, dependency scheduling and parent completion
are future work, not required for removing the line budget.
