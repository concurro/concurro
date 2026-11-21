"""Read-only viewer identity and tier checks on a separate platform listener."""

import secrets
from datetime import UTC, datetime

from fastapi import FastAPI, Header, HTTPException

from github import NotFound
from service.identity import Identity

from .auth import Sessions
from .github import Access
from .settings import Settings


def create_internal_app(settings: Settings, sessions: Sessions, identity: Identity, bearer: str) -> FastAPI:
    """Serve viewer lookups without mounting them on the browser-facing app."""
    app = FastAPI(title="Concurro viewer authority", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/internal/v1/viewer")
    def viewer(
        upstream: str,
        issue_repository: str,
        issue_number: int,
        authorization: str | None = Header(default=None),
        x_concurro_viewer: str | None = Header(default=None),
    ) -> dict:
        """Return the current session identity and issue-specific view tier."""
        expected = "Bearer " + bearer
        if not authorization or not secrets.compare_digest(authorization, expected):
            raise HTTPException(401, "service credential required")
        current = sessions.viewer(x_concurro_viewer)
        if current is None:
            raise HTTPException(401, "viewer token expired")
        target = next(
            (target for target in settings.targets if target.upstream.lower() == upstream.lower()),
            None,
        )
        if target is None or issue_repository.lower() not in {name.lower() for name in target.repositories}:
            raise HTTPException(404, "target not found")
        access = Access(identity, current.token, credential=current.credential)
        upstream_visible = access.repository(target.upstream) is not None
        fork_visible = access.repository(target.fork) is not None
        if upstream_visible and fork_visible and access.writable(target.upstream, current.user.login, current.user.id):
            tier = "writer"
        else:
            try:
                access.transport(issue_repository, as_user=True).request(
                    "GET", f"repos/{issue_repository}/issues/{issue_number}"
                )
            except NotFound:
                tier = "none"
            else:
                tier = "reader"
        return {
            "user": {"id": current.user.id, "login": current.user.login},
            "tier": tier,
            "checked_at": datetime.now(UTC).isoformat(),
        }

    return app
