# 09: Review, conversation and policy modules (09a, 09b)

Part of the [code-quality programme](README.md).

This brief covers **two separate commits**, each reassessed and approved on its own:

- **09a, policy and imports:** see "09a (refreshed)" below. It **replaces** changes 1 and
  4 and the `nonblank` / `reviewer_identity` items in change 8.
- **09b, conversation and review helpers:** see "09b (refreshed)" below. It **replaces**
  changes 2, 3, 5, 6 and 7 and the remaining change-8 items.

The shared protocol constants from iteration 05 already exist (`engine/protocol.py`).
09b's marker changes use them; don't create a second marker definition.

## 09a (refreshed)

Re-checked against `4391431`. Find code by symbol.

### Goal

Remove the hidden import cycles between `policy`, `classification` and `reviews`, and the
genuinely shared parts of the two review validators.

### Scope

- `engine/policy.py`, `engine/classification.py`, `engine/design.py`
- One small stdlib-only leaf module
- Tests

### Changes

1. **Break the `policy` → `classification` edge.**
   - `policy` imports `classification` only for `classification.core()`, a
     dependency-free projection that drops the advisory `classification` field.
   - Move `core` into a stdlib-only leaf module, and have `policy` import it from there.
     `classification.core` stays importable as a re-export, because other modules and
     tests use it.
   - Then move `classification`'s function-local imports to the top of the module:
     `policy` (in `binding` and `candidates`), `reviews` (in `make_room`) and
     `model_config` (in `candidates`). `model_config` has no engine dependencies, so it
     never needed to be local.
   - Confirm with a fresh interpreter that `import policy`, `import classification`,
     `import reviews` and `import design` each succeed on their own, in any order.
2. **Keep the one real cycle explicit.** `policy.consensus_valid` imports `design` inside
   the function, and `design` imports `policy`. Leave that import local, with a one-line
   comment naming the cycle. Don't move `consensus_valid` in this iteration.
3. **Out of scope:**
   - the lazy imports in `repository.snapshot`, which `repository.py`'s import
     constraints require;
   - the lazy imports in `turns.execute`, which belong to 10a.
4. **Review validators: only the genuinely shared parts.**
   - `validate_review` and `validate_pr_review` share the verdict vocabulary
     (`approve`, `changes_requested`, `reopen`), the "invalid review verdict" check, and
     the "review verdict contradicts design issue" check.
   - Name the vocabulary once, for example `REVIEW_VERDICTS`. Use it in both validators,
     and in `markup.VERDICT` if its keys are the same set.
   - Extract the verdict check only if it reads more clearly.
   - **Don't** share the reopen branches. Their messages differ ("cannot also request
     implementation fixes" vs "cannot also report or answer findings"), as do their
     size checks (bytes vs finding count) and the order of those checks.
5. **`design_roster`:** resolve each named agent once, instead of four
   `parsed.resolve(name)` calls per member. The output must stay identical.
6. **Optional:** a `nonblank(value)` helper, but only if it reads more clearly. There are
   about six `isinstance(v, str) and v.strip()` checks across `policy.py` and `design.py`.

**Dropped from this iteration:** `policy.reviewer_identity`. `design_eval`'s reviewer
identity is a different contract: roles `review` vs `plan_qc`, no `effort`, and a
different legacy ID. Sharing it would need a helper with several parameters.

### Must not change

- **Validator results:** each validator's acceptance and rejection, its check order,
  and its messages.
- **Data:** `design_roster` output, and `classification.core` output.
- **Import-time behaviour:** importing `repository.py` stays stdlib-only.

### Tests first

Write these against the current code, and keep them:

- **Validator equivalence:** for each validator, a table of invalid inputs where more than
  one rule fails. Pin the first error raised.
- **Import order:** a test that imports `policy`, `classification`, `reviews` and `design`
  in several orders, each in a fresh subprocess, using a `tempfile` working directory
  rather than a host path.
- **`design_roster`:** pin its output for a named-agent config and a legacy config.

### Commit message

```text
refactor(engine): remove policy and classification import cycles
```

## 09b (refreshed)

Re-checked against the 09a working tree (after `cec2780`). Find code by symbol; line numbers
are hints.

### Goal

Give the repeated conversation and review rendering details one definition each, where
they really are the same concept, and make `conversation.publish` easier to follow. All
rendered text, markers and IDs stay byte-identical.

### Scope

- `engine/conversation.py`, `engine/classification.py`, `engine/reviews.py`,
  `engine/readiness.py`, `engine/design_eligibility.py`
- One stdlib-only leaf location for the path walk: `review_state.py`, `report_core.py`, or
  similar.
- Tests

### Changes

1. **Checkpoint path walk.** Three copies walk `state` along a `source` key path:
   `conversation.render`, `conversation.source_value`, and the pending-event loop in
   `classification.make_room`.
   - Put `source_value` where `classification` can import it without importing
     `conversation`, because `conversation` imports `classification`.
   - Both modules call it, and `conversation.source_value` stays importable.
2. **Classification source reference.**
   `.get("classification", {}).get("source", {}).get("report")` appears three times in
   `classification.make_room` and once in `conversation`. Add `classification.source_ref(report)`.
3. **Current reports.** `classification.make_room` and `conversation.compact` both collect
   `qc` + `reviews` + design-vote reports.
   - **They differ:** `make_room` also appends `state["review_event"]`.
   - Share only the common collection, and keep that append at its call site. Order
     matters for the frozen set, so keep it.
4. **Review markers and messages in `reviews.py`:**
   - `finding_marker(id)` for the four `f"{FINDING_MARKER}{…} -->"` copies.
   - `review_marker(state, config, sequence)` for the three
     `f"{REVIEW_MARKER}{task_id(…)}:{…} -->"` copies.
   - A `CONFLICT` constant for the four "PR review publication differs from its recorded
     content; reconcile manually" messages.
   - `line_suffix(start, line)` for the shared logic in `label` and `where`.
5. **Objection IDs in `conversation.py`:**
   - `objection_prefix(round, reviewer_id, phase)` for the two
     `D{round}-{id[:8]}-{phase}` copies (`objections` and `review_body`).
   - `open_concern_ids(disposition_ids, updates)` for the two identical "open
     disposition IDs" derivations.
   - `update_label(ids, index, update)` for the two
     `ids[i] if ids else u["id"] or "New finding"` copies.
6. **`ELIDED`:** replace the literal `"… [Full text in the published issue conversation.]"`
   in `conversation.compact` with `reviews.ELIDED`. `conversation` already imports `reviews`.
7. **`readiness.ci_reasons`:**
   - **Keep-latest loops:** there are three: workflow runs, check runs and statuses.
     Extract a `latest_by(items, key, ident)` helper only if it stays at two or three
     parameters. Pass already-filtered items for the check-run loop, which skips
     superseded suites. Keep each loop's own ID lookup: `run["id"]` vs `.get("id", 0)`.
   - **`PASSING`:** a constant for the `("success", "neutral", "skipped")` tuple, used
     twice. Leave the check-run branch as it is: it treats `success` separately.
8. **`conversation.publish`** (about 82 lines):
   - Replace the per-branch `create`/`login` lambdas with small named functions, or a
     target object.
   - Make the format-4 `key`/`numbers` explicit locals, instead of re-testing
     `event.get("format") == 4`.
   - Name the format-selection expression in `queue` as `event_format(kind, details)`.
   - Extract `_format4_body`, `_target` and `_receipt` only where they read more clearly.
     Keep the order of every write and receipt.
9. **Ternaries:**
   - Rewrite the ternary used as a statement in `Publisher`
     (`self.review(item) if … else self.withdrawn(item)`) as `if`/`else`.
   - Rewrite the nested ternaries in `reviews.where`, the summary line in `reviews`, and
     `design_eligibility.prepare`'s `reason`, where an `if` chain reads more clearly.

**Dropped from this iteration: unifying the droppable-text mechanisms.** `conversation`
and `reviews` differ in three ways:
- the measure (bytes vs characters);
- the message ("comment limit" vs "GitHub's review limit");
- the behaviour: `conversation.join` removes droppable parts and appends `OMITTED` once,
  while `reviews` replaces each droppable line in place.

A shared mechanism would need all three as parameters.

### Must not change

- **Rendered text:** every comment, review, finding, disposition and archive body,
  byte for byte, including truncation points.
- **Identity text:** markers, objection IDs and labels.
- **Publication behaviour:** the order of publication writes and receipts, and
  recovery behaviour after a lost reply.
- **CI evaluation:** `ci_reasons` output and its order, including deduplication.

### Tests first

Write these against the current code, and keep them:

- **Conversation snapshots:** each event kind (`plan_summary`, `qc` intake and design,
  `review`, `review_rejection`, implementation) in both single-part and multi-part
  archive form, plus compact mode with dropped text.
- **Review publication snapshots:** a finding comment, a file-level finding, a
  disposition reply, and a conflict refusal.
- **`ci_reasons` table:** superseded suites, an empty external suite, pending vs
  unsuccessful runs, duplicate check runs and statuses (latest wins), and no success at
  all.
- **Objection IDs:** the IDs and labels for a design review with open, resolved and new
  updates.

Use `tempfile` for any filesystem need; no host paths.

### Commit message

```text
refactor(engine): share conversation and review rendering helpers
```

## Goal

Remove duplication inside and between the review, conversation, classification and policy
modules. Also replace hidden circular imports (function-local imports) with a proper leaf
module.

## Scope

- `engine/policy.py`, `engine/classification.py`, `engine/conversation.py`,
  `engine/reviews.py`, `engine/markup.py`, `engine/design.py`, `engine/design_eval.py`,
  `engine/design_eligibility.py`, `engine/readiness.py`, `engine/review_state.py`
- One new leaf module
- Tests

## Changes

### 1. Leaf module for the circular imports

Move `digest` and `Blocked` (plus any other dependency-free core that the cycle needs) into
a stdlib-only leaf module, for example `engine/core.py`. `policy` re-exports them, so
existing imports keep working. Then turn these function-local imports into top-level
imports:

- `classification.py` 111, 177–178, 269;
- `policy.consensus_valid`, which imports `design` inside a loop (185).

If a real cycle remains, keep that one import local and add a one-line comment naming the
cycle.

### 2. Checkpoint source-path walk

There are three copies: `conversation.py:46-48` (inside `render`), `conversation.py:108-113`
(`source_value`) and `classification.py:264-266`. Move `source_value` into a leaf module
(`review_state` or the new core module), and have the other two call it.

### 3. Classification source lookup and report aggregation

- **`source_ref(report)`:** `.get("classification", {}).get("source", {}).get("report")`
  is repeated at `classification.py:262/268/273/283` and `conversation.py:361`.
- **Current-reports aggregation:** duplicated at `conversation.py:519-520` and
  `classification.py:258-259`. Make it one function.

### 4. Review validators in `policy.py` (328–350 and 383–421)

Extract the shared pieces:

- `_check_verdict(review)`;
- `_check_reopen(review, lists, message)`;
- the summary/findings type checks.

Each validator keeps **its current check order** (see the README's intentional
differences). `markup.VERDICT` / `DECISION` duplicate the verdict vocabularies; take them
from one definition (`vocab` from iteration 04, or `policy`).

### 5. Markers and locations in `reviews.py`

- `line_suffix()`: the line-suffix logic in `label` (92–95) and `where` (209–210).
- `finding_marker(id)`: built at 164, 173, 422, 443.
- `review_marker(...)`: built at 228, 275, 403.
- A `CONFLICT` message constant for the "publication differs…" `Blocked` text, repeated at
  370, 382, 407, 447.

### 6. Objection IDs in `conversation.py`

- `objection_prefix(round, id, phase)`: `D{round}-{id[:8]}-{phase}`, at 118 and 546.
- `open_update_ids(...)`: `concern_ids` from `disposition_ids` × `objection_updates`, at
  70–72 and 120–121.
- `update_labels(...)`: `ids[i] if ids else u["id"] or "New finding"`, at 76 and 198.

### 7. Droppable text: one mechanism, two measures

`reviews.py` has `Droppable`, `KEPT_TEXT` and `OMITTED` (121–122, 214, 235). `conversation.py`
has `Hidden`, `Text`, `droppable` and `join` (36–37, 132–153). Move one mechanism into
`markup`, with the measure as a parameter: **characters for reviews, bytes for
conversation**. Replace the `ELIDED` literal at `conversation.py:537` with `reviews.ELIDED`,
or with the moved constant.

### 8. Smaller items

- **`readiness.ci_reasons`:** add `latest_by(items, key_fn, id_fn)` for the three
  keep-latest loops (66–69, 88–94, 103–107). Pass `id_fn` in, because the lookups differ.
  Add a `PASSING` conclusions constant (80, 86, 101).
- **Schema checks:** add `nonblank(value)` and `nonblank_list(value)` helpers for the
  roughly 10 copies of `isinstance(v, str) and v.strip()`. Use them in the long schema
  boolean chains: `policy.py` 118–122, 250–257 and 316–320; `design.py` 141–146, 159–165
  and 173–176.
- **Nested ternaries:**
  - `design_eligibility.py:49-58` and `reviews.py:235`: rewrite as if/else.
  - `reviews.py:493`: a ternary used as a statement; rewrite as if/else.
- **`policy.reviewer_identity(config, provider, stage, roles)`:** `design_eval.py:73-75, 100`
  copies the identity logic in `policy.design_roster` (142–144, 151–152). The new function
  resolves each member once; `design_roster` currently calls `parsed.resolve(name)` four
  times per member.
- **`conversation.publish`:**
  - Extract `_format4_body(...)`, `_target(event)` and `_receipt(...)`.
  - Replace the lambdas assigned in a loop with a function.
  - Make `key`/`numbers` explicit locals, instead of re-testing `format == 4` at 474.
  - Name the format-selection ternary in `queue` (355) as `event_format(kind, details)`.

## Must not change

- Validator acceptance or rejection results and the order of checks, and therefore which
  error message is raised first.
- Rendered comment and archive text, including where truncation happens. Keep characters
  vs bytes.
- Objection IDs and labels.

## Tests first

- **Validator equivalence:** for both review validators, a table of invalid inputs where
  more than one rule fails. Assert the exact error raised. Pin it on the old code.
- **Rendering snapshots:** reuse or extend the iteration 07 snapshots for conversation
  events with dropped text and multi-part links. Pin their output.

## Done when

- The function-local imports listed above are gone, or each has a one-line reason.
- Each listed duplicate has one implementation.
- All suites and validation commands are green.

## Commit message

```text
refactor(engine): deduplicate review, conversation and policy helpers
```
