"""Deliver a writer's command to the service, which verifies them and posts it as Concurro's own comment.

The backoffice never writes to GitHub with a user's token; it forwards the sign-in token so the
service resolves and authorizes the writer itself (`service/actions.py`).
"""

import httpx

DEFAULT_URL = "http://concurro-service:8787"
# Failures before the request reached the service; anything later may follow a posted command.
NOT_DELIVERED = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
UNKNOWN = "the command may or may not have been posted; check the issue before retrying"


class CommandError(Exception):
    """The service refused or failed; `status` is the HTTP status to return, `reason` is safe to show."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


class ServiceCommands:
    def __init__(self, url: str | None = None, client: httpx.Client | None = None):
        self.url = (url or DEFAULT_URL).rstrip("/")
        self.client = client or httpx.Client(timeout=60)

    def sponsorships(self, token: str, repository: str, issue: int | None = None) -> dict:
        """Read issue intents from the daemon without opening its registry database."""
        try:
            response = self.client.post(
                self.url + "/sponsorships",
                headers={"Authorization": "Bearer " + token},
                json={"repository": repository, "issue": issue},
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise CommandError(503, "sponsorship status unavailable") from None
        if response.status_code != 200 or not isinstance(payload, dict):
            raise CommandError(response.status_code, "sponsorship status unavailable")
        return payload

    def send(self, token: str, repository: str, issue: int, command: str, argument: str | None) -> dict:
        """`{posted, actor, url}` once the service has posted the command comment."""
        body = {"repository": repository, "issue": issue, "command": command, "argument": argument}
        try:
            response = self.client.post(self.url + "/actions", headers={"Authorization": "Bearer " + token}, json=body)
        except NOT_DELIVERED:
            raise CommandError(503, "Concurro is unavailable; nothing was posted") from None
        except httpx.HTTPError:
            raise CommandError(502, UNKNOWN) from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code != 201 or not isinstance(payload, dict):
            reason = payload.get("error") if isinstance(payload, dict) else None
            raise CommandError(response.status_code, reason if isinstance(reason, str) else "the command failed")
        return payload
