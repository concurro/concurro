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

## Extraction boundaries

- `step()` keeps the orchestration order. `_reconcile_external` handles PR completion
  before issue closure; `_migrate_legacy_block`, `_needs_flush` and `_validate_authority`
  name the remaining seams. The recorded-charter/path-policy guard stays before migration
  and commands. The later PR authority guard still checks only open state and head.
- `Publication` owns branch reservation, object creation, PR lookup/creation and PR text.
  It receives the fork and upstream clients, config, task state and optional plan artifact.
  Save and tree-read callbacks are explicit; it never receives the controller.
  `_advance_branch` and `_find_or_open_pr` preserve the publication recovery sequence.
- `RepositoryReader` owns pinned reads and the invocation-local tree cache. Controller
  methods delegate, and `controller.trees` remains an alias for callers that clear the cache.
- `apply_quality_report` delegates stage-specific validation to `_accept_design_report`
  and `_accept_intake_report`, then records `_quality_history` at the original boundary.
  The common application/transition/save tail remains together.
- `gates` extracts `_observe_ci`. Ready-marking stays inline: intent save, final PR check,
  possible write, confirmation and handoff belong to one recovery sequence. A `_mark_ready`
  helper would need to carry both that early return and the surrounding observation state.
- `ask` stays whole: its two-attempt recovery, eligibility changes and timing cleanup are
  clearest together. `invalidate_task` stays in the command handler from iteration 12.
- `validate_config` shares only the three type-strict integer checks through `_bounded_int`.
  Older numeric checks still accept bools where they did before. `checks.relative_path`
  exposes the existing private predicate without changing it.

The 13 pre-extraction guards in `test_controller_splits.py` pin external-event precedence,
legacy migration, the distinct PR authority check, integer boundaries and four publication
traces: normal, blob upload, lost tree response and lost PR response. The traces include
save boundaries through the first PR review; existing dashboard and command goldens remain
unchanged.
