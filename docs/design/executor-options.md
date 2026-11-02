# Executor options

Status: record of the alternatives behind [the runner executor](runner-executor.md),
the proposed direction. Earlier rounds of this analysis are in Git history.

## Question

Where does the implementer (and local reviewer) run once it has a normal
shell, build tools and web access, so that it never holds or reaches a GitHub
write credential?

## Requirements that decided it

- One implementer per issue, from any provider, with routing by capability
  later.
- Subscription tokens and API keys both supported.
- Builds on the fastest profile (dev, no LTO), within the runner's memory and
  disk, and turns long enough to build, test and repair.
- Concurro's gates apply before merge; the agent's branch may be published
  first.
- No build machines of our own where GitHub can provide them.
- Runner minutes and Copilot usage billed to the target organization.

## Options

| Option | Verdict | Reason |
| ---- | ---- | ---- |
| A. [Agent sandbox](agent-sandbox.md) on a Concurro host | Kept as the sandbox, and as the fallback placement | Fits every requirement, but needs a build host of our own, beside the App key unless split |
| H. A's sandbox on GitHub-hosted runners in the target, credentials over OIDC | **Proposed** | A's properties without build machines; per-operation credentials; the App key never on the agent's machine. Measured to fit a standard runner with ample headroom |
| B. Copilot cloud agent through Agent HQ | Benchmark only | 59-minute sessions on Actions, a cold sandbox of unstated size, a seat-bearing user token, no request binding, and billing at API rates through Copilot |
| C. GitHub Agentic Workflows | Not chosen | Static provider secrets in Actions, an allow-list firewall, no sessions |
| D. Plain Actions job with static secrets | Superseded by H | H fetches credentials over OIDC instead |
| E. Rented sandboxes (Modal, E2B, Daytona) | Later, if needed | Same sandbox, another vendor; no requirement needs it yet |
| F. Claude Managed Agents | Not chosen | Claude only; cloud sandboxes have 8 GB of memory and 10 GB of disk |
| G. Provider cloud agents (Claude Code cloud, Codex cloud) | Not chosen | Personal logins (Claude's refresh is capped at 30 days), actions taken as the user, two different integrations, nothing for Copilot, and the Codex product marked legacy |

## Facts worth keeping

- Copilot cloud agent: three environments since 2026-10-28 (cloud sandbox,
  Actions, remote); a 59-minute cap on Actions; the agent tasks API accepts only
  user-to-server tokens and documents only a task ID and state in responses.
  The partner Claude and Codex agents use the Claude Agent SDK and the Codex
  SDK, with older model lists than Copilot's own harness.
- Copilot billing: since 2026-06-01, AI Credits at each model's API rates.
- Agent SDKs drive the same harnesses as the CLIs: the Claude Agent SDK runs the
  Claude Code binary; the Codex Python SDK drives the app-server; the Copilot SDK
  talks to the Copilot CLI and supports bring-your-own-key. They add typed
  events, tool-call callbacks and long-lived sessions, but are not a security
  boundary.
- GitHub-hosted runners for public repositories: 4 CPUs, 16 GB, passwordless
  `sudo`, 6 hours per job. GitHub documents 14 GB of storage, but a measured
  runner had 86 GiB free. Private repositories: 2 CPUs, 8 GB.
- OIDC tokens carry `repository_id`, `job_workflow_ref`, `job_workflow_sha`,
  `event_name`, `ref`, `run_id`, `run_attempt`, `runner_environment` and `jti`.

## Sources

- [About Copilot cloud agent](https://docs.github.com/en/copilot/concepts/agents/cloud-agent/about-cloud-agent)
- [Using Copilot cloud agent via the API](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/use-cloud-agent-via-the-api)
- [About third-party coding agents](https://docs.github.com/en/copilot/concepts/agent-capabilities/about-agents/about-third-party-coding-agents)
- [Copilot moving to usage-based billing](https://github.blog/changelog/2026/04/27/github-copilot-is-moving-to-usage-based-billing/)
- [GitHub-hosted runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [OIDC in GitHub Actions](https://docs.github.com/en/actions/reference/security/oidc)
- [Claude Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)
- [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)
- [Copilot SDK bring your own key](https://docs.github.com/en/copilot/how-tos/copilot-sdk/auth/byok)
- [Claude Code self-hosted environments (CI authentication)](https://code.claude.com/docs/en/self-hosted-environments-testing)
- [Managed Agents: cloud sandbox reference](https://platform.claude.com/docs/en/managed-agents/cloud-sandboxes-reference)
- [Codex Cloud (Legacy): internet access](https://learn.chatgpt.com/docs/cloud/internet-access)
