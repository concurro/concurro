# Proposal: mentions and assignment

**Status: proposal, not implemented.**

Designed against the target in the [architecture review](architecture.md); build it on
the new orchestrator, not the current Python service.

Let people work with Concurro by mentioning its account (`@ConcurroBot please plan this
but don't implement yet`) or assigning it to an issue, instead of only through the
`concurro` label and the exact `/concurro` grammar. A small model, paid for by the
commenter, translates free text into an existing command. The model never authorizes
anything: the command it proposes goes through the same writer check, replay protection
and approval binding as a typed one.

Mentions and assignment are channels of [request intake](request-intake.md), which this
proposal depends on.

## Today

- **Enrollment.** A writer applies the `concurro` label. `Controller.enrollment` accepts
  it only while the label is present and its latest `labeled` event came from a current
  writer; that event is the funding initiation boundary (provenance `label`). In
  autonomous mode the label starts the task; in manual mode `/concurro start` does
  (`engine/commands.py`).
- **Commands.** `commands.parse` accepts only the exact `/concurro ...` grammar. Ingress
  wakes a task for commands, sponsorship comments, and author or writer comments on
  enrolled issues (`service/ingress.py:Ingress.comment`). Each processed command revision
  gets one App-authored reply.
- **Command transport.** Every command reaches the controller as a GitHub comment,
  including backoffice commands, which the service posts as signed App comments
  (`engine/delegation.py`). [Request intake](request-intake.md) proposes replacing this
  with a `requests` table fed by independent channels.
- **Reviewer account.** With `reviewer` configured, a writer's review request for that
  account on an upstream PR starts a [standalone review](../design/standalone-reviews.md)
  (provenance `review_request`). The account posts reviews through a user-to-server token
  of the service App; the App posts everything else. It is matched by numeric ID
  (`identities.Actor`), and needs only read access.
- **Mode.** `autonomous` is a target-wide boolean. Manual mode waits for
  `/concurro approve <plan-id>`; autonomous mode proceeds on plan-reviewer consensus.
- **Roles and credentials.** Model calls run under the role keys `triage`, `planner`,
  `plan_reviewers`, `implementer` and `pr_reviewers`, and pass central budget admission.
  [Ordered personal credentials](credential-list.md) lets a credential be restricted to
  roles; optional roles fail open to a deterministic path.
- **Feedback on Concurro's own PRs.** PR-thread replies don't start repair work; a writer
  comments on the issue and then uses `/concurro retry`.

## Goals

- Assigning the account to an issue enrolls it, as the label does.
- `@<account>` in an issue, PR conversation, review or review thread is a request to
  Concurro, and gets a visible acknowledgement.
- Free-text requests map to existing commands, and to two new per-task choices: plan only,
  or plan and implement.
- The commenter pays for interpreting their own request.
- Later: answers to questions about a task, and steering work in progress.

## Non-goals

- Replacing the label or the `/concurro` grammar. Both stay, and need no model call.
- Letting a model decide who may do what.
- Conversation in GitHub Discussions, Slack or other surfaces (see
  [Future directions](#future-directions)).

## Design

### Overview

```text
GitHub event (comment, review, review comment, issue body, assignment)
  → ingress: verified, deduplicated, mapped to a typed event on github.*
  → mention channel (orchestrator): mention of the configured account? not quoted, not ours?
  → acknowledge (reaction, posted by the publisher)
  → exact grammar after the mention?  yes → command
                                      no  → interpreter (if the commenter has a matching credential)
  → requests (channel `mention`), sent to the orchestrator
  → the orchestrator applies them; their events update the dashboard and reply to the mention
```

The account is the configured `reviewer` account, matched by ID; without one, mentions
and assignment are off. The App's own bot (`<app>[bot]`) can't be meaningfully mentioned
or assigned, which is why a user account is needed.

### Assignment

- **Channel.** The `assignment` channel turns `issues` `assigned` and `unassigned`
  deliveries whose `assignee` is the account into `enroll` and `unenroll` requests, with
  the assigner as actor.
- **Enrollment.** As with the label, with provenance `assignment`: in autonomous mode the
  first enrollment is the funding boundary
  ([request intake](request-intake.md#applying-requests)). The label is not added, so the
  App never becomes the initiator.
- **Removal.** Unassigning behaves as removing the label does today. An issue enrolled by
  both stays enrolled until both are gone.
- **Modes.** Unchanged: in autonomous mode assignment starts the task, in manual mode it
  enrolls and waits for `start`.
- **GitHub constraint.** GitHub only lets the account be assigned where it has write
  access, is an organization member with read access, or has commented on the issue.
  Upstreams where it is none of these keep using the label.
- **Pull requests.** Assigning the account to a PR does nothing; a review request
  already covers that.
- **Lists.** The backoffice read model lists assigned issues as enrolled. Assignment
  exists only on the new orchestrator, so the [list index](list-index.md) never sees it.

### Mentions

- **Sources.** `issue_comment` (issues and PR conversations), `pull_request_review`
  bodies, `pull_request_review_comment`, and issue bodies on `opened`.
- **Detection.** `@login` of the account's current login, case-insensitive, outside code
  spans, code blocks and `>` quotes. Comments by Concurro's own accounts and other bots
  are ignored, as `Ingress.ours` does today.
- **Revisions.** A mention is processed once per comment revision, like a command. An
  edit is re-interpreted only if the text addressed to the account changed.
- **Acknowledgement.** The account adds an `eyes` reaction on receipt, with no model call.
  The outcome is replied to the mention.
- **Still a comment.** On an enrolled issue a mention also counts as discussion or a
  clarification answer, as any author or writer comment does today.
- **Who may act.** Each command keeps intake's rule for its kind
  ([authorization](request-intake.md#authorization)), checked on the commenter at
  processing time. Exact grammar is parsed before the check, so an exact `withdraw`
  needs only that the commenter can see the issue. Anyone else gets a fixed reply naming
  who can use Concurro, with no model call.

### Exact mentions

`@ConcurroBot start` is `/concurro start`: the text after the mention is parsed with the
existing grammar minus the prefix. It needs no model call and works without an
interpreter credential. It's also the fallback when the interpreter is unavailable.

### Interpreter

A new optional role, `interpreter`, typically a small model (Haiku-class).

- **Input.** The commenter's text addressed to the account; where it was posted; the
  issue or PR title; the task's stage, mode and latest plan ID; and the commands the
  commenter may use there. Other people's text, if included, is marked untrusted.
- **Output.** A fixed schema, validated before use:

  ```json
  {
    "action": "start | plan_only | plan_and_implement | approve | stop | retry | resume | sponsor | withdraw | funding | review | answer | clarify | none",
    "argument": "provider or funding choice, where the action takes one",
    "confidence": "high | low",
    "question": "for clarify: one question back to the commenter"
  }
  ```

- **Low confidence or `clarify`.** The account replies with the question; nothing runs.
- **Result.** Anything else becomes a request and is applied as a typed command would
  be. `answer` goes to the [responder](#responder).
- **Not offered.** `invalidate`, `extend`, `retry --plan-revisions` and
  `resume --description-only` stay exact-grammar only. They discard work or renew
  allowances, and a misreading would cost more than typing the command.
- **Attempts.** Each interpretation has an attempt ID, saved with the pending source
  before the call ([sources](request-intake.md#sources)). A restart recovers that
  attempt's stored result instead of calling again: an exchange turn stores its result
  durably, and a direct gateway call would need to store the response under the attempt
  ID before acknowledging it. Results are kept until the source is final. The Python
  exchange deletes finished turns after a day; a result lost that way falls under the
  deadline below.
- **Deadline.** An attempt whose outcome is still unknown at its deadline (a crash after
  the provider charged, a result lost before it was stored) ends: the source is
  finalized as failed and the reply asks for the exact command. It is never paid for
  again automatically. This is separate from missing consent evidence, which leaves the
  task's payer pending instead.
- **Frozen result.** The final interpretation is stored in the source record with the
  requests it produces, copied into each request's `detail`, and never recomputed on
  replay or restart, like an admission record.
- **Credential.** Selected from the commenter's own list with role key `interpreter`,
  using the [ordered credential](credential-list.md) rules. With no matching credential,
  only exact mentions work, and the reply says so.

#### Mapping

| Where | Example | Becomes |
| ---- | ---- | ---- |
| Unenrolled issue | "plan and implement this" | Enrollment (provenance `mention`) and `start`, task mode autonomous |
| Unenrolled issue | "plan this but don't implement yet" | Enrollment and `start`, task mode manual |
| Enrolled issue, plan awaiting approval | "looks good, go ahead" | `approve <plan-id>`, the latest plan published before the mention |
| Enrolled issue | "stop for now" | `stop` |
| Blocked or failed task | "try again, the fixture moved to tests/data" | The mention is the writer feedback; `retry` |
| PR not written by Concurro | "review this" | A standalone review round, as a review request starts |
| Anywhere | "I'll pay for the OpenAI side" | `sponsor openai` |

- **Plan ID by time.** `approve` binds the latest plan published before the request's
  `source_at`, the mention's own time. If a newer plan exists when it is processed, the
  existing approval check refuses it as stale; nothing approves a plan the commenter
  can't have seen.
- **Per-task mode.** `plan_only` and `plan_and_implement` set the task's `mode` at
  initialization, overriding the target's `autonomous` setting for that task only. A task
  run in autonomous mode must meet the same configuration constraints as an autonomous
  target. After initialization, the mode changes only through existing commands.

### Mention channel

- **Source.** The comment ID and a digest of the revision's text addressed to the account,
  so an edit that changes it is a new request and one that doesn't is not. The channel
  records the source revision as pending, with its editor, before calling the
  interpreter, and as final with the requests it produces
  ([sources](request-intake.md#sources)); one mention can produce an enrollment and a
  `start`, which share the source and differ by index.
- **Edits by others.** An edit by anyone but the author isn't interpreted or charged to
  the author.
- **Time.** `source_at` is the comment's creation or edit time, so interpretation latency
  can't reorder sponsorship claims or approvals.
- **Reply route.** A reply to the mention from the account: in the review thread for a
  review comment, otherwise in the conversation.

### Payment and admission

- **Who pays.** The interpreter call is charged to the commenter's credential, whatever
  the issue's funding choice: the channel asks funding for a grant on that credential,
  and the call runs under it by the [execution path](#open-questions) chosen. That grant
  is the commenter's own and doesn't wait on the task's consent. Work the
  resulting command starts is paid under today's rules, with the commenter as initiator.
- **Admission.** Every model call passes budget admission. A call for an existing task is
  admitted as an optional call against that task's `max_model_calls`. A call with no task
  yet (an unenrolled issue) needs its own record; see
  [Open questions](#open-questions).
- **Failure.** An interpreter that is unavailable, over budget or returns invalid output
  takes the deterministic path: exact mentions still work, otherwise the reply asks for
  the `/concurro` command.

### Responder

A second optional role, `responder`, for replies that aren't commands. It runs later than
the interpreter and costs more, since it needs task and repository context.

- **Read-only answers.** "Why did the plan drop the cache layer?" or "What has this cost
  so far?", answered from the task's artifacts, reports and finding ledger. No writes
  besides the reply.
- **Paid by the asker**, with role key `responder`.
- **Steering a Concurro PR.** "Use the existing retry helper here" in a review thread
  becomes writer feedback plus `retry`, replacing today's manual issue comment. This needs
  the orchestrator to read PR-thread feedback; it's a later phase.

### Identities

| Output | Author |
| ---- | ---- |
| Reaction, clarifying question, responder reply | The account, posted by the publisher |
| Dashboard and notices | The App, posted by the publisher |

Replies come from the account people addressed. The publisher's admission for the
account's writes grows from review writes to reactions and replies on a comment that
mentions it.

All of the account's writes share one user-to-server rate limit across every repository,
while App writes have a limit per installation. Mentions add a few writes per request, so
this matters only at volume.

## Invariants

- **A model never authorizes.** Authorization is intake's rule for the command's kind,
  checked on the commenter at processing time. The interpreter's output only selects a
  command from the set the commenter could have typed.
- **The request is the record.** Its interpretation is frozen with it; replay never
  re-interprets.
- **Approval identity is unchanged.** A free-text approval binds one specific plan ID.
- **Payment identity is unchanged.** The commenter pays for interpretation; task work
  follows the existing sponsor and initiator rules.
- **Untrusted text can't escalate.** Quoted or third-party text can at most change which
  of the commenter's own commands is chosen.
- **Concurro never responds to itself.** Its own accounts' comments never trigger.
- **The label and `/concurro` grammar keep working** with no model, credential or
  account configured.

## Phasing

1. **Assignment.** Arrives with [request intake](request-intake.md#phasing) phase 2.
2. **Exact mentions.** Detection, reaction, the mention channel and the account's reply
   transport. Needs intake phase 1.
3. **Interpreter.** Role key, schema, admission and charging, clarifying replies,
   per-task mode, time-bound approval. Built once exact mentions show what people ask
   for.
4. **Responder.** Read-only answers, then PR-thread steering.

Each phase is usable on its own, and phases 1 and 2 need no credential changes.

## Future directions

- **Conditional starts:** "implement this after #123 merges".
- **Decomposition:** "split this into smaller issues".
- **Focused reviews:** "review this for security".
- **Unenrolled repositories:** the account gets GitHub notifications for mentions where
  the App isn't installed; a reply could explain how to install it.
- **Other surfaces:** the interpreter outputs requests, so a Slack or backoffice
  free-text channel can reuse it.

## Open questions

1. **Execution path.** Run the interpreter as a runner job (every credential type works,
   but a job adds startup latency to each reply), or call the inference gateway directly
   (fast, but only for routes usable outside a CLI harness, the gateway then needs a
   session for the orchestrator, and it must store each response under its attempt ID
   so a restart doesn't pay twice)? If the interpreter is built before architecture
   [phase 5](architecture.md#migration), it runs as a turn on the Python exchange; the
   longer-term path stays open.
2. **Admission without a task.** Charge interpreter calls on unenrolled issues to a
   per-commenter record, or open the task first and charge it there?
3. **Org-funded targets.** Should the org pay for interpretation where it pays for task
   work, instead of the commenter?
4. **Loosening the mode.** May a writer run a task in autonomous mode on a manual target?
   Writers can already approve plans there, but the target owner chose manual.
5. **Non-writers.** Should the responder answer the issue author or other participants,
   at their own cost, or stay writers-only like commands?
