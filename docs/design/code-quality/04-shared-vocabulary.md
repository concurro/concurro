# 04: Shared vocabulary

Part of the [code-quality programme](README.md).

## Goal

Give the engine's repeated bare strings one definition each, as `StrEnum` types and
named sets. Covered: controller stages, agent stages, turn outcomes, termination reasons,
exchange statuses, dashboard steps and finding statuses.

Values must serialise and compare exactly as today. `StrEnum` members are `str` instances
equal to their values, so JSON, digests and `in` checks against existing strings keep
working.

## Scope

- New leaf module `engine/vocab.py`. It imports only the stdlib, so every module can import
  it, including `repository.py` (see the README's import constraints).
- Engine modules that define or compare these values.
- **Not** `service/` mutation statuses or exchange statuses; those are iterations 14–16.
- **Not** `gateway/exchange/exchange.py`, which ships as one file.

## Changes

### 1. Define the vocabulary in `engine/vocab.py`

| Type | Members (values) | Current scattered definitions |
| ---- | ---- | ---- |
| `Stage` | `intake`, `clarify`, `plan`, `plan_qc`, `planned`, `implement`, `publish`, `review`, `gates`, `merging`, `merged`, `observed_merged`, `closed`, `blocked`, `stopped`, `rejected`, `escalated` | `github.STAGES`, `actions.RESUMED_STAGES`, `dashboard.STAGE_STEP`, about 30 inline tuples |
| `AgentStage` | `intake`, `plan`, `plan_qc`, `develop`, `review` | `bundle.STAGES`, `snapshot.py:15`, `policy.runner_bound`, `policy.turn_label` |
| `Outcome` (turn result) | `ok`, `failed`, `uncertain`, `lost`, `timed_out`, `terminated`, `admission_unknown`, `queue_expired` | Literals in `turns.py`, `agents.py`, `runner.py` |
| `TerminationReason` | `turn_timeout`, `runner_stopped`, `process_signal` | `turns.py:275`, `agents.py:149-150` |
| `ExchangeStatus` | `queued`, `running`, `done`, `lost`, `cancelled` | `review_timing.STATUSES`, `agents.py` literals |
| `FindingStatus`, plus a `CLOSED` frozenset | The values used in `reviews.py` / `readiness.py`; `CLOSED = {"fixed", "obsolete"}` | `reviews.py:24/85/433/455`, `readiness.py:29/31` |
| `Step` (dashboard step labels) | The six labels in `dashboard.STEPS` | `dashboard.STEPS`, `conversation.index_entry`, `dashboard_runs`, `design_audit` |

`Stage` and `AgentStage` both contain `review` and `intake`. They are deliberately
separate: controller stage `implement` corresponds to agent stage `develop`. Keep two
types; don't merge them.

### 2. Named sets for repeated tuples

- **Stage sets:**
  - `TERMINAL_STAGES = {merged, observed_merged, closed}` (controller 504/2227/2266);
  - `SUSPENDED = {blocked, stopped}` (11 copies);
  - `JUDGED = {rejected, escalated}` (8 copies);
  - `RETRYABLE = SUSPENDED | JUDGED` (1711, 2229);
  - `RESUMED_STAGES` (moved from `actions`);
  - `ALL_STAGES` (replaces `github.STAGES`).
- **Runner-bound agent stages:** `RUNNER_BOUND = {develop, review}`. `policy.runner_bound()`
  becomes a membership test; keep the function, since callers use it.
- **Outcome sets:** name each outcome set that appears more than once, for example the set
  that leads to `cancel_quietly` (`agents.py:405`).

### 3. Replace literals and tuples with the named members and sets

- Do this only where the replacement is exactly equivalent.
- Where the order of a tuple matters (iteration, `STATUSES[1:]`-style slicing, or anything
  that feeds a digest or display), keep a tuple with the same order.
- Re-export the old names (`github.STAGES`, `actions.RESUMED_STAGES`, `bundle.STAGES`,
  `review_timing.STATUSES`) as aliases of the new definitions, if other code or tests
  import them.

### 4. One table for paused labels

`controller.py` has three near-identical label tables for paused stages, at about 371, 524
and 592. Make them one module-level table keyed by `Stage`. Where an entry differs, keep
the difference explicitly and name it.

## Must not change

- Any persisted value, comparison result, iteration order used for output, or digest.
- Membership semantics. Check each replaced tuple: a set that grows or shrinks by even one
  member is a behaviour change.
- `json.dumps` output: `StrEnum` members serialise as plain strings. Add a test that proves it.

## Tests first

- **`test_vocab.py`:**
  - every enum's values exactly equal the old literal sets (pin them as literal
    tuples in the test);
  - `json.dumps({"stage": Stage.PLAN}) == '{"stage": "plan"}'`;
  - `Stage("plan") == "plan"`.
- **Old sets unchanged:** a test that the old public sets (`github.STAGES`,
  `actions.RESUMED_STAGES`, `bundle.STAGES`, `review_timing.STATUSES`) still equal their
  `f1225e6` contents.

## Done when

- Each listed vocabulary has one definition, and the old names are aliases of it.
- No inline copy of the named stage sets remains in the engine. Find them with
  `grep -nE '"blocked", "stopped"|"rejected", "escalated"|"merged", "observed_merged"' engine/*.py`.
- All suites and validation commands are green, and the test count grows only by the new tests.

## Commit message

```text
refactor(engine): define stages, outcomes and statuses once
```
