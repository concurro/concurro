# 05: URL helpers and protocol markers

Part of the [code-quality programme](README.md).

## Goal

Give hand-built GitHub URLs and repeated protocol markers and patterns one definition
each. This is a small, separate commit from iteration 04: URLs and markers are not stage
vocabulary.

## Scope

- New stdlib-only leaf module `engine/protocol.py` (or a similar name). It imports nothing
  from the engine, so any module can use it without creating an import cycle.
- The engine modules that build these URLs or use these markers.
- **Out of scope:**
  - `gateway/exchange/exchange.py`. It keeps its own copies because it ships as one file.
    Add a comment there naming the engine constant it mirrors.
  - Magic numbers.

## Changes

### 1. URL helpers in the leaf module

Each helper must produce exactly today's string:

- `artifact_url(repository, issue, number=None)`: with no number it returns the root,
  ending in `/artifacts/`.
- `comment_url(repository, issue, comment_id)`. `actions.COMMENT_URL` already exists; keep
  it importable, as an alias or re-export.
- `issue_url(repository, issue)`.
- `clone_url(repository)`, if more than one call site builds it.

Replace the hand-built copies. Find them with
`grep -nE 'https://github.com/\{' engine/*.py`. Sites include:

- `dashboard.artifact_url`;
- `conversation.py` (archive links);
- `design_audit.py`;
- `report_store.py`;
- `checkpoints.py`;
- `controller.py`: comment, issue and artifact links.

### 2. Protocol markers and patterns in the same leaf module

- **`EVENT_MARKER = "<!-- concurro:event:v1:"`.**
  - `conversation.MARKER` and `report_store.py`'s two inline literals use it.
  - `conversation.MARKER` stays importable, so existing imports (`design_audit`, tests)
    keep working.
  - **Don't** make `report_store` import from `conversation`: `conversation` already
    imports `report_store`, so that would create a cycle.
  - `actions.BLOCKED_HEADER` embeds the marker in a regex; build that regex from
    `re.escape(EVENT_MARKER)` only if the compiled pattern is identical.
- **`DIGEST_PATTERN`** (64 lowercase hex) and **`SHA_PATTERN`** (40 hex): replace the
  copies (`agents.py`, `checkpoints.py`, `feedback.py`, `report_store.py`; `mirror.py`,
  `repository.py`, `snapshot.py`). Share only the pattern text, and keep each call site's
  anchoring (`fullmatch` vs `match`) and flags.

### 3. Existing exports

Every name currently imported from another module, by code or tests, stays importable from
where it is today.

## Must not change

- Any produced URL, marker text, or regex match result.
- Import-time behaviour of `repository.py`, in-process and as a script.
- The import graph, apart from the new leaf module.

## Tests first

- **URL helpers:** each helper's output pinned against a literal string, for a sample
  repository, issue and number.
- **Patterns:** each shared pattern matches and rejects the same examples as before,
  including wrong-length and uppercase hex, if the old patterns rejected them.
- **Existing exports:** `conversation.MARKER`, `actions.COMMENT_URL` and any other existing
  export keep their values.

## Done when

- No hand-built artifact, comment or issue URL remains in the engine outside the helpers.
- The event marker, digest pattern and SHA pattern each have one definition.
- All suites and validation commands are green.

## Commit message

```text
refactor(engine): share GitHub URL helpers and protocol markers
```
