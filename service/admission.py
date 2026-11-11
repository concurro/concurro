"""Pure request-shape admission for task, publication and runner writes."""

import re

import review_api
import run_display
from checkpoint_health import MARKER as HEALTH_MARKER
from checkpoints import MARKER as STATUS_MARKER
from conversation import MARKER as EVENT_MARKER
from github import MARK_READY, RESOLVE_THREAD
from policy import task_branch
from protocol import PUBLICATION_BODY_BYTES

NUMBER = r"[1-9][0-9]*"
# Writes whose results the engine reconstructs from GitHub on its next tick; any other write is
# refused before admission. A named `issue` group must equal the task's own issue.
WRITES = (
    ("POST", rf"issues/(?P<issue>{NUMBER})/labels"),
    ("DELETE", rf"issues/(?P<issue>{NUMBER})/labels/[^/?#]+"),
    ("POST", r"labels"),
    ("POST", rf"issues/(?P<issue>{NUMBER})/comments"),
    ("PATCH", rf"issues/comments/{NUMBER}"),
    ("POST", rf"issues/(?P<issue>{NUMBER})/artifacts"),
    ("PATCH", rf"issues/(?P<issue>{NUMBER})/artifacts/{NUMBER}"),
    ("POST", r"git/refs"),
    ("POST", r"git/trees"),
    ("POST", r"git/blobs"),
    ("POST", r"git/commits"),
    ("PATCH", r"git/refs/heads/[A-Za-z0-9._/-]{1,100}"),
    ("POST", r"pulls"),
    ("PATCH", rf"pulls/{NUMBER}"),
    ("POST", rf"pulls/{NUMBER}/reviews"),
    ("POST", rf"pulls/{NUMBER}/comments"),
    ("POST", rf"pulls/{NUMBER}/comments/{NUMBER}/replies"),
)
# A runner session's start: the agent event type, carrying only the session's ID.
DISPATCH = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# The App only ever comments on its PRs; it never approves or requests changes.
PR_BODIES = (
    (re.compile(rf"pulls/{NUMBER}/reviews"), {"commit_id", "event", "body", "comments"}),
    (re.compile(rf"pulls/{NUMBER}/comments"), {"commit_id", "path", "subject_type", "body"}),
    (re.compile(rf"pulls/{NUMBER}/comments/{NUMBER}/replies"), {"body"}),
)
ISSUE_COMMENT = re.compile(rf"issues/comments/(?P<comment>{NUMBER})")
# The App's issue comments it edits: the status comment, its health notice and Input needed requests.
EDITABLE = (STATUS_MARKER, HEALTH_MARKER, EVENT_MARKER)
PULL = re.compile(rf"pulls/(?P<pull>{NUMBER})(?:/comments/(?P<comment>{NUMBER})/replies|/.*)?")
BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,100}")
OID = re.compile(r"[0-9a-f]{40}")
QUERY = re.compile(r"\s*query\b")

RUN_BRANCH_PREFIX = "concurro/runs/"
EXECUTOR_WORKFLOW = "concurro-executor.yml"


def text_blob(body):
    """Only a UTF-8 text blob may be created for controller publication."""
    return (
        isinstance(body, dict)
        and set(body) == {"content", "encoding"}
        and body["encoding"] == "utf-8"
        and isinstance(body["content"], str)
        and "\x00" not in body["content"]
    )


def allowed(key, method, path, body):
    """Whether a write's shape is on the allow-list for this task's repository and issue.

    PR writes and thread resolutions are further bound to the task's own PR and the App's own
    threads by `MutationGateway.bound`, which needs reads.
    """
    if path == "graphql":
        return _graphql_write(method, body)
    prefix = f"repos/{key[0]}/"
    if not path.startswith(prefix):
        return False
    relative = path[len(prefix) :]
    for verb, pattern in WRITES:
        match = re.fullmatch(pattern, relative) if verb == method else None
        if match:
            if "issue" in match.groupdict() and int(match["issue"]) != key[1]:
                return False
            if relative == "git/blobs":
                return text_blob(body)
            if relative == "git/refs":
                return _create_ref_body(body, task_branch(*key))
            # Legacy ref updates deliberately check only shape/fast-forward;
            # allowed_target additionally requires the exact task branch and a valid OID.
            if relative.startswith("git/refs/"):
                return isinstance(body, dict) and set(body) == {"sha", "force"} and body["force"] is False
            if method == "PATCH" and re.fullmatch(rf"pulls/{NUMBER}", relative):
                return (
                    isinstance(body, dict)
                    and set(body) == {"body"}
                    and isinstance(body["body"], str)
                    and len(body["body"].encode()) <= PUBLICATION_BODY_BYTES
                )
            for shape, fields in PR_BODIES:
                if shape.fullmatch(relative):
                    return (
                        isinstance(body, dict)
                        and set(body) == fields
                        and body.get("event", "COMMENT") == "COMMENT"
                        and body.get("subject_type", "file") == "file"
                    )
            return True
    return False


def destination(key, binding, path):
    """The token repository for a request; GraphQL PR mutations use upstream."""
    if path == "graphql":
        return binding.upstream if binding else key[0]
    parts = path.split("/", 3)
    return "/".join(parts[1:3]) if len(parts) == 4 and parts[0] == "repos" else None


def allowed_target(key, binding, method, path, body):
    """Admit only issue writes, fork publication, and upstream COMMENT reviews for a bound target."""
    if path == "graphql":
        return _graphql_write(method, body)
    repository = destination(key, binding, path)
    relative = path.split("/", 3)[-1]
    if relative.startswith(("issues/", "labels")):
        return repository == key[0] and allowed(key, method, path, body)
    branch = task_branch(*key)
    if relative.startswith("git/"):
        if repository != binding.fork:
            return False
        if method == "POST" and relative == "git/blobs":
            return text_blob(body)
        if method == "POST" and relative in ("git/trees", "git/commits"):
            return isinstance(body, dict)
        if method == "POST" and relative == "git/refs":
            return _create_ref_body(body, branch)
        return (
            method == "PATCH"
            and relative == "git/refs/heads/" + branch
            and isinstance(body, dict)
            and set(body) == {"sha", "force"}
            and body["force"] is False
            and isinstance(body["sha"], str)
            and OID.fullmatch(body["sha"]) is not None
        )
    if repository != binding.upstream:
        return False
    if method == "POST" and relative == "pulls":
        return (
            isinstance(body, dict)
            and set(body) == {"head", "head_repo", "base", "title", "body", "draft", "maintainer_can_modify"}
            and body["head"] == binding.fork.split("/")[0] + ":" + branch
            and body["head_repo"] == binding.fork.split("/")[1]
            and body["draft"] is True
            and body["maintainer_can_modify"] is False
            and all(isinstance(body[k], str) for k in ("base", "title", "body"))
            and BRANCH.fullmatch(body["base"]) is not None
        )
    return (
        relative.startswith("pulls/")
        and method in ("POST", "PATCH")
        and allowed((repository, key[1]), method, path, body)
    )


def runner_write(path, method, body):
    """The session ID of an exact session-ref or workflow dispatch request, or None."""
    relative = path.split("/", 3)[-1]
    if method == "POST" and relative == "git/refs" and isinstance(body, dict) and set(body) == {"ref", "sha"}:
        ref = body["ref"]
        prefix = "refs/heads/" + RUN_BRANCH_PREFIX
        return (
            ref[len(prefix) :]
            if isinstance(ref, str) and ref.startswith(prefix) and DISPATCH.fullmatch(ref[len(prefix) :])
            else None
        )
    if method == "DELETE" and relative.startswith("git/refs/heads/" + RUN_BRANCH_PREFIX) and body is None:
        value = relative.removeprefix("git/refs/heads/" + RUN_BRANCH_PREFIX)
        return value if DISPATCH.fullmatch(value) else None
    if (
        method == "POST"
        and relative == f"actions/workflows/{EXECUTOR_WORKFLOW}/dispatches"
        and isinstance(body, dict)
        and set(body) == {"ref", "inputs"}
        and isinstance(body["inputs"], dict)
        and {"session"} <= body["inputs"].keys() <= {"session"} | run_display.INPUTS
        and all(isinstance(value, str) for value in body["inputs"].values())
    ):
        session = body["inputs"]["session"]
        if isinstance(session, str) and DISPATCH.fullmatch(session) and body["ref"] == RUN_BRANCH_PREFIX + session:
            return session
    return None


def ready_pull(body):
    """The PR ID of the exact draft-to-ready mutation, or None."""
    if not isinstance(body, dict) or set(body) != {"query", "variables"} or body["query"] != MARK_READY:
        return None
    variables = body["variables"]
    value = variables.get("input") if isinstance(variables, dict) and set(variables) == {"input"} else None
    if not isinstance(value, dict) or set(value) != {"pullRequestId"}:
        return None
    return value["pullRequestId"] if isinstance(value["pullRequestId"], str) and value["pullRequestId"] else None


def resolved_thread(body):
    """The thread ID of an exact engine `resolveReviewThread` request, or None for anything else."""
    if not isinstance(body, dict) or set(body) != {"query", "variables"} or body["query"] != RESOLVE_THREAD:
        return None
    variables = body["variables"]
    thread = variables.get("input") if isinstance(variables, dict) and set(variables) == {"input"} else None
    if not isinstance(thread, dict) or set(thread) != {"threadId"} or not isinstance(thread["threadId"], str):
        return None
    return thread["threadId"] or None


def _create_ref_body(body, branch):
    """Admit an exact task branch creation with a Git object ID."""
    return (
        isinstance(body, dict)
        and set(body) == {"ref", "sha"}
        and body["ref"] == "refs/heads/" + branch
        and isinstance(body["sha"], str)
        and OID.fullmatch(body["sha"]) is not None
    )


def _graphql_write(method, body):
    """Admit only exact engine mutations, checking thread resolution first."""
    return method == "POST" and (
        resolved_thread(body) is not None or ready_pull(body) is not None or review_api.identify(body) is not None
    )
