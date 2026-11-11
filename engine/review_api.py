"""Exact GraphQL operations and response contracts for pending review publication."""

import re

from protocol import PUBLICATION_BODY_BYTES

REVIEW_FIELDS = (
    "id databaseId body state author { login } commit { oid } pullRequest { number state repository { nameWithOwner } }"
)
RESULTS = {
    "addPullRequestReview": "pullRequestReview { " + REVIEW_FIELDS + " }",
    "addPullRequestReviewThread": "thread { id }",
    "addPullRequestReviewThreadReply": "comment { id }",
    "submitPullRequestReview": "pullRequestReview { " + REVIEW_FIELDS + " }",
    "deletePullRequestReview": "pullRequestReview { id }",
}
MUTATIONS = {
    name: f"mutation($input:{name[0].upper() + name[1:]}Input!) {{ {name}(input:$input) {{ {fields} }} }}"
    for name, fields in RESULTS.items()
}
NODE = "query($id:ID!) { node(id:$id) { ... on PullRequestReview { " + REVIEW_FIELDS + " } } }"
LIST = """query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
 repository(owner:$owner,name:$name) { pullRequest(number:$number) {
 reviews(first:100,after:$cursor) { nodes { FIELDS } pageInfo { hasNextPage endCursor } }
 } } }""".replace("FIELDS", REVIEW_FIELDS)


def request(name, values):
    """Build one exact mutation without optional arbitrary GraphQL fields."""
    return {"query": MUTATIONS[name], "variables": {"input": values}}


def identify(body):
    """Recognize an exact supported mutation and its bounded input shape."""
    if not isinstance(body, dict) or set(body) != {"query", "variables"}:
        return None
    name = next((k for k, q in MUTATIONS.items() if q == body["query"]), None)
    variables = body["variables"]
    if name is None or not isinstance(variables, dict) or set(variables) != {"input"}:
        return None
    value = variables["input"]
    if not isinstance(value, dict):
        return None
    required = {
        "addPullRequestReview": {"pullRequestId", "commitOID", "body"},
        "addPullRequestReviewThread": {"pullRequestReviewId", "path", "body", "subjectType"},
        "addPullRequestReviewThreadReply": {"pullRequestReviewId", "pullRequestReviewThreadId", "body"},
        "submitPullRequestReview": {"pullRequestReviewId", "event", "body"},
        "deletePullRequestReview": {"pullRequestReviewId"},
    }[name]
    if name == "addPullRequestReviewThread" and value.get("subjectType") == "LINE":
        required |= {"line", "side"}
        if "startLine" in value or "startSide" in value:
            required |= {"startLine", "startSide"}
    if set(value) != required:
        return None
    # Retained publication intents use the historical character bound, including Unicode retries.
    for key, item in value.items():
        if key in ("line", "startLine"):
            if type(item) is not int or item < 1:
                return None
        elif not isinstance(item, str) or not item or len(item) > PUBLICATION_BODY_BYTES:
            return None
    if name == "addPullRequestReview" and not re.fullmatch(r"[0-9a-f]{40}", value["commitOID"]):
        return None
    if name == "submitPullRequestReview" and value["event"] != "COMMENT":
        return None
    if name == "addPullRequestReviewThread" and (
        value["subjectType"] not in ("FILE", "LINE")
        or value.get("side", "RIGHT") != "RIGHT"
        or value.get("startSide", "RIGHT") != "RIGHT"
        or value.get("startLine", 1) > value.get("line", 1)
    ):
        return None
    return name, value


def result(name, values, payload):
    """Validate a response; explicit null thread is a confirmed placement failure."""
    if not isinstance(payload, dict) or "errors" in payload or not isinstance(payload.get("data"), dict):
        raise ValueError("unconfirmed review mutation")
    data = payload["data"].get(name)
    field = (
        "thread"
        if name == "addPullRequestReviewThread"
        else "comment"
        if name == "addPullRequestReviewThreadReply"
        else "pullRequestReview"
    )
    if not isinstance(data, dict) or field not in data:
        raise ValueError("missing review mutation result")
    node = data[field]
    if field == "thread" and node is None:
        return None
    if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"]:
        raise ValueError("missing review object identity")
    if name in ("submitPullRequestReview", "deletePullRequestReview") and node["id"] != values["pullRequestReviewId"]:
        raise ValueError("review identity changed")
    expected = {"addPullRequestReview": "PENDING", "submitPullRequestReview": "COMMENTED"}.get(name)
    if expected and node.get("state") != expected:
        raise ValueError("unconfirmed review state")
    return node


def mutate(github, name, **values):
    """Send and verify one supported mutation through the normal GitHub facade."""
    return result(name, values, github.api("graphql", "POST", request(name, values)))


def node(github, identifier):
    """Read one review by its immutable node ID, rejecting incomplete responses."""
    response = github.api("graphql", "POST", {"query": NODE, "variables": {"id": identifier}})
    if response.get("errors") or not isinstance(response.get("data"), dict) or "node" not in response["data"]:
        raise ValueError("cannot read pending review")
    return response["data"]["node"]


def reviews(github, number):
    """Read all reviews, including the authenticated App's invisible pending review."""
    owner, name = github.repository.split("/")
    cursor = None
    while True:
        response = github.api(
            "graphql",
            "POST",
            {"query": LIST, "variables": {"owner": owner, "name": name, "number": number, "cursor": cursor}},
        )
        if response.get("errors"):
            raise ValueError("cannot list reviews")
        page = response["data"]["repository"]["pullRequest"]["reviews"]
        yield from page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return
        cursor = page["pageInfo"]["endCursor"]
