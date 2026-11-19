#!/usr/bin/env bash
# Server half of deploy.sh. `service` stops dispatch, pulls a release's images and starts them;
# `web` moves the backoffice web app on its own; `all` updates both release tags together.
set -euo pipefail
phase=${1:?Missing phase}
release=${2:?Missing release}
[[ "$phase" == service || "$phase" == web || "$phase" == all ]]
[[ "$release" =~ ^[0-9a-f]{40}$ ]]
web_release=${3:-}
if [[ "$phase" == all ]]; then
  [[ "$web_release" =~ ^[0-9a-f]{40}$ ]]
fi
cd "$HOME/concurro"
exec 9>"$HOME/.local/lib/concurro-deploy/lock"
flock -n 9 || { echo 'Another deployment phase is running.' >&2; exit 1; }
# The image tags Compose runs: CONCURRO_RELEASE and CONCURRO_WEB_RELEASE.
releases="$HOME/concurro-release.env"
touch "$releases"
svc() {
  local files=(-f "$HOME/concurro/service/compose.yaml")
  if [[ -f /etc/concurro/ingress/settings.toml ]]; then
    files+=(-f "$HOME/concurro/service/compose.ingress.yaml")
  fi
  if [[ -f /etc/concurro/compose.service.host.yaml ]]; then
    files+=(-f /etc/concurro/compose.service.host.yaml)
  fi
  sudo -n docker compose --env-file "$releases" "${files[@]}" "$@"
}
gw() { sudo -n docker compose --env-file "$releases" -p concurro-gateway -f "$HOME/concurro/gateway/compose.yaml" -f /etc/concurro/gateway/compose.host.yaml "$@"; }
plat() { sudo -n docker compose -f "$HOME/concurro/deploy/compose.platform.yaml" "$@"; }
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
[[ "$phase" == all ]] || grep -q '^CONCURRO_WEB_RELEASE=' "$releases" || { echo 'No web release recorded; run "deploy.sh web FULL_COMMIT_SHA" first.' >&2; exit 1; }
# Older releases cannot open a newer registry.
current=$(sed -n 's/^CONCURRO_RELEASE=//p' "$releases")
if [[ -n "$current" ]] && ! git merge-base --is-ancestor "$current" "$release"; then
  echo "Release $release does not include the deployed $current; refusing to move backward or sideways." >&2
  exit 1
fi
settings_file=/etc/concurro/service/settings.json
staged_file=/etc/concurro/service/settings.next.json
candidate=$settings_file
# Validate the candidate with the release image before switching the checkout or stopping services.
sudo -n docker pull "ghcr.io/concurro/service:$release"
if sudo -n docker run --rm --network none --volume /etc/concurro/service:/etc/concurro/service:ro \
  --entrypoint python3 "ghcr.io/concurro/service:$release" -c \
  'import os, sys; sys.exit(not os.path.isfile("/etc/concurro/service/settings.next.json"))'; then
  candidate=$staged_file
fi
sudo -n docker run --rm --network none --volume /etc/concurro/service:/etc/concurro/service:ro \
  --entrypoint python3 "ghcr.io/concurro/service:$release" -c \
  'import json, sys; s=json.load(open(sys.argv[2])); assert s.get("engine") in (None, sys.argv[1]), "Service settings have a different engine pin; update it explicitly before deploying."' "$release" "$candidate"
sudo -n docker run --rm --network none \
  --volume /etc/concurro/service:/etc/concurro/service:ro \
  --volume /var/lib/concurro/service:/var/lib/concurro/service:ro \
  --volume /var/lib/concurro/diagnostics:/var/lib/concurro/diagnostics:ro \
  --entrypoint python3 "ghcr.io/concurro/service:$release" -c \
  'from service.settings import load; from service.__main__ import host_checks; import sys; s=host_checks(load(sys.argv[2])); assert s.engine == sys.argv[1]' "$release" "$candidate"
git switch --detach "$release"
if [[ -z "$current" && "$phase" == all ]]; then
  record CONCURRO_WEB_RELEASE "$web_release"
fi
if [[ -z "$current" ]]; then
  record CONCURRO_RELEASE "$release"
fi
if [[ -f /etc/concurro/ingress/settings.toml ]]; then
  svc stop service ingress
  plat config --quiet
  plat up -d --wait --wait-timeout 120
else
  svc stop service
fi
if [[ "$candidate" == "$staged_file" ]]; then
  sudo -n docker run --rm --network none --volume /etc/concurro/service:/etc/concurro/service \
    --entrypoint python3 "ghcr.io/concurro/service:$release" -c \
    'import os, shutil; source, target = "/etc/concurro/service/settings.next.json", "/etc/concurro/service/settings.json"; shutil.copy2(target, target + ".previous"); os.replace(source, target)'
fi
if [[ -n "$current" ]]; then
  if [[ "$phase" == all ]]; then
    record CONCURRO_WEB_RELEASE "$web_release"
  fi
  record CONCURRO_RELEASE "$release"
fi
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
