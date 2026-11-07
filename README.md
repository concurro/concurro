# Concurro

Concurro turns an enrolled GitHub issue into a reviewed contribution. It plans against
the target's charter, obtains independent design approval, implements on an ephemeral
GitHub-hosted runner, and publishes an upstream draft PR from an existing fork.
Independent Claude and Codex reviews become native COMMENT reviews and finding threads.
When those reviews approve the current head and CI passes, Concurro marks the PR ready
for human review. It never merges.

The small Linux controller coordinates work and publishes validated text changes. The
exchange and inference gateway keep provider credentials central. Every model turn and
exploratory build runs natively on a disposable GitHub-hosted job; no Mac or host executor,
execution images, prescribed checks or build profiles are needed.

Targets own optional setup and agent instructions. Agent jobs restore caches read-only;
trusted target CI can warm them separately. Issues may come from upstream, the fork, or
both. Task branches and runner session refs remain in the fork.

| Path | Purpose |
| ---- | ---- |
| `service/` | Controller daemon, task scheduler and GitHub mutation gateway |
| `gateway/` | OIDC exchange, inference gateway and shared HTTPS edge |
| `engine/` | Planning, native runner turns, publication and recoverable reviews |
| `bundle/` | Five role contracts, schemas and shared instructions |
| `examples/stacks-core/` | Target config, charter, setup, instructions and caller workflow |
| `docs/` | [Current contracts and pending proposals](docs/README.md) |

See the [operations guide](docs/concurro.md), [deployment and migration](gateway/README.md)
and [MVP design](docs/design/mvp.md). Verify each release and target configuration
before enrolling live work.

## Development

```bash
python3 -m pip install -r service/requirements.txt markdown-it-py==3.0.0
python3 -m unittest discover -s engine/tests -v
python3 -m unittest discover -s service/tests -v
python3 -m unittest discover -s gateway/tests -v
python3 -m compileall -q engine service gateway/exchange
python3 engine/bundle.py examples/stacks-core
```

Tests use fake GitHub/provider transports and local Git/HTTP fixtures. They do not call
live models or Docker. A live pilot requires an explicit deployment and target enrollment.
