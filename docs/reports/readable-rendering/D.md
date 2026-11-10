# Structured description preview

Generated offline from current code and simulated fixtures. No live GitHub evidence.

Regenerate with `python engine/tests/render_readable_previews.py`; add `--check` to detect stale previews.

## Issue dashboard

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
| ↳ Round 1 |  | [codex: 📝 Agent report](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/20) |
| Code review | ✅ 2 of 2 approve | [PR #9](https://github.com/cylewitruk/stacks-core-autobot/pull/9) · 0 open findings |
| ↳ Round 1 |  | [claude: 🛠️ Changes requested](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/15)<br>[codex: ✅ Approve](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/18) |
| ↳ Round 2 |  | [claude: ✅ Approve](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/23)<br>[codex: ✅ Approve](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/25) |
| Handoff | Ready for human review | A human reviews and merges |

| Estimate | Assessment | Evidence |
| ---- | ---- | ---- |
| Complexity: unknown · Change risk: unknown | Confidence: low · PR assessment · Assessment incomplete | [Report 25](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/25) · [Report 23](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/23) |

**Action needed:** a human review and merge ([details](https://github.com/cylewitruk/stacks-core-autobot/issues/7#issuecomment-24)).

<details><summary>Diagnostics</summary>

Readiness: CI passed and all Concurro reviewers approved. Ready for human review.

[Automation audit state](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/1) (public).

</details>

<!-- concurro:checkpoint:v1 {"digest":"0000000000000000000000000000000000000000000000000000000000000000","id":1001,"number":1,"revision":81} -->


## PR title

Clarify the guide

## PR description

Define the guide's boundary behavior.

### Problem

- The guide leaves the boundary undefined.

### Changes

- docs/guide.md explains the boundary.

### Testing

Agent-reported validation for [candidate commit](https://github.com/cylewitruk/stacks-core-autobot/commit/0000000000000000000000000000000000000014):

Read the edited documentation.

### Review focus

- Verify wording and links.

---

[Implementation plan](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/5)

Closes #7

## Published review

<!-- concurro:review:v1:cylewitruk/stacks-core-autobot#7:4 -->
### ✅ Concurro review · codex · Approve

Reviewed.

<sub>codex / gpt-6.1-sol</sub>

<details><summary>Diagnostics</summary>

Reviewed commit `0000000000000000000000000000000000000014` · review 4

</details>

## Commit messages

```text
Clarify the guide.

- Explain the boundary in docs/guide.md.

Refs #7
```

```text
Clarify the boundary example

- Explain the boundary in docs/guide.md.

Refs #7
```

## Rollout

Use an idle window or explicit writer `/concurro invalidate` recovery for the trusted bundle changes.
