# Design review validation — 2026-10-31

Recorded in `cylewitruk/stacks-core-autobot` before the engine moved to this
repository. Image and bundle identities below refer to that layout; images built
from this repository have different identities.

The design-validation change passed 200 regression tests, including four Docker
integrations: repository inspection, retained-session storage, a Rust nextest
failure/repair cycle, and complete-candidate scope/text checks. Workflow lint,
Python syntax, configuration, bundle and skill validation also passed.

Two fresh-session runs used `claude-opus-5-5` through the normal Claude Code
transport and isolated repository tools. Each run reviewed the same four small
fixtures without expected labels or peer findings. No GitHub token was supplied;
neither run advanced a pilot issue.

| Fixture | Expected | Initial run | Calibrated run |
| ---- | ---- | ---- | ---- |
| Accurate supervised workflow | Approve | Revise | Approve |
| Incorrect automatic intake | Revise | Revise | Revise |
| Incorrect automatic merging | Revise | Revise | Revise |
| Incorrect local commit/check order | Revise | Revise | Revise |

The initial false alarm demanded literal-string assertions for prose already
covered by a source-comparison review. The shared skill now distinguishes optional
improvements from missing acceptance guarantees. The repeat caught all three
defects without rejecting the valid plan. Both runs returned valid evidence
contracts and exact source citations. This is a small calibration smoke test on
known fixtures, not an independent estimate of general review accuracy. Codex and
Copilot were covered by transport/contract tests, not this live benchmark.

Initial calibration identities:

- Agent image: `sha256:dad0a1f6550f89cf350f6ddc923f37384692f0307ab2a7c4b4bbdbcb5c9a9a58`.
- Execution image: `sha256:944160fb3669b61a680fdbcbe7d2372ffafc8844827b08988dcc9d3c74830504`.
- Instruction bundle: `31049f53a19a26270960c51a49be38a58d491819e3f8942f7b8703e29632e825`.

Reproduce with the commands in [Local checks](concurro.md#local-checks). Benchmark
JSON records the model, image, bundle and fixture digest for each response. The
pilot's deployed image pins were not changed, issue #10 was not resumed, and
`merge_enabled` remains false. A changed bundle requires reconciliation before
an existing checkpoint can advance; this change does not migrate its approvals.

## Citation and review-recovery follow-up

Claude's review exposed coverage gaps in that initial benchmark: its two criteria
and small unindented source did not exercise realistic report size or citation
fragments. The updated benchmark has 13 substantive criteria, a courtesy comment,
and a 162,350-byte source containing indentation. Each flawed plan changes one
claim from the valid plan; expected labels remain outside model context.

| Fixture | Opus 5.5 decision | Assessments | Serialized report bytes |
| ---- | ---- | ---- | ---- |
| Accurate supervised workflow | Approve | 13 | 4,978 |
| Incorrect automatic intake | Revise | 13 | 5,627 |
| Incorrect automatic merging | Revise | 13 | 5,916 |
| Incorrect local commit/check order | Revise | 13 | 5,946 |

All four responses satisfied the bounded evidence contract. These are fresh
smoke tests, not a general accuracy estimate. Offline regressions additionally
exercise reports above the old 12 KB cap, 32-criterion consensus, schema and
Unicode byte limits, checkpoint overflow, revision/exhaustion transitions, wrong
citations and bounded repairs. Every measured checkpoint stays within 60 KB.

The final suite passed **212 tests with no skips**, including all four Docker
integrations. Workflow lint, syntax, configuration and skill validation passed.

- Agent image: `sha256:dc0207040ce438b7f0ba6afdbad5dbdafcc5e0998c0b2d189f9e5daf23ab9052`.
- Execution image: `sha256:aea41ac3c9ad8c047dfe392ab2dc66d38a684ccceb54a63a0a50a1bbf0ee6ec5`.
- Instruction bundle: `d53bceb41cc0c537b53c826c27a5db235b74b94a1b9d1ce4e6f25a2cc289bd6a`.

This follow-up made no pilot or GitHub changes. Opus 5.5 remains configured;
autonomous merging remains disabled and issue #10 remains paused.
