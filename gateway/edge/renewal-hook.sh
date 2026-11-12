#!/bin/sh
# Certbot deploy hook: give the edge Caddy its renewed certificates and reload it.
# Installed as /etc/letsencrypt/renewal-hooks/deploy/60-edge. /etc/edge/certificate-name
# lists the certbot certificates the edge serves, one name per line: the first is the host's
# default certificate in /etc/edge/tls, each other one goes to /etc/edge/tls/<name> for a
# domain site. Other renewals are ignored. Run without RENEWED_LINEAGE, it installs them all.
set -eu
EDGE_COMPOSE="${EDGE_COMPOSE:-/etc/edge/compose.yaml}"
EDGE_TLS="${EDGE_TLS:-/etc/edge/tls}"
installed=
default=1
while IFS= read -r name || [ -n "$name" ]; do
    [ -n "$name" ] || continue
    if [ -n "$default" ]; then target="$EDGE_TLS"; else target="$EDGE_TLS/$name"; fi
    default=
    lineage="${RENEWED_LINEAGE:-/etc/letsencrypt/live/$name}"
    if [ "${lineage##*/}" = "$name" ]; then
        install -d -m 0750 "$target"
        install -m 0644 "$lineage/fullchain.pem" "$target/fullchain.pem"
        install -m 0600 "$lineage/privkey.pem" "$target/privkey.pem"
        installed=1
    fi
done <"${EDGE_CERTIFICATE_NAME_FILE:-/etc/edge/certificate-name}"
[ -n "$installed" ] || exit 0
if docker compose -f "$EDGE_COMPOSE" ps --status running caddy | grep -q caddy; then
    docker compose -f "$EDGE_COMPOSE" exec -T caddy caddy reload --config /etc/caddy/Caddyfile --force
fi
