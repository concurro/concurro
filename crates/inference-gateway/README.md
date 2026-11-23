# Inference gateway

Phase 5 S5 supplies org, contributed and relay HTTP routes. It remains inactive until S7.
The runner sends its derived session key; the gateway asks the exchange's
private admission listener to authorize each request and select the grant's
frozen route. It injects the frozen org or personal credential, streams provider
bytes, reports request usage, and checks live sessions every ten seconds while
a stream is open. Revoked or expired sessions close their streams; an exchange
outage closes them after 30 seconds without a successful check.

Set `CONCURRO_INFERENCE_GATEWAY_CONFIG` to a TOML file:

```toml
listen = "127.0.0.1:8098"
exchange_admission_url = "http://127.0.0.1:8097"
admission_bearer_file = "/run/secrets/exchange-admission-bearer"

[routes.claude_org]
upstream = "https://api.anthropic.com/v1"
token = "openbao:ops/gateway/providers#claude_token"
auth = "bearer"
anthropic_beta = "oauth-2025-04-20"

[routes.openai_org]
upstream = "https://api.openai.com/v1"
token = "openbao:ops/gateway/providers#openai_key"
auth = "bearer"

[routes.chatgpt_org]
upstream = "https://chatgpt.com/backend-api/codex"
token = "openbao:ops/gateway/providers#chatgpt_token"
auth = "bearer"
account = "openbao:ops/gateway/providers#chatgpt_account"

[personal_routes.anthropic-api-key]
upstream = "https://api.anthropic.com/v1"

[personal_routes.claude-subscription]
upstream = "https://api.anthropic.com/v1"
anthropic_beta = "oauth-2025-04-20"

[personal_routes.openai-api-key]
upstream = "https://api.openai.com/v1"

[personal_routes.chatgpt-subscription]
upstream = "https://chatgpt.com/backend-api/codex"

[relay]
verifier = "http://relay-verifier:7450"
credential = "openbao:ops/gateway/relay#inference_bearer"

[openbao]
url = "http://openbao:8200"
role_id = "<gateway-role-id>"
secret_id_file = "/run/secrets/gateway-openbao-secret-id"
```

Routes may also use `auth = "x-api-key"` for an Anthropic API key. The
upstream base URL is configurable for fixture servers. Token and account
values stay in file or OpenBao references; no client credential header is
forwarded. A finish report is retried for up to five minutes after a temporary
exchange error. Personal routes read only the admitted immutable
`personal/<record_id>` version in OpenBao, using its `secret` field and the
ChatGPT `account` field when needed. Relay routes send the exchange's signed
claim and one stable request ID to the S1 verifier. A verifier
`request_reused` refusal is reported for that same ID without another dispatch.
Org values are cached for at most the OpenBao refresh interval (60 seconds in
the example). A provider 401 triggers one fresh read of the token and account
from the same OpenBao secret; the admitted request is retried once only when
that material changed. A second 401 is reported as a provider authentication
failure. Personal versions and relay claims are never retried this way.

The pinned S1 `responses-bridge` takes `--gateway` pointing to this service's
HTTP listener. It carries Codex WebSocket `response.create` calls into
`POST /v1/responses`, and closing or cancelling the WebSocket drops the active
gateway stream. S7 routes the WebSocket edge path to the bridge.

The inactive Compose example mounts a gateway-only settings directory. It must
contain only the gateway's admission bearer, route references and OpenBao AppRole
secret ID; exchange operator, runner-token and signing material belong in the
separate exchange-only directory.
