# 11: GitHub I/O (11a) and storage helpers (11b)

Part of the [code-quality programme](README.md).

Re-checked against `b653d19`. Find code by symbol; line numbers are hints only.

This brief covers **two separate commits**, because network I/O and storage are unrelated
concerns. 11a comes first; **11b is reviewed and approved separately**.

## Ground rules for both

- **Requests:** the exact method, path, query string, headers (names and values), body
  bytes and request order of every GitHub call stay unchanged.
- **Storage:** artifact bodies, markers, fences, the JSON-in-Markdown encoding, and
  checkpoint load/selection results stay unchanged, including which comment or artifact
  wins when there are several candidates.
- **Errors:** error types and messages stay unchanged, and so do the broad `except` blocks
  that sanitise storage errors.

## 11a: GitHub I/O

### Scope

- `engine/github.py`
- `service/reconciler.py`, `service/transport.py`, `service/identity.py`, `service/cutover.py`
- Tests

### Changes

1. **Review-thread pagination.** `GitHub.review_threads` and `GitHub.threads` run the same
   paginated `reviewThreads` query. Its bound (100 pages), error messages and cursor
   handling are identical; they differ only in the node fields requested and how each node
   is mapped. Share the loop as `_review_thread_nodes(number, fields)`.
   - **It must be lazy** (a generator), or map within each page. `review_threads()` maps
     each page's nodes **before** requesting the next page. An eager list would fetch later
     pages before a malformed node is encountered.
   - Each caller keeps its own fields and mapping.
   - **The query text sent must stay byte-identical for each caller,** so build it from the
     same template only if the produced string is identical.
2. **Issue labels.** `sync_stage` and `sync_attention` both add a label with
   `POST issues/{n}/labels {"labels": [name]}` and remove one with
   `DELETE issues/{n}/labels/{quote(name, safe="")}`. Add `_add_label(number, name)` and
   `_remove_label(number, name)`.
3. **One paginator, with each caller's bounds.** `GitHub.pages` and `reconciler.pages`
   share the loop but **not the bounds**:

   | Caller | Page size | Maximum pages |
   | ---- | ---- | ---- |
   | `GitHub.pages`, paths ending in `/artifacts` | 10 | 1,000 |
   | `GitHub.pages`, all other paths | 100 | 100 |
   | `reconciler.pages` | 100 | 100 (`PAGE_LIMIT`) |

   Add one engine free function, `paginate(fetch, path, *, key=None, page_size, max_pages)`.
   `GitHub.pages` keeps choosing the page size with the **literal**
   `path.endswith("/artifacts")` test: a path like `…/artifacts?x=y` currently gets ordinary
   pagination, and must keep it. `reconciler.pages` passes its own bounds. Request URLs (including `?`/`&` separator handling) and the
   bound-exceeded error must stay identical. Service code may import from the engine; the
   engine must never import from the service.
4. **Service request plumbing.**
   - **Headers:** the `Accept: application/vnd.github+json`, `X-GitHub-Api-Version` and
     `User-Agent` headers are written three times (`transport`, `identity`, `cutover`). Add
     `transport.headers(token=None)`, and keep each site's exact header set, including any
     `Authorization` header and extra headers.
   - **JSON bodies:** `json.dumps(data).encode()` for request bodies appears in `transport`
     and `identity`. Share it only if the bytes stay identical.
   - **Static credential:** `cutover.Token` re-implements the transport's credential
     protocol (`get()`, `drop(token)`). Move it into `transport` as `StaticCredential`, and
     keep `cutover.Token` importable if anything uses it.
5. **Keep `mark_ready` and `resolve_thread` separate.** They don't share an order:
   - `mark_ready()` extracts the PR **first**, then checks `errors` and `isDraft is False`.
     A failed confirmation **raises**.
   - `resolve_thread()` checks `errors` **first**, then returns whether the returned ID
     matches **and** `isResolved is True`. A failed confirmation **returns `False`**.

   Any shared helper is limited to the bare POST, and each method keeps its own sequence,
   checks and messages.

### Tests first

- **Request recording.** Pin the exact request sequence (method, path, headers, body) for:
  - `GitHub.pages` on an artifact path and a non-artifact path;
  - `reconciler.pages`;
  - `review_threads` and `threads` over two pages;
  - `sync_stage` and `sync_attention` (label added, removed and unchanged);
  - `mark_ready` and `resolve_thread`.
- **Pagination bounds.** An artifact listing longer than 100 pages of 10 must reach its
  last page, and each caller must raise at its own bound.
- **Lazy thread pagination.** A mapping failure on page 1 of `review_threads` prevents the
  page-2 request.
- **GraphQL confirmation table**, for both `mark_ready` and `resolve_thread`:
  - missing or null `data`;
  - a wrong returned ID;
  - non-boolean `isDraft`/`isResolved`;
  - `errors` present together with malformed data.

  Pin which cases raise and which return `False`.
- **Pagination edges:**
  - a path with an existing query string;
  - a keyed response (`key=`);
  - a full final page exactly at the bound;
  - `…/artifacts?x=y` using ordinary pagination.
- **Service request contract.**
  - Exact headers for authenticated requests (`transport`, `identity`), and for the
    unauthenticated cutover compare and codeload redirect.
  - Request bodies: `data=None` vs empty containers, with literal JSON bytes, including
    non-ASCII text.
  - A credential failure occurs before the body is serialised, and a token is dropped
    after a 401.
  - Each request builds a **fresh** header dict, so `Authorization` can't leak between
    calls.
- Use `tempfile` for any filesystem need; no host paths.

### Commit message

```text
refactor: share GitHub pagination, label and request helpers
```

## 11b: Storage helpers

### Scope

- `engine/checkpoints.py`, `engine/checkpoint_health.py`, `engine/report_store.py`,
  `engine/conversation.py`, `engine/markup.py`
- Tests

### Changes

1. **Artifact name and marker by kind.** `checkpoints.py` chooses
   `(AUDIT_NAME, AUDIT_MARKER)` vs `(PLAN_NAME, PLAN_MARKER)` by `kind` in `read`, `discover`
   and `write`. Share the choice in one place.
   - **Preserve the fallback:** `"generic"` gives audit metadata, and **any other value**
     gives plan metadata.
   - A direct `ARTIFACT_KINDS[kind]` lookup would add a new `KeyError` path. Use an
     explicit fallback (for example `ARTIFACT_KINDS.get(kind, plan)`, or a small function),
     not new validation.
2. **Report annotations.** `report_store.py` separates annotations from report content six
   times: `{k: v … if k not in ANNOTATIONS}` four times, and `{k: v … if k in ANNOTATIONS}`
   twice. Add `core(value)` and `annotations(value)` helpers. Don't confuse them with
   `report_core.core`, which removes only `classification`. Choose names that make the
   difference clear.
3. **Archive JSON regex.** The `re.search(r"\n(`{3,})json\n(.*)\n\1$", body, re.S)` in
   `Store.put` and `Store.load` becomes one compiled pattern. Matching semantics are
   unchanged.
4. **Reference grafting.** `checkpoints.decode` and `report_store.Store.load` both walk to
   a reference's parent, refuse if the slot isn't `None`, then assign a fetched value.
   - **Errors differ:** `ValueError` in `decode`, and
     `ReportDataError("Overlapping report references.")` in `load`.
   - **Order matters:** both check the slot **before** fetching the value. In `decode`,
     the fetch is `self.reports.get(...)`, which may read GitHub.
   - A shared helper may return the parent and the final key after the check, with the
     error supplied by the caller. Each caller keeps fetching only after the check.
   - **Public errors stay as they are.** `decode()`'s internal `ValueError` surfaces as
     `CheckpointError("checkpoint_format")`, and report-fetch failures surface as
     `CheckpointError("report_data")`.
   - Keep checkpoint-only path, overlap and annotation validation **outside** the shared
     traversal helper.
5. **App-authored items.** "Authored by us and starts with a marker" filters appear in
   `Checkpoints.load` (`user`), `checkpoint_health` (`user`) and `report_store` (`creator`).
   Add `owned(items, author, prefix, author_field)`. The checks on version `actor` in
   `checkpoints` are a different contract; leave them.
6. **Block fences.** The length rule `max(3, longest run of the fence character + 1)`
   appears in `checkpoints.plan_body`, `report_store.Store.put` and twice in
   `conversation`. Add `markup.fence(text, char="`")` for the **length** rule only.
   - **Keep the selection logic at its call sites.** Rejected plan and intake output
     chooses the **shorter** of the backtick and tilde fences, preferring backticks on a
     tie. Ordinary report archives always use backticks.
   - `markup.code`'s **inline** fence (`longest + 1`, with no minimum) is a different rule
     and stays as it is.
7. **Optional splits, only where they read more clearly:**
   - `Checkpoints.load` (83 lines), split into `_parse_comment`, `_locate_audit` and
     `_locate_plan`;
   - `Checkpoints.read` (54 lines), using named predicates for its long boolean chains;
   - `Store.put` (77 lines).

   A sequence that reads clearly top to bottom may stay whole. Name any you leave.

   These existing tests are required guards for any split, and must pass unchanged:
   - **Lost write responses:** `test_lost_initial_create_response_recovers_orphan_without_reset`,
     `test_lost_plan_or_comment_response_repairs_views_without_model_calls`,
     `test_lost_binding_write_response_recovers_one_plan_and_exact_state` and
     `test_lost_creation_response_recovers_without_duplicate_parts`.
   - **Migration:** `test_legacy_migration_preserves_approved_checkpoint_before_new_state` and
     `test_interrupted_migration_keeps_legacy_and_recovers_newer_artifact`.
   - **Bound-plan recovery:** `test_bound_plan_survives_history_window_and_human_rename`,
     `test_deleted_bound_plan_is_recreated_and_rebound_without_resetting_state` and
     `test_bound_plan_cannot_be_replaced_at_the_same_number`.
   - **Immutable-report verification:**
     `test_missing_edited_replaced_and_duplicate_parts_fail_closed`.
8. **Optional: a sanitising context manager.** `Checkpoints.request` and
   `Checkpoints.collection` share a "re-raise `Transient`, otherwise raise
   `CheckpointError(code)`" wrapper. Share it only if it reads more clearly. The broad
   `except` stays broad.

### Tests first

- **Round trips:** `Checkpoints` and `Store` encode then decode a representative state and
  a multi-part report. Pin the exact artifact bodies.
- **Selection and rejection.** Duplicate authoritative comments or artifacts are
  **rejected**, not ranked. The report archive index takes the **first** matching digest in
  collection order. Pin both behaviours.
  - Distinguish permitted human edits to generated plans (repaired) from forbidden edits to
    the audit or reports (refused).
  - Keep the separate `user`, `creator` and version-`actor` authorship checks.
- **Public grafting errors:** `CheckpointError("checkpoint_format")` and
  `CheckpointError("report_data")`.
- **Grafting:** overlapping references raise each caller's own error, and no fetch happens
  before the slot check.
- **Fences:**
  - pin fence lengths for runs of 0, 2, 3 and 5 of the fence character;
  - pin the backtick/tilde **selection** for rejected output, including ties (backticks
    win);
  - pin that report archives always use backticks.

  Use literal rendered bodies, not the new helper.
- Use `tempfile` for any filesystem need; no host paths.

### Commit message

```text
refactor(engine): share checkpoint and report storage helpers
```
