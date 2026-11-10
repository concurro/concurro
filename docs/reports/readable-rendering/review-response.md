# Readable-rendering review corrections

Follow-up: [round-two corrections](review-response-round-2.md).

Scope: findings 1–12 under “Must fix before deployment”, findings 13–14, and the C test
gaps in [the review](review.md). Changes are uncommitted. No live GitHub writes were made.

| Findings | Correction | Regression coverage |
| ---- | ---- | ---- |
| 1, 13 | Escape whitespace-only code spans literally; escape pipes only in table cells. Formats 1–3 stay unchanged. | HTML parsing of finding headings, tables and report views; code-span pipe tests. |
| 2 | A resumed pending submission uses the service journal. Only no prior submit or definite failures permit a send; unknown, in-flight, successful and operator-resolved attempts prohibit one. | Unsent, 401, throttled and disallowed sends; restart; worker crash after the submitting checkpoint and before send. |
| 3 | Adopt an exact legacy body and retain its digest before replacing the plan or candidate, including invalidation. Human edits still stop refresh. | Controller/checkpoint runs from a legacy PR through invalidation and structured refresh, with and without a human edit. |
| 4 | New schemas require non-whitespace description/commit objects. Invalid results receive bounded repair feedback before any candidate edits are applied. | Null description, null commit and whitespace-only content each receive a corrected model attempt. |
| 5, 6 | Use full cross-repository issue URLs in commit references. Neutralize closing keywords, references and mentions in model PR/commit prose; keep controller links active. | First and repair commit messages, description fields, review focus and validation. |
| 7, 8 | Give actionable gate failures a WARNING. Use explicit engine reason mappings for operator-only guidance instead of scanning model prose. | Draft, CI failure, closed PR, unresolved threads and external head changes; operator-word false positives and missing operator reasons. |
| 9 | Compute criterion labels only for design QC; commit the event sequence after descriptor validation. | Four pending intake events for a large issue; no copied labels, no intake criterion-limit call, no lost sequence on failure. |
| 10 | Permit another compensating delete after a definite failure, retaining the old receipt and atomically linking the new attempt. | Failed/unsent/throttled deletes, restart, in-flight delete recovery, unknown delete remaining stopped. |
| 11 | Render validation as inert prose so it cannot introduce headings or task-list attestations. | Model-supplied checked boxes and headings inside a mapped template section. |
| 12 | Handle null GitHub authors without dereferencing them. | Unrelated deleted authors are ignored; matching-marker/null-author reviews conflict; gateway admission refuses null ownership. |
| 14 | Preserve inline code in format-5 summaries, objections and other inline report fields. | Parsed HTML assertions and updated format-5 goldens. |

The submission guard also addresses finding 18: manually clearing an unknown submit does
not make another submit admissible.

## C integration coverage

`service/tests/test_pending_worker_recovery.py` explicitly uses the default `pending-v1`
protocol through the worker, controller, checkpoints, external report storage and gateway.
It verifies large frozen bodies are detached and restored, then completes thread mapping
and resolution after lost thread/reply responses, operator compensation, definite submit
failure, a pre-send checkpoint crash and a post-submit crash. It asserts no standalone
REST review/reply writes and no duplicate submitted report markers.

Additional tests cover all five mutations through `settlement.settle`, including malformed
results, errors and explicit null threads; foreign/submitted/closed reviews; another task's
marker; wrong submit markers; replies outside finding threads or on another PR; mixed REST
and pending outbox items; and registry v3→v4 migration preserving an unknown write.

The prior review overstates the old coverage gap: `ServiceRun.test_finding_lifecycle` already
used the default protocol. The new tests make that contract explicit and add the missing
failure and storage boundaries.

## Validation and remaining scope

- Engine: 872 tests, passed, one skipped.
- Service: 340 tests, passed.
- Gateway: 57 tests, passed.
- Ruff lint/format, Pyright and `git diff --check`: passed.

The other secondary/presentation findings have not received a complete acceptance pass in
this correction scope. The live same-ID retry experiment remains outstanding; uncertain
resubmission stays disabled. The bundle/schema changes still require the documented idle
window or explicit writer recovery before deployment.

The five existing unsigned commits were authorized by the user's explicit request to
implement B→A→C→D→E and commit once per PR without pushing. This correction pass leaves
those commits unchanged and keeps all fixes uncommitted for review.
