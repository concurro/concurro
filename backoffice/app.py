"""The backoffice HTTP API, behind the edge at `<public_url>/api` and `<public_url>/auth`.

Response shapes follow the frontend's domain types (`concurro-backoffice/src/lib/types.ts`).
"""

import logging
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

import private
from checkpoints import CheckpointError
from github import NotFound, Transient
from policy import Blocked
from service.identity import Identity
from service.settings import CHECKOUT

from . import review_tasks
from .auth import DEFAULT_TOKEN_SECONDS, AuthError, GitHubOAuth, Session, Sessions, TokenGrant, authorize_url
from .commands import CommandError, IndexUnavailable, ServiceCommands
from .credentials import STALE, Vault, VaultError
from .github import REPOSITORY_SECONDS, Access
from .live import Relay, stream
from .proofs import CommandSigner
from .read_cache import LISTING, ReadCache
from .revocations import RevocationError, Revocations
from .runs import Runs
from .settings import Settings
from .tasks import COMMANDS, TaskNotFound, Tasks, command_argument
from .turns import Diagnostics

log = logging.getLogger("concurro.backoffice")

SESSION_COOKIE = "concurro_session"
VIEWER_COOKIE = "concurro_viewer"
LOGIN_COOKIE = "concurro_login"
LOGOUT_COOKIE = "concurro_logout"
# Writes must carry this header; browsers can't send it cross-site without a CORS grant, which we never give.
CSRF_HEADER = "x-concurro-request"


class CommandBody(BaseModel):
    """One browser command; feedback is bound to its signed action proof."""

    command: str
    argument: str | None = None
    feedback: str | None = None


class SecretBody(BaseModel):
    """Write-only credential candidate and optional account header."""

    secret: str
    account_id: str | None = Field(default=None, description="Supplied ChatGPT account header for this token.")
    mutation_id: str


class ProfileFilter(BaseModel):
    """One target's profile key."""

    target: str
    profile: str


class EntryBody(SecretBody):
    """A new credential list entry; empty filters match all work."""

    type: str
    name: str
    roles: list[str] = []
    profiles: list[ProfileFilter] = []


class RelayBody(BaseModel):
    """One Sponsor device code approving its selected subscription routes."""

    code: str
    routes: list[str] = Field(min_length=1, max_length=2)
    name: str
    roles: list[str] = []
    profiles: list[ProfileFilter] = []
    mutation_id: str


class RelayCodeBody(BaseModel):
    """One displayed Sponsor code to look up before approval."""

    code: str


class RelayEntry(BaseModel):
    """One entry of a paired device at the revision last read."""

    id: str
    revision: int


class RelayDeviceBody(BaseModel):
    """New Sponsor code for the paired device's entries, one per registered route."""

    code: str
    entries: list[RelayEntry] = Field(min_length=1, max_length=2)
    mutation_id: str


class RelayReplaceBody(BaseModel):
    """New Sponsor code for one existing relay entry at the revision last read."""

    code: str
    revision: int
    mutation_id: str


class ReplaceBody(SecretBody):
    """A replacement token for one entry at the revision last read."""

    revision: int


class EntryEdit(BaseModel):
    """Name, enabled or filter changes to one entry at the revision last read."""

    revision: int
    mutation_id: str
    name: str | None = None
    enabled: bool | None = None
    roles: list[str] | None = None
    profiles: list[ProfileFilter] | None = None


class OrderBody(BaseModel):
    """A provider list's exact new order at the list revision last read."""

    revision: int
    entries: list[str]
    mutation_id: str


def create_app(
    settings: Settings,
    oauth: GitHubOAuth | None = None,
    vault: Vault | None = None,
    sessions: Sessions | None = None,
    commands: ServiceCommands | None = None,
    connection=None,
    revocations: Revocations | None = None,
    identity: Identity | None = None,
) -> FastAPI:
    """Inject external clients, session/logout state and the separate Reader identity for tests."""
    try:
        read_cache = ReadCache(settings.read_cache_dir) if settings.read_cache_dir else None
    except (OSError, sqlite3.Error):
        log.warning("issue read cache unavailable at startup; reading GitHub directly")
        read_cache = None
    oauth = oauth or GitHubOAuth(settings.client_id, settings.client_secret)
    vault = vault or Vault(settings.vault_url)
    commands = commands or ServiceCommands(
        settings.service_url, lists_token=settings.lists_token, task_api_url=settings.task_api_url
    )
    signer = (
        CommandSigner(private.read(settings.command_proof_key_path, (CHECKOUT,)))
        if settings.command_proof_key_path
        else None
    )
    sessions = sessions or Sessions()
    # One Reader identity, so its token cache serves every request.
    reader_app = identity or Identity(settings.reader, **({"connection": connection} if connection else {}))
    sessions.oauth = oauth
    owned_revocations = False
    if revocations is None and settings.auth_state_dir and settings.auth_key_path:
        key = bytes.fromhex(private.read(settings.auth_key_path, (CHECKOUT,)).decode().strip())
        revocations = Revocations(settings.auth_state_dir, key)
        owned_revocations = True
    # Live updates need the list index; without it pages are current only when loaded.
    relay = Relay(commands.list_changes, read_cache) if settings.lists_token else None

    @asynccontextmanager
    async def lifespan(app):
        """Retry encrypted logout jobs after restart without enabling any task recovery."""
        stopped, thread = threading.Event(), None

        def retry():
            """Use the durable queue's own due times and leases, never token material in logs."""
            while not stopped.wait(1):
                try:
                    revocations.drain(oauth)
                except Exception:
                    log.warning("GitHub logout retry state unavailable")

        if revocations:
            thread = threading.Thread(target=retry, name="github-logout", daemon=True)
            thread.start()
        if relay:
            relay.start()
        try:
            yield
        finally:
            if relay:
                relay.stop()
            if read_cache is not None:
                read_cache.close()
            stopped.set()
            if thread:
                thread.join(timeout=5)
            if owned_revocations and (not thread or not thread.is_alive()):
                revocations.close()

    app = FastAPI(
        title="Concurro backoffice", docs_url=None, redoc_url=None, openapi_url="/api/openapi.json", lifespan=lifespan
    )
    diagnostics = Diagnostics(settings.diagnostics_dir)
    cookie_path = settings.base_path or "/"

    def cookie(response, name: str, value: str, max_age: int | None, same_site="lax") -> None:
        response.set_cookie(
            name, value, max_age=max_age, path=cookie_path, secure=settings.secure, httponly=True, samesite=same_site
        )

    def remote_status(group):
        """Keep cleanup storage failures distinct from absent receipts and successful revocation."""
        if not revocations:
            return {"remote": "none"}
        try:
            return revocations.status(group)
        except (RevocationError, sqlite3.Error, OSError):
            return {"remote": "unavailable"}

    def session(request: Request, response: Response) -> Session:
        sid = request.cookies.get(SESSION_COOKIE)
        found = sessions.get(sid)
        if found is None:
            raise HTTPException(401, "sign in with GitHub")
        cookie(response, SESSION_COOKIE, sid, max(1, int(found.expires - sessions.now())), "strict")
        cookie(response, VIEWER_COOKIE, found.viewer_token, max(1, int(found.expires - sessions.now())), "strict")
        return found

    def writer(request: Request, current: Annotated[Session, Depends(session)]) -> Session:
        if request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(403, "missing request header")
        return current

    def tasks(current: Annotated[Session, Depends(session)]) -> Tasks:
        return reader_for(current)

    def reader_for(current: Session) -> Tasks:
        return Tasks(
            settings,
            Access(reader_app, current.token, connection, current.credential),
            current.user,
            diagnostics=diagnostics,
            read_cache=read_cache,
            sponsorships=lambda repository, issue=None: commands.sponsorships(
                current.credential or current.token, repository, issue
            ),
            lists=commands if settings.lists_token else None,
        )

    @app.exception_handler(TaskNotFound)
    async def not_found(_: Request, error: TaskNotFound) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=404)

    @app.exception_handler(Blocked)
    async def refused(_: Request, error: Blocked) -> JSONResponse:
        # Engine refusals carry public text; checkpoint errors render a fixed code and description.
        detail = error.public_detail() if isinstance(error, CheckpointError) else str(error)
        log.warning("engine refused a read: %s", detail)
        return JSONResponse({"detail": "Concurro's task state could not be read: " + detail}, status_code=502)

    @app.exception_handler(Transient)
    async def unavailable(_: Request, __: Transient) -> JSONResponse:
        return JSONResponse({"detail": "GitHub is unavailable or rate-limited; try again shortly"}, status_code=503)

    @app.exception_handler(NotFound)
    async def missing(_: Request, __: NotFound) -> JSONResponse:
        return JSONResponse({"detail": "not found, or not visible to your GitHub account"}, status_code=404)

    @app.exception_handler(IndexUnavailable)
    async def index_unavailable(_: Request, __: IndexUnavailable) -> JSONResponse:
        return JSONResponse(
            {"detail": "Concurro's list index is unavailable and this list has not been saved yet; try again shortly"},
            status_code=503,
        )

    @app.exception_handler(CommandError)
    async def command_refused(_: Request, error: CommandError) -> JSONResponse:
        return JSONResponse({"detail": error.reason}, status_code=error.status)

    @app.exception_handler(VaultError)
    async def vault_refused(_: Request, error: VaultError) -> JSONResponse:
        return JSONResponse({"detail": error.reason}, status_code=error.status)

    @app.exception_handler(AuthError)
    async def authentication_failed(_: Request, error: AuthError):
        """End an unusable browser session without exposing token endpoint responses."""
        response = JSONResponse({"detail": str(error)}, status_code=401)
        response.delete_cookie(SESSION_COOKIE, path=cookie_path)
        return response

    @app.exception_handler(RequestValidationError)
    async def malformed(_: Request, __: RequestValidationError):
        """Credential form validation must not echo submitted secrets."""
        return JSONResponse({"detail": "malformed request"}, status_code=422)

    # Sign-in

    @app.get("/auth/login")
    def login(next: str = "/"):
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        value, pending = sessions.begin(target)
        response = RedirectResponse(authorize_url(settings.client_id, settings.callback_url, pending), status_code=302)
        cookie(response, LOGIN_COOKIE, value, 600)
        return response

    @app.get("/auth/callback")
    def callback(request: Request, code: str | None = None, state: str | None = None):
        try:
            pending = sessions.complete(request.cookies.get(LOGIN_COOKIE), state)
            if not code:
                raise AuthError("GitHub didn't return a sign-in code")
            grant = oauth.exchange(code, settings.callback_url, pending.verifier)
            if not isinstance(grant, TokenGrant):
                grant = TokenGrant(*grant)
            user = oauth.user(grant.token)
        except AuthError as error:
            response = RedirectResponse(f"{settings.base_path}/signin?error={quote(str(error))}", status_code=302)
            response.delete_cookie(LOGIN_COOKIE, path=cookie_path)
            return response
        response = RedirectResponse(settings.base_path + pending.next, status_code=302)
        response.delete_cookie(LOGIN_COOKIE, path=cookie_path)
        lifetime = grant.refresh_expires_in if grant.refresh_token else (grant.expires_in or DEFAULT_TOKEN_SECONDS)
        sid = sessions.create(grant.token, user, grant.expires_in or DEFAULT_TOKEN_SECONDS, grant=grant)
        cookie(response, SESSION_COOKIE, sid, int(lifetime), "strict")
        current = sessions.get(sid)
        assert current is not None
        cookie(response, VIEWER_COOKIE, current.viewer_token, int(lifetime), "strict")
        response.delete_cookie(LOGOUT_COOKIE, path=cookie_path)
        log.info("signed in %s", user.login)
        return response

    @app.post("/auth/logout")
    def logout(request: Request, header: Annotated[str | None, Header(alias=CSRF_HEADER)] = None):
        """End local authorization first; retry only known GitHub access tokens, not App grants."""
        if header != "1":
            raise HTTPException(403, "missing request header")
        group, failed, refreshing = secrets.token_urlsafe(32), [], []

        def persist(current):
            """Journal while the session fence is held, before a late refresh can complete."""
            refreshing.append(current.refreshing)
            if revocations:
                try:
                    revocations.prepare(group, current.token, current.refreshing)
                except RevocationError:
                    failed.append(True)

        def orphan(granted, uncertain):
            """A late token is revoked through the same receipt and never returned to a browser."""
            if revocations and not failed:
                try:
                    revocations.refreshed(group, granted, uncertain)
                except RevocationError:
                    revocations.uncertain(group)
                    if granted:
                        oauth.revoke(granted.token)
                    log.warning("GitHub logout retry state unavailable")
            elif granted:
                oauth.revoke(granted.token)

        current = sessions.end(request.cookies.get(SESSION_COOKIE), orphan, persist)
        if current and revocations and not failed:
            try:
                revocations.drain(oauth, group)
                status = remote_status(group)
            except (RevocationError, sqlite3.Error, OSError):
                status = {"remote": "unavailable"}
        elif current:
            done = oauth.revoke(current.token)
            status = {"remote": "unknown" if any(refreshing) else "revoked" if done else "unavailable"}
        else:
            status = remote_status(request.cookies.get(LOGOUT_COOKIE))
        response = JSONResponse({"signedOut": True, **status})
        response.delete_cookie(SESSION_COOKIE, path=cookie_path)
        response.delete_cookie(VIEWER_COOKIE, path=cookie_path)
        if current and revocations and not failed:
            cookie(response, LOGOUT_COOKIE, group, None, "strict")
        return response

    @app.get("/auth/logout-status")
    def logout_status(request: Request):
        """An opaque, HTTP-only receipt reveals only local logout's remote cleanup status."""
        return remote_status(request.cookies.get(LOGOUT_COOKIE))

    # API

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/me")
    def me(current: Annotated[Session, Depends(session)]):
        user = current.user
        return {"id": user.id, "login": user.login, "name": user.name, "avatarUrl": user.avatar_url}

    @app.get("/api/me/sponsorships")
    def my_sponsorships(reader: Annotated[Tasks, Depends(tasks)]):
        """Visible current and historical provider consent, separate from actual org payment."""
        return reader.my_sponsorships()

    @app.get("/api/repos/{owner}/{repo}/issues/{issue}")
    def issue_view(owner: str, repo: str, issue: int, reader: Annotated[Tasks, Depends(tasks)]):
        """Issue visibility and provider consent before or after enrollment."""
        return reader.issue_view(f"{owner}/{repo}", issue)

    @app.get("/api/events")
    async def events(request: Request, current: Annotated[Session, Depends(session)]):
        """Server-sent changes for the targets this session's user can see; nothing without the list index."""
        if relay is None:
            raise HTTPException(404, "live updates are not enabled")
        sid, seen = request.cookies.get(SESSION_COOKIE), {"until": 0.0, "targets": []}

        def visible():
            """The user's visible targets, decided again once the cached access decisions behind them expired."""
            try:
                if sessions.get(sid) is None:
                    return None
                if time.monotonic() >= seen["until"]:
                    seen["targets"] = [target for target, _ in reader_for(current).visible_targets()]
                    seen["until"] = time.monotonic() + REPOSITORY_SECONDS
            except Exception as error:
                # The stream ends; the browser reconnects and reloads, or is sent to sign in.
                log.warning("live updates: access not decided (%s)", type(error).__name__)
                return None
            return seen["targets"]

        headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        return StreamingResponse(stream(relay, visible), media_type="text/event-stream", headers=headers)

    @app.get("/api/overview")
    def overview(reader: Annotated[Tasks, Depends(tasks)]):
        return reader.overview()

    @app.get("/api/targets/{owner}/{repo}")
    def target(owner: str, repo: str, reader: Annotated[Tasks, Depends(tasks)]):
        return reader.target_view(f"{owner}/{repo}")

    @app.post("/api/targets/{owner}/{repo}/refresh")
    def refresh_target(owner: str, repo: str, current: Annotated[Session, Depends(writer)]):
        """Have every list of a target read from GitHub again; with the list index, through the service."""
        target, _ = tasks(current).find_target(f"{owner}/{repo}")
        if settings.lists_token:
            for repository in target.repositories:
                commands.list_refresh(repository)
        elif read_cache is not None:
            read_cache.outdate(LISTING, target.repositories)
        return {"refreshed": True}

    @app.get("/api/targets/{owner}/{repo}/runs")
    def target_runs(owner: str, repo: str, reader: Annotated[Tasks, Depends(tasks)]):
        return reader.target_runs(f"{owner}/{repo}")

    @app.get("/api/tasks/{owner}/{repo}/{issue}")
    def task(owner: str, repo: str, issue: int, reader: Annotated[Tasks, Depends(tasks)]):
        return reader.task_view(f"{owner}/{repo}", issue)

    @app.get("/api/tasks/{owner}/{repo}/{issue}/sections/{section}")
    def task_section(
        owner: str,
        repo: str,
        issue: int,
        section: Literal["summary", "intake", "plan", "design", "implement", "review", "handoff", "timeline", "pull"],
        reader: Annotated[Tasks, Depends(tasks)],
    ):
        """Load one issue section without unrelated report evidence or live PR enrichment."""
        return reader.task_view(f"{owner}/{repo}", issue, section)

    @app.get("/api/reviews/{owner}/{repo}/{pull}")
    def review(owner: str, repo: str, pull: int, reader: Annotated[Tasks, Depends(tasks)]):
        """A standalone PR review task: live phase, reviewed snapshot, rounds, findings and its report index."""
        return review_tasks.view(reader, f"{owner}/{repo}", pull)

    @app.get("/api/tasks/{owner}/{repo}/{issue}/reports/{report}")
    def task_report(owner: str, repo: str, issue: int, report: str, reader: Annotated[Tasks, Depends(tasks)]):
        """One complete, verified report of a task by its stable publication ID."""
        return review_tasks.report(reader, f"{owner}/{repo}", issue, report)

    @app.get("/api/tasks/{owner}/{repo}/{issue}/turns")
    def turns(owner: str, repo: str, issue: int, reader: Annotated[Tasks, Depends(tasks)]):
        view = reader.task_view(f"{owner}/{repo}", issue)
        turns = diagnostics.turns(f"{owner}/{repo}", issue)
        return {"task": view["task"], "turns": turns, "profiles": view["target"]["profiles"]}

    @app.get("/api/tasks/{owner}/{repo}/{issue}/turns/{identifier}/runs")
    def turn_runs(owner: str, repo: str, issue: int, identifier: str, reader: Annotated[Tasks, Depends(tasks)]):
        """Read Actions evidence for a recorded turn after checking both target repositories."""
        repository = f"{owner}/{repo}"
        target, _ = reader.find_target(repository)
        row = next((row for row in diagnostics.rows(repository, issue) if row["id"] == identifier), None)
        if row is None:
            raise HTTPException(404, "turn not found")
        return Runs(reader.access.transport(target.fork), target.fork).for_turn(row)

    @app.get("/api/repos/{owner}/{repo}/issues/{issue}/sponsorships")
    def sponsorships(owner: str, repo: str, issue: int, current: Annotated[Session, Depends(session)]):
        """Read visible pre-task issue sponsorship without inventing a checkpoint or task stage."""
        repository = f"{owner}/{repo}"
        reader = tasks(current)
        return reader.issue_view(repository, issue)["issue"]["sponsorships"]

    @app.post("/api/repos/{owner}/{repo}/issues/{issue}/commands")
    @app.post("/api/tasks/{owner}/{repo}/{issue}/commands")
    def command(
        owner: str,
        repo: str,
        issue: int,
        body: CommandBody,
        request: Request,
        current: Annotated[Session, Depends(writer)],
    ):
        if body.command not in COMMANDS:
            raise HTTPException(422, "unknown command")
        reader = tasks(current)
        repository = f"{owner}/{repo}"
        reader.find_target(repository, require_writer=body.command != "withdraw")
        try:
            argument = command_argument(body.command, body.argument, lambda: reader.state(repository, issue))
        except ValueError:
            raise HTTPException(422, "invalid command argument") from None
        try:
            if signer:
                try:
                    action_id = str(UUID(request.headers.get("idempotency-key", "")))
                except ValueError:
                    raise HTTPException(400, "missing or invalid action ID") from None
                metadata = reader.access.repository(repository)
                if not isinstance(metadata, dict) or type(metadata.get("id")) is not int:
                    raise TaskNotFound("issue repository is unavailable")
                with current.lock:
                    source_at = current.action_times.setdefault(
                        action_id, datetime.now(UTC).isoformat().replace("+00:00", "Z")
                    )
                result = commands.send_signed(
                    metadata["id"],
                    issue,
                    body.command,
                    argument,
                    {"id": current.user.id, "login": current.user.login},
                    action_id,
                    source_at,
                    signer,
                    body.feedback,
                )
            else:
                result = commands.send(current.credential or current.token, repository, issue, body.command, argument)
        except CommandError as error:
            if error.may_have_posted and read_cache is not None:
                read_cache.invalidate(repository, issue)
            raise
        if read_cache is not None:
            read_cache.invalidate(repository, issue)
        return result

    def outdate_sponsorships():
        """Credential coverage comes from the vault, which the service's signals follow only at its next poll."""
        if read_cache is not None:
            read_cache.outdate(LISTING + "sponsorships:")

    def changed(result):
        """Return a credential write's result once sponsorship coverage is marked stale."""
        outdate_sponsorships()
        return result

    @app.get("/api/me/credentials")
    def credentials(current: Annotated[Session, Depends(session)]):
        return vault.listing(current.credential or current.token)

    @app.post("/api/me/credentials")
    def add_credential(body: EntryBody, current: Annotated[Session, Depends(writer)]):
        fields = body.model_dump(exclude={"type"}, exclude_none=True)
        return changed(vault.add(current.credential or current.token, body.type, fields))

    @app.post("/api/me/credentials/relay")
    def add_relay(body: RelayBody, current: Annotated[Session, Depends(writer)]):
        """Approve the Sponsor's exact route set for the signed-in owner."""
        return changed(vault.add_relay(current.credential or current.token, body.model_dump()))

    @app.get("/api/me/relays")
    def relays(current: Annotated[Session, Depends(session)]):
        """Setup details and live link telemetry of the owner's Sponsor devices."""
        status = vault.relay_status(current.credential or current.token)
        setup = {
            "server": settings.relay_server,
            "download": settings.relay_download_url,
            "command": settings.relay_command(),
            "login": settings.relay_command("login"),
        }
        return {"setup": setup, **status}

    @app.post("/api/me/credentials/relay/pending")
    def pending_relay(body: RelayCodeBody, current: Annotated[Session, Depends(writer)]):
        """Read what a displayed code registered, so approval needs no guessed routes."""
        return vault.pending_relay(current.credential or current.token, body.code)

    @app.put("/api/me/credentials/relay/device")
    def replace_device(body: RelayDeviceBody, current: Annotated[Session, Depends(writer)]):
        """Replace a paired device's approvals with one new Sponsor code."""
        return changed(vault.replace_device(current.credential or current.token, body.model_dump()))

    @app.put("/api/me/credentials/order/{provider}")
    def reorder_credentials(provider: str, body: OrderBody, current: Annotated[Session, Depends(writer)]):
        return changed(vault.reorder(current.credential or current.token, provider, body.model_dump()))

    @app.put("/api/me/credentials/{route}")
    def stale_put_credential(route: str, current: Annotated[Session, Depends(writer)]):
        """Route-addressed saves cannot name one of several entries."""
        raise VaultError(410, STALE)

    @app.patch("/api/me/credentials/{entry_id}")
    def edit_credential(entry_id: str, body: EntryEdit, current: Annotated[Session, Depends(writer)]):
        fields = body.model_dump(exclude_none=True)
        return changed(vault.edit(current.credential or current.token, entry_id, fields))

    @app.put("/api/me/credentials/{entry_id}/secret")
    def replace_credential(entry_id: str, body: ReplaceBody, current: Annotated[Session, Depends(writer)]):
        fields = body.model_dump(exclude_none=True)
        return changed(vault.replace(current.credential or current.token, entry_id, fields))

    @app.put("/api/me/credentials/{entry_id}/relay")
    def replace_relay(entry_id: str, body: RelayReplaceBody, current: Annotated[Session, Depends(writer)]):
        """Replace one relay entry after a fresh Sponsor approval."""
        return changed(vault.replace_relay(current.credential or current.token, entry_id, body.model_dump()))

    @app.delete("/api/me/credentials/{entry_id}")
    def remove_credential(entry_id: str, current: Annotated[Session, Depends(writer)], revision: int | None = None):
        # Only a client older than entry lists removes without the revision it last read.
        if revision is None:
            raise VaultError(410, STALE)
        return changed(vault.remove(current.credential or current.token, entry_id, revision))

    if settings.static_dir:
        mount_frontend(app, settings.static_dir)
    return app


def mount_frontend(app: FastAPI, root: Path) -> None:
    """Serve the static frontend build; unknown paths get the SPA fallback page."""
    root = root.resolve()
    fallback = next((root / name for name in ("200.html", "index.html") if (root / name).exists()), None)

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path.startswith(("api/", "auth/")):
            raise HTTPException(404)
        candidate = (root / path).resolve()
        if candidate.is_relative_to(root):
            for option in (candidate, candidate.with_suffix(".html"), candidate / "index.html"):
                if option.is_file():
                    return FileResponse(option)
        if fallback is None:
            raise HTTPException(404)
        return FileResponse(fallback)
