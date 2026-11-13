# Backoffice API

The API of the [backoffice web app](https://github.com/concurro/concurro-backoffice), at
`https://<host>/concurro/api` and `/concurro/auth`; the edge sends the rest of `/concurro/` to
the web app's own container (see `gateway/concurro.caddy`). Responses follow the frontend's domain types.

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
| POST | `/auth/logout` | `{signedOut, remote}`; checked write header, local-first logout |
| GET | `/auth/logout-status` | Anonymous remote cleanup status |
| GET | `/api/me` | `{id, login, name, avatarUrl}` |
| GET | `/api/overview` | `{targets, tasks, issues, activity}` |
| GET | `/api/targets/{owner}/{repo}` | `{target, tasks, issues, runs}` |
| GET | `/api/tasks/{owner}/{repo}/{issue}` | `{task, target, issueWriter}` |
| GET | `/api/tasks/{owner}/{repo}/{issue}/turns` | `{task, turns}` |
| POST | `/api/tasks/{owner}/{repo}/{issue}/commands` | `{command, argument?}` → `{posted, actor, url}` |
| GET | `/api/repos/{owner}/{repo}/issues/{issue}` | `{issue, target, issueWriter}` without requiring a checkpoint |
| GET | `/api/me/sponsorships` | Visible current/historical provider consent for your stable actor ID |
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
  "service_url": "http://concurro-service:8787"
}
```

`targets` matches the service's settings. An optional `static_dir` also serves a static build of
the web app (built with base path `/concurro`) from this process, for running without the edge. The App's callback URL must be
`https://<host>/concurro/auth/callback`.

## Run locally

```bash
python3 -m pip install -r service/requirements.txt -r backoffice/requirements.txt
python3 -m backoffice --settings /path/to/settings.json --listen 127.0.0.1:8788
```

## Sponsorship and credentials API

Issue-addressed sponsorship reads and commands work before a checkpoint exists:
`GET /api/repos/{owner}/{repo}/issues/{issue}/sponsorships` and
`POST /api/repos/{owner}/{repo}/issues/{issue}/commands`. Commands use the existing CSRF
header and a `command` (`sponsor` or `withdraw`) with an `argument` (`openai` or `anthropic`).
Explicit sponsorship authorizes contributed calls when the target enables that funding mode.
Org-funded targets keep org credentials and prospective sponsor attribution.
Target/overview responses include known unenrolled intents in `issues`, without task
stages/budgets. The companion frontend supplies provider controls and pre-task views.
See [phase 2](../docs/proposals/per-user-credentials/phase-2.md) for lifecycle behavior, optional
encrypted logout retry settings, migration and roll-forward recovery. GitHub refresh pairs remain server-only;
unknown expiry is not invented. Both subscription routes now accept local CLI token paste
with an explicit model set and ChatGPT account ID through the existing authenticated PUT.
The portal derives default models from target requirements and existing validated coverage;
all selected models validate before replacement. See [phase 5](../docs/proposals/per-user-credentials/phase-5.md).


## Credential recovery views

[Phase 6](../docs/proposals/per-user-credentials/phase-6.md) exposes current recovery eligibility,
recorded retry deadlines and historical charged-attempt owners/routes/versions in the existing
issue/task sponsorship projection. Coverage includes exact selected models and current versions.
The portal displays these receipts; it never authorizes a retry locally. Eligible auth/quota
pauses require newer validated material; rate pauses require a trusted retry hint. Writer-only,
unknown, stopped and budget pauses do not automatically resume. Token observations remain
supplementary to the task's charged-call ledger.
