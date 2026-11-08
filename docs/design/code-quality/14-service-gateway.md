# 14: Service gateway

Part of the [code-quality programme](README.md). This is **security-critical code**: the
gateway decides which GitHub mutations the engine may make.

## Goal

Split `service/gateway.py` (590 lines). Today it holds four things: the allow-list
admission policy, response settlement and diagnostics, `MutationGateway` (ownership,
pacing, read-binding checks) and `GatewayGitHub`. This iteration separates them and removes
the duplication inside.

## Scope

- `service/gateway.py`
- New `service/admission.py` and `service/settlement.py`
- `service/worker.py`, for the ownership predicate only
- Tests

## Changes

1. **Admission module.** Move the pure allow-list functions (about 27–210) into
   `service/admission.py`. Inside it, deduplicate:
   - `_ref_create_body(body, branch)`: the create-ref body check, identical at 106–109 and
     146–149.
   - `_graphql_input(body, query, field)`: the `graphql` admission branch at 93–94 and
     132–133.
   - The PATCH-ref check at 112 has **no** OID check; the one at 150–152 does. Keep this
     difference. If a shared helper is used, it takes an explicit `require_oid` flag.
2. **Settlement module.** Move `settle`, `missing_tree` and `rejection` (about 212–310)
   into `service/settlement.py`.
   - Merge `ready_pull` and `resolved_thread` (185–204) into one parser, parameterised
     by field.
   - `settle` calls `ready_pull` twice (234–236); call it once.
3. **Ownership predicate.** Add `Ownership.owns(generation)` for
   `not owner or not owner.live or owner.generation != generation`. It appears at
   `gateway.py` 418, 432 and 528, and as a variant in `worker.py:311`.
   - Add `_require_owner(key, generation, message)`. The messages differ between sites;
     pass each one in.
   - Deduplicate the stop replay `if (key, generation) in self.stops: self.halt(...)` at
     415–416 and 525–526.
4. **Read-binding checks.** Add `_node(repository, query, id)` for the two copies of
   "fetch node, compare `nameWithOwner`" in `bound()` (376–392).
5. **Simpler expressions.** Rewrite the ternary inside the `or` chain (505–508) as a
   named predicate or early return.
6. **Keep `MutationGateway` and `GatewayGitHub` importable from `service.gateway`.** Other
   modules and tests import them from there.

## Must not change

- **Admission decisions,** for every request shape.
- **Settlement results and diagnostic text.**
- **Ownership and stop semantics,** and their messages.
- **The PATCH-ref check difference.**
- **GraphQL documents admitted.** Keep the exact match against the engine's list.
- **Process-lifetime state:** `stops`, `sections` and `pulls` keep their current growth
  behaviour. Bounding them is a behaviour change and out of scope; mention it in the
  summary only.

## Tests first

- **Admission table:** an admission decision table over the existing allow-list cases.
  Add explicit cases for:
  - a PATCH-ref without an OID (allowed exactly as today);
  - a create-ref with a wrong branch;
  - a GraphQL mutation not in the list;
  - each read-binding mismatch.

  Pin the results on the old code.
- `test_gateway.py` and `test_destination_gateway.py` must pass unchanged.

## Done when

- `gateway.py` holds `MutationGateway` and `GatewayGitHub`.
- Admission and settlement are pure modules.
- Each listed duplicate has one implementation.
- The admission table passes before and after.
- All suites and validation commands are green.

## Commit message

```text
refactor(service): split gateway admission and settlement
```
