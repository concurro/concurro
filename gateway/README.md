# Inference gateway deployment

agentgateway plus Concurro's key exchange, behind the host's shared edge Caddy.
Design: [docs/design/inference-gateway.md](../docs/design/inference-gateway.md).

## Host layout

The gateway runs from a Git clone; everything host-specific lives under `/etc` and
`/var/lib`, so updating is a pull.

| Item | Location |
| ---- | ---- |
| Source (clone) | `~/concurro`, Compose project `concurro-gateway` |
| Host override (state mounts, other host specifics) | `/etc/concurro/gateway/compose.host.yaml` |
| Provider secrets (0600) | `/etc/concurro/gateway/secrets.env` |
| The service's operator token (0600) | `/etc/concurro/gateway/exchange.env`, as `EXCHANGE_OPERATOR_TOKEN` |
| Persistent state | `/var/lib/concurro/gateway/{inference,exchange}` |
| Edge Compose project `edge` | `/etc/edge/compose.yaml`, `/etc/edge/Caddyfile` (copies of `edge/`) |
| Route files | `/etc/edge/sites/*.caddy`; Concurro's is a copy of `concurro.caddy` |
| Edge certificate | `/etc/edge/tls`, refreshed by `edge/renewal-hook.sh` |
| Renewal hook | `/etc/letsencrypt/renewal-hooks/deploy/60-edge` |

The gateway joins the external `edge` network as `concurro-gateway` and publishes
no ports. Clients use `https://<host>/concurro` as the base URL. The Concurro
service reaches the exchange's operator API there with the operator token; runner
jobs reach it with their OIDC token and then their session's runner token.

## Commands

Shorthand used below:

```bash
gw() { sudo docker compose -p concurro-gateway -f ~/concurro/gateway/compose.yaml -f /etc/concurro/gateway/compose.host.yaml "$@"; }
```

Update:

```bash
git -C ~/concurro pull --ff-only && gw up -d --build
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
