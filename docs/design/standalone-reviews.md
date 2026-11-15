# Standalone PR reviews

Status: implemented; GitHub contracts verified live, deployed pilot pending (see [Verification against GitHub](#verification-against-github)).
Requesting the reviewer account on an open upstream pull request starts a review round by
the target's `pr_reviewers` panel. Concurro neither planned nor wrote that PR, and never
repairs, re-describes, marks ready or merges it. [PR reviews](pr-reviews.md) covers the
finding ledger and publication protocol shared with contribution tasks.

## Surfaces

| Surface | Content | Author |
| ---- | ---- | ---- |
| PR artifacts (`issues/{pr}/artifacts`) | Checkpoint, full reports, history, finding ledger, outboxes | Service App |
| Check Run on the reviewed head | Queued → Reviewing → Finished; activity only | Service App |
| Native `COMMENT` reviews and threads | Each reviewer's assessment and findings | Reviewer account |
| Command reply comment | One reply per processed `/concurro` comment revision | Service App |
| Backoffice | Live status, rounds, complete reports | Reads the PR artifacts |

A reviewed PR gets no dashboard comment, stage label, health notice or welcome comment.
GitHub shows no artifacts on a pull request; complete reports are read in the backoffice.
Public output carries no backoffice address: check `details_url` is the PR itself, and
shortened review text refers to Concurro's report data without a link.

## Task and rounds

The task key is `(upstream, pr_number)` with kind `standalone_pr_review`
(`service/registry.py`, table `tasks`). The PR is bound to that key in `pulls`, so a PR
already owned by a contribution task keeps its owner and enrolls nothing. The upstream's
target supplies the executor fork and pinned configuration whether or not the upstream
supplies issues; with a reviewer configured, each upstream may appear in one target only.

`engine/standalone.py:ReviewController` extends the contribution controller's model-call,
admission, checkpoint and publication paths. Its stages:

| Stage | Meaning |
| ---- | ---- |
| `queued` | Round opened and snapshot frozen; no model call yet |
| `review` | Panel members are asked one at a time |
| `publishing` | Reports accepted; review writes are settling |
| `complete` | Round finished; waits for a new request |
| `closed` | PR closed or merged; terminal until reopened |

`blocked`, `stopped`, `awaiting_credentials` and `budget_exhausted` pause a round as in
contribution tasks.

A round opens from a verified request by a current writer of the upstream. It freezes the
head, base ref and tip, merge base (`compare/{base_tip}...{head}`), title and body, changed
paths and the reviewer panel. Every panel member reviews that snapshot; reviews, finding
positions and the check stay bound to it.

| Event | Behavior |
| ---- | ---- |
| Request while a round is unfinished | Joins that round |
| Request after completion | New round on the same task; findings, spend and history kept; the same head may be reviewed again |
| Push, retarget, title or body edit | Recorded as observed metadata; no round, no new check, no change to a finished assessment |
| Request removed by anyone but the reviewer | No further reviewer is asked; finished reports publish; round ends `cancelled`. A paused round is cancelled too and ends when a writer retries it |
| PR closed or merged | No further admissions or PR writes; outcome recorded; retained reviews publish if it reopens |
| PR reopened | Returns to `complete`; a fresh request starts a round |

Reviewers answer each of their own open findings as `fixed`, `open` or `obsolete`
(removed or replaced by the author). `reopen` is refused: there is no plan to reopen, and
design defects are findings. Binary, symlinked, submodule and non-UTF-8 paths are listed
for inspection in the checkout instead of rendered.

## Reviewer instructions

`bundle/skills/concurro-review-pr/SKILL.md` holds the rules common to every PR review.
`Bundle.instructions(stage, kind)` adds one trusted addendum, `contribution.md` or
`standalone.md`, selected by the controller's task kind and carried outside the request
context (`task_kind`), so PR text cannot select it. A request without a kind is a
contribution. The standalone response schema has no `reopen` verdict. Prompt sizing counts
the selected addendum.

This split changes the bundle identity: deploy while idle, or have a writer
`/concurro resume` each active task ([bundle rollout](pr-reviews.md#bundle-rollout)).

## Check Run

`engine/review_checks.py` derives the check from checkpoint state on every save. Name
`Concurro review - PR <n>`; `external_id` `concurro:<upstream>#<pr>:<round>:<attempt>`.

| Task state | Check |
| ---- | ---- |
| `queued` | `queued`, title Queued |
| `review`, `publishing` | `in_progress`, title Reviewing, with reviewers complete of total |
| Round complete | `completed` / `neutral`, title Finished; snapshot, completion time, links to the native reviews |
| Paused, closed, cancelled | `completed` / `neutral`, title Finished; a public reason and the applicable command |

Every conclusion is `neutral`. The check carries no verdict, tally or findings and is not
meant for branch protection. A round gets one run; work restarted after that run completed
gets a new one, and nothing is created on a head that was not requested for review.

A failed update never blocks or repeats review work. It is saved as pending in the
checkpoint, and a finished or paused task with a pending update is rescheduled until the
check is current. `external_id` correlates runs but does not deduplicate them: after a
create of unknown outcome the run is looked for on two later syncs before another create is
sent. The uncertainty is saved before each create is sent, so a restart after GitHub
accepted it is treated the same way. For an hour after such a create the task stays
scheduled and finishes any second run that surfaces as superseded; one scan cannot rule out
a run that is not visible yet. Of several runs found, the newest is the live one, also when it
becomes visible only after an older run was adopted: GitHub shows the newest run of a name
by default. A check replaced by a new round or attempt keeps that
window (`check_rechecks`), and one replaced before it finished has its runs finished too. In the gateway, a check write of unknown outcome is recorded and discarded
as a derived view, without stopping the task for an operator.

## Identities and credentials

| Operation | Credential |
| ---- | ---- |
| Artifacts, command replies, Check Runs, runner dispatch, resolving the reviewer's finding threads | Service App installation token |
| Review mutations, reads of pending reviews | Reviewer account's user token |

`MutationGateway.sender` selects the transport by task kind and operation and records the
sending account with each mutation intent (`mutations.actor`). Only the author sees a
pending review, so its reads use the reviewer's transport too. A resolution is admitted
only for a thread on the enrolled PR whose root is the reviewer's finding comment, and the
ledger records the App as its resolver (`resolved_by`). The reviewer account therefore
needs only read access to the upstream.

Accounts are `identities.Actor` descriptors: login for display, REST numeric ID and GraphQL
node ID bound separately at setup. Matching uses the ID when both sides have one, so a
renamed account still matches and a reused login does not. Records without IDs (publications
begun before this release, or the App when its IDs could not be resolved) match by login.
GraphQL author selections include the node ID.

The reviewer's grant is a user-to-server token of the service App
(`service/user_grant.py`), stored owner-only in the state directory and bound to the
configured numeric `user_id`. An expiring token is refreshed shortly before expiry, one
refresh at a time, and the replacement is saved before use; a failed or lost refresh needs a
new authorization. A token without expiry is never refreshed. The token carries the
intersection of App and account permissions; gateway admission, not the credential, limits
it to reviews.

## Commands

`/concurro stop`, `retry` and `resume`, and the funding commands, are accepted in the PR
conversation and from the backoffice, authorized against the upstream. Each processed
comment revision gets one App-authored reply naming its outcome (Applied, No-op or Rejected);
an edited command gets its own. Other commands are rejected with the supported set.
`stop` applies to a running or paused round. `resume` adopts current settings; a round
paused for credentials or budget stays paused and remains recoverable by `retry` or
credential recovery, which revalidates only configuration and instructions.

The first verified requester is the funding initiation boundary (provenance
`review_request`). Funding policy is observed for review upstreams as for issue
repositories, so it is on record before a request arrives. Funding initialization posts no
comment on a reviewed PR.

## Wakeups

Ingress records `pull_request` `review_requested` and `review_request_removed` for the
reviewer's numeric ID together with the delivery receipt, task, evidence and wakeup. A
standalone task also wakes for its PR's closure and reopening and for `/concurro` comments
in its conversation. CI events, pushes, retargets, edits and review activity do not wake it.

## Verification against GitHub

Probes with the service App and Reader App on a disposable PR (2026-11-15) confirmed:

- PR artifact writes with the App's installation token and reads with the Reader App, with
  version history.
- Check display Queued → Reviewing → Finished, completing neutral.
- A review created or submitted on its original commit after a force-push is accepted, so
  no conversation-comment fallback exists. A commit outside the PR is refused with
  `VALIDATION`; the report then stays in the artifacts and the check ends with a
  publication reason.
- Definite refusals arrive as HTTP 200 with the mutation's field null and errors scoped to
  it, of type `FORBIDDEN`, `UNPROCESSABLE`, `NOT_FOUND` or `VALIDATION`
  (`review_api.rejected`). An unplaceable line or file returns a null thread without errors.
- Thread resolution with Pull requests write alone is refused; it needs Contents write.
- With the reviewer account on a read role, its user token publishes reviews and replies,
  and the App's installation token resolves a thread the reviewer authored.
- `viewer.id` equals the node ID GraphQL reports as a review's author, for the App's bot
  under an installation token and for the reviewer under its user token. The bot's login
  differs between the two (`[bot]` suffix); its node ID does not.
- A check on a PR from a contributor fork is displayed, although the API lists no
  `pull_requests` for it.

The implementation itself has not yet run a deployed end-to-end pilot.
