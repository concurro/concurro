# Phase 3 M3 local acceptance

Accepted locally on 2026-11-21. M1 is committed; M2, M3 and the corresponding
backoffice frontend changes remain uncommitted for owner review.

## Implemented scope

| Requirement | Implementation and evidence |
| ---- | ---- |
| Contribution lifecycle and recovery | M2 acceptance plus the final workspace regression run; durable intake, planning, approval, implementation, review, publication and recovery. |
| Funding and label enrollment | Source-ordered consent, immutable attempt payer/version, withdrawal cutoffs, credential fallback and autonomous-label authority fixtures. The later org-funded admission blocks when label authority is removed. |
| Standalone reviews | Independent rounds, current-panel completion, native publication, finding dispositions, cancellation/reopen, self-withdrawal and credential recovery fixtures. |
| Backoffice and commands | Separate read process, current viewer checks, typed event projection, baseline recovery and signed browser action replay. SQL lives in `concurro-backoffice-postgres`; settings use `concurro-config`. |
| Existing frontend | V2 task/review/issue lists, historical plans and findings, funding panels, commands, imported turn history and diagnostics. Adapter tests exercise baseline and live-event turn records, artifact reads and withdrawal state. |
| Archive and cutover | Offline original archive import, historical identity preservation, source activation and exchange slot floors, contribution-only historical PR ownership. |
| Delivery gaps | Independent source reconciliation, recorded ambiguous sources, bounded retry backoff and task/index refresh. |
| Packaging | Task and backoffice-read images build; frontend production build and generated contract checks pass. |

The implementation map is
[phase-3-m3-map.md](proposals/architecture/phase-3-m3-map.md).

## Verification

- Full Rust workspace baseline: 197 tests, 193 passed initially. Four failures
  occurred during local PostgreSQL/NATS fixture startup; all four passed on the
  isolated rerun. Original failures were retained in the local evidence.
- Event fault-injection suite: 13/13 passed.
- Final Rust storage, event and mode corrections: 14/14 focused integration tests
  passed, supplementing the full workspace baseline. Unchanged suites were not
  repeated after those corrections.
- Python engine, service, gateway, backoffice and deployment suites: 2,418 tests
  across 193 test files passed. Files initially blocked by missing local packages
  passed after installing the repository's pinned requirements.
- Python contract fixtures: 27/27 passed. Schema compatibility and regenerated
  Rust/Python contract drift checks passed.
- Frontend: 37/37 tests passed, including the new adapter regressions; type checking
  reported zero errors/warnings and the production build passed.
- Rust formatting, Python lint/format/type checking, shellcheck, bundle packaging
  and whitespace checks passed. Both changed Rust service images built locally.
- Claude Opus 5.5 high reviewed the integrated implementation and corrective pass.
  The coordinator verified the remaining UI/history/storage corrections narrowly
  and independently reran the focused Rust and frontend tests.

## Operational limits

The dedicated live rehearsal was waived. Local fixtures and image builds do not
prove live GitHub publication, installation permissions, deployed runner behavior
or the production cutover. No deployment, push, live retry or cutover was performed
as part of this acceptance.

Unknown GitHub writes can require visible operator repair. A coverage gap with an
edited command whose editor cannot be authenticated remains visible and retries
with backoff. Neither condition grants new authority or triggers unbounded model
retries. These are accepted operational limitations under the repository's
application-level risk tolerance.
