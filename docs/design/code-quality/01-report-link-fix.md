# 01: Audit report-link fix

Part of the [code-quality programme](README.md). This iteration is a **bug fix**. It is the
only iteration allowed to change observable output, and it changes `design_audit.py`
output only.

## Goal

Make `engine/design_audit.py` read multi-part design-review reports correctly.

The archive writer (`conversation.publish`, `engine/conversation.py:439`) has always written
part links as `[Full report part N](…)`. The writer's format dates from `3cf070f`; the
parser came later, in `161d783`. The audit parser (`archived_report`,
`engine/design_audit.py:79`) only matches `[Full report, part N](…)`. Because of this,
multi-part archives find no part links, fall through to `json_block`, raise, and are
recorded as `unavailable: ValueError`. Single-part reports are unaffected.

## Scope

- `engine/conversation.py`
- `engine/design_audit.py`
- `engine/tests/` (the audit tests)

## Changes

1. **Single source of truth for the link format.** In `conversation.py`, add a small
   function that formats one link, `report_link(root, index, count, number)`, and a
   compiled regex, `REPORT_LINK`, that matches the same format. Keep them next to each
   other. `publish` must produce exactly the same text as today, character for character.
2. **Use the shared regex in the audit tool.** `design_audit.archived_report` uses
   `conversation.REPORT_LINK`, bound to the artifact root, instead of its own pattern. It
   should accept the writer's format: an optional `part N`, without a comma.
   - Also accepting the comma form is optional. Nothing has ever written it, so prefer the
     writer's exact format.
3. **Use the existing event-marker constant.** Replace the hard-coded
   `'<!-- concurro:event:v1:'` literal in `design_audit.py:76` with `conversation.MARKER`.
   The value must be identical.

## Must not change

- The archive body text written by `conversation.publish`.
- The `design_audit.py` output for single-part reports.
- `docs/reports/design-review-baseline-19.json` and its Markdown. Pilot #19 had no
  multi-part reports, so they remain valid. Don't regenerate them.

## Tests first

- **Failing round-trip test first:** build a multi-part archive with the real writer path
  (or the same helpers the existing audit tests use), then run `measure()` /
  `archived_report()` on it. Before the fix, show that the record is `unavailable`; after
  the fix, it must be `verified`.
- **Exact writer output:** a unit test that pins the text `report_link` produces for a
  single-part and a three-part report, as hand-written literal strings.
- **Parser against a literal body:** a test that runs the audit parser on a hand-written
  three-part archive body, a literal string not produced by the writer. A shared
  regex cannot then hide a format that is wrong in both places.
- **Shared test pattern:** `engine/tests/test_pipeline.py:155` has its own copy of the
  pattern. Switch it to `conversation.REPORT_LINK` so the test and the code can't drift.

## Done when

- The new round-trip test passes, and fails if the fix is reverted.
- All suites and validation commands are green. Test count goes up by the number of new tests.
- No other output changed.

## Commit message

```text
fix(engine): read multi-part design reports in the audit tool
```
