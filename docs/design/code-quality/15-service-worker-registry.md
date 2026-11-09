# 15: Service worker and registry (15a), control dispatch and server handlers (15b)

Part of the [code-quality programme](README.md).

Re-checked against `2440541`. Find code by symbol; line numbers are hints only.

This brief covers **two separate commits**, because the concerns are unrelated: task
execution and storage, versus the operator CLI and HTTP wiring. 15a comes first; **15b
is reviewed and approved separately**.

## Ground rules for both

- **Database:** the stored schema text (`sqlite_master`), the migration result, the
  explicit `BEGIN IMMEDIATE`/`COMMIT` transactions in the registry, and every stored value
  stay unchanged.
- **Service behaviour:** tick outcomes, failure classification and effects, health and
  `Blocked` notices, acknowledgement and deferral of snapshots, and ownership handling
  stay unchanged.
- **Interfaces:** the CLI arguments and output, the control protocol (newline-delimited
  JSON over a Unix socket, **not HTTP**), and `ingress`'s HTTP responses stay unchanged.

## 15a: Worker and registry

### Scope

- `service/worker.py`, `service/registry.py`, `service/cutover.py`
- New `service/github_reads.py`, `service/target_loader.py` and
  `service/runner_diagnostics.py` (or similar names)
- Tests

### Changes

1. **Move the worker's free functions out of `worker.py`.**
   - GitHub reads (`default_branch`, `head`, `content`, `runner_claims`) go to
     `github_reads.py`. `cutover.py` currently imports `content` and `head` from `worker`;
     point it at the new module.
   - `load_target` (and the `Target` record, if it belongs with it) goes to
     `target_loader.py`.
   - `runner_termination` goes to `runner_diagnostics.py`.
   - **Keep these names importable from `service.worker`**, or update every importer in
     the same commit. Tests import `runner_claims`, `runner_termination` and
     `DeferredExecutor` from `service.worker`.
2. **Small `Worker` helpers.**
   - **`_operator()`:** the construction
     `Operator(gateway.url, private.read(gateway.operator_token_path, (CHECKOUT,)).decode().strip())`
     appears in `executor` and `cleanup_runners`. The token file is read when the helper
     is called, at the same points as today.
   - **`_acknowledge(key, snapshot, controller)`:** `run` and `failed` both acknowledge
     `{**snapshot, "_stage": controller.state["stage"] if controller.state else None}`.
   - **`_facade(repository, issue, generation, task_key=None)`:** four constructions of
     `GatewayGitHub(repository, issue, generation, self.gateway, self.reader(repository), …)`,
     one in `evaluate` and three in `run`. Pass `task_key` only where it is passed today.
3. **Flatten `Worker.failed()`** (49 lines) into a short dispatcher plus a handler per
   effect (`suspend`, `health`). **Keep the exact order and exception boundaries:**
   1. capture `verified`, and read the attempt counter under the guard, **before**
      classification;
   2. classify;
   3. log;
   4. return early on a gateway stop;
   5. apply the effect (`failures.effect` returns `None`, `"health"` or `"suspend"`):
      - **`suspend`:** call `controller.suspend(...)`, with
        `publish_events=not isinstance(error, PublicationError)`, so a `PublicationError`
        suspends with `publish_events=False`.
        - If suspension **succeeds** and the error is a `CheckpointError` (which includes a
          `PublicationError`), publish checkpoint health.
        - If **display fails**, publish checkpoint health with the display diagnostic
          **before** reclassifying from the display error.
      - **`health`:** publish checkpoint health.
      - **`None`:** no diagnostic write. This covers unverified or uncertain errors and
        `Transient`/`GatewayStop`.
   6. a `GatewayStop` raised **during the effect** returns the newly recorded gateway stop
      if there is one, and otherwise the caught stop;
   7. acknowledge (blocked) or defer (wait-until, with a snapshot). This happens
      **outside** the effect's `GatewayStop` handler, so its failures propagate and skip
      step 8;
   8. update the failure counter under the guard.
4. **Registry.**
   - **Split `Registry.open()`** (63 lines) into `_lock_state_dir()`, `_connect()` (pragmas),
     `_initialize(db, registry_id)` and `_migrate_v2(db)`. Keep:
     - **Order:** the order of every step.
     - **Transactions:** each explicit `BEGIN IMMEDIATE`/`COMMIT`.
     - **Error mapping:** it applies **only within the setup block**. `RegistryError` passes
       through, and `sqlite3.Error` becomes "registry is unreadable".
     - **Cleanup:** the database and lock file are closed on failure within that block.
     - **The boundary:** `registry.recover()` runs **after** the setup block. Its failures
       keep their original type and message. [19d](19-recorded-findings.md#19d-release-the-registry-when-recovery-fails)
       adds separate cleanup on recovery failure, releasing the lock even if database
       close raises, without masking the recovery error.
     - **No leaked connection:** the outer handler must still be able to close the
       connection when a pragma fails. Today `db` is assigned before the pragmas run. If
       `_connect()` runs them, it must close the connection itself before re-raising, or
       hand the connection back before the pragmas run.
     - **Identity first:** a wrong registry identity is rejected **before** any migration.
   - **`_mutation_record(row)`:** the `MutationRecord(row[0], (row[1], row[2]), *row[3:11], …)`
     mapping appears in `mutations()` and the single-record lookup.
   - **Bindings query:** the identical `SELECT upstream, fork, config_sha FROM bindings …`
     appears twice. Share the query string.
   - **`without_stage(snapshot)`:** the `{k: v … if k != "_stage"}` projection appears in
     `human_input` and twice in `observe`. `human_input` additionally strips CI fields from
     `pull`; keep that there.

### Dropped from 15a, with reasons

- **A shared `pulls` DDL constant.** The `SCHEMA` copy is multi-line; the v2-migration
  copy is a single line. SQLite stores the exact `CREATE TABLE` text in `sqlite_master`,
  so sharing one string would change the stored schema text of either fresh or migrated
  databases.
- **A `KEY_WHERE` SQL fragment** for `repository = ? AND issue = ?` (23 copies). Splicing
  fragments into SQL makes the queries harder to read.
- **A `MutationStatus` enum.** The status values are mostly embedded in SQL text, the
  same case as the exchange. `STATUSES[1:]` may be named `SETTLED` if that reads better.
- **Rewriting the `BaseException` handlers.**
  - `Registry`'s transaction context manager already uses separate `except` clauses.
  - `Identity`'s token handler revokes, then branches. Splitting it would duplicate the
    revoke call.

### Tests first

- **Migration fixtures:**
  - a fresh v3 database: pin `sqlite_master.sql` for every table, and the `meta` rows;
  - a v2 database migrated in place: pin the resulting schema text and rows, including the
    single-line `pulls` DDL.
- **Registry failure order:**
  - pin the pragma and SQL statement order for opening, initialising and migrating;
  - inject failures during connection setup (including a failing pragma), initialisation
    and migration. Check that the connection and lock file are released and the state
    directory can be reopened.
  - the other refusals: an existing database with `initialize=True`, a missing or empty
    database, an unknown schema version, and a locked state directory;
  - a wrong registry identity, rejected before migration;
  - the separate `recover()` failure path, outside the mapped block.
- **Registry helpers:**
  - mutation records with and without resolution data;
  - both callers of the bindings query;
  - `_stage` removal, with CI-field stripping confined to `human_input`.
- **`failed()` sequence table.** For each failure kind, pin the ordered calls, the returned
  outcome, and the acknowledgement, deferral and failure-counter changes:
  - a gateway stop, `Transient` and preserved errors;
  - unverified and uncertain errors, with no diagnostic write;
  - `Blocked` with a successful suspend;
  - a suspend whose display fails with `CheckpointError` and with another error;
  - a `PublicationError`: suspended with `publish_events=False`, then health;
  - the health effect;
  - a `GatewayStop` raised during the effect, with and without a newly recorded stop;
  - an acknowledgement or deferral that fails: it propagates, and the counter is
    unchanged;
  - a wait-until outcome with no snapshot, which skips the deferral.
- **Worker helpers:**
  - the operator token is read at the same points;
  - each facade's arguments, with and without `task_key`;
  - the acknowledged snapshot's `_stage`.
- **Importers:** the existing `test_worker.py`, `test_transport.py`, `test_runner_diagnosis.py`,
  `test_cutover.py`, `test_registry.py` and `test_service_review_timing.py` pass with at
  most import-path changes.
- **Portability:** use `tempfile` state directories; no host paths.

### Commit message

```text
refactor(service): split worker helpers and registry setup
```

## 15b: Control dispatch and server handlers

### Scope

- `service/__main__.py`, `service/control.py`, `service/ingress.py`, `service/failures.py`,
  `service/targets.py`
- `service/worker.py`, only if the optional effect vocabulary changes its comparison sites
- Tests

### Changes

1. **CLI request building.** `__main__.command(arguments)` is an if-chain that mirrors the
   parser. Attach the fields each subcommand sends with
   `subparser.set_defaults(request_fields=(...))`, or use a small table. Every command must
   produce **the same request dict**: the same keys, the same values, and the same key
   order. Keep the fallback `{"command": arguments.command}` for commands without extra
   fields.
2. **Control dispatch.** Replace `Control.run`'s if-chain with a `{command: handler}`
   table. Preserve:
   - **`evaluate`:** it only applies when `self.evaluate is not None`; otherwise it falls
     through to `ValueError("unknown command")` **without accessing its arguments**.
   - **Conversion:** each command's argument conversion (`number(...)`, the tuple of
     issues) and return value (for example `release` returns `None`).
   - **Unhashable commands:** `{"command": []}` and `{"command": {}}` return
     "unknown command" today, through `==` comparisons. A naive dict lookup would raise an
     unhashable-type error instead. Guard the lookup so these still reach
     "unknown command".
3. **Handler wiring.** `ingress` and `control` build their handler classes with
   `type("XHandler", (Handler,), {...})` to inject a dependency. Read it from
   `self.server.<dependency>` instead, set per server instance, as iteration 16 did for
   the exchange. Keep everything else unchanged:
   - **`control`:** newline-delimited JSON over a Unix socket. Keep the request-size
     boundary, malformed and non-object JSON handling, the exact reply bytes,
     stale-socket replacement and the `0600` socket permissions.
   - **`ingress`:** HTTP. Keep:
     - the 411/413 early returns;
     - the health and unknown GET paths;
     - exact response bodies and headers;
     - exception-to-503 behaviour;
     - POST with **no** path restriction, as today;
     - `GatewayStop` **not** caught by its `except Exception`, as today.
4. **Optional:**
   - `failures.effect` returns `None`, `"health"` or `"suspend"`. Name the two strings as
     constants or a small `StrEnum`, but only if that reads better at the comparison sites
     in `worker.py`. Preserve all three outcomes, including `None`.
   - `targets.verify` imports `policy` inside the function. Move the import to module level
     if no cycle or path bootstrap requires it to be local. The engine path is set when the
     `service` package is imported. Report what you find.

### Tests first

- **CLI table:** for every subcommand and representative arguments, pin the exact request
  dict and its JSON bytes.
- **Control table:** every command routes to its handler, with converted arguments and
  the same return value. Also pin:
  - unknown, missing and unhashable (`[]`, `{}`) commands;
  - missing-argument precedence;
  - invalid issue-number types;
  - absent vs malformed `issues`;
  - disabled `evaluate` falling through without reading its arguments.
- **`control` socket:** the request-size boundary, malformed and non-object JSON, exact
  reply bytes, stale-socket replacement and socket permissions.
- **`ingress` HTTP:**
  - 411 and 413;
  - the health and unknown GET paths;
  - exact bodies and headers;
  - an exception giving 503;
  - POST to an arbitrary path;
  - `GatewayStop` propagating.
- **Per-server isolation:** two `ingress` servers and two `control` servers in one process
  each use their own dependency.
- **Portability:** use `tempfile` for sockets and state; no host paths.

### Commit message

```text
refactor(service): table-driven control dispatch and per-server handler wiring
```
