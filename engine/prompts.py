"""One prompt representation for controller admission and native runner execution."""

import json
import posixpath

import runner_layout
from vocab import CONTRIBUTION, STANDALONE_REVIEW


def serialize(value):
    """Encode model-facing JSON compactly without escaping Unicode text."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def prompt_for(request, bundle):
    """Keep controller-authored implementation authorization apart from untrusted task evidence."""
    context = {k: v for k, v in request["context"].items() if not k.startswith("controller_")}
    authorization = context.pop("design_authorization", None)
    # The controller binds the kind outside the context; a request without one is a contribution.
    kind = request.get("task_kind", CONTRIBUTION)
    context.pop("charter", None)
    repository = context.pop("repository")
    if request["stage"] == "plan":
        contract = context.get("plan_output_contract", {})
        for key in ("edit_policy", "limits"):
            if key in contract and context.get(key) == contract[key]:
                context.pop(key, None)
    if kind == STANDALONE_REVIEW and "published_pr_body" in context:
        issue = context.get("issue", {})
        if issue.get("body") == context["published_pr_body"]:
            context["issue"] = {k: v for k, v in issue.items() if k != "body"}
    prompt = {
        "instructions": bundle.instructions(request["stage"], kind),
        "repository_snapshot": {
            "workspace": ".",
            "commit": context.pop("repository_commit"),
            "refs": repository["refs"],
            "head": repository["head"],
            "local_checkpoint": context.pop("local_commit", None),
            "head_is_local_candidate": bool(request.get("local_candidate")),
        },
        "untrusted_context": context,
    }
    if request.get("feedback_directory"):
        prompt["feedback_files"] = {
            "directory": posixpath.relpath(runner_layout.FEEDBACK, runner_layout.WORKSPACE),
            "instruction": bundle.controller_instruction("feedback_files"),
        }
    if authorization is not None:
        if request["stage"] != "develop":
            raise ValueError("implementation authorization cannot enter an independent review")
        prompt["controller_verified_authorization"] = authorization
    return serialize(prompt)


def size(stage, context, bundle, kind=CONTRIBUTION):
    """Count the full prompt plus the native schema, including during candidate preflight."""
    prepared = dict(context)
    prepared.setdefault("repository_commit", context.get("head", context.get("base", "")))
    prepared.setdefault("repository", {"refs": {}, "head": ""})
    request = {
        "stage": stage,
        "task_kind": kind,
        "context": prepared,
        "local_candidate": "controller_candidate" in context,
        "feedback_directory": bool(context.get("controller_feedback")),
    }
    return len(prompt_for(request, bundle).encode()) + len(serialize(bundle.schema(stage, kind)).encode())
