# Org inference gateway

Phase 5 S4c supplies the org-funded HTTP route. It remains inactive until S7.
The runner sends its derived session key; the gateway asks the exchange's
private admission listener to authorize each request and select the grant's
frozen route. It injects only the configured org credential, streams provider
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

[openbao]
url = "http://openbao:8200"
role_id = "<gateway-role-id>"
secret_id_file = "/run/secrets/gateway-openbao-secret-id"
```

Routes may also use `auth = "x-api-key"` for an Anthropic API key. The
upstream base URL is configurable for fixture servers. Token and account
values stay in file or OpenBao references; no client credential header is
forwarded. A finish report is retried for up to five minutes after a temporary
exchange error. Personal secret-store and relay routes are completed in S4d.
