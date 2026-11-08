# Code-quality refactoring programme

Status: see the iteration table below. Baseline commit `f1225e6`.

## Purpose

This programme improves structure and readability without changing behaviour. It acts on
a read-only audit against idiomatic Python, DRY, SOLID and KISS. The work is split into
iterations. Each iteration is **one deliverable and one commit**, reviewed before the next
one starts.

Only the approved iteration in the table is authorised. The candidates were reassessed
after iteration 07 (2026-11-08); the table shows the result. Reassessment continues after
each "Reassess" point. It may reshape, split or drop a candidate.

Every iteration has its own brief, listed below. A brief covers the goal, scope, specific
changes, what must not change, the tests to write first, and when the iteration is done.

## Principles

- **Extract concepts, not every repeated expression.** Worth extracting:
  - shared bookkeeping;
  - protocol values: markers, status sets, URL formats, wire constants;
  - real domain seams.

  An item that would need a helper parameterised around several differences, or would
  add a layer for a two-branch choice, can stay as it is. Report it rather than force it.
- **Split work that mixes concerns.** A brief that combines unrelated concerns, such as
  subprocess cleanup and session locking, is delivered as separate commits.
- **Readability over size targets.** A recovery sequence that reads clearly from top to
  bottom is better than one split to meet a line count. There is no method-length target.
- **Behavioural tests are authoritative.** Test counts and snapshots are checks, not proof
  of equivalent coverage.
  - Generated snapshots can preserve existing bugs. Review each generated fixture as if it
    were code, and keep independent expected-value assertions for the behaviour that
    matters.
- **Each brief lists what it may do, not what it must do.** If an item doesn't make the
  code clearer once you're in it, skip it and say why.
- **Judge candidates by maintainability value and risk, not by churn.** Refactoring is
  itself high-churn work.
  - How often a file changes can support a priority call, but low churn is never a
    reason to skip a change that materially reduces the maintenance burden.
  - Risk is managed by writing the guard tests first, not by avoiding the code.
  - Tests make a worthwhile refactor safe; they don't make a refactor worthwhile. Each
    change still needs a concrete maintenance benefit.

## Workflow

1. Codex implements **one** iteration, starting from the committed result of the previous one.
2. Codex leaves the work uncommitted and reports:
   - what changed;
   - every brief item it skipped or did differently, with the reason;
   - test counts before and after, for each suite;
   - the output of the validation commands.
3. Claude reviews. The writer commits, using the commit message in the brief or an amended one.
4. The next iteration starts. Update the status column below as iterations land.

Line numbers in the briefs refer to `f1225e6` and will drift. Find code by the symbol
named in the brief; the line number is a hint. If a brief item turns out to be wrong,
already done, or impossible without changing behaviour, **skip it and report it**. Don't
force it through.

## Ground rules (apply to every iteration)

### Behaviour must not change

- **User-facing text stays byte-identical:** issue comments, dashboard bodies, `Blocked`
  reasons, PR titles and bodies, notices, and log messages operators rely on.
- **Persisted and wire formats stay byte-identical:**
  - checkpoint state JSON (keys, values, ordering where it affects digests);
  - artifact bodies and markers;
  - SQLite values;
  - exchange request and response payloads;
  - digests and bindings.
- **Crash-recovery ordering stays fixed:** don't reorder `save()`, `checkpoint()`,
  credential charging, call counting or external writes. Where an extracted helper wraps
  one of these, the call sequence must stay identical.
- **Intentional differences stay** (listed below). Don't merge them into a shared helper
  unless it takes a parameter that keeps the difference.
- **`StrEnum` and constants:** replace a literal only where the value is identical. Values
  must serialise exactly as before.

### Packaging constraints

- **`gateway/exchange/exchange.py` stays a single file.** Its Dockerfile copies only that
  file, so it cannot import from `engine/` or `service/`.
- **`engine/repository.py` keeps its import constraints.** `turns.py`, `mirror.py` and
  `design.py` import it in-process on the native runner, and it also runs as a script
  (`main()` puts its own directory on `sys.path`).
  - Keep it importable both ways.
  - Keep its imports stdlib-only, plus stdlib-only engine leaf modules.
  - Some comments in `repository.py` and `mirror.py` still say "tool container". Treat
    them as historical wording; don't design around a separate container.
- **Python version:** code must run on Python 3.12 (the runner's ubuntu-24.04 system
  `python3`) and 3.13 (service and exchange images). `StrEnum` (3.11+) is fine; don't use
  features newer than 3.12.
- **The bundle is untouched:** don't change anything under `bundle/`. The bundle identity
  must not change.

### Tests

- **Test count must not drop.** This is a check, not proof that coverage is equivalent.
  Change test assertions only to update import paths or symbol locations, never to make a
  refactor pass.
- **When moving a name, check test imports first:** tests import some names from their
  current module (for example `STATE_PREFIX` and `OUTCOMES` from `controller`). Update those
  imports in the same commit, or keep a re-export.
- **Write the guard tests first:** where a brief asks for characterisation or snapshot
  tests, write them before refactoring, run them green against the old code, and keep them.

### Scope and safety

- **Scope discipline:** do only this iteration's items. Report other problems you notice
  in the summary; don't fix them now.
- **Docs:** update `docs/` only where it names a symbol or file that moved.
- **Operational limits:**
  - Don't commit, deploy, or retry any pilot.
  - Don't touch pilot issues #14, #17, #18, #19 or #21.
  - Never set `CONCURRO_TEST_*`.

### Validation (every iteration)

Install the CI test dependencies once, if needed:

```bash
python3 -m pip install -r service/requirements.txt markdown-it-py==3.0.0 ruff==0.17.4 pyright==1.1.408
```

Then run all of the following:

```bash
python3 -m unittest discover -s engine/tests
python3 -m unittest discover -s service/tests
python3 -m unittest discover -s gateway/tests
python3 -m compileall -q engine service gateway/exchange
python3 engine/bundle.py examples/stacks-core
git diff --check
```

From iteration 03 onward, also run `ruff check engine service gateway`. From iteration 17
onward, also run `ruff format --check engine service gateway`. From iteration 18 onward,
run `pyright --project pyrightconfig.json` for the explicitly scoped engine modules.

## Intentional differences: do not merge

| Location | Difference to keep |
| ---- | ---- |
| `service/gateway.py:112` vs `150-152` | The PATCH-ref admission check has no OID check; the other one does. |
| `engine/controller.py:2316-2318` vs `1101-1104` | This PR guard omits the `merged` clause. |
| `engine/controller.py` `invalidate_task` | A refusal consumes the command but does **not** save; the other refusal paths save. |
| `engine/agents.py:352` | Polling-error classification treats 3xx differently from `runner.py`'s session-gone set. |
| `engine/reviews.py` vs `engine/conversation.py` droppable text | One measures characters and the other bytes; keep each measure. |
| `engine/policy.py` review validators | The local validator checks size after the reopen branch; the PR validator checks count before it. |
| Broad `except Exception` blocks (32) | They deliberately sanitise errors or are best-effort; don't narrow them. |
| `engine/readiness.py` keep-latest loops | The ID lookup differs (`run["id"]` vs `.get("id", 0)`). |
| `engine/github.py` `GitHub.pages` vs `service/reconciler.pages` | The engine pages artifacts 10 per page for up to 1,000 pages, and everything else 100 per page for up to 100 pages. The reconciler always uses 100 per page for up to 100 pages. |
| `gateway/exchange/exchange.py` query parsing | The hand-rolled parser rejects `wait=` and percent-encoded values that `parse_qsl` would accept. Changing accepted requests is a behavioural change. |
| Exchange and registry lock and transaction scopes | Each operation's transaction and lock boundaries stay exactly as they are. Don't split one transaction, or merge two. |

## Iterations

Status values:

- **Approved:** authorised to implement.
- **Next:** expected to be approved after the previous one lands.
- **Reassess:** decide after the preceding iteration lands, using what it showed.
- **Candidate:** backlog; reassess before approving.
- **Dropped from this programme:** not planned here. That doesn't mean the area should
  never be revisited.
- **Done:** committed.

| # | Brief | Kind | Risk | Status |
| ---- | ---- | ---- | ---- | ---- |
| 01 | [Audit report-link fix](01-report-link-fix.md) | Bug fix | Very low | Done |
| 02 | [Dead code and naming](02-dead-code-and-naming.md) | Cleanup | Very low | Done |
| 03 | [Lint gate](03-lint-gate.md) | Tooling | Low | Done |
| 06 | [Test fixtures](06-test-fixtures.md) | Tests | Low | Done |
| 07 | [Dashboard extraction](07-dashboard-extraction.md) | Refactor | Low | Done |
| 04 | [Stage and turn-outcome vocabulary](04-shared-vocabulary.md) | Refactor | Low | Done |
| 08a | [Controller authorisation guards](08-guards-and-bookkeeping.md#08a-authorisation-and-recovery-guards) | Refactor | Medium | Done |
| 08b | [Serial and batch call accounting](08-guards-and-bookkeeping.md#08b-serial-and-batch-call-bookkeeping-narrowed) (narrowed) | Refactor | Low–medium | Done |
| 12 | [Commands and revision budget](12-commands-and-revisions.md) (characterisation matrix first) | Refactor | Medium | Done |
| 17 | [One-time format](17-format.md) | Style | Low (noisy) | Done |
| 05 | [URL helpers and protocol markers](05-shared-constants.md) | Refactor | Low | Done |
| 13 | [Controller method splits and publication](13-controller-splits.md) | Refactor | Medium | Done |
| 18 | [Typed records and checked annotations](18-typed-state.md) | Types | Low | Done |
| 10b | [Turn decoding and runner executor](10-execution-path.md#10b-result-decoding-and-the-runner-executor) | Refactor | Medium | Done |
| 10a | [Native turn execution and process cleanup](10-execution-path.md#10a-native-turn-execution-and-process-cleanup) | Refactor | Medium | Approved |
| 10 | [Execution path](10-execution-path.md) (10c, 10d; separate commits) | Refactor | Low–medium | Candidate |
| 09a | [Policy and classification import cycles](09-review-modules.md#09a-refreshed) | Refactor | Low–medium | Done |
| 09b | [Conversation and review helpers](09-review-modules.md#09b-refreshed) | Refactor | Low–medium | Done |
| 16 | [Exchange structure](16-exchange-structure.md) | Refactor | High | Candidate, prepared |
| 14 | [Service gateway](14-service-gateway.md) | Refactor | Medium–high | Candidate, prepared |
| 11 | [GitHub I/O and storage helpers](11-github-and-storage.md) | Refactor | Low–medium | Candidate, prepared |
| 15 | [Service worker, registry and dispatch](15-service-worker-registry.md) | Refactor | Medium | Candidate, prepared |

### Ordering rationale

- **Done:** 01–04, 06, 07, 08a, 08b and 12, plus two behavioural fixes found along the
  way: closed PRs displayed as merged, and the `/concurro start` wedge.
- **Second reassessment** (after 12, 2026-11-08):
  - **17 now:** no other branch is in flight. Formatting before the remaining refactors
    means they are reviewed on formatted code. Its only ordering constraint is never to
    share a commit with structural changes.
  - **05 → 13 → 18:**
    - 05 is small and unblocks 09b.
    - 13 targets `step()` (90 lines, 48 branches), the controller's largest remaining
      knot, and the publication module boundary.
    - 18 then types the extracted components, including `publication`.
  - **Then 10b, 09a, 09b and 10a,** each self-contained.
  - **Prepared group: 16, 14, 11, 15.** Each is worthwhile, and each needs its guard
    tests first:
    - lock maps and concurrency tests (16);
    - an admission decision table (14);
    - pagination bounds (11);
    - schema and migration fixtures (15).

    Higher risk means more preparation, not exclusion.
  - **10c and 10d:** standalone later, or bundled with nearby work.

Numbers are stable identifiers, not the execution order.

## Brief template

Each iteration brief uses these sections:

- **Goal**
- **Scope:** the files the iteration may touch.
- **Changes:** numbered and specific.
- **Must not change:** invariants specific to this iteration, on top of the ground rules.
- **Tests first:** guard tests to add before refactoring.
- **Done when:** acceptance criteria.
- **Commit message**
