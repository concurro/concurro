# Proposal: readable GitHub rendering

**Status: archived design brief; implementation reviewed.** Retained as design history.
Current behavior is documented in [PR reviews](../../design/pr-reviews.md) and
[issue dashboards](../../design/issue-dashboard.md); see the [review record](README.md)
for corrections and release limitations. The original proposal follows.

Make every surface Concurro posts to GitHub easy for a maintainer to scan. The pilot's issues
and PRs (concurro-demo-upstream/stacks-core #21–#30) show three problems:

- walls of text;
- raw JSON where readable reports belong;
- messages that don't say what a human should do.

**Implementation:** build one PR at a time, in the [order below](#pr-order), starting with
**B**. Each PR is reviewed before the next one starts. If the code contradicts anything here,
stop and report it instead of working around it.

## Rules (apply to every surface)

1. **Models write content in bounded fields; Concurro owns the outer layout.** Headings,
   sections and their order come from Concurro. Each field has a size limit. Inline fields
   (titles, bullets) go through `markup.text`. Detail fields may keep the Markdown that
   `markup.block` permits. Model-supplied HTML is never rendered. Concurro's own
   `<details>`, `<summary>` and markers are allowed.
2. **Every message a human sees answers three questions:** what happened, does it need me,
   and what can I do. Where action is possible, give the exact commands. Where none is
   needed, say "No action needed". An operator-only failure says that an operator must act,
   and doesn't suggest that a retry will fix it.
3. **Machine data stays off the path a human reads.** Hashes, bindings, JSON and internal
   counters go into collapsed sections or data artifacts. Exceptions:
   - identifiers a required command needs, such as the plan ID in `/concurro approve <id>`;
   - finding and objection IDs, which humans act on.

   Uncertainty stays visible in plain words, for example "Assessment incomplete" or "Based on
   the previous revision".

## Constraints

- **Already-posted content stays valid.** Some surfaces are reconciled by comparing bodies.
  For example, `reviews.py` raises `Blocked(CONFLICT)` when an existing body differs from a
  fresh render, and findings store a digest of their opening body. Any renderer whose output
  is compared gets a new version, following the `review_body_v1`/`v2` and `render`/`render_v2`
  pattern. Old versions stay in place to verify bodies already posted. Check every surface
  you change for this.
- **Markers and artifact numbering stay stable** (`concurro:*:v1`) unless you version them.
- **Readers of existing formats keep working.** For example, `design_audit.archived_report()`
  parses the "Full report" links to recover canonical data.
- **Bundle changes need a rollout plan.** Changes to schemas, skills and agent instructions
  alter the trusted bundle binding, and renderer compatibility alone won't keep active tasks
  running. Any PR that changes the bundle (D, possibly A) needs the repository's documented
  idle-window or an explicit recovery plan. **B changes rendering only**, so it stays
  bundle-free.
- **Gateway changes** add only exact-shape writes, each bound to the task's PR, review and
  threads, following the existing checks in `service/admission.py`.

## Findings

### 1. Report links lead to raw JSON (PR A, highest impact)

- **Today:** a dashboard link such as "opus55-medium: ✅ Approve" opens a stub artifact whose
  only content is a "Full report" link. That link opens "Report data · part N"
  (`report_store.py`), a JSON dump. Older reports embed the JSON directly.
- **Want:** a new, versioned, readable report artifact format containing:
  - the verdict heading and headline;
  - objections, concerns or findings, as a list with their IDs;
  - criteria as readable names, in a table;
  - the classification on one line;
  - the agent and profile in a footer.
- Render each report kind appropriately. Rejected model output stays visibly rejected and
  never gets a normal verdict layout.
- The canonical data parts don't change. The readable artifact links to them from a
  collapsed "Raw data" section, either keeping the links `archived_report()` parses or
  updating that reader so it still accepts every older format.
- Keep the output within the existing artifact size limits.

### 2. PR description (PR D)

- **Today:**
  - `Publication.pr_body` renders the planner's free-text `plan.summary`. The result is dense
    paragraphs (PR #28) or a multi-screen design document (PR #23).
  - The body is written once when the PR opens. Finding an existing PR neither compares nor
    refreshes it.
  - The gateway doesn't allow a PR-body PATCH.
- **Want:**
  - **Implementer-owned fields.** Add a `description` object to `develop.json`, written by
    the implementer:
    - `lead`: 1 sentence;
    - `problem`: 1–4 bullets;
    - `changes`: 1–6 bullets, each naming a file or function;
    - `compatibility`: 0–4 bullets.

    Set a limit on each field (about 200 characters per bullet), and update the
    `concurro-implement` skill to match.
  - **Scope.** The description covers the **whole current PR diff against base**, not only
    the latest repair round. Store it together with the candidate head it describes.
  - **Testing has one source.** Render the Testing section from `validation`, labelled as
    agent-reported and naming the candidate commit it describes. Don't add a second copy of
    testing claims. If `validation` needs structure (bullets), change it there.
  - **Layout:**
    1. the lead sentence;
    2. `### Problem`, `### Changes`, `### Compatibility`, `### Testing` and `### Review focus`
       (from `publication.review_focus`), omitting empty sections;
    3. a horizontal rule;
    4. `[Implementation plan](…)`, then the existing dependency summaries (📦), then the
       existing closing link, including the cross-repository `Closes <url>` form.
  - **Refresh.** When a new round changes the description, update the PR body, but only if it
    still matches what Concurro last posted (store the digest). If a human has edited it,
    leave it alone and show a short, persistent notice in the dashboard. Add the PR-body
    PATCH to the gateway as an exact-shape, PR-bound write.
    - The digest check before a PATCH protects against human edits on a best-effort basis;
      it isn't atomic.
    - Store the digest of the intended body before sending the PATCH. A body matching
      either the last-published or the intended digest is Concurro's own, so a PATCH whose
      response was lost is never mistaken for a human edit.
  - **Reviewers see it.** PR reviewers receive the **published** PR body in their context,
    including when a human edit has stopped the refresh, and get one review criterion: the
    description is accurate.
- The repository-template mapping moves to a later PR (E).

### 3. "Intake approved with concerns" event (PR B)

- **Today:** about seven dense bullets written for the planner, posted to the issue
  (`conversation.py`).
- **Want:** one summary line, with the concerns inside `<details>`.

### 4. Escalated and Blocked events (PR B)

- **Today:** `actions.explain` shows the first sentence of the reason and then repeats the
  whole reason under "Full reason" (issue #26). "Plan revision budget exhausted…" doesn't link
  to the objections. Current retry, invalidate and plan-revision guidance already exists
  (`BLOCKED_RETRY`, `BLOCKED_INVALIDATE`, `--plan-revisions`). `LEGACY_BUNDLE_REASON` is a
  legacy reason.
- **Want:** extend the existing guidance into a fixed layout:
  - **what happened:** one human sentence, not repeated in the collapsed part;
  - **why:** links to the open objections or findings, where there are any;
  - **what you can do:** the commands for this reason. For operator-only reasons, say that an
    operator must act.

  Map internal reasons to a human sentence plus an operator note.

### 5. Finding titles escape code spans (PR B; bug)

- **Today:** `markup.finding_title` calls `text()`, which escapes backticks. So `` `.clone()` ``
  renders with literal backticks (confirmed with GitHub's `/markdown` API on PR #23 F1).
- **Want:** keep well-formed inline code spans and escape everything else, **in new renderer
  versions only**. Changing `finding_title` globally would alter existing review renderers
  and the stored opening-body digests.
- **Tests:** table pipes, escaped and unmatched backticks, multi-backtick delimiters, and
  truncation through a code span.

### 6. Dashboard status meaning (PR B)

- **Today:**
  - "Merge ✅" and "✅ Handed off" read as "merged".
  - The `TIP` callout is used for waiting and blocked states, for example "PR readiness is
    waiting on: GitHub test-merge commit is not current".
- **Want:**
  - rename the stage to "Handoff";
  - choose the callout by state: NOTE while in progress, TIP only when ready, WARNING when a
    human is needed, CAUTION when blocked;
  - keep readiness blockers that need no human out of the callout, in diagnostics only;
  - state "Every open finding blocks the merge" once here, instead of on every review.

### 7. Commit messages (PR D)

- **Today:** `fix(concurro): address issue #21 (round 1)`.
- **Want:**
  - **First implementation commit:** the plan headline as the subject.
  - **Repair commits:** a subject describing that commit's own changes, supplied by the
    implementer for that round.
  - **Body:** the round's changes as bullets, plus `Refs #N`.
  - **Prefix:** use a conventional-commit prefix only when a config flag says the target
    repository uses that convention, with the type chosen by the model, never hard-coded.

### 8. Empty review entries (PR C)

- **Cause:** each comment posted outside a review submission gets its own review, with an
  empty body. Three code paths do this:
  - fix replies (`pulls/{pr}/comments/{id}/replies`);
  - file-level findings (`pulls/{pr}/comments` with `subject_type: file`);
  - "design reopened" notices (`withdrawn()`).

  Seen on PRs #12, #23 and #30.
- **Fix:** publish **each reviewer's report** as one GraphQL pending review, then submit it.
  This is one review per report, not one review merging several reviewers' reports; that is
  [consolidated PR reviews](../../proposals/consolidated-pr-reviews.md), which can reuse this mechanism.
  1. `addPullRequestReview(pullRequestId, commitOID, body: <marker stub>)` with no event. Put
     the report's marker in the pending body, so a leftover attempt can be identified.
  2. Add threads one at a time with `addPullRequestReviewThread(pullRequestReviewId, …)`, not
     through the `threads` argument: one bad line makes that whole call fail. Use a
     line-level thread, falling back to `subjectType: FILE`. **On an unresolvable line, this
     call returns `thread: null` with no error.** Treat null as failure and fall back.
  3. Post fix replies with `addPullRequestReviewThreadReply(pullRequestReviewId,
     pullRequestReviewThreadId, body)`.
  4. Submit with `submitPullRequestReview(event: COMMENT, body)`, using the full review body.

  A "design reopened" event becomes one review with a short summary body.
- **Placement:** before submitting, record where each finding went (inline, file or
  body-only). Body-only findings stay supported. Keep thread verification and resolution
  receipts.
- **Publication state.** The outbox item records its progress before each step:
  1. `building`: the pending review ID once its creation has settled, plus the placements,
     threads and replies added so far;
  2. `submitting`: the review ID and the frozen body, recorded before the submit is sent;
  3. `submitted`: the submit is confirmed;
  4. done: threads are mapped and verified, and every resolution receipt is finished. Only
     then is the outbox item removed. A submitted review means publication succeeded, not
     that the item's work is finished.
- **Identifying a review.** A review is this report's only if its author is the App and its
  PR, reviewed commit, marker and body all match the item's frozen values. A review with
  this report's marker that fails any of these checks is a conflict and stops the task.
- **Recovery, by state:**
  - **Before creating a review:**
    - a submitted review identified as this report's exists: go to `submitted`;
    - an App pending review carrying this report's marker exists, and the item isn't
      `submitting`: it's an abandoned build. Delete it (`deletePullRequestReview`), and
      rebuild only once the delete is confirmed. A pending review is visible only to the
      App, so deleting it reveals nothing;
    - any other App pending review: stop for an operator. Never delete it automatically.
  - **`submitting`:** never delete or rebuild, and never create another review while the
    submit is uncertain. Read the review by its recorded ID:
    - submitted and identified as this report's: go to `submitted`;
    - positively identified as this report's and still `PENDING`: submit **the same review
      ID** with the frozen body again, once same-ID retry behaviour has been validated live
      (see below);
    - missing, inaccessible, mismatched, or in any other state: stop for an operator.
  - **`submitted`:** finish thread mapping and resolution receipts, then remove the item.
- **Uncertain outcomes.** The gateway keeps stopping the task on any unknown outcome, so
  recovery can't be sent through it. A read showing the review still pending doesn't prove
  a lost submit failed, because the request may still be running.
  - **First scope:** every uncertain write in this flow stops for an operator, as today.
  - **One explicit service path:** an operator command that reconciles one specific stopped
    review publication. When to run it is the operator's judgment, not proof that the
    original request has finished. It reads the PR and the item's state, then:
    - for an uncertain thread or reply write: deletes the identified pending review and
      records the uncertain write as **discarded by confirmed deletion**, with the delete's
      receipt as evidence. Deletion compensates for the write; it doesn't prove the write
      failed;
    - for an uncertain submit: applies the `submitting` cases above.

    Uncertain create-review and delete-review writes stay manual, with no command support.
  - **The command's own writes are tracked.** It keeps the normal ownership checks, and its
    authority covers only the specific unresolved write it reconciles. It records its
    delete or submit through the gateway like any other write, and clears the unresolved
    write only after that write settles. It never clears the unresolved write first and
    then writes untracked. If its own delete or submit is interrupted or uncertain, the
    reconciliation is recoverable in the same way.
  - **Durable recovery:** each intent records the mutation kind, the task and report
    identity, the pending review ID once known, and the recovery progress, so a crash
    mid-reconciliation resumes where it stopped. Today the registry records only a request
    digest.
  - Running this reconciliation without an operator is a later step, taken only if lost
    responses actually occur. It would need its own validation, not just a scheduling
    change. Manual triggering removes scheduling and retry policy; the reconciliation
    logic needs the same crash safety either way.
- **Gateway admission:** admit these five mutations with exact shapes:
  - `addPullRequestReview`;
  - `addPullRequestReviewThread`;
  - `addPullRequestReviewThreadReply`;
  - `submitPullRequestReview`;
  - `deletePullRequestReview`.

  Bind each one to the task's PR, its own pending review, and threads that are the task's
  own findings. Reuse `MutationGateway.ours`, which already handles GraphQL's bot login
  without `[bot]`.
- **Gateway settlement:** `settlement.settle` recognizes successful responses only for the
  two existing GraphQL mutations, so every new mutation would settle as `unknown`. Define
  the accepted response for each:

  | Mutation | Settles `done` when |
  | ---- | ---- |
  | `addPullRequestReview` | `pullRequestReview.id` is present and its state is `PENDING` |
  | `addPullRequestReviewThread` | `thread.id` is present, or the `addPullRequestReviewThread` result is present with an explicit `thread: null` and no errors (no thread was created; the engine falls back to `FILE`). Missing `data`, a missing mutation result or a missing `thread` field is not a null thread and settles `unknown`. |
  | `addPullRequestReviewThreadReply` | `comment.id` is present |
  | `submitPullRequestReview` | `pullRequestReview.id` matches and its state is `COMMENTED` |
  | `deletePullRequestReview` | `pullRequestReview.id` matches |

  Any GraphQL `errors` still settle `unknown`, as today.
- **Migration:** outbox items already partly published through REST (review posted, replies
  or file comments missing) finish under the REST process.
- **GitHub facts (tested live with the App's token on concurro-demo-fork/stacks-core#3):**
  - replies work on open, outdated, resolved and file-level threads, and resolved threads
    stay resolved;
  - pending comments are invisible to REST `pulls/{pr}/comments`, even for the App;
  - only one pending review is allowed per author per PR;
  - `reviews(states: PENDING)` finds the App's own pending review;
  - deleting a pending review leaves no trace.
- **Still to validate live before C relies on it:** how `submitPullRequestReview` behaves
  when sent again for the same review ID, both while the review is pending and after it has
  been submitted.

### 9. Internal noise (PR B)

- Move `<sub>Event N</sub>` to diagnostics.
- Show structured criterion IDs (such as "G-QUALITY") as readable criterion names. Keep
  finding and objection IDs. IDs that reviewers write into their prose are a bundle change
  (reviewer instructions) and move to D.
- Remove `markup.BLOCKING_NOTE` from reviews; item 6 states it once in the dashboard.
- Simplify the estimate wording ("2/2 assessed · provisional · retains earlier intake"), but
  keep its uncertainty visible in plain words.

### 10. PR title suffix (PR B)

- **Today:** `pr_title` appends ` (#issue)`, and GitHub's squash merge appends ` (#PR)`,
  giving "… (#21) (#23)".
- **Want:** drop the suffix for new PRs; `Closes #N` already links the issue.

### 11. Plan artifact (PR B)

`checkpoints.plan_body` stays planner-owned:

- omit empty sections, such as "## Questions";
- number the steps;
- pass model strings (summary, list items, responses, draft reason) through `markup` instead
  of interpolating them raw;
- move the plan and base hashes and the executable-validation JSON into a collapsed section;
- keep a rejected draft visibly rejected, with its raw output collapsed.

Check whether this body is compared anywhere before changing it.

## PR order

| Order | PR | Scope |
| ---- | ---- | ---- |
| 1 | B | Items 3, 4, 5, 6, 9, 10 and 11: presentation, versioned finding titles, plan cleanup |
| 2 | A | Item 1: readable report archives, compatibility readers, bounded output |
| 3 | C | Item 8: pending-review publication, gateway checks, recovery, migration |
| 4 | D | Items 2 and 7: structured description, safe refresh, reviewer context, commit messages; reviewer instructions on criterion IDs (item 9) |
| 5 | E | Mapping onto the repository's PR template (scoped separately) |

## Done when

- Golden-file tests cover each changed renderer, including bodies posted by earlier versions.
- One task's issue, PR and artifacts are rendered end-to-end from a recorded checkpoint, and
  the resulting markdown is attached to each PR for visual review.
- **PR C** has failure-injection tests at every write and every state:
  - settlement of each mutation's response, including an explicit null thread and
    malformed responses (missing `data`, mutation result or `thread` field);
  - an abandoned build, another pending review by the App, and a review with this report's
    marker but a mismatched author, PR, commit or body;
  - an uncertain thread or reply write reconciled by confirmed deletion;
  - an uncertain submit: applied, still pending, and missing or mismatched;
  - an interrupted or uncertain delete or submit sent by the reconciliation command;
  - uncertain create-review and delete-review writes staying stopped;
  - a crash after submit but before resolution receipts finish, and a crash during
    reconciliation;
  - partly published REST items.
- **PR D** has tests for a human-edited body, a lost PATCH response, a stale description and
  a cross-repository closing link, plus its bundle rollout plan.
- No surface shows JSON, hashes or internal counters outside collapsed sections, data
  artifacts or required commands.

## Decisions

- **Description refresh:** Concurro updates the PR body only while it's unchanged since
  Concurro last posted it. Any human edit stops the refresh, rather than Concurro managing
  a marked section of the body.
- **Testing section:** built from the existing `validation` field, with no new
  `description.testing` field.
- **Unidentified pending review:** if the App has a pending review it can't identify as this
  report's, Concurro stops for an operator rather than deleting it.
- **Uncertain review writes:** in C, every uncertain write stops for an operator, who
  reconciles through one service command. Running that reconciliation automatically waits
  for evidence that lost responses happen.
