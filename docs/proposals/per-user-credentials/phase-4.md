# Phase 4: contributed inference and credential pauses

Phase 4 wires frozen phase-3 admissions into the pinned native gateway. Organization
funding remains the default. A task explicitly configured with `funding_mode: contributed`
uses the exact admitted owner, route, record and version; it has no organization fallback.
No deployment or live-account validation was performed. Subscription registration still
returns 404 until phase-5 validators are configured; subscription-shaped routes below are
transport implementations tested with fake material, not claims of backend acceptance.

## Request and observation authority

1. A named-gateway HTTP authorization hook runs **before** the native LLM API-key/budget
   policy. Exchange checks the supplied virtual key against its exact live session and sole
   running admitted turn. It persists a random request UUID with immutable session, turn
   and full funding binding. Incoming payer, route, account and nonce headers are not authority.
2. Native strict API-key/model/token-budget enforcement remains in place. GET model-list
   and activation probes have no turn snapshot, secret lookup, provider request or task pause.
3. For contributed inference, native extAuthz resolves the frozen exact version anew, using
   the vault's separate gateway principal. A post-I/O authority check refuses revoke races.
   Private resolver response **headers** supply token/account/route metadata; no response-body
   CEL expression buffers streaming. Each nonce resolves once and is claimed once by the hop.
4. Four explicit native adapters select only from the trusted route header. Organization
   routes require trusted org funding; their existing plan selector and credentials remain.
   A narrow loopback observer forwards fixed upstream URLs, strips private/account spoofing,
   sanitizes every non-null error shape and SSE error event, and observes bounded JSON/SSE with backpressure and cancellation. It forces identity encoding rather than parsing compressed bytes.
5. `inference_requests` and `inference_observations` live in the existing Exchange database.
   The latter is an append-only sanitized outbox, unique on request/source, independent of
   finished-turn/session cleanup. It retains frozen identity, token counts when known,
   source/completion, selected model, fixed category and numeric retry delay; never tokens, error messages or prompts. Native request completion alone does not establish provider success.

Native budget checks precede the resolver/hop. The native access-log database therefore
records the overwritten start nonce plus a boolean derived from **native-only** `proxy.error`
(`RateLimit`/`Budget exceeded` on the pinned image). A provider429/body cannot forge this
context. Exchange imports exact request completions idempotently; it never guesses by model,
timestamps or the session's later active turn. Missing usage remains unknown.

Native logs flush asynchronously. A generic failed result/replay whose request evidence has
not arrived returns503 and uses existing transport backoff; the task's observations endpoint
reports pending and preflight raises the existing uncharged transient-unavailable outcome.
Healthy in-flight requests do not block unrelated roster corrections. An outage/restart cannot authorize a replacement paid attempt for terminal unresolved work. Once exact completion arrives,
a known native budget denial wins before generic failure. A result already durably accepted as successful before pause/retirement remains recoverable, and its denial pauses subsequent admission; cancelled or revoked work is never revived. A completed unknown retains existing
unknown/session handling. If logs were lost, evidence remains pending/unknown and requires
operator repair; no quota, zero usage or successful completion is invented.

## Pauses, consent and batches

Before a charge, service preflight checks every configured required role, its current grant,
writer permission and exact route/model/version coverage across all owners. The current
pipeline has no optional evaluator; `design_eval` remains a separate operator diagnostic.
Missing sponsor, adapter, token or model coverage uses `awaiting_credentials`; native key
budget denial uses `budget_exhausted`. Both reuse suspended-stage writer retry/resume and successful writer reassessment,
preserve original resume target, completed work, approvals and spending, and never choose
another payer. Trusted auth/quota/rate/budget conditions bypass `SessionFailure` paid restarts. A durable pause latch and all drained failures’ acknowledged turn IDs prevent crash/replay or late same-turn events from demanding a second writer retry. Exact failed-session retirement is checkpointed before cleanup and retried idempotently before replacement admission, including after writer retry and lost replies. Checkpoints retain only the cursor and latest failure; the Exchange outbox owns full history.

The shared review coordinator cancels the failed handle, drains other already-admitted
work under each handle’s exact authority and retains/applies saved peer verdicts. Sharing
a provider does not cancel a different owner/epoch or an independent session budget;
the peer’s own Exchange cutoff or failure receipt stops affected work. A trusted condition discovered while reserving a correction enters the same drain path. It admits no corrections,
retries or new roster members while draining. Ownership loss, stop or a terminal verdict
still cancels the whole remaining batch. Task-local withdrawal/global credential revocation
retain phase-3 provider/owner isolation and do not retire organization-paid sessions.

API structured classification is deliberately route-specific. OpenAI API recognized codes
include legacy `invalid_api_key`/`insufficient_quota`, current credit/spend/usage-limit codes, `rate_limit_exceeded` and `slow_down`;
Anthropic API `authentication_error` is recognized with Origin stripped by the server hop.
Anthropic `rate_limit_error` also covers spend limits, so its category stays unknown.
Both subscription backends remain unknown from provider response bodies; exact-version
vault404/410 supplies trusted auth evidence. HTTP429 and message text never distinguish
quota from rate. Numeric Retry-After is retained only with a verified rate category and
never schedules an automatic paid retry.

Sources: [OpenAI error codes](https://developers.openai.com/api/docs/guides/error-codes),
[Responses error schema](https://developers.openai.com/api/reference/resources/responses/methods/retrieve),
[OpenAI-maintained legacy troubleshooting vocabulary](https://github.com/openai/openai-developers-for-claude/blob/main/plugins/openai-developers/skills/openai-api-troubleshooting/SKILL.md),
and [Anthropic API errors](https://platform.claude.com/docs/en/api/errors). These API
contracts do not establish a subscription error taxonomy.

## Configuration, upgrade and portal

Use the updated `gateway/agentgateway.yaml`, Exchange image and Compose together. The new
private listener is `EXCHANGE_PRIVATE_LISTEN=127.0.0.1:8081`; it has no Caddy/public route.
It authenticates start with the real virtual key, resolve with the unguessable native-only
nonce plus authenticated native API-key session, and observation with the once-claimed nonce.
The vault metadata principal still cannot resolve; `EXCHANGE_VAULT_EVENTS_URL/TOKEN` is the
existing separate private gateway principal used for events **and** exact resolution.

Native logging writes `/data/requests.db`, with payload capture disabled and raw error
fields removed. Exchange reads it via `EXCHANGE_NATIVE_LOG=/gateway-data/requests.db` and a
read-only gateway-data mount. Its private HTTP server shares existing concurrency/body
budgets. Startup without vault configuration remains org-only; no personal material is
provisioned automatically. Private JSON hook replies close their HTTP connection explicitly; this adds a local connection per hook and avoids stale pooled reuse after the handler's idle timeout. The new local start-hook dependency also applies to org traffic;
if Exchange is unavailable, native traffic fails closed rather than bypassing bookkeeping.

Exchange adds tables in place; existing rows, checkpoint format 9, grant cutoffs, receipts,
org keys and audit history are preserved. Deploy only as a coordinated roll-forward release.
Do not delete either Exchange or native request-log data to resolve pending evidence.
The gateway database is telemetry input; the Exchange outbox remains the durable record.
The importer uses SQLite append-row identity plus native file identity, committing its cursor and observations together in one lifecycle transaction. Outbox task/turn indexes avoid whole-history scans under the lifecycle lock. Request-start reads only the small admitted model column; task-scoped pending queries use indexed repository/issue columns. Native-file replacement resets the import cursor; lost historical logs remain unknown. Active upstream sockets are bound to existing session retirement and the admitted deadline; a silent stream gets no new 60-second read limit, and turn cancellation, heartbeat expiry and stop/authority loss interrupt matching sockets.

The portal shows configured task funding, sanitized gateway evidence, coverage and the two
new pause names with writer retry controls. Replacing/revoking a personal version affects
its contributed sessions, not organization sessions. Mock and live contracts match; light
and dark layouts are verified. No automatic refresh/recovery or subscription token import
is enabled here. Real subscription account/header/transfer checks remain user-deferred.

## Isolated evidence and reproduction

`gateway/tests/phase4_spike.py` reuses the phase-0 Docker, fake provider and native command
fixtures, but loads **current production configuration and private adapter code**. Only
local transport endpoints, data paths and poison organization material are substituted.
The image is `cr.agentgateway.dev/agentgateway:v1.7.0` pinned to arm64 Linux digest
`sha256:60216aafb03d37f94e952ff3d51d2f630a65b10ecaebdc0a864882f92610c45b`.
Source commit: `26f5aebe99a36a037fe54f8080a608aca5547e15`. Native pins are Codex 0.163.0
and Claude Code 2.1.301 from `engine/agent-clients.txt`.

```sh
python3 gateway/tests/phase4_spike.py --output /tmp/concurro-phase4-NEW \
  --clients /path/to/pinned/npm/node_modules/.bin
```

The output root must not exist; `--case NAME` runs one explicitly named diagnostic. Docker is disposable with loopback-published ports; no real
provider or login configuration is read. Raw journals, stdout/stderr, request DB/WAL, source hashes captured before the run (and checked unchanged afterward),
CLI outputs and hashes are retained. Secret scans include a positive negative-control and
require actual request-log rows with payload count0. The fake response taxonomy proves
sanitization and route plumbing only. Local unit/integration checks are not remote release
CI, image-build validation, deployment or real subscription acceptance.

The read-only shared-WAL acceptance uses UID 65532 with a temporary Linux tmpfs-backed
Docker named volume. A normal named-volume run reported disk full in the local Docker
runtime; that negative evidence is retained and no unrelated data was removed. The tmpfs
run proves UID/read-only/WAL compatibility, not persistent-volume capacity or crash durability.
Exchange restart/replay tests separately verify durable request identity and cursor logic.
