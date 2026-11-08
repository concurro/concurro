# 03: Lint gate

Part of the [code-quality programme](README.md).

## Goal

Add a conservative `ruff check` gate to CI so that unused imports, unused variables and
real bug patterns can't come back. **No formatting yet**; that is iteration 17.

## Scope

- New `ruff.toml` at the repository root (the repository has no `pyproject.toml`)
- `.github/workflows/ci.yml`
- Python files: only the fixes needed to reach zero violations
- `README.md`: the developer test commands

## Changes

1. **Pin ruff.** Use an exact version that is at least two weeks old. Install it in CI
   (`python3 -m pip install ruff==<version>`), and add `ruff check engine service gateway` as a CI step next to
   the unit-test steps.
2. **Configuration.** In `ruff.toml`:
   - `target-version = "py312"`.
   - Start with `select = ["F", "E9", "PLE"]`: pyflakes, syntax errors, pylint errors.
   - Try `B` (bugbear) and `SIM`. Enable a rule from them only if its existing hits can be
     fixed with no behaviour change, or if it has none.
   - List the rules you deliberately deferred in a comment at the top of `ruff.toml`,
     with one line on why for each.
   - Don't enable line-length (`E501`) or quote rules.
3. **Fix the remaining violations.** Any are left over from iteration 02. Prefer real
   fixes to `# noqa`. A `noqa` is acceptable only where the code is intentional, and then
   it carries a short reason:
   - a re-export kept for tests;
   - a deliberate broad `except` (`BLE`, `B902`);
   - a lazy import in `repository.py` (`E402`).
4. **Don't change the broad excepts.** Do not narrow any of the 32 deliberate `except
   Exception` blocks. If an enabled rule flags them, either leave that rule out or use a
   per-file ignore.
5. **Document it.** Add `ruff check engine service gateway` to the developer commands in `README.md`, next to the
   unittest commands.

## Must not change

- Runtime behaviour. Lint fixes in this iteration must be mechanical: deleting unused
  names, or rewriting a clearly equivalent construct. Report every non-trivial rewrite.
- The bundle and its identity.

## Tests first

None are needed.

## Done when

- `ruff check engine service gateway` reports zero violations locally, using the pinned version.
- CI runs it.
- All suites and validation commands are green with the same test count.
- `ruff.toml` lists the enabled and deferred rules, with reasons.

## Commit message

```text
ci: add a ruff lint gate
```
