# Phase 4 Slice 4 evidence

Implemented from signed Slice 3 commit `9ae3da1` and frontend baseline
`e644260`. No deployment, live GitHub write, App permission change, provider
login or task retry was performed during implementation.

## Browser acceptance

The frontend's `npm run test:browser` builds the production static app, then
Playwright runs B1–B5 against the real `accounts` binary and disposable
PostgreSQL. The harness builds the current accounts binary with `cargo build
--locked -p concurro-accounts` before startup. The isolated fake provides GitHub
OAuth with S256 challenge checking, GitHub REST/App permissions, the vault owner
routes, task targets and request admission, and `/api/v2` reads and SSE. The task
fake has its own listener so closing it produces an actual accounts connection
failure. It verifies RS256 signatures,
actor, action ID and exact command content before accepting a stop request.
The vault responses use the Slice 3 Python-oracle fixture shapes.

All five cases passed together: sign-in/`next` and menu identity; confirmed remote
logout revocation and signed-out reload; reader summary with no commands and live
task change from a nonblocked stage; writer stop with bound proof and a real
task-connection 503 "nothing was recorded" UI; and add, rename,
disable, reorder, replace and delete credential, stale revision, vault outage
with its edited name retained, and the configured Sponsor command. Corrected
combined log: `/tmp/concurro-phase4-browser-corrected-final.log`.

The browser suite is in the frontend checkout at `tests/browser/`. It uses
`CONCURRO_SERVICE_REPO` to build and locate `target/debug/accounts`, and requires
Rust/Cargo, Docker, `pg_isready` and installed Playwright Chromium. It starts no
live endpoint.

## Deployment and retirement

- CI publishes `accounts` from `crates/accounts/Dockerfile`; the image locally
  builds as `concurro-accounts:phase4` (image log
  `/tmp/concurro-phase4-accounts-image.log`). Compose layers the accounts edge
  and platform listeners alongside tasks and the read API. Caddy routes
  `/api/*` and `/auth/*` to accounts and `/api/v2/*` to the read API.
- `deploy/host.sh` selects the Rust overlay from the release's Compose content,
  pulls/starts only accounts, tasks, read API, web and ingress, and checks their
  exact service image tags. Legacy Python settings/image checks apply only to
  legacy releases, so removing the retired settings does not break later Rust
  updates. The stopped Python controller and its state remain for the phase 3
  archive record.
- The development PostgreSQL setup includes an accounts role/database. Settings
  examples bind the internal accounts listener to the platform alias and wire
  the read API viewer URL and task proof public key. Generic host-independent
  key and cutover instructions, including existing-cluster role/database setup
  and the separate Caddy install/validate/reload step, are in `gateway/README.md`
  and `docs/platform.md`.
- Python `backoffice/`, its CI image/tests, Compose service and volume, portal
  override, old Caddy routes, exporter, optional phase 5 spike and its hosted
  relay extension (which subclassed the retired spike) are removed.
  Outside imports were removed from the diagnostics test. Vault/exchange suites,
  Rust historical importer/migrations and task data remain. The Slice 3 Python
  oracle capture script remains reproducible from a pinned pre-retirement
  checkout, not current CI.

Before any live cutover, complete the historical-display import unless its
completion is already recorded, then settle the Python logout queue and
revoke uncovered OAuth App tokens while Python still runs, then remove its
container, volume and settings after the switch. P1–P3 remain deferred until
deployment is authorized. Recovery is roll-forward through a corrective release.

## Local checks

| Check | Result |
| ---- | ---- |
| B1–B5 combined Playwright production-build suite | 5/5 passed after review corrections; `/tmp/concurro-phase4-browser-corrected-final.log` |
| Local accounts image startup against disposable PostgreSQL | `/api/health` succeeded; platform alias returned `401` without bearer; `/tmp/concurro-phase4-accounts-smoke.log` |
| `cargo nextest run -p concurro-accounts --all-targets --locked --no-fail-fast` | 8/8 passed; `/tmp/concurro-phase4-slice4-nextest.log` |
| Frontend `npm test`, `npm run check`, `npm run build` | 64/64 tests, 0 diagnostics, production build passed |
| Deployment Python suite | 28/28 passed after review corrections; `/tmp/concurro-phase4-slice4-deploy-corrected.log` |
| Gateway Python suite | 200/200 passed; `/tmp/concurro-phase4-slice4-gateway.log` |
| Service Python suite | 57/57 files passed with CI's process isolation; three files were rerun after installing the missing pinned CI packages |
| Compose `config --quiet` with base + ingress + Rust overlays; accounts Docker image build | Passed; Compose rechecked after review |
| `bash -n`, ShellCheck, `cargo fmt --check`, accounts Clippy, Ruff check/format, `git diff --check` | Passed; accounts Clippy emitted no warnings; `/tmp/concurro-phase4-slice4-clippy.log` |

The image smoke uses the built image `concurro-accounts:phase4` and synthetic
settings/secrets in `/tmp/concurro-accounts-smoke.sh`. It verifies startup,
database migration and both listeners; it does not exercise live OAuth, GitHub,
vault or task endpoints. The browser suite covers those routes with fakes. The
post-review commands were `CONCURRO_SERVICE_REPO=<backend checkout> npm run
test:browser` in the frontend checkout, `bash /tmp/concurro-accounts-smoke.sh`,
`python -m unittest discover -s deploy/tests -v` in the CI-like disposable venv,
and `docker compose -f service/compose.yaml -f service/compose.ingress.yaml -f
service/compose.rust.yaml config --quiet` with synthetic release tags.

The service suite needs CI's one-test-file-per-process isolation. Its first
local run missed two pinned CI packages (`nats-py==2.17.0` and
`markdown-it-py==3.0.0`) in the disposable venv; after installing them, the
affected files were rerun. The initial broad gateway run lacked loopback bind
permission; the complete rerun passed with local loopback access. The initial
frontend unit run lacked write access to its generated `.svelte-kit` files; the
authorized rerun passed. These are test-environment failures, not runtime code
changes.

Lodestone semantic searches found existing accounts browser/GitHub/vault
patterns and frontend live API behavior before implementation. Lodestone
similarity searches covered every named new browser harness definition, including
the two task-listener controls added after review; source and
locations are in `/tmp/concurro-phase4-slice4-definitions.json`. No Rust
function/type was added in this slice. No third-party Rust dependency changed.
