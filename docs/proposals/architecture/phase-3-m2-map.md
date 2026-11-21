# Phase 3 M2 behavior and acceptance map

M2 carries one contribution from the accepted M1 intake through a ready pull request.
Its [local acceptance evidence](../../rust-phase-3-m2-evidence.md) records the
frozen source and corrective delta.
The Rust task state and decision tests preserve the behavior below; Python's storage
shape is not the Rust data model. All effects use M1's recorded intent, transaction
and job recovery. The dedicated live test repository has been waived, so local
GitHub, exchange, runner and PostgreSQL fixtures supply the implementation evidence.

## Stages

| Stage | Python behavior to preserve | Local acceptance |
| ---- | ---- | ---- |
| Plan | `Controller.plan` (`engine/controller.py:1768`) builds a structured plan from pinned inputs, answers every open objection once, validates file scope and accumulated changes, and charges each bounded output repair (`engine/policy.py:400`). Questions pause in `clarify`; an answer resumes planning against updated requirements (`Controller.continue_planner_clarification`, `:1224`). | Valid plan advances; malformed output gets two recorded repairs; a question waits and an authorized answer continues the same plan; restart does not repeat a charged turn. |
| Design review | `engine/design_batch.py` and `engine/review_batch.py` keep independent reviewer seats, private results and failure allowances. An unfinished sibling survives a restart. Cooperative first-pass approval or a distinct fresh unanimous round is required (`engine/policy.py:268`; `engine/design_eligibility.py:100`). | Each seat has its own current attempt and bound report. A late or superseded result cannot vote. Objections, high risk and changed inputs reopen or revise within the pinned allowance. |
| Planned and approve | `engine/policy.py:248-305` binds exact plan content and ID, base, requirements, configuration, bundle, charter, generation, review phase, roster, parent and approved paths. `CommandHandler.approve` (`engine/commands.py:219`) accepts a writer's matching plan digest only after independent unanimity. | A changed plan, roster, base, requirement or review binding refuses approval without advancing. The approver is the command actor, even when someone else started the task. |
| Implement | `Controller.develop` and `implement` (`engine/controller.py:1858`, `:2045`) recheck design authorization, validate native edits and paths before Git writes, and preserve the bounded fix budget. A supported design issue reopens assessment; a description-only repair keeps the reviewed code and its validation evidence. | An invalid edit creates no Git object or publication intent. A valid candidate freezes its parent, exact content and validation; a restart consumes the existing result. |
| Publish | `engine/publication.py:58-89`, `:91-149`, `:151-286` reserve the branch, create blobs/tree/commit, advance only the expected parent to the exact candidate SHA, then find or create one App-authored draft PR. The PR description is pinned and a human edit is not overwritten (`engine/pr_description.py`). | Lost branch/PR responses converge by exact ref SHA and PR identity. A foreign tip, missing reserved branch, duplicate PR, changed description or wrong head blocks with a visible cause. No second PR create follows an uncertain one. |
| Review and fixes | `Controller.review` (`engine/controller.py:2117`) reviews an immutable head, attributes findings to their owner, and returns to implementation within `max_fix_rounds`. `engine/pending_reviews.py:62-235` freezes review body, pending review ID, threads and replies before submission; only the owner resolves its findings. | A lost submit is read back by review ID, author, PR, head, marker and body. A definite refusal can rebuild once. A submitted review is never duplicated; a stale head cannot publish findings; a sibling result survives restart. |
| Gates | `engine/readiness.py` and `Controller.gates` (`engine/controller.py:2179-2259`) observe the current PR/base/head/files, approvals, findings, threads and CI on both head and current test-merge commit. A real success is required; neutral or skipped alone does not pass. | A changed head or test merge invalidates the observation. A lost ready response is settled by reading draft state. A later human return to draft remains draft, even after a new head. The ready notice is once per head; Concurro never merges or reruns CI. |
| Completion | `Controller.observe_merge` (`engine/controller.py:2261`) requires the published head and merge SHA, recording an external merge with `gates_verified=false`. Closing without merge is separate; stopped, blocked, rejected and escalated retain distinct recovery paths. | Matching external merge and closed-unmerged observations reach distinct terminal stages. A different merged head blocks instead of claiming completion. |

Current Python report publication uses issue generic artifacts at
`issues/{issue}/artifacts`, with readable views and immutable source data
(`engine/conversation.py:557-676`, `engine/report_store.py:490-540`). Rust keeps
authoritative reports in PostgreSQL and queues a generic issue artifact in the
same transaction as each report event. The publication job confirms the App
author, exact body and first artifact version; an uncertain create remains on
readback and is not sent again.

## Rust implementation seams

- `crates/tasks-core` defines later stages, reviewer identities and source-bound
  decision contracts while retaining M1's task ID, revision, pins and call spend.
- `crates/tasks-postgres` serializes plan, design, implementation, publication and
  review decisions under the issue lock. SQL and SQLx stay in the storage crate.
- `crates/tasks` runs stage jobs through M1's recorded exchange and effect paths.
  Task snapshots and dashboard revisions follow the same state transactions.
- A paused `start` follows the clarification and reassessment rules in
  `engine/commands.py:154-218`, with source order and authority rechecks.

## Commands and recovery

Use the exact grammar in `engine/commands.py:55`: `/concurro approve <64-hex-plan-id>`,
`/concurro retry`, `/concurro retry --plan-revisions N`, `/concurro resume`,
`/concurro resume --description-only`, and `/concurro invalidate`. There is no
`/concurro extend` command. M1's temporary refusal of these commands ends when
their M2 decisions and durable replies are installed.

| Input | Behavior and refusal evidence |
| ---- | ---- |
| Start while paused | `CommandHandler.start` (`engine/commands.py:154-218`) continues answered clarification or reassesses within the revision allowance. It does not reset an open PR or pending publication. |
| Retry | `CommandHandler.retry` (`:307-348`) renews the relevant output repair allowance or resumes a paused stage. A proven implementer-skill-only change may use the existing implementation bundle retry while preserving design approval (`engine/implementation_bundle.py`). It preserves spent calls, journal eligibility, findings and current publication intent. |
| Revision extension | `CommandHandler.extend_plan_revisions` (`:402-460`) applies only to a recorded exhausted revision with unchanged requirements and configuration, no pending publication and an unchanged PR. It grants the requested revision capacity without changing the model-call limit or waiving review. |
| Resume | `CommandHandler.resume` (`:257-305`) adopts current configuration and bundle while retaining work, findings, original approval bindings and spend. `engine/bundle_resume.py` uses receipt format 6. It refuses a roster that strands unresolved review owners. Description-only resume keeps the approved candidate. |
| Invalidate | `CommandHandler.invalidate_task` (`:550-635`) records the actor and prior identities, supersedes open questions, adopts current governance and reassesses without resetting spent budgets. It refuses unresolved publication or an externally changed PR. |

Every effect records its frozen request and expected identity before sending. A
`recorded` effect may send once; a `sending` effect must first read back the exact
GitHub or exchange result. The per-task decision queue fences late results, while
independent reviewer seats retain separate attempt identities. Operator `tasks show`
names the waiting stage, binding, pending write and next recovery action; `effects
pending` distinguishes unsent, uncertain, refused and operator-resolved work.

## Retained continuation admission

`engine/session_entry.py:33-96` is the runner journal oracle. Before a new M2
continuation reserves capacity, selects a payer, creates an attempt, charges a call
or dispatches a runner:

1. Freeze the v1 prompt and compute its Python-compatible canonical request digest
   with the existing Rust `python_digest`. Name the exact task, session scope,
   ordinal and expected predecessor digest from the recorded prior attempt.
2. In the admission transaction, call `retained_eligibility` from the task storage
   crate. For ordinal zero, an absent journal permits a fresh session. For a later
   ordinal, require its exact completed predecessor; for the same ordinal, only an
   exact request digest can replay the cached response. Never select an older
   readable revision after the required one is corrupt or missing.
3. Require the source gzip to be present as pending bytes or as a verified object.
   Before funding, fetch and verify a verified object if no pending bytes remain,
   then recheck its database identity in the admission transaction. Compare the
   recorded uncompressed retained file size with `max_retained_bytes`; a size one
   byte over refuses, equality or an explicit null limit permits. A null cached
   response refuses same-turn replay.
4. Pin the eligible gzip SHA and immutable v1 prompt on the admitted attempt.
   An exact replay returns the stored response without a new attempt, grant,
   reservation, charge or provider call. Refusal records a reason and creates none
   of those effects.

The storage check covers scope, ordinal, journal status, predecessor digest,
instructions, schema, pending bytes, corrupt archives and null cached responses.
Continuation preflight verifies an archived object before admission and applies
the uncompressed limit. The
[M2 plan](phase-3-plan.md#m2-one-contribution) names the null-response and
compressible-archive boundary cases.

## End-to-end local acceptance

The task binary, database jobs and local GitHub/exchange fixtures drive
through `start → plan → design review → approve → implement → publish → review →
gates → ready`. The fixture restarts across stage boundaries and checks the
request, attempt and publication records. Focused fixtures cover the command,
review recovery and terminal paths. Local success does not claim live publication
verification; the dedicated live repository was waived.
