# Demo target policy after pilots #33/#34

Status: approved direction; engine/config changes pending review and deployment.
This is a separate follow-up to the minimal YAML/JSON extension addition in demo-fork PR #4.
PR #4 can ship with the already-reviewed release; the broader changes stay out of that release.

Use `paths.protected_extensions: []` to allow every supported text-file extension.
Remove the demo's toolchain, formatter and script-directory protections. Keep
`.concurro/` reserved. Set the count limits to `null`; retain the documented default
scope-pattern length of 200 characters. The exact settings and legacy compatibility
are documented in [the operations guide](../concurro.md#edit-policy-and-workload-limits).

Workflows, governance documents and scripts become editable within the reviewed scope.
They receive normal design review, PR review and CI. Structural path checks, Git metadata
protection, text/mode checks, pinned approvals and aggregate transport limits remain.

## Rollout

1. Review the engine/config support separately from the output-contract and readable-output
   release. The bundle changes; a later deployment needs an idle window or explicit writer
   recovery. Do not merge the new target config before its supporting deployment.
   Large inventories use checkpoint format 5; after such a save, rollback needs an engine
   that understands that format.
2. Replace the demo fork's allow-list config with the new policy and explicit limits.
   The change affects newly enrolled tasks, not the configuration pin of an old task.
3. Replace [#33](https://github.com/concurro-demo-upstream/stacks-core/issues/33) and
   [#34](https://github.com/concurro-demo-upstream/stacks-core/issues/34) after the config
   change. Neither retry nor invalidate adopts a different configuration digest.
4. Link the old attempts and retain their artifacts, findings and spent-budget history.
   Carry unresolved findings into the replacement requirements/evidence, including
   #34's cross-fork fixture and DB-error coverage. A replacement has a new budget;
   record it as a new task rather than erasing the earlier attempts.

No deployment, pilot retry or replacement enrollment is part of this code change.
