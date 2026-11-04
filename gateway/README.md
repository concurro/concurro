# Host deployment: gateway and service

agentgateway plus Concurro's key exchange, and the Concurro service, behind the host's
shared edge Caddy. The host only coordinates and proxies inference: model turns and the
trusted workbench run on the targets' GitHub-hosted runners. Design:
[docs/design/inference-gateway.md](../docs/design/inference-gateway.md).

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
the same commit, so rebuild the service whenever `main` moves.

## Commands

Shorthand used below:

```bash
gw() { sudo docker compose -p concurro-gateway -f ~/concurro/gateway/compose.yaml -f /etc/concurro/gateway/compose.host.yaml "$@"; }
```

```bash
svc() { sudo docker compose -f ~/concurro/service/compose.yaml "$@"; }
```

Update both:

```bash
git -C ~/concurro pull --ff-only && gw up -d --build && svc up -d --build
```

Service commands (`status`, `reconcile-once`, `adopt`, `release`, `resolve`) and logs:

```bash
svc exec service python3 -m service --settings /etc/concurro/service/settings.json status
```

```bash
svc logs -f service
```

When `concurro.caddy` or `edge/renewal-hook.sh` changed, re-install the copy and
reload the edge:

```bash
sudo install -m 0644 ~/concurro/gateway/concurro.caddy /etc/edge/sites/concurro.caddy
```

```bash
sudo install -m 0755 ~/concurro/gateway/edge/renewal-hook.sh /etc/letsencrypt/renewal-hooks/deploy/60-edge
```

```bash
sudo docker compose -f /etc/edge/compose.yaml exec -T caddy caddy reload --config /etc/caddy/Caddyfile --force
```

Exchange operator commands (`issue`, `list`, `turns`, `end`, `reconcile`):

```bash
gw exec -T exchange python3 -I /app/exchange.py list
```
