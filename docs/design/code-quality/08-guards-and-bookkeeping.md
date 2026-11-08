# 08: Controller guards (08a) and call bookkeeping (08b)

Part of the [code-quality programme](README.md).

This brief covers **two separate commits**. 08a comes first. **08b is reassessed after 08a
lands**, and is approved separately.

Line numbers below refer to `f1225e6`, before the dashboard extraction. Find the code by
symbol.

## 08a: Authorisation and recovery guards

### Goal

These guards decide whether a writer command or a resumed task may proceed, and what
happens to the command when it is refused. Today each one is copied by hand at several
call sites. Give each guard **one implementation that states its contract**, so the call
sites can't drift apart.

Extract a helper only where it expresses such a contract. Wrapping individual assignments
adds indirection without value.

### Scope

- `engine/controller.py`
- Tests

### Changes

1. **PR-unchanged guard.**
   - The predicate is: the PR's head SHA equals `state["head"]`, the PR is open, and it is
     not merged. It is copied in `refresh`/resume paths, `extend_plan_revisions` and
     `invalidate_task`; find the copies with
     `grep -n 'pr\["head"\]\["sha"\] != state\["head"\]'`.
   - Add `require_pr_unchanged(message)`, which performs the single GitHub read and raises
     `Blocked(message)`.
   - **The variant without the merged clause** (in `step()`'s authority checks) has a
     different contract. Leave it separate, or use an explicit `require_unmerged` flag;
     don't fold it in silently.
2. **Consume-on-refusal.**
   - The pattern is "validate; on `Blocked`, record `command_id`, optionally save, and
     re-raise", in `extend_plan_revisions`, `retry_reviewed` / bundle retry paths and
     `invalidate_task`.
   - Add a context manager such as `consuming(comment, save=True)`.
   - `invalidate_task` must use `save=False`: a refused invalidation consumes the command
     without saving. This is an intentional difference listed in the README.
3. **Changed-path reconciliation.**
   - The block that reads the PR's files, checks every path with `path_allowed`, and raises
     `"cannot reconcile accumulated PR changes"` is copied twice.
   - Make it one method that returns the changed paths.
4. **Only where it states a real contract:**
   - `issue_digest()`, which defines what counts as the issue's identity for change
     detection;
   - `agent_fields(provider, stage)`, which defines when an agent record is attached to a
     report.

   Leave the remaining small repeats inline (key-pop loops, audit-size expressions).

### Must not change

- **Predicates:** each guard's exact predicate, including the no-`merged` variant.
- **GitHub reads:** the order and number of reads (PR fetch, files listing) along each
  path.
- **Command consumption:** when `command_id` is set, for success and for every refusal.
- **Saving:** whether and when `save()` is called, for success and for every refusal.
- **Messages:** `Blocked` message text.

### Tests first

Write these against the current code, and keep them:

- **Guard refusals:** for each guard call site, a refusal test that records:
  - the GitHub requests made, in order;
  - `command_id` before and after;
  - the number of `save()` calls;
  - the exception type and message.

  Cover a PR whose head changed, a closed PR and a merged PR. For the no-`merged`
  variant, show that a merged PR with the same head is **not** refused there.
- **Reconciliation refusals:** a test for each reconciliation call site, with a disallowed
  path.

### Commit message

```text
refactor(engine): give controller authorisation guards one implementation each
```

## 08b: Serial and batch call bookkeeping (narrowed)

Reassessed after 08a, 2026-11-08. The duplicated code is small, about 15 lines. The value
is in keeping the **accounting and recovery rules** of the serial path (`Controller.ask`)
and the batch path (`design_batch.Design`) from drifting apart.

### Goal

Give each shared bookkeeping rule one implementation, and stop `design_batch` from writing
controller internals. Session restart is deliberately **not** unified: the two control
flows differ.

### Scope

- `engine/controller.py`
- `engine/design_batch.py`
- Tests

### Changes

1. **`charge_call(credential) -> int`:**
   - increments `state["calls"]`;
   - charges the per-credential `calls` counter;
   - returns the new call number.

   The serial path uses it in `ask`. The batch path uses it in `Design.reserve`, and
   keeps storing the returned number in `operation["call"]`. The credential record must
   be built exactly as today: the two copies spell it differently but produce the same
   dict.
2. **`record_session_turn(session, call, request, response)`:** increments the session
   `turn` and writes `completed = {call, request digest, response digest}`. The callers
   pass their own call number and request context:
   - serial: `state["calls"]` and `context`;
   - batch: `operation["call"]` and the member's frozen context.
3. **The uncertain-attempt rule:** one constant (`MAX_UNCERTAIN = 2`) and one message,
   shared by `begin_attempt()` and the batch's pre-admission check in `Design.reserve`.
   **The two checks are not identical today:**
   - `begin_attempt()` increments `operation["uncertain"]` when the status is `started`
     or `uncertain`, then checks the count **unconditionally**.
   - The batch pre-check only fires when the status is `started` or `uncertain`, and
     checks `uncertain + 1`.

   Define one calculation that reproduces **each caller's current result exactly**. For
   example, a function returning the count after this attempt's increment, which each
   caller compares as it does today. If making them share a calculation would change
   either caller's result for any reachable state, don't fix it here. Report the
   discrepancy as a separate behavioural finding.
4. **`Controller.release_result()`:** replaces the four direct
   `controller.consuming = None` writes in `design_batch` (`select`, `complete`, `apply`,
   `close`).
   - `consuming` tells `checkpoint()` which retained response to drop once its result has
     been used. The method names that recovery operation.
   - Behaviour is identical: it sets `consuming` to `None`.
   - The controller's own internal writes may use it too.

**Not in scope:** unifying session restart after `SessionFailure`.
- `ask` uses a two-attempt loop; the batch keeps a per-member failure counter.
- The serial `RunnerTerminated` path deliberately restarts only the session, not
  eligibility.
- Sharing the `"provider session failed twice"` message text is optional; nothing more.

### Persistence boundaries

The helpers are pure bookkeeping on in-memory state:

- **`charge_call` and `record_session_turn`** must not call `save()` or `checkpoint()`,
  dispatch or prepare a turn, restart a session, or change design eligibility. The callers
  keep every one of those calls exactly where they are.
- **`release_result()`** only resets `consuming`.

### Must not change

- **Order and count of side effects,** along each path:
  - `save()`, `checkpoint()`;
  - credential charges, `state["calls"]` increments, session-turn increments;
  - transport `prepare` / `submit` calls.
- **Batch call numbers:** the call number assigned to each roster member, and to
  `operation["call"]`.
- **Atomic roster admission:** in `Design.reserve`, a refusal for any member, including a
  later one, happens before any member is charged, begun or dispatched. The whole-roster
  budget check also stays ahead of all preparation.
- **Messages:** `Blocked` message text.

### Tests first

Write these against the current code, and keep them.

- **Call-order spy test.** Spy on `save`, `checkpoint`, the transport and the GitHub fake.
  Pin the ordered call sequence and the resulting counts for:
  - a serial intake call;
  - a design-review batch with one reviewer succeeding, one hitting `SessionFailure` once,
    and one returning `UncertainOutcome`;
  - a crash after `begin_attempt`, then a resume.
- **Batch call numbers:** pin the `operation["call"]` assigned to each member, and the
  final `state["calls"]` and per-credential counts.
- **Roster refusal is atomic.** Two cases.
  - **A later member fails its uncertainty check.** Assert:
    - no calls or credentials are charged;
    - no member's attempt is begun;
    - no turn is submitted;
    - no partial charges are saved.

    Earlier members **may** already have been prepared with `transport.prepare()`, which
    is local and neither dispatches nor charges. Pin today's preparation calls rather than
    requiring none.
  - **The call budget can't admit the whole roster.** That refusal happens before any
    member is prepared. Assert no preparation either.
- **Uncertain-count boundaries:** for both `begin_attempt()` and the batch pre-check,
  and for each status (`new`, `started`, `uncertain`, `failed`, `completed` without a
  response, `consumed`), cover counts 0, 1 and 2. Pin whether each refuses. Any status
  and count where the two disagree goes in the summary as a finding.
  - These tests show how each check behaves for the states supplied; they don't show
    which states are reachable.
  - The view that a disagreeing state may be unreachable today is provisional. If the
    summary reports a disagreement, say whether you found a path that reaches it.
- **`release_result()`:** the existing batch recovery tests pass unchanged. A retained
  response is still dropped only when its own result is consumed.

### Commit message

```text
refactor(engine): share serial and batch model-call accounting
```
