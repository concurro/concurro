# Exchange operator service

Phase 5 S4c serves the private operator API, the OIDC authenticated runner API,
and the gateway's private admission API.
It persists grants, turns, sessions, retirement fences, results and status snapshots
in PostgreSQL. Terminal turns older than one day become tombstones while their
result and complete session set remain durable. Committed status, finished and
usage events flow to the `EXCHANGE` NATS stream. The binary stays inactive until
the coordinated S7 cutover.

Set `CONCURRO_EXCHANGE_CONFIG` to a TOML file:

```toml
operator_listen = "127.0.0.1:8095"
runner_listen = "127.0.0.1:8096"
admission_listen = "127.0.0.1:8097"
database_url_file = "/run/secrets/exchange-database-url"
operator_bearer_file = "/run/secrets/exchange-operator-bearer"
admission_bearer_file = "/run/secrets/exchange-admission-bearer"
nats_url = "nats://nats:4222"
token_key = "openbao:ops/exchange/token-key#key"
inference_base_url = "https://<host>/concurro/inference/v1"
archive_endpoint = "http://archive:9000"
archive_bucket = "concurro"
archive_region = "us-east-1"
archive_credentials_file = "/run/secrets/exchange-archive-credentials"
archive_prefix = "blobs"
# nats_credentials_file = "/run/secrets/nats-creds"
# max_message_bytes = 1048576

[openbao]
url = "http://openbao:8200"
role_id = "<exchange-role-id>"
secret_id_file = "/run/secrets/exchange-openbao-secret-id"
```

`archive_credentials_file` contains JSON fields `access_key_id` and
`secret_access_key`. For local tests, `token_key` may name a private file instead.
The bucket and `archive_prefix` must match the task application's archive
settings: task submit verifies retained inputs there, and result ingestion reads
the runner's content addressed blobs from that same location.
The runner listener is routed from `/concurro/runner/*`; bind it only to the
platform network in deployment. Sessions have 1,200 seconds to bind, then require
a heartbeat within 180 seconds. Reconciliation retires stale sessions and replaces
queued work; a running turn becomes lost. Uploaded blobs are content addressed and
limited to 256 MiB; result JSON is limited to 1 MiB.

The operator listener accepts `PUT`, `GET` and `DELETE /v1/turns/{turn_id}`, and
`POST /v1/grants/{grant_id}/revoke` with the private operator bearer. The task
application dispatches the session ID returned by a submit or status event. A
pending relay release answers `502 release_pending`; release delivery is completed
with the relay workflow in S4d.

Schemas and cross-language fixtures live under `contracts/`. `concurro-exchange-client`
provides both task operator and gateway admission clients against those types.
The v2 launcher and Python engine use the runner listener. The admission
listener accepts session-key requests, finish reports and live-session checks
with its own private bearer. It records usage, fences revoked grants, closes
unreported requests after their five-minute settlement window, and emits each
terminal turn fact once its requests have settled. The gateway is the only
admission listener caller.
