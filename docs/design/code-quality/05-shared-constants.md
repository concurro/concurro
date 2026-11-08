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

## Extraction audit

The leaf module is `engine/protocol.py` and has no imports. Existing exports remain:
`conversation.MARKER`, `actions.COMMENT_URL`, `actions.BLOCKED_HEADER`, `agents.SCOPE`,
`controller.COMMIT`, `mirror.SHA`, `repository.SHA` and `policy.IMAGE`.

| File | Replaced sites | Shared definition |
| ---- | ---- | ---- |
| `actions.py` | `COMMENT_URL`; `BLOCKED_HEADER`; `answers` reply link | Template re-export; event marker and digest pattern; `comment_url` |
| `agents.py` | `SCOPE` | `DIGEST_PATTERN` |
| `checkpoints.py` | `Checkpoints.audit_body` report-data links; `Checkpoints.links` root; `Checkpoints.load` pointer validation | `artifact_url`; `DIGEST_PATTERN` |
| `commands.py` | `parse` approval ID; `CommandHandler.extend_plan_revisions` command link | `DIGEST_PATTERN`; `comment_url` |
| `controller.py` | `COMMIT`; `Controller.transition` plan link; `Controller.record_pr_review` and `Controller.pr_body` issue links | `SHA_PATTERN`; `artifact_url`; `issue_url` |
| `conversation.py` | `MARKER`; `publish` archive root | Marker re-export; `artifact_url` |
| `dashboard.py` | `Dashboard.artifact_url`; `Dashboard.action_line` latest-notice and open-request links; `Dashboard.previous_runs` invalidation link | `artifact_url`; `comment_url` |
| `design_audit.py` | `Snapshot.repo` and `measure` report links; `archived_report` root and commitment capture | `artifact_url`; `DIGEST_PATTERN` |
| `feedback.py` | `materialize` commitment validation | `DIGEST_PATTERN` |
| `implementation_bundle.py` | `matches` approved-skill validation | `DIGEST_PATTERN` |
| `mirror.py` | `SHA`; `Mirror.__init__` default clone and source URLs | `SHA_PATTERN`; `clone_url` |
| `policy.py` | `IMAGE` digest suffix | `DIGEST_PATTERN` |
| `report_store.py` | `Store.put` archive-marker check and artifact root; `Store.get` commitment validation; `Store.load` archive-marker check | `EVENT_MARKER`; `artifact_url`; `DIGEST_PATTERN` |
| `repository.py` | `SHA` | `SHA_PATTERN` |
| `runner.py` | `checkout` fetch URL | `clone_url` |
| `snapshot.py` | `inspection` reference validation | `SHA_PATTERN` |

Matching methods and flags are unchanged. `BLOCKED_HEADER` concatenates the fixed marker
and digest pattern directly, retaining its exact compiled pattern; `re.escape` would
change that pattern text. `repository.py` moves its script-only search-path bootstrap
before the new leaf import so `python -I` still works. Importing it in-process still
leaves the caller's search path unchanged; both paths have regression coverage.

Left inline intentionally:

- PR URLs in `actions.handoff` and `Controller.pr_url`, branch URLs in
  `Controller.reserve_development_branch`, and review-thread URLs in `reviews.thread_url`: different URL kinds
  outside this iteration's helpers.
- REST endpoint paths and report-link parsing: these are API paths and parsers, not public
  URL construction. Multipart links continue appending numbers to the shared artifact root.
- UUID, 32-character IDs and the variable 40–64-character Git-history pattern: distinct
  formats, not the fixed-width digest or SHA patterns.
- The exchange's runner-token and commit patterns: retained verbatim for its single-file
  image, with comments naming the mirrored engine constants. Its AST is unchanged.

No magic numbers or requested extraction items were skipped. Literal tests were written
before the leaf module: four existing-contract tests passed, and the six new-helper tests
failed only because the module did not yet exist.

## Done when

- No hand-built artifact, comment or issue URL remains in the engine outside the helpers.
- The event marker, digest pattern and SHA pattern each have one definition.
- All suites and validation commands are green.

## Commit message

```text
refactor(engine): share GitHub URL helpers and protocol markers
```
