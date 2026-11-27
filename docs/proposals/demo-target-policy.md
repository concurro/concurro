# Demo target policy after pilots #33/#34

**Status (2026-11-24): implementation complete.** Engine support and the Rust
[policy implementation](../../crates/tasks-core/src/policy.rs) preserve the configurable
protections and limits. Target configuration remains an installation-specific rollout;
this document does not establish that the original pilot tasks were resumed.

Use `paths.protected_extensions: []` to allow every supported text-file extension.
Remove the demo's toolchain, formatter and script-directory protections. Keep
`.concurro/` reserved. Set the count limits to `null`; retain the documented default
scope-pattern length of 200 characters. The exact settings and legacy compatibility
are documented in [the operations guide](../concurro.md#edit-policy-and-workload-limits).

Workflows, governance documents and scripts become editable within the reviewed scope.
They receive normal design review, PR review and CI. Structural path checks, Git metadata
protection, text/mode checks, pinned approvals and aggregate transport limits remain.

## Rollout

1. Ship the engine/config support with output-contract feedback, readable output and
   [checkpoint-preserving resume](../design/checkpoint-resume.md). Use an idle deployment
   window; do not apply the new target config before its supporting deployment.
2. Replace the demo fork's allow-list config with the new policy and explicit limits.
3. A writer can resume #33/#34 under the new settings, preserving their work, findings and
   spent budgets. This does not answer their questions or waive unresolved findings.
4. Large inventories use checkpoint format 5; resume receipts use format 6. Forward
   migrations and corrective releases must preserve every format already saved.

No deployment or pilot commands are part of this code change.
