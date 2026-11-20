# Task application (Phase 3)

The Rust task service consumes issue comments and closures, initializes tasks,
admits intake turns, exchanges results with the Python exchange, writes request
replies, one managed issue dashboard, intake notices, stage labels and runner refs,
and stores turn diagnostics. State, notice, report, request and turn events are
written to the outbox with their state changes and forwarded to the `TASKS`
stream. The Phase 3
implementation is still in progress; this executable is not ready for deployment.

Set `CONCURRO_TASKS_CONFIG` to a TOML settings file. The runtime reads these fields:

| Group | Fields |
| ---- | ---- |
| Storage and events | `database_url_file`, `nats_url`, optional `nats_credentials_file`, `max_message_bytes`, `published_retention_seconds`, `forwarder_poll_ms`, `consumer_name`, `consumer_max_attempts`, `consumer_poll_ms` |
| GitHub App | `github_api_base`, `github_app_id`, `github_private_key_file` |
| Exchange | `exchange_url`, `exchange_token_file` |
| Private archives | `archive_endpoint`, `archive_bucket`, `archive_prefix`, `archive_region`, `archive_credentials_file` |
| `[intake]` | `workflow_ref`, `models`, `budget`, `lease`, and `[intake.gateway]` with the runner's gateway settings |
| `[[targets]]` | `repository_id`, `issue_repository`, `fork`, `upstream`, `issue_permissions`, `issue_write_permissions`, `fork_permissions`, `fork_write_permissions`, `upstream_permissions`, `bot_login`, `publisher_logins`, `engine_sha` |

`fork_permissions` needs Contents and Actions read. `fork_write_permissions`
needs Contents, Actions and Workflows write for the pinned run ref and launcher.

The database URL, App key and exchange token are read from the named files. The archive
credential file contains JSON with `access_key_id` and `secret_access_key`. An optional
NATS credential file contains JSON with `user` and `password`. The task service runs
its local migrations before starting the consumer and jobs.

The same executable has host operator commands:

```text
concurro-tasks tasks list
concurro-tasks tasks show owner/repository#123
concurro-tasks effects pending
concurro-tasks consumer status
concurro-tasks archives retry <gzip-sha256>
concurro-tasks attempts cancel <attempt-uuid>
concurro-tasks writes resolve <write-uuid> --written <comment-url>
concurro-tasks writes resolve <write-uuid> --failed
concurro-tasks writes resolve <write-uuid> --resend
```

`tasks show` includes request dispositions, attempt states, exchange sessions,
intake reports, comment creates, the dashboard's desired revision and edit writes,
managed stage labels, runner ref and dispatch status, turn artifact digests,
and archives.
It omits the stored submit body because that body may contain gateway headers.
`attempts cancel` records the host user and queues retirement of a live
turn; the running service executes that queued exchange effect. `writes resolve`
records the host user. `--failed` stops sends while continuing readback for a
late App comment. `--resend` overrides uncertainty and may duplicate a late
create.
`effects pending` includes dashboard edits awaiting readback, notice and reply
creates, and uploads waiting, retrying, or exhausted. Dashboard creates and
notices use the same marked-comment readback as replies. Dashboard edits read
the App's existing marked comment before writing the latest task revision;
an older uncertain edit stays watched so a late write is corrected.
`archives retry`
requeues an exhausted or worker-stranded original gzip at its
`<archive_prefix>/sha256/<gzip-sha256>.tar.gz` key and records the host user. Concurrent
retries share one active upload job. The uploader verifies an earlier uncertain PUT
by reading that key before sending again. Definitively rejected reply POSTs (401
and proven rate limits) retry with fresh credentials or the server's delay;
uncertain creates stay on readback until the App comment is observed or the host
resolves them.
Stopped and expired attempts fence runner dispatch before exchange retirement;
later session replies retain evidence with a skipped dispatch. Unknown attempts
from a polling outage remain live for stop, while a retired deadline or lost
attempt can use the operation's second unknown allowance. Original exchange
response and intake report bytes are retained beside PostgreSQL-safe JSON
projections. The worker heartbeats and sweeps at five-second intervals, reclaiming
abandoned jobs after fifteen seconds. Recovery scans reuse stable job keys, and
unsettled retirements are requeued after their worker jobs exhaust.
`consumer status` shows the applied stream sequence and unresolved coverage gap
for the comment and closure consumers.

Fresh intake has no retained session input. The storage admission check for later
continuations requires the exact task, scope and ordinal, a complete journal,
matching predecessor digest, instructions and response schema, and restorable
original gzip bytes or a verified archive object, within the uncompressed byte
limit when set. An exact same-turn request reuses a non-null journal response;
a missing or corrupt required revision does not fall back to an older one.
