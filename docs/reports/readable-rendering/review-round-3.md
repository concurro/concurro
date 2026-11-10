# Review round 3: round-two corrections

R1–R4, 15–17 and 19–25 are fixed as described in
[the round-two response](review-response-round-2.md). The reported test results are trusted
and weren't re-run.

The reference neutralizer was probed directly, and it now handles all of these correctly:

- ordinary words, `C#`, `#[derive]`, email addresses and code spans are left alone;
- `#N`, `owner/repo#N`, `GH-N`, issue and PR URLs and `@` mentions are neutralized;
- escaped or unclosed backticks don't open a code span.

## Remaining (low)

**S1. PR titles aren't neutralized.**

- **Where:** `Publication.pr_title` uses the planner's headline without `inert`.
- **Why it matters:** GitHub's squash merge uses the PR title as the commit subject. A
  headline such as "Fix #12 overflow" would then close #12 when the commit reaches the
  default branch.
- **Status:** this predates the branch, but `inert` now exists. Apply it to the title.

**S2. Code spans are exempt in commit messages too.**

- **Where:** `commit_message` passes the subject and `changes` through `inert`, which skips
  code spans.
- **Why it matters:** the code-span exemption reflects how Markdown renders. Whether GitHub's
  closing-keyword parser ignores backticks in a commit message is unverified, so
  ``Closes `#12` `` in a commit body might still close #12.
- **Fix:** neutralize inside code spans as well for commit text, which isn't rendered as
  Markdown anyway.

**S3. The preview reports are out of date.**

- **Where:** `docs/reports/readable-rendering/{A..E}.md` still show renders from before the
  corrections. For example, `B.md` still shows "Every open finding blocks the merge" on a
  ready dashboard with no findings.
- **Fix:** regenerate them from the current code before using them for visual review.
