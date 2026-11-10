# Readable GitHub output: implementation and review record

The implementation and three correction rounds are reviewed. This directory retains the
[original design brief](proposal.md), previews and review history. Current behavior lives
in [PR reviews](../../design/pr-reviews.md), [issue dashboards](../../design/issue-dashboard.md)
and [service operation](../../design/service.md).

| Record | Response |
| ---- | ---- |
| [Initial review](review.md) | [Initial corrections](review-response.md) |
| [Round two](review-round-2.md) | [Round-two corrections](review-response-round-2.md) |
| [Round three](review-round-3.md) | [Round-three corrections](review-response-round-3.md) |

Previews: [A — reports](A.md), [B — presentation](B.md), [C — publication](C.md),
[D — descriptions](D.md), [E — templates](E.md). Regenerate or check them from the root:

```sh
python engine/tests/render_readable_previews.py
python engine/tests/render_readable_previews.py --check
```

The review documents record their point-in-time working-tree and test status. Uncertain
same-review resubmission remains disabled; its live test is separate. The bundle changes
require an idle rollout or explicit writer invalidation recovery. CI and deployment checks
must refer to the final signed release commit.
