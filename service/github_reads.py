"""Read target files and runner identities using repository read credentials."""

import base64
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
EXECUTOR_PATH = f".github/workflows/{EXECUTOR_WORKFLOW}"

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
