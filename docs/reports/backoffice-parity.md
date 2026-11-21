# Backoffice parity audit, 2026-11-21

The comparison baseline is frontend `56dc58a` (before the Rust API switch),
`backoffice/tasks.py`, `review_tasks.py`, `turns.py`, `activity.py`, and
`service/github_reads.py`. Current reads use Rust; the retained Python session,
credential and command authority stays in place.

| View | Previous source | Corrected Rust / frontend path | Acceptance evidence |
| ---- | ---- | ---- | ---- |
| Overview / target issues | GitHub stage labels, issue identity, PR association, funding projections | `tasks::target_index` retains issue text, author and labels; `readTarget` joins index rows with durable tasks and historical display snapshots | Collector integration; live-loader and adapter tests |
| Contribution PRs | GraphQL check rollup and additions/deletions; open PR wins | PR detail plus paginated head check runs, durable index, `pullFromIndex` / `enrichTask`; legacy `ref`/`sha` rows supported | Collector tests include 101 checks and independent 403/404 failures; unavailable data remains unknown |
| Saved lists / failures | Last verified copy plus refresh state | `target_index_status` retains last verification and scan failure; API forwards cache metadata into the existing `SavedData` UI | Storage/collector and live-loader tests |
| Live task phases | Verified checkpoint projection | Existing Rust history supplies plan, approvals, intake questions, separate design passes, finding status and completion; frontend retains issue text and formats structured plans | Adapter tests; existing pipeline/history integration suites |
| Historical task phases | GitHub audit and detached report artifacts | One-time verified projector export → immutable `historical_display` table → writer-scoped Rust API; frontend displays frozen phases with timestamp/provenance and no task controls | Same projector-produced fixture passes Python, Rust storage/API and frontend loaders |
| Standalone reviews / reports | Frozen review snapshot, current PR, rounds, immutable report bodies | Current index refreshes observed PR state; latest attempt per reviewer/round and exact report links are retained; historical views/reports use the display import | Review adapter and historical fixture tests |
| Diagnostics | Recorded turn prompt/response, timing, workspace and native archive | Existing archive importer and writer-scoped transcript/artifact APIs; imported SQLite JSON/epoch fields now populate the original diagnostic sections | Diagnostic adapter tests; existing archive/import suites |
| Runs | GitHub attempt timestamps plus recorded turn association | Index retains start/update timestamps and names; recorded runner sessions supply contribution attribution; reruns do not borrow attempt-one ownership | Collector/storage and adapter tests |
| Activity | Recorded diagnostic turns | Writer-scoped durable Rust turn activity plus imported historical task timelines | API authorization and live-loader tests |
| Own sponsorships | Own consent, current payer and historical debits | Actual consent facts and provider slots reach the page; own debits combine Rust funding grants (excluding released contribution reservations) with the verified historical `state.credentials` ledger | Own-reader API, funding storage and adapter tests |
| Credentials / relays | Retained Python/Vault handlers | Existing credential/relay endpoints and UI stay in use | Frontend credential/relay suites; no new Python feature path |
| Configuration / operator | Target settings and Python controller operations | Existing allowlisted pinned configuration and writer-scoped Rust operational status | Existing configuration/operator integration suites |

## Historical display migration

This is an explicit one-time migration bridge, not a Python runtime read dependency.
It starts no controller, server, command client or task. GitHub reads use the Reader
App; the user token only establishes current visibility and writer authority through
the retained access checks. Exported content is private evidence, not public build output.

From the backend source checkout with its pinned Python requirements installed:

```sh
PYTHONPATH=engine:contracts/python python3 -m deploy.export_historical_display \
  --settings /path/to/backoffice-settings.json \
  --user-token-file /path/to/private-user-token \
  --deployment historical-display-2026-11-21 \
  /path/to/private-historical-display.json
```

The exporter verifies the user token through the retained backoffice's
`GitHubOAuth.user()` account lookup. Repository reads still use the existing scoped
`Access` transport; its repository boundary is unchanged.

The retained Python backoffice image does not contain `deploy/`. To use its compatible
checkpoint/projector runtime, stream the corrected release's exporter into a one-off
Python process in that container, keeping the existing private input/output paths:

```sh
docker exec -i \
  -e PYTHONPATH=/opt/concurro:/opt/concurro/engine \
  "$BACKOFFICE_CONTAINER" python3 - \
  --settings /etc/concurro/backoffice/settings.json \
  --user-token-file "$TOKEN_IN_CONTAINER" \
  --deployment "$DISPLAY_DEPLOYMENT" \
  "$MANIFEST_IN_CONTAINER" \
  < "$CORRECTED_RELEASE/deploy/export_historical_display.py"
```

`CORRECTED_RELEASE` must contain the identity-lookup correction. This command starts no
controller or HTTP server and does not replace files in the retained image. The token
and manifest paths must already be accessible to the container's configured user.

An optional `--inventory /path/to/inventory.json` selects exact source identities:

```json
[{"kind":"contribution","repository":"owner/issues","number":26},
 {"kind":"review","repository":"owner/upstream","number":44}]
```

Automatic discovery pages all visible enrolled issues and standalone review sources.
Absent checkpoints are skipped during discovery; explicit inventory entries and corrupt
or missing referenced evidence fail visibly. The export reuses the verified legacy
checkpoint/report loaders, checks the source revision around projection, and records
its checkpoint reference, source revision, state digest, reader App and collection time.
Issue and PR update times remain their original source times. Current consent is not copied into the historical view. Original credential debit facts
are exported separately, with their ledger ID, payer, provider, route/version and call count.
A missing `credentials` field is explicitly unavailable; an empty retained ledger is distinct.

On the selected Rust release, using the normal task-service configuration and database:

```sh
CONCURRO_TASKS_CONFIG=/path/to/tasks.toml concurro-tasks \
  imports display /path/to/private-historical-display.json
```

Migration `0019` creates separate display/freshness tables. Import is atomic and
idempotent for the original manifest. A changed manifest under the same deployment
and source identity is refused; use a new deployment identity for a later snapshot.
No task state, execution ownership, consent, budgets or original diagnostic archives
are changed. The latest imported snapshot per source is displayed only when no live
Rust task occupies that identity. Current source-specific writer authority is checked
before historical evidence or reports leave the Rust API. Histories remain display-only.

Run the separate existing `imports python` archive migration for native diagnostics;
display import does not synthesize missing archives. Preserve the original exports as
private migration evidence. The export bridge can be retired after source coverage has
been checked against the inventory; Rust reads only its imported records afterwards.

No host export/import, permission change, deployment or task retry was performed for
this changeset. Historical detail is locally verified but requires authorized source
export/import before it can be claimed populated in production.

## Configuration and deliberate limits

`targets.upstream_permissions` needs `checks = "read"` for head check runs, alongside
existing PR read access. The example configuration now names it. Verify the actual App
installation grant and requested token permissions before deployment; this change does
not expand a live installation. A missing permission leaves CI unknown and does not
collapse the PR list. This display rollup retains the prior CheckRun semantics; runtime
merge gates independently inspect their existing broader check/status surfaces.

Historical snapshots retain their recorded stage and approvals but do not count as
currently active work or produce new approval/retry controls. They are not resumed by
this migration. Native provider token/cost telemetry and per-job timings remain unknown
when their sources did not record them. Rust operator status describes the current Rust
projection/tasks; retired Python queues/leases and their controls are not recreated.
Source scan freshness is a target-wide completed-scan observation; a failed scan marks
all its saved lists stale, including any list refreshed before a later list failed.

Historical debit attribution uses the same verified checkpoint `credentials` mapping
that populated the Python registry's `task.usage` and `Shadow.view().charged_calls`.
Rust stores these facts separately from current consent. Personal billing filters them
to the authenticated payer and rechecks current source readability, including a reader
who lost writer access. The latest imported snapshot per source avoids counting the
same historical ledger again. A checkpoint without the ledger produces an explicit
unavailable-source notice instead of a zero or a pending consent request.

Run lists retain exact serving attempts from imported Python `serving_run` records and
Rust contribution/standalone runner sessions. An unrelated rerun never borrows the
original task/turn attribution. Historical contribution diagnostics link directly to
the imported source identity; standalone rows link to their review page, since that UI
has no separate per-turn diagnostic route. Original source archives remain accessible
through the writer-scoped imported transcript API. Current issue/PR observations enrich
historical display metadata while plans, reports, review heads and approvals remain
frozen. Reviewer approval badges and readiness use the same current-head round selector;
a changed head cannot inherit approvals from the frozen review.

## Local validation

The shared synthetic historical fixture is generated by
`backoffice/tests/test_historical_display.py:projector_manifest`, using real checkpoint
and detached-report writers/readers. Its frozen output is
`crates/tasks-postgres/tests/fixtures/historical_display.json`; the frontend test fixture
is the same JSON. No real GitHub captures are committed.

Validation results:

- `cargo nextest run -p concurro-tasks -p concurro-tasks-postgres -p concurro-backoffice-api -p concurro-backoffice-postgres --locked`: 92 passed, including the composed runtime (149.8 seconds). This consolidated run preceded the accepted historical-current observation/debit/attribution correction pass.
- After that correction: `cargo nextest run -p concurro-tasks --test target_index --locked`: 1 passed; `cargo nextest run -p concurro-backoffice-api -E 'test(historical_projector) | test(issue_tiers)' --locked`: 2 passed. These cover exact imported serving attempts, standalone run ownership, own historical charges and current reader/writer boundaries.
- `PYTHONPATH=contracts/python python3 -m unittest discover -s backoffice/tests -p test_historical_display.py -v`: 1 passed, using the real retained projectors and debit ledger.
- Frontend `npm test`: 59 passed before the consolidated correction. Final focused `node --test tests/read-adapter.test.mjs tests/live-parity.test.mjs tests/funding.test.mjs`: 33 passed; `npm run check`: zero errors/warnings; final `npm run build`: passed.
- `ruff check`, `ruff format --check` for the bridge/fixture, `cargo fmt --all -- --check`, and both repositories' `git diff --check`: passed.
- Lodestone semantic searches preceded implementation. Final similarity batches covered 35 backend and 61 frontend added/rewritten definitions. The retained projectors, source read helpers and shared current-head selector are reused; no additional equivalent implementation was found to substitute.

Browser acceptance passed in light and dark themes, including captured real PR44/39
stages/CI/counts, historical plan/report/native diagnostic navigation, exact imported
attempt 2, historical debit 3, current observed head/CI, and absence of inherited
old-head approvals or invented pending consent. Captures and local fixture servers
were read-only and are not committed. Full command/result and changed-file manifests
are attached to the implementation handoff; no commits, staging, push or deployment
were performed.
