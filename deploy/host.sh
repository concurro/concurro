#!/usr/bin/env bash
# Server half of deploy.sh. `service` stops dispatch, pulls a release's images and starts them;
# `web` moves the backoffice web app on its own.
set -euo pipefail
phase=${1:?Missing phase}
release=${2:?Missing release}
[[ "$phase" == service || "$phase" == web ]]
[[ "$release" =~ ^[0-9a-f]{40}$ ]]
cd "$HOME/concurro"
exec 9>"$HOME/.local/lib/concurro-deploy/lock"
flock -n 9 || { echo 'Another deployment phase is running.' >&2; exit 1; }
# The image tags Compose runs: CONCURRO_RELEASE and CONCURRO_WEB_RELEASE.
releases="$HOME/concurro-release.env"
touch "$releases"
svc() { sudo -n docker compose --env-file "$releases" -f "$HOME/concurro/service/compose.yaml" "$@"; }
gw() { sudo -n docker compose --env-file "$releases" -p concurro-gateway -f "$HOME/concurro/gateway/compose.yaml" -f /etc/concurro/gateway/compose.host.yaml "$@"; }
record() {
  { grep -v "^$1=" "$releases" || true; echo "$1=$2"; } > "$releases.tmp"
  mv "$releases.tmp" "$releases"
}
failed() {
  code=$?
  trap - ERR
  echo "Deployment phase '$phase' failed. Do not retry the pilot; inspect the output above." >&2
  exit "$code"
}
trap failed ERR

if [[ "$phase" == web ]]; then
  record CONCURRO_WEB_RELEASE "$release"
  # Before the first service deployment there are no Compose releases to run it beside.
  if grep -q '^CONCURRO_RELEASE=' "$releases"; then
    svc pull web
    svc up -d --no-build --wait --wait-timeout 120 web
    echo 'Web app is running and healthy.'
  else
    echo 'Web release recorded; it starts with the first service deployment.'
  fi
  exit
fi

[[ -z "$(git status --porcelain)" ]] || { echo 'Server checkout has local changes; refusing to overwrite them.' >&2; exit 1; }
git fetch origin main
git merge-base --is-ancestor "$release" origin/main
grep -q '^CONCURRO_WEB_RELEASE=' "$releases" || { echo 'No web release recorded; run "deploy.sh web FULL_COMMIT_SHA" first.' >&2; exit 1; }
# Older releases cannot open a newer registry.
current=$(sed -n 's/^CONCURRO_RELEASE=//p' "$releases")
if [[ -n "$current" ]] && ! git merge-base --is-ancestor "$current" "$release"; then
  echo "Release $release does not include the deployed $current; refusing to move backward or sideways." >&2
  exit 1
fi
# Refuse mismatched explicit pins without changing settings or secrets. The release's own
# image reads them, so this also works before any release is recorded.
sudo -n docker run --rm --network none --volume /etc/concurro/service:/etc/concurro/service:ro \
  --entrypoint python3 "ghcr.io/concurro/service:$release" -c \
  'import json, sys; s=json.load(open("/etc/concurro/service/settings.json")); assert s.get("engine") in (None, sys.argv[1]), "Service settings have a different engine pin; update it explicitly before deploying."' "$release"
git switch --detach "$release"
record CONCURRO_RELEASE "$release"
svc stop service
gw config --quiet
svc config --quiet
gw pull
svc pull
svc run --rm --no-deps --entrypoint python3 service -c \
  'from service.settings import load; from service.__main__ import host_checks; import sys; s=host_checks(load("/etc/concurro/service/settings.json")); assert s.engine == sys.argv[1]; print("Pulled service verified:", s.engine)' "$release"
gw up -d --no-build
svc up -d --no-build --wait --wait-timeout 180
container=$(svc ps -q service)
started=$(sudo -n docker inspect --format '{{.State.StartedAt}}' "$container")
logs=$(svc logs --since "$started" --no-color service)
printf '%s\n' "$logs"
[[ "$logs" == *"serving engine $release"* ]]
echo 'Startup, health and served commit checks passed.'
