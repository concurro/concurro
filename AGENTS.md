# Concurro development and deployment

Keep requested changes uncommitted for review. Do not deploy, change App permissions,
or retry live tasks unless the user authorizes those actions.

Recovery is roll-forward only. Do not add deployment rollback or downgrade support;
preserve existing state through forward migrations and corrective releases.

## Dependencies and hosts

Third-party Python packages are allowed; pin exact versions in the component's
`requirements.txt`. Committed material names the deployment host only as `<host>`, never
its address.

## Rust and Python development

- Write idiomatic modern Rust. Apply DRY, SOLID and KISS pragmatically; search for existing
  behavior before adding functions, types or macros. Reassess a module near 1,000 lines
  for clearer ownership, without treating that size as a hard limit.
- Keep crates focused on reusable responsibilities and encapsulate their APIs. Put SQL and
  SQLx in storage crates; use `concurro-db`'s unit of work and the shared event crates for
  common transactions and transport. Document new public APIs and non-obvious internals.
- Prefer `cargo nextest` for tests, module-level `use` imports, and `std::assert_matches!`
  for match assertions. Design module APIs and re-exports instead of `pub(foo)` visibility.
- Add no new Python features except explicit Rust-rewrite migration bridges or maintenance
  of Python components retained by the current phase.

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
  write. Upstream-only and read tokens must not gain Workflows write. Settings that name
  a `reviewer` add Issues and Checks write to upstream write tokens: confirm that
  installation grant is accepted before such settings are deployed, or no upstream
  write token can be issued.
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
  `/concurro resume` explicitly adopts current configuration and instructions while
  preserving completed work, original approval bindings and spent budgets. It requires
  no release compatibility certificate. Legacy implementer-only retry needs local Git
  history. Resume receipts require checkpoint format 6; include forward-migration
  requirements in release instructions.
- Verify CI for the exact releases. Normal updates run in the user's existing host terminal
  with `update-concurro.sh`; provide `update-concurro.sh SERVICE_FULL_SHA WEB_FULL_SHA` for
  a selected pair. The workstation `deploy/deploy.sh` remains for individual components.
  Use an outer `||` failure message; never put `set -e` in the user's interactive shell.
  Before recommending a pilot retry, verify the running service serves that release and
  the target's caller and `gateway.workflow_ref` name the same launcher ref. Never print gateway secrets or tokens.

## Quality and risk tolerance

Concurro is ordinary application software, not safety-critical software. Prefer
maintainable code, agreed functionality, representative end-to-end tests and practical
roll-forward repair. Fix reproducible material correctness and authorization defects;
keep optional hardening and hypothetical low-impact edge cases out of acceptance gates.
A visible, diagnosable failure with a practical operator remedy can be acceptable.
Consolidate reviews at meaningful checkpoints instead of repeatedly expanding the
failure matrix.

A turn or two of additional billing around withdrawal or expiration is an acceptable
operational tradeoff. Preserve consent and eventual revocation, and prevent unbounded
automatic retries or billing loops. An instantaneous zero-extra-turn cutoff is not
required; do not add synchronization machinery solely to guarantee it. This preference
supersedes stricter boundary guarantees in older plans; update affected documentation
and tests honestly. Keep simple working mechanisms rather than refactoring merely to
weaken them.

## Agent coordination and quota

Treat the user's time and model quota as constraints. Give one implementation owner
clear end-to-end goals and let it own routine testing and troubleshooting. The
coordinator steers and reviews complete workflows, not individual helpers, storage
layers or other unfinished foundations. Do not duplicate implementation, monitoring
or routine tests.

Keep development tests focused. Reserve broad independent verification for completed
workflows and full suites for final acceptance. Consolidate review findings into one
correction pass and verify fixes narrowly; do not restart broad review loops. Let the
implementer continue the next workflow during review. Escalate material design or
behavior decisions, not ordinary implementation choices. Minor polish is nonblocking.
