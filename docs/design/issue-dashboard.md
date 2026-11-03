# Issue dashboard

Status: accepted (revision 3, reviewed). Replaces the issue's chronological conversation with
one status comment kept current in place, and posts a new comment only when a human
must act. Supersedes the issue event and status comment layouts of
[comment format](comment-format.md); its vocabulary, escaping, size and
compatibility rules still apply, and PR reviews are unchanged.

## Problem

Pilot issue #19 changed four lines of `README.md` and added a one-line changelog
fragment. The issue received 18 comments and about 17,000 characters: controller
narration, four near-identical plan approvals, two local validation events, both PR
reviews copied from the PR, and a status comment that repeated most of it again in
a collapsed section. About six comments told a human anything they needed.

Every new comment notifies the issue's watchers; an edited one does not. So the
narration also spent notifications, burying the few comments that asked for action.

## Decisions

| Question | Decision |
| ---- | ---- |
| Where progress is shown | The status comment, edited in place: a progress line and one table, one row per step |
| When a new comment is posted | Only when a human must act: input needed, escalated, blocked, plan ready for approval (manual mode), handed off for merge |
| Where full reports live | One report archive artifact per completed round, written once and never edited, linked from the status comment |
| Clarification | One "Input needed" request at a time, mentioning the issue's author; it is marked answered only when a real answer is recorded |
| Handed off for merge | When all of Concurro's own gates pass and only its merge policy, or GitHub's own requirements, stand between the PR and a merge. It claims Concurro is done, never that GitHub will allow the merge |
| Report length | An approval is its headline, plus at most two sentences when what was verified is not evident; findings, objections and questions keep their detail |
| Diagrams | None. A Mermaid diagram does not render in notification emails, grows tall, and has no room for detail |
| Out of scope | Skipping the fresh design round after a unanimous first round: a change to design consensus, decided separately |

## Status comment

```markdown
## Concurro

Intake ✅ → Plan ✅ → Design review ✅ → Implement ✅ → Code review ✅ → **Merge ✅**

| Step | Status | Detail |
| ---- | ---- | ---- |
| Intake | ✅ Proceed | Actionable, low risk, fits G-USABILITY |
| Plan | ✅ Version 1 | [Implementation plan](link) · focus: changelog category |
| Design review | ✅ 4 of 4 approve | Round 1, cooperative and fresh · 0 open objections |
| Implement | ✅ 3 of 3 checks | Local review ✅ Approve · commit `0c0f521` |
| Code review | ✅ 2 of 2 approve | [PR #20](link) · 0 open findings |
| Merge | ✅ Handed off | [Handed off for merge](link): a human reviews and merges |

<details><summary>Rounds</summary>

| Step | Round | claude | codex | Reports |
| ---- | ---- | ---- | ---- | ---- |
| Intake | 1 | — | ✅ Proceed | [Reports](link) |
| Design review | 1 cooperative | ✅ Approve | ✅ Approve | [Reports](link) |
| Design review | 1 fresh | ✅ Approve | ✅ Approve | [Reports](link) |
| Code review | 1 | ✅ Approve | ✅ Approve | [Reports](link) |

</details>

[Audit state](link) · checkpoint reference
```

- **Progress line:** every step, the current one in bold with its status emoji.
- **Step rows:** status uses the comment format vocabulary, plus **▶️ In progress**
  for the current step and plain "Pending" for steps not reached. A step that loops
  shows its latest round: "🛠️ Revise · 2 open objections", "🧭 Reopened · round 2".
- **Detail column:** engine-generated counts and links, and at most one model
  headline, serialized as a table cell. Never model prose.
- **Rounds:** a collapsed table of every vote by round, each round linking to its
  report archive, so revision history stays visible without any report text here.
- **Action line:** when a human must act, one line under the table names it and links
  the actionable comment.
- The stage-label fallback line keeps its position under the heading.

## Report archive

The audit artifact cannot be the archive: the API returns only an artifact's newest
30 versions ([measurement](../artifact-measurements.md)), so a report compacted out of
the checkpoint could become unreachable.

- **One archive artifact per completed round** (an intake assessment, a design
  review phase, a local validation attempt, a code review round), named
  `Concurro reports · <step> · round <n>`. Its body is a marker, then each report of
  the round rendered as in comment format 2, in full. It is created once and never
  edited, so its only version is permanent.
- **Bounded:** a round's reports are already bounded for the checkpoint. A round whose
  rendering would exceed the artifact limit is split into numbered parts; the event
  size fallback that drops collapsed sections never applies to an archive.
- **Recovery:** before creating it, the controller looks for an App-authored archive
  with the round's marker. One with the expected body is the archive; one with any
  other body is a conflict that blocks the task.
- **Discovery:** checkpoint discovery matches only the audit and plan artifacts'
  names and markers, so archives never become candidates for task state.
- **Compaction:** a report's text is excerpted only once its round's archive is
  confirmed, and still only while no issue event or PR review that renders it is
  pending. The excerpt note links the archive.

## Actionable comments

| Comment | Posted when | Continuation it offers |
| ---- | ---- | ---- |
| ❓ Input needed | The task enters `clarify` | Autonomous: a reply or an issue edit. Manual: a reply, then `/concurro start` |
| 📣 Escalated | The task enters `escalated` | Edit the issue if needed, then `/concurro retry`, in either mode |
| 🛑 Blocked | The task enters `blocked` | `/concurro retry` when the reason allows it; otherwise operator reconciliation, which the reason names |
| Plan ready for approval | Manual mode reaches `planned` | The exact `/concurro approve <plan>` command |
| ✅ Handed off for merge | The hand-off holds (below) for the PR's head | A human review and merge, under GitHub's own requirements |

- **Once per occurrence:** escalated, blocked and plan ready are keyed by stage and an
  occurrence number that advances each time the task leaves that stage. The hand-off
  is keyed by PR head, so handed off, then checks pending, then handed off again on
  the same head posts once.
- **Recovery:** each is an immutable comment published through the existing outbox,
  with its marker first and the same exact-body recovery.

No other events are posted: controller narration, QC and plan events, local
validation, design reopens, PR publication and the issue's copies of PR reviews all
become status comment rows. The PR is already linked from the issue by its
`Closes #N`.

### Handed off for merge

The hand-off says Concurro's work on the PR is done. It does not claim GitHub will
allow the merge: proving that would mean evaluating every protection rule, such as
required deployments, which the engine does not. The comment says so, and points to
the PR for GitHub's own requirements.

It holds when every remaining gate reason is one of:

- "autonomous merge is disabled" or "autonomous merge is limited to configured
  low-risk paths", Concurro's own merge policy; or
- "GitHub mergeability is not clean", while GitHub reports the PR mergeable without
  conflicts. A conflict is not handed off: the PR shows it, and the Merge row says so.

Any other reason is one of Concurro's own gates, such as an open finding, a failing
configured check or a stale approval. The Merge row then shows 🚧 with the reasons,
and no comment is posted.

## Input needed

```markdown
### ❓ Input needed

@author, Concurro needs answers before it can continue.

**Q1.** Should the note go before or after the RUSTFLAGS note? …
**Q2.** …

Reply in a comment or edit the issue description; the author or any writer may answer.

<sub>Plan QC · codex · round 1</sub>
```

- **Sources:** the questions of the step that sent the task to `clarify`: the intake
  QC, the planner, or the plan QC votes collected so far in that round (the round
  stops at its first clarify vote). Identical questions are merged; each is tagged
  with its provider. The skills ask that each question say why it matters and offer
  the options seen.
- **Mention:** only the issue's author, and not when the author is a bot.
- **Continuation:** as in the table above. In manual mode with an existing PR (a
  design reopened into clarification), `/concurro start` resumes planning against the
  existing head when only the discussion changed, as autonomous refresh already does;
  today it refuses that state.
- **Request record:** the checkpoint keeps each request's number, comment, questions,
  state (`open`, `answered` or `superseded`) and, once answered, the answer: the first
  new comment from the author or a writer after the request, or the issue digest that
  changed.
- **Answered:** only a recorded answer marks a request answered. Leaving `clarify` for
  another reason, such as a base change, does not; the request stays open. Marking it
  edits the comment in place: the marker first, then "✅ Answered" with a link to the
  answering comment or "the issue description was edited", then the questions.
- **Escalation after an issue edit:** `/concurro retry` from a terminal judgment
  re-reads the issue's requirements, as `/concurro start` does, in both modes. With an
  existing PR, changed requirements take the requirements-reopen path autonomous
  refresh already uses. Today manual mode instead blocks on the changed issue.
- **Superseded:** a new clarify with different questions while a request is open
  marks it "Superseded by" the new request, which is then posted. Identical questions
  post nothing new.
- **Edit recovery:** before the `PATCH`, the record stores the intended edited body
  and its state as `intended`. A comment found with exactly that body completes it;
  one with the original body is edited again, which is idempotent; any other body is
  a conflict that blocks.
- **Gateway binding (new):** the gateway admits issue-comment `PATCH`es today without
  checking author or marker. It is bound to App-authored comments that start with the
  status comment's or a request's marker, read before the write as for PR replies.

## Report length

The shared instructions and the review skills say:

- An approval is its headline, plus at most two sentences when what was verified is
  not evident from the change.
- Cite evidence where it supports a finding, objection or non-obvious claim; do not
  repeat evidence another report already cites, or restate what the controller
  enforces.
- Omit boilerplate, such as noting that no execution results are claimed.

Structured evidence (QC sources and assessments, finding anchors) is unchanged.

## PR title and description

The PR is titled with the plan's headline, cut to 72 characters, with the issue
number: `Use locked builds in the README build instructions (#19)`. The plan schema
gains the same `headline` field as the reports; a plan without one falls back to its
summary's first sentence. The description is the plan's summary, a link to the plan
artifact, and `Closes #N`. It does not claim CI or review status, which the PR shows
natively.

## Compatibility

- **Queued events:** events queued before the change still publish and recover as
  today; the change only stops queueing new ones.
- **Compaction:** reports published as issue events before the change keep their
  existing excerpt notes; new reports are gated on their archive.
- **Existing issues:** earlier events stay. The status comment is rewritten on the
  next save, as always.

## Rollout

1. Report archives and archive-gated compaction; the status comment dashboard; stop
   queueing non-actionable events. Engine only, no bundle change.
2. The actionable comments, request records and the answered and superseded edits;
   the gateway's comment `PATCH` binding; the hand-off rule; manual `/concurro start`
   resuming clarification with a PR, and `/concurro retry` re-reading requirements.
3. Report length in the instructions and skills, the plan `headline`, and the PR
   title and description. A bundle change: the images are rebuilt, and in-flight
   tasks block for reconciliation.

## Testing

- The happy path posts no issue comments besides the status comment and the hand-off,
  writes one archive per round, and the status comment shows every step's
  outcome, the rounds table and the links.
- A revision, a design reopen and an open finding change only the status comment.
- An archive with a lost response is found by marker, not created twice; one with a
  different body blocks; compaction never excerpts a report before its archive is
  confirmed or while an event or PR review rendering it is pending.
- Clarify from intake, the planner and a plan QC round posts one request; a reply or
  an issue edit records the answer and marks it answered; a base change does not; new
  questions supersede an open request; manual mode resumes with `/concurro start`,
  including with an existing PR.
- A lost answered edit completes on the next tick by its exact body.
- Escalated, blocked and plan ready each post once per occurrence; the hand-off posts
  once per head, never with a conflict or with any of Concurro's own gates open.
- Manual `/concurro retry` after an escalation and an issue edit re-plans from the
  edited issue, with and without an existing PR.
- The gateway refuses a comment `PATCH` on a human comment or one without a Concurro
  marker.
- An archive of an oversized round keeps every report in full, split into parts.
- Queued pre-change events still publish byte for byte.
- Approval reports in the pilot fixtures stay within the new length guidance.
