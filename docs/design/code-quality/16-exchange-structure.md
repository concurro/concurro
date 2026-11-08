# 16: Exchange structure

Part of the [code-quality programme](README.md). The exchange brokers every model turn and
gateway key, so concurrency and crash-safety matter more here than anywhere else.

Re-checked against `7cd9d95` (986 lines after formatting), with Codex's review corrections
applied. Find code by symbol; line numbers are hints only.

## How the exchange works today (the invariants to preserve)

- **Single-file packaging.** `gateway/exchange/exchange.py` ships alone; its Dockerfile
  copies only that file. It must not import from `engine/` or `service/`.
- **No explicit multi-statement transactions.**
  - The connection uses `isolation_level=None` (autocommit). Each statement runs in
    SQLite's own implicit statement transaction and commits on its own.
  - The code orders its mutations and gateway calls deliberately. For example, `key()`
    stores a minted key before calling the gateway, so reconciliation can find it.
  - That ordering doesn't guarantee that **every** intermediate crash state recovers
    automatically. Recovery is what the reconciliation and replay paths implement today.
  - **Don't introduce transactions,** and don't reorder mutations or gateway calls.
- **Lifecycle serialisation is `Exchange.locked()`.** It is a `threading.Lock` combined
  with an `fcntl` file lock, which serialises the server and operator CLI processes, and
  separate `Exchange` instances that share one database.
  - **These public operations take it:** `issue`, `key`, `prove`, `next` (once per loop
    iteration), `complete`, `end`, `submit`, `poll` (once per loop iteration), `cancel`
    and `reconcile`.
  - **These database helpers rely on their callers holding it** for serialisation:
    `runner`, `authorize`, `refuse`, `retire`, `forget`, `staffed`, `observation`, `row`
    and `create`.
  - **`live()`** is a pure predicate over a supplied record, and doesn't need the lock
    itself.
  - **Outside the lock, by design:** the constructor's schema creation and in-place column
    migration, and the CLI `list`/`turns` reads in `main()`. Preserve these exceptions;
    don't broaden locking.
  - **The lock is not reentrant.** Acquiring it **again while already holding it** would
    deadlock, so a helper called under the lock must never call `locked()`.
- **Long polls release the lock between iterations.** `next` and `poll` hold the lock for
  one check, then sleep `POLL_SECONDS` **outside** it. The clock (`current`) is read
  **after** acquiring the lock, on each iteration.
- **The HTTP layer is built per server.** `server()` defines `Handler` and `Bounded`
  inside the function, so each call gets its own classes and dependencies. Each server
  gets its own `Bounded.slots` semaphore (`MAX_REQUESTS`), its own body `Budget`, and its
  own operator token digest.
- **HTTP request flow.** It is branch-specific; pin these precedence differences rather
  than introducing uniform early authentication.
  - **Operator routes:** the operator token is verified **before** any body admission.
    Then the size limit is chosen (`MAX_BODY` for submit, otherwise `SMALL_BODY`).
  - **Runner routes:** the route, the method (POST only) and the presence of a bearer
    token are checked first.
    - **Result submissions** (`turns/<id>`) call `exchange.prove()` **before** selecting
      `MAX_BODY` and checking the length. `prove()` also records a heartbeat.
    - **`key`, `next` and `end`** authenticate **inside** their handler, **after** the
      body has been parsed.
  - **All routes:** `Content-Length` is checked against the limit. Then the body `Budget`
    is reserved for **body parsing and handler execution**, including long polling. The
    reservation ends before the reply is written.
- **Query parsing** happens at the top of the operator handler, before any route
  dispatch, for every operator route, including submit, cancel and session end.
  `dict(p.partition("=")[::2] …)` means:
  - a duplicate `wait` keeps the last value;
  - no `wait` means `0.0`;
  - a bare `wait`, `wait=` or `wait=%31` raises `ValueError`, which becomes a 400.
- **Process-lifetime state:** the HTTP servers, and the `Keys` JWKS cache (its keys,
  refresh timestamp and lock).

## Goal

Make the HTTP layer readable and give the repeated database rules one implementation each.
**Lock scopes, the order of mutations and gateway calls, the HTTP contract, accepted
requests and stored values must stay exactly as they are.**

Splitting `Exchange` into separate classes (`Store`/`SessionAuth`/`TurnQueue`) is **not
required**. Its methods share one lock and call each other heavily: `submit` uses `row`,
`observation`, `staffed`, `live` and `create`, and `poll` uses `retire`. Do it only if a
seam reads clearly better without spreading lock assumptions. Otherwise, report why you
left it.

## Scope

- `gateway/exchange/exchange.py`
- `gateway/tests/`

## Changes

1. **HTTP layer out of the closure.**
   - Move `Handler` and `Bounded` to module level. The handler reads its per-server
     dependencies (`exchange`, the operator token digest, the body `Budget`) from
     `self.server`, and `server()` sets them on each server instance.
   - **`Bounded`'s semaphore becomes an instance attribute,** created per server. A
     module-level class attribute would make all servers in one process share it.
   - Keep the branch-specific request flow above exactly, including:
     - when `prove()` runs;
     - that runner `key`/`next`/`end` authenticate after body parsing;
     - that the budget covers handler execution.
   - **Keep the query-string parsing exactly as it is,** including where it runs.
   - Keep the broad `except Exception` and its bare 500 response. Logging the exception
     type is optional, and must not include request data or secrets.
   - Keep `log_message`'s current behaviour: it formats the supplied log arguments,
     including request-line information, as `exchange <command> <message>`. It does not
     redact anything, so don't describe it as a redactor.
2. **Turn-loss rules.**
   - **Lose a session's turns:** `retire` and `reconcile` both run the identical
     `UPDATE turns SET status = 'lost' … WHERE (session = ? AND status = 'running') OR
     (pin = ? AND status = 'queued')`. Share it as `_lose_session_turns(session, now)`,
     which relies on its caller holding the lock.
   - **Lose one turn:** `poll` runs the same single-turn
     `UPDATE turns SET status = 'lost', finished = ? WHERE id = ?` twice. Share it as
     `_lose_turn(turn, now)`.
   - Each mutation keeps its position.
3. **Session row columns (a permitted read-query change).** `row()` writes its 14-column
   list twice. Use one tuple of column names for both the generated `SELECT` and the
   `zip`. The returned dict must keep the same keys, values and key order.
4. **`observation()` (a permitted read-query change).** It queries the same turn row twice:
   once for status and times, again for `session` when the status is `lost` or
   `cancelled`. Query it once. The returned dict must be identical, including when the
   `runner` entry is present.
5. **Clock defaulting.** Add a small `_now(now)` helper for the
   `time.time() if now is None else now` copies. In `next` and `poll`, keep the call
   **after acquiring the lock**, once per iteration.
6. **Named limits, each next to its use:**
   - `next()`'s accepted capacity ceiling (8). Actual dispatch is still at most one turn,
     via `min(capacity, 1)`. This is unrelated to `MAX_REQUESTS`.
   - The long-poll wait ceiling (50), which `next` and `poll` share.
   - The gateway activation polling (50 × 0.2 s).
   - The JWKS refresh interval (60 s).
   - The finished-turn retention (86400 s).
7. **Optional: a long-poll helper.** Extract one only if both `next` and `poll` keep their
   exact lock and return points. `next` returns after releasing the lock; `poll` returns
   from inside it in several branches. Otherwise leave the loops as they are.
8. **Don't change these:**
   - the SQL text of mutations, and the status literals embedded in it (`'queued'`,
     `'running'`, …);
   - the CLI subcommands in `main()`, which may keep reading `exchange.db` directly
     outside the lock.

## Must not change

- **Locking:** which operations take `locked()`, where it is acquired and released, that
  helpers never acquire it again, and the existing unlocked exceptions (schema migration
  and CLI reads).
- **Ordering:** the order and SQL text of every mutation and gateway call, and the
  absence of explicit transactions. The only permitted SQL changes are the two read
  queries in changes 3 and 4.
- **HTTP contract:**
  - routes, status codes, bodies, headers, size limits, and the branch-specific
    authentication and body-admission order;
  - budget scope;
  - 503 busy behaviour;
  - query-string parsing and where it runs.
- **Data:** the stored schema and values, and the in-place column migration.
- **Long polls:** timing, wake behaviour, sleeping outside the lock, and reading the clock
  after acquiring it.
- **Turn lifecycle:** state transitions, including `lost` and `cancelled` handling and
  replay after a lost reply.
- **Packaging:** the file stays single, and the Dockerfile still copies only
  `exchange.py`.

## Findings to report, not fix

- **Expected-claims validation differs.**
  - `issue` requires the exact expected-claim key set, with every value a string.
  - `submit` requires the key set, a literal `event_name == "workflow_dispatch"` and a
    truthy `actor_id`, but doesn't require string values.
  - **Retained claims:** a non-string retained claim fails later, in `authorize`'s string
    comparison.
  - **`ref`:** it is **overwritten** when a bound session is created. So a non-string
    submitted `ref` never reaches that comparison, and needn't fail.
  - Keep both behaviours, and report them.
- **Recovery gaps.** Report any intermediate crash state that the new ordered traces
  show is not recovered automatically. Don't fix it here.

## Tests first

Write these against the current code, pin them, and keep them. `test_exchange.py` must
pass unchanged, apart from fixture moves from iteration 06.

### HTTP contract

Run the server in-process and pin the request/response pairs:

- runner `key`, `next` (immediate and long-poll timeout), result submission (including a
  conflicting replay) and `end` (with an OIDC token and with a runner token);
- operator submit, poll (immediate and long-poll timeout), cancel and session end;
- missing or wrong bearer tokens, an unknown route, an oversized body, a non-object JSON
  body, and the operator API disabled without a token;
- **precedence:** for result submissions, an invalid runner token is refused before the
  body length is checked. For runner `key`/`next`/`end`, a malformed body is rejected
  before authentication. For operator routes, an invalid token is refused before the
  body is read.

### Query parsing table

Pin the parsed `wait` value, or the resulting status and body:

- `wait=1&wait=2` gives 2.0;
- no `wait` gives 0.0;
- a bare `wait`, `wait=` and `wait=%31` each give a 400.

Also show that parsing happens before route dispatch: a bad `wait` on submit, cancel or
session end is also a 400.

### Per-server isolation

- Create two servers in one process. Saturate one and show the other still accepts
  requests.
- Show that each server keeps its own body budget and its own operator token.

### Release after failure

The `Bounded` semaphore slot and the body budget are both released after:

- a body-parsing failure;
- a handler exception, giving a 500;
- a `Refused` raised inside the handler.

### Concurrency

- competing `next()` calls for the same staffing group, where a turn is claimed by
  exactly one session;
- concurrent staffing through `submit` and `poll`, including **two `Exchange` instances
  sharing one database**, so the file lock is exercised;
- `complete` racing `cancel`;
- `end` and `reconcile` racing a claim or a completion.

Pin the resulting state transitions and that no turn is claimed twice. HTTP snapshots
alone don't show lock boundaries.

### Long-poll lock behaviour

- The long-poll sleep happens with the lock released: another thread can take the lock
  during the sleep.
- The per-iteration clock is read after the lock is acquired.

### Ordered traces and restart checks

Record the exact order of SQL mutations and gateway calls, and inject a failure at each
boundary, then restart (reload the database, reconcile or replay):

- in `retire`/`end`: session ended → turns lost → gateway deletion → `forget`;
- in `key`: key stored → gateway find/create/activate → `key_id` stored;
- in `submit`: turn inserted → staffing session created → staff assigned.

The existing partial-creation test seeds intermediate states by hand; it doesn't inject
failures or prove ordering. Report any boundary that doesn't recover today, as a finding.

### Lock map in the summary

List each public operation with its lock scope, each helper that relies on its caller
holding the lock, and the unlocked exceptions, before and after.

### Portability

Use `tempfile` databases and lock files; no host paths.

## Done when

- `Handler` and `Bounded` live at module level, with per-server semaphore, budget and
  token.
- Each listed duplicate has one implementation.
- The lock map is unchanged, and every test group above passes before and after.
- All suites and validation commands are green, and CI is green after the push.

## Commit message

```text
refactor(gateway): lift the exchange HTTP layer and share its turn-loss rules
```
