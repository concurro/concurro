<h1 align="center">
  <img src="docs/assets/concurro-banner.png" alt="Concurro: your AI team, from issue to PR" width="100%">
</h1>

Concurro takes a GitHub issue through planning, implementation and independent AI review,
then hands a pull request to maintainers. Separate agent sessions develop the plan,
write the code and review the result, with findings fed back into repair rounds.
Progress, plans and reviews stay visible on GitHub.

You choose the models, their reasoning effort and the project's guidance. Concurro
coordinates the work; your project's CI checks the contribution, and humans decide
whether to merge it.

## From issue to pull request

Once a repository is configured, a writer enrolls an issue with the `concurro` label.
The issue can live in the upstream repository or its configured fork.

1. **Assess the issue.** An intake agent checks whether it fits the project and has
   enough information to proceed. It asks for clarification when needed and records
   concerns for the planner and reviewers.
2. **Plan and review.** A planning agent proposes the change. Independent reviewers
   examine the plan and can request revisions before implementation begins. Manual
   mode also waits for a writer to approve the plan.
3. **Implement.** The implementation agent works in a checkout, using the project's
   tools and running tests as needed. Concurro validates the resulting changes and
   opens a draft pull request from the fork to upstream.
4. **Review and repair.** Independent reviewers inspect the PR. Findings appear in
   GitHub review threads, and the implementation agent works through requested fixes.
   The reviewer who raised a finding verifies its resolution.
5. **Hand off.** When all configured reviewers approve the current revision and CI
   passes, Concurro marks the PR ready for human review. It never merges.

The issue dashboard shows what is happening now, which agents are working, and links
to the plan and reports. Diagnostics hold previous runs and validation details without
cluttering the current status. Complexity and change-risk assessments help explain the
work; they do not independently approve or block it.

## What you control

Each project supplies a charter describing its goals and boundaries, plus configuration
in the fork's `.concurro/` directory. You can choose:

- **The team:** named model profiles with explicit effort settings, assigned separately
  to intake, planning, design review, implementation and PR review. Claude Code and
  Codex are supported; each review team requires at least two distinct provider/model pairs.
- **The workflow:** manual plan approval or autonomous progression, with human merging
  in either mode.
- **The environment:** optional setup and role instructions using the project's own
  toolchains, build commands and tests.
- **The boundaries:** protected paths, allowed changes and limits on model calls and
  repair rounds.

Writers can answer questions, approve a plan, stop work or retry a failed step through
issue comments. See the [operations guide](docs/concurro.md) for configuration and
commands, including how retry differs from explicitly invalidating an earlier assessment.

## Getting started

Concurro is self-hosted. A deployment needs:

- An existing public upstream repository and a compatible fork, with GitHub Actions
  enabled. Concurro creates contribution branches in the fork.
- A GitHub App installed with the required permissions on both repositories, and
  webhooks configured for the service.
- A Linux host running the controller, session exchange, inference gateway and HTTPS
  endpoint, with centrally configured provider credentials.
- Target configuration and a caller workflow in the fork, plus the gateway URL stored
  as a repository secret.

Start with the [deployment guide](gateway/README.md), then adapt the
[stacks-core example](examples/stacks-core/) using the
[target setup instructions](docs/concurro.md#target-setup). The example uses Rust;
target setup and instructions can supply other toolchains or a mixture of them.
Verify the deployment and target permissions before enrolling an issue.

## How it runs

A small controller maintains task state, dispatches work and publishes validated
changes. Every agent turn, build and exploratory test runs on an ephemeral GitHub-hosted
runner. Separate jobs isolate agent roles. Jobs restore caches read-only; trusted
project CI can warm those caches separately.

Provider credentials and GitHub write credentials stay on the server. Runners receive
session-scoped inference access and their GitHub job identity. Agents have native tools,
a shell and network access within their job; candidate code can run there. Agent reviews
and reported test results complement project CI and human review.

Current limitations include public repositories only and text-file changes only.
Concurro observes CI but does not automatically diagnose or repair CI failures; writers
can supply failure feedback to request a repair. It does not create forks, automatically
rebase contributions or turn PR-thread replies directly into repair requests.

The [documentation index](docs/README.md) covers the current architecture and clearly
separates proposals from supported features.

## Development

Create a virtual environment, install the test dependencies and run the offline checks:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r service/requirements.txt markdown-it-py==3.0.0 ruff==0.17.4 pyright==1.1.408
ruff check engine service gateway
ruff format --check engine service gateway
pyright --project pyrightconfig.json
python3 -m unittest discover -s engine/tests -v
python3 -m unittest discover -s service/tests -v
python3 -m unittest discover -s gateway/tests -v
python3 -m compileall -q engine service gateway/exchange
python3 engine/bundle.py examples/stacks-core
```

Configure Git once per clone to omit the formatting commit from blame:

```bash
git config blame.ignoreRevsFile .git-blame-ignore-revs
```

Tests use fake GitHub/provider transports and local Git/HTTP fixtures. They do not call
live models or Docker.

| Path | Purpose |
| ---- | ---- |
| `service/` | Controller daemon, scheduler and GitHub writes |
| `gateway/` | Session exchange, inference gateway and HTTPS edge |
| `engine/` | Planning, runner turns, publication and reviews |
| `bundle/` | Agent role instructions and output schemas |
| `examples/stacks-core/` | Example target configuration and workflow |
| `docs/` | Operating guides, architecture and pending proposals |

See [AGENTS.md](AGENTS.md) for repository contribution and deployment instructions.
