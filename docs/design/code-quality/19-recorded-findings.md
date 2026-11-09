# 19: Recorded findings (19a–19e)

Part of the [code-quality programme](README.md).

Re-checked against `ce59bf5`, with Codex's review corrections applied. Find code by
symbol; line numbers are hints only.

The refactoring iterations recorded these behaviours as findings and deliberately left them
unfixed, pinned by tests. Unlike the refactors, **each one changes behaviour on purpose**.
Each is a **separate commit**, reviewed and approved on its own, in this order:

| # | Finding | Impact today | Risk |
| ---- | ---- | ---- | ---- |
| 19a | `submit` crash leaves an orphan staffing session | A turn waits up to `START_SECONDS` (20 min) for a runner | Medium |
| 19b | Retirement crash leaves turns `running` | Turns stay `running` until polled | Low–medium |
| 19c | Empty `threadId` passes shape admission | Fails later, at the GitHub lookup | Low |
| 19d | `Registry.recover()` failure leaks the connection and lock | The state directory stays locked in-process | Low |
| 19e | `submit` accepts non-string expected claims | Fails closed later, in `authorize` | Very low |

## Ground rules for all

- **Scope:** fix only the named finding. Everything outside it keeps the guarantees of
  the brief that recorded it (16 for 19a, 19b and 19e; 14 for 19c; 15a for 19d).
- **No repair of existing state:** don't add sweeps or migrations that repair databases
  already in a gap state. That would be a further behavioural change.
- **Tests:** flip the pinning test from "gap" to "recovered" or "refused". Don't delete
  it, and rename it if its name says "gap". Add the cases listed for each finding.
- **Portability:** use `tempfile` databases, lock files and state directories; no host
  paths.

## Exchange transactions (19a and 19b)

### The helper

19a adds one small `Exchange` context manager, for example `_transaction()`, and 19b
reuses it.

- **Locking:** the caller already holds `locked()`. The helper must not acquire it, since
  the lock isn't reentrant.
- **Scope:** no nesting, and never across a gateway call.
- **Failure handling:** `BEGIN IMMEDIATE`, the body and `COMMIT` are all covered, because a
  failure can occur straight after a successful `BEGIN`. On any exception
  (`BaseException`):
  - roll back **only if `db.in_transaction` is still true**. After a successful `COMMIT`,
    an unconditional `ROLLBACK` would fail and mask the original exception;
  - if the rollback itself fails, re-raise the **original** exception.
- **This deliberately lifts brief 16's "no explicit transactions" rule** for the two
  sequences named here. Don't add transactions anywhere else.

### Crash-test harness

`TracedDatabase.execute` injects a failure **after** a statement succeeds. With a
transaction, the statements inside it are no longer durable boundaries; only `COMMIT` is.

- Update the `TracedDatabase` and `RecoveryTests.trace` docstrings to say so.
- `BEGIN IMMEDIATE`, `COMMIT` and `ROLLBACK` reach the harness. Record them in the expected
  traces, or exclude them from the boundary count, and say which.
- **Split each table's expectations:**
  - **before `COMMIT`** (after `BEGIN` or any statement inside): everything rolls back;
  - **after `COMMIT`** (a lost response): every write is committed, and replay or
    reconciliation recovers that state without duplicates.
- **Rollback state:** after an injected failure before `COMMIT`, `db.in_transaction` is
  false and a fresh `Exchange` on the same file can write. A rollback that itself fails is
  the one exception; test that the original exception still propagates.

## 19a: Atomic turn admission in `submit`

### Current behaviour

Under `locked()`, with autocommit, `submit` runs up to three separately committed writes:

1. `INSERT INTO turns` (`queued`, `staff` NULL);
2. `staffed()` → `create()`: `INSERT INTO sessions` for a new unbound runner session.
   **This is conditional:** `staffed()` returns `None`, with no insert, for a pinned turn
   or a group that already has a live runner session;
3. `UPDATE turns SET staff = ?` (possibly to NULL).

A crash after step 2 leaves an unbound session that the turn doesn't reference. The
replay returns `dispatch: None`, because `staff` is NULL. `poll` won't staff the turn
either: `staffed()` sees the orphan as live (`run_id IS NULL`, created within
`START_SECONDS`). Nobody dispatches it, so the turn waits 20 minutes for a runner.

Pinned by `test_submit_order_and_unrecorded_dispatch_recovery_gap`, boundary 2.

### Fix

Wrap the turn insert, `staffed()` and the staff update in the [helper](#the-helper). They
are all local database writes inside `locked()`, with no gateway call between them.

- Return the admission (`observation` plus `dispatch`) **only after `COMMIT`**, so no
  dispatch is handed out for uncommitted state.
- Leave the replay branches (withdrawn, conflicting digest, existing turn) as they are.

### Must not change

- The SQL text and order of the writes; the lock scope.
- Replay results for every existing state, including a `done` turn's result and a
  pending unbound session.
- `staffed()`'s rules and its other caller (`poll`).
- The connection's `isolation_level=None` and default busy timeout, and the CLI
  `list`/`turns` reads outside the lock.

### Tests

- **Flip the gap test,** split as the [harness section](#crash-test-harness) describes:
  - **before `COMMIT`:** after restart there is no turn and no new session. The replay
    admits the turn afresh, with a `dispatch` matching the stored `staff`, and that
    session is the only unbound session for the group;
  - **after `COMMIT`:** the replay returns the committed session as `dispatch`, without
    creating another.
- **All three `staffed()` outcomes,** each inside the transaction: a new session, a pinned
  turn and a group with a live runner (no session insert in either).
- **A failed `COMMIT`.** The `main()` CLI reads run outside `locked()`. On the exchange's
  rollback-journal database, an open reader can make `COMMIT` fail, leaving the
  transaction open. Hold a reader open on a second connection so `COMMIT` fails, then
  show that:
  - the helper rolls back, and the original error propagates;
  - after the reader is released, a replay admits the turn once.

  Don't change production lock scopes or the busy timeout to avoid this.
- **Concurrency:** the existing two-instance staffing tests still pass. A turn is staffed
  by exactly one session.

### Commit message

```text
fix(gateway): admit a turn and its staffing session atomically
```

## 19b: Atomic session retirement

### Current behaviour

- **`retire`:** `UPDATE sessions SET ended` → `_lose_session_turns` → gateway `find` (when
  `key_id` is unset) and `delete` → `forget`.
- **`reconcile`:** for each dead session, `UPDATE sessions SET ended` →
  `_lose_session_turns`; then its key sweep, gateway `list`/`delete` and `forget`.

A crash between `ended` and the turn loss leaves running and pinned queued turns of an
ended session. `reconcile` sweeps only `ended IS NULL` sessions, so it never repairs
them. `poll` does, one turn at a time.

Pinned by `test_end_order_and_partial_retirement_recovery`, boundary 1.

### Fix

Using the [helper](#the-helper), wrap **`ended` plus `_lose_session_turns`** in one
transaction, in both `retire` and `reconcile`'s dead-session loop: one transaction per
session.

- **Outside the transaction:** in `retire`, gateway `find` and `delete`, and `forget`; in
  `reconcile`, gateway `list` and `delete`, and `forget`.
- `reconcile`'s later key sweep (`ended = COALESCE(...)` for keyed sessions) is
  unchanged.
- **Existing databases** already in the gap state still rely on `poll` (see the
  ground rules).

### Must not change

- The SQL text and order; the lock scope; gateway call order.
- `retire`'s `Refused(502)` on a failed deletion, and `poll`'s handling of it.
- `poll`'s lazy recovery of running turns.

### Tests

Update **both** `test_end_order_and_partial_retirement_recovery` and
`test_reconcile_order_and_restart_after_each_boundary`, split as the
[harness section](#crash-test-harness) describes.

- **`end`, before `COMMIT`:** both writes roll back. After a restart the session is
  neither ended nor are its turns lost, so a still-live session keeps serving. A retried
  `end` then completes the whole sequence.
- **`reconcile`, before `COMMIT`:** both writes roll back, and the next `reconcile`
  retires the dead session.
- **After `COMMIT`:** the turns are already `lost` before any poll, and later boundaries
  (gateway calls, `forget`) recover as they do today.
- **A failed gateway deletion after a successful commit:** `retire` raises
  `Refused(502)`. The session stays ended with its turns lost, and reconciliation deletes
  the key.
- The traces show every gateway call outside the transaction.

### Commit message

```text
fix(gateway): end a session and lose its turns atomically
```

## 19c: Refuse an empty `threadId`

### Current behaviour

`admission.resolved_thread` returns `""` for an empty `threadId`. Shape admission
(`allowed`, `allowed_target`) treats that as present. `ready_pull` returns `None` for an
empty `pullRequestId`, so it's refused.

- The engine never sends an empty thread: `Publisher.resolve` (`engine/reviews.py`)
  skips falsy `thread` values before calling `resolve_thread`.
- `bound()`'s GitHub lookup, or the mutation itself, refuses an empty ID later.

Pinned by `test_graphql_shapes` in `service/tests/test_gateway_contract.py` (the
`("", not ready)` row).

### Fix

`resolved_thread` returns `None` for an empty `threadId`, matching `ready_pull`. This
affects all three callers:

- **shape admission** refuses it;
- **`bound()`** makes no GitHub read and falls through to its unmatched-path fallback,
  which returns `True`. That is harmless, because shape admission has already refused the
  request. **Don't make `bound()` a second shape validator;**
- **settlement** no longer treats it as a thread resolution, so a successful GraphQL
  response settles as `unknown`.

### Must not change

- Every other shape decision, and the exact match against `RESOLVE_THREAD`.
- Settlement's thread confirmation for a non-empty thread (`isResolved is True`, without
  an ID match).
- `ready_pull`.

### Tests

- Flip the row: an empty `threadId` is refused by `allowed` and `allowed_target`, for
  every method.
- **End to end:** `mutate` refuses the request, with no mutation recorded or sent.
- **`bound()`:** no GitHub read, and its existing fall-through result.
- **Settlement:** an empty-thread body with a synthetic successful response settles as
  `unknown`; a non-empty thread's confirmation is unchanged.
- Update brief 14's documented difference ("empty `threadId` vs `pullRequestId`") in the
  same commit, so the docs don't describe the old behaviour.

### Commit message

```text
fix(service): refuse an empty review thread ID at admission
```

## 19d: Release the registry when recovery fails

### Current behaviour

`Registry.open()` calls `registry.recover()` **after** its setup block. If recovery
raises (`RegistryFailure("registry storage failed")`), the connection, its open
transaction and the state-directory lock stay held, and the error propagates. A later
`Registry.open` in the same process gets "in use by another process".

Pinned by `test_recovery_failure_is_outside_setup_cleanup` in
`service/tests/test_registry_setup.py`.

### Fix

In `open()`, if `recover()` raises, release the registry and re-raise the **same**
exception.

- **Catch `BaseException`:** `RegistryFailure` derives from `GatewayStop`, which isn't an
  `Exception`. `except Exception` would miss the main failure.
- **Cleanup must not mask the error.** If closing the database fails, still close the
  lock fd, then re-raise the original recovery exception. With this registry's settings,
  closing a connection with an open transaction rolls it back without raising, but
  `close()` isn't guaranteed never to raise.
- **A normal `close()`** keeps its current behaviour. Change it only if that's the
  clearest way to get the cleanup above, and keep it idempotent.
- **Keep the error mapping as it is:** recovery errors are not mapped to "registry is
  unreadable", and keep their type and message.

### Must not change

- The setup block, its error mapping and cleanup, and step order.
- `recover()`'s writes.

### Tests

- **Flip the gap test:** after a recovery failure, the connection and lock fd are closed,
  the same `RegistryFailure` propagates, and the state directory reopens at once.
- **A failure after a recovery write:** the write is rolled back.
- **A failing database close during cleanup:** the lock fd is still closed, and the
  original exception propagates.
- A successful open is unchanged (the golden statement order).

### Commit message

```text
fix(service): release the registry when restart recovery fails
```

## 19e: Require string expected claims in `submit`

### Current behaviour

- `issue` requires the `EXPECTED` keys, permits an optional `actor_id`, and requires every
  value to be a string. Empty strings are accepted.
- `submit` requires the same key set, then `event_name == "workflow_dispatch"` and a
  truthy `actor_id`, but **not** string values.
- **Consequences:**
  - a non-string retained claim fails later, in `authorize`'s comparison;
  - `ref` is overwritten for bound sessions, so a non-string submitted `ref` is accepted
    and never checked.

Pinned by `test_expected_claims_retained_and_overwritten`.

### Fix

Share one validator for the key set (required keys plus optional `actor_id`) and string
values, for example `_expected_claims(expected)`. It raises `ValueError("unknown, missing
or non-string expected claims")`, and `issue` and `submit` both call it.

- **`issue`:** an omitted `actor_id` and empty string values stay accepted.
- **`submit`:** keeps its separate `workflow_dispatch` and `actor_id` check, in its current
  position relative to the other validation.
- **The message:** `submit`'s key-set message changes to the shared one. That's visible
  externally, since `relay.call()` forwards it, but nothing matches the text; the
  executor's decisions use the HTTP status.

### Must not change

- `issue`'s behaviour and message.
- `submit`'s other validation and its order.
- The `ref` overwrite for bound sessions.

### Tests

- **Flip the pinning test:** `submit` refuses a non-string `repository_id` and a
  non-string `ref` with the shared message, before any write.
- **`actor_id`:** omitted (refused by `submit`'s own check), and non-string (refused by
  the shared validator).
- **Competing failures:** for a request with several faults, pin which error wins.
- A valid `submit` is unchanged.

### Commit message

```text
fix(gateway): require string expected claims when submitting a turn
```
