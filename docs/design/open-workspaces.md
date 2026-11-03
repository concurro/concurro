# Open workspaces

Status: accepted (revision 3, reviewed). Builds on [the agent sandbox](agent-sandbox.md)
and [the runner executor](runner-executor.md), and settles two questions they
leave open: agents may change dependencies, and every model works with the
target's real Git repository.

## Problem

- **Dependencies are frozen.** The rust profile protects `**/Cargo.toml`,
  `**/Cargo.lock` and `**/build.rs`, and the target's `editable_extensions` omit
  `.toml` and `.lock`. Preparation fetches only the approved base's lockfile, and
  checks run `--offline --locked`. A crate bump such as upstream
  stacks-network/stacks-core#7795 (`p256` 0.14) cannot be proposed, checked or
  reviewed. [The agent sandbox](agent-sandbox.md) lets agents fetch crates but
  keeps manifest and lockfile edits protected.
- **Models see prepared files, not the repository.** Planners and reviewers get
  one commit's files through three read-only tools (`list_files`, `read_file`,
  `search`); reviewers also get the before and after text of each changed file.
  None can run `git diff`, `git log` or `git blame`, compare with the base
  branch, or read history. Whole-file texts make large files unreviewable: each
  is capped at 128,000 bytes, and the context at `max_context_bytes`.
  [The runner executor](runner-executor.md) leaves PR reviewers on read-only
  containers.

## Principle

The implementer's result is a diff and an explanation; every merge with a
build-checked path is human-gated. What an agent did inside its workspace, a
dependency bump included, cannot reach the default branch without passing the
same checks, reviews and human merge as any other change. Capabilities are
therefore given in general form, and bounded only where something real is at
stake:

- **Files that run with repository privileges:** `.github/workflows/` and
  `.github/actions/`. A branch in the target repository runs its own
  `pull_request` workflows when published, before anyone reviews it.
- **Files that define the trusted checks' commands and environment:** the
  toolchain pin (`rust-toolchain*`), Cargo configuration (`.cargo/config*`, whose
  aliases could redefine a check's command) and nextest configuration
  (`.config/nextest*`, which can filter tests).
- **State that later trusted steps read:** caches written while candidate code
  runs are never reused by a networked preparation.

The engine's path invariant already refuses every path with a component starting
with `.`, which covers `.github/`, `.cargo/` and `.config/`. Manifests, lockfiles
and build scripts are ordinary code. A manifest can still change what the checks
exercise (workspace members, features, targets, `test`, `harness`, profiles);
that is judged explicitly by reviewers and by the human who merges, not
prevented.

## Decisions

| Question | Decision |
| ---- | ---- |
| May an agent add, remove or bump a dependency? | Yes, inside the approved envelope; design review approves it as part of the design |
| Who edits the lockfile? | The implementer, with the toolchain (`cargo update -p p256 --precise …`), never by hand-written text |
| Do manifest and lockfile changes trigger the Rust profile? | Yes: its mandatory checks run, and the change is build-checked, so it never qualifies for autonomous merge |
| How are the candidate's dependencies fetched for trusted checks? | A fresh preparation from the candidate's tree, in a Cargo home no candidate code has written; checks stay offline |
| Which packages' tests run? | The packages the change touches, plus the nearest workspace members that depend on a changed package; `check --workspace` still compiles everything |
| Supply-chain gates in Concurro | None beyond surfacing: the target's CI (`cargo audit` on the pilot fork) and the human merge decide |
| What repository do models get? | The target's real repository with full history, and the approved base and the head under their branch names |
| Where do models run? | Every role can run on runners, with a shell; the service host is an optional adapter that exposes the same repository through Git in a credential-free tool container |
| How do reviewers see the change in context? | A per-file change list with unified diffs and digests, and a dependency delta, instead of whole before and after texts |

## Dependency changes

- **Path policy:** the rust profile's `protected` list shrinks to
  `rust-toolchain*`; the dot-path invariant keeps protecting `.cargo/`, `.config/`
  and `.github/`. The pilot target adds `.toml` and `.lock` to
  `editable_extensions`.
- **Triggers:** the rust profile triggers on `**/*.rs`, `**/Cargo.toml` and
  `**/Cargo.lock`. A manifest-only or lockfile-only change therefore approves
  the profile, runs its mandatory checks, and is build-checked for the merge
  gate.
- **Envelope:** a dependency change needs its manifests and lockfiles inside the
  envelope, such as `stacks-common/Cargo.toml` and `Cargo.lock`. The design skill
  includes them when the design needs a dependency change; design reviewers judge
  that as a risk like any other.
- **Generated files:** a profile lists them (`"generated": ["**/Cargo.lock"]`).
  They count toward the envelope's file budget but not its line budget, carry no
  text rules, and have no per-file size limit; the aggregate bounds on a
  candidate's diff and on each model context still apply. A primary file cannot
  be generated.
- **Preparation:** when a profile's `prepare.requires` inputs (`Cargo.lock`)
  differ between the approved base and the candidate, the checks job (the
  workbench, until runners) prepares again from the candidate tree,
  credential-free and with network. Every networked preparation writes a fresh
  Cargo home, seeded only from a trusted source that no candidate code has
  written; the cache a check run used, or an agent's cache, is never seeded from
  or reused. Checks receive a copy of the prepared Cargo home and run offline,
  as today. A candidate's `build.rs` can write anything into its copy, including
  `$CARGO_HOME/config.toml`; no later preparation reads it.
- **Trusted seed:** on runners, the seed is the target's own default-branch
  cache. stacks-core already builds one on every push to `main`
  (`create-test-caches`: the Cargo registry, `~/.cargo/bin`, the git db and
  `target/`, under the key prefix `cargo-crates--Linux`). The target's
  configuration names that prefix and its paths per profile. Agent, review and
  checks jobs restore it with `cache-mode: read`; Concurro never saves a cache.
  Executor jobs run on the default branch through `repository_dispatch`, so a
  prefix restore can only find default-branch entries, written by the target's
  workflows. Branch scope says where an entry was saved, not which workflow
  saved it, so trusting every default-branch workflow that saves under the
  prefix is an explicit assumption about the target. The target's configuration
  names the producer's exact paths, since a restore must match the cache
  version they define. The restored copy seeds the preparation and the agent's
  own workspace. Dependency downloads clearly benefit. Whether `target/` speeds
  up ordinary `check` and `nextest` builds needs measurement: stacks-core's
  producer builds coverage-instrumented tests with its own `RUSTFLAGS` and
  features. In the workbench, until runners, the seed is an earlier
  preparation's output.
- **Test scope:** after preparation, a trusted step runs
  `cargo metadata --offline --locked` on the base and the candidate. Packages
  whose lockfile entry changed (version, source or checksum) select the nearest
  workspace members that depend on them: a reverse-dependency traversal from each
  changed package that stops at the first workspace members it reaches, through
  any number of external crates. A change to a member's manifest selects that
  member, and any other change to the workspace manifest selects the whole
  workspace. These join the packages the changed files belong to. If a change
  to a lockfile or manifest selects nothing, the whole workspace is tested; an
  empty selection never means no tests. Concurro's trusted checks thus show that
  the whole workspace compiles and that the nearest affected packages' tests
  pass; the target's CI remains the complete test gate.
- **Surfacing:** the engine computes the dependency delta from both lockfiles:
  packages added, removed and changed in version, source or checksum alone. The
  delta appears in the review context, the PR body and the issue dashboard.
- **Manifest assessment:** when a candidate changes a manifest, every review of
  it must include `manifests`: for each changed manifest, whether the change
  alters what the checks exercise (members, features, targets, `test`,
  `harness`, `doctest`, profiles, build scripts), and how. The engine refuses a
  review that omits a changed manifest.

The implementer needs a shell to run `cargo update`, so dependency changes
arrive with the runner implementer ([the agent sandbox](agent-sandbox.md),
migration step 3). Everything else above can land first and is exercised by
tests.

## Repository access

Every model that reads source works in the target's real Git repository:

| Ref | Commit |
| ---- | ---- |
| The base branch's name, such as `main` | The approved base the engine judges against |
| The PR branch's name, such as `concurro/issue-24` | The reviewed head; for a local review, the candidate commit |
| `concurro/published` (repairs only) | The PR head the candidate repairs |
| `HEAD` | Checked out at the reviewed head (the base, for planning) |

History is real, so commit identities match GitHub, and `git log`, `git blame`
and `git diff main...concurro/issue-24` work as they do for a person. A local
candidate commit is built on its parent with Git plumbing
(`hash-object -w --no-filters`, `update-index --index-info`, `write-tree`,
`commit-tree`), so its blobs hold exactly the workbench's bytes.

The repository's configuration and attributes are written by Concurro, never by
the target or an agent: no remotes with credentials, no hooks, and an
`info/attributes` of `* diff -text -filter -ident -working-tree-encoding`. That
file takes precedence over in-tree `.gitattributes`, so attributes can neither
run a program nor hide or normalize a change. The trusted diff in a review's
context is computed by the engine from the bytes themselves, independently of
Git.

- **On runners** (the destination, for every role): a session clones the
  target anonymously with blobless history (`--filter=blob:none`; the network is
  open, so blobs fetch on demand), checks out the pinned commit and creates the
  refs above. The host step verifies each ref's SHA before the model starts.
  Planners, design reviewers, the implementer and reviewers all get the
  sandbox's normal tools: a shell, the web, and builds if they want them.
  Builds a model runs are advisory; trusted evidence still comes only from
  `checks.yml`. A private target's clone uses a read-only token on the host
  step, outside the sandbox. Sessions follow the runner executor's session
  rules: the request digest binds provider, role, base and head (and the
  published head for repairs), and sessions inherit run binding at attempt 1,
  heartbeats and `cache-mode: read`.
- **The service-host adapter** (optional; also the runner executor's fallback):
  models that run on the Concurro host see the same repository through
  repository tools. Concurro keeps a mirror of each target on the host, fetched
  outside any container (anonymously for a public target). Each snapshot is a
  checkout whose object store borrows the mirror's objects through Git
  alternates. While a snapshot exists, the mirror keeps a ref to its commits
  (`refs/concurro/snapshots/<id>`), and pruning skips the mirror while any
  snapshot is active, so no borrowed object disappears.
  - **Tool container:** the repository tools, the new `git` tool included, run
    in their own container. It has no credentials and no network, a read-only
    root filesystem, and only the snapshot and the mirror mounted read-only. The
    agent container holds the provider credential but no repository; its MCP
    client reaches the tools through a socket on a volume dedicated to that one
    session, which holds only the socket: never provider homes, retained
    sessions, the Docker socket or a host control endpoint. This replaces the
    in-agent repository server (`agent_entry.py`) and the agent's snapshot mount
    (`agents.py`). Reading outside the repository therefore finds nothing secret,
    so the container is the boundary rather than an argument filter.
  - **Programs may run:** free Git arguments can start programs (for example
    `git grep --open-files-in-pager=…` launches one despite `GIT_PAGER=cat`).
    That is acceptable only because the tool container is the boundary: it gets
    the hardened runtime policy of other Concurro containers (dropped
    capabilities, no new privileges, an unprivileged user, process, memory and
    CPU limits) and a whole-container timeout that kills and removes it.
  - **The `git` tool:** any read-only subcommand (`diff`, `log`, `show`, `blame`,
    `grep`, `ls-tree`, `ls-files`, `rev-parse`, `rev-list`, `merge-base`,
    `cat-file`, `shortlog`, `describe`, `name-rev`, `range-diff`, `status`) with
    free arguments, run with `GIT_CONFIG_NOSYSTEM=1`,
    `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_LITERAL_PATHSPECS=1` and
    `GIT_PAGER=cat`, a time limit, and bounded, paged output, as `read_file` has.
    The tool image gains `git`.
  - Models on the service host get no shell tool, since plain Docker is not
    a sufficient boundary for an interactive one there. This is a capability
    choice, not a security guarantee: the boundary is the tool container.

### Review context

- `before` and `after` whole-file texts are replaced by `changes`: for each changed
  file, its path, status, the SHA-256 of its base and candidate bytes, lines
  added and removed, and its unified diff against the approved base with three
  lines of context. A diff over a per-file limit is truncated with a marker, and
  the reviewer reads the rest in the repository.
- A generated file keeps its path, status and digests; its diff is replaced by
  the dependency delta.
- The context names the real head SHA, and findings stay anchored to the head's
  lines as [PR reviews](pr-reviews.md) describes.
- Review capacity measures the full serialized context each reviewer will
  receive, its open and withdrawn findings included, as `oversized()` does now.
  The 128,000-byte cap applies only to replacements an agent writes.

## Changes to existing notes

- [The agent sandbox](agent-sandbox.md): "manifest and lockfile edits stay
  protected" and the open question on dependency changes are settled here.
- [The runner executor](runner-executor.md): every role can run in sessions on
  runners, not only the implementer and local reviewer. Its caller and
  reusable workflows set `cache-mode: read` instead of `none`, which settles its
  cache question, and the service-host adapter above is the form its fallback
  host takes.
- [The scope envelope](scope-envelope.md): generated files are exempt from the
  line budget.

## Rollout

| Step | Work | Done when |
| ---- | ---- | ---- |
| 1 | The service-host adapter: target mirrors with snapshot refs, alternates-backed snapshots, the tool container, the `git` tool, the `changes` context | Every role can run `git log`, `git blame` and `git diff main...<head>` on real history; a 200 KB file with a one-line change is reviewable |
| 2 | Dependency plumbing: triggers, path policy, generated files, fresh-home preparation, dependency-aware test scope, dependency delta, manifest assessment | A lockfile change at the pilot's scale (about 150 KB) passes the workbench end to end |
| 3 | Sessions on runners for every role, with the runner executor's session rules | A planning turn and a PR review each run on a runner with a blobless clone and the refs above |
| 4 | Pilot: upstream #7795 on the fork once the runner implementer exists | The implementer bumps `p256` and migrates `secp256r1.rs`, and the PR reaches handoff with its dependency delta shown |

Steps 1 and 2 are independent. Step 1 is the host executor's form, which the
runner executor keeps as its fallback, not a stage that runners replace.

## Tests

- `git` tool: base and head diff by branch name, `log` and `blame` on real
  history; literal pathspecs; repository attributes marking a file `binary`,
  `-diff` or `text eol=crlf` neither hide nor normalize the change.
- `git diff <repository-file> <outside-file>`, and every other way of reading
  outside the repository, finds only the tool container's files; the agent
  container has no repository mount, and the tool container no credential or
  network.
- With distinct synthetic secrets in the agent container's environment, its
  provider home and its retained-session volume, no tool call (Git arguments
  that start programs included) retrieves any of them.
- A session's tool container cannot reach another session's socket, and a
  hung tool call ends with the whole tool container removed.
- A mirror maintenance run while a snapshot is active removes none of its
  borrowed objects.
- A local candidate commit's blobs equal the workbench's bytes.
- A repair review sees `concurro/published` and the candidate.
- `changes` replaces whole texts, carries digests, truncates an oversized diff with
  a marker, and shows a lockfile as path, status, digests and delta, including
  a checksum-only change.
- A candidate's `build.rs` that writes `$CARGO_HOME/config.toml` during checks
  does not affect the next preparation.
- Executor jobs, caller and called, use `cache-mode: read`: they restore only
  default-branch entries under the configured prefix and paths, and saving
  fails.
- A lockfile change at about 150 KB is prepared from the candidate tree and
  checked offline; nextest covers the nearest workspace members of the changed
  package, also when they reach it only through external crates; a lockfile
  change that selects no member tests the whole workspace.
- A manifest-only change with no dependency delta runs the Rust mandatory checks
  and is build-checked; so is a dependency bump.
- Edits to `rust-toolchain.toml`, `.cargo/config.toml`, `.config/nextest.toml` or
  `.github/workflows/` stay refused (the last three by the existing invariant);
  edits to `Cargo.toml`, `Cargo.lock` and `build.rs` inside the envelope are
  accepted.
- `Cargo.lock` counts toward the file budget and not the line budget.
- A review of a manifest change without its `manifests` entry is refused.

## Open questions

- How deep runner clones should go if blobless history proves slow on
  stacks-core.
- Whether other profiles need generated files beyond lockfiles, and their own
  dependency-aware test scope.
