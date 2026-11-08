# 08: Controller guards and call bookkeeping

Part of the [code-quality programme](README.md).

## Goal

Remove copied guard and bookkeeping logic from the controller. Stop the design-review batch
adapter from reaching into controller internals. This iteration touches crash-recovery
code, so **call order is the main invariant**.

## Scope

- `engine/controller.py`
- `engine/design_batch.py`
- `engine/sessions.py` / `engine/design_eligibility.py`, only if a helper naturally belongs there
- Tests

## Changes

### 1. Model-call bookkeeping shared by the serial and batch paths

| Logic | Serial copy | Batch copy | New `Controller` method |
| ---- | ---- | ---- | ---- |
| Increment `state["calls"]` and charge the credential (`setdefault("credentials")…["calls"] += 1`) | `controller.py:874-880` | `design_batch.py:133-138` | `charge_call(operation, credential)` |
| Record a completed session turn (`turn += 1`, `completed = {call, request, response}`) | `controller.py:906-910` | `design_batch.py:163-168` | `record_session_turn(session, call, request, response)` |
| Session restart after a `SessionFailure` (`design_eligibility.restart`, `sessions.restart`, the "failed twice" limit and message) | `controller.py:900-904` | `design_batch.py:205-213` | `restart_session(session)`, plus a shared `MAX_SESSION_FAILURES` |
| The "uncertain twice" limit and message | `controller.py:945-946` | `design_batch.py:117-118` | `MAX_UNCERTAIN = 2` and one message constant |

- **The two call sources differ.** The serial path uses `state["calls"]`; the batch path
  uses `operation["call"]`. Pass `call` explicitly.
- **Leave `save()` calls where they are.** The helpers must not call `save()` themselves
  unless the call already happens at that exact point in both copies.

### 2. Stop setting controller internals from outside

`design_batch.Design` sets `controller.consuming = None` at four sites (`design_batch.py`
40, 171, 188, 223). Add a `Controller.release_result()` method, or a better-named
equivalent, and call it instead.

### 3. Guards and small helpers in the controller

- **`require_pr_unchanged(message)`:** fetches the PR and raises `Blocked(message)` if
  the head SHA differs, the PR is not open, or it is merged. Use it at 1101–1104, 1574–1577
  and 1611–1614. **Don't** use it at 2316–2318, which omits the merged clause. Leave that
  one alone, or give the helper a `require_unmerged=True` parameter and pass `False` there.
- **`reconcile_changed_files()`:** the changed-path reconciliation duplicated at 1171–1175
  and 1615–1618.
- **`consume_on_block(comment, save=True)`:** a context manager for "validate; on
  `Blocked`, set `command_id`, optionally save, and re-raise". It is repeated at 1092–1108,
  1567–1584 and 1605–1621. `invalidate_task` must use `save=False`; see the intentional
  differences in the README.
- **`agent_fields(provider, stage) -> dict`:** replaces the
  `**({"agent": resolve(...).record()} if "agents" in self.config else {})` splat at 1431,
  1438, 2015 and 2127. Also add a `uses_agent_config` property for the 9
  `"agents" in self.config` checks.
- **`model_label(provider, stage)`:** the copilot/legacy fallback duplicated in `classify`
  (449–451) and `welcome` (1025–1029).
- **`issue_digest()`:** replaces the 9 copies of `digest([self.issue["title"], self.issue["body"]])`.
- **`audit_size(state)`:** replaces the 8 copies of
  `len(self.checkpoints.audit_body(x).encode())`. Add a named constant for
  `CHECKPOINT_LIMIT - <margin>` where the same margin repeats.
- **Key groups:**
  - `discard(state, *keys)` for the repeated `for key in (...): state.pop(key, None)` loops
    (1116, 1150, 1248, 1644, 1691, 1830).
  - Name the key groups that repeat, for example the validation keys at 1150/1830.
  - `("plan", "plan_id", "branch", "qc")` at 1248 is a subset of 1691. Name both groups,
    and don't merge them.
- **`reset_repair_allowance(repair)`:** the top-level copy at 1741–1743 and the per-member
  copy at 1746–1748.

### 4. Split `ask` (845–912) using the helpers above

The budget check is duplicated (862, 867), and so is the `sessions.prepare` +
`design_eligibility.prepare` pair (856–858, 869–871). Extract `_check_call_budget()` and
`_prepare_session(...)`. If `_handle_dispatch_error(...)` (888–905) can be extracted
without moving any `save()`, extract it as well. Otherwise leave it inline and say so.

## Must not change

- **Call order and count, along each path:**
  - `save()`, `checkpoint()`;
  - credential charges;
  - `state["calls"]` increments;
  - session-turn increments;
  - external GitHub reads and writes.
- **`Blocked` message text.**
- **The intentional differences** at 2316–2318 and in `invalidate_task`.

## Tests first

- **Call-order spy test:** wrap `save`, `checkpoint`, `agents.ask` / `transport.prepare`
  and the GitHub fake with spies. Record the ordered call sequence and resulting `calls` /
  credential counts for:
  - a serial intake call;
  - a design-review batch with one reviewer succeeding, one hitting `SessionFailure` once,
    and one with an `UncertainOutcome`;
  - a crash after `begin_attempt`, then resume.

  Run it against the old code, pin the sequences, and keep the test.
- **Refusal tests:** one for each `consume_on_block` call site. The refusal consumes the
  command; the save count matches the old behaviour, including no save for `invalidate_task`.

## Done when

- Each listed duplicate has one implementation.
- `design_batch.py` no longer assigns to controller attributes.
- The spy sequences are identical before and after.
- All suites and validation commands are green.

## Commit message

```text
refactor(engine): share call bookkeeping and controller guards
```
