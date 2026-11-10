# Readable-rendering review: round-two corrections

Scope: R1–R4 in [round two](review-round-2.md), plus findings 15–17 and 19–25 in
[the first review](review.md). Corrections remain uncommitted; the five existing commits
are unchanged. No live GitHub writes were made.

| Findings | Correction and regression coverage |
| ---- | ---- |
| R1 | Neutralize actual references and mentions outside complete code spans. Preserve ordinary words, code identifiers and email addresses. Cover shorthand references, cross-repository references, issue/PR URLs, escaped/unclosed backticks and commit subjects. |
| R2 | Preserve code spans in every structured description field and validation, without truncating bounded fields. Parsed HTML tests also verify that validation cannot create headings or checklists. |
| R3 | Oversized events leave the stored sequence unchanged. A fixed-clock regression proves that the next identical event gets a distinct identifier and the next sequence. |
| R4 | All mapped reasons share constants with their producers, including the five missing operator cases. Tests check guidance and reject duplicated reason literals at production sites. |
| 15 | Mark format-5 views explicitly and parse canonical links only from their terminal Raw data section. Real single- and multipart archive write/read tests include misleading model links and a forged Raw data block inside code. Historical readers remain covered. |
| 16 | Rejected views use the controller diagnostic or its default, never the draft's own acceptance claim. Both rejection kinds are tested. |
| 17 | Long reasons show an ellipsis and retain their tail in Further detail, with an audit-state pointer when necessary. |
| 19, 20 | A configured mapping requires `.github/PULL_REQUEST_TEMPLATE.md`; a missing file blocks explicitly. Validate and pin before asking the implementer. Controller tests cover missing, mismatched and duplicated headings with zero develop calls. |
| 21 | PR titles and commit subjects share word-boundary truncation. The 72-character subject budget includes the conventional prefix. Cover initial and repair commits. |
| 22 | Show the merge-blocking explanation only for open or withdrawn findings. Test each status and update lifecycle snapshots. |
| 23 | Remove an identical leading headline from Explanation while preserving remaining Markdown and distinct explanations. |
| 24 | Append unmapped sections at the template's shallowest heading level, including templates with nested headings. |
| 25 | Intake concern details contain the concerns and report pointer, without repeating the heading or advisory-action message. The lifecycle test still verifies propagation to planning and both review stages without extra calls. |

Format-5 goldens now also cover intake rejection and local review. Only current-format
and dashboard snapshots changed; historical renderer fixtures remain unchanged.

## Validation

- Focused regression suites: passed.
- Engine: 885 tests passed, one skipped.
- Service: 340 tests passed.
- Gateway: 57 tests passed.
- Ruff lint/format, Pyright and diff whitespace checks: passed.

The first full engine run found three stale expectations: two assumed the report heading
immediately followed the event marker, and one expected the removed advisory sentence.
Their suites pass with assertions updated for the view marker and concise advisory text.

## Remaining release work

- Uncertain same-review resubmission stays disabled. The live validation experiment has
  not been run; it needs separate authorization for isolated diagnostic writes.
- Review and sign the changes, then verify CI for the exact release before deployment.
- Prefer an idle rollout after active tasks finish. The schema and skill changes from the
  previous round require this window or explicit writer `/concurro invalidate` recovery.
  The implementer-only retry exception does not apply. Do not rewrite approval bindings.
- At release time, check active tasks and the deployed engine/workflow identities using
  the [deployment guide](../../../gateway/README.md). Those live checks were not performed
  in this local correction pass.
