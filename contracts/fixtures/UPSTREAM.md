# GitHub webhook input fixtures

The `raw` fixtures are minimized, synthetic examples shaped against [Octokit's generated webhook schema](https://github.com/octokit/webhooks/blob/aa80c24a3f55354465b8b1438d33dd5602956d0d/payload-types/schema.d.ts) (commit `aa80c24a3f55354465b8b1438d33dd5602956d0d`, blob `bfee572991a18fb7f371b1d600f2216dec129bec`). They are not captured deliveries. The `expected` fixtures are Concurro's mapped contracts.

The transfer example uses `changes.new_issue` and `changes.new_repository`. The review thread example uses `thread.comments[0].id`; the review comment example uses `comment.pull_request_review_id`. These fields were checked against `IssuesTransferredEvent`, `PullRequestReviewThreadResolvedEvent`, and `PullRequestReviewComment` on 2026-11-19. Keep the raw inputs independently shaped from the mapped contract fields when updating them.
