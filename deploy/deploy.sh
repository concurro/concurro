#!/usr/bin/env bash
# Deploy CI-published images; execute with bash, never source this file.
#   deploy.sh service FULL_COMMIT_SHA   a concurro release: service, backoffice API and gateway
#   deploy.sh web FULL_COMMIT_SHA       a concurro-backoffice release: the web app only
set -euo pipefail
usage='Usage: bash deploy.sh service|web FULL_COMMIT_SHA'
component=${1:?$usage}
release=${2:?$usage}
[[ "$component" == service || "$component" == web ]] || { echo "$usage" >&2; exit 1; }
[[ "$release" =~ ^[0-9a-f]{40}$ ]] || { echo 'Use a full commit SHA.' >&2; exit 1; }
host=${CONCURRO_DEPLOY_HOST:?Set CONCURRO_DEPLOY_HOST to the SSH name of the deployment host}
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo=$(dirname -- "$script_dir")
trap 'echo "Deployment stopped. Your shell remains open. Do not retry the pilot until deployment succeeds." >&2' ERR
[[ "$component" == service ]] && source_repo=concurro/concurro || source_repo=concurro/concurro-backoffice

# The release's push CI run on main publishes its images; wait for it to succeed.
await_ci() {
  local slug=$1 run conclusion
  run=$(gh run list --repo "$slug" --workflow ci.yml --event push --branch main \
    --commit "$release" --limit 1 --json databaseId --jq '.[0].databaseId // empty')
  [[ -n "$run" ]] || { echo "No push CI run on main found for $release in $slug; rerun after it appears." >&2; exit 1; }
  gh run watch "$run" --repo "$slug" --exit-status
  conclusion=$(gh run view "$run" --repo "$slug" --json conclusion --jq .conclusion)
  [[ "$conclusion" == success ]] || { echo "CI is not successful: $conclusion" >&2; exit 1; }
}

if [[ "$component" == service ]]; then
  git -C "$repo" fetch origin main
  if ! git -C "$repo" merge-base --is-ancestor "$release" origin/main; then
    echo "Release $release is not on origin/main. Push the committed release before deploying." >&2
    exit 1
  fi
fi
await_ci "$source_repo"

# A fixed helper avoids piping shell code through a terminal used for sudo prompts.
ssh "$host" 'mkdir -p ~/.local/lib/concurro-deploy'
scp "$script_dir/host.sh" "$host:.local/lib/concurro-deploy/host.sh"
ssh -t "$host" "bash ~/.local/lib/concurro-deploy/host.sh $component $release"
echo "Deployment verified: $component $release. No pilot retry was posted."
