# 09: Review, conversation and policy modules

Part of the [code-quality programme](README.md).

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
