# 16: Exchange structure

Part of the [code-quality programme](README.md).

## Goal

Restructure `gateway/exchange/exchange.py` (800 lines) **within its single file**. Its
Dockerfile copies only `exchange.py`, so it must not import from `engine/` or `service/`.

`Exchange` (223–609) mixes five jobs:

- schema migration;
- OIDC authentication;
- gateway-key lifecycle;
- the turn queue;
- reconciliation.

`server()` (632, 124 lines) defines its HTTP handler inside a closure.

## Scope

- `gateway/exchange/exchange.py`
- `gateway/tests/`

## Changes

### 1. In-file classes

Split `Exchange` into in-file classes that share one database handle:

- `Store`: connection, schema, migration and row helpers;
- `SessionAuth`: OIDC, JWKS refresh and gateway keys;
- `TurnQueue`: submit, claim, poll and complete.

Keep `Exchange` as a facade with today's public methods, so the handler and tests keep
working.

### 2. Duplication

- **`_lose_session_turns(...)`:** the "mark turns lost" SQL is identical at 426–427 and
  586–587, and the UPDATE at 537 is the same as at 541.
- **`_long_poll(wait, check)`:** the long-poll loop is duplicated in `next` (376–390) and
  `poll` (527–563). Keep each loop's wait cap and wake conditions.
- **Expected-claims validation:** duplicated at 270–271 and 469. Make it one function.
- **JSON digest:** one `_digest(value)` for the two sha256-of-JSON copies (220, 482).
- **Row access:** `row()` keeps the column list twice, in the SELECT and the zip (289–295).
  Use `sqlite3.Row` with `dict(row)`, or one column tuple. The returned dict must have the
  same keys and values.
- **`observation`:** it queries the same row twice (447–450). Query it once.
- **Clock defaulting:** a small `_now(now)` helper for the seven copies of
  `now = time.time() if now is None else now`.

### 3. Vocabularies

Turn statuses and session kinds become local `StrEnum`s, defined in this file. Add a
comment naming the engine definitions they mirror (`engine/vocab.py`, `ExchangeStatus`).
Keep the run-branch prefix and session-ID regex as local constants, with the same comment.

### 4. HTTP handler

- Move the handler class out of the `server()` closure to module level. It reads its
  dependencies from `self.server.<dependency>`.
- The nested `Bounded` server keeps the semaphore as a class attribute (737); make it an
  instance attribute.
- **Keep the existing query-string parsing** (`dict(p.partition("=")[::2] ...)`, 699)
  exactly as it is. `parse_qsl` would accept requests the exchange rejects today:
  - `wait=` becomes a zero wait;
  - `wait=%31` becomes a one-second wait.

  Moving the parser into a named function is fine; changing what it accepts is not.
- `except Exception` at 682–683 returns 500 and logs nothing. Keep the broad catch and the
  500 response. Logging the exception type is optional, and must not include request data
  or secrets.

### 5. Constants

Name the capacity (8), wait cap (50), activation polling (50 × 0.2 s), JWKS refresh
interval (60 s) and `86400` (588).

## Must not change

- **HTTP API:** routes, status codes, response bodies and headers.
- **Authentication and authorization decisions.**
- **Database schema and stored values.**
- **Long-poll timing and wake behaviour.**
- **Lock and transaction boundaries.** Each public operation keeps the same SQLite
  transaction scope (`BEGIN` / commit points) and the same `threading` lock and condition
  usage. When code moves into `Store` / `SessionAuth` / `TurnQueue`, no operation may span
  two transactions where it used one, and no lock may be released and re-acquired where
  it was held continuously.
- **Accepted query strings.** The same inputs are accepted and rejected as before.
- **Turn state transitions,** including lost and cancelled handling.
- **Single-file packaging.** Confirm the Dockerfile still copies only `exchange.py` and
  that nothing new is imported.

## Tests first

- **HTTP contract test:** run the exchange in-process and pin the request/response pairs
  for each route: submit, claim, poll (immediate and long-poll timeout), complete, cancel,
  heartbeat loss → lost, and auth failure.
- **Rejected query strings:** a query-parsing test that pins rejection of `wait=`,
  `wait=%31`, duplicate keys and missing values.
- **Concurrency:** a test that runs concurrent claim, complete and poll calls against one
  turn and pins the resulting state transitions. HTTP snapshots alone do not show lock
  boundaries.
- **Lock map in the summary:** list each public operation with its transaction and lock
  scope, before and after.
- `gateway/tests/test_exchange.py` must pass unchanged, apart from fixture moves already
  made in iteration 06.

## Done when

- `Exchange` delegates to the in-file classes.
- The handler is at module level.
- Each listed duplicate has one implementation.
- The HTTP contract test passes before and after.
- All suites and validation commands are green.

## Commit message

```text
refactor(gateway): split the exchange into in-file components
```
