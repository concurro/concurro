# Operating Concurro

The [MVP architecture](design/mvp.md) and [execution contract](design/mvp-execution.md)
govern this version. The [documentation index](README.md) distinguishes current contracts from pending proposals.
See [deployment and migration](../gateway/README.md) before updating an existing service.

## Target setup

Configure an existing upstream/fork pair in service settings, with `issues` equal to
`upstream`, `fork` or `both`. The App needs access to both repositories; writers in the
selected issue repository enroll work with the `concurro` label. Verify the
[App permissions and webhook subscriptions](../gateway/README.md#target-and-app-prerequisites)
before enrolling; granting permissions alone does not subscribe the App to events. Concurro never creates
forks or upstream branches. The current transport supports public repositories only;
it fetches source anonymously and does not give GitHub write credentials to runners.

In the fork's default branch, install:

- `.concurro/config.yaml` (or `config.json`, never both) and `.concurro/charter.json`, adapted from the example.
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
See the [scope envelope](design/scope-envelope.md) for cumulative file limits and large-file handling.

A task records the fork configuration commit separately from the approved upstream
source commit. Setup, instructions and caller workflow changes apply to new tasks;
configuration changes require a new task. `/concurro invalidate` adopts the current
bundle/charter but preserves the task’s configuration pin. An optional target `engine` pin must
match the served commit. Omit it when the same operator controls service and target.

## Models and agents

Use named model profiles and assign each pipeline role explicitly:

```yaml
models:
  opus55-high:
    provider: anthropic
    model: claude-opus-5-5
    effort: high
  sol61-medium:
    provider: openai
    model: gpt-6.1-sol
    effort: medium
  sol61-high:
    provider: openai
    model: gpt-6.1-sol
    effort: high
agents:
  triage: sol61-medium
  planner: opus55-high
  plan_reviewers: [opus55-high, sol61-high]
  implementer: opus55-high
  pr_reviewers: [opus55-high, sol61-high]
```

`anthropic` uses Claude Code; `openai` uses Codex. Each profile requires a model and
an explicit effort. Choose an effort supported by that model and the pinned CLI;
Concurro passes it through on fresh and resumed calls. Profile names are identifiers,
not settings: the `effort` field determines the actual value. Unknown profile fields
and role names are rejected so misspellings do not silently change execution.

Each review list needs at least two profiles with distinct `(provider, model)` pairs.
Aliases or different efforts for the same provider/model cannot appear together in
one reviewer list. Profiles may share a vendor, including different OpenAI models.
Only `plan_reviewers` review plans; triage, planner and implementer assignments add
no implicit reviewers. Triage and planning may share a profile, including in autonomous
mode. Votes, findings, reports, transcripts and runner staffing stay separate by profile; the audit records
the resolved provider, model and effort. Changing assignments or settings invalidates
the configuration binding rather than reusing old approvals or transcripts.

JSON supports the same structure. YAML uses safe data loading, with duplicate keys
and aliases rejected. The service image installs its pinned YAML dependency; local
operators running Python directly install `service/requirements.txt` first. Neither
runners nor the exchange need the YAML package.

Existing JSON configurations with `qc`, `qc_model`, `planner`, `implementer`,
`reviewers` and provider-keyed `models` remain supported, including their existing
design roster and checkpoint identities. Their compatibility mapping explicitly sends
Claude `medium` and Codex `low`; these are Concurro defaults, not a promise about defaults
in other CLI versions. To migrate, replace
those assignments and the model map together with `models` and `agents` above; do
not mix the two formats. Keep repository, policy and budget settings unchanged.
A JSON-to-YAML-only conversion has the same configuration digest; changing profiles
is a configuration change for new tasks. Evaluation uses
a configured profile name (legacy configurations still use `claude` or `codex`).

## Contribution loop

The issue dashboard shows intake, planning, design approval,
implementation, independent PR reviews and readiness. Independent unanimous design
approvals can skip a duplicate fresh round even after output corrections. Negotiated
revisions and restarted reviewer sessions still require fresh review. Retry renews the
correction allowance, not review independence or the total model-call budget. See the
[eligibility contract](design/design-review-corrections.md).

Reports are archived before
compaction. Findings have stable IDs and native threads. Only their raising reviewer profile can
retire them; ambiguous or reopened thread resolutions remain manual. PR-thread replies
do not automatically start repair work; use writer feedback on the issue followed by
`/concurro retry` as described below.

Implementation may add, change, delete or rename UTF-8 regular files inside the
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
`/concurro stop` suspends it. `/concurro invalidate` is a separate writer-authorized
operation: it accepts the current bundle/charter and restarts intake and planning,
retaining the PR, published head, base and spent budgets. Old approvals, votes and
sessions cannot authorize the new assessment. The audit records the actor, command
and old/new bundle and charter identities. For an existing PR, an issue comment
announces reassessment and asks maintainers not to merge yet. The PR's draft/ready
status is unchanged; automatic conversion back to draft is deferred. Refused
invalidation commands are consumed; retrying invalidation requires a new command.
Input-needed comments describe the applicable continuation.

The service's owner-only control socket provides `status`, `reconcile-once`, and
`resolve <intent> applied|not-applied <evidence> --operator <name>`. Check `--help` for
exact arguments. Resolve only after observing whether the uncertain GitHub write applied.
An unknown write prevents takeover and further writes for that task. Do not clear its
registry entry or blindly replay a dispatch.

`adopt`/`release` remain migration tools for old Actions-owned checkpoints, not a supported
execution fallback. Bundle changes block new model work, including reuse of retained
model results. They do not prevent observing PR/issue closure, CI or readiness under
the task's recorded approvals. `/concurro retry` keeps that bundle binding;
`/concurro invalidate` explicitly starts assessment under the current bundle.
A prepared publication finishes under its recorded approval before invalidation:
retry it first, then invalidate. Configuration changes still require a new task.

An explicitly requested `evaluate <issue-repository> <issue> <profile>` runs four
fresh design fixtures on ordinary runner sessions. It uses model budget and temporarily
owns that task, preserving its queue. Reports go under the service state's `evaluations/`;
no fixture model work runs on the host. Use an idle configured task; see
[design evaluation](design-evaluation.md) for fixtures and the command.

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
GitHub mutations or Docker execution are part of those tests. Check CI for the exact
release before deployment; offline tests do not verify live App permissions or target
configuration. A live pilot requires explicit enrollment and deployment authorization.
