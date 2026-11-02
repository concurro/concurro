# Agent sandbox

Status: proposed. Extends [the service design](service.md) (trust boundaries,
step 5) so implementation agents can use their normal tools. [The runner
executor](runner-executor.md) proposes running this sandbox on GitHub-hosted
runners, with a Concurro host as the fallback.

## Problem

Agents today get three read-only MCP tools (`list_files`, `read_file`, literal
`search`) with an 80-call, 1 MB budget, and return whole-file replacements. They
cannot compile, run tests, research or navigate a large workspace, so each
defect costs a local repair round.

## Principle

Two things are protected: GitHub credentials, and the integrity of what merges.
A fully compromised agent may waste provider budget and propose a bad diff; it
must never hold or reach a GitHub credential, and its diff must pass the same
gates as today.

Not protected inside the sandbox: the target's source (public for the pilot)
and the provider credential, whose loss is capped by a spend limit at the
provider.

Input gating, screening and the CLIs' own approval classifiers make compromise
less likely, but they are not the boundary: repository source, dependencies,
web pages and tool output all reach the agent, and the classifiers judge tool
calls from the same context an injection lives in.

## Input trust

Today:

- Work starts only when a current writer applies `concurro`.
- Commands (`approve`, `retry`, …) count only from writers.
- The requirements snapshot holds the issue title and body, plus replies from
  the issue author or writers; other comments are excluded.
- Any requirements change reopens the design for fresh unanimous approval.

Gap: an issue author who is not a writer can still steer an enrolled task by
editing the body or replying. In autonomous mode that re-plans with no writer
involved.

Proposed:

- Replies count only from writers.
- Enrollment binds the digest of the title and body. Any later change, by
  anyone, moves the task to `await-human` until a writer comments
  `/concurro accept <digest>`. This needs no lookup of who edited the issue.
- Intake QC also checks for instruction-like content addressed to agents and
  escalates to a human. This is a tripwire, not a boundary.

## Sandbox

| Concern | Today | Proposed |
| ---- | ---- | ---- |
| Runtime | Docker (`runc`) | Docker with gVisor (`--runtime=runsc`), for agent and workbench containers |
| Network | Default bridge, open egress | Open internet egress; host, private and link-local addresses refused |
| GitHub credentials | None in the container | None in the container or anywhere it can reach |
| Provider credentials | Key in the container | API keys, each with a provider-side spend limit, or subscription tokens, bounded by the plan's usage limits |
| Tools | Three read-only MCP tools | The CLI's normal tools (shell, edit, search, web, rust-analyzer, cargo) in a writable workspace |
| Approvals | Not applicable | The CLI's automatic approval mode |
| Output | JSON file replacements | The workspace diff against the base, extracted by the controller |
| Validation | Path policy, sizes, protected files | Unchanged, applied to the extracted diff |
| Evidence | Trusted workbench, `--network=none` | Unchanged |

This note was written for the earlier placement: a dedicated Linux VM that
also holds the controller's App key and state, where the container boundary is
what keeps GitHub credentials out of reach. Under [the runner
executor](runner-executor.md) the sandbox runs on an ephemeral GitHub-hosted
runner that holds no App key, and that host only remains the fallback, with the
agent and controller on separate machines.

### Runtime: gVisor

gVisor implements the Linux kernel interface in user space, so a container's
system calls reach a small, hardened surface rather than the host kernel. It
is the simplest option that still materially reduces the risk of escaping to
the host, whether that is a runner holding Actions tokens or the fallback
host:

- Installation is one package and one `daemon.json` entry; each `docker run`
  adds `--runtime=runsc`. Images, mounts, limits and volumes are unchanged.
- It needs no KVM, so it runs on ordinary cloud VMs without nested
  virtualization.
- If assurance needs to grow, Kata Containers (a hardware VM per container)
  swaps in behind the same flag on a host with KVM.

Local development on macOS keeps plain Docker: Docker Desktop already runs
containers inside its own VM, and the tests use stubs.

### Network: the internet, but not the host

Agents may reach any public address, so they can research, read documentation
and fetch crates. What they must not reach is anything that could hold or mint
a credential:

- Sandboxes attach to one dedicated bridge network. Host firewall rules refuse
  its traffic to the host itself (the `INPUT` chain, since container-to-host
  traffic bypasses `DOCKER-USER`), to private and carrier-grade NAT ranges, and
  to link-local addresses, which covers the cloud metadata service. IPv6 is off
  on that network.
- DNS goes through Docker's embedded resolver.
- The Docker socket, the state directory and the settings are never mounted.
- The workspace is a copy of the snapshot with no Git remotes or credential
  configuration; the agent image has no `gh` login.
- The controller's webhook listener binds only the address the ingress proxy
  uses, never the sandbox bridge.

GitHub itself is reachable, anonymously and read-only in effect, which helps
research. A sandbox has nothing to authenticate with.

### Agent capability

- The implementer and local reviewer start in a writable copy of the snapshot
  (base plus current overlays) on a per-task volume. They can read anything in
  it, run anything, and edit freely.
- The CLIs run in their automatic approval mode (Claude's auto mode, Codex's
  automatic review), with Codex's own sandbox off since the outer sandbox is the
  boundary. The classifiers stop accidental damage and obvious misuse early,
  which saves runs; nothing depends on them.
- `cargo` may fetch from crates.io. The workbench's prepared cache seeds the
  agent's `CARGO_HOME` so most builds need no download. Manifest and lockfile
  edits stay protected by the path policy.
- Project instructions in the workspace (`CLAUDE.md`, `AGENTS.md`) now load.
  They come from the approved base and remain protected from edits.
- Hooks or MCP servers configured in the repository run only inside the
  sandbox, where the agent could run them anyway.
- When the turn ends, a credential-free helper extracts the diff. The
  controller validates it against the path policy, file count and sizes, then
  runs the trusted workbench checks and a fresh local review as today. Check
  results the agent ran itself are never used as evidence.

## Budgets

Budgets catch runaways; they do not constrain how an agent works.

- **Spend:** a provider-side limit on every key is the real cap, including for
  a leaked key.
- **Per turn:** a wall-clock timeout and the container's CPU, memory, disk and
  process limits.
- **Per task:** a generous ceiling on turns and repair rounds, then
  `await-human`.
- No tool-call, retrieval or byte budgets inside a turn.
- Diff limits stay, since they bound what reviewers must read. Step 5 of the
  migration sets them by risk tier.

## Alternatives

| Option | Isolation | Setup | Verdict |
| ---- | ---- | ---- | ---- |
| Plain Docker (today) | Shared host kernel | None | Not enough once agents have a shell on a host holding credentials |
| gVisor | User-space kernel | One package, one flag | Recommended |
| Kata Containers | Hardware VM per container | KVM, runtime configuration | Fallback if more assurance is needed |
| Firecracker directly | MicroVM | Own rootfs images, tap networking, orchestration | Too much to build and run |
| GitHub-hosted runners | Ephemeral VM per job, with this sandbox inside | Reusable workflows and an OIDC credential endpoint | Proposed placement; see [the runner executor](runner-executor.md) |

## What this adds and removes

Adds:

- the gVisor runtime flag;
- one bridge network and a few host firewall rules;
- seeding `CARGO_HOME` from the prepared cache;
- diff extraction.

Removes, for the implementer and local reviewer:

- the custom MCP server and its retrieval budgets;
- the JSON replacement contract;
- most local repair rounds spent on compile errors.

The egress proxy and endpoint mode are no longer needed for the sandbox.

## Migration

1. **Input trust (engine):** writer-only replies, a digest-bound enrollment, and
   `/concurro accept`.
2. **Sandbox network and runtime:** the dedicated bridge, the firewall rules,
   and `runsc`, with today's tools. This measures gVisor's overhead on a cold
   stacks-core build (the rust profile's dev-profile checks) at 4 CPUs and
   16 GB.
3. **Full tooling** for the implementer and local reviewer: a writable
   workspace, automatic approvals, seeded cargo, and diff extraction.
4. **Budgets:** per-task turn and repair ceilings, and a spend limit verified on
   each configured key where the provider exposes one.
5. **Limits by risk tier:** replace the flat diff limits (8 files, 128 KB per
   file) with limits set by risk tier.

Steps 1 and 2 are independent of the service and can land in the engine first.

## Open questions

- A private target's source would be exposed by open egress. Such a target
  could opt into an egress allow-list.
- gVisor's overhead on cargo builds was measured on a GitHub-hosted runner (see
  [the runner executor](runner-executor.md)). The systrap platform adds about
  20% to a cold build, and the KVM platform is slower.
- Whether the planner and design reviewers get a shell and web access too.
- Whether a dependency change can become a gated plan item: writer approval, an
  audit with `cargo deny`/`cargo vet`, and the lockfile regenerated by the
  workbench.
