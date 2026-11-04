# Runner roles: agents on GitHub-hosted runners, CI as the gate

Status: superseded proposal, retained for design history (2026-11-04). Not implemented.
The [simplified MVP](mvp.md), [execution contracts](mvp-execution.md) and
[implementation plan](mvp-implementation.md) replace this proposal. In particular,
preflight, CI repair orchestration and separate tracker repositories below are not
requirements for the new MVP. Its narrower readiness watcher is specified in the new docs.

## Decision

- **Where agents run:** directly on GitHub-hosted runners, with native agent tools and no
  Concurro-built images. Runners are the only executor. The Concurro host coordinates and
  proxies inference; nothing else.
- **Contributing like a person:** Concurro works the way a contributor does. It pushes to
  a fork, waits for the fork's preflight CI to pass the upstream project's pre-PR
  criteria, then opens a draft PR upstream and iterates on upstream's CI. It marks the PR
  ready for review once upstream's CI and the code reviewers are satisfied.
- **No Concurro checks:** Concurro defines no build, test or lint checks. CI is the only
  judge of a candidate: the fork's for preflight, upstream's for the PR. Agents may build
  and test however they like, to move faster, but what they report is never evidence.

| Owner | Responsibility |
| ---- | ---- |
| **Upstream** | The code, its default branch as the approved base, its PR criteria and CI, and the merge decision (a human) |
| **Fork** (operator-owned) | `.concurro/` config and instructions, the environment setup for agents, the preflight CI workflow, task branches, run refs and every Concurro job |
| **Concurro** | Agent clients, role orchestration, authentication, watching CI, and moving the PR along |

## Why

- **The secret that matters stays on the host:** provider credentials sit behind the
  inference gateway. A runner holds a bounded session key and its job tokens.
- **The target's own criteria decide:** Concurro-defined checks and images duplicated
  what upstream's CI already defines, and drifted from it. The fork's preflight runs the
  project's own pre-PR commands, and upstream's CI is authoritative.
- **No secrets on candidate code:** upstream CI on a fork PR gets no repository secrets
  and a read-only `GITHUB_TOKEN`, as for any outside contributor. The fork holds no
  secrets.
- **Separate jobs protect results:** code running on a machine can rewrite any result
  produced there. Roles that judge each other never share a machine.

## Repositories and identity

A target is a pair, plus where its tasks come from:

```json
{"upstream": "stacks-network/stacks-core", "fork": "cylewitruk/stacks-core-autobot",
 "tasks": ["stacks-network/stacks-core", "stacks-network/some-tracker"]}
```

- **Upstream:** its default-branch head is the approved base for code. Concurro reads it,
  opens the PR there from `fork:branch`, comments on the PR, and marks it ready for review.
- **Fork:** its default branch holds `.concurro/` (config, charter, per-role instructions,
  setup action) and the preflight and caller workflows. Concurro pushes task branches and
  run refs here, dispatches role jobs, and reads CI.
- **Task repositories:** labelled issues in any listed repository become tasks, whose code
  lives in upstream. Status comments go on the issue.
- **The App** is installed on all three, with only what each needs:
  - upstream: contents read, pull requests write, issues write, checks and actions read;
  - fork: contents write, actions write, checks read;
  - task repositories: issues write.

  The service requests exactly these permissions per repository when it mints
  installation tokens, and verifies them.

The fork's default branch must keep its `.concurro/` files and workflows on top of
upstream. Syncing the fork with upstream never overwrites them; the approved code base is
upstream's head, not the fork's.

## Task flow

1. **Intake and plan:** role jobs for intake, the planner and the design reviewers. Each
   reads the approved base (upstream's head) and returns its result.
2. **Implement:** the implementer's job edits a snapshot of the base. It builds and tests
   as its instructions ask. Concurro applies its file rules (protected and generated
   paths) to the edits, then pushes them as a commit to the task branch
   `concurro/issue-<n>-<plan>` in the fork.
3. **Preflight:** the push triggers the fork's preflight workflow, which runs upstream's
   pre-PR criteria. For stacks-core that is `cargo fmt-stacks`, `cargo clippy-stacks`,
   `cargo clippy-stackslib` and the tests, as many as the fork's runners can take. The
   service watches the check runs named in `preflight_checks`, through check-run webhooks
   with polling as a fallback.
   - **Red:** the service fetches a bounded tail of each failing job's log. It starts a new
     implementer turn with those logs, the diff and the agent's retained session.
   - **The implementer** either fixes and pushes, or asks for a rerun when it judges a
     failure flaky.
4. **Draft PR:** once preflight is green, the App opens a draft PR from `fork:branch` to
   upstream, which runs upstream's CI. If upstream requires a maintainer's approval before
   running workflows for outside contributors, that wait is visible on the issue.
5. **Upstream CI loop:** the same red, fix or rerun cycle against upstream's required
   checks. Those come from branch protection or the ruleset when readable, otherwise from
   `required_checks` in the config.
6. **Code review:** when upstream's required checks are green, each code reviewer's job
   reviews the PR's diff. Findings start another implementer round, through preflight
   again.
7. **Ready for review:** when the required checks are green and no reviewer finding is
   open, the App marks the PR ready for review. A human merges.

**Limits that keep "flaky" from becoming a loophole:**
- A rerun never counts as a pass; only a check that actually turns green does.
- Fix rounds and reruns per round are capped (`max_ci_rounds`, `max_reruns`). Past the
  caps, the task blocks for a human.
- A PR is never marked ready while a required check is red, pending or skipped.

## Roles

A role is `<stage>:<provider>`: `intake`, `plan`, `plan_qc` (design review), `develop`
(implementer) and `review` (code review). Each role of a task gets its own session, and
each session is served by exactly one job, in the fork.

- **Turns** are queued with their task and role, and served only to a live session of the
  same task and role.
- **Implementer rounds:** the implementer's job ends once its turn returns. A later round
  (after a red CI run or a review finding) starts a new job. The agent's retained session
  (its conversation) moves through the exchange; build state is rebuilt.
- **Reviewers** may run the candidate's code. Separate jobs keep one role's code from
  altering another role's results. They don't make a reviewer's own report immune to code
  that reviewer chooses to run, and nothing a reviewer runs is a gate.
- **Agent homes:** only the CLIs' own directories (`CODEX_HOME`, `CLAUDE_CONFIG_DIR`,
  `COPILOT_HOME`) are per role. `HOME` and the toolchain homes stay the job's.
- **Ending:** a session ends after its idle timeout.

## Authentication: one run ref per role session

Role sessions of a fork would otherwise share expected claims, so any of the fork's jobs
could try to bind another role's pending session first. Instead:

- **Expected claims:** when the exchange creates a role session, it sets the session's
  expected `ref` claim to `refs/heads/concurro/runs/<session-id>`. The other expected
  claims are kept:
  - the fork's repository and owner IDs;
  - the App's bot as `actor_id`;
  - `event_name: workflow_dispatch`;
  - the pinned reusable workflow's ref and SHA.

  The exchange also keeps its existing checks: GitHub-hosted runners only and
  `run_attempt` 1.
- **What the exchange records:** each session's task (target and issue), role and run ref.
  The role always comes from the stored session, never from a caller or a workflow input.
- **Binding:** a session binds atomically to its first authorized run. Any other run is
  refused before a key or runner token is issued. A job presenting its OIDC token for
  another session fails the `ref` check.
- **Dispatch, using the existing staffing and dispatch path:** a submitted turn with no
  live session for its task and role gets a new session. Before dispatching, the service:
  1. creates the run ref in the fork at the fork's default-branch head, so the caller
     workflow that runs is the fork's own;
  2. calls `POST actions/workflows/<caller>/dispatches` there, with `ref` set to the run
     ref and `inputs: {session}`.

  A failed dispatch ends the session and withdraws the turn, as today.
- **Heartbeats from the claim on:** the exchange treats a bound session as gone after 180
  seconds without a heartbeat. `runner.py claim` starts the existing heartbeat loop as a
  detached process (its PID is kept under `$RUNNER_TEMP`), so heartbeats run through
  setup.
  - GitHub kills a job's leftover processes only when the job ends, so the loop survives
    between steps; the implementation verifies this on a hosted runner.
  - `serve` takes over without a gap: it sends its own first heartbeat, then stops the
    detached loop and confirms it exited.
  - A final `if: always()` step stops any loop still running and ends the session.
- **Replacement:** a lost or ended session is gone. The role's next turn staffs a new
  session with its own run ref.
- **Cleanup:** the reconciler deletes `concurro/runs/<id>` refs whose session has ended,
  and the rest when the task reaches a terminal state.
- **Write allowlist:**
  - the fork: run-ref create and delete, task-branch pushes, workflow dispatch and job
    reruns;
  - upstream: PR create (head in the fork), comments and the draft-to-ready transition;
  - task repositories: issue comments and labels.

  Nothing else is admitted.

## A role job

`agent.yml` (reusable, called by the fork's caller workflow) runs these steps:

1. **Fetch Concurro** at the reusable workflow's own commit into `$RUNNER_TEMP/concurro`,
   outside the workspace.
2. **Claim the session and start heartbeats:** `runner.py claim` exchanges the job's OIDC
   token. It writes the grant to an owner-only file under `$RUNNER_TEMP`, and puts the role
   and base SHA into step outputs, both from the exchange.
3. **Check out the fork's default branch** for its `.concurro/` files, and upstream's base
   SHA as the code. Both use `persist-credentials: false`. Never the candidate.
4. **Run the fork's setup:** `uses: ./.concurro/setup`, when present.
   - Tools, toolchain homes and cache restores for the agents' own work; nothing
     checkout-local.
   - It receives the role, so heavy setup can be limited to `develop:` and `review:` roles.
5. **Install the agent clients:** pinned Codex, Claude Code and Copilot CLIs via npm.
6. **Serve turns:** `runner.py serve` runs agent turns. Agents work in per-turn snapshots
   from the job's mirror: the base, or the candidate at its pushed commit.
7. **End the session** in an `if: always()` step.

`runs-on` is a caller input (default `ubuntu-24.04`); the exchange refuses
non-GitHub-hosted runners.

## Target instructions

`.concurro/instructions/<stage>.md` on the fork's default branch: `planner.md`,
`design-reviewer.md`, `implementer.md` and `reviewer.md`, with an optional `shared.md`.

- **What they are for:** project-specific guidance written by the operator, such as
  "before finishing, `cargo fmt-stacks`, `cargo clippy-stacks` and
  `cargo clippy-stackslib` must pass", conventions, where tests go, changelog rules.
- **Where they go:** each agent's prompt gets a "target instructions" block after
  Concurro's charter and role skill, and before any task content.
- **Precedence:** they outrank the issue, comments and code. Concurro's charter and safety
  rules outrank them, and the prompt says so.
- **Security:** they are read from the fork's default branch at the start of each tick,
  never from a task branch or the candidate, and are size-limited.
- **What replaces them:** `policy_documents` (upstream files such as `CONTRIBUTING.md`)
  stay as reference material. The execution profiles go.

## Caches

- **Role jobs only read shared caches.** Both the caller workflow and `agent.yml` set
  `cache-mode: read`, which the cache service enforces and a called workflow cannot
  exceed. Restores work; saves are refused.
- **Preflight runs on task branches** with candidate code, so it must not save caches
  that the fork's default branch reads. It sets `cache-mode: read` too.
- **Warming is optional, and always a separate job.** It runs trusted default-branch code
  and setup in the fork, and may save caches. It never executes candidate code.
- **The setup owns keys and compatibility.** A missing, stale or evicted cache falls back
  to normal installation.

## Changes

- **Exchange:**
  - Sessions record a task, a role and a run ref; turns record their task and role.
  - Staffing matches task and role, and sets the session's `ref` claim to its run ref.
  - `next` serves only the session's task and role.
  - The grant returns the stored role and the base SHA.
  - Pins are removed, since no workbench needs them.
- **Runner** (`runner.py`):
  - New `claim` and `serve` subcommands, with the heartbeat handoff.
  - Agent turns run `agent_entry.py` as a subprocess. The agent's environment is the
    job's minus `ACTIONS_*`, `GITHUB_TOKEN` and the runner token, which is hygiene, not a
    boundary.
  - Snapshots can be of the base or of a pushed candidate commit.
- **Controller:**
  - The develop loop runs: implement, push, preflight, draft PR, upstream CI, code review,
    ready.
  - CI results come from check runs. Failed logs feed the implementer, with reruns and
    fix rounds capped.
  - `local_review` is removed: reviews happen on the draft PR.
  - The target instructions block goes into every role's prompt.
- **Service:**
  - Targets become (upstream, fork, task repositories).
  - Installation tokens are minted per repository with the exact permissions above.
  - The GitHub facade gains fork run refs and pushes, workflow dispatch and reruns,
    check-run and log reads, PR creation from a fork head, and draft-to-ready.
  - Check-run webhooks feed ticks.
  - The reconciler sweeps run refs.
- **Startup checks:** `host_checks` loses only image validation. It keeps the
  clean-checkout check, engine commit resolution and matching any configured engine pin.
- **Workflows:**
  - `agent.yml` follows "A role job".
  - The fork gains a caller workflow (`workflow_dispatch`, input `session`, optional
    `runs-on`, `cache-mode: read`, `permissions: id-token: write` only) and its own
    preflight workflow.
- **Removed:**
  - the host executor and `host_agents`;
  - `docker/*.Dockerfile` and the settings' image fields;
  - the workbench (`local.py`, `workspace_entry.py`, `checks.py`, `bench.py`,
    `RemoteBench`);
  - execution profiles;
  - `local_review`;
  - the Actions controller example.
- **Design evaluation:** `design_eval.py` is kept and runs through runner sessions, with
  its fixtures and hidden expected answers unchanged.

## Guidance for operators

- Keep the fork free of secrets. Its caller and preflight workflows get
  `id-token: write` (caller only), `contents: read` and `cache-mode: read`.
- Never add a cloud or service OIDC trust for these workflows.
- Write `.concurro/instructions/*.md` from the project's contribution rules. Make
  `preflight_checks` the subset of upstream's criteria the fork can afford to run.
- Keep merging with upstream's maintainers.

## Proposed tests

- **Task flow:** a scripted run through every stage against fakes, including red
  preflight, fix, green, draft PR, red upstream check, rerun, green, review finding, fix,
  ready. Also a rerun that never counts as a pass, the caps blocking, and a skipped
  required check keeping the PR in draft.
- **Repositories:** issues from a task repository other than upstream, and config and
  instructions read from the fork's default branch only. A task branch's `.concurro/`
  changes are ignored.
- **PR from a fork:** the upstream PR's head is `fork:branch`, it opens as a draft, and
  goes to ready only through the allowed transition.
- **Permissions:** per-repository installation tokens with exactly the listed
  permissions, and startup refusing a missing one.
- **Cross-session OIDC:**
  - Unit tests: a token whose `ref` names another session's run ref is refused before any
    key, runner token or binding, and so is a second run for a bound session.
  - Live: a job dispatched for session A presenting its token for session B gets 403, and
    B stays unbound.
- **Heartbeat handoff:**
  - Unit test: `serve`'s first heartbeat lands before the detached loop stops, and no gap
    exceeds the window.
  - Live: the loop survives between steps, `serve` takes over, and a failed setup's
    session is ended by the `always()` step.
- **Caches:** role and preflight jobs restore warmed caches, and their saves are refused.
  A cold cache still completes.
- **Instructions:** each role's prompt carries its instructions after the charter and
  before task content. Oversized files are refused.
- **Engine version:** startup still refuses a dirty checkout, an unresolvable engine
  commit or a mismatched configured pin.

## Decisions

Settled:

| Decision | Outcome |
| ---- | ---- |
| Where agents run | GitHub-hosted runners in the fork, one job per role session |
| Gate | CI only: the fork's preflight, then upstream's required checks; a human merges |
| Task source | Labelled issues in configured repositories (upstream or a tracker) |
| Config location | The fork's default branch |
| Upstream PR | Opened by the App installed upstream, from `fork:branch`, as a draft first |
| Setup interface | A composite action at `.concurro/setup/action.yml`, for agents' own work |
| Run ref naming | `concurro/runs/<session-id>` in the fork |
| Design evaluation | Kept, through runner sessions |

Open, for review:

1. **Preflight definition:** a fork workflow named in config with its check-run names
   (proposed), or a list of commands Concurro turns into a workflow.
2. **Required upstream checks:** read from branch protection or rulesets (proposed, with
   `required_checks` as the fallback), or the config only.
3. **Review timing:** code review after upstream's checks are green (proposed), or in
   parallel with CI to save time.
4. **Upstream workflow approval:** wait visibly (proposed), or ask maintainers to trust
   the App's bot.
5. **Rerun scope:** failed jobs only (proposed), or the whole run.
