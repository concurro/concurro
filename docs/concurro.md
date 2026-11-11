# Operating Concurro

The [MVP architecture](design/mvp.md) and [execution contract](design/mvp-execution.md)
govern this version. The [documentation index](README.md) distinguishes current contracts from pending proposals.
See [deployment and migration](../gateway/README.md) before updating an existing service.

## Target setup

Configure an existing upstream/fork pair in service settings, with `issues` equal to
`upstream`, `fork` or `both`. The App needs access to both repositories; writers in the
selected issue repository enroll work with the `concurro` label. Verify the
[App permissions and webhook subscriptions](../gateway/README.md#target-and-app-prerequisites)
before enrolling; granting permissions alone does not subscribe the App to events. Concurro never creates
forks or upstream branches. The current transport supports public repositories only;
it fetches source anonymously and does not give GitHub write credentials to runners.

In the fork's default branch, install:

- `.concurro/config.yaml` (or `config.json`, never both) and `.concurro/charter.json`, adapted from the example.
- `.github/workflows/concurro-executor.yml`, calling `agent.yml@deployed` with its
  centrally issued session ID and display-only task, operation and agent labels. Explicitly
  forward the repository secret `CONCURRO_GATEWAY_URL`. Store the deployment URL in that secret, not in source or
  workflow inputs. Both jobs must specify `cache-mode: read`.
- Optionally `.concurro/setup/action.yml`, receiving an absolute `base` checkout path.
  Install shared tools and restore download caches here. Workspace installs belong in
  agent workspaces. A missing cache must not prevent normal installation.
- Optional `shared.md`, `planner.md`, `design-reviewer.md`, `implementer.md` and
  `reviewer.md` under `.concurro/instructions/`.

The [stacks-core example](../examples/stacks-core/) installs the base's Rust toolchain
and suggests validation commands. Other projects can install Node, .NET or combinations.
There is no execution profile or mandatory command catalog. Configure edit policy and
optional workload limits as described below. See the [scope envelope](design/scope-envelope.md)
for approved paths, cumulative limits and large-file handling.

A task records the fork configuration commit separately from the approved upstream
source commit. Setup, instructions and caller workflow changes apply to new tasks or
existing tasks after a writer's `/concurro resume`. Resume adopts the current configuration
pin; `/concurro invalidate` restarts assessment with the already selected configuration.
An optional target `engine` pin must match the served commit. Omit it when the same
operator controls service and target.

## Standalone skill use

For ad hoc use, read the [shared instructions](../bundle/instructions.md), the target's
versioned `.concurro/charter.json`, and only the selected role's skill and response
contract from the [role manifest](../bundle/roles.json). Supply applicable trusted target
guidance and the request, plan or diff needed by that role. Apply the role's decisions
in the requesting provider; standalone use grants no pipeline transport or publication
authority. The implementation skill still requires controller-verified authorization
for an approved plan; an ad hoc invocation cannot manufacture it.

In the pipeline, Concurro supplies the shared instructions, charter, selected skill,
applicable target instructions and effective response schema. Agents should not reload
that material or unrelated skills.

## Edit policy and workload limits

New targets use an extension deny-list. An explicit empty list allows all supported
regular UTF-8 text files, including extensionless files, dotfiles, workflows, scripts
and policy documents. Binary, symlink and mode-only edits remain unsupported.

```yaml
paths:
  protected_extensions: []
  protected_names: []
  protected_name_prefixes: []
  protected_prefixes: [.concurro/]
  protected_files: []
  protected: []
  generated: ['**/Cargo.lock']
limits:
  primary_files: null
  scope_paths: null
  scope_path_characters: 200
  changed_files: null
  overlay_files: null
  workspace_files: null
```

Only `paths.protected_extensions` is required in the new policy. Other protection lists
are optional and default to empty, except `protected_prefixes`, which defaults to
`.concurro/`. Extensions use suffix matching (for example `.yaml`); files name exact
paths, prefixes name directories ending in `/`, and `protected` accepts globs.
`policy_documents` supplies reference guidance; it does not independently forbid edits
under this policy. Add any desired protection explicitly. `.concurro` and `.git` path
components remain reserved regardless of configuration, and traversal is never allowed.

| Limit | Meaning | Default |
| ---- | ---- | ---- |
| `primary_files` | Entries in the plan's primary-file list | No cap |
| `scope_paths` | Entries in the approved path envelope | No cap |
| `scope_path_characters` | Characters in each scope pattern | 200 |
| `changed_files` | Cumulative changed paths from approved base to candidate, including repairs | No cap |
| `overlay_files` | Candidate entries materialized in a runner snapshot | No cap |
| `workspace_files` | Git-status entries extracted from one implementation workspace, before filtering skipped entries | No cap |

Each limit accepts a positive integer or `null` (unlimited). Missing keys use the table's
defaults. Unknown keys, booleans and malformed values are rejected. Every agent receives
the effective policy and limits, and refusals name the relevant `limits.*` setting.
Aggregate transport, response, context and checkpoint bounds still apply.
Criteria, findings, evidence lists and report fields have no additional fixed item/prose caps. Older approved
plans retain any `scope.max_files` allowance; new plan schemas omit that field.

Existing `editable_extensions` configurations remain supported with their original
allow-list, hidden-path, governance and policy-document protections. Do not combine
`editable_extensions` with `protected_extensions`; the engine refuses mixed policies.
Switching formats or limits changes the config digest; existing tasks adopt it with `/concurro resume`. Deploy
support before merging a new-format target configuration; older engines cannot read it.

### Task budgets and execution duration

Operator-selected budgets have no engine-imposed upper ceiling. `max_model_calls`,
`max_outcome_issues` and `max_outcome_lessons` require positive integers.
`max_fix_rounds`, `max_plan_revisions`, `max_design_reopens` and `max_review_repairs`
accept non-negative integers; zero permits no corresponding repair/revision/reopen.
Booleans are not integers. Writer revision grants accept any positive integer, preserve
spent revisions and remain subject to the total model-call budget.

`turn_timeout_seconds` defaults to 1200 for intake, planning and design review. Set a
positive integer or `null` to use the runner job lifetime. Implementation and PR review
continue to use the job lifetime. Queue deadlines, ownership and heartbeat checks remain.
`max_retained_bytes` defaults to 67,108,864 bytes of uncompressed retained session files;
a positive integer changes it and `null` disables that storage allowance. The compressed
archive and exchange transport limits still apply independently.

Named model profiles and reviewer lists have no count ceiling. Review rosters still need
at least two distinct provider/model identities. Policy-document lists and path-policy
lists have no entry-count or string-length ceiling; structural path checks remain.

### Large changes and resource bounds

Unlimited counts do not remove transport or memory bounds. Workspace extraction permits
up to 8 MiB of JSON (including escaped content, paths, deletions and skipped entries),
with bounded reads before loading files. Git extraction output is limited to 64 MiB;
Git is stopped and reaped on overflow. These resource guards are separate from file-count policy.

Large changed-path inventories are commitments to pinned base/head trees, not repeated
lists in the audit or candidate report. Approval-bound inventories use the original design
parent, retained across a reopen, and must reconstruct the exact list and approval digest.
These checkpoints use wire format 5; older engines refuse them. Missing, truncated or
mismatched Git evidence stops recovery without replacing approvals.

A PR file list reaching GitHub's 3,000-file ceiling is replaced with a complete pinned-tree
comparison only after verifying that the PR's merge base is the approved base. The compare
API's 300-file list is never used as an inventory. GitHub limits recursive tree responses
to [100,000 entries or 7 MB](https://docs.github.com/en/rest/git/trees#get-a-tree), covering
the repository tree rather than only changed files. Concurro refuses truncated trees;
it does not currently walk subtrees separately, so a repository exceeding this ceiling
can block work even with unlimited file counts. Before advancing a candidate branch or
creating/updating its PR, the controller checks both the publication intent and the
projected completion checkpoint, including archived reviews,
real report-artifact links and space for publication receipts and failure diagnostics.

Review contexts retain every changed-file identity, finding and criterion. Diff previews
may shrink with an explicit instruction to inspect the pinned checkout; if mandatory
context still cannot fit, implementation is refused before publication. Workflow and
policy-document paths are added deterministically to review context, PR review focus and
the dashboard. Display previews are bounded and link the complete pinned change when shortened.

## Models and agents

Use named model profiles and assign each pipeline role explicitly:

```yaml
models:
  opus55-high:
    provider: anthropic
    model: claude-opus-5-5
    effort: high
  sol61-medium:
    provider: openai
    model: gpt-6.1-sol
    effort: medium
  sol61-high:
    provider: openai
    model: gpt-6.1-sol
    effort: high
agents:
  triage: sol61-medium
  planner: opus55-high
  plan_reviewers: [opus55-high, sol61-high]
  implementer: opus55-high
  pr_reviewers: [opus55-high, sol61-high]
```

`anthropic` uses Claude Code; `openai` uses Codex. Each profile requires a model and
an explicit effort. Choose an effort supported by that model and the pinned CLI;
Concurro passes it through on fresh and resumed calls. Profile names are identifiers,
not settings: the `effort` field determines the actual value. Unknown profile fields
and role names are rejected so misspellings do not silently change execution.

Each review list needs at least two profiles with distinct `(provider, model)` pairs.
Aliases or different efforts for the same provider/model cannot appear together in
one reviewer list. Profiles may share a vendor, including different OpenAI models.
Only `plan_reviewers` review plans; triage, planner and implementer assignments add
no implicit reviewers. Triage and planning may share a profile, including in autonomous
mode. Votes, findings, reports, transcripts and runner staffing stay separate by profile; the audit records
the resolved provider, model and effort. Changing assignments or settings invalidates
the configuration binding rather than reusing old approvals or transcripts.

JSON supports the same structure. YAML uses safe data loading, with duplicate keys
and aliases rejected. The service image installs its pinned YAML dependency; local
operators running Python directly install `service/requirements.txt` first. Neither
runners nor the exchange need the YAML package.

Existing JSON configurations with `qc`, `qc_model`, `planner`, `implementer`,
`reviewers` and provider-keyed `models` remain supported, including their existing
design roster and checkpoint identities. Their compatibility mapping explicitly sends
Claude `medium` and Codex `low`; these are Concurro defaults, not a promise about defaults
in other CLI versions. To migrate, replace
those assignments and the model map together with `models` and `agents` above; do
not mix the two formats. Keep repository, policy and budget settings unchanged.
A JSON-to-YAML-only conversion has the same configuration digest; changing profiles
is a configuration change adopted by new tasks or explicit resume. Evaluation uses
a configured profile name (legacy configurations still use `claude` or `codex`).

## Contribution loop

Design and PR reviewers assess independently of their peers. Feedback goes to the
planner or implementer, who revises the work; reviewers do not collaborate on a verdict.
Iterative design reviews retain each reviewer's own session and objections across
revisions. Fresh design reviews use no earlier review discussion. PR reviews retain
each reviewer's private session across repair rounds, supplying the full current
base-to-head diff and that reviewer's own finding history. Each head needs new votes;
retained conversations are not approval authority. Returned transcripts, including
failed turns, are stored privately for diagnosis. Failed PR reviews prepare a fresh
session for a writer-controlled retry; a lost runner may not return a transcript.
The stored phase identifier `cooperative` is retained for compatibility; the dashboard
and newly queued report labels call it `iterative`. Existing archived reports and queued
publications keep their original wording.

The issue dashboard shows intake, planning, design approval,
implementation, independent PR reviews and readiness. Independent unanimous design
approvals can skip a duplicate fresh round even after output corrections. Negotiated
revisions and restarted reviewer sessions still require fresh review. Retry renews the
correction allowance, not review independence or the total model-call budget. See the
[eligibility contract](design/design-review-corrections.md).

Design-review citations declare a unique `sources[].id`, a repository-relative file
path, a line and a literal quote. `assessments[].sources` references those IDs, not
paths or URLs. Invalid references and paths receive indexed field feedback in the
next correction and archived diagnostics. Evidence and verdict checks remain strict;
repeated invalid output can still escalate before the correction allowance is spent.

Reports are archived before
compaction. Findings have stable IDs and native threads. Only their raising reviewer profile can
retire them; ambiguous or reopened thread resolutions remain manual. PR-thread replies
do not automatically start repair work; use writer feedback on the issue followed by
`/concurro retry` as described below.

Implementation may add, change, delete or rename UTF-8 regular files inside the
approved envelope. Renames count as deletion plus addition. Binary files, symlinks and
mode-only edits are refused. The controller checks complete cumulative changes and
builds Git objects itself. Agent validation is reported, never represented as trusted CI.

The PR is created upstream as a draft. All configured agent reviewers must approve the
current head and input revision, with no open findings or pending publication. CI must
have at least one successful result and no failed or pending result. Skipped/neutral
results alone are insufficient. Concurro also reads statuses on a verified current test-
merge commit. It does not inspect branch protection or maintain an ignored-check list.

Check runs retain their suite identity; pending suites and workflows awaiting approval
also keep the PR draft. An explicitly empty, queued non-Actions suite with no conclusion
is ignored only when no fetched check run belongs to it. The App needs upstream Actions
read access to observe workflow approval waits.
CI webhooks and the periodic poll wake only readiness, never a model-stage retry. Red CI,
missing CI or workflow approval leaves the PR draft. A writer can post a failure explanation, then
`/concurro retry` to request a repair within the existing scope and round budget.
The latest new writer comment supplies the feedback; retry without new feedback
only rechecks readiness. There is no automatic log ingestion, rerun or CI repair.
After readiness, failures update the dashboard without redrafting. A human's redraft is
respected. Closed or merged PRs stop the watcher. Only humans merge.

## Commands and recovery

Issue commands require the existing writer permissions. `/concurro start` starts or
resumes manual planning; `/concurro approve <plan-id>` authorizes the recorded plan;
`/concurro retry` retries blocked work with current requirements/feedback;
`/concurro resume` continues validated work without renewing allowances;
`/concurro stop` suspends it. `/concurro invalidate` is a separate writer-authorized
operation: it accepts the current bundle/charter and restarts intake and planning,
retaining the PR, published head, base and spent budgets. Old approvals, votes and
sessions cannot authorize the new assessment. The audit records the actor, command
and old/new bundle and charter identities. For an existing PR, an issue comment
announces reassessment and asks maintainers not to merge yet. The PR's draft/ready
status is unchanged; automatic conversion back to draft is deferred. Refused
invalidation commands are consumed; retrying invalidation requires a new command.
Input-needed comments describe the applicable continuation.

When a plan revision exhausts its allowance, a writer can resume that revision with
`/concurro retry --plan-revisions N` (a positive number of additional revisions). The grant
extends the pinned allowance; it never resets spent revisions or model calls. It keeps
the plan, findings, reviewer sessions and requirements, and reserves the pending
revision without repeating intake. Reviewers must approve the revised plan, including
resolving their own objections. The model-call and design-reopen limits remain unchanged.

The command requires unchanged configuration, requirements and bundle, a matching open
PR head if present, and no pending publication. Each grant records the writer, command
and new allowance in the audit and issue conversation. Replayed commands cannot grant
twice. A grant with no exhausted revision waiting posts a notice and leaves normal work
running. At the revision limit, plain retry or start posts one notice per command pointing
to the extension command, without invalidating the design again. The older design-review
budget-exhaustion checkpoint can resume this way too.

The service's owner-only control socket provides `status`, `reconcile-once`, and
`resolve <intent> applied|not-applied <evidence> --operator <name>`. Check `--help` for
exact arguments. Resolve only after observing whether the uncertain GitHub write applied.
An unknown write prevents takeover and further writes for that task. Do not clear its
registry entry or blindly replay a dispatch.

`adopt`/`release` remain migration tools for old Actions-owned checkpoints, not a supported
execution fallback. Bundle changes block new model work, including reuse of retained
model results. They do not prevent observing PR/issue closure, CI or readiness under
the task's recorded approvals. `/concurro invalidate` explicitly starts assessment under
the current bundle.

A writer's `/concurro resume` adopts the current configuration and instructions and
continues saved work, without a release compatibility certificate. It preserves the
plan, original design approvals, findings, pending results/publication and spent budgets.
Changed unfinished sessions restart as needed; completed work remains recorded. Current
model assignments, edit policy, limits, CI requirements and setup apply to subsequent
work. Questions still need answers, findings still need resolution and normal execution
still accounts for prior spending. Rejected or escalated judgments return through intake
and spend a plan revision; finished tasks ignore resume. A team change that would orphan
an unresolved review is refused with the affected IDs, without adopting settings.
See [checkpoint-preserving resume](design/checkpoint-resume.md)
for configuration-pin recovery and the format-6 rollback boundary.

For blocked or stopped implementation work, a writer's `/concurro retry` can adopt changes
**only** to `bundle/skills/concurro-implement/SKILL.md`. The controller proves that every
other resource still matches the approved bundle; schemas, shared instructions, other
roles, charter and target instructions cannot use this exception. Configuration,
requirements, approval and any existing PR head must still match, with no pending
publication. It restarts only implementation sessions, discards the failed operation and
preserves the plan, votes, approval binding, published work and spent budgets. Unpublished
workspace progress is not recovered. A separate execution-bundle receipt records the
writer, command and old/new identities; original approval digests are never rewritten.
Further bundle changes need another explicit recovery command.

New tasks retain the original implementer-skill hash. For older tasks the controller
tries up to 128 revisions of that file in the local engine Git history, without fetching
or executing historical code. It substitutes only that hash and requires an exact match
to the task's original aggregate bundle digest. Missing history or no match leaves the
task blocked; restore the matching release or explicitly invalidate. Refused adoption
commands are consumed, so another attempt requires a new writer command.

A prepared publication finishes under its recorded approval before invalidation:
retry it first, then invalidate. Use resume to adopt configuration changes.

An explicitly requested `evaluate <issue-repository> <issue> <profile>` runs four
fresh design fixtures on ordinary runner sessions. It uses model budget and temporarily
owns that task, preserving its queue. Reports go under the service state's `evaluations/`;
no fixture model work runs on the host. Use an idle configured task; see
[design evaluation](design-evaluation.md) for fixtures and the command.

## Trust boundaries

Provider secrets, the App key and write tokens stay on the host. Runners receive scoped
gateway/session credentials and their own GitHub job identity. Native agents have a shell,
network and job-level authority; environment filtering is not a sandbox. Separate jobs
isolate roles, and server-enforced read-only caches prevent cache uploads. Do not attach
unrelated cloud trust, inherited secrets or privileged environments to the workflow.

The controller publishes only policy-validated changes and fences writes by task ownership.
A reviewer may execute candidate code in its own job, so its verdict is not trusted build
evidence. Independent reviews and project CI remain complementary signals. Gateway spend
budgets can overshoot under concurrent calls; key revocation waits during an exchange outage.

## Offline verification

Run the commands in the [README](../README.md#development). No live provider calls,
GitHub mutations or Docker execution are part of those tests. Check CI for the exact
release before deployment; offline tests do not verify live App permissions or target
configuration. A live pilot requires explicit enrollment and deployment authorization.

### Large report recovery

Oversized plans, target authority, templates and queued conversation data use the existing
immutable, digest-verified artifact storage on GitHub. New parts are sized from the shared
`protocol.ARTIFACT_RESPONSE_BYTES` transport setting (16,000,000 bytes), including JSON
escaping, response metadata and rendered-view allowance. There is no separate part-size knob.
Every body must also fit GitHub's 16,777,215-byte ceiling. Records can span parts; only the
readable presentation may be shortened. Discovery reads one artifact per page. Frozen
interrupted layouts are retained; reducing the budget can require restoring the old allowance
to finish them. Legacy queued publications keep their original encoding. Comments and human-readable
artifact views retain separate 60,000-byte allowances. Plan and PR views may be excerpted with
links to complete evidence. Canonical content is preserved across save/load and retries.
The mutable audit retains a 60,000-byte operating target, not a GitHub artifact limit. Its
JSON uses a versioned HTML comment, avoiding rendered data duplication. Reads request only
the latest version; older pages are fetched when provenance requires them. Confirmed and
pending dashboard pointers are carried forward in ordinary saves, so expired history does
not invalidate those references. Excess packed metadata moves to immutable data behind a
verified audit pointer.
History directories and unfinished tails stay in the audit; completed eight-entry blocks are
stored once unless their contents change. No past artifacts are deleted or repacked.
These layouts use the unreleased checkpoint format 7; rolling back requires an engine that
understands all format-7 records, including comment-encoded audits, dashboard witnesses and
the current shared-part renderers. An earlier format-7 engine is insufficient.

Instruction, charter, configuration and template files have no separate policy-size ceiling.
HTTP transport, model-response (300,000 UTF-8 bytes), extraction and checkpoint capacities
remain. `max_context_bytes` counts the complete constructed stage prompt: controller context,
role instructions, target charter/instructions, response schema, repository metadata and
feedback-file indexes, serialized exactly as supplied to the harness. Controller-only fields,
file contents read later with tools, CLI-added framing and retained conversation history are
excluded. The full prompt is counted even when a retained session sends only changed fields.
Paths in the prompt are relative to the runner workspace, making accounting independent of
its temporary directory name. Candidate preflight uses the same representation with its
existing dispatch allowance for prospective metadata.

This expands the setting's former context-only meaning. A previously near-capacity stage
may now need a higher operator-selected budget. Admission fails before spending a call;
the diagnostic reports the complete prompt size. The target budget is not changed
implicitly. See the [before/after measurement](reports/limits-cleanup/README.md).

REST and GraphQL readers traverse complete collections without a fixed page count. Request
sizes, HTTP deadlines and GitHub rate-limit handling remain. Outcome retrieval stops at the
operator-selected issue allowance and uses normal pagination for larger windows.
