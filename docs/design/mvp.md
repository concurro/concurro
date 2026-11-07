# Concurro MVP: a reviewed draft PR

Status: implemented. Current architecture; audited against the implementation on 2026-11-07.

Concurro takes an issue, plans a contribution, implements it, obtains independent
agent reviews, and hands a PR to maintainers. It opens the PR as a draft and marks it
ready for review when CI passes and all configured agent reviewers approve the current
head. It observes CI; it does not run it or decide whether GitHub will allow a merge.

Read this document first, then the [execution contracts](mvp-execution.md) and
[service contracts](service.md). The [documentation index](../README.md) separates
implemented behavior from pending proposals.

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
a new task, not a silent mid-turn switch; `/concurro invalidate` does not replace its pinned configuration. Preserve engine/bundle
identity checks when deploying new engine code. Bundle drift blocks model work, not
closure or CI/readiness observation. A writer uses `/concurro invalidate` to adopt
the current bundle and restart assessment; `/concurro retry` retains the existing
bundle binding. Invalidation preserves the PR and spent budgets and records the change.

Upstream advancing alone does not restart a task. The approved base stays pinned.
Automatic rebasing is not implemented; a contribution needing a different base requires
explicit operator handling or a new task.

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

1. **Enroll and plan.** Writer enrollment starts intake against the target charter, then
   planning and independent design review. Advisory intake concerns proceed to planning;
   unanswered questions and charter conflicts do not. Rejected output is archived and gets
   bounded corrections within the model-call budget. Unanimous independent cooperative
   approval can skip duplicate fresh review, including after validation-only corrections.
   Negotiated revisions and session restarts still require fresh review; see the
   [eligibility contract](design-review-corrections.md). Manual mode also requires writer approval. [Recovery commands](../concurro.md#commands-and-recovery)
   distinguish retry from explicit invalidation.
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
still apply. Bundle changes still require the normal release and invalidation procedure.

Source file size is not a scope or edit policy. Design-review contexts carry planned-file
paths, Git blob identities and sizes, not full contents. PR contexts retain every changed
file's identity and statistics; diff previews shrink to fit, with explicit truncation markers.
Agents inspect relevant ranges in the pinned checkout. Extraction and publication preserve
complete UTF-8 contents, with large replacements uploaded as Git blobs before creating the
tree. Path, text, scope, file-count and exact-head checks still apply. Model responses,
aggregate exchange payloads, tool output and HTTP responses retain their resource bounds;
these are transport limits, not a requirement to read an entire source file into a prompt.

## Deliberately deferred

| Capability | When to reconsider |
| ---- | ---- |
| Parent/sub-issue decomposition | A task contains meaningful, independently reviewable slices; not merely a large line count |
| Preflight and protection-rule discovery | Targets need more than observing the PR's reported CI |
| CI logs, reruns, automatic repair | Maintainers repeatedly spend time relaying failures |
| Merge automation | Explicitly requested after the contribution loop is proven |
| Engine-managed cache warming | Target-owned default-branch warmers are insufficient |
| Separate trackers/project boards | A target needs more than upstream/fork issues |
| Fork creation/sync and linked branches | Operators request those administration features |
| More runner platforms/private-source delivery | A target needs capabilities beyond the first public Linux pilot |

Optional target-owned default-branch cache producers work now, including build-artifact
caches when compatible with the target’s CI. They are not a Concurro service or mandatory
workflow. Current pilots use public repositories and Linux GitHub-hosted runners. Different languages do not require a Concurro adapter.

### Future: parent and sub-issue planning

A parent issue would hold the overall objective, architecture, dependencies and integration
criteria. Each sub-issue would carry its slice's detailed plan, implementation and review.
The planner or reviewers could propose this when the work has separable outcomes; a large
but cohesive change could remain one task. Size is a signal to consider decomposition,
not an automatic rejection threshold. Creation, dependency scheduling and parent completion
are future work, not required for removing the line budget.
