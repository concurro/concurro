"""Commands writers give in the backoffice, delivered to the service instead of posted as their comments.

The backoffice forwards the request with the writer's GitHub sign-in token. The service resolves
the writer itself (`GET /user`), checks their write permission with its own read token, posts one
App-authored comment carrying the signed command (`engine/delegation.py`) and wakes the task. The
controller then applies it as that writer's command, re-checking their permission as it does for
any command comment. After a post that succeeded or may have, the service requests a durable
observation of the exact comment (`Registry.request_observation`); it wakes the task through
`registry.observe`, so a tick that already saw the command is not repeated and retry backoff
holds. The command's own webhook schedules the same observation, and webhook recovery redelivers
it after a lost response or a restart. A task with no outcome, live tick or deferred retry is also
queued durably before posting. Reachable on the internal network only; the edge routes just
`/webhook` here.
"""

import json
import logging
import secrets
import time
from urllib.parse import quote

import delegation
from github import NotFound, Transient
from sponsorship import own_consent, own_consent_present

from . import transport
from .settlement import settle
from .targets import enrollment

log = logging.getLogger("concurro.service")

WRITER_PERMISSIONS = ("admin", "maintain", "write")
BODY_LIMIT = 16 * 1024
USER_LIMIT = 1_000_000
# An unscheduled task whose command post may have applied still ticks this much later.
RECOVERY_SECONDS = 600
UNKNOWN = "the command may or may not have been posted; check the issue before retrying"


class Refused(Exception):
    """A request the service won't act on; `status` is its HTTP status, the text is safe to return."""

    def __init__(self, status, reason):
        super().__init__(reason)
        self.status, self.reason = status, reason


def github_user(token, connection=transport.connect):
    """The `(login, id)` a GitHub user token belongs to, or None when GitHub rejects it."""
    headers = {**transport.headers(), "Authorization": "Bearer " + token}
    try:
        response = transport.exchange(connection, transport.API, "GET", "/user", headers, None, USER_LIMIT, 30)
        user = response.json() if response.sent and response.status == 200 else None
    except (OSError, ValueError):
        raise Refused(503, "GitHub is unavailable; try again") from None
    if not response.sent or response.status >= 500:
        raise Refused(503, "GitHub is unavailable; try again")
    if response.status in (403, 429):
        raise Refused(503, "GitHub is rate limiting; try again shortly")
    if not isinstance(user, dict) or type(user.get("id")) is not int or not isinstance(user.get("login"), str):
        return None
    return user["login"], user["id"]


def github_visible(token, repository, issue=None, connection=transport.connect):
    """Verify issue/repository visibility with the caller's own token and fixed error text."""
    path = "/repos/" + repository + (f"/issues/{issue}" if issue is not None else "")
    try:
        response = transport.exchange(
            connection,
            transport.API,
            "GET",
            path,
            {**transport.headers(), "Authorization": "Bearer " + token},
            None,
            USER_LIMIT,
            30,
        )
    except (OSError, ValueError):
        raise Refused(503, "GitHub is unavailable; try again") from None
    if not response.sent or response.status >= 500 or response.status in (403, 429):
        raise Refused(503, "GitHub visibility is unavailable; try again")
    return response.status == 200


class Actions:
    """Verify a writer's command, post it as the App's delegated command comment and enqueue the task."""

    def __init__(
        self,
        key,
        registry,
        reader,
        writer,
        targets,
        user=github_user,
        clock=time.time,
        visible=github_visible,
        projection=None,
    ):
        """`reader(repository)`/`writer(repository)` return that repository's transports."""
        self.key, self.registry, self.reader, self.writer = key, registry, reader, writer
        self.targets = enrollment(targets)
        self.issue_repositories = {r.lower(): r for r in self.targets}
        self.user, self.clock, self.visible = user, clock, visible
        self.projection = projection or (lambda key: registry.sponsorship(key))

    def handle(self, authorization, body):
        """`(status, payload)` for one request; never raises for a refused or failed request."""
        try:
            return 201, self.act(authorization, body)
        except Refused as error:
            return error.status, {"error": error.reason}

    def act(self, authorization, body):
        if not self.key:
            raise Refused(503, "backoffice commands are not configured")
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise Refused(401, "missing bearer token")
        try:
            request = json.loads(body)
            repository = self.issue_repositories[str(request["repository"]).lower()]
            issue, kind, argument = request["issue"], request["command"], request.get("argument")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise Refused(400, "malformed request, or not an issue repository of a configured target") from None
        if type(issue) is not int or issue < 1 or not isinstance(kind, str):
            raise Refused(400, "malformed request")
        try:
            command = delegation.command_text(kind, argument)
        except ValueError:
            raise Refused(422, "not a valid /concurro command") from None
        user = self.user(token.strip())
        if user is None:
            raise Refused(401, "GitHub did not accept the sign-in token")
        login, user_id = user
        if kind == "withdraw" and not self.visible(token.strip(), repository, issue):
            raise Refused(404, "issue not visible")
        upstream = self.targets[repository].upstream
        if kind != "withdraw" and not self.writable(upstream, login):
            raise Refused(403, f"@{login} does not have write access to {upstream}")
        text = delegation.render(self.key, repository, issue, login, user_id, command, secrets.token_hex(16))
        key, path = (repository, issue), f"repos/{repository}/issues/{issue}/comments"
        # Unscheduled, so no poll may find it and no backoff can be shortened: recover it durably.
        self.registry.enqueue_unscheduled(key, "backoffice:" + kind, self.clock() + RECOVERY_SECONDS)
        try:
            response = self.writer(repository).send("POST", path, {"body": text})
        except Exception:
            outcome, payload, error, status = "unknown", None, None, None
        else:
            outcome, payload, error = settle(path, response, self.clock())
            status = response.status
        if outcome != "failed":
            # Observe until this exact comment is visible; a missing response keeps the posted body.
            created = payload if isinstance(payload, dict) else {}
            comment = {"id": created.get("id"), "body": text, "updated_at": created.get("updated_at")}
            self.registry.request_observation(key, comment=comment, action="created")
        if outcome == "unknown":
            log.warning("delegated command for %s#%d: outcome unknown", repository, issue)
            raise Refused(502, UNKNOWN)
        if outcome == "failed":
            if isinstance(error, Transient):
                raise Refused(503, "GitHub is unavailable; nothing was posted")
            raise Refused(502, f"GitHub refused the command comment (HTTP {status})")
        url = payload.get("html_url") if isinstance(payload, dict) else None
        log.info("delegated command %s#%d by %s: %s", repository, issue, login, kind)
        return {"posted": command, "actor": login, "url": url}

    def sponsorships(self, authorization, body):
        """Return issue-addressed nonsecret projections after independent user visibility verification."""
        try:
            scheme, _, token = (authorization or "").partition(" ")
            user = self.user(token.strip()) if scheme.lower() == "bearer" and token.strip() else None
            if user is None:
                raise Refused(401, "GitHub did not accept the sign-in token")
            try:
                request = json.loads(body)
                repository = self.issue_repositories[str(request["repository"]).lower()]
                issue = request.get("issue")
            except (ValueError, KeyError, TypeError):
                raise Refused(400, "invalid issue repository") from None
            if issue is not None and (type(issue) is not int or issue < 1):
                raise Refused(400, "invalid issue")
            if not self.visible(token.strip(), repository, issue):
                raise Refused(404, "issue not visible")
            writer = self.writable(self.targets[repository].upstream, user[0])
            if issue is None:
                issues = []
                for number in self.registry.sponsor_issues(repository):
                    projection = (
                        self.projection((repository, number))
                        if writer
                        else self.registry.sponsorship((repository, number))
                    )
                    if not writer:
                        projection = own_consent(projection, str(user[1]))
                        if not own_consent_present(projection):
                            continue
                    issues.append({"issue": number, **projection})
                return 200, {"issues": issues}
            projection = (
                self.projection((repository, issue)) if writer else self.registry.sponsorship((repository, issue))
            )
            if projection["status"] != "confirmed":
                self.registry.request_observation((repository, issue))
            return 200, projection if writer else own_consent(projection, str(user[1]))
        except Refused as error:
            return error.status, {"error": error.reason}

    def writable(self, repository, login):
        """The same writer check the engine applies (`GitHub.permitted`)."""
        try:
            result = self.reader(repository).request(
                "GET", f"repos/{repository}/collaborators/{quote(login, safe='')}/permission"
            )
        except NotFound:
            return False
        return isinstance(result, dict) and result.get("permission") in WRITER_PERMISSIONS
