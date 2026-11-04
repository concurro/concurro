# Runner executor

Status: implemented in revised form for model turns; see
[the inference gateway](inference-gateway.md#turns). Turns, not sessions with
diff-only results, are the unit; the exchange relays them; credentials are
gateway keys; trusted checks still run on the service host. The text below is the
original direction. Runs [the agent sandbox](agent-sandbox.md) on
GitHub-hosted runners in the target repository, with credentials fetched from
the Concurro service over OIDC. It implements the service's CI-runner executor
([service.md](service.md#execution-contract), migration step 8) earlier.
Alternatives considered are recorded in
[executor-options.md](executor-options.md).

## Goal

Give the implementer and local reviewer their normal tools (shell, build,
tests, web research) without running build machines ourselves, while keeping
GitHub write credentials, and the App key, entirely away from the agent.
[Open workspaces](open-workspaces.md) extends the same sessions to every role.

## Settled requirements

- One implementer per issue, from any provider (Claude, Codex, Copilot), with
  routing by capability possible later.
- Subscription tokens and API keys are both supported. A provider credential
  exposed to the agent is an accepted risk, capped by provider limits.
- Agent and check builds use the fastest profile: the rust execution profile's
  dev-profile `cargo check` and `cargo nextest`, with no LTO. Release builds with
  LTO are for releases only, outside Concurro.
- Concurro's gates (trusted checks, reviews, CI, human merge for build-checked
  paths) apply before merge; the agent's branch may be published first.
- Runner minutes and any Copilot usage belong to the target organization.
  Standard runners cost nothing for public repositories.

## Design

### Components

| Component | Where | Holds |
| ---- | ---- | ---- |
| Concurro service: worker, mutation gateway, identity service, credential service, executor endpoint | A small Concurro VM | App key, provider credentials, registry |
| Caller workflow, `.github/workflows/concurro-executor.yml` | Target repository, default branch, protected by a ruleset requiring review | Nothing; calls the reusable workflows below at a pinned commit |
| Reusable agent workflow, `agent.yml` | `concurro/concurro` at the pinned engine commit | Nothing until it fetches its session's credential |
| Reusable checks workflow, `checks.yml` | `concurro/concurro` at the pinned engine commit | Nothing; never receives a provider credential |
| Agent sandbox | A container on the agent runner | The workspace and one provider credential |
| Extraction container | A second container on the agent runner | The workspace (read-only) and the base archive; no credential and no `ACTIONS_*` variable |
| Check container | A container on the checks runner | The candidate tree; no credential, no network, no `ACTIONS_*` variable |

The caller workflow:

- triggers on `repository_dispatch` with `types: [concurro-agent, concurro-checks]`,
  and runs one job per type, calling `agent.yml` or `checks.yml`;
- sets `permissions: { id-token: write }` and `cache-mode: read` on each job,
  and nothing else;
- passes only the session or check ID from the payload. The agent image digest,
  engine ref and endpoint URL live inside the reusable workflows, where
  `job_workflow_sha` covers them, and a test asserts that they accept no other
  input;
- gives the two jobs distinct `concurrency` groups keyed by that ID, without
  cancellation, so a check never waits behind the agent job that requested it.
  The service, not the workflow, holds exclusive ownership of an issue's
  implementation.

The reusable workflows set their own `timeout-minutes` (at most 360 for
`agent.yml`; a shorter limit for `checks.yml`), since a calling job cannot, and
set `cache-mode: read` on their jobs as well. A called workflow never receives
more cache access than its caller granted, and requesting more fails
validation, so caller and called jobs must both say `read`. `read` allows
restoring the target's default-branch caches, the trusted seed that
[open workspaces](open-workspaces.md) describes, and refuses every save.

The target's Actions policy must allow `concurro/concurro` reusable workflows.
GitHub's "require actions to be pinned to a full-length commit SHA" policy does
not cover reusable workflows, so the endpoint's `job_workflow_sha` check is the
only enforcement of the pin.

### Sessions and turns

A **runner session** is one agent job serving one implementation phase, up to
its timeout. It contains **turns**. Each turn is an operation in the existing
execution contract, with its own logical operation ID, attempt ID, request
digest, budget reservation and deadline, issued by the worker under the
issue's current ownership generation.

A fresh OIDC token proves which job is calling, not that a turn is authorized.
The endpoint serves a turn only if the worker has issued it for that session
and generation.

Three rules hold in every version, including the pilot:

- **Atomic binding.** A session or check binds to exactly one run, and only at
  `run_attempt` 1. A second run, or a re-run, is refused, whether or not the
  first bind has happened.
- **Immutable results.** The endpoint stores a turn's result digest (the diff
  and receipt) atomically with its acknowledgment. An identical retry receives
  the original receipt; a different result for the same attempt is rejected.
  Trusted checks name the exact candidate by that digest.
- **Heartbeat.** A loop in the host step posts a heartbeat every minute,
  independently of turns, during agent execution, extraction and waiting. The
  session's lease is a few minutes and is separate from ownership and turn
  deadlines. The host step stops all work (it kills the containers and exits)
  when the endpoint answers that the session has ended, and also when its last
  successful heartbeat is older than the lease, which covers network loss or a
  dead service.

Recovery stays manual in the pilot (see [Pilot scope](#pilot-scope)).

### One session

1. **Dispatch.** The worker records the session and asks the mutation gateway to
   send a `repository_dispatch` event of type `concurro-agent` to the target,
   carrying only the session ID. `repository_dispatch` needs Contents write,
   which the App already has, and always runs the workflow on the default
   branch. The gateway sends only these event types, and only to enabled
   targets. Like any GitHub write, a dispatch with an unknown outcome stays
   unresolved in the gateway until an operator resolves it. The ID is not a
   secret: every App installed on the target with Contents read receives the
   payload, so the claim checks, not the ID, decide who is served.
2. **Session start.** The agent job's host step requests an OIDC token with
   Concurro's audience and presents it with the session ID. The endpoint
   verifies the token against GitHub's signing keys, checks its claims (below),
   binds the session to that run, and returns a source archive URL for the
   pinned commit and the session's credential. A retry from the bound run
   receives the same response.
3. **Agent run.** The host step starts the agent container with the workspace
   and that credential only, without the Docker socket, host mounts or host
   environment. Its bridge network refuses traffic to the runner itself,
   private ranges, link-local addresses including `169.254.169.254`, and
   Azure's wire server at `168.63.129.16`, with IPv6 off. The runner's
   passwordless `sudo` sets up those rules and gVisor. The agent works with its
   normal tools, as [agent-sandbox.md](agent-sandbox.md) describes.
4. **Turns.** The host step polls the endpoint with a fresh OIDC token for the
   next issued turn, and passes it to the agent. Session and build directory
   stay warm across repair rounds.
5. **Diff extraction, after every turn.** The host step freezes the agent
   container (`docker pause`, or `docker kill -s STOP` and `CONT` if pausing
   under `runsc` proves unreliable) so nothing writes to the workspace,
   including background processes. The extraction container compares the workspace with
   the base archive: a plain recursive diff, or `git diff --no-index` run from
   outside the workspace with `GIT_CEILING_DIRECTORIES` set,
   `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_NOSYSTEM=1`. No repository
   configuration, hook or filter written by the agent runs. The host step posts
   the turn's result and diff with a fresh OIDC token, then unfreezes the
   container. It never writes workspace-derived text to `GITHUB_ENV`,
   `GITHUB_PATH` or `GITHUB_OUTPUT`. The worker validates the diff as today
   (path policy, file count, sizes, protected files).
6. **Trusted checks.** The worker dispatches `concurro-checks` with a check ID
   bound to the candidate's result digest. The checks job's host step fetches
   that candidate, then runs the profile's approved checks inside the check
   container: credential-free and network-disabled, as today's workbench.
   Candidate code (tests, `build.rs`) therefore runs only in that container,
   never directly on the runner, where it could mint the job's OIDC token. The
   host step posts the results. Only these results count as evidence, and they
   reach the agent as its next turn when repair is needed.
7. **Publication.** The mutation gateway publishes the candidate as today. The
   PR's CI, reviews and merge policy follow.

### Claim checks

The executor endpoint accepts a request only when all of these hold:

| Claim | Required value |
| ---- | ---- |
| `iss`, `aud`, `exp`, signature | GitHub's issuer and keys; Concurro's audience; unexpired |
| `repository_id`, `repository_owner_id` | The session's or check's target |
| `job_workflow_ref`, `job_workflow_sha` | Concurro's `agent.yml` or `checks.yml` at the target's pinned engine commit; both checked, since the `job_workflow_ref` format for a commit pin is undocumented (record it in step 1) |
| `event_name`, `ref` | `repository_dispatch` on the default branch |
| `actor_id` | The Concurro App's bot user, so a run a person dispatched is refused (confirm in step 1 that an installation-token dispatch yields the bot) |
| `runner_environment` | `github-hosted` |
| `run_id`, `run_attempt` | First bind at `run_attempt` 1 only; afterwards, the bound run and attempt |
| `jti` | Not seen before |

Roles are separate. `agent.yml` may start a session, heartbeat, poll turns and
post turn results; it can never submit check evidence. `checks.yml` may fetch a
candidate and post check evidence; it can never fetch a provider credential.
Every request must also match the session's or check's target, ownership
generation, attempt and request digest.

### What the runner holds

| On the runner | Risk if the agent or candidate code reached it | Control |
| ---- | ---- | ---- |
| `ACTIONS_ID_TOKEN_REQUEST_TOKEN` | Minting OIDC tokens to act as the job | Never passed into a container; the endpoint serves only issued turns for the bound run |
| `ACTIONS_RUNTIME_TOKEN` | Writing the target's Actions caches and artifacts. Default-branch caches are visible to every branch, including release workflows, and cannot be modified once saved, so a poisoned entry persists | Never passed into a container; `cache-mode: read` makes the cache service refuse every save for the job, so nothing can be poisoned; no artifacts are uploaded; gVisor makes a container escape harder; the target avoids prefix `restore-keys` in release workflows |
| `GITHUB_TOKEN` | Nothing | `permissions: { id-token: write }` only; no checkout (the source comes as an archive) |
| Target secrets and variables | Exposure | The workflows reference none, and use no environment with secrets |
| The provider credential (agent runner only) | The agent has it anyway | A dedicated key; proxy tokens where the provider supports them; masked in logs |

A container without the Docker socket, host mounts or host environment cannot
read these: its PID namespace hides the runner's `/proc`, and the work directory
and runner listener are outside it. The remaining path is a container escape,
which gVisor narrows.

The target is public, so its run logs are public. Host steps log no credential
or agent transcript; the transcript stays in the job and in Concurro's records.

Published agent branches still run the target's own `pull_request` workflows,
with whatever secrets those use, and any `pull_request_target` workflow would
extend that to forks. Step 0 audits them.

### Credentials

The credential service resolves a credential per session. Delivery by type:

- **API keys:** a per-session inference-proxy token where the CLI accepts a
  custom endpoint; otherwise the key itself.
- **Subscription tokens:** a short-lived access token, never a refresh token.
  Refresh stays in the credential service (service migration step 6).
- **Copilot:** the Copilot SDK with bring-your-own-key, or a one-hour
  server-to-server token for the target organization's installation.

An expiring credential bounds the job: its timeout is no longer than the
credential's remaining lifetime. Renewal within a session comes later.

Direct API keys and long-lived tokens reach the runner under the operator's
accepted exposure risk, as [service.md](service.md#adapters) records. The
endpoint revokes proxy tokens when the session ends.

### Changes to Concurro

- **App permissions:** none new. Dispatch uses Contents write; results arrive
  through Concurro's endpoint, so no Actions read is needed.
- **Engine:** an executor adapter behind the execution contract. The implement
  stage consumes workspace diffs instead of JSON file replacements, and takes
  trusted check results from the checks job instead of the local workbench.
- **Service:** a public executor endpoint beside webhook ingress, with OIDC
  verification; durable session, turn, check and heartbeat records in the
  registry; a gateway dispatch allow-list; and per-session credential delivery
  in the credential service, beyond today's resolver, which only reads the
  environment.
- **Repositories:** two reusable workflows and the agent and check images in
  `concurro/concurro`; a caller workflow in each target.

## Pilot scope

One provider, one target, supervised mode, merging disabled.

In scope: the three rules in [Sessions and turns](#sessions-and-turns),
isolated extraction, trusted checks on the checks runner, role separation, and
a dedicated API key for the pilot provider.

Recovery is manual and uses existing mechanisms:

| Event | Pilot handling |
| ---- | ---- |
| Unknown dispatch outcome, or any other unknown GitHub write | Stays unresolved in the mutation gateway until an operator resolves it, as today |
| Lost session-start response | The bound run retries and receives the same response |
| Lost acknowledgment | The run retries; an identical result receives the original receipt |
| Lost heartbeat, timeout, cancellation, or a service restart | The session is invalidated and the task blocks; `/concurro retry` starts a new session from the last accepted diff |
| Ownership change | The old session is invalidated; its runner stops at its next heartbeat, or when its lease expires |

Deferred, with what brings each back:

| Capability | Trigger |
| ---- | ---- |
| Automatic dispatch retry, as a narrow gateway exception that records each send, binds one run atomically and rechecks ownership | Manual dispatch resolution becomes a burden |
| Credential renewal within a session | A provider whose credentials expire within a normal session, such as Copilot's one-hour tokens |
| Transparent session recovery after a restart | Restarts lose enough work to matter |
| Further providers and their credential adapters | The pilot completes an issue |

## Rollout

| Step | Work | Done when |
| ---- | ---- | ---- |
| 0 | Measure the rust profile's checks (`cargo check` and `cargo nextest` on the dev profile) on a standard runner in a throwaway workflow on the fork, and audit the target | Free disk after removing preinstalled tools (first), peak disk and memory, `target` size, and cold and cache-restored times are known, under `runc` and gVisor (systrap, and KVM if `/dev/kvm` is usable); the target's workflows, triggers, secrets, any `pull_request_target`, rulesets and Actions policy are audited |
| 1 | Credential-free OIDC probe: the caller and reusable workflows, the endpoint's claim checks and run binding, with no provider credential and no agent | Tests refuse a second run, a re-run (before and after the first bind), a person-dispatched run, a non-default ref, a modified or unpinned workflow, a self-hosted runner, a replayed token, and each role's forbidden calls; the observed `job_workflow_ref` format and dispatch `actor` are recorded |
| 2 | Runner lifecycle for the pilot: session, turn, check and heartbeat records, the dispatch allow-list, turn polling and per-turn extraction | Tests cover a long turn that stays healthy and then loses its heartbeat (the host stops locally), a lost acknowledgment, a conflicting duplicate result, a crash during acceptance, pause and unpause under `runsc` (or the signal fallback), and a hostile `.git` configuration that runs nothing during extraction |
| 3 | Supervised pilot with one provider on a dedicated API key, trusted checks on the checks runner, and the implement stage on diffs | A pilot issue's implement stage completes on runners, with evidence only from `checks.yml` |
| 4 | The deferred capabilities, as their triggers arise, and further providers | Each provider completes a pilot issue |
| 5 | SDK transports for the implementer and local reviewer, Codex first | Typed events, early refusal of protected-path edits, and stall detection |

Steps 1–3 build on the service skeleton's worker and mutation gateway, now in place; the probe also needs the service's endpoint to be publicly reachable.

### Step 0: target audit (`cylewitruk/stacks-core-autobot`, 2026-11-02)

- **Repository:** public and user-owned. Actions allows all actions and reusable
  workflows, with no commit pinning required, so the endpoint's
  `job_workflow_sha` check is the pin. The default `GITHUB_TOKEN` is read-only;
  fork PRs from first-time contributors need approval.
- **Triggers:** no `pull_request_target` or `workflow_run`. The `pull_request`
  workflows (`ci`, `check-clippy`, `check-nix`, `concurro-rust`) reference no
  secrets and run with `contents: read`, so published agent branches reach no
  secrets.
- **Secrets:** none at repository level. They live only in the `concurro` and
  `autobot` environments, both with required reviewers and branch policies. The
  executor workflows use no environment. The `release` environment that the
  release workflows reference doesn't exist on the fork.
- **Caches:** `create-test-caches` and `concurro-rust` save default-branch caches
  on pushes to `main`, and PR CI restores them through deliberately broad
  `restore-keys`. These are exactly the entries a container escape could poison,
  which `cache-mode: read` on executor jobs prevents, since it refuses saves.
  The release workflows use no Actions cache.
- **Ruleset `Protect main`:** pull requests with one approval, last-push
  approval, stale-review dismissal, signed commits and linear history. It covers
  the caller workflow file, so no separate rule is needed. It requires no status
  checks (the earlier `Concurro Rust` follow-up is still open), and the owner can
  always bypass it.

### Step 0: capacity results (run 41450737419, 2026-11-02)

Standard `ubuntu-24.04` runner on the public fork: 4 CPUs, 16 GB of memory,
`/dev/kvm` present, and 86 GiB free before any cleanup (114 GiB after it; `/mnt`
is the same filesystem). GitHub's documented 14 GB is not the limit.

| Phase | runc | gVisor systrap | gVisor KVM |
| ---- | ---- | ---- | ---- |
| `cargo fetch` | 29 s | 36 s | 51 s |
| `cargo check --workspace`, cold | 168 s | 222 s | 311 s |
| `nextest --workspace --lib --no-run`, cold | 711 s | 826 s | 1,250 s |
| `nextest run -p stacks-common --lib` | 60 s | 74 s | 94 s |
| `cargo check --workspace`, after one change | 8 s | 17 s | 21 s |
| Test build, after one change | 78 s | 132 s | 195 s |
| Peak memory used | 7.6 GiB | 7.8 GiB | 7.4 GiB |
| `target` size | 14 GiB | 14 GiB | 14 GiB |

Conclusions:

- Standard runners fit with ample headroom: about half the memory and a sixth
  of the free disk. The disk cleanup is unnecessary, and the fallback is not
  needed.
- gVisor uses the systrap platform. It adds about 20% to a cold build and about
  a minute to a repair round's rebuild. The KVM platform is slower on these
  nested VMs.
- A session's cold start costs about 18 minutes under systrap, after which
  repair rounds rebuild in about two and a half minutes.

The measurement workflow is `.github/workflows/concurro-runner-measure.yml` on
the fork. It runs on pushes to the `concurro/runner-measure` branch, which
trigger no other workflow, or on manual dispatch, and reports per runtime in the
job summary. It samples host resources during compilation and a limited test
run (`stacks-common`), so it establishes initial capacity, not that the full
workspace tests complete. A failed phase fails the job.

## Fallback

If step 0 shows the build doesn't fit, the same workflows run on larger
GitHub-hosted runners, billed to the target organization (standard runners are
free for public repositories; the pilot target is a user-owned fork). Failing
that, the same sandbox runs on a Concurro-operated host with the agent and
controller on separate machines. The sandbox, contract and endpoint are
unchanged.

## Open questions

- Whether `repository_dispatch` delivery latency and runner queueing are
  acceptable per session and per check.
- Cache use: settled by [open workspaces](open-workspaces.md). Jobs restore the
  target's own default-branch cache with `cache-mode: read`, and Concurro never
  saves one.
- Roles: settled by [open workspaces](open-workspaces.md). Every role
  (planners, design reviewers, the implementer and reviewers) can run in
  sessions on runners; the service-host adapter remains for the fallback host.
