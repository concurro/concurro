"""Read target files and runner identities using repository read credentials."""

import base64
from urllib.parse import quote

from policy import Blocked

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


def runner_claims(reader, repository, workflow_ref, engine, bot_login):
    """The OIDC claims a runner session of this target must present: the target, the pinned agent
    workflow at the served engine commit, a dispatch by the App's bot on the default branch."""
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
        "job_workflow_sha": engine,
        "event_name": "workflow_dispatch",
        "ref": "",
        "actor_id": str(bot["id"]),
    }


def content(reader, repository, path, sha, limit):
    """A regular file's text at one commit; links, directories and oversized files are refused."""
    item = reader.request("GET", f"repos/{repository}/contents/{quote(path)}?ref={sha}")
    if (
        not isinstance(item, dict)
        or item.get("type") != "file"
        or item.get("encoding") != "base64"
        or item.get("path") != path
        or not isinstance(item.get("size"), int)
        or item["size"] > limit
    ):
        raise Blocked("target configuration must be bounded regular files")
    data = base64.b64decode(item["content"])
    if len(data) != item["size"]:
        raise Blocked("target configuration must be bounded regular files")
    return data.decode("utf-8")
