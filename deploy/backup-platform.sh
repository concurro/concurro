#!/usr/bin/env bash
# Nightly full PostgreSQL backup for the private Concurro platform.
set -euo pipefail
umask 077
directory=${1:-/var/backups/concurro/platform}
mkdir -p "$directory"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
archive="$directory/postgres-$stamp.sql.gz"
temporary="$archive.tmp"
trap 'rm -f "$temporary"' EXIT
sudo -n docker compose -f "$HOME/concurro/deploy/compose.platform.yaml" \
  exec -T -u postgres postgres pg_dumpall -U postgres | gzip -9 > "$temporary"
mv "$temporary" "$archive"
echo "$archive"
