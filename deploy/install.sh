#!/usr/bin/env bash
# Install or explicitly update the host commands without deploying or changing settings.
set -euo pipefail
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
lib="$HOME/.local/lib/concurro-deploy"
mkdir -p "$lib" "$HOME/.local/bin"
exec 9>"$lib/lock"
flock -n 9 || { echo 'A deployment or helper installation is running.' >&2; exit 1; }
install -m 0644 "$source_dir/host.sh" "$source_dir/releases.py" "$lib/"
install -m 0755 "$source_dir/update-concurro.sh" "$HOME/.local/bin/update-concurro.sh"
echo "Installed $HOME/.local/bin/update-concurro.sh. No services changed."
