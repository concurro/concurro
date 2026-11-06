# MCP tools on ephemeral runners

Status: proposal, 2026-11-06. Design only; no implementation or deployment.

Give Concurro agents normal MCP clients alongside their shell and native tools. Start
with `cylewitruk/rust-analyzer-mcp` for the working repository and `cylewitruk/rust-mcp`
for published dependencies. The integration is language-neutral: targets choose their
servers and setup; Concurro handles configuration, lifecycle and credentials.

## Placement

| Server | Location | Purpose |
| ---- | ---- | ---- |
| rust-analyzer-mcp | A stdio child of each agent CLI on its GitHub runner | Symbols, references, callers, types, diagnostics, macro expansion and related tests in the actual checkout |
| rust-mcp | A persistent HTTP service behind authenticated ingress | Published crate APIs, source, versions, features, compatibility and advisory information |

No agent or analyzer runs on the Mac or controller. The shared dependency service has
its own storage and resource limits. It may share the Hetzner host if capacity allows,
but has no controller storage, App key, provider credentials or Docker socket mounted.
It does not receive a checkout or execute the candidate's build scripts.

Do not put rust-analyzer behind a shared workspace service: its index belongs to one
agent's files, including that agent's exploratory edits. Conversely, rebuilding
rust-mcp's PostgreSQL/index container for every turn would discard useful shared work.
A runner-local HTTP service remains possible through target setup for other MCPs.

## Configuration and installation

Add an optional `mcp.servers` map to the existing target configuration. Existing
configs remain valid. This is proposed syntax, not a supported configuration yet:

```yaml
mcp:
  servers:
    rust-analyzer:
      transport: stdio
      command: rust-analyzer-mcp
      args: ["{workspace}"]
    rust-dependencies:
      transport: http
      service: rust-dependencies
```

`{workspace}` is the current turn's checkout, substituted as one argument, never
through shell interpolation. A local HTTP server can instead specify a literal URL.
`service` names an operator-approved remote connection; its endpoint and credential
are resolved centrally, not supplied by an issue or candidate. URL and service are
mutually exclusive. The initial local fields are command, args and non-secret env;
ordinary transport timeouts can be configured without provider-specific target files.

Use one normalized server definition and two CLI renderers. MCP selection is independent
of the named model profiles. Initially every role gets the configured servers; no
per-provider catalogs, per-tool permissions framework or role-selection language is
needed. Additional optional fields can be added without changing that structure.

The target's existing pinned `.concurro/setup` installs the fork binary and a compatible
rust-analyzer component, using an immutable revision or verified release artifact.
Do not accidentally install the upstream crates.io package instead of the user's fork.
Setup owns Cargo features and analyzer settings; do not force `--all-features` across
projects. Build scripts and proc macros may execute on the disposable runner, just as
agent-run builds already can. They must never run on the controller.

Concurro reads MCP configuration from the task's pinned `config_sha`, includes it in
normal configuration/session binding, and sends it with the turn. Candidate `.mcp.json`
or provider config files do not replace that configuration. This pins normal execution;
it does not pretend to sandbox an agent that already has a shell.

## Turn and session lifecycle

1. Materialize the existing base/head or candidate workspace.
2. Resolve the pinned MCP definitions against that workspace and fresh session grants.
3. Start the provider CLI with explicit MCP configuration. The CLI owns its stdio children.
4. Use the existing process cleanup to stop the CLI, MCP children and analyzer descendants
   before extracting edits or archiving the retained session.
5. Recreate configuration and processes on the next turn, including resumed conversations.

Every analyzer starts against the current checkout, not the setup checkout or the
previous turn's absolute path. A reviewer can inspect another checked-out revision
with the fork's `workspace_path` support. Its exploratory edits remain local, as today.
Findings still cite the reviewed head; diagnostics from an edited scratch tree do not
prove a defect at that head.

Do not retain analyzer processes, indexes, build output, generated client configuration
or MCP credentials in the transcript archive. Build/download caches continue to use the
target's existing read-only GitHub cache setup. No cache-writing permission is added.

For Codex, retain `--ignore-user-config` and supply the normalized `mcp_servers` through
explicit overrides rather than restoring ambient configuration. The current TOML value
serializer will need array support. For Claude, use a generated scratch JSON file with
`--mcp-config` and `--strict-mcp-config`. Keep hooks and unrelated configuration disabled.
Prove both paths with the pinned CLI versions, including resume; current documentation
alone is not proof that those exact binaries behave identically.

## Remote access

rust-mcp currently exposes Streamable HTTP at `/mcp` without built-in authentication.
Keep that backend private. Use the existing gateway's MCP facilities for routing and
an authenticated session boundary, after verifying them with the deployed gateway.
An inference virtual key must not be assumed to authorize MCP automatically.

The exchange grants access only to connections enabled for the issued task/role session.
Prefer a separate short-lived MCP capability, bound to the same session, with expiry and
revocation. It must not grant inference administration, GitHub writes or arbitrary
upstream access. Validate authorization on every HTTP request, including requests that
resume an MCP transport session. Preserve MCP streaming and protocol/session headers.
If the gateway cannot enforce that contract, use an authenticated narrow proxy rather
than expose the backend or put a long-lived shared secret in runner configuration.

Keep credentials out of public config, command arguments, reports and retained files.
Use environment/header helpers supported by each client. Redact endpoints and headers
from diagnostics. Local tool launchers should omit unrelated credential variables;
this reduces accidental exposure but is not an isolation boundary within the same job.

The shared index covers public crates. Do not upload private source or silently send
private manifests to it. Queries and tool output remain untrusted evidence. On-demand
indexing remains available, with service-side resource/concurrency limits; agents do
not get administrative access to the index or its database.

## Agent guidance and failures

Provide concise trusted guidance: prefer rust-analyzer for workspace semantics and
rust-mcp for published dependency questions; use the shell, repository source and tests
when either is unavailable or incomplete. Reuse the server's maintained usage guidance
at a pinned revision, rendered through Concurro's existing instruction mechanism, rather
than re-enabling arbitrary plugin or skill discovery.

These are useful tools, not new approval gates. Missing indexes, slow startup or an MCP
outage should yield a visible availability warning and let the agent continue. Do not
turn an MCP connection failure into a repeated model-session failure. Bound startup and
calls, and report unavailable tooling rather than empty results that look authoritative.
Malformed target configuration is still a configuration error. Existing evidence,
review and CI requirements are unchanged.

## Implementation and proof

1. Add the optional server definitions, validation and shared renderers. Start with
   stdio rust-analyzer, installed by target setup. Keep runtime changes small and testable.
2. Add the operator-owned HTTP connection and session-scoped authentication for rust-mcp.
   Reuse the current claim/end/reconciliation lifecycle; no new scheduler or database.
3. Update trusted tool guidance and run a dedicated proof with both configured CLIs.

The proof must demonstrate:

- Both CLIs discover and call both servers without interactive setup.
- The analyzer sees the candidate head and subsequent edits; another role sees its own
  checkout. A resumed turn uses its new path, with no stale analyzer or leaked process.
- A known crate/version query returns dependency information with its provenance.
- Candidate config cannot replace the declared servers; secrets do not enter retained
  archives or public logs; revoked sessions cannot use the remote MCP route.
- Missing servers and cold indexes permit an ordinary shell-based turn to finish.
- No-MCP targets behave as before. Record cold-start time and peak memory on stacks-core
  before considering shared indexes or further infrastructure.

No deployment or changes to running pilots are part of this proposal.

## References

- [User's rust-analyzer fork](https://github.com/cylewitruk/rust-analyzer-mcp), including
  workspace selection and feature/config arguments; checked against the local source.
- [rust-mcp](https://github.com/cylewitruk/rust-mcp), including its HTTP transport,
  embedded PostgreSQL and agent guidance; checked against the local source.
- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp).
- [Claude Code MCP](https://code.claude.com/docs/en/mcp) and
  [explicit CLI configuration](https://code.claude.com/docs/en/cli-reference).
