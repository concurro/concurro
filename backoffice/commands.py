"""Deliver a writer's command to the service, which verifies them and posts it as Concurro's own comment.

The backoffice never writes to GitHub with a user's token; it forwards the sign-in token so the
service resolves and authorizes the writer itself (`service/actions.py`).
"""

import httpx

from .auth import BearerAuth, TokenCredential

DEFAULT_URL = "http://concurro-service:8787"
# Failures before the request reached the service; anything later may follow a posted command.
NOT_DELIVERED = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
UNKNOWN = "the command may or may not have been posted; check the issue before retrying"
# List index requests are local and small; a slow answer is treated as no answer.
LIST_SECONDS = 5


class CommandError(Exception):
    """The service refused or failed; `status` is the HTTP status to return, `reason` is safe to show."""

    def __init__(self, status: int, reason: str, *, may_have_posted: bool = False):
        """Retain delivery uncertainty so display caches can refresh after a lost response."""
        super().__init__(reason)
        self.status, self.reason = status, reason
        self.may_have_posted = may_have_posted


class IndexUnavailable(Exception):
    """The service's list index gave no usable answer; never a failure of the user's own sign-in."""


class ServiceCommands:
    def __init__(self, url: str | None = None, client: httpx.Client | None = None, lists_token: str | None = None):
        """`lists_token` is the shared bearer for the list index; without it the index is not used."""
        self.url = (url or DEFAULT_URL).rstrip("/")
        self.client = client or httpx.Client(timeout=60)
        self.lists_token = lists_token

    def lists(self, path: str, body: dict) -> dict:
        """One list index request with the backoffice's own token; any failure is `IndexUnavailable`."""
        try:
            response = self.client.post(
                self.url + path,
                headers={"Authorization": "Bearer " + (self.lists_token or "")},
                json=body,
                timeout=LIST_SECONDS,
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise IndexUnavailable("the list index did not answer") from None
        if response.status_code != 200 or not isinstance(payload, dict):
            raise IndexUnavailable(f"the list index refused the request (HTTP {response.status_code})")
        return payload

    def list_revisions(self, repositories: list[str]) -> dict:
        """`{repository: {kind: {revision, mode, state, verified}}}` as the service reports it."""
        found = self.lists("/lists/revisions", {"repositories": repositories}).get("repositories")
        if not isinstance(found, dict):
            raise IndexUnavailable("the list index answer was malformed")
        return found

    def list_refresh(self, repository: str) -> None:
        """Have the service treat every list of the repository as changed."""
        self.lists("/lists/refresh", {"repository": repository})

    def sponsorships(self, token: str | TokenCredential, repository: str, issue: int | None = None) -> dict:
        """Read issue intents from the daemon without opening its registry database."""
        try:
            response = self.client.post(
                self.url + "/sponsorships",
                auth=BearerAuth(token),
                json={"repository": repository, "issue": issue},
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise CommandError(503, "sponsorship status unavailable") from None
        if response.status_code != 200 or not isinstance(payload, dict):
            raise CommandError(response.status_code, "sponsorship status unavailable")
        return payload

    def send(
        self, token: str | TokenCredential, repository: str, issue: int, command: str, argument: str | None
    ) -> dict:
        """`{posted, actor, url}` once the service has posted the command comment."""
        body = {"repository": repository, "issue": issue, "command": command, "argument": argument}
        try:
            response = self.client.post(self.url + "/actions", auth=BearerAuth(token), json=body)
        except NOT_DELIVERED:
            raise CommandError(503, "Concurro is unavailable; nothing was posted") from None
        except httpx.HTTPError:
            raise CommandError(502, UNKNOWN, may_have_posted=True) from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code != 201 or not isinstance(payload, dict):
            reason = payload.get("error") if isinstance(payload, dict) else None
            raise CommandError(
                response.status_code,
                reason if isinstance(reason, str) else "the command failed",
                may_have_posted=response.status_code >= 500 or response.status_code == 201,
            )
        return payload
