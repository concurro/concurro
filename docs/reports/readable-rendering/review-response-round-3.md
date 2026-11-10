# Readable-rendering review: round-three corrections

All three findings in [round three](review-round-3.md) are addressed. Changes remain
uncommitted; no commits, pushes or live GitHub writes were made.

| Finding | Correction | Verification |
| ---- | ---- | ---- |
| S1 | Neutralize model references in PR titles, including inside backticks, before applying the 72-character limit. | Headline and summary fallbacks; shorthand and URL references; mentions; truncation; ordinary prose and code identifiers. |
| S2 | Commit subjects and change bullets neutralize references inside backticks. Markdown descriptions retain their code-span exemption; controller-owned `Refs` lines remain active. | Initial and repair commits, with and without conventional prefixes, across all supported reference forms. |
| S3 | Regenerate A–E from current renderers and in-memory controller/publication fixtures. Include current titles and commit messages. | The generator's `--check` compares fresh output against all five documents. Fixed timestamps and attempt IDs make regeneration reproducible. |

Regenerate previews from the repository root:

```sh
python engine/tests/render_readable_previews.py
python engine/tests/render_readable_previews.py --check
```

## Validation

- Focused suites: 33 tests passed.
- Full engine suite: passed (887 tests run, one skipped).
- Preview consistency check: passed.
- Ruff lint/format, Pyright and diff whitespace checks: passed.

Service and gateway suites were not rerun for these title, commit-text and preview changes;
[round two](review-response-round-2.md) records their last results.

The live same-review submission experiment, bundle rollout window or explicit writer
invalidation recovery, and CI/deployment checks for the exact release remain outstanding.
Uncertain same-ID resubmission remains disabled. This change does not rely on GitHub
ignoring backticks in commit messages; that behavior has not been verified.
