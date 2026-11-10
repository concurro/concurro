# Structured description preview

Rendered from the recorded `DescribedAgents` task with one repair round. Validation is simulated fixture evidence.

## Issue dashboard

<!-- concurro:v1 -->
## Concurro

> [!TIP]
> Ready for your review. A human decides whether to merge.

Every open finding blocks the merge, whatever its severity.

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
| Complexity: unknown · Change risk: unknown | Confidence: low · PR assessment · Assessment incomplete | [Report 23](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/23) · [Report 25](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/25) |

**Action needed:** a human review and merge ([details](https://github.com/cylewitruk/stacks-core-autobot/issues/7#issuecomment-24)).

<details><summary>Diagnostics</summary>

Readiness: CI passed and all Concurro reviewers approved. Ready for human review.

[Automation audit state](https://github.com/cylewitruk/stacks-core-autobot/issues/7/artifacts/1) (public).

</details>

<!-- concurro:checkpoint:v1 {"digest":"43d1a7f0966a8ef35e3cff8a37be727295ccb56d698974c63f97de5cb3d5f841","id":1001,"number":1,"revision":81} -->


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

The change satisfies the plan.

<sub>codex / gpt-6.1-sol</sub>

<details><summary>Diagnostics</summary>

Reviewed commit `0000000000000000000000000000000000000014` · review 4

</details>

## Rollout

This changes the trusted bundle. Use an idle window or explicit writer `/concurro invalidate` recovery before deployment.
