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

## 08b: Serial and batch call bookkeeping (reassess after 08a)

### Goal

The serial path (`Controller.ask`) and the parallel design-review path (`design_batch`)
each implement model-call bookkeeping by hand. Give them one implementation. Also stop
`design_batch` from assigning to controller attributes.

### Scope

- `engine/controller.py`
- `engine/design_batch.py`
- Tests

### Changes

| Logic | Serial copy | Batch copy | Shared implementation |
| ---- | ---- | ---- | ---- |
| Increment `state["calls"]` and charge the credential | `Controller.ask` | `Design.reserve` | `charge_call(operation, credential)` |
| Record a completed session turn | `Controller.ask` | `Design.complete` | `record_session_turn(session, call, request, response)` |
| Session restart after a `SessionFailure`, with the "failed twice" limit | `Controller.ask` | `Design.failed` | `restart_session(session)` plus a shared limit constant |
| The "uncertain twice" limit and message | `Controller` operation recovery | `Design.reserve` | One constant and one message |
| Releasing the consumed result | — | `design_batch` sets `controller.consuming = None` at four sites | `Controller.release_result()` |

- **The two call sources differ.** The serial path uses `state["calls"]`; the batch path
  uses `operation["call"]`. Pass `call` explicitly.
- **No helper calls `save()`,** unless both copies already save at that exact point.

### Must not change

Along each path, the order and count of:

- `save()`, `checkpoint()`;
- credential charges, `state["calls"]` increments, session-turn increments;
- transport `prepare` / `submit` calls.

### Tests first

**Call-order spy test.** Spy on `save`, `checkpoint`, the transport and the GitHub fake.
Pin the ordered call sequence and the resulting counts for:

- a serial intake call;
- a design-review batch with one reviewer succeeding, one hitting `SessionFailure` once,
  and one returning `UncertainOutcome`;
- a crash after `begin_attempt`, then a resume.

### Commit message

```text
refactor(engine): share serial and batch model-call bookkeeping
```
