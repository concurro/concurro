# 10: Execution path (10a–10d)

Part of the [code-quality programme](README.md).

This brief covers **four separate commits**. Each sub-iteration is reviewed and committed
on its own. Any of them can be dropped at reassessment without affecting the others,
except that 10b's tests reuse 10a's process helpers if they exist.

Recommended order: 10b, 10a, 10c, 10d. 10b gives the turn lifecycle and its failure
handling one clear structure; that is its value. Neither the repeated `runner_bound()`
calls (a cheap membership test) nor `execute()`'s length is a reason on its own. Split
or extract only where it makes the lifecycle easier to follow. 10b can also adopt
`ExchangeStatus` and `TerminationReason` vocabulary in `vocab.py`, next to iteration 04's
`TurnOutcome`.

## Shared constraints

- **Turn results and their fields stay the same:**
  - `outcome`, `termination_reason`, `elapsed_seconds`;
  - `retained`, `workspace`;
  - the exit codes `UNCERTAIN_EXIT` and `TIMEOUT_EXIT`.
- **Exceptions stay the same:** the text and types of `RunnerTerminated`,
  `UncertainOutcome` and `SessionFailure`, and which outcome raises which.
- **Timing stays the same:** poll timing and backoff values, cancellation and withdrawal
  calls, and credential lease arithmetic.
- **`repository.py` keeps its import constraints** (see the README).

## 10a: Native turn execution and process cleanup

**Scope:** `engine/turns.py`, `engine/processes.py`, `engine/policy.py`, tests.

1. **Split `turns.execute`** (200–285):
   - `_validated_timeout(request, timeout)`;
   - `_run_agent(payload, env, timeout, stopped) -> (code, stdout)`, which owns the
     process group, the cancel thread and cleanup. Move the function-local
     `import threading` to the top of the module.
   - `_termination(code, stopped, stage, started)`: turn the nested ternary (275–276)
     into an if-chain.

   Leave the evaluation-fixture swap inline if extracting it doesn't make it clearer.
2. **Named exit codes:** move `(-2, -15, 130, 143)` (270) into `policy.SIGNALLED`, next to
   `KILLED`.
3. **Process-group kill:** add `processes.kill_group(pid)` for the duplicated `killpg` /
   `ProcessLookupError` blocks (241–245, 256–259).
4. **Named `prctl` option:** name `prctl(36, …)` (`processes.py:28`) as
   `PR_SET_CHILD_SUBREAPER`.

**Tests first:** `test_runner_termination.py` and `test_turns.py` exercise the real
subprocess paths; they must pass unchanged. Add a test that pins the
`(outcome, termination_reason)` pair for:

- a normal exit;
- `TIMEOUT_EXIT`;
- each signalled code;
- a stopped runner.

**Commit message:**

```text
refactor(engine): split native turn execution and share process cleanup
```

## 10b: Result decoding and the runner executor

**Scope:** `engine/agents.py`, `engine/relay.py`, `engine/runner.py`, `engine/turns.py`, tests.

1. **`decode_result`** (136–173):
   - Move the message dicts to module constants.
   - Add a `runner_terminated(detail)` factory for the shared suffix, "Inspect … before a
     writer retries; no automatic restart was attempted" (145–147, 156–157, 331–332,
     353–355). Use it only where the produced text is identical character for character.
   - Derive `"30-minute"` from `QUEUE_SECONDS // 60`, producing the same text.
2. **`RunnerExecutor`:**
   - Cache `job_bound` on `TurnHandle`; it is recomputed six times.
   - Name the constants `ADMIT_RETRY_SECONDS` (15), `POLL_BACKOFF_CAP` (60),
     `MAX_POLL_FAILURES` (20) and `LEASE_MARGIN` (300).
   - Use `relay.WAIT` for the 45, only if it is the same value.
3. **Session-gone statuses:** add `relay.SESSION_GONE = frozenset({401, 403, 409, 410})`
   for `runner.py` 44, 67, 103 and 177. **Keep** `agents.py:352`'s classification
   separate.
4. **Empty result:** add `turns.empty_result(outcome)` for `runner.py:95` and `agents.py:308`.

Extracting a `PollBackoff` object is optional. Do it only if the backoff logic reads more
clearly as an object than as three lines.

**Tests first:** a table-driven test over every
`(stage, outcome, termination_reason, session?)` combination `decode_result` handles,
pinning the exception type and exact message on the old code.

**Commit message:**

```text
refactor(engine): simplify turn decoding and name executor constants
```

## 10c: Git environment and command helpers

**Scope:** `engine/mirror.py`, `engine/turns.py`, `engine/repository.py`, possibly a new
stdlib-only leaf module, tests.

1. **Environment builders:** the three copies (`mirror.git_environment` 21–25,
   `turns.git_environment` / `agent_environment` 40–60, `repository.git_environment`
   39–46) share the `GIT_CONFIG_NOSYSTEM` / `GLOBAL` / `TERMINAL_PROMPT` / `safe.directory`
   keys. Add a base `isolated_git_env(**overrides)`. Each caller's resulting dict must be
   **identical** to today's.
2. **Checked-git wrappers:** turn the three copies (`turns.extract.git` 73–79,
   `repository.snapshot.git` 112–117, `Mirror.git` / `initialize` 47–67) into one
   `run_git(...)`. Do this only if their error handling and timeouts can be expressed
   with at most two parameters. Otherwise share only the environment, and say so.
3. **Placement:** the shared code lives where `repository.py` can import it under its
   constraints.

**Tests first:** pin each caller's git-environment dict and a representative argv and
error for each wrapper.

**Commit message:**

```text
refactor(engine): share isolated git environment construction
```

## 10d: Agent entry and session lock

**Scope:** `engine/agent_entry.py`, `engine/session_entry.py`, `engine/turns.py`,
`engine/model_config.py`, tests.

1. **One `DEFAULT_EFFORT` mapping** for the "medium for claude, otherwise low" rule
   (`agent_entry.py:35, 69`, `model_config.py:136`).
2. **One `KEY_VARIABLE` definition,** replacing `turns.py:24` and `agent_entry.py:13`.
3. **`SessionTurn` as a context manager:**
   - Give it `__enter__` / `__exit__`, and use `with` in `agent_entry` instead of manual
     `close()` on success paths only.
   - Define the attributes that `load()` creates in `__init__`.
   - The OS releases the lock at process exit. First confirm that no path relies on the
     lock staying held after an exception, before the process exits.
4. **No provider strategy classes.** The `claude`/`codex` branches in `command_for`,
   `provider_environment` and `main` are small. Leave them as conditionals.

**Tests first:** a test that the session lock is released after a successful turn and
after a failed one. Also check that the lock is still held for the whole time the turn
runs.

**Commit message:**

```text
refactor(engine): manage the session lock with a context manager
```
