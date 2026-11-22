# S5: Rust inference gateway

Part of the [phase 5 plan](../phase-5-plan.md). The gateway is deployed inactive and
takes traffic at S7. It replaces agentgateway (`gateway/agentgateway.yaml`) and the
exchange's forwarder hooks (`gateway/exchange/inference.py`: `/start`, `/resolve` and
`/<route>/`).

## Shape

**A separate, stateless process.** Every decision comes from the exchange's admission
API, and every secret from OpenBao. A restart drops only the streams in flight, which
the runner's harness retries. It needs no readiness rebuild after event loss, because
it holds no binding state.

**Crate `crates/inference-gateway`,** with these modules:

| Module | Does |
| ---- | ---- |
| `listener` | `axum` on `hyper`, `/v1/*` |
| `keys` | Reads the session key from `authorization: Bearer` or `x-api-key`, as the harnesses send it |
| `admission` | Uses `exchange-client` |
| `routes` | The route table |
| `credentials` | `concurro-secrets`: org credentials cached and refreshed; personal versions read per admission, cached in memory for the session's deadline at most |
| `forward` | Header policy, streaming passthrough, cancellation |
| `usage` | A port of `gateway/provider_protocol.py`: Responses and Messages, streaming and non-streaming |
| `errors` | Today's category map and sanitized error bodies |
| `relay` | Verifier dispatch with the claim |

## Request flow

1. **Read the key and model, and enforce the 48 MiB body limit.** `GET /v1/models`
   admits with `probe: true` and answers from its `models`, without a provider call or a
   request record. That keeps today's probe behavior.
2. **Admit.** The gateway calls `POST /internal/v1/requests`. A refusal returns today's
   status with `{"error":{"type":"unknown","message":"private request refused"}}`.
3. **Pick the route from the admission's credential:**
   - Org: the route the grant froze, chosen by the task application from target
     configuration. Today a client header (`x-concurro-plan`) picks it.
   - Secret store: the exact version at `personal/<record_id>`.
   - Relay: the verifier.
4. **Forward:**
   - Strip hop-by-hop headers, `origin`, `x-concurro-*`, `x-internal-auth` and any
     client `authorization`, `x-api-key` and `chatgpt-account-id`.
   - Set `accept-encoding: identity` and inject the route's credentials
     ([table](#routes)).
   - Stream the response through unchanged, tapping events for usage and errors. An
     event over 8 MiB, or one that can't be decoded, marks the request as having
     ambiguous usage, as today.
5. **Finish.** Report the category, usage or the reason it's unavailable, and
   `Retry-After` to `POST /internal/v1/requests/{id}/finish`.
   - If the exchange is unreachable, retry in memory with backoff for up to five
     minutes.
   - A request whose finish is lost meanwhile is closed by the exchange as
     `not_reported` at its settlement deadline
     ([exchange](s4-exchange.md#admission-api-for-the-gateway)).

## Routes

| Route | Upstream | Credentials injected |
| ---- | ---- | ---- |
| Org ChatGPT subscription | `https://chatgpt.com/backend-api/codex` | `Bearer` org access token, `chatgpt-account-id` |
| Org OpenAI API key | `https://api.openai.com/v1` | `Bearer` org key |
| Org Claude OAuth | `https://api.anthropic.com/v1` | `Bearer` org token, `anthropic-beta` with `oauth-2025-04-20` appended; no `x-api-key` |
| `openai-api-key` | `https://api.openai.com/v1` | `Bearer` from the version |
| `chatgpt-subscription` | `https://chatgpt.com/backend-api/codex` | `Bearer` and `chatgpt-account-id` from the version's `account` |
| `anthropic-api-key` | `https://api.anthropic.com/v1` | `x-api-key`; no `authorization` |
| `claude-subscription` | `https://api.anthropic.com/v1` | `Bearer`, `anthropic-beta` joined with `oauth-2025-04-20` |
| `claude-subscription`, relay | Verifier `/dispatch/v2/messages` | `Authorization: Bearer <inference credential>`, `x-concurro-claim`, `x-concurro-request` |
| `chatgpt-subscription`, relay | Verifier `/dispatch/v2/responses` | Same |

- Upstream base URLs are settings, so tests point them at fakes.
- **Codex WebSocket** keeps reaching the gateway through `responses-bridge`, which turns
  each turn into a streaming `POST /v1/responses` with the session key.

## Cancellation and revocation

- **The upstream request is cancelled** when the runner disconnects, when the request
  reaches the admission's deadline, and when the session leaves the live set.
- **The live set** comes from `POST /internal/v1/sessions/status` every 10 seconds, with
  a five-second timeout, for the sessions with open streams.
- **The check fails closed.** If no check for a session has succeeded for 30 seconds,
  during an exchange outage for example, its streams are closed. The runner's harness
  retries, and admission refuses new requests while the exchange is down anyway.
- **Spending stops** within 40 seconds of the exchange's revocation in the worst case
  (30 seconds without a successful check plus one interval), and usually within 10.
  Add the provider's own response to the closed connection.
- **New requests are refused at once,** because admission is checked per request.

## Credentials and rotation

- **Org credentials** come from `ops/gateway/providers` and are refreshed every 60
  seconds. After a provider `401` the gateway refreshes them once at once, and retries
  the request if the value changed.
- **Personal versions** are read when admitted. A version destroyed in OpenBao becomes
  the `auth` category, as today's resolve failure does.
- **No value appears in logs or errors.** Tests check this with known values.

## Usage

- The port keeps `tokens()` and its rules: nonnegative integer `input_tokens` and
  `output_tokens`.
- It also records `cache_read_input_tokens` and `cache_creation_input_tokens` where the
  provider reports them. Today these are dropped.
- **Unavailable usage** carries a reason: `ambiguous`, `incomplete`, `provider_error`,
  `unparsed_shape` or `not_reported`.
- Org requests are recorded like contributed ones.

## Settings

| Setting | Meaning |
| ---- | ---- |
| `listen` | Inference listener |
| `exchange_admission_url` | The exchange's admission API |
| `admission_bearer` | `SecretRef` |
| `openbao` | URL, AppRole role ID, secret ID file |
| `upstreams` | Base URL per provider |
| `verifier_url` | The verifier's private API |
| `verifier_inference_credential` | `SecretRef` |
| `limits` | Body limit, event limit, concurrency |

## Acceptance

- **Every route,** against fake upstreams replaying recorded streams:
  - the right upstream and credentials;
  - stripped client headers;
  - streamed output byte for byte;
  - usage extracted, or unavailable with its reason;
  - errors sanitized per category, with `Retry-After` kept.
- **Relay,** against the S1 verifier with a fake native worker: the claim and inference
  credential are accepted, and a revoked claim is refused.
- **Cancellation:**
  - a runner disconnect closes the upstream;
  - a session revoked mid-stream closes it within one status interval;
  - a request past its deadline closes.
- **Budget:** once the exchange reports the budget spent, the next request is refused,
  and the turn ends as `budget_denied`.
- **Rotation:** an operator patch of the org ChatGPT token is used within 60 seconds,
  and at once after a provider `401`.
- **`responses-bridge`:** a Codex WebSocket turn, with cancel, reaches the gateway and
  returns its events.
- **Exchange outage:** with session checks failing, an open stream is closed within 40
  seconds, and new requests are refused.
- **Dispatch evidence:** a destroyed version reports `dispatched = false, category = auth`;
  a provider `401` reports `dispatched = true, category = auth`.
- **Restart:** a gateway restart mid-stream leaves the request closed as `not_reported`
  at its settlement deadline, and later requests admit normally.
