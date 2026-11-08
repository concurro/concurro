# 05: Shared constants and URL helpers

Part of the [code-quality programme](README.md).

## Goal

Replace copied regexes, markers, magic numbers and hand-built GitHub URLs with single
definitions. Every value must be identical to the literal it replaces.

## Scope

- Engine and service modules
- **Not** `gateway/exchange/exchange.py`. It keeps its own copies because it ships as one
  file. Add a comment there naming the engine constant it mirrors.

## Changes

### 1. URL helpers in `engine/github.py`

These build exactly today's strings, as plain functions:

- `artifact_url(repository, issue, number=None)`: with `number=None` it returns the root
  ending in `/artifacts/`.
- `comment_url(repository, issue, comment_id)`. `actions.COMMENT_URL` already exists;
  reuse or replace it.
- `issue_url(repository, issue)`.
- `clone_url(repository)`.

Replace the hand-built copies, about 20 sites:

- `controller.py`: 320, 498, 574, 578, 696, 1120, 1267, 2100;
- `conversation.py:438`;
- `design_audit.py`: 59, 78, 124;
- `report_store.py:249`;
- `checkpoints.py`: 174, 388;
- `mirror.py`: 42, 45;
- `runner.py:142`;
- `actions.py:151`.

`repository.py` must keep its import constraints (see the README). If `github.py`
pulls in dependencies they can't use, put the helpers in a stdlib-only module
(`engine/urls.py`) instead.

### 2. Regex and marker constants

Put these in a stdlib-only leaf module. `vocab.py` from iteration 04 is fine, or a new
`engine/patterns.py`:

- `DIGEST`: the 64-hex pattern (`agents.py:33`, `checkpoints.py:260`, `feedback.py:64`,
  `report_store.py:293`).
- `SHA`: the 40-hex pattern (`mirror.py:15`, `repository.py:29`, `snapshot.py:13`).
- `SESSION_ID`: the run-session UUID (`gateway.py:49`, `worker.py:154`,
  `review_timing.py:16`).
- `REPOSITORY_NAME`: `mirror.py:38, 43`.
- `EVENT_MARKER`: `'<!-- concurro:event:v1:'`, used in `report_store.py` three times and
  in `actions.BLOCKED_HEADER`. `conversation.MARKER` already holds it; make that the single
  definition and import it.

Keep anchoring (`fullmatch` vs `match`) and flags exactly as each call site has them.
Share only the pattern text.

### 3. Run-branch and workflow constants

- `RUN_BRANCH_PREFIX = "concurro/runs/"`: 9 sites across `service/gateway.py` (171–180,
  435), `service/worker.py` (163, 324–325) and the engine. Exchange keeps its own copy.
- `EXECUTOR_WORKFLOW = "concurro-executor.yml"`: `gateway.py:176, 443`.
- `ENROLLMENT_LABEL = "concurro"`: `service/ingress.py:10`, `service/reconciler.py:10`,
  and the literals at `controller.py:970, 974`.
- `PROVIDERS`: `{"claude", "codex"}` in `agents.PROVIDERS`, `service/settings.py:29` and
  `service/worker.py:123`.
  - `agents.PROVIDERS` is a tuple. Keep its order where it is iterated.
  - Service code may import from the engine (`service/__init__.py` puts `engine/` on the
    path). The engine must never import from the service.

### 4. Name repeated limits that mean the same thing

Name a number only when it is a real limit or protocol value, and the same concept recurs.
A name reused for two values that only happen to be equal couples unrelated code.
Pagination bounds are handled in iteration 11, where each caller keeps its own. Candidates:

- `64_000` (`worker.py:38`, `cutover.py:18`), if both are the same limit;
- `86400` (`worker.py:182`);
- `14_400` (`control.py:111`);
- `classification.py:78-105`: the repeated `600`/`5`/`200` and three repeated diagnostic
  strings;
- `conversation.py:533-537` (`1000`/`900`/`800`), `385/388`, and `339` (`8`);
- `policy.py:115/263/346` (`6000`/`20000`/`16000`).

Don't name a number used only once with an obvious meaning.

## Must not change

- Any produced URL, regex match result, or marker text.
- Import-time behaviour of `repository.py`, both in-process and as a script.

## Tests first

- **URL helpers:** unit tests that pin each helper's output for a sample repository,
  issue and number, against the literal format used today.
- **Patterns:** a test that each shared pattern constant matches and rejects the same
  examples as before. Include a digest of the wrong length and uppercase hex, if the old
  patterns rejected those.

## Done when

- No hand-built `https://github.com/{...}/issues/` artifact or comment URL remains outside
  the helpers. Exchange is exempt.
- Each listed regex, marker and constant has one definition.
- All suites and validation commands are green.

## Commit message

```text
refactor: share URL helpers, patterns and named constants
```
