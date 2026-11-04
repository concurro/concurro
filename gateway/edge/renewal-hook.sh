#!/bin/sh
# Certbot deploy hook: give the edge Caddy the renewed IP certificate and reload it.
# Installed as /etc/letsencrypt/renewal-hooks/deploy/60-edge.
set -eu
LINEAGE="${RENEWED_LINEAGE:-/etc/letsencrypt/live/sbgh-fleet-ip}"
[ "${LINEAGE##*/}" = "sbgh-fleet-ip" ] || exit 0
install -d -m 0750 /etc/edge/tls
install -m 0644 "$LINEAGE/fullchain.pem" /etc/edge/tls/fullchain.pem
install -m 0600 "$LINEAGE/privkey.pem" /etc/edge/tls/privkey.pem
if docker compose -f /opt/edge/compose.yaml ps --status running caddy | grep -q caddy; then
    docker compose -f /opt/edge/compose.yaml exec -T caddy caddy reload --config /etc/caddy/Caddyfile --force
fi
