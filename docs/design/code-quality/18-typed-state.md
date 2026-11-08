# 18: Typed records and checked annotations

Part of the [code-quality programme](README.md).

## Goal

Add types **incrementally and checked**, starting with the records that the extracted
components exchange. A large unchecked catalogue of the whole checkpoint state would drift
like any other description, so this iteration avoids one.

There are no annotations today. The checkpoint state is an untyped dict with about 60 keys.

## Scope

- **Modules:** the components extracted by earlier iterations that have landed, for
  example:
  - `dashboard.py` (07);
  - `commands.py` and `revisions.py` (12);
  - `publication.py` (13);

  plus the leaf modules they depend on: `vocab.py`, `core.py`, `review_timing.py`,
  `implementation_bundle.py`, `design_eligibility.py`. Include only modules that exist when
  this iteration starts.
- **Type definitions:** a small `engine/records.py`, or definitions placed next to their
  owning module. It holds only the records those modules read or write.
- **A pinned type checker** (`mypy` or `pyright`), run on exactly those modules.
- **CI:** add a type-check step for those modules only.

## Changes

1. **Records, not the whole state.** Define `TypedDict`s only for records the modules in
   scope actually pass around: for example `RevisionWait`, `RevisionGrant`, `Operation`,
   `BatchMember` and `Command`, and the fields of `TaskState` those modules access. Use
   `total=False` where keys are optional.
   - Derive keys from the code that writes them.
   - Mark legacy-only keys as such.
   - Don't add a key nothing in scope reads.
2. **Annotations.** Annotate the public functions of the modules in scope with those
   records and the `vocab` types.
3. **Check the annotations.**
   - Configure the pinned checker to check only the modules in scope, with imports from
     unannotated modules treated as `Any`.
   - Fix real annotation errors in the scoped modules without changing behaviour.
   - Add the check to CI for these modules only.
   - If a finding would require a behaviour change, report it; don't fix it.
4. **No runtime effect.**
   - Don't add `isinstance` checks or behaviour-changing `cast`s.
   - Don't add runtime imports that create cycles; use `if TYPE_CHECKING:` imports where
     needed.
5. **Extend later, not now.** The summary proposes the next modules to bring under the
   checker. Don't annotate them in this iteration.

## Must not change

- **Runtime behaviour, import order and import-time side effects.**
- **`repository.py` imports:** add nothing that it would import.

## Tests first

None are needed. Run all suites; `compileall` and the type check must be clean for the
scoped modules.

## Done when

- The scoped modules are annotated, and the pinned checker passes on them, locally and
  in CI.
- Every record definition is used by at least one annotated module.
- All suites and validation commands are green with the same test count.

## Commit message

```text
refactor(engine): add checked types to the extracted components
```
