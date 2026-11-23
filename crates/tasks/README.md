# Task application

The Rust task service consumes issue comments, opt-in label changes and closures,
initializes tasks,
admits intake, planning, design review, implementation and PR review turns,
publishes a task branch, draft PR and App-authored reviews with file and line threads,
observes readiness
gates and external PR closure or merge, exchanges results
with the selected exchange, writes request replies, one managed issue dashboard,
intake notices, stage labels and runner refs, and stores turn diagnostics.
Complete reports are also queued as issue generic artifacts. State, notice,
report, request and turn events are
written to the outbox with their state changes and forwarded to the `TASKS`
stream. The Rust exchange mode is prepared for inactive Phase 5 deployment;
the Python mode remains active until the coordinated S7 cutover.

Opt-in label deliveries scan the issue events timeline and record one request
per `concurro` label event ID. A current label and current writer are required
when a label start applies, and the pinned target must have `autonomous: true`.
Each paid attempt rechecks the original label and labeler's current writer
authority; a definitive change blocks the task before charge. Removing the
label supersedes older label starts without superseding manual starts or stopping
work already active. Failed delivery scans back off and remain visible in
`effects pending`; after 48 failed scans they require reconciliation.
The durable coverage-gap consumer re-reads changed comments and issue timelines,
checks known comment deletions, refreshes pending task decisions and PR gates,
rebuilds target lists, and publishes refreshed snapshots before closing a gap.
An edited command without an authenticated editor remains pending for operator
reconciliation.

When a target sets `reviewer_user_id`, authenticated requests for that numeric
reviewer on its upstream PR enter a separate standalone review channel. The
service scans issue-timeline event IDs, verifies the requester's current writer
permission and PR state, and stores a frozen review round. The service
admits reviewer turns one at a time under org or contributed funding, validates
reports against the pinned source, and publishes native reviews with positioned
findings through the reviewer's user grant. Invalid reports receive bounded
reviewer correction;
complete reports are stored as App artifacts. The App maintains a neutral
activity check and posts one reply per PR command revision. A closed PR pauses
admission and review writes until it
reopens. Removal stops later admissions and lets an in-flight report settle.
Writers can stop, retry, resume, and change funding on the PR. Resume adopts
current review settings and instructions while retaining the round's source
head. The host operator list and detail views include standalone rounds and
receipts.

Set `CONCURRO_TASKS_CONFIG` to a TOML settings file. The runtime reads these fields:

| Group | Fields |
| ---- | ---- |
| Storage and events | `database_url_file`, `nats_url`, optional `nats_credentials_file`, `max_message_bytes`, `published_retention_seconds`, `forwarder_poll_ms`, `consumer_name`, `consumer_max_attempts`, `consumer_poll_ms` |
| Internal task API | Optional paired `api_listen`, `command_proof_public_key_file`, `api_read_bearer_file` |
| Ingress recovery | `delivery_recovery_bearer_file` for authenticated delivery listing and redelivery |
| GitHub App | `github_api_base`, `github_app_id`, `github_private_key_file` |
| Reviewer OAuth | Optional `reviewer_oauth_base`, `reviewer_oauth_client_id`, `reviewer_oauth_client_secret_file` for standalone user-grant refresh |
| Exchange | `exchange_url`, `exchange_token_file`, `exchange_kind = "python"` or `"rust"`; Rust mode also needs `rust_engine_repository` |
| Vault metadata | Optional paired `vault_metadata_url`, `vault_metadata_token_file` |
| Private archives | `archive_endpoint`, `archive_bucket`, `archive_prefix`, `archive_region`, `archive_credentials_file` |
| `[intake]` | `workflow_ref`, `models`, `budget`, `lease`, and `[intake.gateway]` with the runner's gateway settings |
| `[[targets]]` | `repository_id`, `issue_repository`, `fork`, `upstream`, `issue_permissions`, `issue_write_permissions`, `fork_permissions`, `fork_write_permissions`, `upstream_permissions`, `upstream_write_permissions`, `bot_login`, `publisher_logins`, `engine_sha`; optional `org_routes = { claude = "claude_org", codex = "openai_org" }` maps provider names to frozen Rust gateway route names, and `reviewer_user_id`, `upstream_repository_id`, `reviewer_grant_file` configure standalone reviews |

Rust mode sends frozen org submissions to the Rust operator, consumes
`exchange.turn.*` and `inference.usage`, and reads stale attempts from the
operator at startup and every five minutes. It does not run the Python slot
adapter or fifteen-second exchange poll. Its `org_routes` map supplies the
provider route frozen on each org grant, including standalone reviewer turns.
Before sending or replaying a turn with retained input, it verifies that the
exact archive object is available to the exchange runner.
The task and exchange archive bucket and `archive_prefix` settings must match.
Rust mode also freezes selected contributed credential versions and consumes
accounts' credential-change facts to retire revoked grants.

`fork_permissions` needs Contents and Actions read. `fork_write_permissions`
needs Contents, Actions and Workflows write for the pinned run ref and launcher.
`upstream_write_permissions` needs Pull requests write for draft PR and review
publication, Issues write for standalone report artifacts and command replies,
and Checks write for standalone activity checks. `issue_write_permissions` needs
Issues write for contribution report artifacts.
The upstream installation must grant it before this service reaches publication.
The reviewer grant file must belong to the configured numeric account and be
owner-only. When it is missing or expired, the standalone round waits before
charging a model call. A newly created grant file is picked up on service restart;
the recovery scan then resumes the round. The reviewer user token needs Pull
requests write on the upstream for native review publication.

Writer commands include `/concurro approve <plan-id>`, `/concurro retry`,
`/concurro retry --plan-revisions N`, `/concurro resume`,
`/concurro resume --description-only` and `/concurro invalidate`.
An implementer-skill-only retry preserves design approval when bounded local Git
history proves that no other bundle resource changed. The retry starts a fresh
implementer session and retains spent budgets.

The database URL, App key, exchange token and optional metadata-only Vault token are
read from the named files. Contributed model turns require the Vault reader, an
active sponsor with current writer authority, and an acknowledged provider slot.
Intake, planning, implementation and reviewer attempts store the selected
credential version and grant before the call charge. Withdrawal advances the
provider fence and prevents a later charge under the old grant, including when
the withdrawal arrives before initialization finishes. Removing a sponsor
comment after enrollment also records a cutoff. A definitive missing sponsor,
eligible credential or writer authority visibly blocks the stage before charge.
Each admitted attempt records its org or contributed grant. Cancelling an unsent
contributed attempt frees its call capacity while retaining its call ordinal.
A trusted provider failure excludes that exact personal credential version for
later selection; intake, planning, implementer and reviewer turns can select the
next eligible entry. A lost turn with an unknown provider outcome does not
exclude the credential or start a fallback attempt. Automatic recovery when no
entry is currently usable remains under development.

In Rust exchange mode the selected accounts entry, custody, relay and payer
login are frozen in the v2 grant. `credential.changed` facts fence retired
versions before grant insertion and queue retirement for submitted turns.
Withdrawn provider slots are acknowledged only after the Rust exchange has
fenced their grants and the verifier has acknowledged any relay releases.
Replacing or removing a version retires submitted turns; the current stage
follows its existing cancelled or lost-turn handling. A new entry alone does
not automatically resume a stopped turn. A trusted provider failure selects
the next eligible version when the stage's existing retry applies.
The archive credential file contains JSON with `access_key_id` and `secret_access_key`. An optional
NATS credential file contains JSON with `user` and `password`. The task service runs
its local migrations before starting the consumer and jobs.

The optional private task API accepts `POST /v1/requests` with a session-authority
RS256 proof and UUID `Idempotency-Key`. It binds the actor, issue,
command, arguments, detail and original source time. A newly signed retry with
identical contents returns the original result; changed contents conflict.
`GET /v1/requests/{id}` requires a read-only internal bearer. The Rust accounts
service signs the proof for Phase 4; Python remains the live signer until cutover.
This process reads only the public key. `GET /v1/baseline` uses
the same read bearer. Its first page materializes durable task, request, turn,
report and consent records from one PostgreSQL statement snapshot; `next_cursor`
continues that frozen snapshot across later writes. The final page releases it;
abandoned snapshots are retained in a bounded set. This M3 bridge is not deployed.
Turn-scoped `GET /v1/turns/{turn}/transcript` pages preserved transcript lines,
and `GET /v1/turns/{turn}/artifacts/{digest}` returns a linked original artifact;
both require the same internal read bearer.
`GET /v1/index` pages the durable issue, PR and executor-run index;
`GET /v1/index/item` reads one current row for issue details. Target
configuration comes from the task service's revisioned `config.targets` event
and is included in the baseline. Ingress delivery history and redelivery use
the separate recovery bearer.
Phase 4 Slice 1 adds private `GET /v1/targets` for the accounts service. It
reads `revision` and `targets` from the same durable `tasks.config_targets` row,
requires no bearer, and returns `503` until that row is present or if storage is
unavailable. The listener remains on the platform network.
`GET /v1/history` reads retained task and standalone review history with the
same internal read bearer; the backoffice exposes it only after a writer check.

The same executable has host operator commands:

```text
concurro-tasks tasks list
concurro-tasks tasks show owner/repository#123
concurro-tasks effects pending
concurro-tasks consumer status
concurro-tasks imports cutover <manifest.json>
concurro-tasks imports python <manifest.json>
concurro-tasks archives retry <gzip-sha256>
concurro-tasks attempts cancel <attempt-uuid>
concurro-tasks writes resolve <write-uuid> --written <comment-url>
concurro-tasks writes resolve <write-uuid> --failed
concurro-tasks writes resolve <write-uuid> --resend
concurro-tasks writes resolve git:<attempt-uuid> --resend
concurro-tasks writes resolve review:<turn-uuid> --failed
concurro-tasks writes resolve action:<turn-uuid>:<sequence> --resend
concurro-tasks writes resolve ready:<task-uuid> --failed
```

`tasks show` includes request dispositions, attempt states, exchange sessions,
intake, plan, design review, implementation and PR review reports, review seats
and findings, native review action status and thread IDs, the latest Git publication
and ready intents, terminal PR observation, report artifact writes, resume and
invalidation audits, implementation design reopens, comment creates, the dashboard's
desired revision and edit writes,
managed stage labels, runner ref and dispatch status, turn artifact digests,
and archives.
It omits the stored submit body because that body may contain gateway headers.
`attempts cancel` records the host user and queues retirement of a live
turn; the running service executes that queued exchange effect. `writes resolve`
records the host user. `--failed` stops sends while continuing readback for a
late App comment. `--resend` overrides uncertainty and may duplicate a late
create.
For Git publication, PR reviews, native review actions and ready intents, inspect
GitHub and `effects pending` before choosing `--resend` or `--failed`. These typed
targets are accepted with either flag; `review` applies only when no native action
is sent, and `action` names the sent action sequence. `--resend` requeues the
frozen write; `--failed` makes the task block visible. A late GitHub write may
still arrive after this explicit operator decision. Every resolution records the
host user in `operator_actions`.
`effects pending` includes Git publication and ready intents, PR review and report
artifact writes, the next native review action, dashboard edits awaiting readback,
notice and reply creates,
label and standalone review wakes and uploads waiting, retrying, or exhausted.
Dashboard creates and
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
`imports cutover` applies the stopped Python source boundary, Exchange generation
and provider-epoch floors, and historical PR ownership before controller startup.
Create its manifest with `deploy/export_python_cutover.py` from the stopped,
read-only Exchange, list-index and registry databases. `imports python` queues the
offline historical archive inventory from `deploy/export_python_archive.py`.
Both imports are restartable; neither starts live admissions.

Fresh intake has no retained session input. The storage admission check for later
continuations requires the exact task, scope and ordinal, a complete journal,
matching predecessor digest, instructions and response schema, and restorable
original gzip bytes or a verified archive object, within the uncompressed byte
limit when set. An exact same-turn request reuses a non-null journal response;
a missing or corrupt required revision does not fall back to an older one.
