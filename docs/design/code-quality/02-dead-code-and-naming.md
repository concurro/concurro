# 02: Dead code and naming

Part of the [code-quality programme](README.md).

## Goal

Remove code that has no effect: unused imports, locals, parameters, redundant checks. Also
rename locals that shadow imported names. Behaviour does not change.

## Scope

- `engine/*.py`
- `service/*.py`
- Callers and tests that are affected

## Changes

### Unused imports

1. Remove the unused module-level imports below, found by an `ast` scan at `f1225e6`.
   Before removing each one, grep `engine/tests`, `service/tests` and `gateway/tests` for
   `from <module> import <name>` and `patch(` targets. Keep any name tests depend on, or
   update those tests to import it from where it is defined.

| Module | Unused names |
| ---- | ---- |
| `engine/agents.py` | `json`, `tempfile`, `Operator`, `turns` |
| `engine/bundle.py` | `re` |
| `engine/controller.py` | `os`, `Path` (keep `PurePosixPath`), `time`, `sync_checkpoint_health`, `GitHub`, `NotFound`, `OutsideScope`, `validate_review` |
| `engine/resume_proof.py` | `os` |
| `service/worker.py` | `json`, `Mirror` |

**Known exceptions:** tests import `STATE_PREFIX` and `OUTCOMES` from `controller`.
Either keep those imports, or move the tests to import from the defining module
(`checkpoints` for `STATE_PREFIX`). Then remove `OUTCOMES` only if no test needs it.

### Dead code in `engine/controller.py`

2. `refresh_autonomous` (around 1236–1247):
   - `base = state["base"]` makes `base != state["base"]` always false. Drop that clause.
     The condition becomes `if state.get("pending"):`, giving the same truth table.
   - `update(..., base=base)` writes back the same value. Drop that argument.
3. `check_human_approval` (around 2331–2333): the `state.get("approval") and` guard comes
   after `state["approval"]` has already been indexed, so it is always true when reached.
   Remove it.
4. `apply_quality_report` (around 1444): remove the unused local `decision`, after
   confirming it is unused.

### Unused parameters

5. Remove each parameter below and update every caller. Confirm by reading each function
   that the parameter really is unused.
   - `policy.path_allowed(path, config, execution)`: drop `execution`. It is threaded
     through about 10 call sites in `policy`, `readiness`, `candidates` and `controller`.
     Check whether `validate_plan(plan, config, execution)` and others still need
     `execution` for other reasons, and keep it where it is used.
   - `policy.validate_qc(..., criteria_count=32, objection_count=0)`: drop both.
   - `design.report_limit(count, objections=0)`: make it `report_limit()`.
   - `turns.command(root)`: drop `root`.
   - `agents.RunnerExecutor.start(..., identifier)`: drop `identifier`.
   - `review_output_budget(context=None)`: drop `context`. If `pr_review_output_budget` is
     a trivial alias, replace its callers with the base function and delete the alias.

### Shadowed names

6. Rename locals that shadow imported names. Each rename is local to its function.
   - The local `text` that shadows `markup.text`:
     - `controller.py` (`commands`, about line 1659): rename to `body`;
     - `reviews.py` (421, 430);
     - `conversation.py` (54, 531, 554).
   - The local `feedback` that shadows the `feedback` module: `controller.py` (around
     1533, 1869). Rename to `writer_feedback` or similar.
   - The local `where` in `reviews.py:298`, which shadows the module function `where`.
   - The local `items` in `conversation.py:451`.
   - The alias `import reviews as findings` in `controller.py`, which reads like
     `state["findings"]`. Rename the alias to `reviews` and update its uses. Check first
     that no local named `reviews` would collide; if one would, choose `review_rules` and
     say so.

### Stray blank lines

7. Remove the double blank lines inside the class body (`controller.py` around 749 and
   1843).

## Must not change

- Any function's behaviour or return value.
- Message text.
- Public names that tests import, unless the tests are updated in the same commit.

## Tests first

None are needed. The existing suites cover these paths. Run them before and after, and
report both counts.

## Done when

- None of the listed imports, parameters, locals or checks remain, except documented exceptions.
- All suites and validation commands are green with the same test count.
- The summary lists any item that was kept, and why.

## Commit message

```text
refactor: remove unused code and rename shadowing locals
```
