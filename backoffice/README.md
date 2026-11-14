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
| POST | `/auth/logout` | `{signedOut, remote}`; checked write header, local-first logout |
| GET | `/auth/logout-status` | Anonymous remote cleanup status |
| GET | `/api/me` | `{id, login, name, avatarUrl}` |
| GET | `/api/overview` | `{targets, tasks, issues, activity, cache?}` |
| GET | `/api/targets/{owner}/{repo}` | `{target, tasks, issues, cache?}` |
| GET | `/api/targets/{owner}/{repo}/runs` | `{runs, message, cache?}` with the fork's recent executor runs |
| POST | `/api/targets/{owner}/{repo}/refresh` | `{refreshed}`; writers only; every list of the target is read from GitHub again |
| GET | `/api/tasks/{owner}/{repo}/{issue}` | `{task, target, issueWriter}` |
| GET | `/api/tasks/{owner}/{repo}/{issue}/sections/{section}` | Summary, selected phase, timeline or live PR data |
| GET | `/api/tasks/{owner}/{repo}/{issue}/turns` | `{task, turns}` |
| GET | `/api/tasks/{owner}/{repo}/{issue}/turns/{turn-id}/runs` | `{runs, message}` with Actions run attempts and jobs |
| POST | `/api/tasks/{owner}/{repo}/{issue}/commands` | `{command, argument?}` → `{posted, actor, url}` |
| GET | `/api/repos/{owner}/{repo}/issues/{issue}` | `{issue, target, issueWriter}` without requiring a checkpoint |
| GET | `/api/me/sponsorships` | Visible current/historical provider consent for your stable actor ID |
| GET | `/api/me/credentials` | `{routes, usage}` |
| PUT, DELETE | `/api/me/credentials/{route}` | One route |
| GET | `/api/openapi.json` | OpenAPI document |

## List loading

The overview and target views read each target's pull requests, enrolled issues and
sponsorships concurrently. With the read cache enabled, each of those lists and a fork's
Actions runs is saved per repository (sponsorships per repository and user). A request
returns saved lists at once and refreshes the ones older than a minute in the background;
`cache` reports the oldest list's `observedAt` and whether a refresh is `refreshing`, so the
frontend can reload. A posted command outdates the saved lists of its issue repository.
A read that comes back incomplete (sponsorships the service could not supply, an Actions
listing or rerun that could not be read, pull requests from the REST fallback without checks)
never replaces a complete saved list: the saved one is kept, flagged, and the read retried
after a minute. With nothing saved, the incomplete result is shown flagged and retried.
Visibility and writer access are decided per request from their own one-minute caches, never from
saved lists. A target lists its `issueRepositories`, which is where its tasks' `owner`/`repo` come from.

### With the list index

Setting `lists_token_path` (the bearer shared with the service's `lists_token_path`) replaces
the one-minute rule. Each request asks the service once for the revisions of its repositories'
lists (`service/list_index.py`):

- A saved list is read again only when its kind's revision moved or it was outdated here: by a
  posted command, or, for sponsorship lists, by a credential write, since credential coverage
  comes from the vault. Age alone never causes a read.
- `cache` gains `state` (`current`, `catching-up` or `unavailable`) and `verified`, the time
  the service last checked its lists against GitHub's delivery history.
- While the service gives no usable answer, saved lists are served with that reason and are
  not refreshed; a list with nothing saved answers 503. No such failure is reported as 401.
- With the read cache disabled or failing, lists are read from GitHub directly while the
  service answers, and answer 503 while it does not.
- An entry the backoffice cannot use (malformed, or in a mode it does not serve yet) is
  treated as the service giving no answer for that list.
- Each GitHub list read is logged as `list read: <kind> of <repository>`.
- A credential that expires emits no signal, so the credential coverage inside a saved
  sponsorship list can be stale until another sponsorship change or a refresh. List views do
  not display coverage.

`POST /api/targets/{owner}/{repo}/refresh` asks the service to treat the target's lists as
changed. Without the list index it outdates the saved lists directly.

## Issue-page loading

The issue page loads `sections/summary` first. It contains stage, budgets, alerts, current
plan identity, phase-presence flags and checkpoint-backed PR identity. It does not restore
historical reports, plan bodies or file inventories. After the summary, the selected phase,
`timeline` and `pull` load independently. Phase names are `intake`, `plan`, `design`,
`implement`, `review` and `handoff`; the existing full-task endpoint remains available.

The API keeps a rebuildable SQLite read cache in `read_cache_dir`, or the
`CONCURRO_READ_CACHE_DIR` environment variable when the setting is omitted. The image enables
it at `/var/lib/concurro/backoffice`; Compose persists that directory in `backoffice-data`.
It is separate from the service registry and diagnostics. No secret credential material or
user permission decisions are stored. Verified checkpoint summaries may include nonsecret
sponsor slots, credential-ledger account/route/version identities and frozen admissions.
An explicit null or empty directory disables the cache. Without a configured directory, reads
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
Use `update-concurro.sh SERVICE_FULL_SHA WEB_FULL_SHA` for the combined release. If deploying
components individually, deploy the service/Exchange/backoffice API before the web release. The Runner tab is unavailable until the new `/runs` endpoint is deployed. No App
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
  "service_url": "http://concurro-service:8787",
  "lists_token_path": "/etc/concurro/backoffice/lists-token"
}
```

`targets` matches the service's settings. An optional `static_dir` also serves a static build of
the web app (built with base path `/concurro`) from this process, for running without the edge.

Two Apps, neither of them the service's Concurro App:

| App | Kind | Permissions | Setup |
| ---- | ---- | ---- | ---- |
| Concurro Sign-in | OAuth App | No scopes | Callback URL `https://<host>/concurro/auth/callback`, no repository scopes. Its client ID is `client_id`; its client secret goes in `client_secret_path` |
| Concurro Backoffice | GitHub App | Read-only: Contents, Issues, Pull requests, Checks, Commit statuses, Actions | No webhook or callback. Installed on only the selected target repositories (upstream and fork). Its App ID is `reader.app_id`; its private key goes in `reader.private_key_path` (mode 600, uid 65533) |

For an existing deployment, follow [App migration](APP-MIGRATION.md) to prepare credentials
without changing the running configuration, then switch settings during deployment.

## Run locally

```bash
python3 -m pip install -r service/requirements.txt -r backoffice/requirements.txt
python3 -m backoffice --settings /path/to/settings.json --listen 127.0.0.1:8788
```

## Own consent after writer loss

Full targets, task/checkpoint sections, runs and diagnostics require upstream write authority.
Dedicated issue-consent reads and own discovery remain available using genuine user issue
visibility. Nonwriters receive only their own current/historical grants, withdrawal receipts
and cutoffs; other sponsors, task state, coverage and accounting are omitted. Self-withdrawal
has no prior-grant prerequisite. New sponsorship and all other controls require upstream
write access, independent of fork permissions. The service rechecks this both when accepting
source commands and before new contributed admissions.

Repository reads use only the Reader App. Sign-in refresh/logout and personal vault requests
use only the user's Sign-in credential; Reader tokens never represent the user. Browser
sessions use returned expiry when supplied, with the existing local eight-hour default when
absent; refresh is attempted only with supplied usable refresh material.

## Sponsorship and credentials API

Issue-addressed sponsorship reads and commands work before a checkpoint exists:
`GET /api/repos/{owner}/{repo}/issues/{issue}/sponsorships` and
`POST /api/repos/{owner}/{repo}/issues/{issue}/commands`. Commands use the existing CSRF
header. `funding` takes `openai personal`, `openai org`, `anthropic personal` or `anthropic org`.
Personal selection grants the caller's consent and selects that provider's personal credentials;
organization selection preserves consent. Both require current upstream writer permission.
`sponsor` and `withdraw` take `openai` or `anthropic`: sponsor remains prospective consent;
withdrawal affects only the caller's consent and never changes payer selection.
Responses expose `provider_funding`, a `funding` summary (`org`, `contributed`, or `mixed`),
`source_provider_funding` from validated pending transitions, and immutable historical `charged_calls`.
Nonwriters receive only their own consent and withdrawal receipts, without payer/task data.
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

## Optional durable logout cleanup

Set `auth_state_dir` and `auth_key_path` together only when enabling encrypted cleanup
receipts/retry. Set the container `auth_state_dir` to `/var/lib/concurro/backoffice-auth`.
The overlay binds the existing host `/var/lib/concurro/backoffice` there, separately from
the named-volume issue cache at `/var/lib/concurro/backoffice`. Existing host auth files
remain in place; update an older container path in settings before the corrective release.
Provision the owner-only host state directory and separate 32-byte hex key for
uid 65533. Merge `service/compose.portal.yaml` into
`/etc/concurro/compose.service.host.yaml`; the release helper retains this optional mount
on both service and web updates. Without those settings/mounts, sessions remain in memory
and logout reports best-effort remote cleanup. See [current readiness](../docs/proposals/per-user-credentials/deployment-readiness.md).
