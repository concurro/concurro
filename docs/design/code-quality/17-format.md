# 17: One-time format

Part of the [code-quality programme](README.md). **This commit contains formatting only.**

## Goal

Apply one consistent format to the whole codebase and enforce it in CI. Today:

- quoting is mixed, sometimes within adjacent lines;
- 186 lines exceed 120 characters;
- some statements are packed onto one line.

## Scope

- Every Python file in `engine/`, `service/` and `gateway/` (including tests)
- `ruff.toml`
- `.github/workflows/ci.yml`
- New `.git-blame-ignore-revs`
- `README.md`

## Changes

1. **Configure the formatter.** In `ruff.toml`:
   - Set `line-length = 120` at the **top level**, not under `[format]`. Both the linter
     and the formatter read it from there.
   - Keep the default double-quote style; add a `[format]` section only if a non-default
     format option is needed.
   - If the result reads clearly worse for the codebase's long policy expressions, use a
     different line length, and explain why in the summary.
   - Make sure the formatted file set is exactly `engine/`, `service/` and `gateway/`:
     use `include`/`extend-exclude` so virtual environments and other local Python files
     are never touched.
2. **Run `ruff format engine service gateway` once.** Don't hand-edit formatting.
3. **Gate it in CI:** add `ruff format --check engine service gateway`. Add the same
   command to the developer commands in `README.md`.
4. **Hide the commit from blame.** Add `.git-blame-ignore-revs` with a placeholder comment
   explaining that the writer adds the format commit's SHA after committing. Document the
   one-time `git config blame.ignoreRevsFile .git-blame-ignore-revs` command in
   `README.md`.
5. **Strings must not change.** Formatting must not change any string value. Implicit
   string concatenations that the formatter rejoins must produce the same text. If
   `ruff format` reports or makes any change to string content, stop and report it.

## Must not change

- **The AST, apart from formatting.** Before formatting, record a digest of each file's
  AST without line and column attributes. After formatting, compare. Docstring whitespace
  normalisation, if any, must be reported.

  The scan covers only the formatted project directories:

  ```bash
  python3 - > /tmp/ast-before.txt <<'EOF'
  import ast, hashlib, pathlib
  for root in ("engine", "service", "gateway"):
      for p in sorted(pathlib.Path(root).rglob("*.py")):
          if "__pycache__" in p.parts:
              continue
          tree = ast.dump(ast.parse(p.read_text()), include_attributes=False)
          print(p, hashlib.sha256(tree.encode()).hexdigest())
  EOF
  ```

  Run `ruff format engine service gateway`, then repeat the script into
  `/tmp/ast-after.txt`, and `diff /tmp/ast-before.txt /tmp/ast-after.txt`. The diff must
  be empty, or limited to reported docstring-only changes. The file list must match
  `ruff format --check engine service gateway`'s file set.
- **The bundle identity.** `bundle/` contains no Python, but confirm it.

## Tests first

- None are needed. The AST equivalence check above is the guard. Include the command
  and its result in the summary.

## Done when

- `ruff format --check engine service gateway` and `ruff check engine service gateway` are clean.
- AST equivalence holds for every file.
- All suites and validation commands are green with the same test count.

## Commit message

```text
style: apply ruff format across the codebase
```
