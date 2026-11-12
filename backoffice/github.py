"""GitHub reads as the signed-in user, so GitHub decides what each user may see; the backoffice never writes."""

import hashlib
import re
import time

from github import GitHub, NotFound
from service.github_reads import content, default_branch
from service.target_config import read_remote
from service.transport import StaticCredential, Transport

# Repository metadata (visibility and permissions) per user token, briefly.
REPOSITORY_SECONDS = 60
REPOSITORIES: dict[tuple[str, str], tuple[float, dict | None]] = {}
QUERY = re.compile(r"\s*(?:query\b|\{)")
ISSUE_PAGES = 3


class UserGitHub(GitHub):
    """The engine's GitHub client for one repository, read-only, with the user's token."""

    def __init__(self, repository: str, transport: Transport):
        super().__init__(repository)
        self.transport = transport

    def api(self, path, method="GET", data=None):
        if method == "GET" or path == "graphql" and isinstance(data, dict) and QUERY.match(data.get("query") or ""):
            return self.transport.request(method, path, data)
        raise PermissionError("the backoffice reads task state; it never writes it")


class UserAccess:
    """Per-repository transports for one user token."""

    def __init__(self, token: str, connection=None):
        self.token, self.connection = token, connection
        self.transports: dict[str, Transport] = {}

    def transport(self, repository: str) -> Transport:
        if repository not in self.transports:
            options = {"connection": self.connection} if self.connection else {}
            self.transports[repository] = Transport(repository, StaticCredential(self.token), time.time, **options)
        return self.transports[repository]

    def engine(self, repository: str) -> UserGitHub:
        return UserGitHub(repository, self.transport(repository))

    def get(self, repository: str, path: str = ""):
        return self.transport(repository).request("GET", f"repos/{repository}" + ("/" + path if path else ""))

    def repository(self, repository: str) -> dict | None:
        """The repository as this user sees it, or None when it is hidden from them; cached briefly per user."""
        key = (hashlib.sha256(self.token.encode()).hexdigest(), repository.lower())
        cached = REPOSITORIES.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        try:
            found = self.get(repository)
        except NotFound:
            found = None
        REPOSITORIES[key] = (time.monotonic() + REPOSITORY_SECONDS, found)
        return found

    def task_issues(self, repository: str) -> list[dict]:
        """Recently updated issues opted in with the `concurro` label (pull requests excluded)."""
        found = []
        for page in range(1, ISSUE_PAGES + 1):
            batch = self.get(repository, f"issues?state=all&labels=concurro&sort=updated&per_page=100&page={page}")
            found += [issue for issue in batch if "pull_request" not in issue]
            if len(batch) < 100:
                break
        return found

    def config(self, fork: str) -> tuple[dict, str]:
        """The target configuration at the fork's default-branch head, and that branch's name."""
        reader = self.transport(fork)
        branch, sha = default_branch(reader, fork)
        return read_remote(lambda path: content(reader, fork, path, sha)), branch
