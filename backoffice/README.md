# Backoffice API

The API of the [backoffice web app](https://github.com/concurro/concurro-backoffice), at
`https://<host>/concurro/api` and `/concurro/auth`; the edge sends the rest of `/concurro/` to
the web app's own container (see `gateway/concurro.caddy`). Responses follow the frontend's domain types.

- **Sign-in:** GitHub App user-to-server web flow on a **Sign-in App with no permissions**, so
  users only verify their identity. One-use `state` and PKCE are bound to the starting browser by an
  HTTP-only cookie. Sessions are in memory: a restart signs everyone out. The user's token stays
  server-side; it only identifies them, to the service and the vault.
- **Reads** use a **read-only Reader App**: `service.identity.Identity` mints its installation
  tokens per repository with the service's read permissions, so every signed-in user sees the
  configured (public) targets. A target shows writer commands when the user has write access to
  its fork, the same check the service applies. Task state is the engine's own audit checkpoint
  (`Checkpoints.load`); lists use the `concurro` opt-in and stage labels.
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
  "client_id": "<Sign-in App client ID>",
  "client_secret_path": "/etc/concurro/backoffice/client-secret",
  "reader": {"app_id": 123456, "private_key_path": "/etc/concurro/backoffice/reader-app.pem"},
  "targets": [{"upstream": "owner/repo", "fork": "owner/fork", "issues": "fork"}],
  "diagnostics_dir": "/var/lib/concurro/diagnostics",
  "vault_url": "http://concurro-vault:8090",
  "service_url": "http://concurro-service:8787"
}
```

`targets` matches the service's settings. An optional `static_dir` also serves a static build of
the web app (built with base path `/concurro`) from this process, for running without the edge.

The two GitHub Apps, neither of them the Concurro App:

| App | Permissions | Setup |
| ---- | ---- | ---- |
| Sign-in | None | Callback URL `https://<host>/concurro/auth/callback`; no webhook. Its client secret goes in `client_secret_path` |
| Reader | Read-only: Contents, Issues, Pull requests, Checks, Commit statuses, Actions | No webhook. Install it on only the selected target repositories (upstream and fork); its private key goes in `reader.private_key_path` (owner-only, uid 65533) |

## Run locally

```bash
python3 -m pip install -r service/requirements.txt -r backoffice/requirements.txt
python3 -m backoffice --settings /path/to/settings.json --listen 127.0.0.1:8788
```
