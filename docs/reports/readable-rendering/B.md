# Presentation preview

Generated offline from current code and simulated fixtures. No live GitHub evidence.

Regenerate with `python engine/tests/render_readable_previews.py`; add `--check` to detect stale previews.

<!-- concurro:v1 -->
## Concurro

> [!TIP]
> Ready for your review. A human decides whether to merge.

Intake ✅ → Plan ✅ → Design review ✅ → Implement ✅ → Code review ✅ → **Handoff Ready**

| Step | Status | Detail |
| ---- | ---- | ---- |
| Intake | ✅ Proceed | Scope and acceptance criteria are sufficient. |
| ↳ Round 1 |  | [codex: ✅ Proceed](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/4) |
| Plan | ✅ Version 1 | [Implementation plan](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/5) |
| Design review | ✅ 2 of 2 approve | Round 1 · iterative · 0 open objections |
| ↳ Round 1 iterative |  | [claude: ✅ Approve](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/8)<br>[codex: ✅ Approve](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/10) |
| Implement | ✅ Candidate published | Agent-reported validation; independent reviews and CI follow |
| ↳ Round 0 |  | [codex: 📝 Agent report](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/12) |
| Code review | ✅ 2 of 2 approve | [PR #9](https://github.com/cylewitruk/stacks-core-autobot/pull/9) · 0 open findings |
| ↳ Round 1 |  | [claude: ✅ Approve](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/15)<br>[codex: ✅ Approve](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/17) |
| Handoff | Ready for human review | A human reviews and merges |

| Estimate | Assessment | Evidence |
| ---- | ---- | ---- |
| Complexity: medium · Change risk: high | Confidence: high · PR assessment · reviewers disagree | [Report 17](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/17) · [Report 15](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/15) |

**Action needed:** a human review and merge ([details](https://github.com/cylewitruk/stacks-core-autobot/issues/7#issuecomment-20)).

<details><summary>Diagnostics</summary>

Readiness: CI passed and all Concurro reviewers approved. Ready for human review.

[Automation audit state](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/1) (public).

</details>

<!-- concurro:checkpoint:v1 {"digest":"0000000000000000000000000000000000000000000000000000000000000000","id":1001,"number":1,"revision":49} -->


<!-- concurro:review:v1:o/r#7:2 -->
### 🛠️ Concurro review · claude · Changes requested

Two defects remain; one earlier finding is fixed.

| Severity | Finding | Where |
| ---- | ---- | ---- |
| ❗ High | **F3** Missing \*changelog\* \[fragment\]\(x\) | general |
| ℹ️ Low | **F2** Wording | `docs/guide.md:3-4` |

**Earlier findings:** [F1](https://github.com/o/r/pull/9#discussion_r101) ✅ Fixed

<details><summary>Full review</summary>

The guide is close.

&lt;!-- unclosed

</details>

<details><summary>Findings without a file</summary>

**F3 · Missing \*changelog\* \[fragment\]\(x\)**

No fragment.

```
</details>
```

</details>

<sub>claude / m</sub>

<details><summary>Diagnostics</summary>

Reviewed commit `dddddddddddddddddddddddddddddddddddddddd` · review 2

</details>

<!-- concurro:finding:v1:F2 -->
ℹ️ **Concurro · Low · F2 · Wording**

The term is undefined. Define it before use.

<sub>Concurro review · claude</sub>

<!-- concurro:disposition:v1:F1:2 -->
✅ **Fixed**

Lines 3-4 define it.

<details><summary>Diagnostics</summary>

in `ddddddd`

</details>

### Intake approved with concerns

Planning will continue. No action needed.

<details><summary>Planning concerns</summary>

- Consider `<limit>` behavior.

The full concerns are in the intake report linked from the dashboard.

</details>

**What happened:** The task cannot proceed.

<details><summary>Further detail</summary>

A dependency is unavailable.

</details>

**What you can do:**

Once the cause is fixed, a writer comments `/concurro retry`; a reason that asks for reconciliation needs an operator first.

<!-- concurro:plan:v1 -->
# Concurro implementation plan

Generated view; edits are overwritten. Request changes in issue comments.

Approval is recorded in the audit state; editing this document does not approve work.

Make the output readable.

## Files

- engine/markup.py

## Steps

1. Escape &lt;script&gt; tags
2. Keep code spans

## Tests

- Run renderer tests

<details><summary>Diagnostics</summary>

Plan: `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`

Base: `bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb`

</details>
