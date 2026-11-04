# Concurro

An engine for end-to-end autonomous, agentic implementation of GitHub issues.

A writer enrolls an issue with the `concurro` label. Concurro checks it against the
repository's charter, has a planner propose a bounded design, and requires every
participating model to approve that exact design — cooperatively, then again in
fresh sessions — before any code is written. An implementer then edits and tests
in a credential-free local workbench, a fresh reviewer checks the result, and only
then does the controller publish a pull request for independent Claude and Codex
review and your CI. Autonomous merge is off by default.

Every decision, vote, objection and response is recorded on the issue: a
bot-authored audit artifact holds the authoritative state, and each model posts
its own attributed review comments.

## How it fits together

| Path | Contents |
| ---- | ---- |
| `action.yml` | Composite action a target workflow calls, pinned by commit SHA |
| `engine/` | Controller, agent and workspace entry points, GitHub and checkpoint handling, tests |
| `bundle/` | Shared instructions, role routing, response schemas and the six stage skills |
| `docker/` | Inference image (all provider CLIs) and the Rust execution image |
| `docs/` | [Operations guide](docs/concurro.md) and artifact measurements |
| `examples/stacks-core/` | A complete target configuration, charter and caller workflow |

A target repository supplies `.concurro/config.json`, `.concurro/charter.json` and
a workflow that runs `uses: concurro/concurro@<sha>` on a dedicated self-hosted
runner with its own GitHub App and provider credentials. The engine's instructions
and the target's charter are hash-bound together, so changing either invalidates
in-flight approvals.

## Development

```bash
python3 -m unittest discover -s engine/tests -v
python3 -m unittest discover -s service/tests -v
python3 -m compileall -q engine service
python3 engine/bundle.py examples/stacks-core
```

The suite uses fake GitHub and model transports: no network, credentials or model
quota. Docker integration tests run only when `CONCURRO_TEST_AGENT_IMAGE` or
`CONCURRO_TEST_EXECUTION_IMAGE` is set; see the operations guide for building the
images.

## Status

v1 is in supervised pilot. Model calls go through an [inference gateway](docs/design/inference-gateway.md)
and run on GitHub-hosted runners of the target; Claude and Codex are routed, Copilot not yet. It supports
pluggable [execution profiles](docs/design/execution-profiles.md); `rust` is built in.
The webhook-driven [service](docs/design/service.md) replaces the Actions controller. Read the [trust boundaries](docs/concurro.md#trust-boundaries)
before running it: model containers hold a capped gateway session key, run with a
shell and network, and belong on a disposable host such as a GitHub-hosted runner.
