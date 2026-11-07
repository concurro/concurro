# MVP execution and publication contracts

Status: implemented; audited 2026-11-07. Defines the boundary in
[MVP architecture](mvp.md), including operation journals, outboxes and gateway settlement.

## Identities and ownership

| Identity | Binding |
| ---- | ---- |
| Task | Issue repository and issue number; one recorded upstream/fork pair |
| Approval | Task, base, plan, configuration/bundle identity and existing approval phase |
| Role | Stage and CLI harness (`develop:codex`), plus resolved profile/model/effort in the staffing binding |
| Runner session | Task, role, ownership generation, configuration commit and unique session ID |
| Turn | Existing logical operation, attempt, request digest and deadline |
| Candidate | Approved base, expected parent and controller-published head SHA |

Staff sessions by task, role and compatible ownership/configuration. Never reuse one
across issues or incompatible model profiles, efforts, phases or ownership generations.
One session is one GitHub job; turns run sequentially.
First-turn unanimous design approvals need no duplicate round. After resumed reviews,
fresh design approval uses a new session/home, without cooperative transcripts, even
when stage and provider match. Retained conversations remain scoped by the existing
session journal; a runner-session ID is not a provider conversation ID.

Ownership loss invalidates the old task’s authority; stale results never update its
checkpoint. Session cleanup and bounded uncertain-call recovery use the same ownership
and operation records.

## Dispatch and OIDC

The exchange records a session before dispatch. The service creates
`refs/heads/concurro/runs/<session-id>` in the fork at the recorded `config_sha`, then
dispatches the caller workflow at that ref with only the session ID. An existing ref
must match the recorded SHA; never force-update it to recover a conflict.

Validate GitHub signature, issuer, audience, timestamps and one-time token identity,
then require the recorded fork/owner IDs, App actor, `workflow_dispatch`, exact session
ref, GitHub-hosted runner, run attempt 1 and expected reusable-workflow ref/commit.
Atomically bind the first authorized run ID; a second run or another session's token
gets no credential. The grant returns the stored role, base, configuration and task
identity. Workflow inputs are not proof of authorization.

The caller uses `concurro/concurro/.github/workflows/agent.yml@deployed`. This release
ref moves with service deployments, not ordinary pushes to `main`. Its resolved
`job_workflow_sha` must still equal the served engine commit. Pause dispatch while
updating the service and ref; resume only when they match. A mismatch fails visibly
rather than silently running different engine code.

Keep dispatch writes inside the mutation gateway. An unknown dispatch outcome follows
its existing operator-resolution path; do not blindly create a replacement run. Ended
sessions have their keys revoked, and the reconciler sweeps only their recorded run
refs. Keep contribution branches and PRs for humans.

GitHub documents the signed [OIDC claims](https://docs.github.com/en/actions/reference/security/oidc)
and dispatch behavior: the selected ref runs, and the caller workflow must also exist
on the fork's default branch ([workflow_dispatch](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch)).

## Job lifecycle and setup

Use the existing reusable workflow, with `ubuntu-24.04` as the pilot default. The target
may select another supported GitHub-hosted Linux label. No execution images are built.

1. Fetch Concurro at the workflow's verified engine commit under `$RUNNER_TEMP/concurro`.
2. Claim the session. Store the grant in an owner-only temporary file and start the
   heartbeat immediately, including through long toolchain installation.
3. Materialize the fork's trusted configuration at `config_sha` in a separate directory
   from the upstream source. Fetch the recorded code commits from the configured upstream
   and fork. Neither checkout may remove Concurro's runtime directory.
4. Run the optional local composite setup action from that configuration checkout. Make
   the upstream base path available as a documented input; its source is separate from
   the action's files. Preserve `$GITHUB_ENV` and `$GITHUB_PATH` changes.
5. Install the pinned provider CLIs and serve native agent turns. Tools have the job's
   `HOME` and toolchain homes; only provider transcript/config directories are scoped to
   their sessions. Workspaces hold the exact base or published candidate for the turn.
6. On exit, end the session and stop its heartbeat. An `if: always()` cleanup step handles
   setup failures; exchange expiry handles a lost runner or cleanup that never executes.

The setup heartbeat survives step boundaries. `serve` renews successfully before stopping
the detached helper. A bound session silent for 180 seconds is retired by reconciliation;
a job normally exits after ten idle minutes. Compatible turns can reuse the role job
while it remains live. No runner job is required to wait for CI.

Transcript archiving/restoration includes Codex cache pruning. Replacement jobs restore conversation state, not build directories.
Provider failure uses the existing fresh-session/full-context fallback and consumes
the existing attempt allowance.

## Caches and credentials

Both caller and reusable role workflow explicitly set `cache-mode: read`. The job may
restore shared caches and write its local copies, but cannot upload them. Cache misses
are normal; agents may install dependencies and run tests in their own workspaces.
The target owns keys, paths and compatibility. Optional warmers are separate trusted
default-branch jobs and never run candidate code.

GitHub enforces cache mode through scoped tokens, including the caller's limit on a
reusable workflow ([cache controls](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#controlling-cache-access-with-cache-mode)).

No provider secret, App key, installation write token or exchange operator credential
goes to a runner. Do not pass job tokens into the agent subprocess environment, but do
not describe that as isolation: native tools and sudo can access their own job. The
runner token is valid only for that session's assigned turns. Job permissions are
`id-token: write` and only any necessary read access; no secret inheritance or privileged
environment. Do not trust these workflows for unrelated cloud identities.

Retain gateway model limits, accounting, session deadlines and revocation reconciliation.
Gateway spending budgets are not guaranteed hard caps under concurrent inference, and
revocation may wait while the exchange is unavailable. Those existing limitations do
not justify putting provider credentials back on runners.

## Candidate return and publication

A successful develop turn returns an explanation, optional design-reopen request,
agent-reported validation and changes extracted from its native workspace. It returns
no mandatory-check receipts. The runner’s extractor is a convenience, not trusted
evidence against a hostile job.

Support UTF-8 additions, replacements and deletions within aggregate transport limits.
There is no per-source-file size cap. Normalize extracted removals to `{path, content: null}`;
a deletion must name a supported regular file in the recorded parent. Represent a rename
as a deletion and an addition, validating both paths before publishing either. Preserve
existing modes; explicitly reject symlinks, binary files and mode-only changes instead
of dropping them. A general patch/binary protocol remains separate work.

Deleted paths remain in review context and cumulative scope accounting. Renames count
both paths. Line counts are informational, not an authorization limit. When using `base_tree`, publish deletions as explicit `sha: null` tree entries;
omitting an entry preserves the original file ([Git Trees API](https://docs.github.com/en/rest/git/trees#create-a-tree)).

Before any push, the controller validates the returned bytes against its recorded parent:

- safe unique paths, protected-path policy and the approved scope;
- aggregate transport bounds, and cumulative path scope and file-count limits already in the plan;
- the complete proposed change across repair rounds, not just this turn's additions;
- sufficient bounded context to review every changed file, with no silent omissions.

These are pure checks in `policy.py` and `candidates.py`; the controller does not execute
repository code. There are no build-specific assertions, profile triggers or mandatory
primary-file completion checks. Generated files still count toward the file limit;
[scope rules](scope-envelope.md) apply to all candidate paths.

The controller constructs Git objects from validated bytes and updates only the recorded
fork task branch, fast-forward from its recorded parent. Do not push the agent's Git
metadata or arbitrary commits. External branch edits block for reconciliation rather
than being overwritten. A task branch starts at upstream's approved base, not at the
fork configuration commit.

Create/recover the branch and one issue link when implementation begins. After the first
published candidate, create/recover one upstream draft PR from that fork branch. Use a
fully qualified issue link in its body; avoid an ambiguous `Closes #N` when the issue is
in the fork. Admit the task-bound draft-to-ready transition described below. No linked-
branch mutation, upstream branch creation, fork creation or merge endpoint belongs in
the v1 write allowlist.

## GitHub routing and recovery

Store a task's issue repository, fork branch and upstream PR explicitly. The mutation
gateway receives the task ownership key separately from the destination repository:

| Destination | Allowed task-bound actions |
| ---- | ---- |
| Issue repository | Dashboard/action comments, labels, audit/plan/report artifacts |
| Fork | Recorded task-branch objects/ref updates, session-ref create/delete, session dispatch |
| Upstream | Draft PR creation/readiness, native COMMENT reviews, finding comments/replies and owned-thread resolution |

Allow only those object identities, not arbitrary writes to all configured repositories.
PR recovery checks its recorded number, App author, head repository and branch.
Creation uses `maintainer_can_modify: false`; outside pushes require reconciliation. Artifact/comment recovery still
checks markers, author and expected content. Preserve read-only access needed for these
checks, and preserve at-most-once/manual recovery for ambiguous thread resolution.

Reuse per-repository installation tokens and verify granted permissions. Fork dispatch
needs Actions write. Fork publication also requests Contents and Workflows write: creating
a branch at an upstream commit can introduce workflow blobs absent from the fork, even
when the candidate never edits a workflow. All configured fork installations must accept
that grant before deploying. The controller never passes that token to a job. Upstream needs
PR writes and, for enrolled upstream issues, issue/artifact writes. Preserve the tested
permission combination for owned-thread resolution until its narrower scope is proven;
token permission and gateway endpoint authorization are separate boundaries. There is
no need to request upstream Actions write for CI reruns or administration access for
required-check inspection. Read permissions include Actions read for approval waits, checks and commit
statuses; readiness uses the existing PR-write authority. Union permissions when one
repository serves multiple roles.

Keep write intents, unknown-write stops, operator resolution and result receipts. A retry
must not produce a second branch-link comment, PR or native review. Do not replace the
existing recovery model with either blind retries or a new generic workflow engine.

## Reviews, handoff and readiness

Keep current reviewer-profile-owned finding IDs, dispositions, input revisions, native COMMENT
reviews, outboxes and report archives. Code reviews read the recorded upstream PR head;
reviewers can inspect history and run their own experiments. A new head invalidates
previous approvals. A failed or conflicting publication remains visible and bounded.

The review-complete predicate requires current plan authorization,
expected published head, all configured reviewers approving that head/input revision,
no outstanding findings requiring action, and no pending publication. Preserve existing
manual exits for unresolved/reopened engine threads; never resolve a human thread.
These are Concurro's agent approvals, not GitHub's required human approvals.

Mark an open draft PR ready when that predicate holds and its current CI has passed:

- Read the PR's current checks and commit statuses, including the current test-merge
  commit when GitHub uses it. Identify latest results, fetch all pages and bind the
  observation to the current head; do not reuse a previous head's green result.
- Require at least one actual successful result and no pending, failed, cancelled,
  timed-out or action-required result. Terminal skipped/neutral jobs do not block an
  otherwise passing set; an empty or entirely skipped/neutral set does not prove a pass.
  Missing/unreadable results leave the PR draft. Do not discover branch-protection rules
  or accept a configurable list of Concurro checks.
- `check_run`/`check_suite` and commit-status webhooks act as wake-ups for
  mapped PRs. The controller fetches authoritative state after an event, using the reconciler’s
  periodic poll as fallback; no agent job, paid model call or separate polling service
  stays alive waiting for CI.
- Re-read the head and review eligibility before the write. Admit only the exact
  `markPullRequestReadyForReview` GraphQL mutation for the task's bound PR. Preserve
  gateway intent/settlement handling and recover by reading `isDraft`; an already-ready
  PR needs no duplicate mutation. A head change requires fresh approvals and CI.

Record readiness and notify once per head after it is confirmed. Review completion with
red or pending CI is visible on the dashboard and leaves the PR draft. CI events never
start repair agents or rerun workflows. Human commands may resume bounded review/repair
through `/concurro retry` with supplied feedback, without automated CI-log ingestion.

Respect manual draft/ready changes; do not fight a human converting a PR back to draft.
Once ready, a later CI regression updates the display but does not automatically convert
the PR back or trigger repair. Closure/merge stops the watcher. Readiness is a decision
from current observations, not a guarantee about future checks or GitHub mergeability.
Some projects run CI only after leaving draft; show that wait for a human rather than
adding a bypass. No required-review, deployment or merge-policy evaluator is introduced.

GitHub exposes [PR check results](https://cli.github.com/manual/gh_pr_checks) and the
[draft-to-ready mutation](https://docs.github.com/en/graphql/reference/pulls#markpullrequestreadyforreview).
Keep validation labeled as agent-reported in the handoff, and preserve report archives
before compaction, including across restarts.

CI log excerpts supplied by humans remain untrusted task data, not instructions that
override the charter, target policy or allowed publication destinations.
