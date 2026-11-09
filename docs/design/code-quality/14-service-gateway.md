# 14: Service gateway

Part of the [code-quality programme](README.md). This is **security-critical code**: the
gateway decides which GitHub mutations the engine may make.

Re-checked against `b0b453d` (688 lines after formatting). Find code by symbol; line numbers
are hints only.

## Goal

`service/gateway.py` holds four things:

- the allow-list admission policy;
- response settlement and diagnostics;
- `MutationGateway`: ownership, pacing and read-binding checks;
- `GatewayGitHub`.

Separate the pure policy from the stateful gateway, and give each duplicated rule one
implementation, **without changing a single admission or settlement decision**.

## Scope

- `service/gateway.py`
- New `service/admission.py` and `service/settlement.py`
- `service/registry.py`, only for an `Ownership.owns()` method
- `service/worker.py`, only for the ownership check and the run-branch literals
- Tests

## Changes

1. **Admission module.** Move the pure functions into `service/admission.py`:
   - `text_blob`, `allowed`, `destination`, `allowed_target`, `runner_write`,
     `ready_pull`, `resolved_thread`;
   - the constants they use (`WRITES`, `PR_BODIES`, `DISPATCH`, `ISSUE_COMMENT`,
     `EDITABLE`, `PULL`, `BRANCH`, `OID`, `QUERY`, `NUMBER`).

   Inside the module:
   - **Create-ref body:** the check is identical in `allowed` and `allowed_target`
     (`{"ref", "sha"}`, `ref == "refs/heads/" + task branch`, `sha` an OID). Share it as
     `_create_ref_body(body, branch)`.
   - **GraphQL admission:** `method == "POST" and (resolved_thread(body) is not None or
     ready_pull(body) is not None)` appears in both. Share it.
   - **Keep the PATCH-ref difference.** `allowed`'s PATCH-ref check accepts any
     `heads/...` path, with `{"sha", "force"}` and `force is False`, and **no OID
     check**. `allowed_target`'s requires the exact task branch path **and** an OID `sha`.
     Don't merge these. A shared helper, if any, needs an explicit flag.
   - **Run-branch literals:** name `"concurro/runs/"` and `"concurro-executor.yml"` once,
     for example as `RUN_BRANCH_PREFIX` and `EXECUTOR_WORKFLOW`. They're used in
     `runner_write` (three times), `MutationGateway.dispatch_runner` and `service/worker.py`.
     The exchange keeps its own copy, since it ships as one file.
2. **Both GraphQL parsers require non-empty IDs.** The empty-thread finding retained
   during this refactor is fixed by [19c](19-recorded-findings.md#19c-refuse-an-empty-threadid).
   `ready_pull` and `resolved_thread` now return `None` for empty `pullRequestId` and
   `threadId` values, so shape admission refuses both. `bound()` makes no GitHub lookup
   for either and retains its unmatched-path fallback; it is not a second shape validator.
   Keep each parser's exact query and variable checks, and the distinct settlement
   confirmation rules.
3. **Settlement module.** Move `settle`, `missing_tree` and `rejection` into
   `service/settlement.py`, together with the exceptions `settle` raises: `Rejected` and
   `TreeUnavailable`.
   - `service.gateway` re-exports them. Importing them back from `gateway` would create a
     cycle, because `gateway` imports `settlement`.
   - `settlement` is not pure: `settle` emits the operator warning, using text built by
     `rejection`. Keep that log call, its logger and its message unchanged.
   - In `settle`, compute `ready_pull(body)` once instead of twice; that's pure, so
     behaviour is unchanged.
   - **Keep the asymmetric GraphQL confirmation.** Readiness requires the returned PR `id`
     to equal the requested ID **and** `isDraft is False`. Thread resolution requires only
     `isResolved is True`, without matching the returned thread ID.
4. **Ownership.**
   - Add `Ownership.owns(generation)` in `service/registry.py`, meaning
     `self.live and self.generation == generation`. Callers keep their `owner is None`
     check: `if not owner or not owner.owns(generation)`.
   - It replaces the predicate in `MutationGateway.check_owner`, `dispatch_runner` and
     `_send`. Each site keeps its own `OwnershipLost` message.
   - `worker.py` has a **different** predicate that also requires an unexpired lease
     (`owner.lease_until > clock`, a strict comparison). If it uses `owns()`, keep the lease
     condition explicit beside it.
   - **Expired leases don't affect the three gateway checks today;** keep it that way.
   - Deduplicate the stop replay (`if (key, generation) in self.stops: self.halt(...)`) in
     `check_owner` and `_send` **only**. `dispatch_runner` has no stop replay today; don't
     add one.
5. **Read-binding checks.** `bound()` has two near-identical "POST a GraphQL node query and
   read `data.node`" blocks: the ready-for-review PR and the review thread. Share the fetch
   as `_graphql_node(repository, query, node_id)`. Each caller keeps its own query text and
   its own checks (`type(...) is int` vs `isinstance(..., int)`: keep each as it is).
6. **`mutate` admission.** The admission expression is a ternary inside an `or` chain
   (runner session, else bound target, else issue-only). Name it as a predicate method, or
   rewrite it with early returns, with the same evaluation order.
7. **Imports stay working.** Tests and modules import `allowed`, `allowed_target`, `settle`,
   `rejection`, `Disallowed`, `Rejected`, `LEASE_SECONDS`, `TREE_RETRY_DELAYS`,
   `WRITE_INTERVAL`, `MutationGateway` and `GatewayGitHub` from `service.gateway`. Either
   re-export the moved names from `service.gateway`, or update every importer in the same
   commit.

## Must not change

- **Decisions:** every admission decision, for every method, path and body shape. That
  includes the order in which `mutate` evaluates runner, target and issue admission, and
  when `bound()` reads from GitHub.
- **Settlement:** results, error types and messages, and the operator warning that `settle`
  logs (with text from `rejection`).
- **Runner admission:** a `DELETE` of a recorded session ref is admitted for a different
  recorded generation, while `_send` still requires current ownership.
- **Ownership:** ownership and stop semantics, and their messages.
- **The documented differences:**
  - the PATCH-ref check;
  - readiness vs thread settlement confirmation;
  - `type(...) is int` vs `isinstance(..., int)` in `bound()`.
- **GraphQL:** the exact match against the engine's `MARK_READY` and `RESOLVE_THREAD`
  documents.
- **Process-lifetime state:** `stops`, `sections` and `pulls` keep their current growth
  behaviour. Bounding them is out of scope; mention it in the summary only.

## Tests first

Write these against the current code, pin the results, and keep them.

### Shape admission (`allowed`, `allowed_target`, `runner_write`)

Cover every `WRITES` entry, and at least these refusals and acceptances:

- a write to another issue;
- **blobs:** a wrong `encoding`, non-string `content`, or content containing NUL. Pin
  that a string with an unpaired surrogate **passes** today, since `text_blob` doesn't
  check that content encodes as UTF-8.
- a create-ref with a wrong branch or a non-OID `sha`;
- **PATCH-ref:** without an OID it's admitted by `allowed` and refused by
  `allowed_target`; a PATCH-ref to another branch;
- a PR review or comment body with extra fields, `event != COMMENT` or
  `subject_type != file`;
- a PR creation that isn't a draft, or where `maintainer_can_modify` is missing or not
  exactly `False`, or where `head`/`head_repo`/`base` are wrong;
- a GraphQL query that isn't one of the two documents;
- empty `pullRequestId` and `threadId` values (both refused by shape admission);
- each runner-write shape, including a wrong session, a wrong ref and extra inputs.

### Stateful admission (`MutationGateway.runner_allowed` and `mutate`'s routing)

Use a fake registry:

- **runner sessions:** a missing or removed session record; a wrong task; a wrong
  destination; a config-SHA mismatch; a generation mismatch.
- **The intentional exception:** a `DELETE` of an older session's ref is admitted for a
  different recorded generation, while `_send` still refuses without current ownership.
- **Routing order:** runner admission is checked first and short-circuits; then
  bound-target admission. For an **unbound** task, only issue and label writes are
  admitted, even where `allowed()` alone would accept a git, PR or GraphQL write.

### `bound()`

Use a fake reader, and assert the reads made, their order and short-circuiting:

- **success paths:** a ready-for-review PR, a thread resolution, an editable issue comment,
  a PR comment reply, and a non-PR write that needs no read;
- a missing binding or a missing bot identity; a wrong task branch; a fork mismatch; a PR
  not authored by the bot; a recorded-PR mismatch (number or repository);
- missing or empty GraphQL nodes;
- **finding authorship:** both accepted bot-login forms (with and without `[bot]`), and a
  first comment that isn't the App's;
- an issue-comment edit on another issue, or without an editable marker; a PR-comment reply
  in another PR;
- **boolean PR numbers:** readiness rejects `True` (`type(...) is int`); thread resolution
  accepts it (`isinstance(..., int)`). Pin both.
- **caching:** `owned_pull` reuses its cached result for the same task, repository and PR.

### Ownership and stops

- **Gateway checks ignore leases:** each of the three gateway checks behaves the same
  with an expired lease.
- **Worker cleanup is strict:** it requires `lease_until > clock`. Include
  `lease_until == clock`, which must be refused.
- **Messages:** each site raises its own distinct message.
- **First-stop replay:** a later call repeats the first recorded stop for that generation.
- **`dispatch_runner`** has no early stop replay.

### Settlement

Cover:

- an unsent response;
- 401;
- 403 throttled, and 403 not throttled;
- 408 and 429 vs other 4xx;
- 404;
- 422 with a missing tree, both as structured errors and as legacy message text, and 422
  without one;
- 204;
- 2xx with a lost body;
- 3xx redirects;
- 5xx;
- **GraphQL:** malformed JSON, a missing body, `errors` present (including `errors: []`), and
  `data` missing;
- **readiness confirmation:** a wrong PR ID, and `isDraft` true or missing;
- **thread confirmation:** `isResolved` true, false or missing, with a non-matching
  returned thread ID still accepted.

For each case, pin the status, the error type and message, and the warning that `settle`
logs.

### Existing tests and portability

- `test_gateway.py`, `test_destination_gateway.py` and `test_readiness_gateway.py` pass with
  at most import-path changes.
- Use `tempfile` for any filesystem need; no host paths.

## Done when

- `gateway.py` holds `MutationGateway` and `GatewayGitHub`.
- Admission is a pure module; settlement is policy plus its operator logging.
- Each listed duplicate has one implementation.
- The shape, stateful-admission, `bound()`, ownership and settlement tables pass before and after.
- All suites and validation commands are green, and CI is green after the push.

## Commit message

```text
refactor(service): split gateway admission and settlement
```
