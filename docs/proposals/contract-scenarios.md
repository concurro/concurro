# Contract scenarios

**Status: proposal, not implemented.**

These traces walk the critical flows through the [service contracts](contracts.md), step by
step, and check them against the invariants of the
[architecture review](architecture.md), [request intake](request-intake.md) and
[mentions](mentions.md). Most are adversarial: delayed messages, lost replies,
concurrent attempts and restarts. A step that needed a contract change names its
[finding](#findings), and each finding is applied in the contracts.

Notation: `A → B: message {key fields}`, for calls between processes. Phase 3's task
application is one process whose modules (intake, tasks, funding policy, attempts, the
exchange adapter, publication) share one database, so a step between them is no message.
"Tx" means one transaction in its database, outbox included, which also records the
effects the change wants; an effect is `recorded`, then `sending`, then settled. The
backoffice API is a separate, lower-privilege process: it reads a read model fed by the
task application's events and has no command path. Browser commands go to the session
authority, which signs each one and forwards it to the task application's requests API.
The traces name it the Python backoffice, as in phase 3; from phase 4 the
[accounts service](contracts.md#accounts-service) plays the same part with the same
contracts. Paths omit their
`/v1` prefix. Before phase 5, steps that reach the Python exchange go through the
[bridges](contracts.md#bridges).

The traces keep four identities apart: the task's **author** (who opened the issue), a
command's **actor** (the GitHub user who asked, by comment or through the backoffice),
the **operator** (host-only commands, recorded as the operator and never as a GitHub
user) and an attempt's **payer** (the org, the initiator or a sponsor).

## Starting

### 1. A manual start

Phase 3, M1, org funding. A opened issue I on a target; a writer W comments
`/concurro start` on it. A is the author, W the actor, the org the payer.

1. GitHub → ingress: `issue_comment.created` delivery D1. Ingress Tx: receipt and
   `github.issue_comment.created {sender: W}`.
2. The task application consumes it. Tx: the event's ID, source `comment:C:digest` final
   with one request `start {source_at: comment created_at}`.
3. The task's decision queue applies it. Task application → GitHub, with a read token:
   the issue, open, and W's permission, a writer. Tx: task T created, the request
   applied, the fork's configuration pinned as `config_sha`, the upstream base SHA and
   the requirements recorded, stage `intake`, and writes recorded for the reply, the
   dashboard, the stage label and the welcome notice.
4. Tx: attempt A admitted: the call budget checked, the payer decided (the org), a call
   charged, and A recorded with its inputs and its submit body, stored before the first
   call ([finding 5](#findings)). Its spec and prompt are stored in Postgres in the same transaction
   as their turn references and the `turn.A.submitted` outbox event ([finding 41](#findings)).
5. The exchange adapter marks the submit `sending`. Task application → exchange:
   `POST turns {turn: A, ...}`; reply `{dispatch: S}`. Tx: the reply, session S, and the
   run ref and dispatch writes.
6. Publication creates `concurro/runs/S` at `config_sha` and dispatches the launcher,
   settling each write.
7. Runner: OIDC, key, `next`, runs, posts the result.
8. The adapter polls `GET turns/A?wait=45`, recording each status change. Tx: the result,
   the intake report applied, the next stage, its publication writes and turn retirement.
9. Task application → exchange: retire A's sessions. Tx: record the acknowledged session
   IDs and release A's staffing group. Publication deletes the run refs.

**Same-group repair.** Make A's report invalid and hold retirement. The result is
visible and a repair is ready. Restart, then let the exchange retire S but drop its
reply. Across another restart no submit for the repair reaches the exchange. Replay
retirement, record the full acknowledgement and submit the repair once. Its fresh
session completes it. A different staffing group can run throughout. Repeat with a
stop while waiting: releasing the group never sends the cancelled repair.

**Checks.** Every state change and the effects it wants commit together. Retrying any
step reuses its key: request by source, attempt by ID, turn by turn ID and stored body,
write by its record. The task records A as author and W as the `start`'s actor, and
neither as payer. Label enrollment and autonomous mode arrive in M3, before the cutover,
with their requests taken from the issue's timeline ([finding 14](#findings),
[trace 22](#22-a-delivery-and-reconciliation-that-overlap)).

### 2. A standalone review requested

Phase 3, M3, before the cutover. Writer W requests a review from the reviewer account on
a PR Concurro didn't write.

1. `pull_request.review_requested {requested_reviewer: account}`. The review-request
   channel reads the PR's timeline: `review_requested` event E2 by W. Request
   `enroll {source: review_request:E2:0}`.
2. The task is `standalone_review` in mode `review`; the first verified review request is
   its funding boundary. The funding policy fills from W's credentials with provenance
   `review_request`, and the exchange accepts it
   ([contracts](contracts.md#python-exchange)).
3. W removes and re-adds the request: two more timeline events, each its own request.
   The removal unenrolls; the re-request enrolls again and starts a new round, as today.
4. Publication posts the review with a reviewer account's user grant and reads it back:
   its author is the configured numeric reviewer ID ([finding 37](#findings)). Before
   the cutover that's an independent test reviewer account and grant; the production
   grant moves to the task application only at the cutover, after the Python service has
   stopped, since its refresh token is single-use ([finding 44](#findings)). The cutover
   check refreshes the grant and reads a published review's author.

**Checks.** Contributed standalone reviews aren't refused by the exchange, a re-request
is a new source rather than a duplicate of the first, and a review is never published
under another account.

### 3. A mention that enrolls and starts

After the cutover. Mentions are new: the Python service has none, so deferring them
drops no existing workflow; until they land an `@account` mention does nothing, as
today. An interpreter is configured; W writes "@account plan and implement this" on an
unenrolled issue. Before phase 5.

1. Mention channel. Tx: task T, source pending `{comment: C, digest}` with
   interpretation attempt I, and a write for the `eyes` reaction as the account.
2. Tx: I admitted with payer W; the funding policy selects W's interpreter credential.
   The interpreter runs as a turn on the Python exchange, with an `interpreter` role and
   no slot transition ([finding 9](#findings)).
3. Result: `plan_and_implement`. Tx: source final with `enroll` (index 0) and `start`
   (index 1), the interpretation frozen in both.
4. Both apply; publication sends one reply once both have outcomes.
5. Crash after the call: on restart I is pending, so the adapter polls turn I and
   finalizes from its stored result. If the result is gone (the Python exchange keeps
   finished turns for a day) and the deadline passes, the source is final with
   `failure: interpreter_deadline`, and the reply asks for the exact command. Nothing is
   paid again.

**Checks.** One request per source and index, one reply per source, a model never
authorizes, and an interpretation is never repeated or recomputed.

### 4. A mention without an interpreter credential

1. As trace 3 to step 1. The funding policy finds no interpreter credential for W:
   `no_eligible_credential` ([finding 15](#findings)).
2. Tx: source final with `failure: interpreter_unavailable` and no requests. The reply
   says only exact mentions work and gives the command.
3. The task, if one exists, doesn't enter `awaiting_credentials`.

**Checks.** A missing interpreter credential takes the deterministic path and never
blocks a task.

## Personal credentials

Traces 5 to 14 apply from M3, which brings personal credentials before the cutover,
since sponsorship is an existing team workflow; M1 and M2 are org-funded. Traces 15 and
16 are phase 5's. The tombstone steps of trace 11 and the result rules of trace 10 apply
to org turns from M1.

### 5. A crash between credential selection and the submit

1. Trace 1 with contributed funding: attempt A admitted under W's consent, its credential
   selected and its submit body stored; the task application crashes before the submit.
2. On restart A's submit is `recorded`, never sent, so it's sent with the stored body.
3. Had it crashed after marking the submit `sending`, the resubmit of the same body gets
   the original admission back, since the exchange keys on the turn ID and the body's
   digest.
4. If W withdrew meanwhile, A is under the cutoff and isn't submitted.

**Checks.** A replay keeps its payer and credential. The digest only matches because the
body is stored before the first call, not rebuilt ([finding 5](#findings)).

### 6. Concurrent attempts under one slot

Task T's `openai` slot is active for P at epoch 4. Two reviewers start at once.

1. Attempts A and B are admitted, and the funding policy selects a credential for each
   under slot epoch 4.
2. The slot adapter maps both to the slot grant `sha256(T, openai, 4)`. The epoch 4
   transition was acknowledged when P sponsored, so neither waits
   ([finding 16](#findings)).
3. A's credential is then replaced by a new version; C's selection takes it. Same slot,
   same Python `grant_id`; only the admission's credential version differs, and the
   exchange checks that version separately.
4. Had the epoch 4 change been unacknowledged, A's and B's submits would wait for it; a
   lost transition reply is retried with the same body and returns the stored one.
5. P withdraws (epoch 5, withdrawn), then sponsors again (epoch 6): two slot changes,
   each naming the previous one, sent as two transitions, each one epoch higher.
   Attempts under epoch 6 map to a new slot grant.
6. Each reviewer seat has its own current attempt, so A and B are both current. B
   finishes first, then A: both results apply to their seats. Had a `stop` come first,
   it would cancel both, and neither late result would change anything
   ([finding 36](#findings)).

**Checks.** Every attempt under one consent maps to one Python slot, the exchange's
"epoch + 1 or unchanged" rule is never broken, renewed consent is a new slot grant, and
parallel reviewers don't make each other stale.

### 7. Consent changes before the first attempt, with a lost reply

Manual task T. Before anyone starts it, P sponsors `openai`, withdraws and sponsors again.
Then W starts it.

1. Before the boundary, no slot change is recorded: the three comments are sponsorship
   facts folded at the boundary.
2. W's `start` freezes the boundary. The fold leaves P active, so the first slot change
   is `initialize` at epoch 1 for P. The exchange never sees an epoch 3 it can't place
   ([finding 24](#findings)).
3. P then withdraws (epoch 2) and sponsors again (epoch 3): two more changes, revisions
   r2 and r3, each naming the previous one. r3's transition waits until r2's is
   acknowledged.
4. The adapter sends r2 with `expected_epoch` 1, from its own record. The reply is lost.
   It resends the same body and gets the stored reply. Then it sends r3 with
   `expected_epoch` 2.
5. A submit at epoch 3 waits until r3 is acknowledged.

**Checks.** The adapter needs nothing but its own record and the exchange's replies; a
lost reply or a job run out of order can't skip or repeat an epoch.

### 8. A turn submitted before its sponsorship is recorded

1. A bug sends `POST turns {turn: A}` before the exchange has the slot row.
2. The exchange has no active slot matching the admission and refuses with `409`. It
   never takes the grant from the submitted admission.
3. The adapter retries the submit, which the exchange accepts once the transition lands.
   In normal operation this doesn't happen: a contributed submit waits until its
   transition is acknowledged ([finding 4](#findings)).

**Checks.** The refusal keeps payment safe; the ordering keeps it from happening in
normal operation.

### 9. A credential fails and another is tried

1. Turn A finishes `failed`, reason `provider_error`, with
   `provider_failure {category: auth}` and `provider_requests: some`.
2. Tx: attempt B admitted with `retry_of {attempt: A, failure {category: auth, model}}`;
   the funding policy excludes that version for T and selects the next eligible entry
   in the same transaction ([finding 10](#findings)).
3. Turn C later fails with `model_denied` for model M on version V2. The exclusion is
   `(V2, M)`: an attempt for model N may still use V2 ([finding 17](#findings)).
4. Turn D's outcome is `unknown`, `provider_requests: unknown`. No `retry_of` is
   recorded; the uncertainty limit applies, and nothing is excluded.

**Checks.** Fallback stays within the same sponsor's eligible credentials, exclusions are
as narrow as the evidence, and an unknown outcome never switches accounts.

### 10. An unusable result after paid work

1. The runner gets a response that isn't valid JSON. Today that's outcome `failed`
   (`engine/turns.py:273`), after the provider has been called.
2. The adapter records `outcome: failed, reason: invalid_output, provider_requests:
   some` ([finding 18](#findings)).
3. Retrying is a new attempt: admitted against `max_model_calls` and charged like any
   other. Nothing treats `failed` as "never happened".
4. The same failure on an org-funded turn: the exchange records no org requests, so an
   empty record proves nothing, and `provider_requests` is `unknown`. A contributed turn
   that finished with no recorded request and none awaiting observation is `none`
   ([finding 25](#findings)).

**Checks.** An unusable result can't be mistaken for free, unexecuted work, and `none` is
only claimed where the exchange's records cover the turn.

### 11. A replay after a credential replacement

Attempt A was admitted under P's version 1. P then replaces the secret: version 2.

1. A's first submit lost its reply, so after a restart the adapter resends A's stored
   body. The exchange finds A's admission and returns it before looking at current
   metadata (`exchange.py:1305`); A keeps version 1. The adapter records the reply and
   its `dispatch`, and from then on only polls A.
2. Attempt B, never admitted, was built with version 1 before the replacement. Its first
   submit gets the credential `410`: B never ran. The funding policy treats version 1 as
   unusable for T's new attempts, and B's retry selects version 2.
3. Two days later the exchange has pruned A, leaving a tombstone. Polling A gets `404`:
   if A's result was already recorded, that result stands; otherwise A is `unknown`, and
   nothing falls back automatically ([finding 26](#findings)).
4. The same with P's credential unchanged, and a resubmit of A from a bug or an old
   recovery job: without the tombstone the exchange would admit A again and could repeat
   paid work. With it, the same digest gets `410 turn_expired`, a changed body `409`,
   and A stays `unknown` ([finding 29](#findings)).
5. Turn X was cancelled before it was ever submitted, then pruned. Its tombstone has no
   digest, and any later submit with X's ID is refused.

**Checks.** A pruned turn can never be admitted again, and a `410` only leads to a
fallback when the attempt is known never to have run.

### 12. Switching from org to contributed funding

The target funds `openai` from the org; T's `openai` slot is `active` for W from an
org-era `start`.

1. W comments `/concurro funding openai personal`. Tx: consent revision +1, slot funding
   `contributed`, a slot change with `reason: funding`, sent as a `funding` transition,
   which today's exchange accepts without an epoch change.
2. Running attempts keep their org payer and recorded mode.
3. The next attempt: W's slot came from an org-era source, which isn't consent to charge
   W ([cross-cutting rules](README.md#cross-cutting-rules)). The funding policy has no
   contributed decision for the slot, so the task waits for an explicit `sponsor`
   (`awaiting_credentials`).
4. W comments `/concurro sponsor openai`: epoch +1, a transition, and the next attempt
   is paid by W.
5. Another issue was labeled by writer L while the target was org-funded, and the label's
   delivery is only processed after the switch. Its source's mode is the policy
   activation covering the label's time with the task's configuration digest: `org`. The
   slot records `org`, so no implicit personal fill happens and no grant is issued until
   an explicit `sponsor` ([finding 33](#findings)).
6. Meanwhile W `resume`s an older task pinned to the target's configuration from before
   the switch. Reading that pinned SHA activates nothing: only a read of the current
   configuration at the fork's head records an activation, so a later source's mode
   stays `contributed` (`service/worker.py:345-347`, [finding 42](#findings)).

**Checks.** Changing the funding mode never turns earlier attribution into permission to
charge, however late the earlier source is processed.

### 13. A withdrawal during a run

Writer W started task T; sponsor P pays for `openai`; a turn under P's consent is
running. W is the `start`'s actor, P the payer and the `withdraw`'s actor.

1. P comments `/concurro withdraw openai`. Comment channel: request `withdraw`. P can see
   the issue, so it's allowed.
2. Tx: consent revision +1, `openai` slot `withdrawn`, epoch 2 → 3, the cutoff for
   `(T, openai, P)` raised to epoch 2, every attempt under it marked revoked, a withdraw
   transition with its cutoff recorded, and the request applied with its replies
   ("withdrawal recorded", "new attempts blocked") ([finding 7](#findings)).
3. The adapter sends the transition, and records the acknowledgement time and the slot's
   longest grant lease.
4. The exchange's fences now refuse new sessions and relay requests for the slot. The
   adapter polls the turn: `funding_ended`, then finished.
5. When the acknowledgement time plus the slot's longest grant lease has passed, or
   earlier if every turn has finished and none used relay custody, Tx: the "spending
   stopped" reply ([finding 7](#findings)).
6. A crash anywhere repeats a step with its key: the transition is retried until the
   exchange replies.
7. A delayed attempt carrying epoch 2 is refused at admission by the cutoff, in the same
   transaction. A replay of an attempt under the cutoff isn't submitted.

**Checks.** Once the withdrawal commits, nothing restores P's spending authority.
Another provider's slot and a later sponsor are unaffected.

### 14. A relay release that fails

As trace 13, with P's credential in relay custody, before phase 5.

1. When the exchange retires the session, its `/release` call to the verifier times out;
   the exchange ignores the error (`inference.py:350`) and marks the turn lost.
2. The turn finishes. The task application doesn't count that as stopped: the turn used
   relay custody.
3. The verifier stops the relay worker at the session's deadline by itself. "Spending
   stopped" is replied once acknowledgement time plus the longest lease has passed
   ([finding 19](#findings)).

**Checks.** "Spending stopped" never depends on a release call that can fail silently.

### 15. A relay session revoked, phase 5

1. As trace 13 to the cutoff. The task application revokes grant G and records the
   exchange effect `POST /v1/grants/G/revoke`.
2. The exchange marks G revoked, so any admission under it is refused from now on. It
   ends G's sessions and queues the release of each session's relay claim with the
   verifier, through the `release` credential.
3. The verifier persists each claim's revocation, ends its leases and acknowledges.
4. The exchange answers the revoke with G's sessions. The task application settles the
   effect.
5. The gateway's next session check finds those sessions no longer live and closes any
   stream still open.
6. "Spending stopped" is replied at the acknowledgement plus that 10-second bound.

**Checks.**

- A verifier restart doesn't restore a revoked claim.
- A request that reaches the gateway after step 2 is refused at admission.
- "Spending stopped" covers the relay as well as the gateway.

### 16. A request racing a revocation, phase 5

The gateway admits request R under grant G just before G is revoked; R's stream is
still open when the revoke is acknowledged.

1. R's admission came before the revocation, so the exchange recorded R as G's.
2. The revoke ends R's session. Within 10 seconds the gateway's session check closes R's
   upstream stream, and R's finish reports the usage spent until then.
3. A second request from the same runner, arriving after the revocation, is refused at
   admission with `grant_revoked`.

**Checks.** No request is admitted under a revoked grant, and R's usage before the close
is recorded against G. The bound is the documented one, not an event's delivery time.

## Recovery

### 17. A crash after an uncertain dashboard write

1. Publication records a dashboard write for task state revision 5, marks it `sending`
   and sends it. The connection drops: uncertain.
2. The dashboard's next write, for revision 6, waits.
3. Restart: the write is `sending` with no response, so publication reads the dashboard
   comment back by marker and compares the rendered digest. Revision 5 was written; the
   write is settled.
4. It renders revision 6 and writes it. Had revision 7 arrived meanwhile, revision 6
   would be `skipped`.

**Checks.** One write per surface at a time, the newest snapshot wins, and nothing acts
on an unsettled write.

**Delayed label and ref writes (M1).** Hold the initial create of a live session's run
ref at the HTTP boundary. Read absence; let a retry create the ref and dispatch once.
Retire the session and observe the ref deleted. Restart, then let the original create
land. Without a webhook the publisher discovers the recreated ref and deletes it again.
Assert the ref is absent and there is still only one dispatch. Repeat with a managed
label: delay the first add until after its retry and a newer removal, then reverse the
add/remove order. The latest desired membership wins and unrelated labels are unchanged.
A send whose outcome is unknown is never recorded as skipped.

### 18. A comment create whose readback runs first

1. Publication records the welcome notice, marks it `sending` and posts it; the response
   is lost. GitHub hasn't finished creating the comment.
2. The readback lists the issue's comments and finds no marker. The create stays
   pending; it isn't resent.
3. The next readback finds the comment with this write's marker, and the write is
   settled as written.
4. Had the comment never appeared, it would stay pending in `effects pending` until the
   operator ran `writes resolve`, resending it or settling it as `failed`
   ([trace 35](#35-an-operator-settles-a-pending-write)).
5. Had the task application crashed before marking the write `sending`, it was never
   sent, and it's sent after the restart.

**Checks.** An absent comment never proves an uncertain create failed, so no comment is
posted twice.

### 19. An uncertain branch or PR write

Phase 3, M2. Publication writes the task's branch revision 3 for T.

1. It records the exact commit it will write, with fixed author and committer times, and
   its SHA C3. It creates the commit and fast-forwards `concurro/issue-…`. The ref
   update times out.
2. It reads the ref back with `git/matching-refs`. Only if the ref points at C3 is the
   write settled; a commit with the same tree but other ancestry isn't
   ([finding 28](#findings)). Tx: the write settled `{written, commit_sha: C3}`.
   Otherwise it retries the fast-forward: recreating the commit gives the same C3.
3. It creates the PR; the response is lost. It looks the PR up by head branch, finds it,
   and settles the write `{written, pr_number}`.
4. The review stage waits for these outcomes before it reviews the commit or the PR.

**Checks.** No duplicate PR or branch, and no review of a head that isn't written.

### 20. A restart between a submit and its reply

1. Attempt A's submit is `sending`; the task application is killed before the reply is
   recorded.
2. On restart the submit is uncertain. The adapter resends A's stored body; the exchange
   finds A by turn ID and digest and returns its admission and any pending `dispatch`.
3. Tx: the reply and session. One turn runs, and one dispatch is written for the session.
4. Had the exchange pruned A meanwhile, the tombstone answers `410 turn_expired` and A is
   `unknown` ([finding 29](#findings)).

**Checks.** A restart at any point leaves one exchange turn and one dispatch per session.

### 21. A stop after a polling outage

1. Turn A is running. The exchange can't be reached for more than 20 consecutive polls:
   A's attempt is `unknown`, but the turn isn't cancelled, and it stays live.
2. W comments `/concurro stop`. Tx: stage `stopped`, and a cancel for every turn that may
   be live, A included, whatever its attempt's outcome.
3. Task application → exchange: cancel A. The exchange cancels it, ends every session
   allocated to it and returns their IDs. Tx: those sessions ended; publication deletes
   their run refs.
4. A's late result is recorded and changes nothing.

**Checks.** `stop` reaches every turn that may still run, not only open attempts, and a
session a lost reply would have named ends too.

### 22. A delivery and reconciliation that overlap

Phase 3, M3, before the cutover.

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

### 23. A missed webhook

Phase 3, M3, before the cutover.

1. Ingress is down while W comments `/concurro stop`. GitHub records failed delivery D2.
2. Ingress's recovery job → the task application's delivery recovery API (the Python
   service's before the cutover): `GET /deliveries?cursor=`. D2 has no receipt.
   `POST /deliveries/D2/redeliver`.
3. D2 arrives again and is processed normally; a second redelivery would be dropped by
   its GUID.

**Checks.** A failed delivery within GitHub's retention is redelivered once and applied
once.

### 24. A gap longer than redelivery covers

Phase 3, M3, before the cutover. Ingress is down for four days. Meanwhile: W comments
`/concurro start` on issue I; a PR task's CI completes; a comment is created and
deleted.

1. Ingress can't recover the period and publishes `ingress.coverage.gap {from, to}`.
2. For each target repository the task application lists issue comments, review
   comments, issues updated since `from` with each one's events, and updated PRs
   ([finding 8](#findings)). W's comment becomes a `start` request with its comment
   source.
3. Every active task re-reads its decision inputs. The PR task sees its CI completed and
   moves on.
4. The created-and-deleted comment is not recovered, as documented.

**Checks.** Commands, and CI and PR changes, survive the retention window. What doesn't
is listed in [coverage gaps](contracts.md#coverage-gaps): a deleted comment, a comment's
intermediate revisions, reactions.

### 25. Commands around the cutover

1. The operator announces the window. While the Python service still runs normally, they
   finish or `/concurro stop` outstanding tasks and settle unknown writes with `resolve`
   until it's quiescent, then stop the whole daemon. The stop signal starts a drain, and
   a running tick finishes after it (`service/scheduler.py:94-100`); once the exit is
   confirmed, they set the activation time `A`. The final registry, read with
   `Registry.inspect`, and the exchange show no queued or running turns and no live
   sessions ([cutover](architecture/phase-3-plan.md#cutover)). They record the stream's
   incarnation, `S` (the last sequence the service's consumer acknowledged), and the
   exchange's generations and slot epochs for the issues the service used.
2. W commented `/concurro approve <digest>`, with H's current plan digest (Python's
   grammar requires it, `engine/commands.py:63-70`), on issue H shortly before `A`.
   Python read H's comments directly and applied it (`engine/controller.py:2411`),
   recording W's comment ID in `approval.comment` (`engine/commands.py:227-238`), but the
   comment's
   first delivery was delayed and reaches the stream after `S`. The task application's
   consumer, starting at `S + 1`, reads it: the comment is dated before `A`, so it's
   history, and nothing applies twice.
3. Y commented `/concurro stop` on issue L during the drain, after the stop was
   requested. The tick that was still running read it and applied it before the daemon
   exited. It's dated before `A`, which was set after the exit: history for the task
   application. The registry's outcome is per issue, so it shows the task stopped, not
   that this command stopped it. The evidence is L's checkpoint: stage `stopped` with
   `command_id` equal to Y's comment ID, which the stop handler records
   (`engine/commands.py:373-379`). So it isn't reissued. Without that evidence, the stop
   would be ambiguous, handled as M's is below.
4. X commented `/concurro start` on issue K a minute before `A`, despite the quiet
   window; Python never read it. The operator lists the sources dated strictly before `A`
   within the window, `/concurro` comments and standalone review requests included, and
   checks each against evidence specific to it: Python's reply, the writes it caused,
   K's dashboard and labels. The final registry has no row for K at all, so no task was
   ever created: positive evidence the `start` was missed. The operator asks X to comment
   again. The old comment is never applied implicitly.
5. On issue M, Z commented `/concurro approve <digest>` with M's current plan digest,
   then `/concurro stop`, within the window. M's registry row shows only the latest
   outcome and command cursor, which can't say what happened to each. M's
   `approval.comment` holds the approval's comment ID, so it applied. For the stop there
   is neither a reply nor other evidence either way: it's ambiguous. The task application
   takes no automatic action on it, and Z is told the stop may have acted and decides
   afresh whether to stop again.
6. W comments `/concurro start` on issue I after `A` but before the task application
   starts. It isn't on the operator's list, which covers only sources before `A`.
   Ingress publishes it; once the task application starts, it applies once.
7. Message `S` is an `ingress.coverage.advanced` event, with no GitHub delivery behind
   it. The consumer starts at `S + 1` all the same: the stream position only says where
   reading starts, and the source rule decides what counts.
8. Issue J was labeled `concurro` a month ago. A comment on J wakes the label channel,
   which reads J's timeline and finds that `labeled` event, dated before `A`: history,
   no request ([contracts](contracts.md#cutover-sources)).
9. Ingress re-maps an old delivery: the new event's source is still dated before `A`,
   so it's history too.

**Checks.** One rule, the original GitHub source's time against `A`, set after the
daemon's exit is confirmed, decides for live delivery, re-mapping, replay and
reconciliation, whatever order the forwarder publishes in. Reissuing covers only sources
strictly before `A` with positive evidence that Python missed them, and sources at or
after `A` are never reissued, so no command acts twice. A command whose outcome stays
ambiguous is left to its author, told it may have acted, and the task application takes
no automatic action on it ([findings 34, 48 and 52](#findings)). The rehearsal covers
steps 2 to 6: no command acts twice, the missed `start` is reissued, and the ambiguous
stop gets no automatic action.

#### Archive import rehearsal

1. Start with a populated, caught-up backoffice read model containing a test turn.
   Stop Python and keep the Rust controller and its ingress consumer off. Run the
   import with only its archive uploader. Two Python turns in one harness scope
   share serving runner S and run `77/2`. The first dispatched S and completed turn 0.
   The second requested turn 1 and failed before updating the journal. Its returned
   archive still contains the complete journal for turn 0.
   It ran warm with no new dispatch and inherited S and a replacement candidate in
   `runner_sessions`. Supply just the SQLite observations
   and archives to the importer. Include a legacy turn with staffing IDs, no serving
   assignment and no archive. Include a retained session file, two retained-only
   scopes, an unlinked diagnostics file, one tar encoded two ways and one gzip
   shared by multiple sources. Capture the live turn outbox before import.
2. Include the M1 escaped-NUL fixture and two members packed as `a.jsonl` then
   `z.jsonl`, with `z` older. At commit SQL finds the terms on both sides of the NUL
   and the neighboring messages at their source positions. The fallback's raw bytes
   match the input. Both turns and the retained session resolve to their expected
   snapshots. The failed Python turn keeps its ID, requested ordinal 1 and link to
   the returned gzip with completed ordinal 0. Import and replay complete with both ordinals
   intact. For the first retained-only scope, a matching complete `turn.json`
   with `turn: 0` yields ordinal zero. For the second, a pending journal yields an
   unknown ordinal. Both retain their source paths and have no Python turn ID.
   Give each unlinked source a unique search term. The diagnostics file retains its
   path and indexed entries.
3. Make the object store unavailable through retry exhaustion, interrupt the import, then restart it with
   the controller still stopped. Search and v1 restore work from the pending bytes.
   Restore object-store access and requeue the exhausted digest. The import's uploader verifies
   every inventoried object's size and SHA-256 before the controller starts. Restore
   the session from its verified object. Both tar encodings share event rows and have
   distinct object keys. Identical gzip bytes share a key. Rerunning changes no row,
   link or object counts and creates no runnable tasks. Compare the live turn outbox
   after import and again after replay with the initial capture. Neither operation
   added a submitted, status or finished event, including for linked historical turns.
4. Follow the cutover runbook with the existing read model and consumer in place.
   Start with test turn revision 4. Read the first baseline page to pin its snapshot.
   Only then commit revision 5 and deliver its event to the backoffice consumer
   while the final page is held back. Confirm receipt, release the final page and
   finish the refresh. The baseline contains revision 4. At the frontend switch
   the test turn must show revision 5, applied once, and a writer must be able to
   open the imported failed turn and its transcript without a manual rebuild.
   Both served turns appear under `77/2` with their original deployment,
   repository, issue, generation, turn IDs and harness session links. Both retain
   their recorded staffing sets. Neither
   gains a per-turn dispatch from that set. The replacement candidate never appears
   as the warm turn's serving runner, and the legacy turn has no serving assignment.
   Opening a linked turn returns `z` before `a` with stable line and part order.
   A reader cannot see the transcript. For the retained-only and unlinked sources,
   even a writer sees no entries through the backoffice, and a viewer-scoped search
   for their unique terms returns no matches. Missing or corrupt archives remain
   identified for repair. An empty inventory completes.

[Import contract](contracts.md#python-archive-import)

### 26. Someone else edits a sponsor comment

Phase 3, M3, before the cutover.

1. P commented `/concurro sponsor openai` (revision 1, applied). M, a maintainer, edits
   the comment.
2. `issue_comment.edited {sender: M, comment.user: P}`. Comment channel: a new source
   revision whose editor isn't its author. The `sponsor` request is refused; nobody is
   charged, and revision 1's consent stands.
3. If the edit was only found by reconciliation and its editor can't be established, the
   source is final with `failure: unresolved_edit`. Revision 1's applied consent stands,
   and the next attempt proceeds under it. An unresolved edit made before the funding
   boundary's time leaves the boundary pending instead.

**Checks.** Edits by anyone but the author never count for payment.

### 27. The phase 5 cutover's turn handover

1. The window is announced. No task is running or queued, every attempt is settled, and
   `effects pending` is empty.
2. The Python exchange has no queued or running turn and no live session. The task
   application stops.
3. The task application starts in Rust mode. Its next submits go to the Rust exchange,
   which publishes `exchange.turn.*`.
4. No Python-era turn is resubmitted: none was unsettled, and stored submit bodies of
   settled attempts are never replayed.

**Checks.**

- No turn spans the two exchanges, so none is lost or completed twice.
- Turn event IDs derive from the turn ID, type and revision, so a redelivered event
  changes nothing ([finding 21](#findings)).

## Commands

### 28. Stop supersedes a pending start

1. W comments `/concurro start`; before it's applied, W comments `/concurro stop`. Both
   are pending.
2. Control requests go first: `stop` applies and supersedes the earlier `start`, which is
   recorded as `superseded`.
3. W then comments `/concurro start` again: it's after the `stop`, so it applies.
4. Each comment gets its own reply.

**Delayed delivery.** On a running task, withhold a writer's start at T1. Apply a stop at
T2 and restart the application with no pending control. Deliver the T1 source. Its
outcome names the writer and is `superseded`. The task stays stopped, its budget is
unchanged and no submit is queued. A fresh start at T3 applies. Repeat with a delayed
`retry` and `resume` when M2 supports them. A refused stop creates no cutoff.

### 29. Edited commands

1. W comments `/concurro start`; it applies. W then edits the comment, to fix a typo
   elsewhere in it or to change it to `/concurro stop`.
2. The edit is a new source revision. Its kind is ordinary and an earlier revision was
   applied, so it's refused as `already_applied` ([decisions](contracts.md#decisions)).
3. From M3, W edits the same comment into `/concurro withdraw openai`.
   The new revision is a sponsorship command, so it applies: W's consent is withdrawn
   ([finding 27](#findings)).
4. Had the first revision still been pending, the edit would supersede it, and the first
   would stay recorded as `superseded`.
5. From M3, P's applied `/concurro sponsor openai` is edited by P into
   `/concurro sponsor anthropic`. The new revision applies: P now sponsors Anthropic,
   and OpenAI stays sponsored by P until P withdraws it. This is a deliberate behavior
   change: today's service ignores every later revision of a source it has adopted
   (`service/shadow.py:1197-1199`), so the edit would change nothing
   ([decisions](contracts.md#decisions), [finding 46](#findings)).

**Checks.** An edit never re-runs an applied ordinary command; sponsorship is decided
per revision, and an edit adds consent without silently removing what an earlier
revision gave.

### 30. A writer loses access before their request applies

1. W submits `retry` from the backoffice. The Python backoffice signs a command proof
   for W and that exact request. Backoffice API → task application:
   `POST /requests {kind: retry, proof}`; the request is recorded with W, from the proof,
   as actor.
2. Before it's applied, W's write access is removed.
3. Applying it reads W's permission again: not a writer. The request is refused, and the
   backoffice API reads the refusal by request ID.
4. The same with a comment: the request is refused with a reply.

**Checks.** Authorization is checked at application, whatever the channel checked.

### 31. A task out of model calls

1. Task T has used `max_model_calls`. The next stage needs an attempt.
2. Admission refuses it in its transaction: stage `blocked` with today's reason
   (`engine/controller.py:682`), a notice, nothing selected or submitted.
   `budget_exhausted` is a different stop: a session that spent its token budget.
3. The target's `max_model_calls` is raised and W comments `/concurro resume`, which
   adopts the fork's current configuration (`engine/commands.py:257`); a `start` would
   keep the task's pinned `config_sha`. Admission passes and the attempt is submitted.

**Checks.** The call budget is checked in the transaction that decides the payer; no
credential is selected for an attempt the task can't afford.

With budget available, request a continuation requiring a known-corrupt revision.
Repeat with a readable archive whose journal is pending: its message is searchable,
and the runner cannot resume it. Exercise both with org funding and an eligible
sponsor (M3). In each case the refusal identifies the required revision. After
restart and replay there is no new attempt, grant, reservation, call charge or
submit effect. No credential lookup or exchange submission occurred.

### 32. An issue transferred

1. The task's issue is transferred to another repository: `issues.transferred` with the
   new issue key.
2. The old key's task stops with reason `transferred`; its live turns are cancelled, and
   writes to the old issue fail `not_found` and are recorded.
3. The new issue has its own key. It's a new task only if a writer starts it.

**Checks.** One task per issue key, and a transfer never moves consent or payment to an
issue nobody started.

### 33. A refused backoffice command

Phase 3, M3. Task T, started by writer W, is at `planned`. V can see the repository but
isn't a writer.

1. V clicks approve. The browser posts it to the Python backoffice, which checks CSRF
   and V's session token, and refuses to sign a proof: V isn't a writer. Nothing reaches
   the task application.
2. Suppose V's write access was removed only after the proof was signed. Python
   backoffice → task application: `POST /requests {kind: approve, proof}` with an
   idempotency key. Tx: the request recorded with actor V, taken from the proof.
3. Applying it reads V's permission: not a writer. Tx: the request `refused`, reason
   `not_writer`. Nothing else changes: T's stage, plan binding and consent revision, and
   no GitHub write is recorded for T.
4. The backoffice API shows the refusal from its `request.tasks.refused` event, naming V.
   A retry with the same key returns the same refusal.
5. Something on the platform network replays W's earlier `approve` proof on a `stop`
   request, then V's `withdraw` proof on a `stop`, then W's `retry` proof with different
   `detail.feedback`: each `403 proof_mismatch`, recorded and changing nothing.
6. While V browses, the edge strips V's session cookie from requests to `/api/v2/*`; the
   backoffice API sees only V's viewer token. With it, and its own read-scoped
   credential, it can ask who V is and V's tiers. Posted to the Python backoffice's
   command handler, the viewer token is refused (the handler requires the session cookie
   and its CSRF header), and the requests API refuses anything without a proof the
   command handler signed ([findings 43 and 51](#findings)).

**Checks.** A refused command records only itself; the actor comes from a proof only the
session authority signs, for a command the browser posted to it, bound to the exact
request, and authorization happens at application. The credentials actually delivered to
the backoffice API authorize no command through either the Python backoffice or the task
application.

### 34. A viewer sees only their entitlement

Phase 3, M3. On one target, writer W started T and U; P sponsored `openai` on T; V, who
can see the upstream but not the fork and isn't a writer, sponsored `anthropic` on U.
Former writer F sponsored `openai` on U, later replaced by P; S sponsored on issue K,
which nobody has started.

1. The backoffice API's read model follows the task application's events (`task.*`,
   `request.*`, `consent.*`, `turn.*` and the target index), delivered through its
   outbox and the bus.
2. V opens the target's task list. The backoffice API resolves V's session through the
   Python backoffice and V's tier for the target: `reader`.
3. V sees T and U as summaries: issue, title, stage and reason, mode, PR link, updated
   time. Of the consent, V sees only their own `anthropic` consent on U.
4. V opens T's page and asks for one of its turn transcripts: refused, since full task
   views and every turn and blob read need `writer` (upstream write access, and both the
   upstream and the fork visible), as today ([finding 31](#findings)).
5. W, a writer who can see the fork, sees both tasks in full, with every sponsor.
6. F's "my sponsorships" shows F's replaced `openai` consent on U, though F is no longer
   a writer or the payer; S's shows the consent on K, which has no task. Both come from
   `consent.*` facts by their numeric ID ([finding 39](#findings)).

**Checks.** A viewer sees only what their tier entitles them to, from the read model,
without the backoffice API holding GitHub credentials, and each user sees their own
consent history.

### 35. An operator settles a pending write

Phase 3, M1. Task T was opened by A and started by W, with P paying.

1. T's welcome notice is a pending comment create: its marker never appeared
   ([trace 18](#18-a-comment-create-whose-readback-runs-first)).
2. On the host, the operator checks the issue, sees no comment, and runs
   `writes resolve <write> --failed`. Tx: the write settled `failed`, with actor
   `operator` and the host command as evidence; nothing is sent, and no GitHub user is
   recorded as having acted.
3. GitHub finishes the first create late: a comment carrying the write's marker appears.
   Publication adopts it as written, so there's still one notice.
4. Had the operator run `--resend` instead, the write would be recorded as the
   operator's explicit override, and a late first create would leave two notices: the
   command's help says so ([finding 40](#findings)).
5. The dashboard still names W as the `start`'s actor and P as payer; the operator's
   action appears only in the task's records.

**Checks.** Author, actor, operator and payer stay distinct, a host command is never
attributed to a GitHub user, and only an explicit override can duplicate a comment.

### 36. A withdrawal while the fill is outstanding

Phase 3, M3. Writer W starts T; the boundary is ready and the fill job is reading W's
credentials from the vault.

1. The fill job persisted the candidate boundary, the folded sources' versions, the
   issue's source revision and the consent revision when it started.
2. Sponsor P's earlier `withdraw` arrives late: a new source, raising the issue's source
   revision and then the consent revision when it applies.
3. The vault answers. The transaction that would apply it finds the source and consent
   revisions changed, discards the answer and recomputes the fill
   ([finding 32](#findings)).

**Checks.** A delayed vault reply never restores consent that changed while it was
outstanding.

### 37. A Python-era issue started again

After the cutover. Issue I had a Python task, which reached generation 3 at the exchange
and an `openai` slot at epoch 2.

1. At the cutover the operator exported I's generation (3) and slot epochs (`openai`: 2).
2. W starts I with personal funding for `openai`. The new task takes generation 4; its
   first `openai` transition expects epoch 2 and sets epoch 3, with W as the payer from
   the new boundary. The exchange accepts it, since it requires the replacement slot's
   epoch to follow the one it holds (`exchange.py:783-788`), and the contributed
   attempt is submitted ([finding 45](#findings)).
3. The old task's consent isn't carried over: the new slots start from the new task's
   own boundary.

**Checks.** A fresh task on a previously used issue isn't refused by the exchange's
generation or epoch fences, tested with the personal-slot transition itself, which an
org-funded intake never sends ([finding 35](#findings)). This case is mandatory in the
rehearsal, against a test exchange holding a historical slot. At the live cutover it
runs only when the export holds a slot; otherwise the operator confirms the export is
empty and that a first slot initializes normally ([finding 56](#findings)).

### 38. An empty backoffice read model

Phase 3, M3. The backoffice API's database is empty. A target has an inactive PR and a
workflow run that finished three weeks ago, beyond the stream's seven-day retention; a
turn completed then, and a pre-enrollment sponsorship on issue K was replaced.

1. The backoffice API creates its durable consumer, which delivers new messages from
   now on.
2. Backoffice API → task application: `GET /baseline`, paged, every page from one
   consistent database snapshot: every current task, consent and index snapshot, the PR
   and the run included, and every turn record, request outcome and consent fact.
3. While it pages, the task application commits a stage change and its event; that
   event's publication is delayed until after the last page.
4. It loads the baseline, then applies the buffered and later messages, deduplicated by
   fact ID and snapshot revision. The stage change is either in the baseline or arrives
   as a newer revision; either way it's applied once.
5. The old turn's prompt opens by its artifact reference and the transcript opens
   from its indexed PostgreSQL entries. K's replaced
   sponsorship shows in its sponsor's history. A turn that's been running since before
   the rebuild, whose status doesn't change during it, shows its live run, runner session
   and start time from its status snapshot in the baseline.
6. Issue M was deleted before the baseline; the baseline carries its tombstone at index
   revision 41. An older upsert for M at revision 39, delayed, arrives afterwards and is
   dropped: M stays deleted.
7. A gap repair publishes a replacement of the repository's index at revision 50, but a
   new issue's row at revision 52 arrives first. The replacement, older than the row,
   doesn't remove it: a row change applies only above both its row's revision and the
   repository's last replacement revision.
8. The repository's index is rebuilt from GitHub. The task application records index
   revision 60 before the scan; while it reads, a webhook updates PR 12's row at
   revision 61. Before committing, it keeps PR 12's newer row over the scanned one and
   publishes the replacement at revision 62, so the backoffice doesn't lose the update.
9. PR 20 is on a task branch from before the cutover, no task application task links
   it, and 100 newer PRs were updated since. Python's discovery would miss it on its
   first page (`service/github_reads.py:298`); the first build has PR 20's number from
   the cutover's export of Python's list index, and a cold build without that seed pages
   through every PR. Either way it stays in the list.

**Checks.** A read model built after the stream's retention shows everything the lists
and views show today, live turns included, with no change lost or applied twice across
the baseline, no delayed index change undoing a removal or a newer row, and no rebuild
overwriting a change made during its scan ([findings 38, 47, 54 and 55](#findings)).

### 39. A Python-era sponsor on a new task

After the cutover. On issue P, S commented `/concurro sponsor openai` while the Python
service ran; P never got a task. This runs on the test deployment always, and on a real
target only where such a comment exists.

1. W comments `/concurro start` on P after `A`. The task application reads P's comments
   for the requirements and finds S's comment, dated before `A`: shown as history, and
   left out of the consent fold, the fills and the boundary
   ([contracts](contracts.md#cutover-sources)).
2. Nobody sponsors again. The boundary freezes from sources at or after `A` only: W's
   `start` fills the `openai` slot from W's credentials if eligible; otherwise the slot
   stays unassigned, or the org pays where `openai` is org-funded.

**Checks.** S never pays for a task the task application runs unless S sponsors again
after `A` ([finding 53](#findings)).

### 40. A command retried after its proof expired

Phase 3, M3. Writer W approves task T's plan in the backoffice.

1. The frontend gives the action an ID, `a1`. The Python backoffice checks CSRF and W's
   permission, records `source_at` for `a1`, signs a proof and forwards
   `POST /requests {kind: approve, proof}` with `Idempotency-Key: a1`. The task
   application applies it and records the response, but the response is lost.
2. The browser retries after the proof has expired. The Python backoffice signs again
   with the same contents, `a1`'s original `source_at` included.
3. The task application matches `a1` on the actor and the command's contents, not the
   new proof, and returns the recorded response. Nothing is applied twice.
4. A request under `a1` with different contents is `409 idempotency_conflict`.

**Checks.** A browser command whose response is lost can be retried after its proof
expires and returns its original outcome ([finding 57](#findings)).

### Also checked

- **Approving by time.** "Looks good" binds the latest plan published before the
  mention's `source_at`, so the task application records each plan notice's
  publication time ([finding 13](#findings)).

## Findings

Each is applied in [contracts](contracts.md). Findings 6, 12 and 23 were withdrawn with
the single cutover; their numbers are kept.

1. **`source_at` for non-comment channels.** Superseded by finding 14: these requests
   now take their timeline event's time.
2. **A task can be enrolled before it's initialized.** A task has no `stage` until
   initialization, and an `initialized` flag.
3. **Filling the boundary is recorded once,** per task and boundary, with `unknown` when
   the credential history is incomplete.
4. **Contributed submits follow their transitions.** A contributed attempt is submitted
   only once the exchange has acknowledged the change that set its slot epoch.
5. **The submit body is stored before the first call,** so a retry matches the exchange's
   digest.
6. **Withdrawn:** the exchange's ownership lookup existed only for two controllers.
7. **Withdrawals name their request,** so their later replies report against it.
8. **Gaps beyond redelivery are reported and reconciled from repository listings,** not
   from known issues: comments, review comments, issue events, updated PRs, then every
   task's decision inputs.
9. **The interpreter on the Python exchange needs bridge changes:** an `interpreter` role
   and interpretation grants without a slot.
10. **Retries carry the failure they follow:** `retry_of` in the attempt.
11. **Relay release is acknowledged in phase 5:** the exchange answers a grant revocation
    only after the verifier confirms each claim's revocation is durable.
12. **Withdrawn:** ownership records existed only for two controllers.
13. **Publication outcomes carry their time:** `written_at`.
14. **Enrollment requests come from the timeline,** identified by its event ID; a
    delivery only wakes the channel, so delivery and reconciliation can't double a
    request.
15. **Grants are a tagged union** (`task_contributed`, `task_org`, `interpretation`), each
    with its own payer and refusals.
16. **Python's grant is a slot:** many attempts map to one slot grant per consent epoch,
    and transitions follow consent, not attempts.
17. **Exclusions are as narrow as the evidence:** per version, or per version and model,
    with expiry, and none after an unknown outcome.
18. **Result and cost are separate:** `outcome` says whether the result is usable,
    `provider_requests` whether paid work may have happened.
19. **Before phase 5, "spending stopped" is a time bound:** the acknowledgement time plus
    the longest grant lease, since a failed relay release isn't reported.
20. **The exchange answers a grant revocation with its sessions,** after ending them.
    Admission is checked per request, so no binding can arrive late.
21. **Snapshot event IDs include the revision,** so successive status snapshots aren't
    deduplicated as one.
22. **The phase 5 cutover drains before it switches:** an idle window, with no turn
    spanning the two exchanges.
23. **Withdrawn:** the list bridge existed only for two controllers.
24. **Consent changes reach the exchange in order:** one slot change per change from the
    funding boundary on, each naming the previous one. The slot adapter applies them
    strictly in order, from its own record of acknowledged epochs.
25. **`provider_requests: none` needs complete records:** a finished, contributed turn
    with no recorded request and none awaiting observation. Org turns are `unknown`.
26. **A `410` isn't always "never ran":** a replay returns its stored admission; a `410`
    for a never-admitted turn makes the version unusable; a `410` after the exchange
    dropped the record is `unknown`.
27. **An edit's new kind decides:** an edit into a sponsorship command applies per
    revision, whatever the earlier revision was.
28. **An uncertain branch write settles on the exact commit:** the publication module
    records the commit SHA before writing and accepts only that SHA on the ref.
29. **Pruned turns leave tombstones:** written with the deletion, checked by every
    admission, kept until the Python submission path is retired. After its admission is
    recorded, a turn is only polled, never resubmitted.
30. **Commands carry a proof, not a claimed actor:** the Python backoffice signs a
    short-lived proof bound to the user, issue, command, arguments, `detail.feedback`,
    `source_at` and idempotency key; the task application takes the actor from it.
31. **Diagnostics keep today's writer rule:** full task views, turns and blobs need
    upstream write access and both repositories visible; a reader gets a summary.
32. **A fill applies only to the state it was asked for:** boundary, folded sources,
    source revision and consent revision are compared when its answer applies.
33. **The funding mode comes from policy activations,** never backdated, matched by the
    task's configuration digest; else `unknown`.
34. **One cutover rule for every path,** superseded by finding 48: a delivery's receipt
    order can't decide it, since Python reads comments itself and the forwarder can
    publish out of order.
35. **Generations and epochs carry across the cutover** for issues the Python service
    used, so a new task there isn't fenced out (with finding 45's slot epoch).
36. **Current attempts are per operation or seat,** and `stop` cancels them all.
37. **The reviewer's user grant moves to the task application,** and a published review
    is checked against the reviewer's numeric ID.
38. **The backoffice API starts from a baseline,** with the target index, then follows
    events (with finding 47's history and cursor).
39. **Own consent history is its own fact,** per user and issue, with or without a task.
40. **Resending a pending comment is an explicit override** that can duplicate it;
    `--failed` sends nothing and adopts a late create.
41. **The prompt is its own blob,** as today's diagnostics reconstruct it.
42. **Only a read of the current configuration activates a policy:** reading a pinned
    SHA to recover or resume an older task never does (`service/worker.py:345-347`).
43. **Browser commands stay on the session authority through phase 3:** the Python
    backoffice takes the command POST with its CSRF check, signs it and forwards it; the
    backoffice API's credential is read-scoped and it has no command path, since it
    receives viewers' cookies.
44. **The reviewer grant is never shared:** its refresh token is single-use, so M3 uses
    an independent test reviewer grant, and the production grant moves only after the
    Python service stops, verified by a refresh.
45. **A reused issue's first slot is at the exported epoch plus one,** expecting the
    exported epoch, with payer and consent from the new boundary only.
46. **An author's edit of an adopted sponsorship applies,** adding consent while the
    earlier consent stands until withdrawn: a behavior change from today's service,
    which ignores the edit.
47. **The baseline carries the views' history and needs no outbox position:** turn
    records, request outcomes and consent facts come with it, from one consistent
    snapshot, after the backoffice API's durable consumer exists; overlap is
    deduplicated by fact ID and revision.
48. **One activation time for every path:** the original GitHub source's own time
    against `A`, when the Python daemon stops; sources Python missed just before `A` are
    reissued by their authors. The stream position only says where reading starts.
49. **The target index keeps today's selection,** with removals and per-repository
    replacement on a gap, so a deleted row can't stay stale.
50. **Event families publish from the milestone that introduces them:** task, request
    and turn events from M1; consent and index events from M3, when the backoffice API
    starts consuming.
51. **The backoffice API never receives the session cookie:** the edge strips it from
    `/api/v2/*`, and a distinct read-only viewer token answers identity and tiers only,
    so nothing delivered to the backoffice API can authorize a command.
52. **`A` follows the daemon's confirmed exit,** since its stop drains running ticks;
    only sources strictly before `A` are reissued, so one posted after `A` but before the
    task application starts isn't also reissued.
53. **Pre-cutover sources never select a payer:** they're shown and read for
    requirements, but the consent fold, fills and boundary use only sources at or after
    `A`.
54. **Index changes share one ordering:** a per-repository index revision on every
    upsert, removal and replacement, with tombstones in the baseline.
55. **The baseline carries each turn's latest status snapshot,** so a running turn keeps
    its live run after a rebuild.
56. **The reused-issue slot check is conditional at the live cutover:** mandatory in the
    rehearsal, against a populated test exchange; live only when the export holds a slot.
57. **Command idempotency doesn't depend on the proof:** a browser action ID that
    survives retries, and the actor and contents compared, so a retry after the proof
    expired returns the recorded response.
