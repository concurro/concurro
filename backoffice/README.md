# Backoffice API

The API of the [backoffice web app](https://github.com/concurro/concurro-backoffice), at
`https://<host>/concurro/api` and `/concurro/auth`; the edge sends the rest of `/concurro/` to
the web app's own container (see `gateway/concurro.caddy`). Responses follow the frontend's domain types.

- **Sign-in:** web flow on the **Concurro Sign-in OAuth App**, requesting no scopes, so users only
  verify their identity. One-use `state` and PKCE are bound to the starting browser by an HTTP-only
  cookie. Sessions are in memory: a restart signs everyone out. The user's token stays server-side;
  it identifies them to the service and the vault. Targets are available only to users with
  write, maintain or admin permission on the upstream repository. Their token must also see
  both upstream and fork (public repositories only, with no scopes). Fork write access is not required.
- **Reads** use the read-only **Concurro Backoffice GitHub App**: `service.identity.Identity` mints its
  installation tokens per repository, resolving each organization's installation, with the
  service's read permissions. Upstream writer authorization precedes configuration, issue,
  snapshot and diagnostic reads, including direct API requests. Writer commands use the same
  upstream authority in the service and engine. Task state is the engine's own audit checkpoint
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
| GET | `/api/tasks/{owner}/{repo}/{issue}/sections/{section}` | Summary, selected phase, timeline or live PR data |
| GET | `/api/tasks/{owner}/{repo}/{issue}/turns` | `{task, turns}` |
| GET | `/api/tasks/{owner}/{repo}/{issue}/turns/{turn-id}/runs` | `{runs, message}` with Actions run attempts and jobs |
| POST | `/api/tasks/{owner}/{repo}/{issue}/commands` | `{command, argument?}` → `{posted, actor, url}` |
| GET | `/api/me/credentials` | `{routes, usage}` |
| PUT, DELETE | `/api/me/credentials/{route}` | One route |
| GET | `/api/openapi.json` | OpenAPI document |

## Issue-page loading

The issue page loads `sections/summary` first. It contains stage, budgets, alerts, current
plan identity, phase-presence flags and checkpoint-backed PR identity. It does not restore
historical reports, plan bodies or file inventories. After the summary, the selected phase,
`timeline` and `pull` load independently. Phase names are `intake`, `plan`, `design`,
`implement`, `review` and `handoff`; the existing full-task endpoint remains available.

The API keeps a rebuildable SQLite read cache in `read_cache_dir`, or the
`CONCURRO_READ_CACHE_DIR` environment variable when the setting is omitted. The image enables
it at `/var/lib/concurro/backoffice`; Compose persists that directory in `backoffice-data`.
It is separate from the service registry and diagnostics. No credentials or user permissions
are stored in it. An explicit null or empty directory disables the cache. Without a configured directory, reads
retain their direct-GitHub behavior. Database startup or read failures also fall back to direct reads.

The first section request fetches issue metadata and checkpoint comments and verifies a
lightweight checkpoint through the engine reader. Only owned checkpoint comments are retained,
not the whole discussion. The issue, comments and summary state are published together in one
SQLite update, scoped to the Reader App and effective target configuration. Summary, timeline,
PR and phase requests share these inputs instead of scanning the issue and comments separately.

Snapshots are fresh for 60 seconds. Stale snapshots return immediately while one background
refresh runs per issue/configuration in the API process. Cold concurrent requests wait for the
same refresh. Failures retain the last verified snapshot with an error and observation time;
confirmed missing issues discard it. Retry deadlines survive restarts and honor GitHub's
requested delay (at least 60 seconds). Checkpoint verification failures retain their diagnostic
codes and use a short two-second retry interval. Unexpected refresh failures wait 60 seconds
before another attempt. Successful commands, and deliveries whose
outcome is uncertain, invalidate every cached configuration for that issue. Saved data and
GitHub retry deadlines survive invalidation; older in-flight refreshes cannot overwrite it.
After a command, unchanged checkpoint state stays on a five-second refresh interval for up to
60 seconds while the browser waits for the change. A successful read retires obsolete cached
configurations for that issue; closed issues remain available as historical display data.

The browser checks for completion while a background refresh is active and shows saved-data
status on refresh failure. Repository visibility remains checked per user through the existing
short-lived cache. Upstream writer decisions, including denials, are cached for 60 seconds
across sessions of the same verified user and Reader App. The service still authorizes every
command with a fresh permission check. Commands read live checkpoint state where needed.

Phase readers still verify and hydrate their selected evidence using the engine reader. A phase
whose cached pointer no longer matches a mutable audit refreshes the shared snapshot before
retrying. Full task/diagnostics loads and live PR/check reads remain direct GitHub reads. The
controller's authoritative storage and verification are unchanged. Responses carry a revision
derived from the checkpoint comment, verified audit reference and issue update time; live CI
remains `unknown` until read.

This iteration fills the cache on access, not through service checkpoint hooks or webhooks.
Inactive issues need no background polling. Deploy one API process for shared refresh coalescing;
the database is persistent display data, not a replacement for the service registry.

The web app reuses task and section responses for navigation, refreshes entries older than
ten seconds in the background, and clears them after commands, sign-out or account changes.
Section failures can be retried without hiding the rest of the page. Deploy the API before the
web enhancement to enable background-refresh status. Existing settings need no edits with the
image and Compose defaults; custom mounts must make `read_cache_dir` writable by uid 65533.

## Run diagnostics

Run diagnostics load Actions jobs on demand using the Reader App. New turn observations retain
the Exchange's assigned runner session and OIDC-bound run/attempt. Older records can resolve
dispatched sessions by their unique run branch; these are labeled as associated dispatches,
not confirmed assignments. Missing/deleted history and failed API reads remain unavailable.
Job durations cover the entire session, which can execute several turns.

Token counts are `null` until per-turn usage is available; neither zero counts nor totals are
inferred from retained conversation archives. Provider sponsorship's durable usage projection
is the integration point for metering. Diagnostics are not the accounting authority.

The service automatically adds the nullable `runner` column to existing diagnostics databases.
Deploy the web release first so it accepts nullable counts, then the service/Exchange/backoffice
API release. The Runner tab is unavailable until the new `/runs` endpoint is deployed. No App
permission changes are needed (Actions read is already required).

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
