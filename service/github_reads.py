"""Read target files and runner identities using repository read credentials."""

import base64
import re
from urllib.parse import quote, urlencode

from github import NotFound
from policy import Blocked

from .admission import EXECUTOR_WORKFLOW

# What a list of executor runs shows of each run attempt.
RUN_FIELDS = (
    "id",
    "run_attempt",
    "status",
    "conclusion",
    "head_branch",
    "event",
    "path",
    "created_at",
    "run_started_at",
    "updated_at",
)
TARGET_RUNS = 50
# The label that opts an issue in.
ENROLLMENT = "concurro"
EXECUTOR_PATH = f".github/workflows/{EXECUTOR_WORKFLOW}"
# The engine's task branches (`policy.py`): `concurro/issue-<number>-<digest>`.
TASK_BRANCH = re.compile(r"concurro/issue-([1-9][0-9]*)-[0-9a-f]+")
# What a list shows of a pull request, with its head commit's check runs.
PULL_FIELDS = """
        number title state isDraft headRefName headRefOid additions deletions updatedAt
        commits(last: 1) {
          nodes { commit { statusCheckRollup { contexts(first: 100) {
            nodes { __typename ... on CheckRun { status conclusion } }
          } } } }
        }"""
# The 100 most recently updated pull requests.
PULL_REQUESTS = f"""
query($owner: String!, $name: String!) {{
  repository(owner: $owner, name: $name) {{
    pullRequests(first: 100, orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
      nodes {{{PULL_FIELDS}
      }}
    }}
  }}
}}
"""
PULL_REQUEST = f"""
query($owner: String!, $name: String!, $number: Int!) {{
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $number) {{{PULL_FIELDS}
    }}
  }}
}}
"""

DEFAULT_BRANCH = """query($owner:String!,$name:String!) {
  repository(owner:$owner,name:$name) { defaultBranchRef { name target { oid } } }
}"""


RUNNER_IDENTITY = """query($owner:String!,$name:String!) {
  repository(owner:$owner,name:$name) {
    databaseId defaultBranchRef { name }
    owner { ... on User { databaseId } ... on Organization { databaseId } }
  }
}"""


def default_branch(reader, repository):
    """The default branch's name and head commit."""
    owner, name = repository.split("/")
    response = reader.request("POST", "graphql", {"query": DEFAULT_BRANCH, "variables": {"owner": owner, "name": name}})
    branch = ((response or {}).get("data") or {}).get("repository", {}).get("defaultBranchRef") or {}
    sha = (branch.get("target") or {}).get("oid")
    if response.get("errors") or not isinstance(sha, str) or len(sha) != 40 or not isinstance(branch.get("name"), str):
        raise Blocked("cannot establish the target's default branch")
    return branch["name"], sha


def head(reader, repository):
    """The default branch's head commit."""
    return default_branch(reader, repository)[1]


def runner_claims(reader, repository, workflow_ref, bot_login):
    """The OIDC claims a runner session of this target must present: the target, the launcher
    workflow at its configured ref, a dispatch by the App's bot on the default branch."""
    owner, name = repository.split("/")
    response = reader.request(
        "POST", "graphql", {"query": RUNNER_IDENTITY, "variables": {"owner": owner, "name": name}}
    )
    found = ((response or {}).get("data") or {}).get("repository") or {}
    branch = (found.get("defaultBranchRef") or {}).get("name")
    # A bot is not a GraphQL `user`; its public profile is the one read outside the repository.
    bot = reader.request("GET", "users/" + quote(bot_login))
    if (
        response.get("errors")
        or not isinstance(found.get("databaseId"), int)
        or not isinstance((found.get("owner") or {}).get("databaseId"), int)
        or not isinstance(branch, str)
        or not isinstance(bot, dict)
        or not isinstance(bot.get("id"), int)
        or bot.get("login") != bot_login
    ):
        raise Blocked("cannot establish the target's runner identity")
    return {
        "repository_id": str(found["databaseId"]),
        "repository_owner_id": str(found["owner"]["databaseId"]),
        "job_workflow_ref": workflow_ref,
        "event_name": "workflow_dispatch",
        "ref": "",
        "actor_id": str(bot["id"]),
    }


def content(reader, repository, path, sha, limit=None):
    """A regular file's text at one commit; links, directories and oversized files are refused."""
    item = reader.request("GET", f"repos/{repository}/contents/{quote(path)}?ref={sha}")
    if (
        not isinstance(item, dict)
        or item.get("type") != "file"
        or item.get("encoding") != "base64"
        or item.get("path") != path
        or not isinstance(item.get("size"), int)
        or limit is not None
        and item["size"] > limit
    ):
        raise Blocked("target configuration must be regular files within the requested transport allowance")
    data = base64.b64decode(item["content"])
    if len(data) != item["size"]:
        raise Blocked("target configuration must be regular files within the requested transport allowance")
    return data.decode("utf-8")


def run_fields(run):
    """One run attempt reduced to the listed fields; refuses anything without its identity."""
    if not isinstance(run, dict) or type(run.get("id")) is not int or type(run.get("run_attempt")) is not int:
        raise ValueError("malformed workflow run")
    return {field: run.get(field) for field in RUN_FIELDS}


def executor_run(run):
    """A webhook's `workflow_run` as a listed executor run, or None for any other workflow or trigger.

    A run without its identity, trigger or workflow path is refused: it cannot be told apart.
    """
    fields = run_fields(run)
    if not isinstance(fields["event"], str) or not isinstance(fields["path"], str):
        raise ValueError("malformed workflow run")
    if fields["event"] != "workflow_dispatch" or fields["path"].split("@", 1)[0] != EXECUTOR_PATH:
        return None
    return fields


def executor_runs(reader, fork, limit=TARGET_RUNS):
    """The fork's most recent executor `workflow_dispatch` runs, newest first, on any branch."""
    query = urlencode({"event": "workflow_dispatch", "per_page": limit})
    listing = reader.request("GET", f"repos/{fork}/actions/workflows/{EXECUTOR_WORKFLOW}/runs?{query}")
    listed = listing.get("workflow_runs") if isinstance(listing, dict) else None
    if not isinstance(listed, list):
        raise ValueError("malformed workflow run listing")
    return [run_fields(run) for run in listed]


def run_attempt(reader, fork, run, attempt):
    """One exact attempt of a run, or None when GitHub no longer has it."""
    try:
        return run_fields(reader.request("GET", f"repos/{fork}/actions/runs/{run}/attempts/{attempt}"))
    except NotFound:
        return None


def gone(error):
    """Whether a read failed because GitHub moved or deleted the item, not because of an outage."""
    return isinstance(error, NotFound) or str(error).endswith(("(HTTP 301)", "(HTTP 410)"))


def issue_fields(issue):
    """An issue reduced to what lists show, in GitHub's own shape; None for a pull request.

    Refuses anything without the identity, labels and update time a row is ordered and listed by.
    """
    if (
        not isinstance(issue, dict)
        or type(issue.get("number")) is not int
        or not isinstance(issue.get("updated_at"), str)
        or not isinstance(issue.get("labels"), list)
    ):
        raise ValueError("malformed issue")
    if "pull_request" in issue:
        return None
    return {
        **{field: issue.get(field) for field in ("number", "title", "state", "created_at", "updated_at")},
        "user": {"login": (issue.get("user") or {}).get("login", "")},
        "labels": [{"name": label["name"]} for label in issue["labels"]],
    }


def enrolled_issues(reader, repository, pages=None):
    """Issues opted in with the enrollment label, open and closed, most recently updated first.

    Every page unless `pages` caps them; pull requests are left out.
    """
    found, page = [], 1
    while pages is None or page <= pages:
        path = f"repos/{repository}/issues?state=all&labels={ENROLLMENT}&sort=updated&per_page=100&page={page}"
        batch = reader.request("GET", path)
        found += [issue for issue in batch if "pull_request" not in issue]
        if len(batch) < 100:
            break
        page += 1
    return found


def issue(reader, repository, number):
    """One issue, or None when GitHub moved or deleted it."""
    try:
        return reader.request("GET", f"repos/{repository}/issues/{number}")
    except RuntimeError as error:
        if not gone(error):
            raise
        return None


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
        "updated_at": node.get("updatedAt"),
        "runs": [
            {"status": (c.get("status") or "").lower(), "conclusion": (c.get("conclusion") or "").lower() or None}
            for c in contexts
            if c.get("__typename") == "CheckRun"
        ],
    }


def pull_fields(pull):
    """A webhook's `pull_request` in the shape `graphql_pull` gives, without check runs.

    Refuses anything without the identity, head and update time a row is ordered and matched by.
    """
    head = pull.get("head") if isinstance(pull, dict) else None
    if (
        not isinstance(head, dict)
        or type(pull.get("number")) is not int
        or not isinstance(pull.get("updated_at"), str)
        or not isinstance(head.get("ref"), str)
        or not isinstance(head.get("sha"), str)
    ):
        raise ValueError("malformed pull request")
    return {
        "number": pull["number"],
        "title": pull.get("title") or "",
        "open": pull.get("state") == "open",
        "draft": bool(pull.get("draft")),
        "ref": head["ref"],
        "sha": head["sha"],
        "additions": pull.get("additions"),
        "deletions": pull.get("deletions"),
        "updated_at": pull["updated_at"],
    }


def rollup_pull(node):
    """A pull request read with its rollup; refuses one that cannot be ordered or matched to its checks."""
    found = graphql_pull(node)
    if type(found["number"]) is not int or not isinstance(found["updated_at"], str) or not found["sha"]:
        raise ValueError("malformed pull request")
    return found


def graphql(reader, repository, query, **variables):
    """One GraphQL read of a repository; its `repository` object and the errors GitHub reported."""
    owner, name = repository.split("/")
    response = reader.request(
        "POST", "graphql", {"query": query, "variables": {"owner": owner, "name": name, **variables}}
    )
    found = (response.get("data") or {}).get("repository") if isinstance(response, dict) else None
    if not isinstance(found, dict):
        raise ValueError("malformed GraphQL answer")
    return found, response.get("errors") or []


def task_pulls(reader, repository):
    """The task-branch pull requests among the 100 most recently updated, with their check runs.

    An answer carrying errors is refused whole: part of it, such as a rollup, may be missing.
    """
    found, errors = graphql(reader, repository, PULL_REQUESTS)
    if errors:
        raise ValueError("incomplete GraphQL answer")
    pulls = [rollup_pull(node) for node in found["pullRequests"]["nodes"]]
    return [pull for pull in pulls if TASK_BRANCH.fullmatch(pull["ref"])]


def pull_request(reader, repository, number):
    """One pull request with its check runs, or None when GitHub no longer has it.

    Only an answer without the pull request whose every error says it was not found means
    absence; any other answer carrying errors is refused, since part of it may be missing.
    """
    found, errors = graphql(reader, repository, PULL_REQUEST, number=number)
    missing = all(isinstance(error, dict) and error.get("type") == "NOT_FOUND" for error in errors)
    if found.get("pullRequest") is None and errors and missing:
        return None
    if errors or found.get("pullRequest") is None:
        raise ValueError("incomplete GraphQL answer")
    return rollup_pull(found["pullRequest"])
