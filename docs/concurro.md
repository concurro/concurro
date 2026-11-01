# Concurro v1

An engine for opt-in, end-to-end agentic implementation of GitHub issues. A
target repository calls it from its own default-branch workflow and owns its
charter, configuration and credentials; the engine owns the controller, roles,
skills and response contracts. The first target is `cylewitruk/stacks-core-autobot`
([example configuration](../examples/stacks-core)), a Stacks Core fork that never
writes upstream.

## What v1 does

1. A current repository writer enrolls a feature request by applying `concurro` to
   an issue. Intake checks actionability, risk and fit against the versioned
   project charter. Clarification, rejection and escalation stop implementation.
2. A planner proposes bounded files, steps and observable tests. Every participating
   provider/model identity reviews the **same exact design**, with a separate
   retained session per reviewer during cooperative revisions. The roster includes planner,
   implementer, intake QC, local reviewer and all configured PR reviewers;
   duplicate provider/model pairs vote once per round. Reviewers do not see peers' votes.
3. All roster members must approve cooperatively, then approve again in separate
   fresh sessions without prior votes or objection responses. One dissenter in
   either phase returns consolidated objections
   to the planner; a revision invalidates every prior vote. High risk escalates;
   rejection and missing intent stop. Manual mode adds writer approval after
   consensus and cannot waive the charter or unanimous design gate.
4. When approved implementation starts, the controller creates a branch at the
   approved base commit and links it in the issue's native **Development** section.
   The implementer proposes edits in a credential-free local workbench. Real checks
   and a fresh local review must pass before candidate Git objects are created,
   the branch advances, or a PR opens. The App's publication triggers hosted PR CI.
5. Claude and Codex independently review the published commit. Optional Copilot
   is an additional required reviewer. Implementation defects enter bounded fix
   rounds. Requirements or design defects reopen planning and require fresh
   unanimous approval before any more implementation or publication.
6. The controller waits for deterministic CI, review and protection gates.
   Autonomous merge remains off by default and, when explicitly enabled, is
   limited to the target's configured low-risk paths that need no build checks.
   Changes to build-checked sources (Rust in the `rust` profile) always require
   human merge.
7. Bot-owned GitHub state retains success, failure and escalation evidence.
   Future planning reads a bounded set of relevant outcomes as untrusted evidence.
   Lessons never edit or override governance, and do not imply model training.

Agent verdicts are structured reports in the issue state, not native GitHub
approval reviews. Human review threads and required approvals remain effective.
Local execution results come from the workbench, never an agent's assertion.
Hosted CI independently checks the published PR.

## Charter, roles and reusable skills

Each target keeps trusted, versioned governance in its own `.concurro/charter.json`:
stable goal and non-goal IDs, constraints and acceptance standards. The Stacks
example grounds its charter in `CONTRIBUTING.md`: correctness, focused quality and
developer/operator usability are goals; autonomous consensus changes, governance
edits, speculative work and upstream AI-code submissions are non-goals.

Scope judgments must cite known goal IDs and explain the fit. Deterministic
validation rejects unknown IDs, missing fit, unresolved concerns and acceptance
with non-goal conflicts. High-risk judgments escalate. These checks establish
report completeness and consistency; they cannot prove semantic alignment or
prevent an incorrect unanimous model judgment.

Provider-independent roles live in the engine's `bundle/roles.json`. Each
references one `bundle/skills/concurro-*/SKILL.md` and an external strict JSON
contract in `bundle/schemas/`. Shared instructions occur once in
`bundle/instructions.md`. The inference prompt contains shared governance, the
target charter and only the selected stage's skill. The skill text is also
reusable directly with other providers. All six skills point to the shared
instructions and the target charter for standalone use; the implement skill
requires exact-plan unanimous approval and
asks for missing evidence before edits. In pipeline mode, the controller supplies a
compact verified authorization attestation separately from untrusted issue/source
evidence: exact plan/input/bundle/generation binding, required identities and
successful current vote bindings. This attestation meets the implementation
approval prerequisite without requiring the model to recompute hashes. It is
withheld from independent design-review sessions. Ad hoc use does not confer
publication authority.
Python adapters contain CLI transport/parsing, rather than the workflow's prompt
prose. The controller retains deterministic permissions, validation and budgets.

The allowlisted bundle loader rejects traversal, symlinks, unapproved resources
and oversized files. It freezes/hash-binds the engine manifest, shared
instructions, skills and schemas plus the target's charter into one bundle
identity. The immutable inference image carries the engine resources; the
controller sends the exact charter text with each call, and the container must
reproduce the controller's bundle identity before it builds a prompt. An engine
upgrade therefore invalidates active approvals exactly like a charter change.
All votes bind the full plan, requirements, base, governance, participating
model identities, image IDs and design generation. Trusted bundle or
configuration drift blocks active work for operator reconciliation. Candidates
cannot edit hidden paths (including `.concurro/` and `.github/`), agent policy
files or the target's configured policy documents. Policy changes require an
operator-reviewed change. Engine CI verifies bundle packaging against the example
charter as well as the offline lifecycle suite.

## Trust boundaries

The controller runs only trusted default-branch code on a dedicated Linux runner.
It reads candidate files through GitHub's Git database API and downloads source
archives by immutable commit for inspection. A separate execution
container extracts the pinned archive, creates local Git checkpoints, and runs
checks. Candidate code never runs directly on the controller host. Remote commits
are created through the Git database API only after local checks and review pass.

Each model runs in a fresh, non-root, read-only Docker container. It has no host
bind mounts, Docker socket, GitHub controller token, SSH agent or personal home.
A disposable named volume supplies a read-only repository snapshot at the pinned
base commit, or the published head for follow-up design and PR reviews. Local
implementation and review overlay the current workbench files on that parent,
so inspection sees the current candidate rather than stale base contents.

Planner, cooperative design reviewer and implementer sessions retain their own
CLI transcript volume across turns. Scope includes repository, issue, role,
provider/model, requirements, base, governance, image and reopen cycle; a changed
scope starts a new session. Local code reviews and final design/PR reviews remain
fresh. The entry point appends changed context through explicit CLI session IDs,
never a global "latest session" lookup. Session files are private runner data;
only scope digests, turn counters and hashed completion receipts enter public audit state. No GitHub credential
is mounted or forwarded, and repository MCP tools cannot read session storage.

Completed turns can replay their cached response after a lost transport reply.
A failed or ambiguous turn, mismatched counter or missing resumed volume starts
that role in a new isolated session with full current context. Recovery retries
once immediately, charges the same model-call budget, and preserves approval
gates. Two failures block the task with a fresh session prepared for `/concurro retry`.
Completed turn receipts are checkpointed before local checks can be interrupted.
Transcripts are limited to 64 MiB at turn admission and isolated per role. They
are retained for diagnosis; operators can list only these volumes with
`docker volume ls --filter label=concurro=session` and remove selected
inactive volumes after the issue finishes. Do not prune unrelated Docker data.
Losing this cache cannot authorize progress. Prefix-cache savings are not assumed;
resuming sessions preserves context but actual cache hits depend on the provider.

All providers receive the same bounded MCP tools: `list_files`, `read_file` and
literal `search`. Protected governance and reference files remain readable; the
`editable_files` list describes only candidate edit eligibility. Archives omit
links and Git metadata; traversal, special files and excessive expansion are
rejected. Reads reject links, binary and oversized text, paginate results and
share an 80-call/1-MB retrieval budget per session. Search reports truncation and
skipped files. Tool results are untrusted evidence, never governing instructions.

The trusted MCP server starts with an empty environment and has no execution or
write methods. Only the provider CLI receives its inference credential. Claude
built-in tools are disabled; Copilot restricts tool availability to this server;
Codex disables shell tools and web search and uses the read-only sandbox. Each CLI
starts outside the snapshot in an empty directory with explicit MCP configuration,
so candidate hooks, skills and configuration cannot be automatically loaded.
The controller prepares snapshots offline without credentials and removes only
its own disposable volume when the model exits. Candidate code still runs solely
in the separate credential-free execution workbench.

Container isolation is not a VM boundary. The container can contact its model
service and holds that provider's credential. Run it on a dedicated, disposable
host with no unrelated workloads or credentials. Do not expose the Docker API.
The controller host and its trusted workflow have access to the configured secrets.
Use runner-group workflow restrictions for this exact workflow on `refs/heads/main`
where supported. Otherwise, do not share this runner with repositories or writers
you do not trust. Labels and workflow `if` expressions are not runner access controls.

The App token is limited to the target repository. It needs Contents, Issues and Pull
requests **write**, Checks and Administration **read**, plus implicit Metadata
read. Do not grant Actions, Workflows or Administration write. Do not put the App
on a protection bypass list. A GitHub App token is used because writes using
`GITHUB_TOKEN` generally do not trigger subsequent PR workflows.

All issue text, comments, source and model responses are untrusted. Plan approval,
path validation, size limits, publication, budgets and merge decisions are code,
not model instructions. Engine invariants always reject traversal, absolute and
hidden paths, `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `SECURITY.md`, symlinks,
binary content and unusual characters; containers enforce these again. The
target's `paths` policy narrows further: editable extensions, protected names,
name prefixes, directory prefixes and files (the Stacks example protects Cargo
manifests, lockfiles, `build.rs`, toolchain files and automation directories).
A plan names at most eight files. Each replacement is at most 128 KB, a response
256 KB, and context 350 KB. Oversized tasks stop safely.

## Target repository setup

A target repository contains:

| Path | Purpose |
| ---- | ---- |
| `.concurro/config.json` | Roles, models, budgets, CI names and path policy ([example](../examples/stacks-core/.concurro/config.json)) |
| `.concurro/charter.json` | Goals, non-goals, constraints and acceptance standards |
| `.github/workflows/concurro.yml` | Triggers, runner, environment, App token and the pinned engine step ([example](../examples/stacks-core/.github/workflows/concurro.yml)) |

Target-specific configuration keys:

| Key | Meaning |
| ---- | ---- |
| `repository`, `base` | The target repository and its protected default branch; must match the workflow's repository |
| `bot_login` | The controller App's `<slug>[bot]` login |
| `required_checks` | Names of the target CI check runs that must pass before merge |
| `policy_documents` | Trusted repository guidance supplied to models and protected from edits; `.github/instructions/*.md` with a matching `applyTo` is added automatically |
| `execution_profiles` | Ordered [execution profiles](design/execution-profiles.md): built-in names (`rust`) or `target:<id>` for `.concurro/profiles/<id>.json` |
| `paths` | `editable_extensions`, `protected_names`, `protected_name_prefixes`, `protected_prefixes`, `protected_files` and `autonomous_merge_prefixes` |

The workflow checks out the target's trusted commit, mints the App token and runs
`uses: concurro/concurro@<full-commit-sha>`, which executes the engine's
`engine/controller.py` with the target checkout as its working directory. Pin the
engine by full SHA and upgrade it through a reviewed change to that line.

## Credentials and model selection

Verified against official documentation on 2026-10-29:

| Provider | Supported credential in this pipeline | Model selection |
| ---- | ---- | ---- |
| Codex | `CODEX_API_KEY`; API-backed only for this public repository | `models.codex` passed as `--model` |
| Claude Code | Exactly one of `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` | `models.claude` passed as `--model` |
| Copilot CLI | Separate `COPILOT_GITHUB_TOKEN`, with Copilot Requests access and no repository write permissions | `models.copilot` passed as `--model` |

OpenAI documents advanced account-backed CI, but explicitly says not to use that
workflow for public/open-source repositories and recommends API keys for
automation. This implementation never mounts or copies Codex account `auth.json`.
Claude documents `claude setup-token` for subscription OAuth use in CI; such tokens
still have plan usage and expiry limits. Copilot documents programmatic CLI use
and model selection. CLI support does not guarantee model entitlement, unlimited
quota, or compatibility with every organization's policies.

Sources: [Codex non-interactive authentication](https://learn.chatgpt.com/docs/non-interactive-mode),
[Claude authentication](https://code.claude.com/docs/en/iam),
[Claude programmatic mode](https://code.claude.com/docs/en/headless),
[Copilot programmatic reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference),
[Copilot piped input](https://docs.github.com/en/copilot/how-tos/copilot-cli/automate-copilot-cli/run-cli-programmatically).

Edit `.concurro/config.json` only on the trusted branch. Any supported provider
can fill the planner, implementer, QC or local-review role. Defaults select Claude
planning, Codex implementation/QC and Claude local review. Claude and Codex remain
mandatory PR reviewers. Add `"copilot"` to `reviewers` to require its PR approval.
`qc_model` and `local_review_model` can select separate role models; those distinct
identities also join the design roster. Copilot selection is passed through as
`--model` for all roles.

**Live inference requires an explicit nonempty model ID for every participating
role.** The configured Codex/Claude IDs target the supervised pilot; Copilot
remains unset until enabled deliberately. Confirm access for each selected model
before activation. A CLI model ID records the requested model; it does not attest
the service's actual model weights or freeze a server-side alias. Offline fake
transports can use placeholders to test policy without provider calls.

`autonomous=true` discovers writer-enrolled issues without start commands.
`autonomous=false` requires `/concurro start` and a writer's approval after the
same intake and unanimous design gates. Neither a human command nor QC can waive
path restrictions, check requirements, branch protection or merge restrictions.

## Local development loop

Planning, design review, implementation and local review share the bundled
`concurro-validation` skill. The controller supplies its check catalog from the
same registry used by execution. Plans select named checks and declare each
file's add/modify status, optional line limit and required literal text. Every
candidate runs `diff`, `scope` and `text`; Rust changes also require the Cargo
checks. Scope compares the complete committed candidate with the approved
original files, including across repair attempts. Empty, missing, extra and
uncommitted changes fail. The workbench has a synthetic local Git baseline;
remote commit IDs identify provenance, not usable local revisions.

These checks enforce declared mechanical constraints. Reviewers still assess
whether the plan and its semantic tests satisfy the request. Each design vote
must cover every supplied request criterion, cite exact lines from the pinned
repository, and explain a considered counterexample. Missing evidence, fabricated
citations or unresolved criteria cannot authorize implementation. Reviewers
resolve or refine each of their own objection IDs independently, with concrete
closure conditions. Fresh review excludes prior responses and publication summaries.

Citation quotes may be literal fragments anywhere within their stated line;
multiline quotes must start there and remain contiguous. Evidence reads use the
same 2 MB text-file limit as repository inspection, including executable source.
The 128 KB editable-file limit is unchanged. Binary files, symlinks, Git metadata,
wrong line numbers and invented text remain invalid evidence.

Criterion extraction removes known structural labels, exact duplicate statements
and pure courtesy replies such as “Thanks!”. Substantive headings and short answers
such as “Yes” or “No” remain. The complete request stays in review context; the
controller never truncates substantive criteria to fit its 32-criterion limit.

Reviews have schema-enforced field lengths and a JSON byte allowance that scales
with criterion and objection counts. The controller also derives an allowance
from remaining checkpoint space and checks the proposed vote and resulting stage
transition before saving. Invalid evidence, oversized output and checkpoint-space
failures return fixed diagnostics to the same reviewer. Cooperative corrections
retain that reviewer's session; fresh corrections remain independent of cooperative
history and peer findings. Four output repairs per task are allowed by default
(`max_review_repairs`); repeated identical failures stop early. Every attempt
consumes the shared model-call budget, and retry replenishes neither budget nor
approvals. Transport and checkpoint-integrity failures do not enter this loop.

Malformed or unavailable validation specifications return to the planner before
design review. `max_validation_repairs` defaults to three per task, independently
of semantic plan revisions. Repeating an unchanged invalid specification stops
the loop early. These repairs retain sessions and consume the normal persistent
model-call budget; retry does not replenish either allowance.

The model proposes bounded edits and check identifiers; the controller executes
those requests and returns diagnostics on the next turn. This gives the agent an
edit/test/debug loop without giving its credential-bearing process a shell over
candidate code. Local commits are unsigned, disposable checkpoints with no remote.
The controller publishes the validated final changes as a separate remote commit.

The execution container has no provider or GitHub credentials, host directory
mounts, Docker socket, or network during candidate checks. Source is read-only
while tests run; build output and Cargo caches use per-task volumes. Local Git
metadata is mounted only into the trusted checkpoint helper, never into tests.
Each selected execution profile declares its triggers, protected manifests,
per-task caches, an optional networked preparation step and named checks. Before
any edit, a profile whose prepared checks the approved plan selects runs its
preparation from a neutral directory with network access but no credentials,
reading only its protected inputs (and, for `rust`, a read-only source view) from
the approved base commit. Candidate commands then run offline. Caches persist
across local fixes but are not shared with other tasks or the host.

Every candidate runs `git diff --check` and the scope and text checks. In the
built-in `rust` profile, plans containing Rust also require
`cargo fmt --all -- --check`, `cargo check --workspace --locked --offline`, and
`cargo nextest run --workspace --lib --locked --offline`. The agent can request
any catalogued check by identifier; arbitrary shell commands are not accepted.
Tests needing network access or writes to source fail closed and require operator
attention. This suite does not cover every consensus or integration scenario.
Preparation failures stop with an explicit environment error rather than
entering a repair loop.

### Repository Cargo configuration

Preparation runs from `/prepare`, so Cargo never discovers the repository's
`.cargo/config.toml` there; checks, running in `/workspace`, do. A repository
that relies on that configuration during download (vendored sources, source
replacement or alternative registries) needs a target profile that passes it
explicitly. Copy `bundle/profiles/rust.json` to `.concurro/profiles/<id>.json`,
then:

- set `id` and select `target:<id>` instead of `rust` (check IDs cannot repeat
  across selected profiles);
- append `"--config", "/workspace/.cargo/config.toml"` to the `cargo fetch` step;
- add vendored directories such as `"vendor/**"` to `protected`.

```json
"prepare": {"requires": ["Cargo.lock"], "source": true, "steps": [
  ["cargo", "fetch", "--locked", "--manifest-path", "/workspace/Cargo.toml",
   "--config", "/workspace/.cargo/config.toml"]]}
```

Opting in trusts the approved base's Cargo configuration during the networked
step: it can redirect downloads and name programs Cargo may run, such as a
credential provider, including executables committed to the repository.
Candidates cannot change it (hidden paths are never editable), and no
credentials are available, so authenticated private registries are not
supported.

`local_reviewer` selects one fresh reviewer (Claude by default);
`local_review_model` overrides its model, including when Copilot is selected.
Failing checks or review findings return to the implementer before any publication.
The default allows two local repair rounds per PR revision, 15 minutes per check,
and 40 minutes for the local stage. The shared 48-call budget includes every local implementation and review attempt. Expensive independent PR reviews start
only after local acceptance and publication.
Timeouts, output exhaustion and killed check processes stop immediately for
operator diagnosis; they do not trigger additional model repair turns. Cold
workspace builds under the 4-CPU/8-GB container limits have not been qualified
against these time limits. Validate that capacity before enabling Rust tasks;
the supervised pilot remains documentation-only. Per-task caches are not shared
across distinct tasks to avoid importing untrusted build state.

The issue state records budgets, checkpoint/tree identities and successful check
and review evidence. GitHub's native Development link appears when approved
implementation starts, while the remote branch still contains only the base.
A failure or design reopen can leave that base-only branch linked for inspection;
the controller does not automatically delete it. Actions artifacts retain candidate edits and bounded check
output for seven days. Treat test output as untrusted data. A crashed runner may
lose unuploaded diagnostics and local checkpoints; retries start from the last
published parent with the remaining budget. Cleanup is best effort: an abruptly
killed runner can leave task volumes behind. Use a dedicated disposable host.

## Local checks

An opt-in model benchmark uses four isolated fixtures: one valid plan and
three workflow defects, each with 13 criteria and a 162 KB source containing
indented text. It uses the normal image, repository tools and review
contract, with fresh sessions and no GitHub token or writes. Expected labels stay
outside model context. Supply only the chosen provider's dedicated credential
and a matching immutable `CONCURRO_AGENT_IMAGE`, then run it from a target
checkout, which supplies the charter and model configuration (for example
`examples/stacks-core`):

```bash
python3 /path/to/concurro/engine/design_eval.py --provider claude --output /tmp/concurro-design-eval
```

Results distinguish missed defects, false alarms and invalid responses. This is
a smoke test for review quality, not a guarantee of production correctness.

From the engine repository root (tests run against the example target):

```bash
python3 -m unittest discover -s engine/tests -v
python3 -m compileall -q engine
python3 engine/bundle.py examples/stacks-core
actionlint .github/workflows/ci.yml examples/stacks-core/.github/workflows/concurro.yml
```

The lifecycle tests use fake GitHub/model transports, perform no network writes,
and consume no model quota. They cover autonomous and manual approval, trusted
enrollment, clarification, plan revisions, publication, independent review, repair,
Copilot model selection, guarded merge, restart, lost responses, tampered evidence,
failed checks, local repair/review budgets, human objections and credential isolation.
The optional Docker test runs an actual failing Rust test, commits a repair locally,
and verifies that nextest passes for the new tree; it consumes no model quota.

Build the inference image with explicit versions:

```bash
docker build \
  --build-arg CODEX_VERSION=0.163.0 \
  --build-arg CLAUDE_VERSION=2.1.301 \
  --build-arg COPILOT_VERSION=1.0.37 \
  -f docker/agent.Dockerfile -t concurro-agent:v1 .
docker image inspect concurro-agent:v1 --format '{{.Id}}'
```

Use the returned immutable local image ID as `CONCURRO_AGENT_IMAGE`, or publish to
your registry and use `repository@sha256:digest`. Mutable tags are rejected.
Build the separate execution image, pin its ID as `CONCURRO_EXECUTION_IMAGE`, and
run the real Docker integration test before enabling the pipeline:

```bash
docker build -f docker/execution-rust.Dockerfile \
  -t concurro-execution:v1 .
export CONCURRO_EXECUTION_IMAGE="$(docker image inspect concurro-execution:v1 --format '{{.Id}}')"
CONCURRO_TEST_EXECUTION_IMAGE="$CONCURRO_EXECUTION_IMAGE" \
  python3 -m unittest discover -s engine/tests -p test_local_loop.py -v
```

Repository inspection Docker coverage can be enabled with
`CONCURRO_TEST_AGENT_IMAGE=<immutable-image-id>` when running the Python suite.
It verifies real read-only mounts, protected-reference reads and path isolation
without provider credentials.

The inference image contains only engine resources, so one image serves every
target that pins the same engine commit. Rebuild and repin it whenever its helpers
or the engine bundle change; rebuild the execution image when its helpers change.
Image changes invalidate active work rather than silently replacing its execution
environment. For a live local tick, use a dedicated App installation token in
`GH_TOKEN`, the configured provider credentials, and `CONCURRO_ISSUE=123`, then
run from the target repository checkout:

```bash
python3 /path/to/concurro/engine/controller.py
```

This command **writes to GitHub**. Do not run it concurrently with Actions; disable
`CONCURRO_ENABLED` and wait for any existing job to finish first. Do not use a
personal owner token as the controller: checkpoint artifacts and summary comments
must belong to the configured App bot. Credentials are read from the environment,
never from committed files.

## Supervised local pilot

Keep `merge_enabled=false`, GitHub auto-merge off, and `CONCURRO_ENABLED=false` at
repository and environment scope. Defer the Actions runner. `engine/pilot.py`
advances one explicit issue per invocation under a local lock; inspect its GitHub
state before invoking it again. A stage can contain bounded local repair turns.
The GitHub environment reviewer protects future Actions runs; local supervision
comes from the explicit invocation, not from that environment gate.

Verify entitlement to every configured model with the provider credentials before
starting. Copilot remains optional and needs an explicit model and its own
inference-only credential when enabled.

Create a GitHub App and install it only on the selected target repository. A
private App can be installed only on its owner's account, so an App owned by an
organization (such as `concurro`) must be public to serve a user-owned target;
installation still requires the target owner's approval and grants nothing
elsewhere. Disable webhooks and user authorization requests.
Grant Contents, Issues and Pull requests write; Checks and Administration read;
Metadata read is implicit. Do not grant workflow/actions writes or protection
bypass. Use the actual App slug plus `[bot]` as `bot_login` before publishing the
configuration. Download its private key and restrict the file to mode 600.

Store local settings outside the repository at
`~/.config/concurro-pilot/pilot.json` (directory 700, file 600): `app_id`,
`private_key_path`, `credentials_file`, `agent_image`, and `execution_image`, plus
an optional `gh_user` naming the `gh` login used for read-only administrative
preflight. Both image fields take immutable digests/IDs. These settings contain paths and
identifiers, not API keys. Dedicated provider credentials go into a separate
owner-only JSON file; the helper rejects symlinks, permissive modes, owner GitHub
tokens and ambiguous Claude authentication.

In your own terminal, generate a Claude subscription token with `claude setup-token`
and create a dedicated OpenAI API key. Then capture them without putting values
in shell arguments or history:

```bash
python3 /path/to/concurro/engine/pilot.py capture-secrets
```

Use `--claude-api-key` instead to capture an Anthropic API key. Add `--copilot` only
when configuring that provider. The command requires an interactive terminal and
refuses to overwrite existing credentials; rotate into a new private filename
with `--credentials` and update `credentials_file` in settings. Never paste secrets
into issues, source files or chat. Existing CLI login state is not copied.

Review, sign and publish the target configuration to `main` using the normal
maintainer workflow. Both checkouts must be clean, the target's HEAD must equal
remote `main`, and the target workflow must pin exactly the engine checkout's
commit before the pilot can write. Then run from the target checkout:

```bash
python3 /path/to/concurro/engine/pilot.py preflight
python3 /path/to/concurro/engine/pilot.py advance --issue 123
```

Preflight performs read-only GitHub checks and a credential-free image check. It
requires a `gh` login with administrative read access to the target, disabled
scheduled/automatic merging, no active Actions controller, explicit model IDs,
private credential files and a matching instruction bundle. `advance` verifies
the App slug, requests an installation token scoped to this one repository and
exact permissions, then revokes it on exit. It never uses the owner's token for
controller writes. A failed revocation leaves only GitHub's normal short expiry.
Each invocation creates a private directory beneath `~/.config/concurro-pilot`
for local check reports and `controller.json` (exit status and bounded captured
output with supplied credentials redacted). The terminal prints the outcome and
directory path, not the captured output. A private per-invocation controller
home and XDG directories keep `gh` device state out of the checkout and prevent
loading personal CLI configuration. Treat these diagnostics as private,
untrusted data; redaction is not a guarantee that arbitrary output is shareable.

Start with a small documentation-only issue, apply `concurro` as a writer, and
observe intake, design consensus, local repair/review, publication, independent
reviews and hosted CI. Leave the PR open when the controller reaches its disabled
merge gate. A separate deliberate human merge can exercise outcome reconciliation.
Do not enable schedules or autonomous merging as part of this pilot.

## Runner and GitHub setup

Start with a dedicated Linux x64 VM, Docker Engine, Python 3.11+, `gh`, and a
GitHub Actions runner labelled `concurro-controller`. Model CLIs are preinstalled
in the inference image. Prepublication Rust checks use the execution image;
independent PR CI also runs on hosted runners. Hetzner is a possible
host; provisioning and billing are intentionally outside this repository's scripts.

1. Review and land the target's `.concurro/` files and caller workflow on `main`.
   Keep `CONCURRO_ENABLED` unset.
2. Create a GitHub App with the permissions above, installed only on the target.
   Set `bot_login` in `.concurro/config.json` to its actual `<slug>[bot]` login.
3. Create the `concurro` environment. Restrict deployment branches to `main`.
   Initially add an environment reviewer for supervised rollout; remove that
   reviewer only when you deliberately enable unattended operation.
4. Set repository/environment variables `CONCURRO_APP_ID`, `CONCURRO_AGENT_IMAGE`,
   and `CONCURRO_EXECUTION_IMAGE`. Both images must use immutable digests or IDs.
5. Store secrets `CONCURRO_APP_PRIVATE_KEY`, `CONCURRO_CODEX_API_KEY`, and exactly one
   of `CONCURRO_CLAUDE_OAUTH_TOKEN` / `CONCURRO_ANTHROPIC_API_KEY`. Add
   `CONCURRO_COPILOT_TOKEN` when enabling Copilot. Use a fine-grained token with
   Copilot Requests permission and no repository access, not your normal `gh` token.
6. Register the dedicated runner with the labels in `concurro.yml`. Restrict its
   allowed workflow where runner groups support that feature. Prefer disposable
   hosts and short-lived registration credentials. Pre-pull both pinned images.
7. Create an `concurro` issue label. A repository writer applies it to a small pilot
   issue. This enrolls the issue for automatic QC and makes dropped events recoverable.
8. Set `CONCURRO_ENABLED=true`. Dispatch `Concurro controller` to advance an issue
   immediately, or wait for the 15-minute schedule. The controller verifies who
   applied the current label and checks their current write permission. An unknown,
   non-writer, or revoked label actor cannot authorize model calls. Public issues
   outside this queue do not consume model quota.
9. Keep `merge_enabled=false` until a supervised end-to-end pilot passes. To enable
   it, change the config **before starting a new issue**. Protect `main` with classic
   branch protection, active rulesets, or both: require the checks listed in
   `required_checks` with branches up to date, and require conversation resolution.
   Before each merge the controller reads both sources and counts only protection the
   App cannot bypass: classic protection enforced for administrators, and active
   rulesets whose `current_user_can_bypass` is `never` for the App. Requirements may
   be split across sources. With read-only Administration the App cannot see ruleset
   bypass lists, so it does not verify human bypass actors. Review those yourself, and
   keep the App off every bypass list. GitHub does not let a PR's author approve it,
   and Concurro's model verdicts are not GitHub approvals, so a ruleset that requires
   approving reviews needs an approval from another account before the App can merge.

| Command | Effect |
| ---- | ---- |
| `/concurro start` | Manual mode only: plan a new issue, or re-plan after clarification |
| `/concurro approve <full-plan-digest>` | Manual mode only: approve exactly the attached plan and base |
| `/concurro stop` | Stop advancement while retaining branch, PR and evidence |
| `/concurro retry` | Resume a stopped/blocked stage, or request reassessment after rejection/escalation; preserve budgets |

Clarification answers belong in normal issue comments. In autonomous mode, a new
or edited answer from the issue author or a current writer automatically resumes
QC. Other commenters cannot repeatedly wake model calls. The controller snapshots
the last 20 author/writer discussion comments; slash commands and bot status are
excluded. Unchanged clarification waits, rejected/escalated tasks, and stopped
tasks consume no further model calls. Manual mode still requires `/concurro start`
after clarification. A manual restart refreshes safe pre-publication requirements
and the current base while retaining spent budgets and invalidating the old plan.
Prepared publication or existing PR ancestry requires manual reconciliation.

Before publication, changed requirements or a moving base trigger intake and
fresh design approval. A prepared publication or a base move after published work
requires manual reconciliation. After publication, authorized requirement changes
can reopen design while preserving the existing PR head. The implementer, local
reviewer and PR reviewers can also return a structured `design_issue` with concrete
evidence instead of attempting changes outside the approved plan.

A reopen archives design/vote digests and objection evidence, invalidates approval
and local publication evidence, preserves the published parent and branch, and
returns to planning. The planner and every roster member see the accumulated PR
changes. The revised plan must include all accumulated changed files; otherwise
it blocks for manual reconciliation. Fresh implementation starts from the preserved
head, updates the same PR without force-pushing, and receives new local checks and
PR reviews. A foreign/closed head always stops further work.
Discussion answers while a published design is already awaiting clarification
refresh its plan and invalidate votes without spending another reopen or revision.
Changing the issue title/body still counts as a new requirements change. The
persistent model-call ceiling applies to every clarification turn.

Default limits are two plan revisions, two design reopens, two PR fix rounds,
two local repair rounds per implementation generation, and **48 total attempted
model calls per task**. Reopens consume both the separate reopen allowance and
shared plan-revision allowance. This increased call ceiling accommodates fresh
multi-model rounds and local repairs; it does not promise convergence or enough
calls for every possible path with extra distinct role models. Calls are debited
in durable GitHub state before invocation, including failures and interrupted
retries. Stop/retry, clarification and manual restart never replenish budgets.
Each stage still has local time/check limits; repeated exhaustion escalates or
blocks rather than converging indefinitely. Even an exhausted reopen/revision
attempt first retains its defect evidence and invalidates prior approvals. No subscription quota behavior is
inferred from these controller limits.

Success, observed external merges, blocked/failure, stopped, rejected and escalated outcomes are recorded
with plan/head/bundle and evidence digests in the App-owned audit artifact. At new
task creation, retrieval reads at most ten recently updated enrolled issues, their
artifacts and the first ten comments on each. Both artifact checkpoints and legacy
comment checkpoints are supported; invalid or unavailable history is skipped.
It selects at most three outcomes with matching title terms. This bounded heuristic
can miss useful history. Relevant records reach intake/planning as
`prior_outcomes_untrusted`; contradictory suggestions cannot grant permissions,
replace the charter, or bypass scope and consensus checks. No candidate auto-edits
skills, workflows or the charter in response to a proposed lesson.

A matching externally merged published head records `observed_merged` completion
evidence, including merge SHA and actor. This observation performs no merge and
does not attest controller merge gates. It captures the normal human-merge path
for Rust PRs even when the issue closes. Wrong-head merges block, and issue closure
without a confirmed merge records a stopped outcome. Closed-issue events wake
reconciliation; the scheduled scan also checks a bounded recent closed-issue window.
Controller-initiated merges and confirmed lost-response recovery retain distinct
completion provenance. Earlier outcome/evidence records remain intact.

State version 2 adds these governance and consensus bindings. Existing version 1
comments fail closed and require operator reconciliation; there is no silent
approval migration. Preserve old state and PR evidence when replacing a task.
The controller never deletes branches or force-pushes automatically. Removing or
reapplying enrollment or revoking its actor's access also invalidates authorization.

## Native GitHub progress and audit state

The current stage is shown by one `concurro:stage:*` label. The controller creates
missing status labels and updates only that namespace, preserving the `concurro`
enrollment label and unrelated human labels. These labels are derived displays:
changing one cannot grant approval, alter budgets or skip checks. A failed label
sync leaves the durable checkpoint intact and is retried on the next invocation.
The labels provide native filtering and can be used when organizing a project
board. Projects integration requires separate access and configuration; milestones
remain available for human-defined release grouping.

The bot's editable summary shows the current reviewer roster, phase and verdicts, with
links to **Concurro implementation plan** (`plan`) and **Concurro audit state**
(`generic`) issue artifacts. It does not repeat full plan steps or the JSON
checkpoint. Stage-label failures show a temporary fallback in the comment, cleared
when synchronization recovers even on idle tasks.

Separate App-authored comments record intake, each model's review in each round,
plan publication, local validation, PR reviews
and controller decisions. Plan comments link the artifact version and plan hash,
summarize changes, and identify review focus; full steps, tests and responses stay
in the plan artifact. A durable publication intent recovers interruptions between
the artifact write and comment delivery. Existing queued legacy comments retain
their original rendering. Each review identifies its provider/model, plan or
commit, phase and round. Bot comments do not become issue requirements or peer
review input. The controller never rewrites these events; GitHub users can still
edit or delete comments, so they are a conversation record, not approval authority.
A bounded checkpoint outbox references plans and reports already in state, with
content hashes protecting pending publications. Exact App-authored markers
reconcile lost responses without duplicate comments. Failed or conflicting
publications block work and independently update the summary and attention label;
restore an edited pending event before retrying. Published comments retain full
narrative text, while checkpoints keep bounded excerpts and publication receipts.
Plans, findings, unresolved objections and approval bindings remain intact; resolved
objections and unused votes are pruned. The total checkpoint size limit still applies.

The audit artifact is authoritative and contains exact commit, plan, image and
approval bindings, budgets and interrupted-operation intent. The plan artifact is
a generated view of that checkpoint; human edits are overwritten on the next
invocation, with the edits retained in artifact history. Request plan changes in
issue comments. Editing the view cannot authorize work or change approval bindings.
Both artifacts are public in a public repository. Updates retain GitHub artifact version history.
The [measured API response](artifact-measurements.md) includes only the latest 30 versions. The audit envelope
pins the verified plan artifact's ID and number so later edits remain repairable
when its first version is no longer returned. Older unbound plans are adopted only
with verified first-version provenance; missing proof stops execution.
The controller uses the issue artifacts REST API with the App's existing Issues
write permission; the preview API must be available to advance a task.

On the next save, an existing expanded or collapsed v2 comment checkpoint is
preserved as the audit artifact's first version before any newer state is written.
Only then is the comment replaced with links and a hidden artifact reference.
Lost responses recover the existing artifact; a stale comment cannot roll back
newer artifact state. Missing/replaced audit artifacts, duplicate bot artifacts,
foreign audit edits or conflicting versions stop execution without resetting
budgets or approvals.

Writes verify creator/editor attribution and read back the exact stored version.
Unchanged artifacts are not rewritten. Plans are verified on each load; repeated
saves of an unchanged view avoid another history read. Edits during an invocation
are repaired on the next invocation. Audit writes always recheck the current
version and confirm readback. GitHub's API provides no documented atomic
compare-and-swap here: the existing Actions concurrency group and local pilot lock
must still enforce one controller writer. Never run the two controllers concurrently.
Do not edit or delete the audit artifact or recovery reference manually.

Checkpoint failures set `concurro:attention` and update a separate App-owned health
notice, without replacing uncertain state or changing the last known stage label.
Reporting is best effort if GitHub is unavailable; repeated failures reuse the
notice. Diagnostics show a stable code and fixed controller text, never raw API or
provider errors. A successful invocation clears the label and marks the notice recovered.
Attention-label events and bot comments cannot trigger controller execution.

A linked Development branch is reserved only after current design approval, at the
approved base SHA, using GitHub's `createLinkedBranch` mutation. Intent is saved
first so a lost response can be reconciled without creating a second branch.
An unlinked same-name branch or an unexpected tip stops work. Candidate code is
published only after local checks and review; a deleted reservation or changed
base-only branch is never overwritten or silently recreated. Opening the PR links
it to the issue through the existing closing reference. Projects are not required
for this native Development link.

Issue label/edit events and author/writer comments can wake the controller.
Stage- and attention-label additions and removals are excluded so display updates
do not advance the controller.
Each invocation advances one stage or one reviewer. The schedule rotates through
labelled open issues and up to ten recently updated labelled closed issues in
issue-number order. It inspects at most `max_open_issues` candidates per run and
advances the first actionable one. A separate bot-owned queue cursor comment on
the oldest enrolled open issue records each visit before dispatch, so waiting
or failing issues do not starve later work. If no open issues remain, a closed
candidate hosts the cursor. A host change reuses that host's cursor, or starts
from the lowest issue number if it has none. Explicit issue invocations
do not read or move the cursor. Duplicate or malformed bot-owned cursors block
scanning for operator reconciliation.
GitHub may delay scheduled runs. Commands remain in issue comments; durable state
lives in issue artifacts, so runner disks and queued events are not the source of
truth. The App maintains one summary comment per issue; duplicate marked comments
stop processing. The queue cursor remains a separate bot-owned comment.

## Gates, CI and recovery

Local checks and local approval must authorize the published revision; missing,
failing or stale evidence blocks publication and merge. All configured PR
reviewers must approve the same controller-produced PR head. The
issue, configuration, approved plan, base and changed-file set must still match.
In autonomous mode, the bound unanimous design approval and current writer-backed enrollment
must also remain valid. The same design consensus gate applies in manual mode. In manual mode, the human approval must remain present
and its author must retain write access.
Required check runs must be unique, successful, complete, attached to that head,
and emitted by GitHub Actions. Skipped, neutral, missing, duplicate and stale checks
block. Unresolved threads (including outdated ones), human changes requests,
drafts, foreign forks, conflicts and non-clean mergeability block. The merge API
receives the reviewed SHA, and server-side strict branch protection guards races.
The controller never uses an administrator bypass.

The target owns its CI; the engine only requires the configured check names. The
Stacks example's `Rust` check runs formatting, workspace compilation and a nextest
smoke subset. That is a smoke baseline, **not the full Stacks consensus/integration
suite**; do not promote Rust changes to autonomous merge on it. Keep privileged
controller operations away from candidate build caches. Local execution uses
separate per-task Cargo and target volumes.

Publication intent is persisted before updating a branch. Retrying a lost response
reuses the expected commit; unexpected branch tips stop the run. Merge intent is
also persisted before the API call and reconciled against GitHub after interruption.
Provider failures consume the attempt budget. Raw provider output and errors are
suppressed because they may contain credentials. Malformed output, quota exhaustion
or a transport error stops safely and leaves a retryable state. Check provider and
runner health out of band; do not post credentials or raw transcripts to issues.

Disable `CONCURRO_ENABLED` and stop the active job to halt the service. Rotate the
App/inference credentials if a runner is compromised. Preserve the issue state and
PR for inspection; replacing the host should not require reconstructing progress.
