# Proposal: backoffice API surface

**Status: proposal (input for a design spec), not implemented.** Inventory of the HTTP
endpoints a web backoffice needs, mapped to where the data lives today and what is
missing. Credential management builds on [per-user credentials](per-user-credentials.md).

## Use cases

1. Sign in with GitHub.
2. Manage your own provider credentials, bound to your stable GitHub user ID, so sponsored
   tasks are paid by the sponsor.
3. Helicopter view of targets → issues/PRs/Actions, joined with Concurro task state, scoped
   to what the signed-in user can see.
4. Deep-dive into a task's runs: prompts, transcripts, raw responses, validation and runner
   diagnostics.
5. Act on tasks (the `/concurro` commands) without hand-typing comments.
6. Operator views that today exist only on the control socket or CLI.

## Data sources today

| Source | Holds | Access notes |
| ---- | ---- | ---- |
| GitHub REST/GraphQL | Repos, issues, PRs, reviews/threads, checks, Actions runs | User-to-server token; GitHub enforces visibility |
| Issue artifacts (`issues/{n}/artifacts`) | Audit checkpoint (`concurro:audit:v1`), plan versions, immutable report-data | Decoding/hydration is engine logic (`Checkpoints.load`, `report_store`); reuse, don't reimplement |
| Service registry (SQLite, daemon-locked) | Bindings, queue/due times, ownership leases, last outcome, mutation intents, PR mapping | Exclusive writer lock; reads must go through the daemon |
| Exchange DB | Runner sessions, turns (`payload` = full request, `result`, timings, binding, staffing) | **Finished turns are deleted after 24 h** (`FINISHED_RETENTION_SECONDS`) |
| Retained transcripts | `state_dir/sessions/<repo>/<scope>.tar.gz` (Claude Code / Codex homes) | **One archive per role scope, overwritten each turn**; no per-turn history |
| Bundle at `bundle_id` | Role instructions, skills, response schemas | Needed to reconstruct the exact prompt (`agent_entry.prompt_for`) |
| Target config at `config_sha` | `.concurro/config.yaml`, charter, instructions | Fork default branch or pinned commit |
| agentgateway | Per-session token budgets; stats listener (loopback) | Concurro records **no per-turn token usage** today |
| `state_dir/evaluations/` | Design-evaluation reports | Operator-only |

## Decisions

- **Frontend repository:** the web app lives in
  [`concurro/concurro-backoffice`](https://github.com/concurro/concurro-backoffice). The
  backend endpoints below live with the services that own the data.
- **Diagnostics visibility:** run diagnostics (prompts, transcripts, raw responses,
  workspace diffs, runner details) are visible at the Reader tier for now.
- **Diagnostics retention:** per-turn diagnostics are persisted in a database rather than
  relying on the exchange's 24-hour turn retention or the overwritten transcript archives.
- **Backend:** a separate Python service (`backoffice/` in this repository) using pinned
  third-party packages (FastAPI, pydantic, httpx, uvicorn). It imports the engine's
  decoders instead of reimplementing them.
- **Diagnostics store:** a SQLite file in its own directory, written by the service daemon
  and read by the backoffice. Transcript archives are stored as files named by their
  SHA-256, outside the database. Local disk, no pruning for now.
- **Vault:** a separate service in the gateway trust zone. The backoffice forwards the
  user's GitHub token, and the vault resolves the user itself (`GET /user`) rather than
  trusting an identity asserted by the backoffice. A compromised backoffice can then act
  only for users with live sessions, and can never read a stored credential.
- **API types:** the backoffice's pydantic models are the source of truth; the frontend
  generates its TypeScript types from the OpenAPI document (`openapi-typescript`).
- **Host:** committed material names the deployment host only as `<host>`.

## Phase 1 (demo) scope

- **In:** GitHub sign-in; overview, target and task views from GitHub and issue artifacts;
  run diagnostics captured from deployment onwards; commands posted as the user's issue
  comments; storing, validating and listing your own credentials.
- **Out:** sponsorship, live updates (the frontend polls), operator endpoints,
  preferences, and injecting stored credentials into the gateway (credentials proposal
  phase 0).

## Architecture assumptions

- **One public API (BFF)** under the edge, e.g. `/concurro/api/*`, holding the user's
  GitHub token server-side in an HTTP-only session. All writes need CSRF protection.
- **Credential endpoints are served from the gateway trust zone** (vault per the
  [proposal](per-user-credentials.md#5-vault)); the BFF forwards them but never stores
  or reads back secrets.
- **Task endpoints read GitHub with the user's token,** so GitHub enforces visibility;
  diagnostics endpoints read the diagnostics store. Registry state (queue, leases, write
  intents) needs a daemon read API, deferred with the operator endpoints.
- **Commands are delivered to the service, never posted with the user's token.** The
  service verifies the writer and posts a signed, App-authored command comment, which the
  controller applies as the writer's own command (`engine/delegation.py`); the engine's
  `permitted` check, audit and replay protection apply unchanged.
- **Visibility caveat:** a GitHub App user-to-server token sees only repositories where the
  App is installed *and* the user has access. That matches Concurro's scope (configured
  targets); showing unrelated repos would need a classic OAuth App and is not proposed.

## Authorization tiers

| Tier | Rule | Covers |
| ---- | ---- | ---- |
| Self | Signed-in user ID | Credentials, own usage, sponsorships, preferences |
| Reader | Write, maintain or admin access to the target upstream; both repositories visible | Task summary, plan, reviews, reports, readiness, run diagnostics, raw audit |
| Writer | Same check as `controller.permitted` | Commands, write intents |
| Operator | Deployment-configured user IDs | Registry status, write resolution, exchange/gateway health, evaluations |

Diagnostics follow the upstream-writer gate, including direct task and turn URLs. Public
repository visibility alone does not grant backoffice access. Transcripts must never contain
session keys or provider credentials.

## Endpoints

Paths are relative to `/concurro/api`. A task is addressed as `{owner}/{repo}/{issue}`
(the issue repository, matching the registry key). Lists are cursor-paginated.

### 1. Auth and session

| Method | Path | Purpose |
| ---- | ---- | ---- |
| GET | `/auth/github/login` | Start App user-to-server flow; set short-lived `SameSite=Lax` transaction cookie (state + PKCE) |
| GET | `/auth/github/callback` | Verify state against that browser's transaction; exchange code; `/user` lookup for stable ID |
| POST | `/auth/logout` | Revoke session (and the user token) |
| GET | `/me` | User ID, login, avatar, CSRF token, per-target role (reader/writer/operator) |

Token refresh (8 h expiry, rotating refresh token) stays server-side.

### 2. Credentials and sponsorship (self)

| Method | Path | Purpose |
| ---- | ---- | ---- |
| GET | `/me/credentials/routes` | Supported routes (Anthropic API key, Claude subscription, OpenAI API key, ChatGPT subscription) and the model patterns each covers |
| GET | `/me/credentials` | Per route: version, fingerprint, status (`valid`/`expired`/`exhausted`/`revoked`), validated/last-used times |
| PUT | `/me/credentials/{route}` | Add or replace (write-only); validated with a cheap provider call before acceptance; returns version + fingerprint |
| POST | `/me/credentials/{route}/validate` | Re-validate an existing credential |
| DELETE | `/me/credentials/{route}` | Revoke; retires sessions per the [vault rules](per-user-credentials.md#5-vault) |
| POST | `/me/credentials/{route}/login` | Subscription login flow (proposal phase 5) |
| GET | `/me/coverage?target=` | Which roles of each target's roster your credentials cover; what's missing |
| GET | `/me/sponsorships` | Tasks you sponsor, epoch, state (highlights `awaiting_credentials`) |
| GET | `/me/usage?from&to&group_by=task\|route\|model` | Calls and tokens charged to your account |

### 3. Overview and targets

| Method | Path | Purpose |
| ---- | ---- | ---- |
| GET | `/overview` | Counts across visible targets: active, awaiting human, blocked, ready for merge; "needs you" list (plan approvals, input needed, credential pauses, failed CI on your tasks) |
| GET | `/targets` | Configured upstream/fork pairs visible to the user, `issues` mode, your role, task counts |
| GET | `/targets/{owner}/{repo}` | Target detail: fork/upstream, roster (profiles → roles), limits, edit policy, charter, bundle ID, executor workflow presence |
| GET | `/targets/{owner}/{repo}/config?sha=` | Parsed config + charter at a commit; `?compare=` diff against a task's pin (drift → `resume` hint) |

### 4. Repository browsing (GitHub-backed, joined with tasks)

| Method | Path | Purpose |
| ---- | ---- | ---- |
| GET | `/repos/{owner}/{repo}/issues?state&label&enrolled&q` | Issues with task stage/sponsor badge where enrolled |
| GET | `/repos/{owner}/{repo}/pulls?state&q` | PRs with linked task, CI rollup, agent approval count, open findings |
| GET | `/repos/{owner}/{repo}/pulls/{n}` | PR detail: checks/statuses, reviews and threads (finding markers resolved to finding IDs), linked task |
| GET | `/repos/{owner}/{repo}/actions/runs?kind=executor\|ci` | Workflow runs; executor runs mapped to session → task/role via `concurro/runs/<session>` refs |
| GET | `/repos/{owner}/{repo}/actions/runs/{id}` | Run + jobs; logs linked to GitHub (no proxying initially) |

Use GraphQL for list views and ETags for detail views; user tokens have their own rate limit.

### 5. Tasks (reader)

| Method | Path | Purpose |
| ---- | ---- | ---- |
| GET | `/tasks?target&stage&sponsor=me&needs=me` | Task list |
| GET | `/tasks/{t}` | Stage, mode, sponsor/enrollment, binding (upstream, fork, branch, `config_sha`), PR/head, plan ID, current activity (dashboard alert), scheduler state (due time, last outcome, retry-after, lease), pending human actions |
| GET | `/tasks/{t}/timeline` | Ordered events: enrollment, commands + actors, stage changes, model calls, publications, reviews, CI, blockers, invalidations/resumes |
| GET | `/tasks/{t}/display-runs` | Invalidation boundaries (`dashboard_runs.boundaries`) |
| GET | `/tasks/{t}/assessment` | Intake decision, concerns, complexity/risk/confidence |
| GET | `/tasks/{t}/plan` | Current plan, approval binding; `/plan/versions` and `/plan/versions/{v}`, `?diff=` between versions |
| GET | `/tasks/{t}/design-reviews` | Generations → reviewers → verdict, objections and status |
| GET | `/tasks/{t}/pr-reviews` | Rounds by head → reviewer votes; findings with status and thread links |
| GET | `/tasks/{t}/findings/{id}` | Finding history: raised, dispositions, verification, withdrawal |
| GET | `/tasks/{t}/reports/{artifact}` | Canonical report JSON + rendered view |
| GET | `/tasks/{t}/readiness` | CI rollup, approvals vs. current head, open findings, pending publication, blocking reasons |
| GET | `/tasks/{t}/budget` | Model calls used/max, fix rounds, plan revisions, design reopens, per-credential ledger |

### 6. Task actions (writer, posted by the service as a signed delegated command)

| Method | Path | Maps to |
| ---- | ---- | ---- |
| POST | `/repos/{owner}/{repo}/issues/{n}/enroll` | Apply the `concurro` label |
| POST | `/tasks/{t}/commands/start` | `/concurro start` |
| POST | `/tasks/{t}/commands/approve` | `/concurro approve <plan-id>` (body: plan ID shown to the user) |
| POST | `/tasks/{t}/commands/retry` | Optional feedback comment (≤ 4,000 bytes) then `/concurro retry [--plan-revisions N]` |
| POST | `/tasks/{t}/commands/resume` | `/concurro resume` |
| POST | `/tasks/{t}/commands/stop` | `/concurro stop` |
| POST | `/tasks/{t}/commands/invalidate` | `/concurro invalidate` (UI warns about reassessment) |
| POST | `/tasks/{t}/commands/sponsor` | `/concurro sponsor` (proposal; not implemented) |
| POST | `/tasks/{t}/answers` | Reply to an Input-needed request |

Each returns the comment ID; the outcome is observed through the task endpoints/SSE.
Pre-validation (e.g. config drift, revision limit) is advisory; the engine decides.

### 7. Run diagnostics (reader)

| Method | Path | Purpose |
| ---- | ---- | ---- |
| GET | `/tasks/{t}/turns?stage&role&outcome` | Every model call: call number, stage, role/profile, provider/model/effort, session scope + turn index, attempt, `TurnOutcome`, queued/started/finished, runner run, request/response digests, credential (route, sponsor), tokens |
| GET | `/tasks/{t}/turns/{turn}` | Turn detail |
| GET | `/tasks/{t}/turns/{turn}/prompt` | Reconstructed prompt: role instructions, repository snapshot, response schema, untrusted context, feedback index, implementation authorization |
| GET | `/tasks/{t}/turns/{turn}/feedback` | Feedback JSON files delivered to that turn |
| GET | `/tasks/{t}/turns/{turn}/response` | Raw result, validation outcome, correction reasons |
| GET | `/tasks/{t}/turns/{turn}/transcript?format=normalized\|raw` | Messages/tool calls/tool results normalized across Claude Code and Codex; `raw` streams the archive |
| GET | `/tasks/{t}/turns/{turn}/workspace` | Extracted implementation changes as a diff |
| GET | `/tasks/{t}/turns/{turn}/runner` | Actions run/jobs and `runner_termination` diagnosis |
| GET | `/tasks/{t}/sessions` | Role conversations: scope, binding, turns, restarts |
| GET | `/tasks/{t}/validation` | Output-validation and correction history (dashboard Diagnostics) |
| GET | `/tasks/{t}/audit` | Decoded, hydrated checkpoint (power users) |
| GET | `/tasks/{t}/writes` | Mutation intents: status, settled, resolution |
| GET | `/bundles/{bundle_id}` | Roles, skills and schemas for a bundle identity |

### 8. Live updates

| Method | Path | Purpose |
| ---- | ---- | ---- |
| GET | `/events?task=&target=` | SSE: task updated, turn queued/started/finished, CI/readiness changed, credential status changed |

Fed by the daemon (tick outcomes, ingress deliveries) and exchange turn transitions.

### 9. Preferences (self)

| Method | Path | Purpose |
| ---- | ---- | ---- |
| GET/PUT | `/me/preferences` | Pinned targets/tasks, default filters, notification opt-ins (e.g. "my sponsored task paused") |

### 10. Operator

| Method | Path | Maps to |
| ---- | ---- | ---- |
| GET | `/admin/status` | Control `status` |
| POST | `/admin/reconcile` | Control `reconcile-once` |
| POST | `/admin/writes/{intent}/resolve` | Control `resolve` (disposition, evidence, operator = signed-in user) |
| POST | `/admin/reviews/{intent}/recover` | `recover-review` |
| GET | `/admin/deployment` | Served engine SHA and launcher workflow ref; bundle drift per task |
| GET | `/admin/exchange/sessions`, `/admin/exchange/turns` | Live sessions/turns, staffing, refusals |
| POST | `/admin/exchange/sessions/{id}/end` | Exchange operator `end` |
| GET | `/admin/gateway` | Health, active keys, budget consumption |
| POST/GET | `/admin/evaluations`, `/admin/evaluations/{id}` | `evaluate` and its reports |
| GET/PUT | `/admin/targets/{owner}/{repo}/org-pool` | Org-pool opt-in ([open question 4](per-user-credentials.md#open-questions)) |

## Backend prerequisites (not just endpoints)

1. **Diagnostics database.** Exchange turns vanish after 24 h and transcripts are
   overwritten per scope, so section 7 only works for recent/latest turns today. Persist
   per call: request payload, `bundle_id`, result, validation outcome, transcript snapshot,
   timings, runner identity and usage, keyed by task + call number. Transcript archives
   are up to 24 MiB each, so the schema should keep them out of hot rows (separate table
   or blob storage).
2. **Per-turn token usage.** Attribute agentgateway usage to the session key (and thus the
   turn/sponsor); spike alongside proposal phase 0.
3. **Daemon read API.** Internal HTTP (or extended control socket) exposing registry state
   and engine-decoded task projections, reusing `Checkpoints`, `report_store`, `dashboard`
   view models and the shared report cache.
4. **Turn → task index.** Query exchange turns by binding (repository, issue, role).
5. **Stable user IDs** in enrollment and command records (today: logins only).
6. **Portal + vault + sponsorship** per [per-user credentials](per-user-credentials.md)
   phases 1–3.
7. **GitHub App changes:** callback URL, client secret, user-token expiry/refresh; Issues
   write is already granted for issue repositories (needed for comment-posted commands).

## Suggested phasing

1. Auth, `/me`, targets/overview, repo browsing, task read endpoints (sections 1, 3–5).
2. Comment-posted commands and SSE (sections 6, 8).
3. Diagnostics store, then section 7.
4. Credentials and sponsorship (section 2), following the credentials proposal phasing.
5. Operator endpoints (section 10).

## Open questions

1. Should diagnostics later be restricted again (e.g. writer-only), once private
   repositories are supported?
2. Operator identity: a settings allow-list of GitHub user IDs, or an org/team membership?
3. Proxy Actions logs, or link to GitHub only?
