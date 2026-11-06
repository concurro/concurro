# Scope envelope

Status: implemented (revision 3). The plan's file list stops being a hard fence: a plan
approves a design, the area it may touch and a size budget, and the implementer
makes whatever edits that design needs inside them. A design reopens when the change
outgrows the design, not when it needs one more file.

Line-budget rules below are superseded by [the MVP design](mvp.md#what-remains-enforceable); changed-line counts are informational, not enforced limits.

## Problem

An approved plan names 1–8 files, and the implementer may replace only those
(`validate_edits`). The `scope` check then requires exactly those files to change, with
exactly their declared status. So a planner must predict every edit, including
mechanical follow-on ones: a caller of a changed signature, an import, a test helper.
Planners routinely miss some. The only way forward is then a full design reopen: a
new plan and both design review rounds (about five to seven model calls), bounded by
`max_design_reopens`. An edit outside the list currently blocks the task outright.

Planning is meant to settle the architecture, the design decisions and their risks.
Predicting exact edits is the implementer's job; a planner that did it would
effectively be implementing.

## Decisions

| Question | Decision |
| ---- | ---- |
| What design review approves | The design, its primary files and their rules, an envelope of paths the change may touch, and a size budget |
| What the implementer may edit | Any file the repository's path policy allows that lies inside the envelope, within the budget |
| When a design reopens | When the implementer judges the change has outgrown the design, or a deterministic tripwire fires: a path outside the envelope, the budget exceeded, or a new execution profile triggered |
| What stays strict | Path policy and protected files, mandatory checks for the approved profiles, the primary files' rules, full-diff reviews, every merge gate, and the existing byte limits |
| Baselines | Edits apply to the parent (the approved base, or the published head when repairing a PR). Everything that judges the whole change measures the approved base to the candidate: budgets, envelope, review context, package selection and accumulated files |
| Review capacity | Before publication, the controller measures each complete review context the candidate will need. One that would not fit returns feedback to reduce the change or reopen the design to split it. Files are never omitted |
| Compatibility | A plan without an envelope has the envelope of exactly its files and the budget of its file count, which is today's behaviour |

## Plan contract

The plan gains a `scope` object, approved with the rest of the plan and therefore
part of the approval binding:

```json
"scope": {
  "paths": ["stacks-common/src/deps_common/**", "stacks-common/src/util/hash.rs"],
  "max_files": 6,
  "max_changed_lines": 300
}
```

- **`files`** become the *primary files*: the ones the design is about. Each still
  has its validation rule (`change`, `max_lines`, `required_text`), and must lie
  inside the envelope.
- **`scope.paths`**: 1–8 relative path globs (the profile trigger matcher), with the
  same path rules as triggers.
- **`scope.max_files`** and **`scope.max_changed_lines`**: the budget for the whole
  candidate against the approved base, counting files changed and lines added plus
  removed. Engine caps bound them (at most 32 files and 3,000 lines); the planner
  sets them for the design, and the reviewers judge them.

The planner skill changes its emphasis: the plan states the architecture, the
decisions, the risks and the primary files. It draws the envelope around the area the
design legitimately affects, generous enough for mechanical follow-on edits (callers,
imports, tests) and no wider. It sets a budget proportionate to the design.

## Baselines

Two inputs, used for different things:

- **The parent** is what edits apply to: the approved base, or the published head
  when repairing a PR. The workbench's local Git base stays the parent, as today, and
  edit originals come from it.
- **The approved base** is what the whole change is judged against. The workbench
  always stages the approved base snapshot (today it does so only when a profile needs
  preparation), and the cumulative change is the approved base to the candidate. That
  diff decides the budget, the envelope, the review context, the accumulated file set
  and `nextest` package selection.

So a first round adding 200 lines and a repair adding another 200 count as 400 lines
against the budget, and a non-primary file changed in the first round stays in review
and package selection after the repair. The same holds across design reopens: a
revised plan's envelope must contain every file the PR already changes.

## Implementation

- **Edits:** `validate_edits` accepts a replacement for any path the path policy
  allows that matches an envelope glob. Originals for paths outside the primary files
  are read from the parent as they are first edited.
- **Leaving the envelope:** a proposal with any edit outside the envelope is refused
  whole, before any edit is applied, as local feedback rather than by blocking the
  task. The feedback names the paths and tells the implementer to return a
  `design_issue` if the change needs them. Repeated refusals consume local repair
  rounds as any failing check does, then block.
- **Implementer judgement:** the implementer skill says to return a `design_issue`
  when the change outgrows the design: new behaviour, another subsystem, a different
  approach, or a budget it cannot meet. It must not reopen for one more call site
  inside the envelope.
- **Per-attempt edits:** the cap of 8 edits per attempt rises to the plan's
  `max_files`. The existing byte limits stay: 128,000 bytes per replacement, 256,000
  bytes per implementation response, and the export bounds.

## Checks

- **`scope`** compares the candidate with the staged approved base. It passes when
  every path changed since the approved base matches the envelope, the cumulative
  change is within the budget, each primary file has its declared status, and the
  diff is not empty. Each failure has
  a fixed message naming its cause, so the implementer can reduce the change or return
  a `design_issue`.
- **`text`** applies the primary files' rules, as today. Other changed files have no
  text rules; checks and reviews cover them.
- **Approved profiles:** the profiles a primary file, or a file inside the envelope
  at the approved base, triggers. Their mandatory checks are required, and the plan
  must select them. Preparation runs, before any candidate edit, for each approved
  profile whose selected checks need it; a profile can be approved without needing
  preparation. A candidate that changes a file triggering a profile outside the
  approved set fails `scope` as a new execution profile, a design change. No
  preparation ever starts after a candidate edit.
- **Local evidence:** `local_evidence_valid` requires the checks of the approved
  profiles plus the plan's selection, not only those the primary files trigger.
- **Package scoping** for `nextest` uses the files changed since the approved base,
  recomputed before each attempt's checks, instead of the primary files.

## Review capacity

Review context holds the before and after contents of every changed file, and
`Controller.ask` refuses a context over `max_context_bytes`, blocking the task. Two
100 KB files with a one-line change each already make about 400 KB. Identical files do
not make identical requests: the local review carries check reports, and each PR
reviewer carries its own earlier findings and dispositions. So capacity is measured,
not reserved:

- **One sizing helper** builds a review context exactly as its stage will send it and
  measures it with the same serialization and limit as `ask()`, plus a small fixed
  allowance for transport fields that are added at dispatch.
- **Before publication**, once the checks have run, the controller measures the local
  review's context and each reviewer's prospective PR review context, including that
  reviewer's open and withdrawn findings. If any would exceed the limit, the attempt
  fails with bounded feedback to reduce the change or return a `design_issue` to split
  the work, consuming a local repair round like a failing check. Files are never
  omitted from a review to make it fit.
- **At dispatch**, `ask()` still enforces the limit. Inputs can change after
  publication, for example when a writer's reply or a design reopen adds to a
  reviewer's context, so the earlier measurement is no guarantee. A PR review that
  no longer fits blocks with a capacity diagnostic naming the stage, rather than an
  unexplained failure.

## Reviews and gates

- **Review context:** the local review and both PR reviews receive before and after
  contents of every file changed since the approved base, not only the primary files,
  together with the plan's envelope and budget.
- **Design review** judges whether the envelope and budget fit the design and its
  risks. A broad envelope around consensus or wire code is a reason to revise.
- **Code review** may still reopen the design when the change drifted from it, even
  inside the envelope.
- **Merge gates:** "unapproved path" becomes "a changed path outside the approved
  envelope". The PR file set must still equal the controller's evidence, and
  autonomous-merge path limits are unchanged.
- **Revised plans** against an open PR must place every already-changed file inside
  the new envelope, rather than in the file list.

## Compatibility

A plan without `scope` behaves as today: its envelope is exactly its files and its
budget is its file count, with no line budget. Adding `scope` to the plan schema is a
bundle change: the images are rebuilt, and in-flight tasks block for operator
reconciliation.

## Testing

- An edit to an unplanned call site inside the envelope is accepted, checked and
  reviewed; the PR includes it and the gates pass.
- Across a PR repair, the budget counts both rounds' lines, and a non-primary file
  changed in the first round stays in review and package selection.
- A docs-only plan, which prepares no profile, still stages the approved base and
  measures the budget against it.
- A candidate whose files fit, but whose complete local or PR review context would
  not, fails before publication with feedback; a reviewer's existing findings pushing
  its context over the limit is one such case. No review omits a file.
- A PR review whose context grew past the limit after publication blocks with the
  capacity diagnostic.
- `local_evidence_valid` requires the approved profiles' checks.
- A proposal with an edit outside the envelope is refused whole, with feedback,
  before anything is applied; a `design_issue` then reopens the design; repeated
  refusals block after the repair budget.
- Exceeding the budget or adding a file of an unprepared profile fails `scope` with
  its fixed cause.
- Mandatory checks and preparation follow the envelope at the base; `nextest`
  package scoping follows the actual diff.
- Reviews receive every changed file; the merge gate refuses a PR path outside the
  envelope.
- A plan without `scope` keeps today's exact-files behaviour.
