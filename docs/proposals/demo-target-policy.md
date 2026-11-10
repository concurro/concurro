# Demo target policy after pilots #33/#34

Status: proposal only. No configuration, charter, deployment or pilot recovery has
been changed. This is separate from the implemented output-contract feedback fixes.

## Problem and minimum correction

Both [#33](https://github.com/concurro-demo-upstream/stacks-core/issues/33) and
[#34](https://github.com/concurro-demo-upstream/stacks-core/issues/34) need to update
`docs/rpc/openapi.yaml` for RPC behavior changes. The target's contribution guidance
requires that documentation; its pinned extension allow-list excludes YAML.
The block is configuration, not a charter conflict or a reason to omit the docs.

The smallest change is to add `.yaml` to the fork's `.concurro/config.yaml` under
`paths.editable_extensions`. Add `.yml` and `.json` as well if permitting the usual
documentation/schema formats, rather than only these two tasks. No App permission
change is needed. Existing dot-path and protected-file checks still exclude
`.github`, `.concurro` and policy documents even when their extension is allowed.

#33 also hit planner limits: publication bullets over 300 characters, more than
eight primary files and more than eight scope paths. Those are separate from target
policy. The new feedback exposes them together; primary files need not list every
anticipated edit, and narrow directory/glob scopes can cover related tests. Do not
omit necessary work to fit the lists or assume that fixing YAML alone completes #33.

## Prefer less incidental restriction

For realistic upstream work, prefer ordinary regular UTF-8 text inside the reviewed
scope, with explicit protections for governance and automation. File extensions are
a poor proxy for either safety or reviewability: `.rs` and `.toml` already allow
executable build logic, while YAML may just document the API.

| Restriction | Recommendation |
| ---- | ---- |
| Extension allow-list | Replace with an explicit all-text mode in a separate engine/config change; retain list mode for targets that want it. A possible spelling is `editable_extensions: all-text`. It is **not supported today**; null or a missing key must never enable it. |
| Blanket `tools/`, `contrib/`, `scripts/` exclusions | Remove for the demo: its CI runs `cargo fmt` and `cargo nextest` with read-only contents permission and no secrets. Re-check real upstream CI for trusted script execution before applying this policy there. Executable-file/mode restrictions remain separate. |
| Toolchain and formatter protections | Keep explicit for now; changing validation infrastructure deserves a separate policy decision. |
| Governance, workflow and Concurro configuration protections | Keep. Changing ordinary code must not change the instructions or automation authorizing that work. |
| Path traversal, Git metadata, symlink/binary/mode restrictions | Keep the engine checks. A broader target policy does not bypass them. |
| Reviewed envelope, cumulative file count, immutable bindings, independent review and CI | Keep. These define what the task may publish and when it is ready. |

Do not simply delete `paths` or clear `editable_extensions`: `controller.validate_config`
requires the current fields and a non-empty extension list, and `policy.path_allowed`
implements suffix matching. An explicit all-text mode needs matching parser, planner
context, candidate and publication tests. It must not make an empty/malformed list
silently mean unrestricted access. The blanket hidden-path rule also remains; relaxing
it for ordinary dotfiles would be a separate change with explicit protected paths.

## Recovery and checks

1. Review and merge the chosen policy update before enrolling replacements. Prefer
   the small YAML/JSON update if the broader engine work is not ready.
2. Test that `docs/rpc/openapi.yaml` is editable and that workflows, `.concurro`,
   policy documents, traversal and symlink/binary edits remain forbidden. Test both
   planner validation and complete candidate publication, including an existing task
   whose config digest no longer matches.
3. Start replacement tasks pinned to the new configuration. Current
   `CommandHandler.invalidate_task` rejects config-digest changes; neither `/concurro retry`,
   `/concurro invalidate` nor a prose exception adopts the new file policy.
4. Preserve the old tasks' artifacts and make their unresolved findings explicit in
   the replacement's requirements/evidence: #34's cross-fork fixture and DB-error
   coverage still need review. Do not turn the failed run into an approval or erase
   its spent-budget history. A new task has a new budget; record that as a new run.

The output-contract schema/skill edits also change the bundle. They are not the
implementation-only retry exception. Existing tasks need explicit bundle recovery
before continuing; any invalidation must account for its reset of design votes and
objections. No such recovery is authorized or performed by this change.
