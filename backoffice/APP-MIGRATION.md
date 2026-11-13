# Separate sign-in and repository access

Use this once when migrating from the service App to the Apps in [Settings](README.md#settings).
The existing service App, its key and its installations stay in place. Private targets remain
hidden with no-scope OAuth sign-in; the current service supports public targets only.

## Prepare credentials

On your workstation, replace the PEM path and SSH host. This transfers the key without
printing it or placing its contents in shell history:

```bash
ssh <host> 'umask 077; cat > "$HOME/concurro-reader-app.pem"' < /absolute/path/to/backoffice.pem
```

In an interactive SSH terminal on that host:

```bash
sudo install -m 600 -o 65533 -g 65533 "$HOME/concurro-reader-app.pem" /etc/concurro/backoffice/reader-app.pem &&
  rm "$HOME/concurro-reader-app.pem"
```

Replace the two IDs below (neither is secret). Paste the OAuth client secret only at the
hidden prompt. This creates a new secret file and draft settings; it preserves the old
client secret and leaves `settings.json` unchanged.

```bash
sudo python3 - OAUTH_CLIENT_ID NUMERIC_BACKOFFICE_APP_ID <<'PY'
import getpass
import json
import os
import sys
from pathlib import Path

root = Path('/etc/concurro/backoffice')
client_id, app_id = sys.argv[1], int(sys.argv[2])
data = json.loads((root / 'settings.json').read_text())
secret = getpass.getpass('New OAuth client secret (hidden): ')
os.umask(0o077)
secret_path = root / 'client-secret.oauth'
secret_path.write_text(secret + '\n')
del secret
secret_path.chmod(0o600)
os.chown(secret_path, 65533, 65533)
data['client_id'] = client_id
data['client_secret_path'] = str(secret_path)
data['reader'] = {'app_id': app_id, 'private_key_path': str(root / 'reader-app.pem')}
draft = root / 'settings.next.json'
draft.write_text(json.dumps(data, indent=2) + '\n')
draft.chmod(0o600)
os.chown(draft, 65533, 65533)
print('Draft prepared; live settings are unchanged.')
PY
```

## Validate and switch

After the change is committed, merged and its main-branch CI has published images, replace
`FULL_COMMIT_SHA` with that exact release. On the host, validate the draft using the new image
and its normal uid, without networking or restarting anything:

```bash
sudo docker pull ghcr.io/concurro/backoffice-api:FULL_COMMIT_SHA &&
sudo docker run --rm --network none \
  --volume /etc/concurro/backoffice:/etc/concurro/backoffice:ro \
  --entrypoint python3 ghcr.io/concurro/backoffice-api:FULL_COMMIT_SHA -c \
  'from backoffice.settings import load; from service.identity import Identity; s = load("/etc/concurro/backoffice/settings.next.json"); Identity(s.reader).app_jwt(); print("Draft settings and signing key verified")'
```

During the deployment window, retain a copy of the old settings and atomically activate the draft:

```bash
sudo cp -p /etc/concurro/backoffice/settings.json /etc/concurro/backoffice/settings.before-app-split.json &&
sudo mv /etc/concurro/backoffice/settings.next.json /etc/concurro/backoffice/settings.json
```

Immediately run the [combined host release command](../gateway/README.md#releases)
with `SERVICE_FULL_SHA WEB_FULL_SHA` for the selected backend and portal commits. It deploys the backoffice API too. The old version rejects the new
`reader` field, so do not restart the old backoffice between switching settings and deploying.
Retain the old secret and settings backup as recovery evidence. If deployment fails, inspect
the failed phase and correct the settings or publish a corrective release moving forward.
Do not restart an old image against the new settings or restore an earlier deployment.
The new release requires the separate Reader settings.

## Verify

- Sign out and back in; verify that GitHub names Concurro Sign-in and requests no repository scopes.
  A private window does not reset an existing GitHub authorization; use a fresh account if needed.
- Check the overview, target configuration, task details, pull-request line counts and CI status.
- Check a viewer without write access: commands are hidden and a direct command POST is refused.
- With an authorized writer, exercise commands only on a disposable task; Resume can start work.
- After success, remove the old backoffice callback from the service App. Keep its webhook and key.
