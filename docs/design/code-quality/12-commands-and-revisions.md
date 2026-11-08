# 12: Commands and revision budget

Part of the [code-quality programme](README.md).

## Goal

Move writer-command handling and the plan-revision budget out of `Controller`.

- `commands()` (about 1654–1756) is a 103-line if/elif with 54 branches. Every branch
  repeats `self.state and text == ...`.
- The revision logic is spread across `revise_plan`, `plan_revision_limit`,
  `waiting_revision`, `refuse_revision_command` and `extend_plan_revisions`.
- The "restart intake" sequence is written three times.

## Scope

- New `engine/commands.py` and `engine/revisions.py`
- `engine/controller.py`
- Tests

## Changes

### 1. Command parsing

`commands.parse(body) -> Command | None` owns the regex, which is currently inline in
`commands()`. `Command` is a small frozen dataclass: `kind` (a `StrEnum`: `start`,
`approve`, `stop`, `retry`, `extend`, `invalidate`) and `argument` (the plan ID or the
revision count).

### 2. Command dispatch

A `CommandHandler`, given the controller, holds one method per kind (`start`, `approve`,
`retry`, `extend`, `invalidate`, `stop`) and a dispatch table.

- **The loop stays the same.** `Controller.commands(comments)` keeps its signature and
  return value. It still iterates comments, applies the `command_id` and permission
  filters in the same order, and dispatches.
- **Each handler returns the loop action:** `CONTINUE`, `RETURN_TRUE`, or `FALL_THROUGH`
  where today's code reaches the end of the branch. This keeps today's `continue` /
  `return True` / fall-through flow exactly.
- **Handlers that move into `commands.py`:**
  - `extend_plan_revisions`, `refuse_revision_command`;
  - `retry_reviewed`, `retry_implementation_bundle`;
  - `invalidate_task`, `check_human_approval`.

  Keep delegating methods on the controller for any of these that tests or other modules
  call by name.
- **Name the output-repair predicate:** the 4-clause expression at about 1715–1719 becomes
  `output_repair_pending(state)`.

### 3. Revision budget

`revisions.RevisionBudget(state, config)` owns `limit()`, `waiting()` and
`revise(stage, reason, invalidate=..., save_exhausted=...)`, today's `revise_plan`.

- `revise()` needs `invalidate_design` and `save`. Pass them in as callables; don't hand
  over the controller.
- `Controller.revise_plan` stays as a thin delegate. It has many callers.

### 4. Optional intake-restart helper

The "restart intake" sequence appears three times, at about 1683–1693, 1724–1733 and
1244–1250. The copies differ:

- only the `/concurro start` path refetches the base;
- the sets of keys popped differ;
- the refusal handling differs.

Extract a shared helper only for the part that is genuinely identical: refreshing the
requirements and `issue_digest` after a successful `revise_plan("intake")`. If a helper
would need more than about two parameters to cover the variants, keep the three sequences
inline. Either way, pin each call site's exact behaviour in tests.

## Must not change

- **Command semantics, for every command and state combination:**
  - which commands are consumed (`command_id`), and when;
  - whether and when `save()` is called;
  - `Blocked` raised versus a notice posted;
  - return values (`True` / `None`);
  - the order of comment processing.
- **Revision accounting:** `plan_revisions`, `plan_revision_extra`, `revision_wait`,
  `retry_stage` and grant receipts.
- **Notice and `Blocked` text.**

## Tests first

**Characterisation matrix.** Using the iteration 06 fixtures, drive the controller to each
relevant stage. Stages:

- `clarify`, `intake`, `plan`, `plan_qc`, `planned` (manual), `implement`, `review`, `gates`;
- `blocked` (with retry stages `implement` / `plan_qc` / output repair);
- `stopped`, `rejected`;
- `escalated` with and without a waiting revision.

Apply each command:

- `start`, `approve <id>` (matching and not), `stop`, `retry`;
- `retry --plan-revisions 0|1|4`, `invalidate`;
- for each of these, a non-writer variant.

For each cell, record:

- the resulting `stage`, `command_id`, `retry_stage` and `revision_wait`;
- the number of `save()` calls;
- whether `Blocked` was raised (and its message), and any new notices;
- the return value.

Generate this matrix against the old code and commit it as a pinned fixture. It must pass
unchanged after the refactor.

## Done when

- `Controller.commands` is a short loop plus dispatch.
- Handlers and the revision budget live in their modules.
- The characterisation matrix and `test_revision_extension.py` pass unchanged.
- All suites and validation commands are green.

## Commit message

```text
refactor(engine): extract writer commands and the plan-revision budget
```
