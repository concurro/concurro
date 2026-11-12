# Backoffice API

Serves the [backoffice web app](https://github.com/concurro/concurro-backoffice) and its API at
`https://<host>/concurro/` (everything under `/concurro/` that isn't `webhook`, `exchange` or `v1`;
see `gateway/concurro.caddy`). Responses follow the frontend's domain types.

- **Sign-in:** GitHub App user-to-server web flow on the existing App, with one-use `state` and PKCE
  bound to the starting browser by an HTTP-only cookie. Sessions are in memory: a restart signs
  everyone out. The user's token stays server-side.
- **Reads** use the signed-in user's token, so GitHub decides what they see. Task state is the
  engine's own audit checkpoint (`Checkpoints.load`); lists use the `concurro` opt-in and stage labels.
  PR lists load line counts and head-commit check runs in one GraphQL query. If that fails,
  a REST listing supplies the PRs with unknown (`null`) line counts and no CI information.
- **Diagnostics** come from the service's `diagnostics_dir` (`service/diagnostics.py`), read-only.
- **Commands** go to the service (`POST <service_url>/actions` with the user's sign-in token),
  which verifies the writer and posts the command as Concurro's own signed comment. The
  backoffice never writes to GitHub.
- **Credentials** are forwarded to the vault with the user's token; the vault resolves the user itself.
- Writes need the `X-Concurro-Request: 1` header.

| Method | Path | Returns |
| ---- | ---- | ---- |
| GET | `/auth/login?next=/path` | Redirect to GitHub |
| GET | `/auth/callback` | Session cookie, redirect to `next` |
| POST | `/auth/logout` | Ends the session |
| GET | `/api/me` | `{id, login, name, avatarUrl}` |
| GET | `/api/overview` | `{targets, tasks, activity}` |
| GET | `/api/targets/{owner}/{repo}` | `{target, tasks, runs}` |
| GET | `/api/tasks/{owner}/{repo}/{issue}` | `{task, target}` |
| GET | `/api/tasks/{owner}/{repo}/{issue}/turns` | `{task, turns}` |
| POST | `/api/tasks/{owner}/{repo}/{issue}/commands` | `{command, argument?}` → `{posted, actor, url}` |
| GET | `/api/me/credentials` | `{routes, usage}` |
| PUT, DELETE | `/api/me/credentials/{route}` | One route |
| GET | `/api/openapi.json` | OpenAPI document |

## Settings

`/etc/concurro/backoffice/settings.json` (directory 0700, uid 65533):

```json
{
  "public_url": "https://<host>/concurro",
  "client_id": "<GitHub App client ID>",
  "client_secret_path": "/etc/concurro/backoffice/client-secret",
  "targets": [{"upstream": "owner/repo", "fork": "owner/fork", "issues": "fork"}],
  "diagnostics_dir": "/var/lib/concurro/diagnostics",
  "vault_url": "http://concurro-vault:8090",
  "static_dir": "/srv/concurro-backoffice",
  "service_url": "http://concurro-service:8787"
}
```

`targets` matches the service's settings. `static_dir` holds the web app's static build (built
with base path `/concurro`); without it, only the API is served. The App's callback URL must be
`https://<host>/concurro/auth/callback`.

## Run locally

```bash
python3 -m pip install -r service/requirements.txt -r backoffice/requirements.txt
python3 -m backoffice --settings /path/to/settings.json --listen 127.0.0.1:8788
```
