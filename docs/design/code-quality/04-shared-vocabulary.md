# 04: Stage and turn-outcome vocabulary

Part of the [code-quality programme](README.md).

## Goal

Name the **meanings** behind the engine's repeated stage groupings and turn-result
strings, so a reader can see which concept each check uses.

The closed-PR display bug showed why. `current_step()` grouped
`("merged", "observed_merged", "closed")` where it meant "merged". A generic "terminal"
set would have kept that bug just as well. The value of this iteration is in **separate,
purpose-named sets**, not in removing string literals.

## Scope

- New stdlib-only leaf module `engine/vocab.py`. It must import nothing from the engine,
  so every module can use it, including `repository.py` under its import constraints.
- Engine modules that define or test these groupings.
- **Out of scope:**
  - service vocabularies and the exchange;
  - finding statuses and dashboard step labels;
  - exchange statuses and termination reasons. The turn lifecycle can adopt those in
    iteration 10b.
  - URL helpers and protocol markers (iteration 05).

## Changes

### 1. `Stage` (`StrEnum`)

Its members are every controller stage value: `intake`, `clarify`, `plan`, `plan_qc`,
`planned`, `implement`, `publish`, `review`, `gates`, `merging`, `merged`,
`observed_merged`, `closed`, `blocked`, `stopped`, `rejected`, `escalated`.

Members compare equal to their strings and serialise identically, so existing persisted
values and comparisons keep working.

### 2. `TurnOutcome` (`StrEnum`)

Its members are the turn result values: `ok`, `failed`, `uncertain`, `lost`, `timed_out`,
`terminated`, `admission_unknown`, `queue_expired`.

- **Name:** don't call it `Outcome`. `controller.Outcome` is the scheduler's tick result,
  with unrelated values.
- **No new validation:** introducing the enum must not add any failure mode. Code must not
  convert incoming or persisted strings with `TurnOutcome(value)` where an unknown value is
  currently tolerated or handled by a fallback branch. Compare against members; don't
  coerce.

### 3. Purpose-named stage sets

Audit every inline stage grouping (`grep -nE '\("(merged|blocked|rejected|stopped|closed)"' engine/*.py`)
and classify each by **what the check means**, then name only the concepts that recur.
Expected concepts, to confirm or correct during the audit:

| Concept | Members | Example use today |
| ---- | ---- | ---- |
| `MERGED_STAGES` | `merged`, `observed_merged` | `dashboard.current_step()`; PR-sync guards (`controller.py` around 1920) |
| `TERMINAL_STAGES` | `MERGED_STAGES` plus `closed` | End-of-life handling (`actions.py:75`, `controller.py` around 1936) |
| `SUSPENDED_STAGES` | `blocked`, `stopped` | Retry and resume handling |
| `JUDGED_STAGES` | `rejected`, `escalated` | Terminal judgement and retry handling |

- **Name each set for its purpose, not its members.** Two checks with the same members
  today but different purposes get different names, or one stays inline.
- **Mixed groupings stay explicit.** For example, `("merged", "observed_merged", "stopped")`
  around `controller.py:1931` and the six-stage tuple around 859 can be written as
  `MERGED_STAGES | {Stage.STOPPED}`, or left inline with a comment naming the purpose.
  Don't invent a name for a one-off grouping.
- **Not every literal has to go.** Leave a grouping inline when naming it would not make
  its meaning clearer.
- **Keep order where it matters.** Where a tuple's order is used for iteration or
  display, keep an ordered tuple.

### 4. Existing public sets

`github.STAGES`, `actions.RESUMED_STAGES` and `bundle.STAGES` stay importable under their
current names, with identical contents. `bundle.STAGES` is agent stages, not controller
stages, so leave it as it is. Make `github.STAGES` derive from `Stage` only if the set is
provably identical.

### 5. Paused-label tables

The paused-label tables in `dashboard.py` (`activity_line` and `step_row`) and in the
controller's action line overlap. Merge them only where the entries are identical for
the same purpose. They currently hold different texts per stage, so keep separate tables
where the wording differs.

## Must not change

- Any comparison result, persisted value, iteration order used for output, or digest.
- Membership semantics: for each replaced grouping, the summary states its old members, its
  new expression and why the meaning is the same.
- Acceptance of unknown or legacy stage and outcome strings.

## Tests first

- **`test_vocab.py`:**
  - each enum's values equal a literal tuple written out in the test;
  - `json.dumps({"stage": Stage.PLAN}) == '{"stage": "plan"}'`;
  - `Stage("plan") == "plan"`;
  - each named set equals its literal membership.
- **Unknown-value tolerance:** for each place where outcome decoding has a fallback branch,
  a test feeds an unrecognised outcome string. It must take the same path, raise the same
  exception and produce the same message as before.
- **Existing public sets:** `github.STAGES`, `actions.RESUMED_STAGES` and `bundle.STAGES`
  still equal their current contents.

## Done when

- `Stage`, `TurnOutcome` and the purpose-named sets exist in `vocab.py`.
- Each audited grouping is either replaced by a named concept or deliberately left inline.
  The summary lists every grouping with its classification.
- All suites and validation commands are green.

## Commit message

```text
refactor(engine): name stage groupings and turn outcomes by meaning
```
