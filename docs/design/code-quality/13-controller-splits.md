# 13: Controller method splits and publication

Part of the [code-quality programme](README.md).

## Goal

Split the remaining long controller methods into named steps, and move PR publication and
repository reading into their own modules. After iterations 07, 08 and 12, this leaves
`Controller` as the stage state machine.

## Scope

- `engine/controller.py`
- New `engine/publication.py` and `engine/repository_reader.py` (or similar names)
- Tests

## Changes

### 1. `step` (about 2237–2326, 90 lines, 48 branches)

Extract:

- `_reconcile_external()`: PR merged/closed and issue closed (2250–2267);
- `_migrate_legacy_block()`: 2273–2279;
- `_needs_flush()`: the 5-clause outbox predicate (2281–2284);
- `_validate_authority(comments)`: config digest, issue digest, bundle, approval and PR
  head checks (2300–2318). Keep the no-`merged` PR check here as it is.

Also make the stage dispatch dict (2319) a class-level mapping of method names.

### 2. Other long methods

- `apply_quality_report` (1391–1461): extract `_accept_design_report`,
  `_accept_intake_report` and a `qc_history` record builder.
- `publish` (2019–2075): extract `_advance_branch` and `_find_or_open_pr`.
- `gates` (2145–2199): extract `_observe_ci` and `_mark_ready`.
- `invalidate_task`: already moved in iteration 12; tidy it there only if it is still long.
- `validate_config` (82–137): add `_bounded_int(key, low, high, message)` for the
  int-in-range checks (113, 115, 132). It **must** keep `type(x) is int`, which rejects
  bools. Stop calling the private `checks._relative` from another module: make it public
  in `checks`, or add a public wrapper.

### 3. Publication module

`publication.py` takes:

- `reserve_development_branch`;
- the tree/blob building in `implement` (1990–2007);
- `publish`, `pr_title`, `pr_body`, `pr_url`.

Also extract `_branch_refs(branch)` for the matching-refs filter duplicated at 1952 and
2028–2029. Give the module the narrow dependencies it needs (the pulls client, config and
state), not the whole controller.

### 4. Repository reader

Move `source`, `tree`, `file_manifest` and `files` (735–779), together with their `trees`
cache, into a `RepositoryReader` class.

## Must not change

- **Stage transitions** and which external events win when several are observed.
- **GitHub write sequences** for branch reservation, tree/commit creation, PR creation or
  lookup, and ready-marking.
- **`validate_config` acceptance and rejection,** and its messages.
- **`save()` ordering** around publication. `pending` / outbox recovery depends on it.

## Tests first

- **Request recording:** a recording-GitHub test for the full publication path (reserve
  branch → implement → publish → first review). Pin the exact request sequence and bodies,
  including a retry after a crash between tree creation and the PR request.
- **`validate_config`:** a table of valid and invalid configs, including bools in integer
  fields. Pin each result and message.
- The dashboard snapshots from iteration 07 and the command matrix from iteration 12 must
  still pass.

## Done when

- Each listed method is split where the named steps read more clearly than the inline
  sequence. There is no line-count target. A crash-recovery sequence that reads clearly
  from top to bottom stays inline; name any method you deliberately left whole, and say why.
- Publication and repository reading live in their modules.
- Pinned request sequences are identical before and after.
- All suites and validation commands are green.

## Commit message

```text
refactor(engine): split long controller methods and extract publication
```
