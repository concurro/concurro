# Operating Concurro

The [MVP architecture](design/mvp.md) and [execution contract](design/mvp-execution.md)
govern this version. Earlier workbench, profile and self-hosted-runner designs are historical.
See [deployment and migration](../gateway/README.md) before updating an existing service.

## Target setup

Configure an existing upstream/fork pair in service settings, with `issues` equal to
`upstream`, `fork` or `both`. The App needs access to both repositories; writers in the
selected issue repository enroll work with the `concurro` label. Concurro never creates
forks or upstream branches. The current transport supports public repositories only;
it fetches source anonymously and does not give GitHub write credentials to runners.

In the fork's default branch, install:

- `.concurro/config.json` and `.concurro/charter.json` from the example, adapted to the project.
- `.github/workflows/concurro-executor.yml`, calling `agent.yml@deployed` with only its
  centrally issued session ID and explicitly forwarding the repository secret
  `CONCURRO_GATEWAY_URL`. Store the deployment URL in that secret, not in source or
  workflow inputs. Both jobs must specify `cache-mode: read`.
- Optionally `.concurro/setup/action.yml`, receiving an absolute `base` checkout path.
  Install shared tools and restore download caches here. Workspace installs belong in
  agent workspaces. A missing cache must not prevent normal installation.
- Optional `shared.md`, `planner.md`, `design-reviewer.md`, `implementer.md` and
  `reviewer.md` under `.concurro/instructions/`.

The [stacks-core example](../examples/stacks-core/) installs the base's Rust toolchain
and suggests validation commands. Other projects can install Node, .NET or combinations.
There is no execution profile or mandatory command catalog. Configure protected and
generated globs under `paths`; existing extension and path restrictions still apply.

A task records the fork configuration commit separately from the approved upstream
source commit. Setup, instructions and caller workflow changes apply to new tasks;
changing an active task requires reconciliation. An optional target `engine` pin must
match the served commit. Omit it when the same operator controls service and target.

## Contribution loop

The issue dashboard shows intake, planning, cooperative/fresh design approval,
implementation, independent PR reviews and readiness. Reports are archived before
compaction. Findings have stable IDs and native threads. Only their raising provider can
retire them; ambiguous or reopened thread resolutions remain manual.

Implementation may add, change, delete or rename bounded UTF-8 regular files inside the
approved envelope. Renames count as deletion plus addition. Binary files, symlinks and
mode-only edits are refused. The controller checks complete cumulative changes and
builds Git objects itself. Agent validation is reported, never represented as trusted CI.

The PR is created upstream as a draft. All configured agent reviewers must approve the
current head and input revision, with no open findings or pending publication. CI must
have at least one successful result and no failed or pending result. Skipped/neutral
results alone are insufficient. Concurro also reads statuses on a verified current test-
merge commit. It does not inspect branch protection or maintain an ignored-check list.

Check runs retain their suite identity; pending suites and workflows awaiting approval
also keep the PR draft. An explicitly empty, queued non-Actions suite with no conclusion
is ignored only when no fetched check run belongs to it. The App needs upstream Actions
read access to observe workflow approval waits.
CI webhooks and the periodic poll wake only readiness, never a model-stage retry. Red CI,
missing CI or workflow approval leaves the PR draft. A writer can post a brief failure explanation (up to 4,000 bytes), then
`/concurro retry` to request a repair within the existing scope and round budget.
The latest new writer comment supplies the feedback; retry without new feedback
only rechecks readiness. There is no automatic log ingestion, rerun or CI repair.
After readiness, failures update the dashboard without redrafting. A human's redraft is
respected. Closed or merged PRs stop the watcher. Only humans merge.

## Commands and recovery

Issue commands require the existing writer permissions. `/concurro start` starts or
resumes manual planning; `/concurro approve <plan-id>` authorizes the recorded plan;
`/concurro retry` retries blocked work with current requirements/feedback;
`/concurro stop` suspends it. Input-needed comments describe the applicable continuation.

The service's owner-only control socket provides `status`, `reconcile-once`, and
`resolve <intent> applied|not-applied <evidence> --operator <name>`. Check `--help` for
exact arguments. Resolve only after observing whether the uncertain GitHub write applied.
An unknown write prevents takeover and further writes for that task. Do not clear its
registry entry or blindly replay a dispatch.

`adopt`/`release` remain migration tools for old Actions-owned checkpoints, not a supported
execution fallback. Bundle/config changes invalidate old approvals. Preserve the old
issue and archives; use a fresh pilot issue rather than bypassing that check.

An explicitly requested `evaluate <issue-repository> <issue> <claude|codex>` runs four
fresh design fixtures on ordinary runner sessions. It uses model budget and temporarily
owns that task, preserving its queue. Reports go under the service state's `evaluations/`;
no fixture model work runs on the host. Use an idle configured task.

## Trust boundaries

Provider secrets, the App key and write tokens stay on the host. Runners receive scoped
gateway/session credentials and their own GitHub job identity. Native agents have a shell,
network and job-level authority; environment filtering is not a sandbox. Separate jobs
isolate roles, and server-enforced read-only caches prevent cache uploads. Do not attach
unrelated cloud trust, inherited secrets or privileged environments to the workflow.

The controller publishes only policy-validated changes and fences writes by task ownership.
A reviewer may execute candidate code in its own job, so its verdict is not trusted build
evidence. Independent reviews and project CI remain complementary signals. Gateway spend
budgets can overshoot under concurrent calls; key revocation waits during an exchange outage.

## Offline verification

Run the commands in the [README](../README.md#development). No live provider calls,
GitHub mutations or Docker execution are part of those tests. The next release still
needs a supervised native-runner pilot, including a draft-to-ready transition.
