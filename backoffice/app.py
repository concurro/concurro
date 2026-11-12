"""The backoffice HTTP API, behind the edge at `<public_url>/api` and `<public_url>/auth`.

Response shapes follow the frontend's domain types (`concurro-backoffice/src/lib/types.ts`).
"""

import logging
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from checkpoints import CheckpointError
from github import NotFound, Transient
from policy import Blocked

from .auth import AuthError, GitHubOAuth, Session, Sessions, authorize_url
from .commands import CommandError, ServiceCommands
from .credentials import Vault, VaultError
from .github import UserAccess
from .settings import Settings
from .tasks import COMMANDS, TaskNotFound, Tasks, command_argument
from .turns import Diagnostics

log = logging.getLogger("concurro.backoffice")

SESSION_COOKIE = "concurro_session"
LOGIN_COOKIE = "concurro_login"
# Writes must carry this header; browsers can't send it cross-site without a CORS grant, which we never give.
CSRF_HEADER = "x-concurro-request"


class CommandBody(BaseModel):
    command: str
    argument: str | None = None


class SecretBody(BaseModel):
    secret: str


def create_app(
    settings: Settings,
    oauth: GitHubOAuth | None = None,
    vault: Vault | None = None,
    sessions: Sessions | None = None,
    commands: ServiceCommands | None = None,
    connection=None,
) -> FastAPI:
    """`oauth`, `vault`, `sessions`, `commands` and the GitHub `connection` are injectable for tests."""
    app = FastAPI(title="Concurro backoffice", docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    oauth = oauth or GitHubOAuth(settings.client_id, settings.client_secret)
    vault = vault or Vault(settings.vault_url)
    commands = commands or ServiceCommands(settings.service_url)
    sessions = sessions or Sessions()
    diagnostics = Diagnostics(settings.diagnostics_dir)
    cookie_path = settings.base_path or "/"

    def cookie(response, name: str, value: str, max_age: int) -> None:
        response.set_cookie(
            name, value, max_age=max_age, path=cookie_path, secure=settings.secure, httponly=True, samesite="lax"
        )

    def session(request: Request) -> Session:
        found = sessions.get(request.cookies.get(SESSION_COOKIE))
        if found is None:
            raise HTTPException(401, "sign in with GitHub")
        return found

    def writer(request: Request, current: Annotated[Session, Depends(session)]) -> Session:
        if request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(403, "missing request header")
        return current

    def tasks(current: Annotated[Session, Depends(session)]) -> Tasks:
        return Tasks(
            settings,
            UserAccess(current.token, connection),
            current.user,
            lambda repository: commands.sponsorships(current.token, repository),
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

    @app.exception_handler(CommandError)
    async def command_refused(_: Request, error: CommandError) -> JSONResponse:
        return JSONResponse({"detail": error.reason}, status_code=error.status)

    @app.exception_handler(VaultError)
    async def vault_refused(_: Request, error: VaultError) -> JSONResponse:
        return JSONResponse({"detail": error.reason}, status_code=error.status)

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
            token, lifetime = oauth.exchange(code, settings.callback_url, pending.verifier)
            user = oauth.user(token)
        except AuthError as error:
            response = RedirectResponse(f"{settings.base_path}/signin?error={quote(str(error))}", status_code=302)
            response.delete_cookie(LOGIN_COOKIE, path=cookie_path)
            return response
        response = RedirectResponse(settings.base_path + pending.next, status_code=302)
        response.delete_cookie(LOGIN_COOKIE, path=cookie_path)
        cookie(response, SESSION_COOKIE, sessions.create(token, user, lifetime), int(lifetime))
        log.info("signed in %s", user.login)
        return response

    @app.post("/auth/logout")
    def logout(request: Request, _: Annotated[str | None, Header(alias=CSRF_HEADER)] = None):
        sessions.end(request.cookies.get(SESSION_COOKIE))
        response = JSONResponse({"signedOut": True})
        response.delete_cookie(SESSION_COOKIE, path=cookie_path)
        return response

    # API

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/me")
    def me(current: Annotated[Session, Depends(session)]):
        user = current.user
        return {"id": user.id, "login": user.login, "name": user.name, "avatarUrl": user.avatar_url}

    @app.get("/api/overview")
    def overview(reader: Annotated[Tasks, Depends(tasks)]):
        return reader.overview()

    @app.get("/api/targets/{owner}/{repo}")
    def target(owner: str, repo: str, reader: Annotated[Tasks, Depends(tasks)]):
        return reader.target_view(f"{owner}/{repo}")

    @app.get("/api/tasks/{owner}/{repo}/{issue}")
    def task(owner: str, repo: str, issue: int, reader: Annotated[Tasks, Depends(tasks)]):
        return reader.task_view(f"{owner}/{repo}", issue)

    @app.get("/api/tasks/{owner}/{repo}/{issue}/turns")
    def turns(owner: str, repo: str, issue: int, reader: Annotated[Tasks, Depends(tasks)]):
        view = reader.task_view(f"{owner}/{repo}", issue)
        turns = diagnostics.turns(f"{owner}/{repo}", issue)
        return {"task": view["task"], "turns": turns, "profiles": view["target"]["profiles"]}

    @app.get("/api/repos/{owner}/{repo}/issues/{issue}/sponsorships")
    def sponsorships(owner: str, repo: str, issue: int, current: Annotated[Session, Depends(session)]):
        """Read visible pre-task issue sponsorship without inventing a checkpoint or task stage."""
        repository = f"{owner}/{repo}"
        reader = Tasks(settings, UserAccess(current.token, connection), current.user)
        reader.find_target(repository)
        reader.access.get(repository, f"issues/{issue}")
        return commands.sponsorships(current.token, repository, issue)

    @app.post("/api/repos/{owner}/{repo}/issues/{issue}/commands")
    @app.post("/api/tasks/{owner}/{repo}/{issue}/commands")
    def command(
        owner: str,
        repo: str,
        issue: int,
        body: CommandBody,
        current: Annotated[Session, Depends(writer)],
    ):
        if body.command not in COMMANDS:
            raise HTTPException(422, "unknown command")
        reader = Tasks(settings, UserAccess(current.token, connection), current.user)
        repository = f"{owner}/{repo}"
        try:
            argument = command_argument(body.command, body.argument, lambda: reader.state(repository, issue))
        except ValueError:
            raise HTTPException(422, "invalid command argument") from None
        return commands.send(current.token, repository, issue, body.command, argument)

    @app.get("/api/me/credentials")
    def credentials(current: Annotated[Session, Depends(session)]):
        return {"routes": vault.routes(current.token), "usage": []}

    @app.put("/api/me/credentials/{route}")
    def put_credential(route: str, body: SecretBody, current: Annotated[Session, Depends(writer)]):
        return vault.put(current.token, route, body.secret)

    @app.delete("/api/me/credentials/{route}")
    def revoke_credential(route: str, current: Annotated[Session, Depends(writer)]):
        return vault.revoke(current.token, route)

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
