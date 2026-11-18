# Timeline check: evidence

Read-only evidence for the [enrollment channels](../contracts.md#enrollment-channels) and
[coverage gaps](../contracts.md#coverage-gaps). Run on 2026-11-18 with `gh` 2.104.0
against REST API version `2022-11-28`, on the demo target
(`concurro-demo-upstream/stacks-core`) and, for the listing cap, its upstream
(`stacks-network/stacks-core`). `<person>` stands for the person who acted.

```bash
V="X-GitHub-Api-Version: 2022-11-28"
R=concurro-demo-upstream/stacks-core
FIELDS='[.id, .event, (.label.name // .assignee.login // .requested_reviewer.login // ""), .actor.login, .created_at] | @tsv'
EVENTS='labeled|unlabeled|assigned|unassigned|review_requested|review_request_removed'
for n in 36 52; do
  gh api -H "$V" "repos/$R/issues/$n/events?per_page=100" \
    --jq ".[] | select(.event|test(\"$EVENTS\")) | $FIELDS"
done
gh api -H "$V" "repos/$R/issues/52/timeline?per_page=100" \
  --jq '.[] | select(.event|test("review_request")) | [.id, .event, .created_at] | @tsv'
for i in 1 2; do
  gh api -H "$V" "repos/$R/issues/52/events?per_page=100" --jq '[.[]|.id]|join(",")' | shasum
done
gh api -H "$V" "repos/stacks-network/stacks-core/issues/events?per_page=100&page=300" \
  --jq 'length, .[-1].created_at'
# Expected to fail with HTTP 422, so the script exits non-zero.
gh api -H "$V" "repos/stacks-network/stacks-core/issues/events?per_page=100&page=301"
```

## Results

**Repeated labels get distinct IDs.** Issue 36, Concurro's App moving stage labels (an
excerpt; the same label is added more than once):

```text
35679820104  labeled    concurro                <person>       2026-11-10T12:30:39Z
35680257340  labeled    concurro:stage:plan     concurro[bot]  2026-11-10T12:35:20Z
35680831195  labeled    concurro:stage:plan-qc  concurro[bot]  2026-11-10T12:41:39Z
35680834891  unlabeled  concurro:stage:plan     concurro[bot]  2026-11-10T12:41:41Z
35733485098  labeled    concurro:stage:plan-qc  concurro[bot]  2026-11-10T21:49:24Z
35744017848  labeled    concurro:stage:plan     concurro[bot]  2026-11-11T00:06:12Z
```

**A review request removed and re-added two seconds apart gets two IDs.** PR 52; the
timeline endpoint returns the same three IDs:

```text
36180807062  review_requested        ConcurroBot  <person>  2026-11-15T09:08:25Z
36180866061  review_request_removed  ConcurroBot  <person>  2026-11-15T09:09:48Z
36180867694  review_requested        ConcurroBot  <person>  2026-11-15T09:09:50Z
```

**IDs are stable:** two reads of PR 52's events give the same ID list (identical hashes).

**The repository-wide listing is capped:** page 300 returns 100 events, the last from
2025-01-10T16:58:28Z; page 301 returns HTTP 422, "pagination is limited for this
resource".

## Not covered

Assignment events and a person removing and re-adding `concurro` don't occur in this
history. The listing delay and `updated_at` behaviour need controlled changes on a test
repository ([open items](../contracts.md#open-items)).
