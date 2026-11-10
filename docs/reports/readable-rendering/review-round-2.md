# Review round 2: uncommitted corrections

Reviewed the uncommitted corrections against [the first review](review.md) and
[the response](review-response.md). The test results reported in the response are
trusted and weren't re-run.

## Confirmed fixed

| Findings | Notes |
| ---- | ---- |
| 1, 13 | Verified through GitHub's `/markdown` API: whitespace-only spans render literally, no HTML gets through, and `\|` is escaped only in table cells. |
| 2, 18 | A same-ID submit is sent only when every journaled submit for that review definitely failed. A submit cleared by an operator never allows another one. |
| 3 | A legacy body is adopted before invalidation or implementation replaces the plan. |
| 4 | Invalid results now go back to the model as repair feedback. |
| 5 | Commit references use the full issue URL when the PR is in another repository. |
| 7 | Gate states that need a human now show a WARNING. |
| 8 | Operator reasons now come from an explicit map, with the caveat in R4. |
| 9 | Criterion labels are computed only for design-QC events; see R3. |
| 10, 11, 12, 14 | Fixed as described in the response. |
| 6 | Fixed, but the neutralization goes too far; see R1. |

Codex's two corrections to the first review are accepted:

- you authorized the five commits;
- `ServiceRun.test_finding_lifecycle` already used the default publication protocol.

## New findings

**R1. Medium: `pr_description.inert()` changes far more than references.**

- **Where:** `engine/pr_description.py`.
- **What it changes:** it inserts a zero-width space into:
  - every "fix", "fixed", "close" or "resolve" word;
  - every `#` and every `@`.
- **Where that text ends up:** commit subjects and bodies, PR description fields and the
  plan-summary fallback.
- **Verified:** `inert("Fix overflow in `foo#bar` for C# and #[derive]; fixed-size buffer")`
  returns `F​ix overflow in `foo#​bar` for C#​ and #​[derive];
  f​ixed-size buffer`.
- **Problems:**
  - commit subjects carry invisible characters, so `git log --grep=fix` no longer matches;
  - copied code identifiers stop working;
  - the keyword edits are redundant, because breaking the reference already stops the
    issue from closing.
- **Fix:** neutralize only actual references:
  - `#123`;
  - `owner/repo#123`;
  - `GH-123`, which isn't handled today;
  - issue and PR URLs;
  - `@` followed by a login character.

  Leave closing keywords and other `#` characters alone, and skip code spans, where GitHub
  creates no links.

**R2. Medium: code spans in PR descriptions render with literal backticks.**

- **Where:** `pr_description.sections` renders `lead`, `problem`, `changes`,
  `compatibility` and `review_focus` with `markup.text`.
- **Verified:** `` Use `Vec::with_capacity()` in `read()` `` renders as
  `` Use \`Vec::with\_capacity\(\)\` in \`read\(\)\` ``.
- **Why it matters:** the schema asks every `changes` bullet to name a file or function,
  so most bullets will show this. It's the same problem finding 14 fixed for report views.
- **Fix:** use `markup.inline`, applied after the narrower R1 neutralization, which must
  not touch code spans.

**R3. Low–medium: an oversized event now decrements the stored sequence.**

- **Where:** `engine/conversation.py`, near line 502.
- **Cause:** `queue` no longer increments `journal["sequence"]` before rendering, but the
  oversize path still runs `journal["sequence"] -= 1`.
- **Result:** a rejected event moves the stored sequence backwards, and the next event
  reuses a sequence number that was already used.
- **Fix:** delete that line, and add a test for an oversized event followed by a normal
  one.

**R4. Low: operator reasons are matched against copied strings, and the list is incomplete.**

- **Where:** `engine/reason_guidance.py`.
- **Copied strings:** `OPERATOR_REASONS` and `GATE_ACTIONS` duplicate most reasons as
  string literals; only four raise sites use the new constants. Each literal matches its
  raise site today, but rewording a reason would silently drop its mapping.
- **Unmapped operator-style reasons:** these still get the generic retry guidance:
  - "PR closed or head changed outside the controller";
  - "PR is not authored by the controller App";
  - "branch moved outside the controller";
  - "implementation branch moved outside the controller";
  - "implementation branch disappeared".
- **Fix:** use the constants at every raise site, or record the class where the reason is
  raised (for example an operator-only `Blocked` subclass), so a new reason can't be missed.

## Still open from the first review

- **Should-fix items not yet addressed:** 15–17 and 19–21.
- **Presentation items not yet addressed:** 22–25.
- **Live validation:** the same-ID submit still needs to be tested live. It stays disabled
  until then.
- **Bundle rollout:** this round changes `develop.json` and the implementer skill again, so
  deployment needs an idle window or `/concurro invalidate` recovery.
