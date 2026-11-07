# Comment format

Status: implemented. Shared rendering rules for native PR reviews, finding threads,
issue notices and the [dashboard](issue-dashboard.md). The dashboard owns current issue
layout; this document does not prescribe a second status or rounds table.

## Layout and identity

Lead with the outcome and a concise headline. PR review headings and finding roots name
Concurro explicitly, with the reviewer profile/model identified; an icon is never the
only indication of authorship. Review details can be collapsed, while actionable findings
keep their evidence. An approval should not repeat the plan or controller-enforced rules.

The enrollment comment uses a short acknowledgment followed by a compact roster: role
columns, model and effort together, paired reviewers in their role's cell. It has no
redundant “I'm on it” heading. The current dashboard's activity alert and stage table
carry progress after enrollment.

| Meaning | Rendering |
| ---- | ---- |
| Review verdict | ✅ Approve · 🛠️ Changes requested · 🧭 Design reopened |
| Intake/design decision | ✅ Proceed or Approve · 🛠️ Revise · ❓ Clarify · 🛑 Reject · 📣 Escalate |
| Finding severity | ‼️ Critical · ❗ High · ⚠️ Medium · ℹ️ Low |
| Finding status | 📌 Open · ✅ Fixed · 🗃️ Obsolete · ↩️ Withdrawn |
| Active stage | ▶️ In progress |

Severity is presentation, not a bypass: every open finding blocks readiness. Older findings
without severity/title retain their compatible rendering. Presentation fields are
normalized; substantive verdicts, paths, evidence and dispositions are validated separately.

## Markdown and model text

Use `engine/markup.py` rather than interpolating untrusted prose into structural HTML.

- Blocks preserve Markdown, inline code and fenced code. Outside code, `<` is escaped so
  model text cannot close a surrounding section or introduce HTML comments. An unfinished
  fence is closed before the surrounding content resumes.
- Headlines and titles collapse whitespace, escape Markdown/HTML syntax and shorten to
  their display limits. A model identifier or path uses the inline-code serializer.
- Table cells escape pipes and keep model text on one line. The renderer may insert its
  own `<br>` between reviewers; model HTML is not accepted. Nested tables are not used.
- Links use controller-constructed destinations and escaped labels. Collapsed sections
  have blank lines around their Markdown content.
- Shortened blocks carry an explicit excerpt note and have fences re-closed. Full evidence
  remains in the report archive; display shortening is not report validation.

Concerns use the block renderer inside indented list items, preserving code spans and
blocks. The heading is “Intake approved with concerns”. Active Blocked notices keep their
continuation instructions; resolved notices show only the muted original heading/reason
and the resolved message, as specified in the dashboard contract.

Markdown rendering is not a notification sanitizer: `@mentions` are not suppressed by
these helpers. Do not claim that text escaping prevents notifications or prompt injection.

## PR presentation

A PR review shows the verdict, headline, new findings ordered by severity/ID and earlier
finding dispositions. Findings attributable to changed files get inline or file-level
threads according to [the review contract](pr-reviews.md#publication-and-placement).
Findings without a changed file stay in the review body. An approval with no findings
omits empty tables and blocker explanations.

The PR title comes from the plan headline (or legacy summary fallback), with the issue
number. Its body describes the planned change and links the plan and originating issue;
it does not invent CI results or human approvals. Cross-repository issue references must
identify the issue repository rather than using an ambiguous local `#N`.

## Recovery and capacity

Markers, authors and recorded formats identify published objects. A missing format means
the legacy format, not whichever renderer is newest. Queued bodies and outbox items must
render byte-for-byte identically on recovery. The dashboard itself can be regenerated
from state on a save; its machine-readable checkpoint reference remains intact.

Rendered comments and artifacts have their own bounds. Excess display text is excerpted
with a report link; full reviews are stored through [report storage](issue-dashboard.md#report-archive)
within the model-response transport bound. Historical publication receipts retain both
full and core-report digests where needed, so advisory-rating trimming cannot prevent
later text compaction. Full-body identity still governs publication recovery.
