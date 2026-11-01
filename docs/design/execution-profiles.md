# Execution profiles

Status: steps 1–2 implemented (profile loader, composition, bundle binding and
profile-driven execution with the built-in `rust` profile); steps 3–5 pending. Replaces the hardcoded Rust
workbench so any repository can define how candidates are prepared and checked.

## Problem

Local execution is Rust-specific end to end:

| Place | Rust assumption |
| ---- | ---- |
| `engine/checks.py` | `COMMANDS` holds three Cargo commands; `.rs` paths make them mandatory |
| `engine/local.py` | `prepare` runs `cargo fetch --locked`; volumes are `cargo` and `target` |
| `engine/workspace_entry.py` | `/cargo` and `/target` mount points |
| `engine/policy.py` | `merge_path_allowed` treats "needs Cargo checks" as "not low-risk" |
| `docker/execution-rust.Dockerfile` | The only execution image; sets `CARGO_HOME` / `CARGO_TARGET_DIR` |
| `config.execution_profile` | Validated against `{"rust"}` |

The model-facing check catalog, plan validation contract, local evidence and
merge gate all derive from those constants.

## Goals and non-goals

Goals: a target describes its toolchains as data; the engine keeps every current
safety property; Rust behaviour is unchanged; a second profile proves the shape.

Non-goals for v1: arbitrary shell strings, network access for candidate code,
per-check images, and Node-style dependency installs into the source tree.

## Profile document

A profile is a strict JSON document, version 1:

```json
{
  "version": 1,
  "id": "rust",
  "triggers": ["**/*.rs"],
  "protected": ["**/Cargo.toml", "Cargo.lock", "**/build.rs", "rust-toolchain*"],
  "caches": ["cargo", "target"],
  "env": {"CARGO_HOME": "{cache:cargo}", "CARGO_TARGET_DIR": "{cache:target}"},
  "prepare": {
    "requires": ["Cargo.lock"],
    "source": true,
    "steps": [["cargo", "fetch", "--locked", "--manifest-path", "/workspace/Cargo.toml"]]
  },
  "checks": [
    {"id": "format", "purpose": "Run cargo fmt without modifying files.",
     "command": ["cargo", "fmt", "--all", "--", "--check"],
     "prepared": false, "mandatory": true},
    {"id": "check", "purpose": "Compile the workspace offline.",
     "command": ["cargo", "check", "--workspace", "--locked", "--offline"],
     "prepared": true, "mandatory": true},
    {"id": "nextest", "purpose": "Run workspace library tests offline.",
     "command": ["cargo", "nextest", "run", "--workspace", "--lib", "--locked", "--offline"],
     "prepared": true, "mandatory": true}
  ]
}
```

| Field | Meaning |
| ---- | ---- |
| `id` | Lowercase identifier; must equal the built-in name or the target file's `<id>` |
| `triggers` | Path globs (the scoped-instruction matcher). A plan touching one makes this profile's `mandatory` checks required |
| `protected` | Globs candidates cannot edit, added to the target's path policy. Every preparation input must be covered |
| `caches` | Cache names only. The engine creates a fresh per-workbench volume for each and mounts it at `/cache/<profile>/<name>` |
| `env` | Variables for this profile's preparation and check processes only. Values may use `{cache:<name>}` placeholders for this profile's caches |
| `prepare` | Optional. `requires` lists protected files that must exist and are copied into the read-only input view; `source` (default `false`) additionally mounts the full source read-only at `/workspace` for tools that must parse the tree; `steps` is an ordered list of argv commands run with network access from a neutral working directory |
| `checks` | Ordered checks with fixed argv. `prepared` checks need this profile's preparation; `mandatory` checks are required when a trigger matches. Argv may use `{cache:<name>}` placeholders |

Profiles name caches but never choose mount points. `/workspace`, `/state`,
`/tmp`, `/opt/concurro` and another profile's caches therefore cannot be
replaced or overlapped by construction. Environment keys must match
`[A-Z][A-Z0-9_]*`; `PATH`, `HOME`, and names starting with `LD_`, `GIT_` or
`CONCURRO_` are rejected. Profile variables never reach the trusted control helper.

Engine control checks (`diff`, `scope`, `text`) stay built in: always available,
always mandatory, run in the trusted helper, and cannot be redefined. Profile
check IDs must not collide with them.

## Selection and trust

- The engine ships built-in profiles under `bundle/profiles/` (`rust`, then `python`).
- A target lists profiles in order: `"execution_profiles": ["rust", "target:docs"]`.
  A bare name selects a built-in; `target:<id>` selects `.concurro/profiles/<id>.json`
  from the target. A target file never shadows a built-in implicitly.
- Every selected profile's text is hash-bound into the bundle identity in list
  order, like the charter, so any profile change invalidates active approvals.
- `CONCURRO_EXECUTION_IMAGE` stays a single immutable digest per target, and
  approvals keep binding it.

A target-defined profile carries the same trust as the target's own CI: it is
read only from the trusted default branch and runs only in the credential-free
workbench. That is the point of allowing it — each project knows its test
command. Target profiles get exactly the same engine-enforced preparation
isolation as built-ins and cannot disable any of it. What the engine cannot
judge is the semantics of a target's own argv (for example a pip command without
`--only-binary`); that choice is reviewed like the target's CI, and still runs
inside the isolation below.

## Composition

Profiles combine in list order. Composition fails closed at configuration load:

| Element | Rule |
| ---- | ---- |
| Check IDs | Unique across all selected profiles and distinct from control checks; collisions are rejected, not renamed, so existing Rust IDs stay stable |
| Triggers | Each profile's triggers make only its own `mandatory` checks required |
| Protected paths | Union of all profiles plus the target's path policy |
| Caches | Mounted at `/cache/<profile>/<name>`; distinct by construction |
| Environment | Applied per profile to its own preparation and checks; never merged across profiles |
| Prepare | Each profile prepares independently, in list order, only when the approved plan selects one of its `prepared` checks |
| Check order | Control checks, then profiles in list order, then each profile's declared order |
| Autonomous merge | A path is low-risk only if it matches no trigger of any selected profile |

## Check selection

The catalog offered to planners and reviewers lists every control check and
every check from every selected profile, with its profile, purpose, whether it
needs preparation, and whether the plan's paths make it mandatory. A plan's
validation contract must select all mandatory checks and may select any other
listed check. Which profiles prepare is decided from the approved plan before
the workbench starts, so an optional prepared check is always selectable. Local
runs and local evidence use exactly the approved selection. The implementer is
offered only checks the completed preparation can run.

## Preparation contract

Preparation is the only networked execution, so it is held to a stricter rule
than checks:

- It runs at workbench start, before the first candidate edit is checkpointed,
  in freshly created cache volumes. Caches written by an earlier workbench, or
  by any check, are never reused for preparation.
- It must not execute repository code: no project builds, build scripts, install
  hooks, source-distribution builds, or modules resolved from the checkout.
  Two kinds of guarantee combine here:
  - **Engine-enforced, for every profile:** steps run from a neutral, empty
    working directory (`/prepare`); inputs are a read-only view at
    `/prepare/input` containing only the profile's `requires` files, all covered
    by `protected`; the source is mounted only when the profile declares
    `source: true`, and then read-only at `/workspace` from the **approved base
    snapshot**, never the workbench parent (which may be a previously published
    candidate); caches are fresh; there are no credentials. No candidate-written
    file is ever visible to a networked process.
  - **Command-dependent:** a step that names a repository file explicitly (for
    example `python /workspace/script.py`) would still run it, read-only. The
    engine cannot judge argv semantics. Built-in profile commands are reviewed to
    only download and verify pinned artifacts; target profile commands are
    reviewed like the target's CI.
  - Built-in Python steps run the interpreter in isolated mode (`-I`), so neither
    the working directory nor `PYTHON*` variables affect module resolution.
- Built-in profiles only download and verify pinned artifacts
  (`cargo fetch --locked`; pip with hashes and wheels only). Repository tool
  configuration found by working directory (for example `.cargo/config.toml`)
  does not apply to preparation; a project that depends on it defines a target
  profile that references it explicitly.
- The workbench parent may be a previously published candidate, so preparation
  never reads it: inputs and the optional source view come from a separate
  extraction of the approved base commit.
- Helper operations and steps from all profiles share one preparation budget
  inside the existing local stage deadline; each starts only with time left. A failed, timed-out or output-limited step blocks the task as
  an environment failure; it never starts a model repair turn.
- Source stays read-only; only the profile's own caches are writable. There are
  no provider or GitHub credentials, as today.

## Invariants kept

- Candidate code never runs with network access; only preparation does, inside
  the engine-enforced preparation isolation.
- Source is read-only during checks; only declared caches are writable.
- Commands are argv lists from trusted data; models select check IDs only.
- Local Git metadata is mounted only into the control helper, which receives no
  profile environment.
- Output, time, memory, CPU and PID limits are unchanged.
- Profile `protected` paths can only narrow the editable set.

## Behaviour mapping

| Today | With profiles |
| ---- | ---- |
| `required(paths)` | Control checks + `mandatory` checks of profiles whose triggers match |
| `available(required)` | Planning: all control and profile checks (no circular dependence on preparation) |
| Development choices | `executable()`: control checks plus checks the completed preparation supports |
| Prepare when `check`/`nextest` required | Prepare a profile when the approved plan selects one of its `prepared` checks |
| `merge_path_allowed`: no Cargo checks | No profile trigger matches the path |
| `cargo`/`target` volumes | `/cache/<profile>/<name>` volumes |
| `catalog()` Rust text | Generated from profiles and control checks |

The control helper's `validation` operation currently re-runs `contract_errors`,
which needs profile data. The controller will pass the selection and file rules
it already validated; the helper re-validates only file-rule structure and paths.

## Transport

Target profile files are part of the bundle identity, so they travel like the
charter:

- Each agent call carries the charter text and the selected target profile
  texts; the agent container recomputes the identity from its built-in engine
  resources plus that text and refuses a mismatch.
- Pilot preflight passes the same texts to its credential-free image check.
- Built-in profiles ship inside the agent image as engine resources.

## Execution images

An execution image must provide `python3`, `git`, the engine helpers at
`/opt/concurro/` (`workspace_entry.py`, `local.py`, `policy.py`, `checks.py`),
user `10001`, and every selected profile's toolchain on `PATH`. The engine ships
one Dockerfile per built-in profile, sharing a helper layer. A target that
composes profiles owns a Dockerfile that starts from one toolchain base, installs
the others, and copies the helpers from the pinned engine commit; the operations
guide will include a worked Rust-plus-Python example. Operators pin the result
by digest as today.

## Python profile

Built in as `python`:

- `triggers`: `**/*.py`.
- `protected`: `requirements*.txt`, `constraints*.txt`, `pyproject.toml`,
  `setup.py`, `setup.cfg`.
- `caches`: `venv`, `pycache`.
- `env`: `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPYCACHEPREFIX={cache:pycache}`.
  Explicit compilation ignores `PYTHONDONTWRITEBYTECODE`, so bytecode goes to the
  `pycache` cache instead of the read-only source.
- `prepare.requires`: `requirements.txt`, fully pinned with hashes; no source view.
- `prepare.steps`: `python3 -I -m venv {cache:venv}`, then
  `{cache:venv}/bin/python -I -m pip install --no-input --only-binary=:all: --require-hashes -r /prepare/input/requirements.txt`.
- `checks`: `py-compile` (`python3 -m compileall -q .`; unprepared, mandatory)
  and `pytest` (`{cache:venv}/bin/python -m pytest -q -p no:cacheprovider`;
  prepared, mandatory, so `pytest` must be in the hashed requirements). Both
  write bytecode only beneath the `pycache` cache.

The project itself is never installed. Projects needing source builds, other
tools or different commands define a target profile.

## Tests

Required before profiles are considered done:

- Offline: the existing Rust suite unchanged as the regression baseline; profile
  schema and placeholder validation; composition of Rust plus a target profile;
  check-ID, environment-key and reserved-path rejection; nested protected files
  from profile globs; optional prepared check selection; shared preparation
  budget exhaustion across profiles; bundle identity and agent/pilot transport
  of target profile text; preparation working directory, input view and
  source-mount rules.
- Docker: a Python failure-and-repair cycle mirroring the Rust one, and the
  existing Rust cycle under the new cache layout. Two regressions reproduced
  during design review: a repository `venv.py` (and `pip.py`) must not shadow
  the standard library during preparation, and `py-compile` must leave the
  read-only source unchanged while writing bytecode only to its cache. A Rust
  workspace with path and vendored registry dependencies: it prepares and
  passes offline checks through a target profile that passes the approved
  base's `.cargo/config.toml` explicitly, and fails preparation under the
  built-in profile, which never discovers it (Stacks Core's configuration
  currently holds only aliases). These run only where
  Docker is available to the operator or Codex; the Rust cycle gates acceptance
  of step 2.

## Implementation steps

1. Add the profile schema, loader, composition and validation; ship
   `bundle/profiles/rust.json` reproducing today's behaviour; replace
   `execution_profile` with `execution_profiles`; bind selected profiles into the
   bundle and transport target profiles.
2. Route `checks.py`, `local.py`, `workspace_entry.py`, `policy.py` and the
   controller through the composed profiles, including selection-driven
   preparation and the shared budget.
3. Split the execution image into a helper layer plus per-profile Dockerfiles.
4. Add the Python profile, an example target and the required tests.
5. Document target-defined profiles and multi-toolchain images in the
   operations guide.

## Decisions

1. Targets may use built-in profiles and their own `.concurro/profiles/<id>.json`.
2. A target composes an ordered list of profiles.
3. Python is the second profile; Node waits for the generated-path extension.
4. Preparation never sees candidate-written files and never executes repository
   code. The engine enforces a neutral working directory, a protected-only input
   view, an opt-in read-only approved-base source view and isolated built-in
   interpreters for every profile; target profiles cannot opt out. Not naming
   repository code in argv is a reviewed property of each profile's commands.
   Built-in Python preparation is wheels-only with hashes.
5. Cache mount points and the profile-environment boundary are engine-assigned.
