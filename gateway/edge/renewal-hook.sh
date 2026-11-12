#!/bin/sh
# Certbot deploy hook: give the edge Caddy its renewed certificate and reload it.
# Installed as /etc/letsencrypt/renewal-hooks/deploy/60-edge. /etc/edge/certificate-name
# holds the name of the certbot certificate the edge serves; other renewals are ignored.
set -eu
EDGE_COMPOSE="${EDGE_COMPOSE:-/etc/edge/compose.yaml}"
NAME=$(cat "${EDGE_CERTIFICATE_NAME_FILE:-/etc/edge/certificate-name}")
LINEAGE="${RENEWED_LINEAGE:-/etc/letsencrypt/live/$NAME}"
[ "${LINEAGE##*/}" = "$NAME" ] || exit 0
install -d -m 0750 /etc/edge/tls
install -m 0644 "$LINEAGE/fullchain.pem" /etc/edge/tls/fullchain.pem
install -m 0600 "$LINEAGE/privkey.pem" /etc/edge/tls/privkey.pem
if docker compose -f "$EDGE_COMPOSE" ps --status running caddy | grep -q caddy; then
    docker compose -f "$EDGE_COMPOSE" exec -T caddy caddy reload --config /etc/caddy/Caddyfile --force
fi
