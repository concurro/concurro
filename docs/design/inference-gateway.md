# Inference gateway

Status: implemented. The gateway holds provider credentials centrally; agents use
scoped session keys on GitHub-hosted runners. There are no service-host model sessions.
See [execution](mvp-execution.md) for runner lifecycle and [deployment](../../gateway/README.md)
for host setup, secret files and release commands.

## Components and protocols

The deployment uses upstream agentgateway, pinned by image digest, plus Concurro's
exchange. Claude Code uses native Anthropic Messages; Codex uses OpenAI Responses with
WebSockets disabled. The implementation supports these two harnesses; Copilot is not a
supported gateway route. Target profiles select explicit models and efforts.

Provider API keys or subscription credentials stay in the gateway deployment. The configured
routes inject the provider credential and remove/replace the client's session credential.
The ChatGPT route also supplies the account header; the Claude subscription route appends
its OAuth beta header. Subscription refresh and availability are operator responsibilities;
Concurro does not implement a general credential-refresh service.

The shared edge serves Concurro under `/concurro/`, forwarding `/v1/*` to inference and
`/exchange/*` to the exchange without buffering streams. Neither gateway nor service
publishes a host port. The exchange shares the gateway's network namespace; admin and
stats listeners bind to that namespace's loopback. The admin API has no authentication,
so network isolation is its boundary. Other containers on the shared edge network can
reach inference and exchange listeners directly and must still authenticate; they cannot
reach loopback administration through that network. Service route files are trusted
host configuration, not mutually isolated tenants.

## Session authorization

The controller's operator token authorizes session/turn submission. A task-bound session
records its repository, issue, role, ownership generation, source/configuration commits,
phase and model identity. Staffing cannot assign another task's or incompatible role's
turns to that job. Each session has its own run ref at the task's `config_sha`.

The runner exchanges a fresh GitHub OIDC token for its inference key and runner token.
The exchange verifies the signature against GitHub's keys and requires:

| Claim | Requirement |
| ---- | ---- |
| Issuer, audience and timestamps | GitHub issuer, configured audience and fresh token |
| Repository and owner IDs | The controller-issued target fork |
| Reusable workflow ref and SHA | The configured workflow and served engine commit |
| Event and ref | `workflow_dispatch` on `refs/heads/concurro/runs/<session-id>` |
| Actor | The controller App's bot identity |
| Runner environment | `github-hosted` |
| Run and attempt | Attempt 1; atomically bound to the first authorized run |
| Token ID | Single use; a repeated exchange uses a new OIDC token |

The public session ID is not authentication. The recorded claims, unique ref and atomic
run binding prevent a job from claiming another role's session. The runner token serves
subsequent polling, results, heartbeats and shutdown; the job need not repeatedly mint
OIDC tokens during a long turn.

## Keys, budgets and revocation

The exchange records a key before creating it in agentgateway and waits for activation
before returning it. A repeated exchange for the same bound session adopts the existing
key rather than granting a fresh budget. The configured `gateway.budget` is a **token**
budget, not dollars: `Gateway.create` writes a `Tokens` budget and model allow-list.
The current key record does not configure per-key rate limits.

Budgets are spending controls, not strict concurrent-request ceilings: charging happens
after responses, so in-flight requests can overshoot. Model-call and turn limits remain
separate controller controls. Session keys are bearer credentials and must not appear
in logs, artifacts, command text or retained transcripts.

The gateway key has no native expiry. The exchange enforces the session deadline and
heartbeat lifetime by deletion:

- End the session durably before deleting its key. A failed deletion cannot revive it.
- Reconcile every minute, retiring expired/dead sessions and deleting unowned gateway keys.
- Retry deletion after failure or restart. A controller ownership change ends the old
  role sessions; their results cannot authorize new work.

Revocation waits while the exchange is down, and deleting a key does not cancel an
already admitted inference request. These are deployment limitations, not automatic
credential expiry. The gateway instance's virtual-key inventory belongs to this exchange:
its orphan cleanup is not suitable for unrelated keys managed by another service.

## Turns and retained conversations

`RunnerExecutor` submits a turn with its request digest, task/role binding, snapshot,
deadline and optional retained transcript. Repeated submission returns the stored admission
or completed result; it does not queue another model call. A conflicting result is refused.
Unknown submission outcomes follow bounded recovery, not blind redispatch.

New turn admission commits the turn, any newly created staffing session and its staff
assignment in one SQLite transaction under the lifecycle lock. Dispatch is returned only
after commit; a lost reply reuses that committed admission. Retirement likewise commits
the session's ended state and loss of its running and pinned queued turns together, one
session per transaction. Gateway calls and key forgetting occur afterwards, outside the
transaction, so failed key deletion cannot revive work. These transactions do not repair
older partial admissions or retirements; older retirement gaps still recover through polling.
Other exchange writes use autocommit, so their statement order remains significant.

The lifecycle lock is non-reentrant and spans threads and processes. Long polls release
it between observations; schema setup and CLI listing reads run outside it. Gateway I/O
remains under the lifecycle lock but outside SQLite transactions. The service registry
separately uses its own explicit transactions and exclusive state-directory writer lock.

Each job serves one turn at a time. The runner prepares real Git history, runs the native
CLI, stops descendant processes, extracts implementation edits and returns the report.
The role job is the execution boundary: environment filtering and a pristine Git copy
are not protection against a hostile native process in that same job. The controller
independently validates publication; builds and reviewer experiments remain advisory.

Retained homes preserve provider transcripts and thread-state files between role jobs.
Regenerable Codex caches are pruned before packing; the retained gzip archive has a
24 MiB transport limit. No build cache or provider credential belongs in that archive.
The service stores it privately and restores it only into the matching conversation.
PR reviewers retain separate conversations across repair rounds; changed heads still
need new reviews. Authority changes, a different PR and design reopens rotate those
conversations. Fresh design reviews do not reuse iterative transcripts. Existing tasks
without PR transcripts start them on their next review call; completed votes stay valid.

Claude receives the session key through `ANTHROPIC_AUTH_TOKEN`; Codex uses a custom
Responses provider with an environment-key reference. Both receive explicit model/effort
settings on fresh and resumed calls. Client user configuration, hooks and nonessential
telemetry are disabled by `engine/agent_entry.py`. The pinned CLI versions and invocation
flags in that file and `agent.yml` define supported behavior, not generic SDK claims.

## Operational boundaries

The fork's `CONCURRO_GATEWAY_URL` secret must equal the service's `gateway.url`. Secret
storage masks routine workflow logs; the public endpoint still requires authentication.
Never pass provider credentials, the App key or the exchange operator token to jobs.

Gateway and exchange databases are persistent; they are not disposable runner caches.
Back them up with the service registry according to the host's recovery procedure. An
inference outage stops model progress; it does not grant runners a direct-provider fallback.

MCP integration remains a [separate proposal](mcp-tools.md). An inference session key
must not be assumed to authorize MCP administration or arbitrary tool traffic.
