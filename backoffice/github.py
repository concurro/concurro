"""GitHub reads with the Reader App's read-only tokens; sign-in only identifies the user. The backoffice never writes."""

import re
import time
from urllib.parse import quote

from github import GitHub, NotFound
from service.actions import WRITER_PERMISSIONS
from service.github_reads import content, default_branch
from service.identity import Identity
from service.target_config import read_remote
from service.transport import Transport

# Repository metadata, briefly.
REPOSITORY_SECONDS = 60
REPOSITORIES: dict[str, tuple[float, dict | None]] = {}
QUERY = re.compile(r"\s*(?:query\b|\{)")
ISSUE_PAGES = 3
# The 100 most recently updated pull requests with line counts and their head commit's check runs.
PULL_REQUESTS = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 100, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number title state isDraft headRefName headRefOid additions deletions
        commits(last: 1) {
          nodes { commit { statusCheckRollup { contexts(first: 100) {
            nodes { __typename ... on CheckRun { status conclusion } }
          } } } }
        }
      }
    }
  }
}
"""


class ReadOnlyGitHub(GitHub):
    """The engine's GitHub client for one repository, refusing every write."""

    def __init__(self, repository: str, transport: Transport):
        super().__init__(repository)
        self.transport = transport

    def api(self, path, method="GET", data=None):
        if method == "GET" or path == "graphql" and isinstance(data, dict) and QUERY.match(data.get("query") or ""):
            return self.transport.request(method, path, data)
        raise PermissionError("the backoffice reads task state; it never writes it")


class Access:
    """Per-repository transports with the Reader App's token for each repository; one per request."""

    def __init__(self, identity: Identity, connection=None):
        self.identity, self.connection = identity, connection
        self.transports: dict[str, Transport] = {}

    def transport(self, repository: str) -> Transport:
        if repository not in self.transports:
            options = {"connection": self.connection} if self.connection else {}
            credential = self.identity.credential(repository, "read")
            self.transports[repository] = Transport(repository, credential, time.time, **options)
        return self.transports[repository]

    def engine(self, repository: str) -> ReadOnlyGitHub:
        return ReadOnlyGitHub(repository, self.transport(repository))

    def get(self, repository: str, path: str = ""):
        return self.transport(repository).request("GET", f"repos/{repository}" + ("/" + path if path else ""))

    def repository(self, repository: str) -> dict | None:
        """The repository, or None when the Reader App can't see it; cached briefly."""
        key = repository.lower()
        cached = REPOSITORIES.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        try:
            found = self.get(repository)
        except NotFound:
            found = None
        REPOSITORIES[key] = (time.monotonic() + REPOSITORY_SECONDS, found)
        return found

    def writable(self, repository: str, login: str) -> bool:
        """Whether `login` may give writer commands: the service's and engine's check."""
        try:
            found = self.get(repository, f"collaborators/{quote(login, safe='')}/permission")
        except NotFound:
            return False
        return isinstance(found, dict) and found.get("permission") in WRITER_PERMISSIONS

    def pull_requests(self, repository: str) -> list[dict]:
        """Recently updated pull requests: `number`, `title`, `open`, `draft`, `ref`, `sha`, `additions`,
        `deletions` and the head commit's check `runs` (`status`/`conclusion`, as REST spells them).

        One GraphQL query; if GitHub refuses it, the REST listing, with counts and runs `None`.
        """
        owner, name = repository.split("/")
        try:
            result = self.engine(repository).api(
                "graphql", "POST", {"query": PULL_REQUESTS, "variables": {"owner": owner, "name": name}}
            )
            return [graphql_pull(node) for node in result["data"]["repository"]["pullRequests"]["nodes"]]
        except NotFound:
            raise
        except (RuntimeError, LookupError, TypeError, AttributeError):
            listing = self.get(repository, "pulls?state=all&sort=updated&direction=desc&per_page=100")
            return [rest_pull(pull) for pull in listing]

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


def graphql_pull(node: dict) -> dict:
    """Normalize a GraphQL pull request and its check runs for task lists."""
    commits = (node.get("commits") or {}).get("nodes") or []
    rollup = (commits[-1]["commit"].get("statusCheckRollup") or {}) if commits else {}
    contexts = (rollup.get("contexts") or {}).get("nodes") or []
    return {
        "number": node["number"],
        "title": node.get("title") or "",
        "open": node.get("state") == "OPEN",
        "draft": bool(node.get("isDraft")),
        "ref": node.get("headRefName") or "",
        "sha": node.get("headRefOid") or "",
        "additions": node.get("additions"),
        "deletions": node.get("deletions"),
        "runs": [
            {"status": (c.get("status") or "").lower(), "conclusion": (c.get("conclusion") or "").lower() or None}
            for c in contexts
            if c.get("__typename") == "CheckRun"
        ],
    }


def rest_pull(pull: dict) -> dict:
    """Normalize a REST listing without inventing unavailable counts or check runs."""
    head = pull.get("head") or {}
    return {
        "number": pull["number"],
        "title": pull.get("title") or "",
        "open": pull.get("state") == "open",
        "draft": bool(pull.get("draft")),
        "ref": head.get("ref") or "",
        "sha": head.get("sha") or "",
        "additions": None,
        "deletions": None,
        "runs": None,
    }
