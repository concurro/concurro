# Review: readable-github-output (da374b0..e9328c2)

Reviewed against [the proposal](../../proposals/readable-rendering.md). Ordered by severity
within each group. Line numbers refer to the branch tip unless a commit is named.
"Verified" means the finding was reproduced by running code or GitHub's `/markdown` API; the
rest come from reading the code.

None of the format-4/format-5 bodies has been posted yet, so the rendering fixes (1, 13) can
change those formats in place. Once anything is deployed, they need another version bump.

## Must fix before deployment

1. **Model HTML gets through an empty code span (B, carried into A). Verified.**
   - **Where:** `markup.code()` (`engine/markup.py:71-78`), called from `inline` (`:242`).
   - **Cause:** `code("")` returns a bare ` `` ` (two backticks), which isn't a closed span.
   - **Input:** the title `` Fix ` ` ``a`<img src=x>`` `` renders `<img>` as live HTML.
   - **Affected:**
     - format-4 finding headings, which are frozen into `opening_digest`;
     - the format-4 review table;
     - format-5 report views.
   - **Fix:** escape whitespace-only span content as literal text.
2. **A submit that was never sent leaves the item stuck for good (C). Verified.**
   - **Where:** `engine/pending_reviews.py:146-164`.
   - **How it happens:** the item is checkpointed as `submitting`, and then the submit fails in a way that definitely didn't apply:
     - the gateway reports it unsent, a 401 or throttled;
     - the gateway refuses it with `Disallowed`;
     - the process crashes before sending.
   - **Result:**
     - every later tick raises `Blocked("…Same-ID retry is not enabled")`;
     - `recover-review` refuses, because no `unknown` intent exists;
     - a generic `resolve` doesn't change the engine's state;
     - humans can't see or delete the App's pending review;
     - every later pending-review item on that PR is blocked too.
   - **Fix:** distinguish "no submit intent, or one that settled `failed`" from an uncertain
     submit. In the first case the same-ID submit is a first send, not a retry. Add a test
     that fails the checkpoint between `submitting` and the send.
3. **PRs opened before D are flagged as human-edited for good (D). Verified.**
   - **Where:** `engine/pr_description.py:106-110`.
   - **Cause:** with no `pr_description` receipt, `current not in (None, None)` is always
     true. That covers every PR opened before D, including tasks recovered with
     `/concurro invalidate`, which keeps `state["pr"]`.
   - **Result:** the body is never refreshed, and the dashboard permanently shows a false
     "human edit" notice.
   - **Fix:** with no receipt, treat the body as owned if it equals the legacy `pr_body`
     render.
4. **A schema-valid implementer response hard-blocks the task (D). Verified by reading.**
   - **Where:** `bundle/schemas/develop.json`, `engine/pr_description.py` `validate`, and
     `engine/controller.py:1684`.
   - **Cases:**
     - `description: null` or `commit: null` alongside edits and `design_issue: null`
       passes the schema;
     - a whitespace-only field passes `minLength`/`pattern` but fails `strip()`.
   - **Result:** `validate` raises `Blocked`, outside the repair `try`, so the model gets no
     feedback.
   - **Fix:** make the schema and the code enforce the same rules, and send any remaining
     failure back as repair feedback instead of blocking.
5. **Cross-repository commit messages point at the wrong issue (D). Verified.**
   - **Where:** `engine/pr_description.py:95` writes `Refs #N`.
   - **Cause:** when `upstream != repository`, the merged commit lives upstream, so `#N` links
     an unrelated upstream issue.
   - **Fix:** use the full issue URL, as `pr_body` already does.
6. **Model text can close issues and mention people (D, E). Verified passthrough.**
   - **What happens:** `markup.text` leaves `Fixes #12`, `owner/repo#N` and `@org/team`
     unchanged, and commit-message `subject`/`changes` go in raw. A closing keyword in a
     description bullet or a commit body closes that issue on merge, and mentions send
     notifications.
   - **What's new:** this class of problem already existed for `plan.summary`. D adds
     implementer output (which issue or repository content can steer) and commit messages.
   - **Fix:** neutralize closing keywords, issue references and `@` mentions in model fields
     for the PR body and commit messages.
7. **The gates callout says "No action needed" while a human is needed (B). Verified by reading.**
   - **Where:** `engine/dashboard.py:173-189`, `:379`, `:396-407`.
   - **Cause:** every gates state that isn't ready shows NOTE "Checking reviews and CI… No
     action needed", including:
     - "PR returned to draft; a human must mark it ready";
     - "CI suite unsuccessful";
     - "PR is closed";
     - "unresolved review threads";
     - "head changed outside the controller".
   - **Contradicts:** item 6 and rule 2.
   - **Related:** `action_line` still offers "a fix and a retry" for operator-only reasons.
8. **The operator check scans model text, and the per-reason mapping is missing (B). Verified.**
   - **Where:** `engine/actions.py:150-154`, a substring search for "operator" across the
     whole reason.
   - **Model-text false positive:** an escalation rationale that mentions "the `?` operator"
     becomes "needs operator reconciliation". The retry or `--plan-revisions` guidance is
     dropped.
   - **Missed operator-only reasons:** these still get `BLOCKED_RETRY`:
     - "prepared publication must be reconciled before reopening design";
     - "cannot reconcile accumulated PR changes";
     - "PR review publication failed repeatedly".
   - **Missing:** item 4 asked for a mapping from internal reasons to a human sentence plus an
     operator note. Make it an explicit table keyed by the engine's own reason constants, and
     never search model text.
9. **Event rendering now applies the criterion limit at intake and copies the issue text into every event (A). Verified.**
   - **Where:** `engine/conversation.py:468`.
   - **The limit:** `design.criteria()` raises `Blocked` above 32 requirement parts, and now
     runs for every sourced event, including intake QC. A large issue loses its intake
     decision and wastes the intake call on every retry. `journal["sequence"]` has already
     been incremented when it raises.
   - **The copies:** every pending event also stores a full copy of the requirement text
     inline: 21.6 KB per event for a 21.5 KB issue. With several pending events this can
     exceed the 60,000-byte checkpoint limit.
   - **Fix:** only design-QC events use the labels, so compute them only there.
10. **A failed compensating delete disables `recover-review` (C). Verified.**
    - **Where:** `service/registry.py` `admit()` links `recovery = child` before sending, and
      `service/review_recovery.py:29-31` refuses any child that isn't `done`.
    - **What happens:** if the delete settles `failed` (for example unsent or throttled):
      - re-running the command gives "compensating write requires manual resolution";
      - the child can't be `resolve`d, because it isn't `unknown`.
    - **Contradicts:** the proposal's "interrupted reconciliation recoverable in the same way".
11. **Model checklist lines appear in mapped template sections (E). Verified.**
    - **Where:** `engine/pr_template.py:89`, where `validation` goes through `markup.block`.
    - **What happens:** `- [x] Security reviewed` and `## Maintainer approval` render inside
      the template's checklist section. Model text can effectively tick boxes with no cited
      evidence.
    - **Fix:** neutralize task-list markers and headings in `validation` when rendering it.
12. **A review with a null author breaks publication (C). Plausible, not verified live.**
    - **Where:** `engine/pending_reviews.py:22` and `:52-56`, and `service/review_writes.py:39`.
    - **Cause:** `node.get("author", {}).get("login")` raises when `author` is `null`, as it
      is for deleted accounts. The item retries until `MAX_ATTEMPTS`, then blocks.
    - **Fix:** use `(… .get("author") or {})`, which the gateway already does elsewhere.

## Should fix

13. **Pipes in code spans are escaped outside tables (B). Verified.**
    - **Where:** `markup.py:242` always calls `code(..., cell=True)`, so headings and lists
      render `a\|b`.
    - **Timing:** this is frozen into format-4 digests once posted, so fix it together with
      finding 1.
14. **Report views still escape code spans in model text (A).**
    - **What happens:** objections and summaries render `` \`<limit>\` `` and `` \`read\(\)\` ``
      with literal backticks.
    - **Fix:** format 5 is new, so it can use `inline` directly.
15. **A model-written link can redirect `archived_report()` (A). Verified.**
    - **Where:** `engine/design_audit.py:94` scans the whole format-5 body.
    - **What happens:** a rationale containing a `[Full report](…/artifacts/99)` link is found
      first, and the report is recorded as "unavailable".
    - **Fix:** parse only the Raw data section. There is also no `archived_report` test on a
      format-5 body.
16. **The rejection view shows the model's own `reason` (A). Verified.**
    - **Where:** `engine/report_view.py:40` prefers the rejected draft's `reason` over the
      engine's `diagnostic.detail`. "All good, output accepted." renders as the rejection
      explanation.
17. **`explain` drops reason text (B). Verified.**
    - **Where:** `engine/actions.py:156-158`.
    - **What happens:** a reason with no usable first sentence is cut at 1,000 characters,
      with no ellipsis and no "Further detail" section.
18. **The gateway doesn't block a second submit of the same review (C).**
    - **Where:** `review_writes.binding` requires only `PENDING`.
    - **What happens:** the engine never resends today, but after an operator `resolve` the
      gateway would admit a second submit.
19. **A configured template mapping is silently ignored when the template isn't at the
    exact path (E).**
    - **Where:** `engine/pr_template.py`.
    - **What happens:** only `.github/PULL_REQUEST_TEMPLATE.md` is read. GitHub also accepts
      lowercase names, the repository root and `docs/`.
    - **Fix:** a configured mapping that matches nothing should block the task or show on the
      dashboard.
20. **Template errors surface only after a full implementer run (E).**
    - **Where:** `pr_template.pin` runs after the develop call (`controller.py:1689`).
    - **What happens:** a bad mapping wastes the call, and because the config is pinned,
      fixing it needs a new task.
    - **Fix:** validate before the implementer is asked.
21. **The commit subject can exceed 72 characters (D).**
    - **Where:** `pr_description.py:91-93`.
    - **What happens:** the subject is cut to 72 characters before the conventional prefix is
      added. The plan-summary fallback is cut mid-word with no ellipsis.
    - **Fix:** reuse `pr_title()`'s truncation.

## Presentation

22. "Every open finding blocks the merge" appears on every dashboard, including during intake
    and on a ready PR with no findings (`dashboard.py:128`). Show it only when findings are
    open.
23. Each report-view "Explanation" section starts by repeating the headline sentence.
24. With a template, "Review focus" is added as `###` after the template's last section, so it
    reads as part of "Maintainer notes".
25. The intake-concerns details repeat the heading ("Intake approved moving forward…") and say
    "not a request for human action" right after "No action needed".

## Test gaps (C)

- **The default publication path never runs end to end.**
  - Every controller and worker harness patches `PUBLICATION="rest"`
    (`review_fixtures.py:74`, `test_controller_splits`, `test_worker`).
  - So the pending-review path never runs through `Controller`, `Checkpoints`/`report_store`
    (which detaches frozen bodies of 2,000 bytes or more) or `GatewayGitHub`.
  - No test runs an engine stop, then `recover-review`, then a rebuild.
- **Settlement:** tested through `review_api.result`, not `settlement.settle`. Not covered:
  - a non-`PENDING` create;
  - a non-`COMMENTED` or mismatched submit;
  - a mismatched delete;
  - a reply with no ID;
  - `errors` on each mutation.
- **Admission negatives:** not covered:
  - a reply to a thread that isn't a finding, or on another PR;
  - a write against a submitted or foreign review;
  - another task's marker;
  - a closed PR;
  - a submit body that doesn't start with the marker.
- **Interruptions:** no checkpoint-write failures (finding 2) and no failed or in-flight
  compensating delete (finding 10).
- **Not tested at all:** a mixed outbox (a legacy REST item, then a pending-review item) and
  the registry v3→v4 migration. A manual run of that migration preserved an `unknown` intent.

## "Done when" gaps

- The A and B previews come from fixtures, not an end-to-end render from a recorded
  checkpoint.
- The format-5 goldens skip the `intake_rejection` and `local` kinds.
- **C:** the same-ID resubmit is still to be validated live, as recorded in the proposal.
- **D:** bundle rollout needs an idle window or `/concurro invalidate`. Finding 3 affects every
  task recovered that way.

## Checked and sound

- **Bundle:** B and A don't touch `bundle/`.
- **Compatibility:** formats 1–3 for reviews, findings, events and archives are unchanged and
  gated by format. Older archives still parse. `plan_body` is never compared for conflicts.
- **PR-body PATCH admission:** limited to a body-only payload on the task's own PR.
- **PR-body refresh:** the intended digest is stored before the POST and the PATCH, so a lost
  response is recovered.
- **PR body content:**
  - reviewers see the live body;
  - the footer keeps the dependency summaries and the cross-repository `Closes <url>`.
- **Pending-review admission (C):** bound to the task's own App-authored pending review, its
  owned open PR, its marker and its finding threads.
- **Explicit null thread (C):** settles `done`; malformed responses and any `errors` settle
  `unknown`.
- **Outbox removal (C):** an item is removed only after reply verification, thread mapping and
  resolution receipts.
- **Unknown pending reviews (C):** another App pending review, or duplicate markers, stops the
  item before any delete.
- **Template (E):**
  - pinned to `state["base"]` and capped at 16 KB;
  - template HTML is escaped;
  - mapped headings that are fenced or duplicated are refused.
