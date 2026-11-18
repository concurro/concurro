# Contract scenarios

**Status: proposal, not implemented.**

These traces walk the critical flows through the [service contracts](contracts.md), step by
step, and check them against the invariants of the
[architecture review](architecture.md), [request intake](request-intake.md) and
[mentions](mentions.md). Most are adversarial: delayed messages, lost replies,
concurrent attempts and restarts. A step that needed a contract change names its
[finding](#findings), and each finding is applied in the contracts.

Notation: `A → B: message {key fields}`. "Tx" means one database transaction, outbox
included. Paths omit their `/v1` prefix. Before phase 5, steps that reach the Python
exchange go through the [bridges](contracts.md#bridges).

## Enrollment and starting

### 1. Label enrollment and a manual start

Phase 3, M1. A writer W labels an issue on a pilot target in manual mode, then comments
`/concurro start`.

1. GitHub → ingress: `issues.labeled` delivery D1. Tx: receipt and
   `github.issues.labeled {label: concurro, sender: W}`.
2. Label channel and Python service both consume it and claim
   `{issue, input: label}`. The pilot rule decides, not the claimant: both get
   `{owner: rust, task_id: T, created}` once. The Python service drops the event.
3. Label channel → GitHub: the issue's events timeline. It finds `labeled` event E1 by W
   with no request yet. Tx: request `enroll {source: label:E1:0, source_at:
   E1.created_at}` ([finding 14](#findings)).
4. Orchestrator → publisher: `POST /tokens/read`; orchestrator → GitHub: W's permission.
   Tx: request applied, `task.state` revision 1 (enrolled, not initialized:
   [finding 2](#findings)), `task.consent` revision 1 (boundary pending),
   `request.orchestrator.applied`.
5. W comments; ingress publishes `issue_comment.created`. Comment channel: claim returns
   the recorded owner. Tx: source final with one request `start
   {source: comment:C:digest:0, source_at: comment created_at}`.
6. Orchestrator applies `start`, the manual boundary. It reads the timeline up to the
   comment's time and finalizes earlier sources: no sponsor comments, so W is the
   initiator with provenance `task_start`.
7. Orchestrator → funding: `POST /fills {task, boundary, initiator: W, providers}`.
   Funding's vault adapter reads W's metadata at the boundary, with complete history:
   `openai` yes, `anthropic` no ([finding 3](#findings)).
8. Tx: boundary frozen, consent revision 2 (`openai` active for W, epoch 1;
   `anthropic` unassigned at epoch 0), two `task.T.slot.changed {reason: initialize}`,
   stage `intake`, attempt A admitted against `max_model_calls` and recorded with its
   decision ([finding 24](#findings)).
9. Funding's slot adapter sends both `initialize` transitions; the exchange acknowledges
   them. Orchestrator → funding: `POST /grants {purpose: task_contributed, attempt: A,
   decision, limits}`. Had the grant arrived before the `openai` acknowledgement, funding
   would have answered `503 slot_pending` ([finding 4](#findings)). Funding selects W's
   eligible credential. Response: grant G.
10. The orchestrator's exchange adapter builds today's submit request from G: binding,
    frozen admission, payload v1. Tx: it stores that exact body for turn A
    ([finding 5](#findings)). Orchestrator → exchange: `POST turns {turn: A, ...}`; reply
    `{dispatch: S}`.
11. Tx: `turn.A.submitted`, `runner.dispatch {session: S}`. Publisher checks write
    admission, creates `concurro/runs/S`, dispatches the launcher and publishes
    `publication.outcome`.
12. Runner: OIDC, key, `next`, runs, posts the result.
13. Orchestrator polls `GET turns/A?wait=45`: `turn.A.status` per change, then reads
    observations for A. Tx: `turn.A.finished {outcome, provider_requests}`, intake
    applied to the task, `task.state` revision 3.
14. Orchestrator → exchange: end session S; `runner.session.ended`. Publisher deletes
    the run ref.

**Checks.** One owner for one issue whichever consumer claims first. Every state change
and its events commit together. The funding boundary is the `start`, and a credential W
adds after it never fills `anthropic`. Retrying any step reuses its key: claim by issue,
request by source, grant by attempt, transition by digest, turn by turn ID.

### 2. A standalone review requested

Phase 3, M3. Writer W requests a review from the reviewer account on a PR Concurro didn't
write.

1. `pull_request.review_requested {requested_reviewer: account}`. The review-request
   channel reads the PR's timeline: `review_requested` event E2 by W. Claim
   `{input: review_request}`; request `enroll {source: review_request:E2:0}`.
2. The task is `standalone_review` in mode `review`; the first verified review request is
   its funding boundary. Fills, a grant with provenance `review_request`, and the exchange
   accepts it ([contracts](contracts.md#python-exchange)).
3. W removes and re-adds the request: two more timeline events, each its own request.
   The removal unenrolls; the re-request enrolls again and starts a new round, as today.

**Checks.** Contributed standalone reviews aren't refused by the exchange, and a
re-request is a new source rather than a duplicate of the first.

### 3. Two first inputs across the pilot switch

1. Before issue I is on the pilot list, U comments `/concurro sponsor openai` on it. The
   Python service claims `{input: comment}` and gets `python`.
2. I is added to the pilot list. W labels I. The orchestrator claims and gets the
   recorded `python`, so it ignores the label; the Python service enrolls the task.
3. Had both inputs arrived at once on either side of the switch, both claimants would
   still get the one record the first claim inserted.

**Checks.** One owner per issue, and a recorded owner wins over later changes to the rule
(M2's exit test).

### 4. A mention that enrolls and starts

An interpreter is configured; W writes "@account plan and implement this" on an
unenrolled issue. Before phase 5.

1. Mention channel: claim `{input: mention}` → `rust`, task T. Tx: source pending
   `{comment: C, digest}` with interpretation attempt I, `request.source.received`. The
   publisher adds the `eyes` reaction as the account.
2. Orchestrator → funding: `POST /grants {purpose: interpretation, attempt: I, payer:
   W}`. The interpreter runs as a turn on the Python exchange, with an `interpreter`
   role and no slot transition ([finding 9](#findings)).
3. Result: `plan_and_implement`. Tx: source final with `enroll` (index 0) and `start`
   (index 1), the interpretation frozen in both, `request.source.final {requests: [R0,
   R1]}`.
4. Both apply; the publisher sends one reply once both have outcomes.
5. Crash after the call: on restart I is pending, so the orchestrator polls turn I and
   finalizes from its stored result. If the result is gone (the Python exchange keeps
   finished turns for a day) and the deadline passes, the source is final with
   `failure: interpreter_deadline`, and the reply asks for the exact command. Nothing is
   paid again.

**Checks.** One request per source and index, one reply per source, a model never
authorizes, and an interpretation is never repeated or recomputed.

### 5. A mention without an interpreter credential

1. As trace 4 to step 1. The grant request for I is refused with
   `no_eligible_credential` ([finding 15](#findings)).
2. Tx: source final with `failure: interpreter_unavailable` and no requests. The reply
   says only exact mentions work and gives the command.
3. The task, if one exists, doesn't enter `awaiting_credentials`.

**Checks.** A missing interpreter credential takes the deterministic path and never
blocks a task.

## Grants and attempts

### 6. A crash between the grant and the submit

1. Trace 1 to step 9: attempt A and grant G recorded; orchestrator crashes before step 10.
2. On restart A is pending with a grant. Orchestrator → funding: `POST /grants` with A's
   recorded body. Funding returns the original grant with G's current status.
3. If G is `issued`, the orchestrator submits turn A with the body it stores first. If it
   crashed after submitting, the same body gets the original admission back, since the
   exchange keys on the turn ID and the body's digest.
4. If G is `revoked`, it doesn't submit, and the task follows the revocation.

**Checks.** A replay keeps its payer and grant (M1's exit test). The digest only matches
because the body is stored before the first call, not rebuilt ([finding 5](#findings)).

### 7. Concurrent attempts under one slot

Task T's `openai` slot is active for P at epoch 4. Two reviewers start at once.

1. Attempts A and B each `POST /grants {task_contributed, slot_epoch: 4}`.
2. Funding issues grants GA and GB. The slot adapter maps both to the slot grant
   `sha256(T, openai, 4)`. The epoch 4 transition was acknowledged when P sponsored, so
   neither waits ([finding 16](#findings)).
3. A's credential is then replaced by a new version; C's grant selects it. Same slot,
   same Python `grant_id`; only the admission's credential version differs, and the
   exchange checks that version separately.
4. Had the epoch 4 change been unacknowledged, A and B would both get `503 slot_pending`
   and retry; a lost transition reply is retried with the same body and returns the
   stored one.
5. P withdraws (epoch 5, withdrawn), then sponsors again (epoch 6): two `slot.changed`
   facts, each naming the previous one, sent as two transitions, each one epoch higher.
   Grants under epoch 6 map to a new slot grant.

**Checks.** Every grant under one consent maps to one Python slot, the exchange's
"epoch + 1 or unchanged" rule is never broken, and renewed consent is a new slot grant.

### 8. Consent changes before the first grant, with a lost reply

Manual task T. Before anyone starts it, P sponsors `openai`, withdraws and sponsors again.
Then W starts it.

1. Before the boundary, nothing is published for the slot: the three comments are
   sponsorship facts folded at the boundary.
2. W's `start` freezes the boundary. The fold leaves P active, so the first
   `slot.changed` is `initialize` at epoch 1 for P. Funding never sees an epoch 3 the
   exchange can't place ([finding 24](#findings)).
3. P then withdraws (epoch 2) and sponsors again (epoch 3): two more facts, revisions r2
   and r3, each naming the previous one. The bus delivers r3 before r2; the adapter holds
   r3.
4. The adapter sends r2 with `expected_epoch` 1, from its own record. The reply is lost.
   It resends the same body and gets the stored reply. Then it sends r3 with
   `expected_epoch` 2.
5. A grant at epoch 3 requested during step 4 got `503 slot_pending` and succeeds on
   retry.

**Checks.** The adapter needs nothing but its own record and the exchange's replies; a
lost reply or an out-of-order fact can't skip or repeat an epoch.

### 9. A turn submitted before funding records its sponsorship

1. A bug or a race sends `POST turns {turn: A}` before the exchange has the slot row.
2. The exchange has no active slot matching the admission and refuses with `409`. It
   never takes the grant from the submitted admission.
3. The orchestrator retries the submit, which the exchange accepts once the transition
   lands. In normal operation this doesn't happen: funding answers only after the
   transition is acknowledged ([finding 4](#findings)).

**Checks.** The refusal keeps payment safe; the ordering keeps it from happening in
normal operation (M1's exit test).

### 10. The wrong token reaches the exchange first

1. A Python service bug submits a turn for task T, which the orchestrator owns.
2. On T's first operation the exchange reads `GET /ownership/{issue}`: `rust`. It records
   the owner and refuses the Python token with `403`.
3. The orchestrator's later submit for T is accepted.
4. If the ownership lookup fails, the exchange answers `503` and records nothing, so it
   never guesses an owner ([finding 6](#findings)).

**Checks.** The recorded owner proceeds whatever arrived first (M1's exit test).

### 11. A credential fails and another is tried

1. Turn A finishes `failed`, reason `provider_error`, with
   `provider_failure {category: auth}` and `provider_requests: some`.
2. Orchestrator → funding: `POST /grants {attempt: B, decision, retry_of {attempt: A,
   failure {category: auth, model}}}`. Funding excludes that version for T and selects the
   next eligible entry in the same transaction ([finding 10](#findings)).
3. Turn C later fails with `model_denied` for model M on version V2. The exclusion is
   `(V2, M)`: an attempt for model N may still use V2 ([finding 17](#findings)).
4. Turn D's outcome is `unknown`, `provider_requests: unknown`. The orchestrator sends no
   `retry_of`; it follows its uncertainty limit, and funding excludes nothing.

**Checks.** Fallback stays within the same sponsor's eligible credentials, exclusions are
as narrow as the evidence, and an unknown outcome never switches accounts.

### 12. An unusable result after paid work

1. The runner gets a response that isn't valid JSON. Today that's outcome `failed`
   (`engine/turns.py:273`), after the provider has been called.
2. The adapter publishes `outcome: failed, reason: invalid_output, provider_requests:
   some` ([finding 18](#findings)).
3. Retrying is a new attempt: admitted against `max_model_calls` and charged like any
   other. Nothing treats `failed` as "never happened".
4. The same failure on an org-funded turn: the exchange records no org requests, so an
   empty record proves nothing, and `provider_requests` is `unknown`. A contributed turn
   that finished with no recorded request and none awaiting observation is `none`
   ([finding 25](#findings)).

**Checks.** A consumer can't mistake an unusable result for free, unexecuted work, and
`none` is only claimed where the exchange's records cover the turn.

### 13. A replay after a credential replacement

Attempt A was admitted under P's version 1. P then replaces the secret: version 2.

1. A's first submit lost its reply, so the orchestrator restarts and resends A's stored
   body. The exchange finds A's admission and returns it before looking at current
   metadata (`exchange.py:1305`); A keeps version 1. The orchestrator records the reply
   and its `dispatch`, and from then on only polls A.
2. Attempt B, never admitted, was built with version 1 before the replacement. Its first
   submit gets the credential `410`: B never ran. Funding treats version 1 as unusable
   for T's new attempts, and B's retry selects version 2.
3. Two days later the exchange has pruned A, leaving a tombstone. Polling A gets `404`:
   if the orchestrator already recorded A's result, that result stands; otherwise A is
   `unknown`, and nothing falls back automatically ([finding 26](#findings)).
4. The same with P's credential unchanged, and a resubmit of A from a bug or an old
   recovery job: without the tombstone the exchange would admit A again and could repeat
   paid work. With it, the same digest gets `410 turn_expired`, a changed body `409`,
   and A stays `unknown` ([finding 29](#findings)).
5. Turn X was cancelled before it was ever submitted, then pruned. Its tombstone has no
   digest, and any later submit with X's ID is refused.

**Checks.** A pruned turn can never be admitted again, and a `410` only leads to a
fallback when the attempt is known never to have run.

### 14. Switching from org to contributed funding

The target funds `openai` from the org; T's `openai` slot is `active` for W from the
label, an org-era grant.

1. W comments `/concurro funding openai personal`. Tx: consent revision +1, slot funding
   `contributed`, a `slot.changed {reason: funding}`. The slot adapter sends it as a
   `funding` transition, which today's exchange accepts without an epoch change.
2. Running attempts keep their `task_org` grants and recorded mode.
3. The next attempt: W's slot came from an org-era label, which isn't consent to charge W
   ([cross-cutting rules](README.md#cross-cutting-rules)). The orchestrator has no
   `task_contributed` decision for the slot, so the task waits for an explicit `sponsor`
   (`awaiting_credentials`).
4. W comments `/concurro sponsor openai`: epoch +1, a transition, and the next grant is
   `task_contributed` for W.

**Checks.** Changing the funding mode never turns earlier attribution into permission to
charge.

## Withdrawal

### 15. A withdrawal during a run

Sponsor P pays for `openai` on task T; a turn under grant G is running.

1. P comments `/concurro withdraw openai`. Comment channel: request `withdraw`. P can see
   the issue, so it's allowed.
2. Orchestrator Tx: consent revision +1, `openai` slot `withdrawn`, epoch 2 → 3,
   `slot.changed {reason: withdraw, actor: P, cutoff: 2}`, revocation job J `{task,
   provider, sponsor: P, slot epoch: 2, request}`, `request.orchestrator.applied`:
   "withdrawal recorded".
3. Job J → funding: `POST /withdrawals {job: J, request_id, ...}`
   ([finding 7](#findings)). Funding Tx: cutoff for `(T, openai, P)` raised to epoch 2,
   every grant under it revoked; recorded response. Funding publishes `funding.grant`
   (revoked) and `request.funding.blocked`: "new grants blocked".
4. Funding's slot adapter sends the withdraw change as a transition with its cutoff, and
   records the acknowledgement time and the slot's longest grant lease with J.
5. The exchange's fences now refuse new sessions and relay requests for the slot. The
   orchestrator polls the turn: `funding_ended`, then `turn.finished`.
6. Funding publishes `request.funding.stopped` when the transition's acknowledgement time
   plus the slot's longest grant lease has passed, or earlier if every turn has finished
   and none used relay custody ([finding 7](#findings)).
7. A crash anywhere repeats a step with its key: J is retried until funding
   acknowledges, the transition job until the exchange replies.
8. A delayed grant request for a new attempt, carrying epoch 2, is refused with
   `consent_cut_off`. A replay of an attempt under G returns G as `revoked`.

**Checks.** After funding acknowledges, nothing restores P's spending authority (M2's
exit test). Another provider's slot and a later sponsor are unaffected.

### 16. A relay release that fails

As trace 15, with P's credential in relay custody, before phase 5.

1. When the exchange retires the session, its `/release` call to the verifier times out;
   the exchange ignores the error (`inference.py:350`) and marks the turn lost.
2. The turn finishes. Funding doesn't count that as stopped: the turn used relay custody.
3. The verifier stops the relay worker at the session's deadline by itself. Funding
   publishes "spending stopped" once acknowledgement time plus the longest lease has
   passed ([finding 19](#findings)).

**Checks.** "Spending stopped" never depends on a release call that can fail silently.

### 17. A relay session revoked, phase 5

1. As trace 15 to funding's cutoff; `funding.grant` revoked.
2. The exchange refuses new sessions under the grant, sends the verifier `/release
   {claim}` and publishes each relay session's binding as `revoked` once the verifier
   confirms. It then publishes `exchange.grant.released {grant, sessions}`.
3. The gateway drops each binding and publishes `inference.binding.closed`.
4. Funding publishes "spending stopped" once every session in
   `exchange.grant.released` is closed and, for relay, revoked.

**Checks.** A verifier restart doesn't restore a revoked claim, and "spending stopped"
covers the relay as well as the gateway.

### 18. A binding event delayed during a withdrawal

Phase 5. The exchange binds session S2 under grant G just before G is revoked; the
`session.binding` event for S2 is delayed.

1. Funding revokes G. The gateway gets `funding.grant` revoked before it sees S2's
   binding, records G as revoked, and refuses S2's binding when it arrives, publishing
   `inference.binding.closed` for it.
2. Funding doesn't rely on having seen S2: `exchange.grant.released` lists every session
   the exchange bound to G, S2 included, so funding waits for S2's close
   ([finding 20](#findings)).

**Checks.** A delayed binding can neither spend under a revoked grant nor let funding
report "stopped" too early.

## Recovery

### 19. A crash after an uncertain dashboard write

1. Publisher consumes `task.state` revision 5 and sends a dashboard update. The
   connection drops: unknown.
2. It records the write as in flight and stops writing that surface. Revision 6 arrives
   and waits.
3. Restart: the publisher reads the dashboard comment back by marker and compares the
   rendered digest. Revision 5 was written; it publishes `publication.outcome` for it.
4. It renders revision 6 and writes it. Had revision 7 arrived meanwhile, revision 6
   would be `skipped`.

**Checks.** One write per surface at a time, the newest snapshot wins, and consumers never
see an unknown outcome.

### 20. An uncertain branch or PR write

Phase 3, M3. The publisher writes `task.branch` revision 3 for T.

1. It records the exact commit it will write, with fixed author and committer times, and
   its SHA C3. It creates the commit and fast-forwards `concurro/issue-…`. The ref
   update times out.
2. It reads the ref back with `git/matching-refs`. Only if the ref points at C3 is the
   write settled; a commit with the same tree but other ancestry isn't
   ([finding 28](#findings)). It publishes `publication.outcome {written, commit_sha: C3}`.
   Otherwise it retries the fast-forward: recreating the commit gives the same C3.
3. It creates the PR; the response is lost. It looks the PR up by head branch, finds it,
   and publishes `{written, pr_number}`.
4. The orchestrator waits for these outcomes before it reviews the commit or the PR.

**Checks.** No duplicate PR or branch, and the orchestrator never reviews a head it
hasn't seen published.

### 21. A missed webhook

1. Ingress is down while W comments `/concurro stop`. GitHub records failed delivery D2.
2. Ingress's recovery job → publisher (the Python service in phase 2):
   `GET /deliveries?cursor=`. D2 has no receipt. `POST /deliveries/D2/redeliver`.
3. D2 arrives again and is processed normally; a second redelivery would be dropped by
   its GUID.

**Checks.** A failed delivery within GitHub's retention is redelivered once and applied
once.

### 22. A gap longer than redelivery covers

Ingress is down for four days. Meanwhile: U comments `/concurro sponsor openai` on an
unenrolled issue I; W labels I; a PR task's CI completes; a comment is created and
deleted.

1. Ingress can't recover the period and publishes `ingress.coverage.gap {from, to}`.
2. For each target repository the orchestrator lists issue comments, review comments,
   issues updated since `from` with each one's events, and updated PRs
   ([finding 8](#findings)):
   - U's comment becomes a `sponsor` request with its comment source; I is claimed by
     that input.
   - W's `labeled` event becomes an `enroll` request with its timeline source.
3. Every owned task re-reads its decision inputs. The PR task sees its CI completed and
   moves on.
4. The backoffice read model refreshes the period's workflow runs and PR rows.
5. The created-and-deleted comment is not recovered, as documented.

**Checks.** Commands and sponsorship on unenrolled issues, enrollment, and CI and PR
changes survive the retention window. What doesn't is listed in
[coverage gaps](contracts.md#coverage-gaps): a deleted comment, a comment's intermediate
revisions, reactions.

### 23. A delivery and reconciliation that overlap

1. W labels I; delivery D3 arrives. The label channel reads I's timeline, but the
   `labeled` event isn't listed yet. It reads again with backoff.
2. Meanwhile a gap's reconciliation reads the timeline, finds `labeled` event E3, and
   creates `enroll {source: label:E3:0}`.
3. The channel's next read finds E3 already has a request; D3 creates nothing
   ([finding 14](#findings)).
4. W labels, unlabels and labels again quickly: three events with three IDs and three
   requests, whatever order and number the deliveries arrive in.

**Checks.** Each enrollment action becomes exactly one request, however deliveries and
reconciliation interleave.

### 24. Someone else edits a sponsor comment

1. P commented `/concurro sponsor openai` (revision 1, applied). M, a maintainer, edits
   the comment.
2. `issue_comment.edited {sender: M, comment.user: P}`. Comment channel: a new source
   revision whose editor isn't its author. The `sponsor` request is refused; nobody is
   charged, and revision 1's consent stands.
3. If the edit was only found by reconciliation and its editor can't be established, the
   source is final with `failure: unresolved_edit` and the task's payer stays pending.

**Checks.** Edits by anyone but the author never count for payment.

### 25. The phase 5 turn handover

1. The orchestrator stops submitting to the Python exchange; new attempts wait.
2. It polls the Python exchange's outstanding turns until each is finished and its final
   events are published.
3. It switches to the Rust exchange and retires the polling. Funding stops posting
   transitions to the Python exchange.
4. The Rust exchange publishes `turn.*` for the turns it runs. Turn event IDs derive from
   turn ID, type and (for status) revision, so a turn reported by both producers would
   still be deduplicated ([finding 21](#findings)).

**Checks.** No turn is lost or completed twice, and the read model keeps one history,
following the architecture's drain-then-switch order ([finding 22](#findings)).

## Commands

### 26. Stop supersedes a pending start

1. W comments `/concurro start`; before the orchestrator applies it, W comments
   `/concurro stop`. Both are pending.
2. Control requests go first: `stop` applies and supersedes the earlier `start`, which is
   recorded as `superseded`.
3. W then comments `/concurro start` again: it's after the `stop`, so it applies.
4. Each comment gets its own reply.

**Checks.** Control cancels what it undoes, and requests after it still apply.

### 27. Edited commands

1. W comments `/concurro start`; it applies. W then edits the comment, to fix a typo
   elsewhere in it or to change it to `/concurro stop`.
2. The edit is a new source revision. Its kind is ordinary and an earlier revision was
   applied, so it's refused as `already_applied` ([decisions](contracts.md#decisions)).
3. W edits the same comment into `/concurro withdraw openai`. The new revision is a
   sponsorship command, so it applies: W's consent is withdrawn ([finding 27](#findings)).
4. Had the first revision still been pending, the edit would supersede it, and the first
   would stay recorded as `superseded`.
5. P edits `/concurro sponsor openai` to `/concurro sponsor anthropic`: sponsorship
   applies per revision, as today.

**Checks.** An edit never re-runs an applied ordinary command, and sponsorship keeps
today's behavior.

### 28. A writer loses access before their request applies

1. W submits `retry` from the backoffice; the backoffice checked W's token.
2. Before the orchestrator applies it, W's write access is removed.
3. Applying it reads W's permission again: not a writer. The request is refused.

**Checks.** Authorization is checked at application, whatever the channel checked.

### 29. A task out of model calls

1. Task T has used `max_model_calls`. The next stage needs an attempt.
2. The orchestrator's budget admission refuses it before asking funding for a grant:
   stage `budget_exhausted`, a notice, no grant request.
3. The target's `max_model_calls` is raised and W comments `/concurro start`; admission
   passes and a grant is requested.

**Checks.** Call budgets are the orchestrator's alone; funding never sees an attempt the
task can't afford.

### 30. An issue transferred

1. The task's issue is transferred to another repository: `issues.transferred` with the
   new issue key.
2. The old key's task stops with reason `transferred`; writes to the old issue fail
   `not_found` and are recorded.
3. The new issue has its own key. It's a new task only if it's enrolled again.

**Checks.** One task per issue key, and a transfer never moves consent or payment to an
issue nobody enrolled.

### 31. Lists during phase 3

1. The frontend calls the backoffice API for a target's tasks.
2. The list adapter reads the Python service's issue list and its sponsorship
   projections with the viewer's token, and takes owners from `ownership.claimed`
   ([finding 23](#findings)).
3. It keeps Python entries for Python-owned issues and read-model entries for Rust-owned
   ones, then sorts and filters.
4. `/api/events` merges its read model's changes with `/lists/changes`, dropping changes
   for Rust-owned issues; a `resynchronize` reply reloads the Python lists and the
   frontend.

**Checks.** Every enrolled or sponsored issue appears once, assigned issues included, and
a non-writer still sees only their own consent.

### Also checked

- **Approving by time.** "Looks good" binds the latest plan published before the
  mention's `source_at`, so the orchestrator needs each plan notice's publication time
  ([finding 13](#findings)).

## Findings

Each is applied in [contracts](contracts.md).

1. **`source_at` for non-comment channels.** Superseded by finding 14: these requests
   now take their timeline event's time.
2. **A task can be enrolled before it's initialized.** `task.state` has no `stage` until
   initialization, and an `initialized` flag.
3. **Filling the boundary needs funding.** `POST /fills`, frozen per task and boundary,
   with `unknown` when the credential history is incomplete.
4. **The bridge orders grants and transitions.** A contributed grant is answered only
   once the exchange has acknowledged the change that set its slot epoch; until then
   funding answers `503 slot_pending`.
5. **The submit body is stored before the first call,** so a retry matches the exchange's
   digest.
6. **The exchange's ownership lookup fails closed:** `503`, retryable, nothing recorded.
7. **Withdrawals name their request,** so funding's two later steps report against it.
8. **Gaps beyond redelivery are reported and reconciled from repository listings,** not
   from known issues: comments, review comments, issue events, updated PRs, then every
   task's decision inputs.
9. **The interpreter on the Python exchange needs bridge changes:** an `interpreter` role
   and interpretation grants without a slot.
10. **Retries carry the failure they follow:** `retry_of` in the grant request.
11. **Relay release is acknowledged in phase 5:** a relay session's binding is `revoked`
    only after the verifier confirms.
12. **Ownership is published:** `ownership.claimed`, for the list adapter.
13. **Publication outcomes carry their time:** `written_at`.
14. **Enrollment requests come from the timeline,** identified by its event ID; a
    delivery only wakes the channel, so delivery and reconciliation can't double a
    request.
15. **Grants are a tagged union** (`task_contributed`, `task_org`, `interpretation`), each
    with its own payer and refusals.
16. **Python's grant is a slot:** many target grants map to one slot grant per consent
    epoch, and transitions follow consent, not attempts.
17. **Exclusions are as narrow as the evidence:** per version, or per version and model,
    with expiry, and none after an unknown outcome.
18. **Result and cost are separate:** `outcome` says whether the result is usable,
    `provider_requests` whether paid work may have happened.
19. **Before phase 5, "spending stopped" is a time bound:** the acknowledgement time plus
    the longest grant lease, since a failed relay release isn't reported.
20. **The exchange lists a revoked grant's sessions:** `exchange.grant.released`, so a
    delayed binding can't let funding report early.
21. **Snapshot event IDs include the revision,** so successive status snapshots aren't
    deduplicated as one.
22. **The handover drains before it switches,** as the architecture says.
23. **The list bridge includes sponsorships,** with the viewer's token and the change
    feed's resynchronization.
24. **Consent changes reach funding in order:** `task.<task>.slot.changed`, one per slot
    change from the funding boundary on, each naming the previous one. The slot adapter
    applies them strictly in order, from its own record of acknowledged epochs.
25. **`provider_requests: none` needs complete records:** a finished, contributed turn
    with no recorded request and none awaiting observation. Org turns are `unknown`.
26. **A `410` isn't always "never ran":** a replay returns its stored admission; a `410`
    for a never-admitted turn makes the version unusable; a `410` after the exchange
    dropped the record is `unknown`.
27. **An edit's new kind decides:** an edit into a sponsorship command applies per
    revision, whatever the earlier revision was.
28. **An uncertain branch write settles on the exact commit:** the publisher records the
    commit SHA before writing and accepts only that SHA on the ref.
29. **Pruned turns leave tombstones:** written with the deletion, checked by every
    admission, kept until the Python submission path is retired. After its admission is
    recorded, a turn is only polled, never resubmitted.
