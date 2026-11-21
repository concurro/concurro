# Phase 3 M1 acceptance evidence

M1 is accepted for local implementation. [M2](rust-phase-3-m2-evidence.md) is
also accepted locally; M3 remains in progress. This is
not production qualification; no live GitHub, provider or runner acceptance is
claimed. The Python task controller remains the behavior oracle. Python runtime
changes are limited to the planned retained exchange bridge.

## Reviewed source

Base: `e99403956eb850bf582ca353146f9932d6d91328`.

| Snapshot | Manifest SHA-256 | Independent validation |
| ---- | ---- | ---- |
| M1 runtime 0800 | `143e88e89912aa4f49e82b1b30eaec58f5065b959ebfd881d3febc3d1e765950` | Full Rust workspace: 123 passed, none skipped |
| M1 closure delta 1000 | `92ec3f2bffa343009b69af3c1a7f9ef23b9456c12d15141d7f72cade3c9cd88c` | Five focused regressions passed |

The closure delta shares JSONB representability checks, preserves exact event wire
bytes, treats exchange authentication failures as polling outages, and keeps ended
attempts unchanged on stop. All other runtime source matches the reviewed snapshot.
The full suite was run before this delta; the five closure tests were then run
independently on the exact delta. The implementation checks also passed formatting,
all-targets compilation and 27 Python contract tests.

## Recovery evidence

The actual task executable runs against PostgreSQL, NATS and controlled GitHub,
exchange and S3 endpoints. The test kills and restarts it during submit response,
poll response and notice publication. It verifies one attempt and dispatch, marked
comments without duplication, one accepted report and finished fact, retirement,
ref cleanup, outbox delivery and exact archived gzip bytes.

Separate tests cover source ordering and authorization, delayed stop and retirement,
unknown writes, polling outages, budgets, report repairs, malformed transcript data,
and the retained Python exchange's tombstone and session-retirement behavior.
Claude Opus 5.5 reviewed the runtime checkpoint; its remaining concrete findings
were closed by the independently inspected and tested delta.

## Reproduction

With the repository's Python test dependencies and Docker available:

```bash
cargo nextest run --workspace --no-fail-fast --test-threads 1
```

The committed tests include the closure regressions. The composed runtime test uses
controlled HTTP services; it does not substitute for later authorized live checks.
