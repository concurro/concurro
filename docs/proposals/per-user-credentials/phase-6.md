# Phase 6: credential recovery and provider views

Automatic recovery now uses the existing task queue and ownership/admission path. It does
not grant sponsorship, reset spent work, refresh tokens or select an organization fallback.
Real subscription/backend checks remain deferred; Phase 5a refresh is not implemented.

## Scheduling authority

The service reads `GET /metadata/events?after=N` with its existing metadata principal on
the public metadata listener. The feed contains only existing immutable lifecycle fields.
GitHub owner tokens and the gateway bearer cannot use it; the metadata bearer still cannot
resolve secrets. Exchange retains its independent private events/resolver principal.
No new secret or enablement setting is introduced. The vault and service must both serve
the new feed contract; an older/unavailable vault delays recovery while manual paths remain.

The existing delivery-recovery timer (300 seconds by default) polls pages on every check,
including healthy webhook coverage, alongside current-target policy observation; a vault outage delays scheduling without
changing consent. Expected missing/inaccessible target lookups are logged per target and
do not disable the feed or App delivery scan; arbitrary failures remain failures. Registry schema 8 adds a cursor and a latest-version metadata cache,
not another lifecycle ledger. One transaction updates that evidence, matching queue receipts
and cursor acknowledgment. A failure before commit leaves all three unapplied. Out-of-order
or repeated sequences cannot replace newer evidence. Key rewrapping preserves the original
validation receipt and does not count as another validation. The poll scans current task
projections and latest-version metadata in the existing Registry transaction; this favors
atomic receipts over a new scheduling index, with work scaling with those local records.

Each checkpoint's recovery intent binds current provider/owner/route/grant/epoch requirements,
failed versions and one pause identity. Registry projects its pending delivery alongside the
existing sponsorship task document. Queueing during a live tick waits until that owner ends;
validation arriving before the failure checkpoint therefore is not lost. Consuming a receipt
is checkpointed. A known coverage refusal consumes only that receipt; transient I/O retains
it and uses existing queue backoff. That service deadline survives new validation,
restart and an earlier inspection tick; consuming a work item cannot lose its future wake.
Unrelated events cannot renew the same retry. An explicitly empty current-failure snapshot
stays empty after writer recovery; only an absent legacy snapshot consults the old failure.

## Eligibility and execution

| Pause | Automatic condition |
| ---- | ---- |
| Trusted `auth` or `quota_exhausted` | A newer validated version for each affected current owner/route, plus usable evidence for every required route. A validation of the failed version cannot retry it. |
| Precharge credential coverage | Recorded active consent and validation evidence for all required owner/routes; the tick checks exact models and current versions. Missing grants are never restored by token events. |
| Trusted `rate_limited` | A numeric retry hint already accepted by the gateway parser; persist the original not-before time. Same-version retry is allowed only after that time and full admission. |
| Unknown, missing rate hint, stopped, human wait, budget block | No automatic recovery. Ordinary writer commands retain their existing behavior. |

The owned tick processes stop, withdrawal, permission loss and other current issue commands
before recovery. It rechecks whole-roster coverage, current slots/cutoffs/epochs, repository
writer authority, bundle/requirements/plan authority and remaining call budget before clearing
the credential latch. Metadata older than the failed version's required replacement remains
transient; it cannot select the old payer/version merely because a newer event was received.

Fallible retirement/preflight checks keep the task durably paused. Authority can be checked
for the intended retry stage without temporarily publishing that stage. Only a successful
check restores the saved stage and acknowledges the latch. Existing normal admission freezes
the new attempt and charges it exactly once. Saved operations, reports, approvals, transcript
scopes, historical admissions and counters remain intact. A two-provider failed review batch
waits for both replacements; unrelated admitted work follows the existing isolation rules.

The original failure clock survives receipt/source/provider changes, duplicate delivery and
restart. A new actual failed attempt may record a new hint. No provider error taxonomy is
inferred from status/text, and unknown subscription errors stay unknown. Trusted terminal
failures retain their existing `failed` operation status and do not create uncertainty.
Preexisting uncertainty/correction limits still apply; automatic recovery never resets them. Manual commands may
still recover work explicitly; automatic recovery does not reset correction allowances.

## Views and forward migration

Existing issue/task projections now expose recovery eligibility/deadline/status and actual
charged-attempt ledger rows. Coverage shows profile, role, exact route/model and current
credential version. Historical charges keep their actual owner/version across transfer;
organization-funded calls are labelled separately from prospective personal consent. Token
observations remain source-labelled supplementary evidence, with missing usage unknown.
A consumed delivery remains visible as a prior check, not a scheduled retry; its status
survives inspection until a new intent supersedes it. The live and mock portal use the same shapes and existing command/credential transport.
Mock validation updates only matching current sponsors' coverage/scheduling display, never
spent budgets or simulated paid work.

Registry schemas 2–7 migrate forward preserving journals, grants and queue deadlines.
Previously acknowledged contributed `awaiting_credentials` tasks without a recovery receipt
receive one owned inspection after upgrade. It reconstructs only from their checkpoint and
current source/metadata; old same-version validation cannot authorize an auth retry. Stopped,
unknown/human and budget states receive no such bootstrap. Preserve the vault's event
sequence, versioned state and Registry together when moving services; recovery is roll-forward
only. Old admission bindings and historical artifacts are unchanged.

## Verification boundaries

Isolated acceptance uses actual scheduler/Worker/Controller/Registry/Exchange ownership,
request-start observations, charged admissions and synthetic provider completions. It covers
same-version suppression, event-before-pause, duplicate/out-of-order feed, disk restart,
transaction failure, current model/version checks, multi-provider recovery, scoped permission
loss, stop/human/budget exclusions and schema 7 upgrade. Actual public/private TCP tests
prove the metadata feed cannot obtain resolver authority. No live provider, login, deployment
or paid task retry is used. Existing Phase 5 connection-establishment/proxy and live-account
limitations remain; this phase adds scheduling, not new provider capability claims.
