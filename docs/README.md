# Documentation

Current architecture and operating contracts. Source and regression tests remain
authoritative for exact limits and behavior.

| Document | Purpose |
| ---- | ---- |
| [Operating Concurro](concurro.md) | Target configuration, contribution flow and recovery commands |
| [Deployment](../gateway/README.md) | Host installation, App permissions and coordinated releases |
| [MVP architecture](design/mvp.md) | Scope, trust boundaries and deferred capabilities |
| [Execution and publication](design/mvp-execution.md) | Runner identity, setup, extraction, publication and readiness |
| [Service](design/service.md) | Scheduling, ownership, GitHub writes and recovery |
| [Inference gateway](design/inference-gateway.md) | Central credentials, session authorization and limits |
| [Scope envelope](design/scope-envelope.md) | Approved paths, cumulative file limits and large files |
| [Design-review eligibility](design/design-review-corrections.md) | Independent approvals through corrections, recovery and follow-ups |
| [Review-cost baseline](reports/design-review-baseline.md) | Reproducible pilot measurements and coverage limits |
| [PR reviews](design/pr-reviews.md) | Findings, native threads and publication recovery |
| [Issue dashboard](design/issue-dashboard.md) | Current-run display, diagnostics and durable report storage |
| [Comment format](design/comment-format.md) | Rendering and compatibility rules |
| [Design evaluation](design-evaluation.md) | Current operator-run review smoke test |
| [Complexity and change risk](design/complexity-risk.md) | Advisory assessments; no model routing or extra gates |

## Pending work

[Parallel review dispatch](design/parallel-reviews.md) starts with optional turn timing,
implemented locally for review. Shared parallel dispatch for design and PR reviews remains
a proposal at current recovery guarantees. Stronger crash recovery is deferred separately.
[Other design-review follow-ups](design/design-review-corrections.md#deferred-follow-ups)
include a defect/suggestion distinction and revision/convergence aids.

[MCP tools](design/mcp-tools.md) is a proposal, not supported target configuration.
Optional status summarization, model routing and sub-issue decomposition remain future
work in their respective designs. The current MVP does not implement automatic CI repair,
automatic merge or repair triggered directly by PR-thread replies.

## Documentation scope

These documents describe supported behavior. Completed migration plans, obsolete executor
architectures and historical experiment logs are kept only in Git history. Brief rationale
remains where it explains a non-obvious constraint, such as immutable report archives or
at-most-once thread resolution. Pending proposals are not configuration instructions.
