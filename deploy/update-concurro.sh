#!/usr/bin/env bash
# Update all Concurro images from an already-open terminal on the deployment host.
set -euo pipefail
lib="$HOME/.local/lib/concurro-deploy"
trap 'echo "Update stopped; inspect the error above. No pilot retry was posted." >&2' ERR
releases=$(python3 "$lib/releases.py" "$@")
read -r service_release web_release <<< "$releases"
bash "$lib/host.sh" all "$service_release" "$web_release"
echo "Deployment verified: service $service_release; web $web_release. No pilot retry was posted."
