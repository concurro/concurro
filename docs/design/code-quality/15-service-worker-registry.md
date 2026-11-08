# 15: Service worker, registry and dispatch

Part of the [code-quality programme](README.md).

## Goal

Separate the service worker's helpers from `Worker`, break up the registry's monolithic
`open()`, define service statuses once, and replace hand-rolled command dispatch.

## Scope

- `service/worker.py`, `service/registry.py`, `service/cutover.py`
- `service/__main__.py`, `service/control.py`, `service/ingress.py`
- `service/failures.py`, `service/identity.py`
- New `service/github_reads.py`, `service/target_loader.py` and
  `service/runner_diagnostics.py` (or similar names)
- Tests

## Changes

### 1. `worker.py` (about 62–188)

- **Move the free functions out:**
  - GitHub reads (`default_branch`, `head`, `content`, `runner_claims`) go to
    `github_reads.py`. `cutover.py:13` currently imports `content`/`head` from `worker`;
    point it at the new module.
  - Target loading goes to `target_loader.py`.
  - `runner_termination` goes to `runner_diagnostics.py`.
- **Small helpers inside `Worker`:**
  - `_operator()`: the `Operator(...private.read(...).decode().strip())` construction,
    duplicated at 261 and 307.
  - `_acknowledge(key, snapshot, controller)`: `{**snapshot, "_stage": ...}` at 407 and 442.
  - `_facade(repo, key, generation)`: the three `GatewayGitHub` constructions (384–388).
- **Flatten `failed()`** (412–451) into a short dispatcher plus one handler per effect
  (`suspend`, `health`). The handlers keep the exact order of
  `suspend` → `sync_checkpoint_health` → `classify`.

### 2. `registry.py`

- **Split `open()`** (159–218) into `_lock_state_dir()`, `_initialize(db)` and a
  `MIGRATIONS = {"2": _v2_to_v3}` table.
- **Schema:** the migration re-declares the `pulls` DDL (198–200 duplicates 48–50). Use a
  `PULLS` constant in both places, and keep the DDL text identical.
- **Row mapping:** `_mutation_record(row)` for the duplicated row-to-`MutationRecord`
  mapping (359, 365). Also make the duplicated bindings SELECT (461, 468) one query string.
- **Key filter:** add a `KEY_WHERE` constant for `WHERE repository = ? AND issue = ?`
  (about 25 copies). Do this only where the SQL stays textually equivalent.
- **Mutation statuses:** a `MutationStatus(StrEnum)` (`in-flight`, `done`, `failed`,
  `unknown`). Replace the literals at 249, 341, 350, 370, 374, 387, 391 and 521.
  `STATUSES[1:]` (347) must keep the same members; name it `SETTLED`. `StrEnum` values bind
  to SQLite as the same text.
- **Exception handling:** at `registry.py:209-215` and `identity.py:150-155`, replace
  `except BaseException` followed by `isinstance` branching with two `except` clauses that
  behave the same.

### 3. Vocabularies

- **Outcome kinds:** they are compared as raw strings in `registry.py:329-332, 436`,
  `worker.py:440-447` and `cutover.py:123`. Use the engine's outcome definition
  (`controller.Outcome` / `vocab`), or one shared constant set.
- **Failure effects:** `failures.py:53` (`health` / `suspend`) becomes a small `StrEnum`.

### 4. Command dispatch

- **`__main__.command`** (129–140): replace the if-chain that mirrors the parser with
  `subparser.set_defaults(handler=...)`.
- **`Control.run`** (37–52): replace the if-chain with a `{command: handler}` table. The
  wire protocol and responses stay identical.

### 5. HTTP handlers and logging

- **HTTP handlers:** ingress (163) and control (102) inject dependencies with
  `type("XHandler", (Handler,), {...})`. Read them from `self.server.<dependency>` instead,
  by setting attributes on the server instance.
- **Logging:** leave the per-module `getLogger("concurro.service")` calls as they are.
  That is idiomatic, and a shared logger module would add indirection without benefit.
- **`targets.verify`** (62) imports `policy` inside the function. Move it to the top if no
  cycle forces the local import.

## Must not change

- **Database schema and migration result:** the same tables, the same DDL text where it is
  stored or compared, and the same `user_version` handling.
- **Control-socket and CLI behaviour,** arguments and output.
- **HTTP responses** from ingress and control.
- **Failure classification order and effects.**

## Tests first

- **Migration fixture:** a v2 database fixture. Migrate it, then pin the resulting schema
  (`sqlite_master` SQL) and rows. Also pin the schema of a fresh v3 database.
- **CLI and control:** a CLI-parse table and a control-command table, pinning the handler
  each one invokes and its output.
- **`failed()`:** a test that pins the sequence of calls for each effect.

## Done when

- `worker.py` holds `Worker` and `DeferredExecutor`, plus their private helpers.
- `registry.open()` is split, and the mutation statuses are a `StrEnum`.
- Command dispatch is table-driven.
- All suites and validation commands are green.

## Commit message

```text
refactor(service): split worker helpers, registry setup and command dispatch
```
