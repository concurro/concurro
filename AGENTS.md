# Concurro development and deployment

Keep requested changes uncommitted for review. Do not deploy, change App permissions,
or retry live tasks unless the user authorizes those actions.

## Documentation

For changes affecting documented behavior, configuration, interfaces, architecture or
operation, add, update or remove relevant documentation in the same changeset. Describe
the current implementation; remove obsolete guidance and fix affected links. Keep
rationale only where it explains a non-obvious choice. Internal refactors and trivial
changes need no documentation unless they make existing guidance inaccurate.

## Before proposing deployment

- Read the [deployment guide](gateway/README.md), including upstream workflow changes.
  For changed permissions or a new/updated target, verify both the installation grants
  and the token permissions requested by `service.targets.permissions()` for each role.
  Fork publication needs Contents and Workflows write; dispatch additionally needs Actions
  write. Upstream-only and read tokens must not gain Workflows write.
- Compare the approved upstream base with the fork's default branch, particularly
  `.github/workflows/`, and check applicable branch rules. Compare exact file/blob identities;
  GitHub's three-dot comparison is relative to the merge base, not the current fork tree.
  Check other fork branches for the same workflow path and contents. A successful executor run
  does not prove that the App can create a publication branch at the upstream base.
  Diagnose a ref-write 403 from service logs before suggesting retries. Do not create
  task branches manually or overwrite fork-specific setup as a workaround.
- Treat missing-permission explanations as hypotheses until supported by a specific
  GitHub diagnostic or a controlled live comparison. Do not mark a permission expansion
  ready for review/deployment based solely on generic 403 text and passing unit tests.
  Obtain authorization for isolated diagnostic writes; do not use the pilot task branch.
- State any App permission approvals or configuration migration required **before**
  deployment. Do not claim the live publication path is verified from unit tests alone.
- Check bundle changes against active tasks. A bundle change requires an idle deployment
  window or explicit writer recovery. A blocked implementation may use `/concurro retry`
  for a proven change only to the engine implementer skill, preserving design approvals.
  Other bundle changes require `/concurro invalidate`. Legacy tasks need local Git history
  to prove the narrow exception; never assume compatibility or rewrite approval bindings.
- Verify CI for the exact release and provide the full-SHA deployment command. For this
  installation, use `bash /Users/cylwit/Documents/Codex/concurro-deploy/deploy.sh FULL_SHA`
  with an outer `||` failure message; never put `set -e` in the user's interactive shell.
  Before recommending a pilot retry, verify `deployed`, the running service and the
  reusable workflow all resolve to that release. Never print gateway secrets or tokens.
