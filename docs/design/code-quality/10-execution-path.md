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

Re-checked against `1f49de3`. Find code by symbol.

**Scope:** `engine/turns.py`, `engine/processes.py`, `engine/policy.py`, tests.
`engine/repository.py` also contains a `killpg` block. **Leave it alone**, because of
`repository.py`'s import constraints.

1. **Split `turns.execute`** (about 119 lines). Split it only where the named steps read
   more clearly:
   - **`_validated_timeout(request, timeout)`**, for the timeout validation at the top.
   - **`_run_agent(payload, env, stopped, timeout) -> (code, stdout)`**, which owns the
     process group. Keep this order exactly:
     1. start the cancel thread if `stopped` is given;
     2. `communicate` with `timeout + 60` (or `None`), where `TimeoutExpired` gives
        `TIMEOUT_EXIT` and `""`;
     3. in `finally`: kill the process group, `process.wait()`, join the timer (2 s),
        then `processes.finish(baseline)`. If `finish` raises, set `stopped` and
        re-raise.

     Move the function-local `import threading` to the top of the module.
   - **`_termination(code, stopped)`**, which returns the `termination_reason`. Turn the
     nested ternary into an `if` chain with the same precedence: `runner_stopped`
     (stopped is set), then `turn_timeout` (`TIMEOUT_EXIT`), then `process_signal`.
   - **Keep the evaluation-fixture swap inline**, and keep its `from design_eval import
     fixture_mirror` lazy: `design_eval` pulls in `design`, `mirror` and `policy`, and is
     needed only for evaluation runs. Add a one-line comment saying so.
2. **Named exit codes:** move `(-2, -15, 130, 143)` into `policy.SIGNALLED`, next to
   `KILLED`. The runner-bound termination check becomes `code in (*KILLED, *SIGNALLED)`.
   The membership must be unchanged.
3. **Process-group kill:** add `processes.kill_group(pid)` for the two identical
   `os.killpg(…, SIGKILL)` / `ProcessLookupError` blocks in `turns.execute`: the cancel
   thread and the `finally`.
4. **Named `prctl` option:** `PR_SET_CHILD_SUBREAPER = 36` in `processes.py`, replacing
   the literal and its trailing comment.

**Linux-only path.** `processes.begin()` returns `None` off Linux. The subreaper and
descendant-reaping paths therefore run only in CI (ubuntu), not in local macOS runs, and
the CI run is the real check for this iteration. Tests must pass on both platforms. Don't
skip them on macOS unless they already are skipped.

**Tests first:**

- `test_runner_termination.py` and `test_turns.py` exercise the real subprocess paths;
  they must pass unchanged.
- Add a test that pins the `(outcome, termination_reason)` pair, and whether
  `elapsed_seconds` is present, for:
  - a normal exit;
  - `TIMEOUT_EXIT`;
  - each killed and signalled code, for runner-bound and non-runner-bound stages;
  - a stopped runner;
  - `processes.finish` raising, which must set `stopped` and propagate.
- Use `tempfile`; no host paths.

**Commit message:**

```text
refactor(engine): split native turn execution and share process cleanup
```

## 10b: Result decoding and the runner executor

**Scope:** `engine/agents.py`, `engine/relay.py`, `engine/runner.py`, `engine/turns.py`, tests.
If the optional vocabulary (item 5) is included, also `engine/vocab.py` and
`engine/review_timing.py`.

Line numbers below are from `f1225e6`; find code by symbol. Re-checked against `9d3ebfb`
before approval.

1. **`Agents.decode_result`:**
   - Move the message dicts to module constants.
   - **The failure-message suffixes are not one string.**
     - Two messages end "Inspect the exchange and runner before a writer retries; no
       automatic restart was attempted."
     - One ends "Inspect the runner before a writer retries; no automatic restart was
       attempted."
     - The lost-runner message in `RunnerExecutor.receive` ends "The cause is unconfirmed.
       Inspect the runner before a writer retries." It has no restart clause.

     **Preferred:** name the two exact full suffix strings as constants and leave the
     outlier inline. Don't add a configurable factory.
   - Derive `"30-minute"` from `QUEUE_SECONDS // 60`, producing the same text.
2. **`RunnerExecutor`:**
   - **`job_bound`:** cache it on `TurnHandle`, and use it **only where a handle exists**.
     `runner_bound(...)` is also evaluated during preparation and decoding. Don't reshape
     those APIs just to eliminate every call. This is about lifecycle clarity, not
     performance.
   - **Name constants by purpose:** `POLL_BACKOFF_CAP` (60), `MAX_POLL_FAILURES` (20),
     `LEASE_MARGIN` (300) and the default `6 * 3600` lease.
     - Three separate 15-second intervals share a value but not a meaning: the admission
       retry sleep, the polling backoff base and non-job poll delay, and the
       ownership-check interval in `run()`. Give each its own name, or leave it inline.
       Never share one constant between them.
   - **`relay.WAIT`:** replace only `run()`'s `self.observe(handle, wait=45)` argument with
     it. `observe()` keeps its `wait=0` default for parallel polling.
   - **Backoff:** leave the short backoff calculation inline. A `PollBackoff` object is
     not needed.
3. **Session-gone statuses:** add `relay.SESSION_GONE = frozenset({401, 403, 409, 410})`
   for the four checks in `runner.py`. **Keep** the polling-error classification in
   `RunnerExecutor.observe` separate: it treats 3xx differently.
4. **Empty result: leave both inline.** `RunnerExecutor.lost()` and `runner.Loop`'s
   failure result build the same empty result **with a different key order**
   (`retained, workspace` vs `workspace, retained`), and the runner's dict is serialised to
   the exchange. The JSON-byte test pins both.
5. **Optional vocabulary:** if it makes the lifecycle clearer, add `ExchangeStatus`
   (`queued`, `running`, `done`, `lost`, `cancelled`) and `TerminationReason`
   (`turn_timeout`, `runner_stopped`, `process_signal`) to `vocab.py`, next to
   `TurnOutcome`. Compare against members; don't coerce incoming strings.
   `review_timing.STATUSES` stays importable with identical contents.

**Tests first**, against the current code:

- **Decoding table:** a table-driven test over every
  `(stage, outcome, termination_reason, session?)` combination `decode_result` handles.
  Pin the exception type and exact message. Include unknown outcomes and unknown
  termination reasons.
- **Executor messages:** pin the exact `RunnerTerminated` messages raised in
  `RunnerExecutor.receive` and `observe`, including the HTTP-refusal message.
- **Empty results:** pin the exact JSON bytes of the runner's failure result and of `lost()`.
- **Backoff and leases:** pin the poll backoff sequence for a job-bound turn
  (15/30/60/60 seconds), and the lease arithmetic.

**Commit message:**

```text
refactor(engine): simplify turn decoding and name executor constants
```

## 10c: Git environment helpers

Re-checked against `a24bbf1`. Find code by symbol.

**Scope:**
- `engine/mirror.py`, `engine/turns.py`, `engine/repository.py`
- One new stdlib-only leaf module, for example `engine/git_env.py`
- Tests

**Goal:** name the git isolation the engine relies on, so the four environment builders
can't drift apart.

1. **Shared isolation keys.** All four builders set `GIT_CONFIG_NOSYSTEM="1"`,
   `GIT_CONFIG_GLOBAL=os.devnull` and `GIT_TERMINAL_PROMPT="0"`. The four builders are
   `mirror.git_environment`, `turns.git_environment`, `turns.agent_environment` and
   `repository.git_environment`. Name those three keys once, for example as a
   `GIT_ISOLATION` mapping or function.
2. **Shared base for the git-only builders.** The three git-only builders also set
   `PATH` (from the environment, defaulting to `/usr/bin:/bin`), `HOME`
   (`tempfile.gettempdir()`) and `LC_ALL="C"`. Give them one base, for example
   `isolated_git_env(**extra)`. Each builder adds its own keys on top:
   - `mirror`: `GIT_ASKPASS`;
   - `turns.git_environment`: `GIT_DIR`, `GIT_WORK_TREE` and `GIT_INDEX_FILE`;
   - `repository`: `GIT_DIR`, `GIT_WORK_TREE`, pathspec, pager and optional-lock
     settings.
3. **`safe.directory`.** `turns.git_environment`, `turns.agent_environment` and
   `repository.git_environment` set the same three keys (`GIT_CONFIG_COUNT=1`,
   `GIT_CONFIG_KEY_0=safe.directory`, `GIT_CONFIG_VALUE_0=…`), with different values: `*`
   for the turns builders, the snapshot root for `repository`. Add a
   `safe_directory(value)` helper.
4. **`agent_environment` keeps its own contract.** It inherits `os.environ` minus the
   excluded prefixes, then adds the provider homes. It uses only items 1 and 3. Don't
   give it the git-only base.
5. **Placement.** The new module must be stdlib-only, so `repository.py` can import it
   in-process, and as a script under `python -I` after its `__main__` path bootstrap.
   This is the same arrangement as `protocol`.
6. **Each builder's resulting dict must be identical** to today's: the same keys and the
   same values. Key order doesn't matter to `subprocess`, but keep it where convenient.

**Dropped from this iteration: a shared `run_git`.** The four git runners differ in
arguments (`-c` hardening flags), input (stdin), extra environment, failure handling
(none, `TurnError`, `ValueError`, and paged output with exit codes) and return type. A
shared runner would need several parameters.

**Tests first**, against the current code:

- **Builder dicts:** pin each builder's exact dict for representative arguments. For
  `agent_environment`, patch `os.environ` to a controlled mapping that includes excluded
  keys (`GITHUB_TOKEN`, `GH_TOKEN`, `OPENAI_API_KEY`, …), and confirm they are dropped.
- **Script mode:** `repository.py` still imports and runs under `python -I` from a
  `tempfile` working directory. Extend the existing `test_protocol` script tests if that
  is simplest.
- Use no host paths.

**Commit message:**

```text
refactor(engine): share isolated git environment construction
```

## 10d: Agent entry and session lock

Re-checked against `337f042`. Find code by symbol.

**Scope:** `engine/agent_entry.py`, `engine/session_entry.py`, `engine/turns.py`,
`engine/model_config.py`, `engine/policy.py`, tests.

1. **One effort default.** The rule "medium for claude, otherwise low" is written in
   `agent_entry.command_for` and `model_config`'s legacy resolution. The Claude-only
   `request.get("effort", "medium")` in `agent_entry.provider_environment` is the same
   rule.
   - Add `model_config.default_effort(provider)`; `model_config` is stdlib-only, so
     `agent_entry` can import it.
   - Use it at all three sites. Every resolved effort must stay identical.
2. **One `KEY_VARIABLE`.** `"CONCURRO_GATEWAY_KEY"` is defined in both `turns.py` and
   `agent_entry.py`. Define it once, in a module both already import (`policy`), and keep
   both module-level names importable.
3. **Release the session lock on every path.**
   - **Today:** `SessionTurn.__init__` takes an exclusive `flock`, and `agent_entry.main`
     releases it only on the cached-response and success paths. On a provider failure,
     the lock is held until the agent process exits, which happens immediately after
     `run()` maps the error to an exit code.
   - **Change:** give `SessionTurn` `__enter__`/`__exit__`, or use `try`/`finally` in
     `main`, whichever reads better. A `with` block around the roughly 40-line provider
     section, plus `contextlib.nullcontext()` for session-less turns, may read worse than
     `try`/`finally`.
   - **Hard constraint:** the release path only closes the lock. It must **never write
     the journal**. A failed turn must leave the `pending` entry exactly as it is today,
     because that is how recovery detects an interrupted turn.
   - Optionally initialise the attributes that `load()` sets (`previous`,
     `request_digest`, `cached`, `identifier`) in `__init__`, if that helps readability or
     pyright.
4. **No provider strategy classes.** The `claude`/`codex` branches in `command_for`,
   `provider_environment` and `main` stay as conditionals.

**Tests first**, against the current code:

- **Effort table:** the effort resolved by `command_for`, `provider_environment` and
  legacy model resolution for `claude` and `codex`, with and without an explicit effort.
- **Session journal and lock:**
  - **cached response:** the response is printed and the lock released;
  - **success:** the journal is `complete` and the lock released;
  - **provider failure:** the journal stays `pending` with unchanged bytes, and the lock
    is releasable afterwards;
  - **lock contention:** while a turn holds the lock, a second `SessionTurn` on the same
    root fails.
- Use `tempfile` directories; no host paths.

**Commit message:**

```text
refactor(engine): share agent defaults and release the session lock on every path
```
