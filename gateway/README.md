# Host deployment: gateway and service

agentgateway plus Concurro's key exchange, and the Concurro service, behind an HTTPS
reverse proxy on the same host (the edge). The host only coordinates and proxies inference:
model turns and the exploratory builds run on the targets' GitHub-hosted runners. Design:
[MVP execution](../docs/design/mvp-execution.md).

An installation is independent of every other: it runs the published images with its own
GitHub App, host, secrets and targets, and needs neither a fork of this repository nor
write access to it. Set one up with [New installation](#new-installation).

## Host layout

The host runs CI-published images, with Compose files and gateway configuration from a Git
clone; everything host-specific lives under `/etc` and `/var/lib`.

| Item | Location |
| ---- | ---- |
| Compose files and configuration (clone) | `~/concurro`; Compose projects `concurro-gateway` and `concurro-service` |
| Image tags Compose runs | `~/concurro-release.env`: `CONCURRO_RELEASE`, `CONCURRO_WEB_RELEASE`; written by `deploy/host.sh` |
| Gateway host override (state mounts, other host specifics) | `/etc/concurro/gateway/compose.host.yaml` |
| Provider secrets (0600) | `/etc/concurro/gateway/secrets.env` |
| The service's operator token (0600) | `/etc/concurro/gateway/exchange.env`, as `EXCHANGE_OPERATOR_TOKEN` |
| Gateway state | `/var/lib/concurro/gateway/{inference,exchange,vault}` |
| Required vault metadata bearer (0400, uid 65532) | `/etc/concurro/gateway/vault-service-token`; required by base Compose, including org-only deployments |
| Optional nonsecret service override (0644; parent traversable by deploy user) | `/etc/concurro/compose.service.host.yaml`; outside private service settings |
| Vault master key (0400, uid 65532) | `/etc/concurro/gateway/vault-master-key` |
| Service settings and secrets (0700, uid 65533) | `/etc/concurro/service`: `settings.json`, `github-app.pem`, `gateway-operator-token`, NATS credentials and private delivery/replay bearers |
| Service state (0700, uid 65533) | `/var/lib/concurro/service`: the task registry and reports |
| Accounts settings and secrets (0700, uid 65533) | `/etc/concurro/accounts`: `settings.toml`, `database-url`, `logout-key`, OAuth client secret, read-only App PEM, command-proof private PEM and viewer bearer; see [Phase 4 cutover](#phase-4-accounts-cutover) |
| Task and read API settings (0700, uid 65533) | `/etc/concurro/tasks` and `/etc/concurro/backoffice-read`; each has its own database URL and NATS credentials. Tasks receive the command-proof public PEM; the read API shares the accounts viewer bearer. |
| Run diagnostics (0700, uid 65533) | `/var/lib/concurro/diagnostics`: per-turn requests, prompts, results and transcripts, recorded when `settings.json` sets `"diagnostics_dir"` to this path |
| Bundled edge, if used: Compose project `edge` | `/etc/edge/compose.yaml`, `/etc/edge/Caddyfile` (copies of `edge/`) |
| Bundled edge route files | `/etc/edge/sites/*.caddy`; Concurro's is a copy of `concurro.caddy` |
| Bundled edge certificates | `/etc/edge/tls/{fullchain,privkey}.pem`; a domain name's in `/etc/edge/tls/<name>/` |
| Bundled edge domain sites | `/etc/edge/domains/<name>.caddy` |

The gateway, accounts and read API join the external `edge` network. Accounts,
tasks and the read API also join the private `platform` network. None publishes a
host port. Clients use `https://<host>/concurro` as
the base URL, and the GitHub App delivers webhooks to `https://<host>/concurro/webhook`.
The service reaches the exchange's operator API at the same base URL with the operator
token; runner jobs reach it with their OIDC token and then their session's runner token.
The service's image holds the release's commit, which it serves. Every runner session names
that commit and the launcher workflow runs it, so a deployment changes nothing on GitHub and
ordinary `main` pushes do not change the engine a deployment runs.

## Phase 5 cutover candidates

These files are inactive preparation for the coordinated [Phase 5 cutover](../docs/proposals/architecture/phase-5-plan.md#cutover):

| Component | Compose files |
| ---- | ---- |
| OpenBao | `deploy/compose.openbao.yaml`, with a host database-network overlay when using an existing PostgreSQL cluster |
| Rust exchange and inference gateway | `deploy/compose.phase5.inactive.yaml`, then `deploy/compose.phase5.cutover.yaml` at the switch |
| Accounts native store | Existing service Compose files, then `service/compose.phase5.cutover.yaml` at the switch |
| Protocol 5 verifier and Responses bridge | `deploy/compose.relay-v2.yaml` in its own project at the switch |
| Edge routes | `gateway/concurro.phase5.caddy`, merged with the installed site and validated before reload |

The cutover overlays use the external `concurro-secrets` and
`concurro-relay-private-v2` networks. The exchange operator, runner and admission
listeners bind to their platform, edge and private aliases respectively; the
gateway's public listener binds to its edge alias. Keep each service's settings
and secret mount separate. The verifier's old and new releases cannot hold the
public Sponsor port at once. The existing general updater still selects the old
gateway stack and is not a Phase 5 switch command; use installation-specific,
reviewed steps and exact image pins.

The fork's `CONCURRO_GATEWAY_URL` secret is the public runner control base,
`https://<host>/concurro`. The v2 launcher appends
`/runner/v2/sessions/<session>/claim`, yielding
`https://<host>/concurro/runner/v2/sessions/<session>/claim`. The exchange's
`inference_base_url` is separately
`https://<host>/concurro/inference/v1`; model calls use that URL. Check the
fork secret when changing the launcher ref. Release `backoffice-read` from the
same backend commit no later than v3-producing tasks, while preserving its
database and public read routes.

## New installation

For a host that has never run Concurro. This sequence follows the scripts, Compose files
and settings loaders but has not been run end to end on a fresh host.

**Fixed by the published images and scripts:**

- The host is Linux on amd64, with Docker and its Compose plugin, Git and `flock`.
- The deploying user can run `sudo -n docker` and owns the clone at `~/concurro`.
- Everything is served under `https://<host>/concurro`: `concurro.caddy` routes that prefix
  and the web image is built for it.
- Services join an external Docker network named `edge` and publish no ports.
- Images come from `ghcr.io/concurro`, built from `main` of this repository and of
  concurro-backoffice. Running your own builds means changing the image names in both
  Compose files and in `deploy/host.sh`, and the repositories in `deploy/deploy.sh`.

**1. Clone and create directories**

```bash
git clone https://github.com/concurro/concurro.git ~/concurro
```

```bash
sudo install -d -m 0700 /etc/concurro/gateway
sudo install -d -m 0700 -o 65533 -g 65533 /etc/concurro/service /etc/concurro/accounts \
  /etc/concurro/tasks /etc/concurro/backoffice-read \
  /var/lib/concurro/service /var/lib/concurro/diagnostics
```

Before the first gateway start, also create the master key and mandatory metadata bearer
using [vault provisioning](#credentials-vault). Base Compose mounts both even for org-only targets.

**2. Edge**

Provide HTTPS for `<host>` on the `edge` network as described in [HTTPS edge](#https-edge),
and start it before the first deployment.

**3. Gateway**

| File in `/etc/concurro/gateway` | Content |
| ---- | ---- |
| `secrets.env` (0600) | The provider variables [`agentgateway.yaml`](agentgateway.yaml) references: `OPENAI_API_KEY`, `CLAUDE_OAUTH_TOKEN`, `CHATGPT_ACCESS_TOKEN`, `CHATGPT_ACCOUNT_ID` |
| `exchange.env` (0600) | `EXCHANGE_OPERATOR_TOKEN=<token>`; generate the token with `openssl rand -hex 32` |
| `compose.host.yaml` | Required. Mounts gateway state from `/var/lib/concurro/gateway`, as shown for the vault; `services: {}` keeps the named volumes of `compose.yaml` instead |
| `vault-master-key` | See [Credentials vault](#credentials-vault) |

**4. GitHub App**

Create an App owned by the account that operates the installation, with the permissions
and event subscriptions in [Target and App prerequisites](#target-and-app-prerequisites):

- Webhook URL `https://<host>/concurro/webhook`, with a generated webhook secret.
- Install it on each upstream and fork.

Store the webhook secret with Rust ingress. Follow the [release 1 cutover](../docs/platform.md#compose-ingress-example)
before deploying release 2; this checkout's edge route targets Rust ingress.

Browser sign-in uses a separate OAuth App; repository reads use a separate read-only
GitHub App. Configure them in `deploy/accounts.settings.example.toml`. The OAuth
callback stays `https://<host>/concurro/auth/callback`. The service App needs no
browser callback URL.

**5. Rust service settings**

Provision PostgreSQL, NATS and the private `platform` network as in the
[platform guide](../docs/platform.md). Create separate database URL files and
settings from `deploy/accounts.settings.example.toml`,
`deploy/tasks.settings.example.toml` and
`deploy/backoffice-read.settings.example.toml`, each readable by uid 65533.
Generate an accounts `logout-key` with `openssl rand -hex 32`. Generate an RSA
command-proof key pair; put its private PEM in accounts and its public PEM in
tasks. Share one new viewer-authority bearer between accounts and the read API,
and give the read API its task-read bearer. Set the accounts OAuth App, read-only
App, vault and task URLs. Do not place the accounts platform listener on `edge`.
See [Phase 4 accounts cutover](#phase-4-accounts-cutover) for an existing
installation's token and historical-display prerequisites.

**6. Targets**

Prepare each fork as in [Target and App prerequisites](#target-and-app-prerequisites): the
caller workflow, `.concurro/` and the `CONCURRO_GATEWAY_URL` secret. Add the
matching configured target to the task settings. Check installation grants and
requested token permissions for each role before deployment.

**7. Ingress and release settings**

Prepare Rust ingress as in the [platform guide](../docs/platform.md#compose-ingress-example).
The release helper layers `service/compose.ingress.yaml` when ingress settings exist
and `service/compose.rust.yaml` for releases containing the accounts image. A host
Compose override must not restore the retired Python backoffice service.

**8. Deploy**

Install the host updater and deploy both releases as in [Releases](#releases). Check the result with the
commands in [Operation and pilot](#operation-and-pilot).

## Releases

CI publishes an image per component to GHCR for every `main` commit, tagged with the full
commit SHA. Nothing is built on the host.

| Image | Source |
| ---- | ---- |
| `ghcr.io/concurro/service` | `service/Dockerfile`; holds the commit's checkout with its history |
| `ghcr.io/concurro/accounts` | `crates/accounts/Dockerfile` |
| `ghcr.io/concurro/tasks`, `ghcr.io/concurro/backoffice-read`, `ghcr.io/concurro/ingress` | Their Rust crate Dockerfiles |
| `ghcr.io/concurro/exchange`, `ghcr.io/concurro/vault` | `gateway/exchange`, `gateway/vault` |
| `ghcr.io/concurro/backoffice-web` | [concurro-backoffice](https://github.com/concurro/concurro-backoffice), released on its own |

The packages must be public, or the host's root Docker logged in to `ghcr.io` with a
`read:packages` token. Normal releases run directly in an already-open terminal on the host.

### One-time command installation

From a checkout containing these deployment helpers, on the host:

```bash
bash ~/concurro/deploy/install.sh
export PATH="$HOME/.local/bin:$PATH"
```

The installer copies the command to `~/.local/bin/update-concurro.sh` and its helpers to
`~/.local/lib/concurro-deploy/`. It changes no services or settings. Keep `~/.local/bin` on
PATH in the host's shell configuration. Run the installer again only when intentionally
updating the deployment helpers. The updater needs Python 3.10+ in addition to the host's
existing Bash, Git, Docker Compose, `flock` and noninteractive Docker sudo access.

### Normal release

After merging and pushing the changes, run on the host:

```bash
update-concurro.sh || printf '\nUpdate failed; inspect the output above.\n'
```

The command selects the current `main` commit of **both** repositories once, prints their
full SHAs, and waits for successful push CI at those exact commits, including image
publication. It waits if GitHub has not registered a new run yet. A failed CI run stops the
update before deployment. Later pushes do not change the selected pair during that wait.
Public GitHub API reads need no login; an optional `GH_TOKEN` supplies authenticated API
quota. Neither `gh` nor SSH is used by this host command.

After CI succeeds, one deployment lock covers the combined update. The helper checks out
the service release's Compose files, records both image tags, stops service dispatch, pulls
the images selected by that release, starts its application components, and verifies their
health and release tags. Phase 4 Rust releases start accounts, tasks, the read API, web and
ingress; exchange, vault and the gateway remain separately managed. Nothing is built on
the host. Separate edge/Caddy infrastructure is not part of an application release.

To deploy a previously selected pair, or repeat a failed update with the same pair:

```bash
update-concurro.sh SERVICE_FULL_SHA WEB_FULL_SHA || printf '\nUpdate failed; inspect the output above.\n'
```

Explicit pairs still require successful main push CI. The service release must include the
recorded release. If deployment fails after stopping the service, correct the reported
problem and rerun; recovery always moves forward. No pilot retries or GitHub writes occur.

### Release-specific preparation

New installations, configuration or secret changes, App permission approvals, infrastructure
changes, and release-specific migrations remain explicit preparation steps. Follow the
release's instructions **before** the normal update. The updater neither modifies settings
nor runs arbitrary migration hooks. Updating the installed deployment helpers is also an
explicit installer operation, separate from pulling application images.

### Phase 4 accounts cutover

This is a single roll-forward deployment window. The approved
[Phase 4 cutover plan](../docs/proposals/architecture/phase-4-plan.md#cutover) gives the
exact order. Before switching, complete the historical-display export/import if its
coverage is not already recorded. The exporter must run from a pinned pre-retirement
release while the Python backoffice and its sign-in token still work. Then settle
Python logout jobs and revoke remaining Sign-in OAuth App authorizations. A single
browser's `revoked` receipt does not cover other tokens.

Prepare `/etc/concurro/accounts` with its own PostgreSQL URL, new 64-hex-character
logout key, existing Sign-in OAuth App credentials, read-only App PEM, new RS256
command-proof private PEM and new viewer-authority bearer. The task application
gets the matching public PEM; the read API gets the same viewer bearer and names
`http://concurro-accounts-platform:8794`. Prepare the vault, relay and task URLs
in `deploy/accounts.settings.example.toml`. On an existing PostgreSQL cluster,
provision the accounts role and database before updating; the development init
script only runs for a fresh volume. Use a distinct production password:

```sql
CREATE ROLE accounts LOGIN PASSWORD '<new password>';
CREATE DATABASE accounts OWNER accounts;
REVOKE CONNECT ON DATABASE accounts FROM PUBLIC;
GRANT CONNECT ON DATABASE accounts TO accounts;
```

Place the matching connection string in `/etc/concurro/accounts/database-url`.
Replace any host Compose override that defines `backoffice`. The release helper
starts accounts, tasks and the read API with the Rust overlay, checks their exact
image tags, and leaves the Python controller stopped. The edge sends `/api/*` and
`/auth/*` to accounts and `/api/v2/*` to the read API.

After the helper succeeds, install the new Caddy route file, validate it, then
reload Caddy. This edge step is separate from the application release:

```bash
sudo install -m 0644 ~/concurro/gateway/concurro.caddy /etc/edge/sites/concurro.caddy
sudo docker compose -f /etc/edge/compose.yaml exec -T caddy caddy validate --config /etc/caddy/Caddyfile
sudo docker compose -f /etc/edge/compose.yaml exec -T caddy caddy reload --config /etc/caddy/Caddyfile --force
```

After the switch, remove the Python backoffice container, image reference,
`backoffice-data` volume and `/var/lib/concurro/backoffice`, and archive/remove
`/etc/concurro/backoffice` after its logout queue is settled. Keep the stopped
Python controller state until the phase 3 archive import is recorded complete.
The operator then performs P1–P3 from the cutover plan with their own account.
These live checks have not been run by this local implementation.

The sign-in callback URL remains `https://<host>/concurro/auth/callback`; no App
permission change is implied by this cutover. Sessions and viewer tokens do not
carry over. A failed cutover is repaired by a corrective release; the Python
backoffice is not restarted.

### Historical rollout notes

The following migration procedures describe earlier releases and their Python
backoffice at the time. They are retained as operator evidence, not as current
Phase 4 deployment instructions.

#### Save-only credential rollout

The save-only release emits `stored` lifecycle events. Older service and Exchange consumers
cannot advance past them, including to later revocations. Upgrade both consumers before
allowing the new vault to accept saves; never leave an old consumer running against the new feed.
Upgrade the vault before the backoffice API and frontend, whose saves no longer include models.

For the combined updater, finish active work and stop the `backoffice` Compose service
before running the update so credential writes cannot race the gateway replacement. The
updater stops service dispatch and replaces the gateway stack (Exchange and vault), then
starts the new service, backoffice API and frontend. Keep the backoffice stopped if an update
fails before the new consumers are ready; correct the failure and rerun the same or a later
release. Verify all running versions before reopening credential saves. Individual component
updates must order Exchange and service first, vault second, backoffice API and frontend last.

Preserve vault, registry and Exchange state and their event cursors. Recovery is roll-forward
only: once `stored` events exist, every corrective consumer release must understand them.
This change requires no new App grants, settings or database migration.

#### Ordered credential lists rollout

This release moves the vault to schema 4, writes checkpoint format 10 for tasks that select
from a credential list, and replaces the route-addressed credential API and page. Registry
schema 9 and the Exchange database are unchanged; no settings, secrets or App permissions
change. Earlier services, Exchanges, backoffice APIs and frontends cannot read the new vault,
its `policy` events or format 10, so the service and web releases deploy as one pair in one
idle window, and recovery is roll-forward only.

Both releases must have passed `main` push CI. `SERVICE_FULL_SHA` and `WEB_FULL_SHA` are
their commits; `gw` and `svc` are the shell functions defined under
[Upgrade from the workbench release](#upgrade-from-the-workbench-release).

1. Stop credential writes and commands, and let work drain:

   ```bash
   svc stop backoffice
   ```

   ```bash
   svc exec service python3 -m service --settings /etc/concurro/service/settings.json status
   ```

   Wait until no task has a running turn or batch. Tasks paused in `awaiting_credentials`
   may stay paused; they are selected again once after the update.

2. Stop dispatch and capture the ordering manifest with the new service image. The command
   only reads the registry:

   ```bash
   svc stop service
   ```

   ```bash
   sudo docker run --rm --network none --volume /etc/concurro/service:/etc/concurro/service:ro --volume /var/lib/concurro/service:/var/lib/concurro/service ghcr.io/concurro/service:SERVICE_FULL_SHA credential-order-manifest > ~/order-manifest.json
   ```

   Read the file. Each owner/provider is `agreement` (that type goes first), `conflict` or
   `unavailable` (subscriptions go first); owners without a personal grant are absent and
   also get subscriptions first.

3. Stop the gateway stack and back up `/etc/concurro` and `/var/lib/concurro` with
   owner-only access, including SQLite WAL files and the vault master key:

   ```bash
   gw stop
   ```

4. Give the vault the manifest for its one migration start. Copy it into the vault state
   directory, readable by uid 65532:

   ```bash
   sudo install -o 65532 -g 65532 -m 0400 ~/order-manifest.json /var/lib/concurro/gateway/vault/order-manifest.json
   ```

   and add this to the `vault` service in `/etc/concurro/gateway/compose.host.yaml`:

   ```yaml
   environment:
     VAULT_ORDER_MANIFEST_FILE: /data/order-manifest.json
   ```

   With the variable set, a missing or malformed file stops the vault before it migrates.
   Without it, every list starts with subscriptions first.

5. Deploy the pair:

   ```bash
   update-concurro.sh SERVICE_FULL_SHA WEB_FULL_SHA || printf '\nUpdate failed; inspect the output above.\n'
   ```

   If it fails, keep the backoffice stopped, correct the reported problem and rerun the same
   pair. A vault that already migrated does not read the manifest again.

6. Verify before reopening. The vault's first start prints one `adopted_lists` line naming
   every owner/provider order and whether the manifest or the default set it:

   ```bash
   gw logs vault | grep adopted_lists
   ```

   ```bash
   svc logs --tail 50 service
   ```

   Confirm the service serves `SERVICE_FULL_SHA`, the vault is healthy (`gw ps vault`) and
   the backoffice and frontend are running the pair (`svc ps`). The updater starts the
   backoffice; credential writes are open from that point.

7. Remove the `VAULT_ORDER_MANIFEST_FILE` entry and the copied file at any later time; the
   vault never reads either again.

After the update, each sponsor's saved credentials appear as entries in their previous
versions and state, enabled and unfiltered. Tasks admitted before the update keep their frozen
credential; new selections use list order, so a target's `provider_credential_routes` and
`credential_routes` settings no longer choose the personal credential type. A task paused for
credentials under the earlier rules is selected once against the sponsor's whole list, with
its failed versions still excluded. No live provider call is part of this rollout; the
classification gaps listed in [credential failures](../docs/design/credential-failures.md)
need a separately authorized check.

### Individual component deployments from a workstation

The existing workstation helper remains available for a selected service or web release.
Install the host helpers as above first; normal calls reuse them through one SSH connection
and never upload scripts. For upgrades, deploy the API/service before the web when using
individual commands; the combined updater above is preferred. On a first installation only,
record the web release first (without starting a service), then deploy the service to start
the selected pair.

```bash
export CONCURRO_DEPLOY_HOST=<ssh-name>
bash deploy/deploy.sh service SERVICE_FULL_SHA || printf '\nDeployment failed; this terminal remains open.\n'
bash deploy/deploy.sh web WEB_FULL_SHA || printf '\nDeployment failed; this terminal remains open.\n'
```

- **`web`** waits for its commit's CI and updates only the web image. Before the first
  service deployment, it records the web release for that first startup.
- **`service`** waits for its commit's CI and updates the service, API and gateway images,
  retaining the recorded web release. It uses the same host deployment checks as the
  combined updater.

When a release changes the service settings format, place the new file at
`/etc/concurro/service/settings.next.json` with the same ownership and permissions as
`settings.json`. The host helper validates it using the release image while the current
service is running, then stops the old service and atomically promotes it. It keeps the
previous file as `settings.json.previous` for recovery. Without a staged file, the helper
validates the current settings before stopping services.

Host Docker commands use non-interactive `sudo docker` only. Execute the scripts; never
source them. The Compose `build` sections remain for local builds.

## Phase 3 listener and reviewer storage

At the single controller cutover, layer `gateway/compose.rust.yaml` over the gateway
Compose configuration, preserving existing private-network and state overrides. It
attaches the gateway network namespace to `platform` and sets
`EXCHANGE_OPERATOR_LISTEN=concurro-exchange-operator:8082`. The exchange then serves
operator calls only on that listener; public port 8080 serves runner calls only.
Keep `EXCHANGE_PRIVATE_LISTEN=127.0.0.1:8081` for inference hooks. Do not route the
operator listener through the edge. Rotate its bearer when Python stops, and give
it only to tasks. `deploy/tasks.settings.example.toml` uses this private endpoint.

Without `EXCHANGE_OPERATOR_LISTEN`, the exchange retains the combined runner/operator
listener for existing deployments. Setting it requires `EXCHANGE_OPERATOR_TOKEN`;
all configured listeners bind before request handling starts.

The task configuration mount remains read-only. The `service/compose.rust.yaml`
example additionally mounts `/var/lib/concurro/tasks/reviewer` writable, because
reviewer OAuth refresh atomically replaces the grant file. Before starting tasks:

```bash
sudo install -d -m 0700 -o 65533 -g 65533 /var/lib/concurro/tasks/reviewer
```

After the Python controller has stopped, copy its final `reviewer-grant.json` into
that directory with owner `65533:65533` and mode `0600`. Set `reviewer_grant_file`
to `/var/lib/concurro/tasks/reviewer/reviewer-grant.json`, and configure the App's
OAuth client ID and secret-file reference as shown in the settings example.
Mount the directory, not only the file: refresh needs to rename a temporary file.
Include this state directory in private configuration/state backups. Installations
without a reviewer need no grant or OAuth configuration.

## Launcher and engine

Target callers reference the launcher, `concurro/concurro/.github/workflows/agent.yml@main`,
and `gateway.workflow_ref` in the service settings names the same workflow and ref. The
launcher's commit is independent of the engine a deployment runs:

1. The service puts its served commit in each session's binding (`engine`).
2. The launcher presents its job's OIDC token to `POST <gateway>/exchange/sessions/<id>/engine`.
   The exchange authenticates it exactly as a claim (target repository and owner, launcher
   workflow ref, bot dispatch, session ref, hosted runner, first run) and answers
   `{"repository", "engine"}`: the launcher's own repository and the session's commit.
3. The launcher fetches that commit by SHA and runs `engine/runner.py` from it.
4. `runner.py claim` states its checkout's commit. The exchange issues the key and runner
   token only if it equals the session's; any other runner is refused and the refusal recorded.

A target's optional `engine` pin is checked by the service: without one the target runs
whatever engine the operator deployed; with one, any other served commit blocks the task
with "target pins another engine commit". Upgrading a deployment therefore needs no change
in target repositories, no engine fork and no write access to `concurro/concurro`.

Rust task prompts are runtime files, separately configurable from the task binary and
runner commit. See [runtime prompt bundles](../crates/tasks/README.md#runtime-prompt-bundles)
for the bundle mount, first-release runner pin, exact snapshot behavior and legacy recovery.

### Launcher contract

The launcher on `main` serves every supported release, so these only ever grow:

- The exchange's `engine` action above.
- `engine/runner.py claim|prepare|serve|end|summary --session --gateway --root`, with
  `--config` and `--base` for `prepare`.
- `engine/agent-clients.txt`: the npm packages the launcher installs, one pinned spec per line.
- The target's optional `.concurro/setup` action, given `base`.

`engine/tests/test_launcher.py` runs the workflow's own steps against the exchange. A
launcher change that needs something new from the engine must keep working with releases
that lack it. An operator who wants launcher changes only on their own schedule can name a
tag in `gateway.workflow_ref` and the callers instead of `main`.

### Migrating from the `deployed` ref

Releases before this one loaded the engine from the launcher's own commit and moved a
shared `deployed` branch on every deployment. Once, per deployment:

1. Stop the service and let active runner jobs finish. Sessions issued before the upgrade
   cannot be claimed afterwards.
2. Set `gateway.workflow_ref` in `/etc/concurro/service/settings.json` to
   `concurro/concurro/.github/workflows/agent.yml@refs/heads/main` (back the file up first).
3. Deploy this release with `deploy/deploy.sh service`.
4. In each target fork, change the caller's `uses:` from `agent.yml@deployed` to
   `agent.yml@main` on the default branch.
5. Tasks keep the caller commit they started with. Give each in-flight task `/concurro resume`
   so it adopts the updated caller; until then its runs present the old workflow ref and are
   refused.

To defer steps 4 and 5, leave `gateway.workflow_ref` and the callers on `deployed` and
fast-forward that branch one last time to this release or later; it then serves as the
launcher ref and later deployments leave it alone. A caller still reaching a launcher from
before this release is refused with "runner engine differs from the session's".

## Credentials vault

[`vault/`](vault/vault.py) stores per-user provider credentials
([proposal §5](../docs/proposals/per-user-credentials.md#5-vault)). It has no Caddy route
and publishes no port: other `edge` services, such as accounts, reach it as
`http://concurro-vault:8090`. Owner credential requests carry
`Authorization: Bearer <GitHub user token>`; the vault resolves the user with
`GET https://api.github.com/user` (cached for five minutes by token SHA-256) and answers
401 for a rejected token.

| Request | Result |
| ---- | ---- |
| `GET /credentials` | `{providers: [{provider, revision, entries}], types}`: each provider's entries in priority order |
| `POST /credentials` | Appends a new entry and stores its secret without provider requests; 422 for malformed input |
| `POST /credentials/relay` | Approves one displayed Sponsor code for its registered routes and appends an owner-bound entry per route without stored provider secrets |
| `PATCH /credentials/{entry_id}` | Changes name, enabled or role/profile filters |
| `PUT /credentials/{entry_id}/secret` | Stores the entry's next version, retiring only its previous one |
| `PUT /credentials/{entry_id}/relay` | Approves a replacement Sponsor for that one route at the entry's current revision |
| `PUT /credentials/relay/device` | Approves one new Sponsor code for a paired device's entries, one per registered route |
| `POST /credentials/relay/pending` | Shows the device name and exact routes of a pending Sponsor code |
| `GET /credentials/relay/status` | Live links, capacity and active leases of the owner's current Sponsor approvals |
| `PUT /credentials/order/{provider}` | Commits the provider list's exact new order |
| `DELETE /credentials/{entry_id}` | Removes the entry, marks its record `revoked` and destroys its ciphertext |
| `GET /health` | `{"status": "ok"}` |

Routes are `anthropic-api-key`, `openai-api-key`, `chatgpt-subscription` and
`claude-subscription`; an owner may hold several entries of one route. ChatGPT secrets also
require `account_id`. Saving requires no model selection; token and model-access failures are
reported during execution. The [vault guide](vault/README.md#owner-api) has the request fields,
revisions and replay rules. A record's
`status` is `valid`, `invalid` or
`revoked`; `fingerprint` is the secret's last 4 characters and the first 8 hex characters
of its SHA-256. Secrets are never returned or logged. Each version is sealed with its own
AES-256-GCM data key, which the master key wraps; only each entry's latest version keeps
ciphertext. Records live in SQLite (WAL) at `/data/vault.db`. Configured contributed
inference resolves exact admitted versions through the separate private listener; org-funded
work keeps org credentials. The service metadata principal never receives secrets. See the
[vault lifecycle/private-listener guide](vault/README.md).

The optional [hosted relay overlay](compose.relay.yaml) adds a Rust verifier for
`claude-subscription` and `chatgpt-subscription` entries with `custody=relay`.
Set `CONCURRO_VERIFIER_IMAGE` to the qualified Linux amd64 image and
`CONCURRO_RELAY_VERIFICATION_URL` to the full public backoffice URL ending in
`/concurro/credentials/relay` (or `/credentials/relay` when mounted at root).
The owner approves one displayed Sponsor code for exactly the routes Sponsor registered
in the portal. Exchange admits one request using its existing
model, budget, turn and exact-version checks, then forwards it to the verifier on
the private network. A live Sponsor link is required; one task retains one native
provider session for cold and warm requests. The verifier ends an idle link whose relay
disconnects, or stops answering TCP keepalive within about a minute, and reports live links
only to their owner through the Vault. Set accounts' `relay_server` to the public
`<host>:<port>` of the relay listener so the portal shows the launch command. The overlay
requires a TLS certificate and key, and a shared private service token.
It is not part of the base deployment.

The vault refuses to start unless its master key file holds 64 hex characters and is a
regular file owned by uid 65532 with no group or other access. Losing the key loses every
stored secret, so back it up with owner-only access. Create it and the state directory
once, before the first `gw up -d`:

```bash
sudo sh -c 'umask 077 && openssl rand -hex 32 > /etc/concurro/gateway/vault-master-key'
sudo chown 65532:65532 /etc/concurro/gateway/vault-master-key
sudo chmod 0400 /etc/concurro/gateway/vault-master-key
sudo sh -c 'umask 077 && openssl rand -hex 32 > /etc/concurro/gateway/vault-service-token'
sudo chown 65532:65532 /etc/concurro/gateway/vault-service-token
sudo chmod 0400 /etc/concurro/gateway/vault-service-token
sudo install -d -m 0700 -o 65532 -g 65532 /var/lib/concurro/gateway/vault
```

The metadata bearer is mandatory with the supplied base Compose, even without contributed
funding. The resolver bearer remains optional. For service metadata reads, install a separate
owner-only copy of the metadata token for uid 65533; do not expose the resolver token there.

Mount that state directory in `compose.host.yaml` as the other gateway state:

```yaml
services:
  vault:
    volumes:
      - /var/lib/concurro/gateway/vault:/data
```

`VAULT_LISTEN`, `VAULT_STATE_DIR` and `VAULT_MASTER_KEY_FILE` are set in
[`compose.yaml`](compose.yaml); the key file is mounted read-only at
`/run/secrets/vault-master-key`.

## Target and App prerequisites

The target fork needs the [caller workflow](../examples/stacks-core/.github/workflows/concurro-executor.yml)
on its default branch, calling `agent.yml@main`, and the current `.concurro/` config
and charter. The upstream and fork must be public and in the same fork network. No new
fork is created. Optional setup is a composite action at `.concurro/setup/action.yml`.
The caller passes the repository secret `CONCURRO_GATEWAY_URL` explicitly to the reusable
workflow and may set `runs-on`. Dispatch passes the issued session ID plus display-only
`task-label`, `operation-label` and `agent-label` strings. The labels populate run, job and
step names; the final job summary reports actual turn outcomes.
Both jobs use `cache-mode: read`. Store the deployment URL as a secret, not a workflow
input, variable or source default, so GitHub masks it from the start of the job:

```bash
gh secret set CONCURRO_GATEWAY_URL --repo OWNER/FORK
```

Enter the URL at the prompt. It must match `gateway.url` in the service settings: the
workflow uses the secret for the exchange, while turn payloads use the service setting
for inference. Never pass provider keys or the exchange operator token to runner workflows,
and do not use `secrets: inherit`.

When upgrading from a session-only caller:

1. Pause dispatch and finish active runner jobs.
2. While dispatch remains paused, update the fork caller and the service. Old callers
   reject the new inputs; the updated caller needs a reusable workflow that declares the
   labels.
3. Explicitly resume affected tasks against the updated configuration before allowing
   further dispatch. Updating the default branch alone does not change a task's pinned
   caller commit.

Optional label defaults still support session-only manual dispatch. No additional App
permissions are required for these display fields.

For the secret-interface migration, finish active tasks and runner jobs, then stop the
service before landing the updated fork caller. Keep dispatch stopped while deploying the
matching engine; restart only after both sides are updated. A new caller forwarding an
undeclared secret and an old caller omitting a required secret are both incompatible.

Secret forwarding requires the updated caller in the task's pinned configuration commit.
Changing the default branch does not update existing task pins; reconcile those explicitly
before resuming them. Masking prevents routine disclosure in future logs, not discovery of
the public endpoint, and does not redact old logs or Git history.

Install the controller App on the selected repositories. Grant Contents read/write,
Pull requests read/write, Checks read, Commit statuses read, Actions read on the upstream, Issues read/write where
issues enroll, and Actions write plus Workflows write on the fork. Standalone PR reviews
additionally need Issues write and Checks write on the upstream. Fork read tokens explicitly request
Actions read for runner-termination diagnosis; this uses the already required fork
Actions-write installation grant. Token requests narrow these permissions
per repository; the write gateway narrows permitted endpoints further. Retain Contents
write upstream for the tested review-thread resolution permission combination. No
Administration permission is needed for readiness. Historical `adopt` additionally needs
Actions, Variables and Environments read to verify the retired Actions controller is off.

### Standalone PR reviews

Reviews of existing upstream PRs are published by a real GitHub user, the reviewer account,
through a user token of the service App ([design](../docs/design/standalone-reviews.md)).
Nothing here is enabled until `settings.json` names a `reviewer`.

Do these in order. The service requests exactly the permissions its settings imply and
refuses a token that differs, so starting a service with a `reviewer` before the grant in
step 1 is accepted stops every upstream write token, for contribution tasks too.

1. **App permissions.** Add Issues write and Checks write on each upstream and accept the
   updated permissions on every installation. Subscribe the App to `pull_request` events
   if it is not already.
2. **Device flow.** Enable device flow for the App. Keep its user-token expiration setting
   as it is; both expiring and non-expiring tokens are handled.
3. **Repository access.** Give the reviewer account read access to each upstream, as a
   collaborator or through a team. GitHub lists only such accounts as requestable reviewers;
   installing the App does not. Read access is enough: the account only publishes reviews
   and replies, and the App resolves its finding threads.
4. **Settings.** Add the block below, with the account's numeric ID (`gh api users/LOGIN --jq .id`),
   the App's client ID and a 0600 file holding its client secret. Each upstream may then
   appear in only one target.

   ```json
   {
     "reviewer": {
       "user_id": 123456789,
       "client_id": "Iv23abcdefghijklmnop",
       "client_secret_path": "/etc/concurro/service/app-client-secret"
     }
   }
   ```

5. **Authorize.** With the service stopped, run the command below and approve the shown code
   in a browser signed in as the reviewer account. A grant for any other account is
   discarded. The grant is stored as `reviewer-grant.json` in the state directory.

   ```bash
   sudo docker run --rm -it --volume /etc/concurro/service:/etc/concurro/service:ro \
     --volume /var/lib/concurro/service:/var/lib/concurro/service \
     ghcr.io/concurro/service:FULL_COMMIT_SHA authorize-reviewer
   ```

6. **Start.** The service refuses to start with a `reviewer` and no usable grant. Repeat
   step 5 if a refresh is refused or the account's grant is revoked.

The release that introduced this also split the PR reviewer skill, which changes the bundle
identity: deploy it while idle, or have a writer `/concurro resume` each active task.
Registry schema 9 migrates forward on first start; back up the stopped registry first.

Before a pilot, confirm on disposable resources what unit tests cannot: PR artifact writes
with the App's token and reads with the Reader App, the reviewer account's selection,
reviews and replies, and how the check shows on a PR from a contributor fork. The design
document lists these.

### Upstream workflow changes and fork publication

Creating a fork branch at an approved upstream commit can introduce new or changed
`.github/workflows/` files, even though the agent changed no workflows.
GitHub may require both Contents write and Workflows write for that ref creation or update
([Git references API](https://docs.github.com/en/rest/git/refs#create-a-reference)). Actions
write permits dispatch; it does not replace Workflows write. Concurro requests Workflows
write only on fork publication tokens; read tokens and upstream-only write tokens omit it.
Candidate edits to `.github/` remain prohibited, and publication remains restricted to
the task's branch. No installation token is passed to runners.

Before deploying this permission change, grant Workflows read/write in the GitHub App
settings and accept the update on every configured fork installation, including legacy
targets still configured on the service. Check the installation's granted permissions
and `service.targets.permissions()`; an App grant alone does not widen an explicitly
narrowed token. The token request will fail if the installation has not accepted it.

For a `403 Resource not accessible by integration` on `POST git/refs`, inspect the service
log, the recorded base SHA, fork rulesets and workflow differences between the fork default
branch and that base. Runner dispatch success is not proof that publication works: session
refs use the fork's own setup commit. In pilot #19, the approved base changed Demo Rust CI
to enable `rusqlite`; every fork branch still held the older workflow revision.
Do not bypass the controller's branch reservation by manually creating its task branch,
or overwrite the fork's caller/configuration while synchronizing upstream.

Controlled probe on 2026-11-07: the same installation token (Contents write, Actions write,
no Workflows permission), endpoint and temporary branch name produced:

| Commit | Only difference | Create-ref result |
| ---- | ---- | ---- |
| `a0ce7aa91d3a97a6ec4fd7af04626db74fa9940a` | Before the SQLite CI change | 201 Created |
| `4f295075846a96c03b5754b47188b2d89aa7cab2` | Adds `--features rusqlite` to that workflow | 403 Resource not accessible by integration |

GitHub request IDs were `E304:2AEA27:1DD9EEE:2DAB130:6AEED7D8` and
`EC74:32F722:1CC6B75:2C47D66:6AEED7DD`, respectively. The temporary branch was deleted,
the token revoked, and zero workflow runs were created. This isolates the workflow change
as the trigger.

After the operator granted Workflows write and accepted it for the demo fork, a second
probe used the same commit `4f295075846a96c03b5754b47188b2d89aa7cab2` and temporary
branch name for both attempts, changing only the requested token permissions:

| Token permissions | Create-ref result | GitHub request ID |
| ---- | ---- | ---- |
| Contents + Actions write | 403 Resource not accessible by integration | `E5FA:1A35CD:1FD18BC:30A6318:6AEED9C2` |
| Contents + Actions + Workflows write | 201 Created | `941E:340FBF:1E6207F:2E670F0:6AEED9C5` |

Both responses included `x-accepted-github-permissions: contents=write; contents=write,workflows=write`.
The permission remedy is verified live. The probe branches were deleted and verified
absent, all diagnostic tokens were revoked, and no deployment or pilot retry occurred.

Subscribe to issues, issue comments, pull requests, pull request reviews, pull request
review comments, pull request review threads, check runs, check suites, workflow runs,
commit statuses and pushes. Verify the added Workflow runs and Push subscriptions before
shipping webhook-driven idle handling; this change does not alter live App settings.
The webhook URL is `https://<host>/concurro/webhook`; after the release 1 cutover,
Rust ingress receives it and owns delivery receipts. Python service consumes durable
bus events and uses applied producer coverage to verify list freshness. Its private
App delivery API remains available for Rust ingress to list and redeliver deliveries.
Unchanged idle PRs are not polled. [Compose example](../docs/platform.md#compose-ingress-example)
gives the required release 1 first deployment, parity, edge switch, reconciliation,
and duplicate GUID checks before release 2. Release 2 removes Python's webhook intake
and delivery scan.

The current service upgrades the registry to schema 9, retaining durable observation
jobs. Back up the stopped registry and preserve its identity; use
corrective releases and forward migrations.
When combining with delegated backoffice commands, wire the command predicate and durable
post-observation callback and run their combined regressions before release. See
[service scheduling](../docs/design/service.md) and the
[request measurements](../docs/reports/webhook-recovery/README.md).
Upstream may require a maintainer to approve the first fork PR's workflow runs; Concurro
shows that wait and does not bypass it.

The stacks-core example charter requires an upstream that permits the contribution. Use
a permitted test upstream for the pilot; the target's contribution policy still governs.

## Upgrade from the workbench release

Commit and push the reviewed source first: CI builds the service image from it, and the
service refuses a dirty checkout. The following are operator-run deployment instructions, not automatic actions.
No step runs a controller, model or build agent on a Mac.

1. Stop dispatch by stopping the service on the Linux host. Let its active ticks finish,
   cancel any remaining old runner jobs, and end their exchange sessions using the
   exchange CLI. Resolve unknown GitHub writes from observed evidence before retiring
   their tasks. Do not blindly re-dispatch an uncertain workflow start.
2. Stop the exchange and gateway briefly and back up `/etc/concurro` and
   `/var/lib/concurro` with owner-only access, including SQLite WAL files. If using named
   volumes instead of the existing host override, back those volumes up too. Keep the
   registry ID. Never use `down -v`, delete the registry or reinitialize an existing store.
3. Check out the exact reviewed release on the host. Install the fork's updated caller
   and target configuration through its normal review process. Convert a legacy Rust
   target config to a separate file with `python3 -m service.migrate_target INPUT OUTPUT`,
   review its path policy and replace the config in the fork. Remove an old optional
   target `engine` pin, or update it to the reviewed release. Custom profiles require
   manual translation to setup, instructions and protected/generated paths.
4. Pull the new server images and migrate service settings as shown below. The target
   settings now use `{upstream, fork, issues}`; execution-image and per-target credential
   settings are gone. Preserve all service secrets, state paths and `registry_id`.
5. Start the gateway and service; verify the logged served SHA equals the release. Only
   then enroll a fresh pilot issue.

Use the existing host mounts and Compose project names:

```bash
gw() { sudo docker compose --env-file ~/concurro-release.env -p concurro-gateway -f ~/concurro/gateway/compose.yaml -f /etc/concurro/gateway/compose.host.yaml "$@"; }
svc() { sudo docker compose --env-file ~/concurro-release.env -f ~/concurro/service/compose.yaml "$@"; }
```

After stopping/backing up, checking out the committed release and recording it as
`CONCURRO_RELEASE` in `~/concurro-release.env`:

```bash
gw pull
svc pull
svc run --rm --volume /etc/concurro/service:/etc/concurro/service:rw \
  --entrypoint python3 service -m service.use_gateway \
  --settings /etc/concurro/service/settings.json \
  --url https://<host>/concurro \
  --operator-token /etc/concurro/service/gateway-operator-token \
  --upstream OWNER/UPSTREAM \
  --fork OWNER/FORK --issues fork
```

The one-off command overrides the normally read-only settings mount. It runs as the
service's uid 65533, validates a private temporary file, then atomically replaces settings.
The running service keeps its read-only mount. Multi-target settings must be edited
explicitly; the helper refuses to guess their pairs. Back up settings before replacing them.

With the host checkout at the reviewed commit:

```bash
gw up -d
svc up -d
svc logs --tail 30 service
```

A non-fast-forward release move needs explicit operator reconciliation, not a blind
force push. The current registry migrates schemas 2–8 to schema 9 transactionally. Exchange columns are
added without clearing history; old unbound pending turns are lost rather than staffed
under new authority. In-flight legacy Python checkpoints retain their old bundle identity
and block for reconciliation. Rust tasks with frozen runtime bundles keep their recorded
bytes; a writer resume explicitly adopts a newer bundle. A fresh issue is the straightforward pilot path; never replace a
checkpoint's bundle hash to make old approvals look current. A blocked implementation
can instead use a writer-authorized retry when only the engine implementer skill changed
and the controller proves compatibility. Keep local engine Git history for this check on
legacy tasks; see [recovery](../docs/concurro.md#commands-and-recovery).
[`/concurro resume`](../docs/design/checkpoint-resume.md) adopts current configuration
and instructions while preserving completed work and original approval bindings.
It records explicit writer authority, without a release compatibility certificate.
Resume receipts require at least checkpoint format 6; large inventories require format 5.
Artifact consolidation introduced format 7; shadow sponsorship introduced format 8.
Frozen admissions write **format 9 on every charged checkpoint save**, and a task that selects
from a credential list writes format 10; Registry schema 9
is current (provider fences/admission support first shipped with schema 7). This applies to ordinary calls as well as contributed-mode tests.
Corrective releases must preserve phase 1's comment-encoded audits and publication
receipts for all such tasks. Tasks
writing shared artifacts additionally need the final phase-4b reader, including version-3
nonce/attempt-tagged parts and their completion records. Ship phases 3, 4a and 4b together;
their combined request-volume criteria passed. That historical storage package used checkpoint format 7. See the [closeout evidence](../docs/reports/limits-cleanup/README.md#artifact-consolidation-closeout).
Do not restore older task checkpoints to bypass this requirement: that loses spent budgets
and publication progress.

Recovery is **roll-forward only**. Fix incidents with corrective releases and forward
migrations that preserve current stores, spent budgets, approvals and unknown-write
records. Deployment rollback and downgrades are not supported or required.

## Operation and pilot

```bash
svc exec service python3 -m service --settings /etc/concurro/service/settings.json status
svc exec service python3 -m service --settings /etc/concurro/service/settings.json reconcile-once
svc logs -f service
gw exec -T exchange python3 -I /app/exchange.py list
gw exec -T exchange python3 -I /app/exchange.py turns
gw ps vault
```

An end-to-end pilot should show native setup and model turns, a fork branch link, an
upstream draft PR, independent COMMENT reviews, then readiness after current CI passes.
Check that another role cannot claim its session, failed CI leaves the PR draft, session
keys expire/end, and recorded session refs are swept without deleting contribution branches.
No model job should remain alive merely waiting for CI. These live checks have not been
run for this refactor; offline tests do not prove GitHub workflow compatibility.

Optional design evaluation uses the normal runner dispatch path and model budget:

```bash
svc exec service python3 -m service --settings /etc/concurro/service/settings.json \
  evaluate OWNER/FORK ISSUE codex
```

Use an idle configured task. Results remain under `/var/lib/concurro/service/evaluations/`.

## HTTPS edge

Concurro needs one HTTPS reverse proxy that:

- Holds a certificate GitHub and its hosted runners accept for `<host>`.
- Is attached to the Docker network `edge`. Create the network with `sudo docker network
  create edge` if the proxy's own Compose project does not.
- Routes `/concurro/*` as [`concurro.caddy`](concurro.caddy) does, stripping the prefix and
  streaming `/v1/*` and `/api/events` responses unbuffered.

Concurro publishes no host ports and its routes claim only `/concurro/*`, so the proxy can
serve other applications too. The gateway's unauthenticated admin listener stays on the
loopback it shares with the exchange, not the `edge` network. Other containers on that
network can reach the inference and exchange listeners, whose key, operator and OIDC
authorization still applies.

[`edge/`](edge/) is a ready proxy: Caddy on port 443, serving the certificate files in
`/etc/edge/tls` and every route file in `/etc/edge/sites`. It does not obtain certificates.

Use Caddy 2.11.7 or later: 2.11.6 [cuts HTTP/1.1 POST streams off after one
minute](https://github.com/caddyserver/caddy/issues/8103), interrupting model responses.
For an existing edge, update only its installed Compose image to the version and digest
in `edge/compose.yaml`, preserving host-specific networks, ports and mounts. Run
`sudo docker compose -f /etc/edge/compose.yaml pull caddy`, then
`sudo docker compose -f /etc/edge/compose.yaml up -d --no-deps caddy`. This briefly
restarts the shared proxy; Concurro's services and saved state remain in place.

```bash
sudo install -d /etc/edge/sites /etc/edge/domains
sudo install -d -m 0750 /etc/edge/tls
sudo install -m 0644 ~/concurro/gateway/edge/compose.yaml ~/concurro/gateway/edge/Caddyfile /etc/edge/
sudo install -m 0644 ~/concurro/gateway/concurro.caddy /etc/edge/sites/concurro.caddy
sudo docker compose -f /etc/edge/compose.yaml up -d
```

Put `fullchain.pem` and `privkey.pem` in `/etc/edge/tls` first.
[`edge/renewal-hook.sh`](edge/renewal-hook.sh) is a certbot deploy hook that copies a renewed
certificate there and reloads Caddy. It acts only on the certbot certificates listed in
`/etc/edge/certificate-name`, one per line; the first is the default certificate:

```bash
echo CERTIFICATE_NAME | sudo tee /etc/edge/certificate-name
sudo install -m 0755 ~/concurro/gateway/edge/renewal-hook.sh /etc/letsencrypt/renewal-hooks/deploy/60-edge
```

A host whose installed hook predates that file must create it before reinstalling the hook.

When a route changes, install only Concurro's file and validate before reloading; leave
other route files alone:

```bash
sudo install -m 0644 ~/concurro/gateway/concurro.caddy /etc/edge/sites/concurro.caddy
sudo docker compose -f /etc/edge/compose.yaml exec -T caddy caddy validate --config /etc/caddy/Caddyfile
sudo docker compose -f /etc/edge/compose.yaml exec -T caddy caddy reload --config /etc/caddy/Caddyfile --force
```

### Domain names

The default certificate covers the host's IP address. A domain name, with its DNS `A` record
pointing at the host, gets its own certbot certificate and a site file that serves the same
routes. Certbot uses port 80, which the edge leaves free. For `NAME`:

```bash
sudo certbot certonly --standalone -d NAME
echo NAME | sudo tee -a /etc/edge/certificate-name
sudo env RENEWED_LINEAGE=/etc/letsencrypt/live/NAME /etc/letsencrypt/renewal-hooks/deploy/60-edge
printf 'NAME {\n\ttls /tls/NAME/fullchain.pem /tls/NAME/privkey.pem\n\n\timport routes\n}\n' | sudo tee /etc/edge/domains/NAME.caddy
sudo docker compose -f /etc/edge/compose.yaml exec -T caddy caddy validate --config /etc/caddy/Caddyfile
sudo docker compose -f /etc/edge/compose.yaml exec -T caddy caddy reload --config /etc/caddy/Caddyfile --force
```

An edge installed before domain sites existed needs the current `Caddyfile`, `compose.yaml`
and hook first, and `sudo docker compose -f /etc/edge/compose.yaml up -d` to mount
`/etc/edge/domains`.

## Sponsorship metadata and private resolution

The vault serves `GET /metadata/{user_id}?at=<ISO timestamp>` and
`GET /metadata/events?after=N` to a separate metadata/service bearer. These responses contain
status/model/version/lifecycle evidence, never provider material. Owner endpoints reject this
bearer before GitHub lookup. Configure `VAULT_SERVICE_TOKEN_FILE` with an owner-only 32-byte
hex token, and an owner-only service copy through `vault_metadata_url` and
`vault_service_token_path`. See the [current deployment readiness guide](../docs/proposals/per-user-credentials/deployment-readiness.md)
for the complete principal, environment and mount wiring.

Subscription registration and contributed inference are implemented. Issues select funding
independently per provider; organization remains the default for new tasks. Personal selections
use confirmed consent and exact credential versions, with no organization fallback. Provider
credential defaults and optional profile overrides are described in the
[service design](../docs/design/service.md#provider-funding-and-credential-routes). Legacy
`funding_mode: contributed` keeps its existing initialization semantics. The earlier [phase 1](../docs/proposals/per-user-credentials/phase-1.md)
and [phase 3](../docs/proposals/per-user-credentials/phase-3.md) org-only boundaries are historical.
Current checkpoints use format 9, or 10 once a task selects from a credential list, and Registry schema 9. The [vault guide](vault/README.md)
describes schema 4 and the isolated gateway principal used for private events and resolution.

The release helper reads `/etc/concurro/gateway/compose.host.yaml`; merge the needed
`compose.private.yaml` network/listener/mount entries into that existing override, preserving
its persistent volume bindings. Merely leaving the optional overlay in the checkout does not
activate it. Accounts uses the vault's owner routes on `edge`; tasks use the
private resolver only through Exchange.

The helper reads `/etc/concurro/compose.service.host.yaml` when present on both
service and web updates. Keep this nonsecret override stat-able by the deploy user
and outside uid-65533 private settings. The retired Python portal override is not
part of the Rust deployment. Accounts queues encrypted logout revocations in its
own PostgreSQL database using the required `logout-key`.

## Contributed inference

[Phase 4](../docs/proposals/per-user-credentials/phase-4.md) describes the configured
four-route injection/observation path and its isolated evidence. Deploy gateway configuration,
Exchange and Compose together as a roll-forward release. Exchange’s new loopback-only
`EXCHANGE_PRIVATE_LISTEN=127.0.0.1:8081` serves pre-budget request attribution, exact-version
resolution and the narrow streaming provider hop; expose no Caddy route for it. Native request
logs use `/data/requests.db`, metadata only. Exchange reads them from the read-only gateway-data
mount at `EXCHANGE_NATIVE_LOG=/gateway-data/requests.db`; preserve both databases during recovery.
The existing separate private vault principal supplies events and material, while metadata auth
still cannot resolve secrets. Default task funding stays organization-owned; contributed tasks
require confirmed consent and complete required-route coverage, with no organization fallback.
Subscription token-paste registration is implemented in phase 5; real backend/account
acceptance remains pending. [Phase 6](../docs/proposals/per-user-credentials/phase-6.md)
adds service-principal nonsecret event polling and eligible automatic recovery. Registry
schema 8 migrates forward; preserve vault event sequence and Registry state when moving
services. No new secret or recovery enablement setting is required.

### Read API cache

The Rust read API retains indexed issue metadata and task projections in its own
PostgreSQL database. The retired Python backoffice cache and `backoffice-data`
volume do not participate in current reads.
