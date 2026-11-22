# Exchange operator service

Phase 5 S4a serves the private operator API and persists grants, turns, sessions,
retirement fences and status snapshots in PostgreSQL. Terminal turns older than one
day become tombstones while their result and complete session set remain durable.
Committed status events flow to the `EXCHANGE` NATS stream. The gateway admission
and runner listeners are added in later S4 deliverables; this binary stays inactive
until S7.

Set `CONCURRO_EXCHANGE_CONFIG` to a TOML file:

```toml
operator_listen = "127.0.0.1:8095"
database_url_file = "/run/secrets/exchange-database-url"
operator_bearer_file = "/run/secrets/exchange-operator-bearer"
nats_url = "nats://nats:4222"
# nats_credentials_file = "/run/secrets/nats-creds"
# max_message_bytes = 1048576
```

The listener accepts `PUT`, `GET` and `DELETE /v1/turns/{turn_id}`, and
`POST /v1/grants/{grant_id}/revoke` with the private operator bearer. The task
application dispatches the session ID returned by a submit or status event. A
pending relay release answers `502 release_pending`; release delivery is completed
with the relay workflow in S4d.

Schemas and cross-language fixtures live under `contracts/`. `concurro-exchange-client`
provides both task operator and gateway admission clients against those types.
