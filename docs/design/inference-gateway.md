# Inference gateway

Status: accepted (reviewed); proven locally and on a GitHub-hosted runner. Promotes the [service design](service.md)'s inference proxy and
proxied API keys to the default for every model call, wherever the agent runs.
Revises the credential parts of [the runner executor](runner-executor.md) and
[the agent sandbox](agent-sandbox.md).

## Problem

Agent containers hold provider credentials: an API key or a subscription token,
in the environment of the container that runs the model and its tools. That
drives most of the hardening we have designed or built:

- gVisor and host firewall rules for runner sandboxes;
- per-session credential delivery and its accepted "exposure risk";
- on the service host, a separate tool container, so that model-driven commands
  cannot read the agent container's credential.

Each of these restricts or complicates what agents can do. The tool container
also splits tools into two paths: repository tools behind it, and anything else,
such as an MCP server we give the agent, beside it.

## Decisions

| Question | Decision |
| ---- | ---- |
| Where do provider credentials live? | Only in a gateway on the Concurro server. No agent container, runner or host process outside the gateway holds one |
| What does an agent hold? | A gateway key bound to one session: an expiry, a dollar budget, a model allow-list and rate limits, revoked when the session ends |
| Which gateway? | agentgateway (upstream, Apache-2.0, Linux Foundation); Bifrost, then LiteLLM, only if it fails a concrete requirement |
| Which protocols? | Native ones: Anthropic Messages for Claude Code, OpenAI Responses for Codex. Copilot's bring-your-own-key mode speaks either. No translation into a common chat format |
| How does a runner get its key? | Its job's GitHub OIDC token goes to Concurro's executor endpoint, which checks the claims and the session and mints the key |
| How does a service-host agent get its key? | The controller mints it directly; it needs no OIDC |
| Subscriptions | Gateway-side only, never on a runner: the gateway injects the Claude or ChatGPT subscription credential, proven for both (see [Subscriptions](#subscriptions)) |
| Tool container | Retired from the agent path. Agents use their CLIs' native tools and any MCP servers we provide, in one container |

## Gateway

The proof runs on **agentgateway**, evaluated as its upstream project: Apache-2.0
and hosted by the Linux Foundation, whose charter requires open-source releases
([charter](https://github.com/agentgateway/agentgateway/blob/main/CHARTER.md)).
Commercial distributions exist; we rely only on upstream. The selection rule:
everything we need must be in upstream open source, a missing convenience is
acceptable, and rebuilding substantial gateway functionality is not.

What upstream provides, and what Concurro adds:

| Need | agentgateway upstream | Concurro adds |
| ---- | ---- | ---- |
| Claude Code | Anthropic Messages to an Anthropic backend is "sent natively, with no conversion"; token counting and model listing are served | Proof: `anthropic-beta` passing through unchanged, streaming and thinking on the native path |
| Codex | An OpenAI Responses route, and a passthrough route that forwards "without transformation" | Proof: which route serves Codex faithfully; `supports_websockets = false` |
| Session keys | Virtual keys created and deleted through the admin config API (`llm.apiKey`), with per-key model allow-lists (403 otherwise) and dollar or token budgets that block when spent, kept in SQLite | **Expiry**: none is documented, so Concurro enforces deadlines by deleting keys (see [Keys](#keys)) |
| Budgets | Per-key budgets charge usage after each response, so concurrent requests can exceed the nominal limit even on one instance. `tokenize: true` estimates request tokens for rate limiting, not a spending ceiling | A spending control, not a guaranteed cap: the proof measures concurrent overshoot. No reservation system for the pilot |
| Client authentication | API keys, JWT against a JWKS, external authorization and CEL policies, all open source | Nothing for the proof |
| Accounting | Token usage per request and key; dollars when the model is in its cost catalog | Prices for the models we route |

The admin API has no authentication. The gateway and the exchange service share
one network namespace (the exchange container joins the gateway's, e.g. Docker's
`--network container:<gateway>`), and the admin listener binds to its loopback.
Only the inference listener is published to the reverse proxy; agent containers
and public ingress cannot reach the admin port, which the proof checks. New keys
reach the traffic path asynchronously, so the exchange waits until the gateway
accepts a key before handing it out. In-flight requests finish after a key is
deleted.

**Fallbacks**, only if agentgateway fails a concrete requirement:

- **Bifrost** (Apache-2.0 core, open-core product). `/anthropic_passthrough` and
  `/openai_passthrough` forward provider-native paths and bodies with streaming,
  inject the provider key centrally, and apply virtual-key model restrictions
  ([passthrough](https://docs.getbifrost.ai/integrations/passthrough)); its
  virtual keys carry expiry, budgets and rate limits
  ([virtual keys](https://docs.getbifrost.ai/features/governance/virtual-keys)).
- **LiteLLM** (MIT core, open-core product). Native Messages and Responses with a
  published Claude Code matrix, and key minting with expiry
  ([virtual keys](https://docs.litellm.ai/docs/proxy/virtual_keys)). Its release
  pipeline was compromised on 2026-10-21, publishing malicious `v1.107.0`
  packages and Docker Hub and GHCR images for about two hours
  ([investigation](https://docs.litellm.ai/blog/security-incident-oct-21-2026-attack-chain)),
  so adopting it would need scrutiny proportionate to holding every provider key.

Others were set aside for this proof, not dismissed: Kong's native routes are in
its paid tier, Helicone's gateway is in maintenance, TensorZero is archived, and
Portkey's Gateway 2.0, which moves enterprise features into open source, is still
a pre-release.

Whichever runs, it gets its own container and only the provider credentials it
needs; it never sees the controller's GitHub App key or the registry. The gateway
is reachable over public HTTPS (or a tailnet later). Its reverse proxy must not
buffer server-sent events and must allow long idle streams.

On the pilot host the gateway publishes no ports. The host's shared edge Caddy
(`gateway/edge`) owns 443 with the host's Let's Encrypt IP certificate and imports
one route file per service; Concurro's (`gateway/concurro.caddy`) serves everything
under `/concurro/`, so clients use `https://<host>/concurro` as the base URL.
`gateway/README.md` records the host layout and operator commands.

## Keys

Concurro's exchange is the only minter. It reaches the gateway's admin API only
over the shared loopback described above; that API has no credential of its own.

**Runner sessions.** The runner job's host step requests an OIDC token with
Concurro's audience and presents it with its session ID. The endpoint checks,
against GitHub's JWKS:

| Claim | Required value |
| ---- | ---- |
| `iss`, signature, `aud`, `exp`/`nbf`/`iat` | GitHub's issuer and keys (RS256); Concurro's audience; fresh |
| `repository_id`, `repository_owner_id` | The session's target |
| `job_workflow_ref`, `job_workflow_sha` | Concurro's reusable agent workflow at the pinned engine commit |
| `event_name`, `ref` | `repository_dispatch` on the default branch |
| `runner_environment` | `github-hosted` |
| `actor_id` | The Concurro App's bot user |
| `run_id`, `run_attempt` | Bound to the session at its first presentation, at attempt 1 only |
| `jti` | Not seen before |

The session ID in the dispatch payload is not a secret: every App with Contents
read on the target receives it. The binding rests on these claims and on the
session accepting exactly one run, as the runner executor already specifies.

The minted key carries the role's allowed models, the session's budget, and
metadata naming the target, issue, session and run. The gateway enforces no
expiry, so Concurro does:

- **Durable record.** Before a key is handed out, the registry records it with
  its session and deadline (the session lease; GitHub-hosted jobs stop at
  6 hours).
- **Deletion.** The exchange deletes the key when the session ends, loses its
  heartbeat or changes ownership.
- **Reconciliation.** On startup and periodically, Concurro deletes every
  recorded key past its deadline and every gateway key it has no record of.
- **One allowance per session.** Re-minting a key for the same session carries
  over the remaining budget, never a fresh one.

While Concurro is down, revocation waits for it to return: an accepted pilot
trade-off, not gateway-enforced expiry.

**Service-host sessions.** The controller mints the same kind of key per model
call or session, records it the same way, and deletes it afterwards.

**In the agent container.** The key is the only secret. Each CLI reads it from a
file the host step owns and can replace, so a key can be re-minted without
restarting the agent:

| Client | Endpoint | Credential |
| ---- | ---- | ---- |
| Claude Code, Claude Agent SDK | `ANTHROPIC_BASE_URL` at the gateway's Anthropic route | `apiKeyHelper` reading the file |
| Codex | `[model_providers.concurro]` at the gateway's OpenAI route, with `wire_api = "responses"` and `supports_websockets = false` | `[model_providers.concurro.auth] command` reading the file |
| Copilot (bring your own key) | `COPILOT_PROVIDER_BASE_URL`, with `TYPE` and `WIRE_API` | `COPILOT_PROVIDER_API_KEY_COMMAND` reading the file |

Which header carries the session key (`Authorization`, `x-api-key`, or a
gateway-specific header) is set per gateway; the proof fixes it.

Non-model traffic is switched off: `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`
and `skipWebFetchPreflight`, Codex's analytics and update checks, and
`COPILOT_OFFLINE=true`. Claude Code's fast-mode check still calls
`api.anthropic.com`; it carries no credential.

## Subscriptions

Technically, a subscription is a bearer credential like any other, so it can
live in the gateway:

- **Codex.** A ChatGPT login is a bearer token plus a `chatgpt-account-id` header
  to `chatgpt.com/backend-api/codex`, which the gateway injects.
- **Claude.** A subscription is an OAuth bearer plus an OAuth `anthropic-beta`
  value, which the gateway injects alongside the client's own betas.

Both work server-side; see the proof results below.

## Consequences

- **Runners hold no provider or GitHub write credential.** A compromised job can
  spend its session's capped budget and submit a bad diff, which the gates and
  reviews already handle. gVisor and the custom firewall rules are dropped from
  the runner plan. Kept: ordinary hardened containers, nothing from the job's
  environment passed in, network-disabled trusted checks bound to the exact
  candidate digest, and `cache-mode: read`.
- **No provider key on the service host outside the gateway.** The tool
  container's purpose was keeping model-driven commands away from the agent
  container's credential. With only a capped key there, agents read the snapshot
  with their CLIs' native tools, and MCP servers run beside them in the same
  container. The tool container code stays for a future need, unused.
- **One credential path everywhere**, with usage and spend per session in one
  place.
- **The gateway is critical.** Its availability bounds every model call; a
  runner job without it fails like a provider outage.

## Turns

Every model call is a **turn**: the agent request, the snapshot spec (pinned
refs and any candidate files) and the role's retained transcript as an archive.
A turn runs the same way wherever it runs (`engine/turns.py`):

1. A workspace is built from the spec on the target's Git mirror: real history,
   the base and head under their branch names, the candidate committed on the
   head. A copy of its `.git` is kept outside the agent's reach.
2. The agent container gets the workspace, scratch space, the mirror read-only,
   the transcript, and the gateway key as its only secret. It runs as an
   unprivileged user with no capabilities, with network, and the CLIs use their
   native tools with their own sandboxes off: the container is the sandbox.
3. For `develop`, the controller takes the files the agent changed, found with
   the kept `.git` copy so nothing the agent wrote under `.git` takes part;
   submodules are never entered, since Git would read their configuration. The
   implementer no longer returns file contents; policy, scope and budgets apply
   to the extracted edits as before, and a protected or out-of-scope path comes
   back to it as feedback.

**Executors.** The service's `gateway.executor` setting picks one:

- **`runner`**: turns queue at the exchange for their target. When a target has
  no live runner session, the exchange issues one and the service dispatches
  `concurro-agent` to the target. The target's caller workflow runs
  `agent.yml`, which fetches Concurro at its own `job_workflow_sha`, builds the
  agent image on the Rust toolchain image and runs `engine/runner.py`. The
  runner trades its OIDC token for the session key and a runner token, then
  long-polls for turns (its heartbeat), runs up to two at once and ends the
  session after ten idle minutes. A session silent for three minutes, or not
  started within twenty, is ended and its running turns are lost (uncertain to
  the controller). A runner refused for a claim of the target's own repository
  is reported to the service, which blocks the task with the reason; refusals
  of other repositories' tokens are not, since anyone can present one. The
  service names each turn; submitting it again returns its first admission, so
  a lost reply never queues a second turn, and an admission that stays unknown
  is withdrawn and reported uncertain.
- **`host`**: the service mints a key per turn through the exchange's operator
  API and runs the turn on its own Docker, deleting the key afterwards. The
  Actions controller, the local pilot and evaluations use this path, configured
  by environment (`agents.host_agents`).

The exchange's operator API (`/exchange/operator/...`) takes the service's
operator token, so the service can run anywhere that reaches the gateway. Every
caller is authenticated before its request body is read; only turn submissions
and results may be large, and bodies held at once share a fixed memory budget.
Copilot has no gateway route yet, and a role assigned to it is refused.

**Agent shells on the service host.** The host executor gives agents the same
full native tools as runners, in the unprivileged container above. Its network is
not yet limited to the gateway, so a kernel escape or a reachable local service
is the residual risk on that host; runners are the default.

## Proof

One runner, one target, API keys, agentgateway:

1. agentgateway on the Concurro server in its own container, with Anthropic and
   OpenAI keys and nothing else.
2. The exchange in the executor endpoint, enforcing the claim table, minting and
   blocking keys.
3. A reusable workflow that runs Codex and Claude Code through the gateway's
   native routes on a multi-turn task, with the key in a file and no other secret.

Done when:

- both clients complete the task, with streaming, tool calls and multi-turn
  continuation working;
- the session key stops working when Concurro deletes it at session end, cannot
  select a model outside its allow-list, and is deleted by reconciliation after a
  simulated controller restart;
- usage is attributed to the session, its budget stops further requests, and the
  overshoot from concurrent requests is measured;
- no provider key reaches the runner, and the admin API is unreachable from agent
  containers and public ingress.

If agentgateway fails a concrete requirement, the same proof runs against
Bifrost, then LiteLLM.

## Changes to existing notes

- [Runner executor](runner-executor.md): "Credentials" becomes "a session
  gateway key, minted by the executor endpoint"; provider credentials leave the
  "What the runner holds" table; gVisor and the host firewall leave the session
  steps and rollout. Its claim checks and session rules stay.
- [Agent sandbox](agent-sandbox.md): the provider credential and its spend limit
  are replaced by the session key and its budget; gVisor and the firewall are no
  longer required.
- [Service design](service.md): the inference proxy is this gateway, and proxied
  API keys are the default credential type.
- [Open workspaces](open-workspaces.md): the service-host tool container and its
  `git` tool give way to native tools in the turn workspace (see [Turns](#turns)).

### Proof results: local phase (2026-11-04)

agentgateway v1.7.0 (`cr.agentgateway.dev/agentgateway:v1.7.0`, image
`sha256:60216aaf…`) in Docker with an OpenAI key, `passthrough: detect` models,
SQLite, and the admin API on loopback. The exchange ran in a container sharing
the gateway's network namespace; clients ran in Concurro's agent image.

| Check | Result |
| ---- | ---- |
| Mint a scoped key through the admin API | Works: Concurro supplies the key; `allowedModels` and `budgets` are fields of the record. Budgeted keys need `metadata.name`. Keys were active within a second |
| Codex on `/v1/responses` | Works: a multi-request turn with tool calls and streaming, and a resumed second turn answering from the first's context. Prompt caching survives (22,043 of 35,431 input tokens cached) |
| Usage accounting | The gateway's per-key total matched Codex's own report (49,114 tokens) |
| Model allow-list | A model outside it gets 403 `model_not_allowed` |
| Budget | Blocks with 429 `budget_exceeded` once exceeded. Twenty concurrent requests against 120 remaining tokens all passed, ending 80% over the limit: overshoot is bounded by what is in flight |
| Deletion | The next request gets 401; a second delete gets 404 |
| Admin isolation | The admin API is unreachable from an agent container and from the host |

Findings for the implementation:

- **Stats listener.** `statsAddr` defaults to all interfaces (port 15020) and is
  reachable from containers on the gateway's network. Its metrics carry only
  per-model aggregates, but it binds to loopback in production, and agent
  containers reach the gateway only through the published inference port.
- **Codex's model list.** Codex cannot decode the gateway's `/v1/models` (it
  expects its own catalog format) and falls back; harmless.
- **Codex's sandbox.** `bwrap` cannot create namespaces in an unprivileged
  container, so commands fail under `workspace-write`. Inside our container,
  Codex runs with its own sandbox off, as the agent sandbox note specifies.

**Claude Code through a server-held subscription token.** The `claude-*` model
forwards natively (`passthrough: detect`) and injects the token as
`Authorization: Bearer`, appending `oauth-2025-04-20` to the client's own
`anthropic-beta` value and removing the client's `x-api-key`. Claude Code, holding
only its session key, completed a four-turn task (test run, edit, rerun) with
cache reads of 45,686 tokens; the gateway charged the key exactly Claude Code's
reported total (69,432 tokens). Claude Code takes the session key from
`ANTHROPIC_AUTH_TOKEN`; an `apiKeyHelper` in user settings was ignored in
headless mode ("Not logged in"), which a session-long key does not need.

**Codex through a server-held ChatGPT subscription.** A second `gpt-*` model,
selected by a header Codex's provider config sends (`x-concurro-plan: chatgpt`,
removed before forwarding), uses `baseUrl: https://chatgpt.com/backend-api/codex`,
injects the ChatGPT access token as `Authorization: Bearer` and sets
`chatgpt-account-id`. Codex, holding only its session key, completed a
multi-request turn with caching (33,920 of 46,335 input tokens cached); the request
log records `endpoint: chatgpt.com:443`, and per-key usage again matched Codex's
report. The gateway also priced each request from its model catalog, so dollar
budgets work for these models.

**Subscription token lifetimes.** The ChatGPT access token lasts 10 days and
needs refreshing with its refresh token; `claude setup-token` issues a one-year
token. agentgateway reloads a credential given as a file when it changes, so a
small refresher that rewrites the file keeps a subscription current without a
restart.

### Proof results: runner phase (2026-11-04)

The gateway and exchange on the pilot host behind the shared edge
(`https://144.76.56.188/concurro`), deployed as in `gateway/README.md`. The fork's
`workflow_dispatch` caller ran `gateway-proof.yml@main` on a GitHub-hosted runner
([run 41820856875](https://github.com/cylewitruk/stacks-core-autobot/actions/runs/41820856875)),
for a session issued with the claim table pinned to Concurro's `main` commit.

| Check | Result |
| ---- | ---- |
| OIDC exchange | The job's token, checked against the claim table, returned the session key; nothing else crossed to the runner |
| Claude Code (Claude subscription) | Fixed the bug in six turns; 69,000 of 92,678 input tokens read from cache. Tests pass in a fresh offline container |
| Codex (ChatGPT subscription) | Fixed the bug with tool calls and streaming, then answered a resumed second turn from the first's context; 33,024 of 44,354 and 44,288 of 55,762 input tokens cached. Tests pass offline |
| No credential in agent containers | No `ACTIONS_*`, `GITHUB_TOKEN` or provider key in their environment |
| Session end | The job ended its session with a fresh OIDC token; the key then got 401 |

With the local phase this covers every "done when" item above.

## Later: MCP and agent-to-agent traffic

The same gateway can carry more than inference, which matters for agents with
MCP servers and for cooperative review:

- **MCP.** It federates several MCP servers behind one endpoint, over stdio,
  HTTP/SSE and Streamable HTTP, with per-client tool filtering and authorization.
  MCP servers we give agents can sit behind it, authorized by the same session,
  instead of each agent container wiring them up itself.
- **A2A.** It proxies agent-to-agent JSON-RPC traffic to backend agents, rewrites
  agent cards to point at the gateway, and logs task states
  ([A2A](https://agentgateway.dev/docs/standalone/latest/documentation/agent/a2a/)).
  It routes, secures and observes; it does not orchestrate. Our agents are
  headless CLI runs, not A2A servers, so using A2A for cooperative review means
  wrapping each participant as an A2A agent, with Concurro still deciding who
  talks to whom.

Neither is needed for the proof.

## Open questions

- Whether the chosen gateway stays faithful to new Claude Code betas and Codex
  Responses features at our upgrade pace.
- Where the Concurro server runs for the proof (it must be reachable from GitHub's
  runners), and whether a tailnet replaces public exposure later.
