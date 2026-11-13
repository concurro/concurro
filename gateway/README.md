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
| Service settings and secrets (0700, uid 65533) | `/etc/concurro/service`: `settings.json`, `github-app.pem`, `webhook-secret`, `gateway-operator-token` |
| Service state (0700, uid 65533) | `/var/lib/concurro/service`: the task registry and reports |
| Backoffice settings and OAuth client secret (0700, uid 65533) | `/etc/concurro/backoffice`: `settings.json`, `client-secret` (see `backoffice/README.md`) |
| Backoffice command signing key (0600, uid 65533) | `/etc/concurro/service/delegation-key`, enabled by `"delegation_key_path"` in the service's `settings.json`; create it with `openssl rand -hex 32` |
| Run diagnostics (0700, uid 65533) | `/var/lib/concurro/diagnostics`: per-turn requests, prompts, results and transcripts, recorded when `settings.json` sets `"diagnostics_dir"` to this path |
| Bundled edge, if used: Compose project `edge` | `/etc/edge/compose.yaml`, `/etc/edge/Caddyfile` (copies of `edge/`) |
| Bundled edge route files | `/etc/edge/sites/*.caddy`; Concurro's is a copy of `concurro.caddy` |
| Bundled edge certificates | `/etc/edge/tls/{fullchain,privkey}.pem`; a domain name's in `/etc/edge/tls/<name>/` |
| Bundled edge domain sites | `/etc/edge/domains/<name>.caddy` |

The gateway joins the external `edge` network as `concurro-gateway`, the service as
`concurro-service`; neither publishes ports. Clients use `https://<host>/concurro` as
the base URL, and the GitHub App delivers webhooks to `https://<host>/concurro/webhook`.
The service reaches the exchange's operator API at the same base URL with the operator
token; runner jobs reach it with their OIDC token and then their session's runner token.
The service's image holds the release's commit, which it serves. Every runner session names
that commit and the launcher workflow runs it, so a deployment changes nothing on GitHub and
ordinary `main` pushes do not change the engine a deployment runs.

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
sudo install -d -m 0700 -o 65533 -g 65533 /etc/concurro/service /etc/concurro/backoffice \
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
- Callback URL `https://<host>/concurro/auth/callback`, for backoffice sign-in.
- Install it on each upstream and fork.

**5. Service settings**

In `/etc/concurro/service`, each a regular file owned by uid 65533 with mode 0600:
`github-app.pem` (the App's private key), `webhook-secret`, `gateway-operator-token` (the
token from `exchange.env`), `delegation-key` and `settings.json`. The delegation key signs
the commands the backoffice delivers; without it they are refused with 503:

```bash
sudo sh -c 'umask 077 && openssl rand -hex 32 > /etc/concurro/service/delegation-key'
sudo chown 65533:65533 /etc/concurro/service/delegation-key
```

```json
{
  "controller_id": "service:main",
  "app_id": 123456,
  "private_key_path": "/etc/concurro/service/github-app.pem",
  "webhook_secret_path": "/etc/concurro/service/webhook-secret",
  "state_dir": "/var/lib/concurro/service",
  "registry_id": "main",
  "targets": [{"upstream": "OWNER/UPSTREAM", "fork": "OWNER/FORK", "issues": "fork"}],
  "limits": {"global": 4, "per_repository": 2},
  "gateway": {
    "url": "https://<host>/concurro",
    "operator_token_path": "/etc/concurro/service/gateway-operator-token",
    "executor": "runner",
    "workflow_ref": "concurro/concurro/.github/workflows/agent.yml@refs/heads/main",
    "models": ["claude-*", "gpt-*"],
    "budget": 20000000,
    "headers": {"codex": {"x-concurro-plan": "chatgpt"}}
  },
  "diagnostics_dir": "/var/lib/concurro/diagnostics",
  "delegation_key_path": "/etc/concurro/service/delegation-key"
}
```

`headers.codex` selects the ChatGPT subscription route; use `"headers": {}` with an OpenAI
API key. `issues` is `upstream`, `fork` or `both`. Omit `engine`: the service then serves
the release it was deployed with. Backoffice settings are in
[`backoffice/README.md`](../backoffice/README.md#settings).

**6. Targets**

Prepare each fork as in [Target and App prerequisites](#target-and-app-prerequisites): the
caller workflow, `.concurro/` and the `CONCURRO_GATEWAY_URL` secret.

**7. Registry**

Create the empty task registry once, with the release you are about to deploy. The command
validates the settings, creates the registry and exits; it contacts nothing and starts no
task processing, and refuses to run over an existing registry:

```bash
sudo docker run --rm --network none --volume /etc/concurro/service:/etc/concurro/service:ro \
  --volume /var/lib/concurro/service:/var/lib/concurro/service \
  ghcr.io/concurro/service:FULL_COMMIT_SHA initialize
```

**8. Deploy**

Deploy `web`, then `service`, as in [Releases](#releases). Check the result with the
commands in [Operation and pilot](#operation-and-pilot).

## Releases

CI publishes an image per component to GHCR for every `main` commit, tagged with the full
commit SHA. Nothing is built on the host.

| Image | Source |
| ---- | ---- |
| `ghcr.io/concurro/service` | `service/Dockerfile`; holds the commit's checkout with its history |
| `ghcr.io/concurro/backoffice-api` | `backoffice/Dockerfile` |
| `ghcr.io/concurro/exchange`, `ghcr.io/concurro/vault` | `gateway/exchange`, `gateway/vault` |
| `ghcr.io/concurro/backoffice-web` | [concurro-backoffice](https://github.com/concurro/concurro-backoffice), released on its own |

The packages must be public, or the host's root Docker logged in to `ghcr.io` with a
`read:packages` token. Deploy from a workstation with `gh` and SSH access to the host:

```bash
export CONCURRO_DEPLOY_HOST=<ssh name>
```

```bash
bash deploy/deploy.sh web FULL_COMMIT_SHA || printf '\nDeployment failed; this terminal remains open.\n'
```

```bash
bash deploy/deploy.sh service FULL_COMMIT_SHA || printf '\nDeployment failed; this terminal remains open.\n'
```

- **`web`** takes a concurro-backoffice commit: it waits for that commit's CI, records
  `CONCURRO_WEB_RELEASE`, then pulls and restarts only the web app. The service keeps running.
  A host that has never deployed images only records the release; the first `service`
  deployment needs it and starts the web app.
- **`service`** takes a commit of this repository: it waits for its CI, checks out the
  release's Compose files, stops the service, pulls and verifies the images, starts the
  gateway and the service with the backoffice, and checks health and the served commit. It
  refuses a release that does not include the one recorded. A failure after stopping can
  leave the service stopped; fix the reported problem and rerun. Nothing is rolled back,
  migrated or retried, and nothing is written to GitHub.

Host Docker commands use non-interactive `sudo docker` only. Never source either script.
The `build` sections in the Compose files remain for local builds, for example
`CONCURRO_RELEASE=$(git rev-parse HEAD) docker compose -f gateway/compose.yaml build`.

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
and publishes no port: other `edge` services, such as the backoffice API, reach it as
`http://concurro-vault:8090`. Owner credential requests carry
`Authorization: Bearer <GitHub user token>`; the vault resolves the user with
`GET https://api.github.com/user` (cached for five minutes by token SHA-256) and answers
401 for a rejected token.

| Request | Result |
| ---- | ---- |
| `GET /credentials` | `[{route, status, fingerprint, version, updated}]`, latest version per route |
| `PUT /credentials/{route}` `{"secret": "..."}` | Validates with the provider, stores a new version, returns its entry; 422 if rejected, 502 if the provider gives no verdict |
| `DELETE /credentials/{route}` | Marks it `revoked` and destroys its ciphertext; returns its entry |
| `GET /health` | `{"status": "ok"}` |

Routes are `anthropic-api-key`, `openai-api-key`, `chatgpt-subscription` and
`claude-subscription`. Subscription PUTs also carry a `models` array; ChatGPT requires
`account_id`. Each distinct selected model is probed with the supplied token before one
atomic replacement. See [phase 5](../docs/proposals/per-user-credentials/phase-5.md).
`status` is `valid`, `invalid` or
`revoked`; `fingerprint` is the secret's last 4 characters and the first 8 hex characters
of its SHA-256. Secrets are never returned or logged. Each version is sealed with its own
AES-256-GCM data key, which the master key wraps; only the latest version keeps
ciphertext. Records live in SQLite (WAL) at `/data/vault.db`. Configured contributed
inference resolves exact admitted versions through the separate private listener; org-funded
work keeps org credentials. The service metadata principal never receives secrets. See the
[vault lifecycle/private-listener guide](vault/README.md).

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
issues enroll, and Actions write plus Workflows write on the fork. Fork read tokens explicitly request
Actions read for runner-termination diagnosis; this uses the already required fork
Actions-write installation grant. Token requests narrow these permissions
per repository; the write gateway narrows permitted endpoints further. Retain Contents
write upstream for the tested review-thread resolution permission combination. No
Administration permission is needed for readiness. Historical `adopt` additionally needs
Actions, Variables and Environments read to verify the retired Actions controller is off.

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
The webhook URL is `https://<host>/concurro/webhook`. App-wide delivery recovery runs every
five minutes; unchanged idle PRs are not polled. Bootstrap and gaps beyond GitHub's
three-day delivery history use state reconciliation. The existing App JWT signs recovery
requests; no installation-token permission expansion is required.

The current service upgrades the registry to schema 8, retaining the coverage/observation
jobs introduced in schema 5. Back up the stopped registry and preserve its identity; use
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
force push. The current registry migrates schemas 2–7 to schema 8 transactionally. Exchange columns are
added without clearing history; old unbound pending turns are lost rather than staffed
under new authority. In-flight checkpoints retain their old bundle identity and block
for reconciliation. A fresh issue is the straightforward pilot path; never replace a
checkpoint's bundle hash to make old approvals look current. A blocked implementation
can instead use a writer-authorized retry when only the engine implementer skill changed
and the controller proves compatibility. Keep local engine Git history for this check on
legacy tasks; see [recovery](../docs/concurro.md#commands-and-recovery).
[`/concurro resume`](../docs/design/checkpoint-resume.md) adopts current configuration
and instructions while preserving completed work and original approval bindings.
It records explicit writer authority, without a release compatibility certificate.
Resume receipts require at least checkpoint format 6; large inventories require format 5.
Artifact consolidation introduced format 7; shadow sponsorship introduced format 8.
Frozen admissions now write **format 9 on every charged checkpoint save**; Registry schema 8
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
  streaming `/v1/*` responses unbuffered.

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

Subscription registration and contributed inference are implemented. Default target funding
remains org-owned; explicit contributed targets use confirmed per-provider consent and exact
credential versions. The earlier [phase 1](../docs/proposals/per-user-credentials/phase-1.md)
and [phase 3](../docs/proposals/per-user-credentials/phase-3.md) org-only boundaries are historical.
Current checkpoints use format 9 and Registry schema 8. The [vault guide](vault/README.md)
describes schema 3 and the isolated gateway principal used for private events and resolution.

The release helper reads `/etc/concurro/gateway/compose.host.yaml`; merge the needed
`compose.private.yaml` network/listener/mount entries into that existing override, preserving
its persistent volume bindings. Merely leaving the optional overlay in the checkout does not
activate it. Service/backoffice remain outside the private resolver network.

For optional durable GitHub logout cleanup, the helper also reads
`/etc/concurro/compose.service.host.yaml` **when present** on both service and web updates. The override is nonsecret and must be stat-able by the
deploy user; keep it outside the uid-65533/0700 service settings directory.
Merge `service/compose.portal.yaml` there and provision its state/key before setting
`auth_state_dir`/`auth_key_path`. Without that optional file/configuration, existing base-only
service deployments are unchanged. Do not edit tracked Compose files on the release host.

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
