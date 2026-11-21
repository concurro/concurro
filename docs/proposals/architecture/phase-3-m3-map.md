# Phase 3 M3 implementation map

M3 adds team funding, enrollment, standalone reviews, the read-only backoffice,
historical import and delivery-gap recovery to the Rust task application.
The frozen M2 source is the baseline for this work; its local pass is not a
live publication claim.

## Contracts and source owners

| Area | Existing behavior and contract | Rust seam |
| ---- | ---- | ---- |
| Consent and funding | `engine/sponsorship.py:215-283` folds source-ordered grants and withdrawals, then fills only vacant provider slots at first enrollment. `service/registry.py:1182-1219` records current-config funding-policy activation. [Sponsorship rules](../per-user-credentials.md#1-sponsorship), [source scenarios](../contract-scenarios.md#personal-credentials). | Add source and consent facts under the issue decision lock in `tasks-postgres`; pure fold and payer selection in `tasks-core`. Freeze each attempt's payer, provider, grant and slot epoch before the existing call charge. |
| Boundary and policy history | [Fill contract](../contracts.md#fills) compares boundary candidate, folded source versions, issue source revision and consent revision in one apply transaction. Only a current-head configuration read activates funding policy at local receipt time; pinned recovery reads do not. | Record activation digest and local time separately from task config. Persist a boundary-time Vault eligibility answer once, then reject stale fill results. A later credential cannot fill an old empty slot. |
| Vault metadata | `service/shadow.py:43-152` reads nonsecret owner metadata and cursor pages. `gateway/vault/vault.py:1086-1165` supplies historical records and list policy. [Vault bridge](../contracts.md#vault). | Add a typed `concurro-vault-v1` metadata client. The task service receives a metadata-only bearer, never the resolver bearer or secret material. |
| Exchange authority | `gateway/exchange/exchange.py:696-811` applies replay-safe provider transitions and cutoffs. [Admission and fences](../per-user-credentials/phase-3.md#exchange-authority). | Extend `exchange-v1` with the existing sponsorship operator contract; retain the per-provider generation and epoch in PostgreSQL. A charged attempt keeps its original payer. |
| Attempts and fallback | [Grant and retry contract](../contracts.md#grants) separates call reservation from a paid grant and freezes credential version per attempt. `engine/credentials.py` classifies model and provider failures. | Reuse M2 admission and charge; refuse a grant after cancellation or cutoff and release its reservation. A later attempt selects eligible-now credentials; unknown provider outcomes do not trigger automatic fallback. |
| Label enrollment | `engine/controller.py:904-920` checks the current opt-in label and labeler's writer authority. [Timeline channel](../contracts.md#enrollment-channels). | Map issue timeline IDs into source-ordered requests, then reuse M2 initialization and stage jobs. Recheck authority before admission. |
| Standalone reviews | `engine/standalone.py`, `engine/pending_reviews.py` and [standalone review design](../../design/standalone-reviews.md) define round state, account attribution and pending review publication. | Keep standalone task state separate from contribution task state; reuse report storage and native-review action mechanics where identities and permissions match. Use the reviewer account's grant for review mutations. |
| Backoffice | [Backoffice API contract](../contracts.md#backoffice-api) defines a lower-privilege Rust process, consistent baseline, event read model and viewer tiers. [Command proofs](../contracts.md#command-proofs) stay with the Python session authority. | Add a read-only process and task-service baseline/diagnostic endpoints. Python backoffice signs browser commands; Rust verifies exact proof and deduplicates by action ID, actor and content. |
| Historical import | [Archive import](../contracts.md#python-archive-import) preserves Python turn, session, run and gzip identities. | Reuse `tasks-diagnostics` decoding and the M2 archive uploader; add restartable import inventory and audit rows without creating live tasks. |
| Delivery gaps | [Delivery recovery and coverage](../contracts.md#delivery-recovery) require GitHub delivery readback, source reconstruction and active-task refresh. | Serve ingress's recovery API from the task application and consume coverage-gap facts through the existing source-order queue. |
| Configuration and cutover | [Configuration](../contracts.md#configuration) evolves M1 bootstrap settings to owner-managed targets and funding settings. [Cutover](phase-3-plan.md#cutover) transfers source, slot and publication floors. | Keep mutable target/funding settings in the designated authority. Import generation and slot floors plus historical PR identities before one controller activation boundary. Reread active tasks across coverage gaps. |

## Implementation order

1. Build the vault metadata bridge and pure consent fold. Reuse M2's issue lock,
   source order, attempt charge and outbox; do not create a second controller.
2. Admit explicit sponsor and self-withdrawal commands, including before a task
   exists. Record the first start or label boundary and its policy receipt before
   selecting an implicit payer. Add Exchange transitions when the selection can
   actually be submitted.
3. Add timeline-backed label enrollment and standalone review requests. Keep the
   standalone reviewer grant and App installation token roles distinct.
4. Expose a consistent task baseline and diagnostic reads, then build the read-only
   backoffice model and signed-command forwarding.
5. Add restartable Python archive import and ingress delivery-gap reconciliation.

## Current foundations

The [Vault metadata client](../../../crates/vault-v1/src/lib.rs) reads owner
snapshots and lifecycle pages with a dedicated bearer and drops unrecognized
response fields. Its list selection checks route, role, profile, status and
expiry. The [consent fold](../../../crates/tasks-core/src/sponsorship.rs) keeps
the explicit funding choice separate from sponsor consent, preserves withdrawal
tombstones in source order, and resolves explicit claims before an eligible
initiator fill at the first source boundary.
[Issue storage](../../../crates/tasks-postgres/src/sponsorship.rs) records
pre-enrollment facts and comment removals with a revision for stale-read checks;
post-enrollment removal of an active sponsor source records a cutoff. The
decision loop verifies the source actor's numeric GitHub ID, resolves renamed
accounts by ID, and refuses a definitively unverified source without holding
later requests. The
[Exchange bridge](../../../crates/exchange-v1/src/lib.rs) submits persisted
transition bytes and checks each acknowledgement. A PostgreSQL transition
worker retries the exact body after an uncertain send and exposes failed writes
to the operator. Current-head target reads and a startup/periodic observer record
funding policy activation at the database clock. Initialization confirms visible
source revisions, rechecks the issue revision after a boundary-time Vault read,
and records the first boundary and initial Exchange transitions together. The
model-turn paths select a current Vault credential for an acknowledged active
slot, bind its exact version and grant to each persisted attempt, and recheck
consent and slot identity under the issue lock before charging. The admission
transaction records an attempt-level org or contributed grant and its charged
call reservation. Cancelling an unsent contributed turn revokes the grant and
releases that reservation while retaining a monotonic call ordinal. Self-withdrawal
records a provider cutoff; the transition worker sends it after its predecessor
is acknowledged, and the affected attempt grants are marked revoked. Removing
an older sponsor comment leaves a newer same-actor slot active. A withdrawal
accepted while initialization is reading is
replayed after the historical first-fill transition. Later sponsor or funding
changes at that boundary keep admission pending until reconciliation. A
definitive absence of a sponsor, eligible credential or writer authority blocks
the stage before charge. A local
bridge fixture submits the persisted Rust intake body through the retained
Python Exchange admission validator and checks replay and withdrawal. A second
fixture records a trusted provider failure, excludes its exact record version,
and admits the next listed credential for a fresh intake call. Provider failures
also queue role-specific replacement turns for planning, implementation and
reviewer seats. Replacement turns rotate retained sessions without consuming a
reviewer's separate session-failure allowance. A successful use of an exact
credential version clears its temporary backoff while keeping the failure audit.
A lost turn remains unknown even if its observation carries
failure details; it creates no exclusion or fallback attempt. Role-specific
replacement turns use the same exact-version exclusion and bounded admission
rules. A missing grant remains visible until its owner grant can be refreshed.

The label channel now wakes an issue-events scan for each `concurro` label
delivery. Timeline event IDs supply deduplicated, source-ordered enroll and
unenroll requests. Unenrollment supersedes older label starts without affecting
manual starts or stopping an active task. Applying a label start checks the
current label, latest matching timeline event, numeric actor ID, upstream writer
permission and the pinned target's `autonomous: true` setting. Every later paid
attempt rechecks the original label and labeler's current writer authority;
definitive loss blocks before charge. Label initialization reads discussion only
through the label boundary. A failed delivery scan backs off for up to 48
attempts, then remains visible for reconciliation. Matching currently uses the
delivery issue-update time; coverage recovery also re-reads the issue timeline
and applies each event by its own source ID and timestamp.

Contribution PR publication now records `(upstream, PR number)` ownership in a
unique table when its verified draft enters review. Standalone enrollment can
claim the same table for a distinct review task. The stopped-Python cutover
manifest reserves historical PR identities before activation.

Standalone intake now records authenticated review-request deliveries for the
configured numeric reviewer account. A durable issue-timeline scan records
source IDs and retries unresolved deliveries; a serial decision rereads the
requester's current upstream writer permission and the PR's open state before
claiming the PR. The round snapshot freezes the head, base, merge base, changed
paths, panel and pinned configuration. Duplicate and older sources cannot open
another round; a later request joins an unfinished round or opens a new one after
completion. Removal by someone other than the reviewer records a cancellation
on the active round so later admission stops while accepted reports may settle.
The GitHub client can authorize the configured reviewer through device flow,
verify its numeric account ID, load the existing owner-only reviewer grant,
serialize expiring-token refresh and save the replacement before use. Missing
or expired grants pause a round before charge; the recovery scan resumes a
configured grant after service restart.
The prompt builder selects the standalone review addendum and removes the
`reopen` verdict using a trusted task-kind argument; a Python oracle fixture
checks the resulting prompt and schema.
Shared attempts now carry exactly one contribution or standalone task owner.
The runner resolves that typed owner to its configured fork target; a local
regression keeps contribution dispatch behavior unchanged. Exchange submit,
poll, deadline and terminal observation now lock and settle through that owner.
Standalone terminal reports retain their raw bytes on the independent round;
local standalone and contribution observation regressions pass. Org-funded and
contributed rounds admit one reviewer at a time, validate each report against
its pinned source, and publish a native review with positioned finding comments
under the reviewer account. The send fence and exact review/comment readback
prevent duplicate writes after an uncertain response. A definite inline placement
refusal retains the attempted comments and retries the review body containing
the findings. PR closure pauses new admissions and writes; reopening restores
the round. Removal while a reviewer is in flight waits for its review to
settle, then ends that round as cancelled. The operator list and detail views
show independent rounds, attempts, findings and publication receipts. PR
commands are source ordered and receive one App reply per revision. Funding
facts reconcile against the current provider slot, including edited-command
withdrawal and funding-choice reset. A trusted credential failure excludes its exact version and
retries the seat; invalid reports receive bounded correction. Full reports
publish as App artifacts, neutral Check Runs show progress, and finding
dispositions reply under the reviewer account before App thread resolution.
The next round supplies each reviewer's unresolved findings and accepts its
dispositions. A report in flight keeps its original reviewer identity when a
resume adopts a different panel; completion counts only the adopted panel.
Former writers may withdraw their own consent. Removal recorded while a PR is
closed cancels its round when the PR reopens. Completed checks stop polling
after their duplicate-readback window unless a new request revives the task.
Resume adopts current settings and instructions with an audit row while
retaining the pinned PR head. Local fixtures cover both funding modes,
Exchange acknowledgement, exact payer binding, native review and thread
readback, report artifacts, checks, command replies and dispositions. Live
publication and uncertain-write behavior remain unverified.

The private task API now accepts signed browser requests from the retained Python
session handler. PostgreSQL stores each action ID and exact contents across
retries, and the existing request queue rechecks writer authority. Python holds
the private key and original source time; Rust holds only the public key.
Local tests cover Python proof interoperability, changed-field refusal, durable
action replay and retry feedback storage. The Python session authority also
issues a separate read-only viewer token and serves identity and issue tier
lookups on an optional internal listener. The task API now materializes paged
task, request, turn, report and consent history from one database statement
snapshot; a local regression verifies later writes cannot split its pages.
Turn-scoped diagnostic reads expose ordered transcript lines and linked
artifacts to the internal read credential.
The task service persists a revisioned `config.targets` snapshot from its own
settings. A separate Rust backoffice process consumes task, request, turn and
configuration and read-model record events before freezing paged source
baselines. It keeps a lower-privilege model; startup and detected stream loss
rebuild it, while task, consent, funding, standalone and report changes flow
through typed durable consumers. Completed baseline snapshots are deleted;
abandoned snapshots have a bounded retained count. The frontend uses
`/api/v2` for task, review, issue and target list reads. Python still owns
sign-in, credentials and signed commands. Caddy forwards the viewer token to
the read API without the Python session cookie. Target rows rebuild from GitHub
and merge against webhook updates by source time and scan-start database clock.
Issue details read their current document directly from that index.
The backoffice keeps its SQL and migrations in `concurro-backoffice-postgres`.
Writer history reads use the task service's durable detail projection after a
fresh viewer check, including imported turns and previous review rounds.

The offline Python importer inventories stopped diagnostics and retained gzip
files, preserving turn, session, run and source paths. It queues original
archives through the shared uploader. The cutover importer records activation
time, Python's last source sequence, Exchange generation and provider slot
floors, and PRs actually owned by a Python contribution. Historical list rows
and standalone reviews do not acquire contribution ownership. Pre-activation
sources cannot start or fund new work; initial Rust slot writes advance
imported floors. Ingress delivery
listing and redelivery use a separate App-key credential. Coverage gaps are
durable tasks: the worker re-reads changed comments and issue events, checks
known deletions, refreshes active PR state and target lists, then republishes
task snapshots. An edited command whose editor GitHub cannot authenticate
records a source failure and lets independent sources reconcile; the gap
retries with backoff until every source is resolved.

## Local acceptance

Use representative local GitHub, Vault, Exchange, PostgreSQL and task-process
fixtures for each slice. Verify consent source order, stable actor IDs, payer and
provider binding at charge, first-fill priority, withdrawal, label authority,
standalone review attribution, signed command replay, empty-model baseline,
historical archive links and one missed-delivery recovery. A bounded extra turn
around withdrawal or expiration is acceptable under [repository guidance](../../../AGENTS.md#quality-and-risk-tolerance); consent must still revoke future use and
automatic retries must remain bounded. Visible recoverable failures are acceptable
when the operator can diagnose and correct them.

The dedicated live test repository was waived across phases 1–3. Local fixtures
do not establish live publication behavior; authorized direct cutover checks
remain separate under the [phase plan](phase-3-plan.md#team-readiness).
