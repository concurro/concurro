# Host deployment: gateway and service

agentgateway plus Concurro's key exchange, and the Concurro service, behind the host's
shared edge Caddy. The host only coordinates and proxies inference: model turns and the
exploratory builds run on the targets' GitHub-hosted runners. Design:
[MVP execution](../docs/design/mvp-execution.md).

## Host layout

The gateway runs from a Git clone; everything host-specific lives under `/etc` and
`/var/lib`, so updating is a pull.

| Item | Location |
| ---- | ---- |
| Source (clone) | `~/concurro`; Compose projects `concurro-gateway` and `concurro-service` |
| Gateway host override (state mounts, other host specifics) | `/etc/concurro/gateway/compose.host.yaml` |
| Provider secrets (0600) | `/etc/concurro/gateway/secrets.env` |
| The service's operator token (0600) | `/etc/concurro/gateway/exchange.env`, as `EXCHANGE_OPERATOR_TOKEN` |
| Gateway state | `/var/lib/concurro/gateway/{inference,exchange}` |
| Service settings and secrets (0700, uid 65533) | `/etc/concurro/service`: `settings.json`, `github-app.pem`, `webhook-secret`, `gateway-operator-token` |
| Service state (0700, uid 65533) | `/var/lib/concurro/service`: the task registry and reports |
| Edge Compose project `edge` | `/etc/edge/compose.yaml`, `/etc/edge/Caddyfile` (copies of `edge/`) |
| Route files | `/etc/edge/sites/*.caddy`; Concurro's is a copy of `concurro.caddy` |
| Edge certificate | `/etc/edge/tls`, refreshed by `edge/renewal-hook.sh` |
| Renewal hook | `/etc/letsencrypt/renewal-hooks/deploy/60-edge` |

The gateway joins the external `edge` network as `concurro-gateway`, the service as
`concurro-service`; neither publishes ports. Clients use `https://<host>/concurro` as
the base URL, and the GitHub App delivers webhooks to `https://<host>/concurro/webhook`.
The service reaches the exchange's operator API at the same base URL with the operator
token; runner jobs reach it with their OIDC token and then their session's runner token.
The service's image holds the clone's commit, which it serves; runner sessions must run
the same commit, so deploy the service and the `deployed` workflow ref together. Ordinary `main` pushes do not change the deployed release.

## Target and App prerequisites

The target fork needs the [caller workflow](../examples/stacks-core/.github/workflows/concurro-executor.yml)
on its default branch, calling `agent.yml@deployed`, and the current `.concurro/` config
and charter. The upstream and fork must be public and in the same fork network. No new
fork is created. Optional setup is a composite action at `.concurro/setup/action.yml`.
The caller passes the repository secret `CONCURRO_GATEWAY_URL` explicitly to the reusable
workflow and may set `runs-on`; the service dispatch input is only the issued session ID.
Both jobs use `cache-mode: read`. Store the deployment URL as a secret, not a workflow
input, variable or source default, so GitHub masks it from the start of the job:

```bash
gh secret set CONCURRO_GATEWAY_URL --repo OWNER/FORK
```

Enter the URL at the prompt. It must match `gateway.url` in the service settings: the
workflow uses the secret for the exchange, while turn payloads use the service setting
for inference. Never pass provider keys or the exchange operator token to runner workflows,
and do not use `secrets: inherit`.

For the secret-interface migration, finish active tasks and runner jobs, then stop the
service before landing the updated fork caller. Keep dispatch stopped while deploying the
matching engine and moving `deployed`; restart only after both sides are updated. A new
caller forwarding an undeclared secret and an old caller omitting a required secret are
both incompatible. Ordinary pushes to Concurro's `main` do not move `deployed`.

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
review comments, pull request review threads, check runs, check suites and commit statuses.
The webhook URL is `https://<host>/concurro/webhook`. Periodic polling covers lost events.
Upstream may require a maintainer to approve the first fork PR's workflow runs; Concurro
shows that wait and does not bypass it.

The stacks-core example charter requires an upstream that permits the contribution. Use
a permitted test upstream for the pilot; the target's contribution policy still governs.

## Upgrade from the workbench release

Commit and push the reviewed source before building the service: it refuses a dirty
checkout. The following are operator-run deployment instructions, not automatic actions.
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
4. Build the new server images and migrate service settings as shown below. The target
   settings now use `{upstream, fork, issues}`; execution-image and per-target credential
   settings are gone. Preserve all service secrets, state paths and `registry_id`.
5. While the service remains stopped, move Concurro's `deployed` branch to the same
   reviewed engine commit. Start the gateway and service; verify the logged served SHA
   equals that release ref. Only then enroll a fresh pilot issue.

Use the existing host mounts and Compose project names:

```bash
gw() { sudo docker compose -p concurro-gateway -f ~/concurro/gateway/compose.yaml -f /etc/concurro/gateway/compose.host.yaml "$@"; }
svc() { sudo docker compose -f ~/concurro/service/compose.yaml "$@"; }
```

After stopping/backing up and checking out the committed release:

```bash
gw build
svc build
svc run --rm --volume /etc/concurro/service:/etc/concurro/service:rw \
  --entrypoint python3 service -m service.use_gateway \
  --settings /etc/concurro/service/settings.json \
  --url https://gateway.example.com/concurro \
  --operator-token /etc/concurro/service/gateway-operator-token \
  --upstream stacks-network/stacks-core \
  --fork cylewitruk/stacks-core-autobot --issues fork
```

The one-off command overrides the normally read-only settings mount. It runs as the
service's uid 65533, validates a private temporary file, then atomically replaces settings.
The running service keeps its read-only mount. Multi-target settings must be edited
explicitly; the helper refuses to guess their pairs. Back up settings before replacing them.

With the host checkout at the reviewed commit and dispatch still stopped, an authorized
operator updates the release ref (this is a GitHub write):

```bash
git -C ~/concurro push origin HEAD:refs/heads/deployed
gw up -d
svc up -d
svc logs --tail 30 service
```

A non-fast-forward release move needs explicit operator reconciliation, not a blind
force push. The new registry migrates schema 2 to 3 transactionally. Exchange columns are
added without clearing history; old unbound pending turns are lost rather than staffed
under new authority. In-flight checkpoints retain their old bundle identity and block
for reconciliation. A fresh issue is the straightforward pilot path; never replace a
checkpoint's bundle hash to make old approvals look current. A blocked implementation
can instead use a writer-authorized retry when only the engine implementer skill changed
and the controller proves compatibility. Keep local engine Git history for this check on
legacy tasks; see [recovery](../docs/concurro.md#commands-and-recovery). Other bundle
changes still require explicit invalidation.

For rollback, stop the new service and jobs first. Restore the matching backed-up stores,
settings and old committed server release together, then align its workflow ref. Do not
run old code against a newer registry schema or discard unknown-write records.

## Operation and pilot

```bash
svc exec service python3 -m service --settings /etc/concurro/service/settings.json status
svc exec service python3 -m service --settings /etc/concurro/service/settings.json reconcile-once
svc logs -f service
gw exec -T exchange python3 -I /app/exchange.py list
gw exec -T exchange python3 -I /app/exchange.py turns
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
  evaluate cylewitruk/stacks-core-autobot 123 codex
```

Use an idle configured task. Results remain under `/var/lib/concurro/service/evaluations/`.

## Shared edge

Keep the existing edge and SBGH routes. Concurro publishes no host ports; its route file
serves only `/concurro/*`. The gateway's unauthenticated admin listener remains on shared
loopback with the exchange, not the edge network. Other edge services can reach the
inference/exchange listeners, whose key/operator/OIDC authorization still applies.

When a route or renewal hook changes, install only Concurro's reviewed file and validate
before reloading; do not replace unrelated route files:

```bash
sudo install -m 0644 ~/concurro/gateway/concurro.caddy /etc/edge/sites/concurro.caddy
sudo docker compose -f /etc/edge/compose.yaml exec -T caddy caddy validate --config /etc/caddy/Caddyfile
sudo docker compose -f /etc/edge/compose.yaml exec -T caddy caddy reload --config /etc/caddy/Caddyfile --force
```

The existing `60-edge` renewal hook handles `sbgh-fleet-ip`; port 80 stays with certbot.
This refactor does not require changing certificates, SBGH's listener or its routes.
