"""Task-bound ownership and durable identity for pending review mutations."""

import review_api
from reviews import REVIEW_MARKER

PULL_NODE = "query($id:ID!) { node(id:$id) { ... on PullRequest { number state repository { nameWithOwner } } } }"


def binding(gateway, key, repository, body):
    """Return verified review context, or None when the mutation is outside this task."""
    identified = review_api.identify(body)
    if identified is None:
        return None
    name, values = identified
    creating = name == "addPullRequestReview"
    node = gateway._graphql_node(
        repository,
        PULL_NODE if creating else review_api.NODE,
        values["pullRequestId" if creating else "pullRequestReviewId"],
    )
    pr = node if creating else node.get("pullRequest") or {}
    if (
        pr.get("repository", {}).get("nameWithOwner", "").lower() != repository.lower()
        or type(pr.get("number")) is not int
        or pr.get("state") != "OPEN"
        or not gateway.owned_pull(key, pr["number"])
    ):
        return None
    marker = values["body"].split("\n", 1)[0] if creating else (node.get("body") or "").split("\n", 1)[0]
    prefix = f"{REVIEW_MARKER}{key[0]}#{key[1]}:"
    if not marker.startswith(prefix) or not marker.endswith(" -->"):
        return None
    if not creating and (
        node.get("id") != values["pullRequestReviewId"]
        or node.get("state") != "PENDING"
        or (node.get("author") or {}).get("login")
        not in (gateway.bot_login, (gateway.bot_login or "").removesuffix("[bot]"))
    ):
        return None
    if name == "submitPullRequestReview" and values["body"].split("\n", 1)[0] != marker:
        return None
    if name == "addPullRequestReviewThreadReply" and not gateway.finding_thread(
        key, repository, values["pullRequestReviewThreadId"], pr["number"]
    ):
        return None
    return {
        "kind": name,
        "request": body,
        "report": marker,
        "review": None if creating else node["id"],
        "pull": pr["number"],
        "repository": repository,
        "head": values["commitOID"] if creating else node.get("commit", {}).get("oid"),
        "pending_body": marker,
    }


def definitely_failed(record):
    """Only a definite failure, not an operator-cleared unknown, permits another send."""
    return record is not None and record.status == "failed" and record.resolution is None
