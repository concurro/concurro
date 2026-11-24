# Runtime prompt bundles and intake remarks

## Frozen candidate

- Backend base: `f7cfbd2191f8f8a2bd2595460ecfcde88804a38b`.
- Frontend base: `8a65807ec39dcdd4904b094261d315c640b335db`.
- Both changesets are unstaged and uncommitted. No push, deployment or live call.
- The 32 preexisting proposal/retrospective Markdown edits are preserved separately.
- Candidate file inventory and SHA-256 values: `/private/tmp/concurro-runtime-correction-candidate-files.json`.
- Backend implementation inventory digest: `7d9be1065c9941a5ec27b6de18ae8c43be68de8f838be6170c219363693ee547` (90 files, excluding this report and the 32 preexisting documents).
- Frontend inventory digest: `68605f37a440d0fc5650fa1ef617225c2de9153a8fec156225f49382b94e7ef3` (6 files).

## Behavior

Intake emits advisory `remarks`; historical `concerns` remain parseable without rewriting
stored reports. The latest accepted proceed report supplies `intake_remarks` to planning,
design review, implementation and PR review, including corrections. Its immutable attempt
prompt supplies the assessed base and requirements. Different base or requirements suppress
stale remarks; configuration or bundle adoption alone does not. Remarks add no approval
condition. Reviewer concerns and objections retain their existing contract. Raw report events
and the frontend preserve both historical and current intake reports.

Task initialization and explicit resume read an operator-owned runtime bundle and freeze its
exact resource bytes with the initialization snapshot. The bundle digest retains the existing
Python-compatible per-path SHA-256 identity. Each new attempt carries those bytes to the
retained Python runner and stores its rendered prompt. Existing tasks and ordinary retries
continue with their frozen bytes after file changes or restart. Implementer-only retry compares
resource maps and accepts only the implementer skill change.

Legacy snapshots resolve only a complete matching digest from current files or Git history;
the image retains its history. Missing provenance gives an explicit restore-or-resume remedy.
Earlier inline controller wording is an immutable application resource shared by Rust
(`include_str!`) and the retained Python runner. It is absent from operator-managed bundles.
One `ResolvedBundle` per attempt verifies resource bytes and parses controller addenda once;
role helpers, prompt/schema rendering and request serialization consume that same object.
Intake counts the one rendered prompt/schema pair instead of rebuilding it.
Already-admitted payloads are unchanged. No templates, fragments or network prompt source
were added. See [operation and first-release migration](../../crates/tasks/README.md#runtime-prompt-bundles).

Strict Clippy is enforced in CI through `cargo clippy`, inheriting the pinned
`rust-toolchain.toml` (currently Rust 1.100.0). All workspace crates already inherit edition
2024. Lint fixes name SQL query row shapes, group existing operation inputs, and apply local
idiomatic simplifications. No lint suppression was added. String-valued sponsor IDs retain
explicit string comparisons; fixture URL IDs are parsed numerically.

## Original full verification

The following broad checks completed before the focused corrective pass below. Unchanged
frontend and full engine/workspace suites were not rerun for the correction.

| Check | Result | Evidence |
| ---- | ---- | ---- |
| `cargo +1.100.0 nextest run --workspace --locked --test-threads 2 --no-fail-fast` | 282 passed; 1 existing ignored test | `/private/tmp/concurro-runtime-prompts-workspace-nextest.log`; run `4668b9cb-b8eb-49f6-8469-e6299e7ea346` |
| Strict workspace/all-target Clippy, `--locked -- -D warnings` | Passed | `/private/tmp/concurro-runtime-prompts-clippy-frozen.log` |
| `cargo +1.100.0 build --workspace --all-targets --locked` | Passed | `/private/tmp/concurro-runtime-prompts-build-final.log` |
| Real PostgreSQL remarks provenance, historical raw bytes and report event | Passed in focused and workspace runs | `/private/tmp/concurro-runtime-prompts-provenance-final2.log`; run `631476b4-9fc4-4d2c-b396-c8bd710a0792` |
| Real task binary through Start → Ready with restarts | Passed twice; final 131.570s | Workspace log; focused `/private/tmp/concurro-runtime-prompts-runtime.log` |
| Runtime bundle files change without rebuild; frozen snapshot serialization/restart, legacy recovery, skill-only proof | Passed | Workspace and `/private/tmp/concurro-runtime-prompts-unit-final.log` |
| Python/Rust bundle digest and large feedback-file prompt oracles | Final narrow 2/2 passed | `/private/tmp/concurro-runtime-prompts-final-oracles.log`; run `46ecd2d0-1643-4c0a-b6cf-e36580a0bb2f` |
| Retained Python engine, including native frozen-bundle execution and malformed QC | 1,277 tests across 105 files passed in aggregate | `/private/tmp/concurro-runtime-prompts-python-engine-final.log` plus three dependency reruns below |
| Retained diagnostics | 6/6 passed | `/private/tmp/concurro-runtime-prompts-python-diagnostics.log` |
| Frontend test/check/build | 65/65, zero diagnostics, build passed | `/private/tmp/concurro-runtime-prompts-frontend-tests-final.log`, `-frontend-check-final.log`, `-frontend-build.log` |
| Task image build and packaging smoke | Passed | `/private/tmp/concurro-runtime-prompts-docker.log`, `-container-smoke.log` |
| Rust formatting, Ruff check/format, both `git diff --check`, empty indexes | Passed | `/private/tmp/concurro-runtime-prompts-ruff-final2.log`, `-ruff-format-final2.log`; final local checks |

The full Rust run covers contributed credential failure, withdrawal, standalone commands,
recovery, projection and gateway/exchange tests affected by lint cleanup. The runtime workflow
runs from outside the source working directory with an explicit bundle path, changes the
on-disk shared instructions after intake, restarts the binary, and checks all four downstream
prompts still contain the original instructions and applicable remarks.

The default Python interpreter lacked the already-pinned PyYAML dependency: 102 engine files
passed; three files failed only at import. Those three passed using the existing pinned CI
venv: `-python-test_launcher.log` (4), `-python-test_workflow_secrets.log` (3), and
`-python-test_mcp_servers.log` (9), all under `/private/tmp/concurro-runtime-prompts`.
No product change was made for that environment failure. Earlier fixture-shape failures and
strict-lint failures remain in the corresponding initial logs; their corrected tests passed.
The image smoke checks its configured bundle directory, readable current/legacy resources,
UID 65533, and absence of a shared instruction phrase in the compiled executable. It is a
packaging check; the complete lifecycle runs the native task binary with disposable services.

## Focused corrective verification

The correction resolves bundles once at attempt construction (once per reviewer batch)
and passes the verified resource set through every role and the common runner envelope. Its map is read-only outside the
bundle API. Controller addenda are parsed once in both runtimes. Historical wording comes
from the same original bytes in `engine/resources/legacy-controller.json`, with the original
SHA-256 `22ab6a49b333684f2d82c992d653ab705105180f57a49d7dbb651e38d6c94952`.
Neither a current operator bundle nor a restored historical bundle needs a legacy helper file.

Substantive multi-field joins now use named `FromRow` projections; query aliases make their
column bindings explicit. Existing `RunnerSession` and `PrReviewAction` DTOs directly derive
`FromRow`, avoiding duplicate mapping types. Short tuple aliases remain for simple query
results and the unchanged persisted reopen-finding JSON shape. No database migration or wire
format change was introduced. Touched design, implementation, contribution PR and standalone
PR builders use documented input structs; the common envelope takes one input object.
The CI command inherits the pinned toolchain, intake wording says “advisory remark”, and
module imports plus the implementation preflight indentation are normalized.

| Corrective check | Result | Evidence |
| ---- | ---- | ---- |
| Task unit tests, including one resolved legacy object reused after its source pin changes, all-stage Python parity and exact prompt sizing | 21/21 | `/private/tmp/concurro-runtime-correction-unit.log`; run `ad249883-00e4-4824-99ce-52f02d4aff71` |
| Real PostgreSQL task storage, named row decoding, remarks provenance, recovery and withdrawal | 40/40 | `/private/tmp/concurro-runtime-correction-postgres.log`; run `32706385-0043-4250-93bf-0f4f0c5dd05a` |
| Runtime/standalone/Git publication/runner/sponsorship integrations, two test threads | 8/8; full restart case 144.866s | `/private/tmp/concurro-runtime-correction-lifecycle.log`; run `b81abc8d-e11e-4675-98d0-045aa3634338` |
| Retained Python native runner and legacy wording with an empty operator root | 9/9 | `/private/tmp/concurro-runtime-correction-python-runner.log` |
| Retained intake and diagnostics | 22/22 and 6/6 | `/private/tmp/concurro-runtime-correction-python-intake.log`, `-python-diagnostics.log` |
| Strict workspace/all-target Clippy (`-D warnings`), formatting and all-target build | Passed | `/private/tmp/concurro-runtime-correction-clippy-final.log`, `-build.log` |
| Corrected task image and operator/application prompt separation smoke | Passed; no operator legacy file; immutable wording present/current wording absent in binary | `/private/tmp/concurro-runtime-correction-docker.log`, `-container-smoke.log` |
| Ruff check/format, parsed CI Clippy command, both diff checks and empty indexes | Passed | `/private/tmp/concurro-runtime-correction-ruff.log`, `-ruff-format.log`; final local checks |

The first corrective strict Clippy run exposed the existing four-element JSON finding tuple
inside the new named recovery row. Naming that preserved tuple shape resolved the warning;
no suppression was added. Its failed log remains `-clippy.log`. Final changes after the focused
suites were limited to import/indentation tidy, equivalent shorthand/tuple-alias naming and
reverting one unrelated documentation-only edit. The final strict Clippy check covers them.

One updated Lodestone similarity batch checked 119 corrective definitions. Exact checkpoint
input and results are `/private/tmp/concurro-runtime-correction-definitions.json` and
`/private/tmp/concurro-runtime-correction-similarity.txt`. The final diff omits the incidental
documentation-only edit from that input. Matches included the reused runner/action DTOs,
existing snapshot/retained recovery and the shared role envelope. No competing per-attempt
bundle resolver was present. Frontend source hashes are unchanged from the original pass.

## Reviewer-batch follow-up

Design and PR-review admission now resolve the bundle before iterating over reviewers.
Their input objects and the common runner envelope borrow that same verified bundle.
Implementation preflight retains the lookup error in its persisted blocker and advises
restoring the recorded files/history or an authorized resume, rather than an ineffective
implementer-only retry. The diagnostic branch was checked in source; the existing tests
below cover bundle validation and successful lifecycle execution, not a live recovery.

- Task unit tests: 21/21, `/private/tmp/concurro-bundle-followup-unit.log`.
- Restarting lifecycle integration: 1/1 (148.886s),
  `/private/tmp/concurro-bundle-followup-runtime.log`, run
  `25749b51-69cd-4027-9510-d1d5e3201f1e`.
- `cargo clippy --workspace --all-targets --locked -- -D warnings`: passed,
  `/private/tmp/concurro-bundle-followup-clippy.log`.
- Formatting and diff checks passed. One Lodestone batch checked the 11 changed definitions
  in `/private/tmp/concurro-bundle-followup-definitions.json`; existing shared admission,
  payload and bundle APIs remain reused.

## Committed-HEAD test correction

The bundle regression failed on committed `5c47358`: it loaded `HEAD` as a historical
bundle but asserted pre-rename wording. Earlier successful runs used `f7cfbd2` as HEAD.
The reproduced failure is retained in `/private/tmp/concurro-head-bundle-before.log`.

History recovery now compares the recovered committed resources without assuming their
wording. Legacy controller/schema assertions use an explicit no-controller resource set
and `crates/tasks/tests/fixtures/legacy-intake.json`, copied unchanged from
`f7cfbd2191f8f8a2bd2595460ecfcde88804a38b:bundle/schemas/intake.json`.
The fixture needs no old Git objects at test runtime. Production code is unchanged.

- Task unit suite: 21/21, `/private/tmp/concurro-head-bundle-after.log`.
- Regression with process-local `GIT_SHALLOW_FILE` at `5c47358` (one visible commit):
  1/1, `/private/tmp/concurro-head-bundle-shallow.log`. Repository history is unchanged.
- Strict workspace/all-target Clippy: passed, `/private/tmp/concurro-head-bundle-clippy.log`.
- Isolated restart lifecycle: 1/1 (148.931s), `/private/tmp/concurro-head-bundle-runtime.log`.
  The separately reported full-run failure has no retained output, so its cause remains unknown.

## Reuse and limits

The original Lodestone similarity batch checked 251 new or changed named definitions. Final source snippets:
`/private/tmp/concurro-runtime-prompts-definitions.json`; results:
`/private/tmp/concurro-runtime-prompts-similarity.txt`. The final snippets include the later
oracle-golden and import-only cleanup; no new named definition followed that batch.
Matches included existing snapshot,
recovery, retained-session, payload and Python oracle code, which remains reused. Query row
aliases retain their owning storage module; no competing Rust runtime bundle loader existed.

No live model/provider call, production data migration or deployed image was exercised.
Before using runtime bundles, pin the target engine to a runner release with frozen-bundle
support. Later prompt updates require neither a binary nor runner rebuild. Changing the
bundle environment path needs a service restart; replacing complete files at the selected
path is picked up by new initialization/resume reads. Existing snapshots retain their bytes.
