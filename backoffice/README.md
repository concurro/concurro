# Backoffice API

The API of the [backoffice web app](https://github.com/concurro/concurro-backoffice), at
`https://<host>/concurro/api` and `/concurro/auth`; the edge sends the rest of `/concurro/` to
the web app's own container (see `gateway/concurro.caddy`). Responses follow the frontend's domain types.

- **Sign-in:** web flow on the **Concurro Sign-in OAuth App**, requesting no scopes, so users only
  verify their identity. One-use `state` and PKCE are bound to the starting browser by an HTTP-only
  cookie. Sessions are in memory: a restart signs everyone out. The user's token stays server-side;
  it identifies them to the service and the vault, and decides which targets they see: a target
  is listed only when their token can see its upstream and fork (public repositories only, with
  no scopes).
- **Reads** use the read-only **Concurro Backoffice GitHub App**: `service.identity.Identity` mints its
  installation tokens per repository, resolving each organization's installation, with the
  service's read permissions. A target shows writer commands when the user has write access to its
  fork, the same check the service applies. Task state is the engine's own audit checkpoint
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
  "client_id": "<Concurro Sign-in OAuth App client ID>",
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

Two Apps, neither of them the service's Concurro App:

| App | Kind | Permissions | Setup |
| ---- | ---- | ---- | ---- |
| Concurro Sign-in | OAuth App | No scopes | Callback URL `https://<host>/concurro/auth/callback`, expiring user tokens. Its client ID is `client_id`; its client secret goes in `client_secret_path` |
| Concurro Backoffice | GitHub App | Read-only: Contents, Issues, Pull requests, Checks, Commit statuses, Actions | No webhook or callback. Installed on only the selected target repositories (upstream and fork). Its App ID is `reader.app_id`; its private key goes in `reader.private_key_path` (mode 600, uid 65533) |

For an existing deployment, follow [App migration](APP-MIGRATION.md) to prepare credentials
without changing the running configuration, then switch settings during deployment.

## Run locally

```bash
python3 -m pip install -r service/requirements.txt -r backoffice/requirements.txt
python3 -m backoffice --settings /path/to/settings.json --listen 127.0.0.1:8788
```
