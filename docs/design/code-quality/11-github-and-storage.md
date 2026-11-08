# 11: GitHub I/O and storage helpers

Part of the [code-quality programme](README.md).

## Goal

Remove duplication in GitHub request plumbing, GraphQL, pagination, and checkpoint and
report storage.

## Scope

- `engine/github.py`, `engine/checkpoints.py`, `engine/checkpoint_health.py`,
  `engine/report_store.py`, `engine/markup.py`
- `service/reconciler.py`, `service/transport.py`, `service/identity.py`, `service/cutover.py`
- Tests

## Changes

### 1. `engine/github.py`

- **Thread pagination:** `review_threads()` and `threads()` (147–213) are the same
  paginated GraphQL query, apart from the node fields and the mapping. Extract
  `_thread_nodes(number, fields)`.
- **GraphQL calls:** add `graphql(query, variables)` for the "post, check `errors`, dig into
  `data`" pattern in `mark_ready()` and `resolve_thread()`.
  - The gateway admits exactly the GraphQL documents listed in `github.py`. The document
    **text** sent must stay byte-identical. Build only the plumbing around it.
- **Labels:** `_add_label(name)` / `_remove_label(name)` for the add/remove logic repeated
  in `sync_stage()` and `sync_attention()` (87–115).

### 2. One paginator, with each caller's bounds

`service/reconciler.pages` (17–29) and `engine/github.GitHub.pages` (117–128) share the
same loop, but their bounds differ:

| Caller | Page size | Maximum pages |
| ---- | ---- | ---- |
| `GitHub.pages`, paths ending in `/artifacts` | 10 | 1,000 (`10000 // 10`) |
| `GitHub.pages`, all other paths | 100 | 100 (`10000 // 100`) |
| `reconciler.pages` | 100 | 100 (`PAGE_LIMIT`) |

- Add one free function to the engine,
  `paginate(fetch, path, *, key=None, page_size, max_pages)`. Each caller passes its own
  bounds.
- `GitHub.pages` keeps choosing `page_size` from the path exactly as it does today.
- Request URLs, including separator handling, and the error raised when the bound is
  exceeded must stay identical.
- If one function would need more than these two parameters to cover both callers, leave
  the two loops separate.

### 3. Service GitHub request plumbing

- **`transport.headers(token=None)`:** the Accept, API-version and User-Agent headers are
  built three times (`transport.py:155-167`, `identity.py:78-83`, `cutover.py:43-44`).
  Do the same for JSON body encoding, with `transport.encode(data)`.
- **`transport.StaticCredential`:** replaces `cutover.Token` (28–38), which re-implements
  the credential protocol.

### 4. `engine/checkpoints.py`

- **`ARTIFACT_KINDS`:** a `{"generic": (AUDIT_NAME, AUDIT_MARKER), "plan": (...)}` table for
  choosing the name and marker by kind, repeated at 107, 138 and 338.
- **Named predicates:**
  - `_has_plan_view(state)` (297, 373);
  - `_plan_stale(state)` (298, 376);
  - `_attributed(artifact, latest)`.

  Use them to make the 7–10-term chains in `read()` (102–134) and the `dirty` expression
  (297–299) readable.
- **Split `load()`** (240–305) into `_parse_comment`, `_locate_audit` and `_locate_plan`.
- **`_sanitized(code)` context manager:** replaces the Transient/sanitise wrapper shared by
  `request()` and `collection()` (84–100). The broad `except` it contains stays broad.

### 5. `engine/report_store.py`

- **`ARCHIVE`:** compile the archive regex once (it is duplicated at 231 and 329).
- **Event marker:** use the shared event-marker constant from iteration 05.
- **`core(value)`:** replaces the six "strip `ANNOTATIONS`" dict comprehensions.
- **`graft(root, path, value)`:** "walk the path, require `None` at the leaf, assign" is
  duplicated with `checkpoints.decode` (`checkpoints.py:202-216` vs `report_store.py:362-368`).
  Use one implementation; keep both error messages if they differ.
- **Split `put()`** (about 65 lines) into `_archive_index()`, `_footer(refs)` and
  `_write_parts()`.

### 6. Ownership filter and fences

- **`owned(items, author, prefix, key)`:** the
  `x.get("user"/"creator", {}).get("login") == author and body.startswith(MARKER)` filter
  repeated in `checkpoints.py:142, 242`, `checkpoint_health.py:27` and
  `report_store.py:191, 228`. The key differs (`user` vs `creator`), so pass it in.
- **`markup.fence(text)`:** the Markdown fence-length calculation repeated in
  `checkpoints.py:314`, `report_store.py:267` and `conversation.py:291-293`.

## Must not change

- **Request method, path and query string,** headers (names and values), and body bytes
  for every GitHub call.
- **GraphQL document text.**
- **Artifact bodies, markers, fences,** and the JSON-in-Markdown encoding.
- **Checkpoint load and selection results,** including which comment or artifact wins
  when there are several candidates.

## Tests first

- **Request recording:** a recording-transport test for `GitHub.pages`,
  `reconciler.pages`, `threads`, `review_threads`, `mark_ready` and `resolve_thread`.
  Pin the exact request sequence (method, path, body) against the old code.
- **Artifact history beyond 100 pages:** an artifact-listing test with more than 100
  pages of 10 artifacts. Loading must reach the last page, so report history isn't cut
  short. Add a separate test that each caller raises at its own bound.
- **Round trips:** `checkpoints` and `report_store` tests that encode, then decode, a
  representative state and a multi-part report, and pin the exact artifact bodies.

## Done when

- Each listed duplicate has one implementation.
- Recorded request sequences and artifact bodies are identical before and after.
- All suites and validation commands are green.

## Commit message

```text
refactor: share GitHub request, pagination and storage helpers
```
