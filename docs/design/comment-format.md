# Comment format

Status: accepted (revision 3, reviewed); rollout steps 1–2 implemented. Gives Concurro's GitHub comments, reviews and
threads one scannable, professional layout and a visual vocabulary of its own,
distinct from Copilot code review.

## Problem

Every Concurro comment is a heading, a metadata line and a wall of model prose
(issue #16 in the pilot). A human has to read each one to learn its outcome.
Copilot's reviews lead with the verdict, list findings in a table and collapse
the rest, but Concurro must not look like Copilot: both may review the same PR,
and a human must never mistake one for the other.

## Decisions

| Question | Decision |
| ---- | ---- |
| Layout | Outcome first: one heading with the verdict, one headline sentence, then at most one table (the status comment has two). Everything else in `<details>`. Metadata on one `<sub>` line at the end |
| Identity | Every PR review heading and finding thread names Concurro in text; symbols never carry identity alone |
| Vocabulary | One fixed set (below), no coloured circles. An emoji always precedes its text label; none is decorative |
| Severity | PR review findings gain `severity` and `title`. Presentation and ordering only: every open finding still blocks the gates |
| Compatibility | Comment identity is the marker, which no format changes. Each event, outbox item and finding records the format it was created in and renders in it forever; a missing format means format 1, never inferred |
| Headline | An explicit `headline` field on every report, with first-sentence extraction only for reports that lack one |
| Model text | Inert: it cannot open or close HTML, leave a code fence open or break a table. Presentation fields are normalized, never a reason to reject a report |
| Scope | Issue conversation events, the status comment, PR reviews, finding threads, replies and withdrawal notices. Plan artifacts and checkpoints are unchanged |

## Vocabulary

| Meaning | Rendering |
| ---- | ---- |
| Review verdict | ✅ Approve · 🛠️ Changes requested · 🧭 Design reopened |
| Intake and plan QC decision | ✅ Proceed or Approve · 🛠️ Revise · ❓ Clarify · 🛑 Reject · 📣 Escalate |
| Severity | ‼️ Critical · ❗ High · ⚠️ Medium · ℹ️ Low |
| Finding status | 📌 Open · ✅ Fixed · 🗃️ Obsolete · ↩️ Withdrawn |
| Check | 🟩 Pass · 🟥 Fail |
| Merge gate blocker | 🚧 |
| Task blocked, for operator reconciliation | 🛑 |
| Task escalated for a human decision | 📣 |

Controller events that report progress carry no emoji.

## Layout rules

- **Heading:** `### <emoji> <subject> · <actor> · <Outcome>`. On the PR the subject
  is `Concurro review`; on the issue it is the stage (`Plan QC`, `Code review`,
  `Local validation`). The issue is Concurro's own thread, so its headings omit the name.
- **Headline:** the report's `headline` field, normalized like a finding title
  (see [Model text](#model-text)) to one line of at most 160 characters. A report without one (recorded before rollout
  step 2) falls back to the first sentence of its main text (`summary` or
  `rationale`): split at the first `.`, `!` or `?` followed by whitespace, after at
  least 20 characters, and only if the result is at most 240 characters; it is
  then normalized the same way. Otherwise the whole text is shown expanded, so
  nothing is hidden without a visible headline.
- **Tables:** one row per item, with only single-line cells: IDs, labels, titles,
  paths, short hashes and links. Free text never goes in a table, and every
  dynamic cell is written by one cell serializer (see [Model text](#model-text)).
- **Collapsed sections:** `<details><summary>Label</summary>`, a blank line, the
  content, a blank line, `</details>`; GitHub renders Markdown inside only with both
  blank lines.
- **Metadata:** one `<sub>` line: event number, model, plan (12 hex characters),
  round, phase, head (7 characters, linked to the commit). The visible ISO timestamp
  is dropped; GitHub shows the time, and the event's marker still binds it.
- **Ordering:** findings by severity, then ID.

## Layouts

### PR review

```markdown
### 🛠️ Concurro review · codex · Changes requested

The note says release builds lock the fuzz lockfiles, which they do not.

| Severity | Finding | Where |
| ---- | ---- | ---- |
| ❗ High | **F3** Note claims the fuzz lockfiles are locked | `docs/release-process.md:79` |
| ℹ️ Low | **F4** Fragment is not a complete sentence | `changelog.d/release-process-locked-builds.changed` |

Every open finding blocks the merge, whatever its severity.

**Earlier findings:** [F1](thread link) ✅ Fixed · [F2](thread link) 📌 Open

<details><summary>Full review</summary>

…summary…

</details>

<sub>codex / gpt-6.1-sol · head 5fd0814 · round 2</sub>
```

- New findings link to nothing: their threads do not exist until the review is
  posted, and published reviews are never edited. Earlier findings link to their
  opening comments.
- A finding with no file has no thread; it gets its row (`Where`: `general`) and
  its detail under a second collapsed section, `Findings without a file`.
- An approval with no findings and no earlier findings omits both tables and the
  blocking note.
- A design reopen is `🧭 Concurro review · <provider> · Design reopened`, with the
  issue's explanation as headline and its evidence as a list.

### Finding threads

```markdown
<!-- concurro:finding:v1:F3 -->
❗ **Concurro · High · F3 · Note claims the fuzz lockfiles are locked**

…detail…

<sub>Concurro review · codex</sub>
```

```markdown
<!-- concurro:disposition:v1:F3:5 -->
✅ **Fixed** in 9c1e2ab: Lines 79–81 now name only the root workspace lockfile.
```

```markdown
<!-- concurro:withdrawn:v1:F3:2 -->
↩️ **Withdrawn**: the design was reopened. codex will re-assess this finding under the new plan.
```

Markers are unchanged. The text after the marker follows the finding's recorded
format, so an opening posted in format 1 is still recognized by its format-1 prefix.

### Issue events

```markdown
### ✅ Plan QC · claude · Approve

The plan is a small docs change limited to the two files the issue names.

<details><summary>Rationale</summary>

…rationale, concerns and questions with their IDs…

</details>

<sub>Event 4 · claude-opus-5-5 · plan 1f8a3b7eea58 · round 1 · cooperative</sub>
```

| Event | Outcome first | Collapsed |
| ---- | ---- | ---- |
| Plan updated | Plan summary as headline; artifact link | Changes, review focus |
| Plan QC, design review | Decision; objection IDs as a table (ID, status) | Rationale, concern text, dispositions |
| Local validation | Checks as a table (🟩 or 🟥 per check, exit code); local reviewer's verdict as a second heading line | Review summary and findings |
| Code review (PR exists) | Verdict; counts by severity; link to the PR | Summary |
| Code review (no PR) | As the PR review, without thread links | Summary |
| Controller progress | One sentence; no emoji | Nothing |
| Gates blocked | `### 🚧 Merge gates · Blocked`, reasons as a list | Nothing |
| Blocked | `### 🛑 Task blocked`, reason as headline | The full reason when longer than the headline |
| Escalated | `### 📣 Escalated`, the decision needed as headline | The full reason when longer than the headline |

### Status comment

```markdown
## Concurro

| Stage | Plan | PR |
| ---- | ---- | ---- |
| Gates | [1f8a3b7eea58](artifact link) · round 1 | #17 · head 5fd0814 |

| Reviewer | Design | Code review |
| ---- | ---- | ---- |
| claude / claude-opus-5-5 | ✅ Approve | ✅ Approve |
| codex / gpt-6.1-sol | ✅ Approve | ✅ Approve |

🚧 Merge blocked: autonomous merge is disabled; GitHub mergeability is not clean.

📌 2 open findings · every open finding blocks the merge, whatever its severity.

<details><summary>Plan, QC and local evidence</summary>

…

</details>

[Latest event](link) · checkpoint links
```

The stage-label fallback line (`Stage: **…**`) keeps its position directly under
the heading, so its detection is unchanged.

## Finding severity and title

The PR review schema's findings gain two required fields:

- `severity`: `critical`, `high`, `medium` or `low`. The skill defines them:
  critical breaks consensus, security, funds or data; high breaks required
  behaviour or an acceptance criterion; medium is a real defect with limited
  impact; low is a minor correctness or clarity issue.
- `title`: a short label for the finding, normalized to one line of at most 100
  characters (see [Model text](#model-text)).

Both are stored in the ledger. Findings recorded before the change have neither:
they render with no severity label and their detail's first sentence, cut to 100
characters, as title. Severity never changes the gates, the verdict rules or
repair priority. Advisory, non-blocking findings are out of scope; if added
later, they would carry an `Advisory` label.

Local review keeps its contract and renders without severity.

## Compatibility

Every published comment is found again by its marker and author, which no format
changes; its content is then compared with the body rendered in the item's
recorded format. A missing `format` always means format 1, and format 1 renders
through the current code, byte for byte. So an item created before the change
neither re-renders differently nor fails to find, or duplicates, its comment.

- **Issue events:** each queued event records `format: 2`. An event without it
  renders through the current code, byte for byte, so queued events still match
  their content digests and posted events their receipts. Pre-rendered events
  (no source) store their body and are unaffected.
- **PR outbox:** each item records its format. A review item queued before the
  change publishes, and is recovered, with the `v1` body.
- **Findings:** each ledger entry records the format of its opening comment, and
  `opening_matches` uses that format's prefix.
- **Status comment:** rewritten on every save; no compatibility constraint.

## Model text

GitHub sanitizes dangerous HTML, so the concern is presentation integrity: model
text must not change the structure around it.

- **Placement:** free model text goes only in collapsed sections and thread
  bodies, each a block of its own. Titles and headlines are the only model text
  outside a block.
- **Blocks:** outside code spans and fenced code, every `<` is written as `&lt;`.
  It renders as `<`, but no tag, HTML comment, processing instruction or raw HTML
  block can start, so none can swallow the closing tag or metadata after the
  block. Code spans and fenced code are untouched, so `Vec<u8>` renders as
  written. An unclosed fence, backtick or tilde, of any length, is closed at the
  end of the block with a matching fence. Headings and rules inside a block stay
  inside its section.
- **Titles and headlines:** normalized, never rejected. Whitespace, including line
  breaks, collapses to single spaces; leading block syntax (`#`, `>`, `-`, `*`, `+`,
  a numbered item, `---`) is backslash-escaped; `<` is escaped as in blocks; every
  inline Markdown metacharacter (`` \ ` * _ [ ] ( ) ! ~ ``) is backslash-escaped,
  so a title or headline renders as plain text, never as a link, image, emphasis
  or code; the result is cut to its limit with `…`. Only an empty result falls back: a title to
  the detail's first sentence, a headline to the first-sentence rule. Substantive
  fields (verdicts, paths, lines, dispositions, details) are still validated
  strictly.
- **Table cells:** every dynamic cell, whether a path, link, hash, ID or title, is
  written by one serializer with three kinds of value: plain text, escaped like a
  title; inline code; and links whose URL and label the engine builds. It
  collapses whitespace, escapes `|` as `\|`, which GitHub requires even inside code
  spans, and wraps code in a backtick fence longer than any backtick run in the
  value. No value is assumed safe because the engine
  produced it.
- **Size:** every rendered event stays within the existing 24,000-byte bound, and
  the status comment within 60,000. An event that would not fit drops its collapsed
  sections for one note pointing to the audit state; its outcome-first lines remain.

## Rollout

1. Vocabulary, layout helpers, escaping and format versioning; issue events and
   the status comment. No bundle change.
2. Report `headline` in the intake, plan QC, local review and review schemas;
   finding `severity` and `title` in the review schema and ledger; the skills; PR
   review, thread, reply and withdrawal layouts. A bundle change: the images are rebuilt,
   and in-flight tasks block for operator reconciliation.

## Testing

- Golden renderings for each event kind, the status comment, PR reviews (findings,
  earlier findings, no file, approval, reopen), threads and replies.
- Events, outbox items and findings created in format 1 render byte for byte as
  before, and recover after the upgrade without posting twice.
- Model text containing `</details>`, `<summary>`, an unclosed HTML comment, a
  processing instruction, a raw HTML block, unclosed and nested fences (backtick
  and tilde, three or more characters), headings and rules cannot change the
  structure around it; code containing `<` renders unchanged.
- Titles and headlines that are multiline, over their limit, or contain table,
  HTML, fence or block syntax are normalized and render inside the structure; a
  title such as `Vec<u8> length is not validated` reads as written, and a title or
  headline containing an image, link, emphasis or backticks renders as plain text.
- Table cells with `|`, backticks and newlines in paths, links and titles keep the
  table intact.
- Headline fallback: abbreviations and paths such as `Dockerfile:12. Next`, text
  with no sentence break, text over 240 characters, and text starting with block
  syntax.
- Rendered sizes stay within their bounds at the largest permitted reports.
