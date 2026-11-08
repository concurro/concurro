# 06: Test fixtures

Part of the [code-quality programme](README.md). **This iteration changes test code only.**

## Goal

Give tests proper shared fixtures, so later refactors can add characterisation tests
cheaply. Today, tests borrow helpers across classes and import other test modules for
their fakes.

## Scope

- `engine/tests/`
- `service/tests/`
- `gateway/tests/`
- No production code

## Changes

### 1. Shared fakes module

Create `engine/tests/fakes.py`. Move the shared fakes and constants out of
`test_pipeline.py` into it: `FakeGitHub`, `FakeAgents`, `CONFIG`, `PLAN`, and whatever else
other modules import. `test_pipeline.py` is imported 44 times.

- `test_pipeline.py` imports them back, so its own tests are unchanged.
- Update the importing modules to use `fakes`.
- Do the same for helpers imported from `test_autonomy` (9), `test_consensus` (8) and
  `test_turns` (5) where other modules reuse them.
- Service tests currently reach the engine fakes through a `sys.path` hack
  (`service/tests/fixtures.py:15`). Point them at `fakes`.

### 2. Fixture mixins

Replace borrowed `setUp`/helper attributes with mixin classes that don't subclass
`TestCase`. Today's borrowed attributes include `setUp = test_consensus.ConsensusTests.setUp`
and `tick = …`.

- `PipelineFixture`: provides `setUp`, `tick`, `reach`, `persist` and `loaded`.
- `ConsensusFixture`: extends `PipelineFixture` with the consensus-specific setup.
- `ExchangeFixture` in `gateway/tests`: replaces the direct `ExchangeTests.setUp(self)` calls
  at `test_exchange.py:250, 402`.

Known borrowing sites, about 10:

- `test_runner_termination.py:291, 344-347`;
- `test_credentials.py:28-29`;
- `test_ownership.py:57`;
- `test_outcomes.py:14`;
- `test_design_audit.py:18`;
- `test_revision_extension.py:17`;
- `test_design_eligibility.py:102`;
- `test_review_timing.py:86`.

`service/tests` already uses this pattern with `ServiceFixture`; follow it.

### 3. Shared helpers

Where the 16 copies of `tick()` and 6 copies of `reach()` are identical, replace them with
the mixin method. Where a copy differs, keep it as an override and note why in one line.
Introduce `ISSUE = 7` for the 70 uses of `controller.advance(7)`, at least where those
uses go through the shared helpers.

### 4. Optional split of `ConsensusTests`

`test_consensus.py:29-762` holds about 730 lines and 29 tests. Split it into classes by
stage only if the move is mechanical. Otherwise defer it and say so.

## Must not change

- **Test count and test names:** report `unittest` discovery counts per suite, before and
  after. If you rename a test method, list both names.
- **What tests assert:** no assertion is weakened or removed.
- **Production code:** none changes.

## Tests first

Not applicable: this iteration restructures tests only. Run each suite before you start,
and record the counts.

## Done when

- No test class assigns another class's `setUp` or helper as a class attribute.
- No test module imports another `test_*` module for fakes. Importing test cases to
  subclass them is still allowed, but prefer mixins.
- Suite counts are identical before and after, and all validation commands are green.

## Commit message

```text
test: share fixtures through mixins and a fakes module
```
