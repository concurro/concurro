"""Writer-directed recovery of a description repair mistakenly treated as a design change."""

from copy import deepcopy

import bundle_resume
from checkpoints import Checkpoints
from policy import Blocked, approval_valid, digest

# Restore only the approval's original inputs and evidence, never the task checkpoint wholesale.
APPROVAL_FIELDS = ("approval", "design_votes", "design_generation", "design_phase", "cooperative_votes")
SAME_INPUTS = (
    "issue",
    "base",
    "head",
    "pr",
    "plan_id",
    "plan",
    "requirements",
    "config_digest",
    "bundle_id",
    "charter_id",
    "design_parent",
    "approved_accumulated_files",
    "candidate",
)


def previous_approval(controller):
    """Find the bot-written pre-reopen approval for this exact plan and candidate."""
    state, checkpoints = controller.state, controller.checkpoints
    reopen = state.get("reopen_history", [])
    history = state.get("design_history", [])
    if (
        not reopen
        or not history
        or reopen[-1].get("source") != "implementation"
        or reopen[-1].get("plan") != state.get("plan_id")
        or reopen[-1].get("head") != state.get("head")
        or history[-1].get("generation", -1) >= state.get("design_generation", 0)
        or history[-1].get("candidate_digest") != digest(state.get("candidate"))
    ):
        raise Blocked("No unchanged implementation reopen is available for description-only recovery.")
    # Separate readers prevent historical plan bindings, dashboards and report pins replacing current receipts.
    reader = Checkpoints(checkpoints.gh, checkpoints.config, checkpoints.issue, reader=checkpoints.reader)
    for version in checkpoints.history(checkpoints.audit):
        if version.get("actor", {}).get("login") != checkpoints.author:
            continue
        prior = reader.decode({**checkpoints.audit, "body": version["body"]})
        if prior.get("design_generation") != history[-1]["generation"]:
            continue
        if (
            prior.get("stage") == "implement"
            and all(prior.get(key) == state.get(key) for key in SAME_INPUTS)
            and approval_valid(prior, controller.config)
        ):
            return version, prior
    raise Blocked("The original approval is unavailable for this plan and head; no approval was reconstructed.")


def recover(controller, comment):
    """Undo only a writer-identified publication-only reopen, retaining all spent budgets and history."""
    state = controller.state
    if state.get("pending") or state.get("description_update"):
        raise Blocked("Finish the pending publication before description-only recovery.")
    if not state.get("pr") or state.get("issue_digest") != controller.issue_digest():
        raise Blocked("Description-only recovery requires the existing PR and unchanged requirements.")
    controller.require_pr_unchanged("Description-only recovery requires the existing open PR head.")
    if approval_valid(state, controller.config):
        return
    version, prior = previous_approval(controller)
    before = {f["id"]: f for f in prior.get("findings", [])}
    current = {f["id"]: f for f in state.get("findings", [])}
    if (
        set(before) != set(current)
        or any(
            {k: v for k, v in current[i].items() if k != "status"}
            != {k: v for k, v in before[i].items() if k != "status"}
            or current[i]["status"] not in (before[i]["status"], "withdrawn")
            for i in before
        )
        or state.get("reviews") != prior.get("reviews")
    ):
        raise Blocked("Review evidence changed after the reopen; description-only recovery was not applied.")
    receipt = {
        "actor": comment["user"]["login"],
        "comment": comment["id"],
        "audit_revision": version["version"],
        "audit_digest": digest(version["body"]),
        "reopened_generation": state["design_generation"],
        "binding": prior["approval"]["binding"],
        "head": state["head"],
        "plan": state["plan_id"],
    }
    restored = deepcopy(state)
    for key in APPROVAL_FIELDS:
        if key in prior:
            restored[key] = deepcopy(prior[key])
        else:
            restored.pop(key, None)
    if not approval_valid(restored, controller.config):
        raise Blocked("The original approval does not apply to the current task; recovery was not applied.")
    state.setdefault("resume_history", []).append({"description_recovery": receipt})
    state["design_generation_high"] = max(state.get("design_generation_high", 0), state["design_generation"])
    for key in APPROVAL_FIELDS:
        if key in restored:
            state[key] = restored[key]
        else:
            state.pop(key, None)
    for identifier, finding in current.items():
        finding["status"] = before[identifier]["status"]
    # A posted withdrawal stays in GitHub's history; an unsent one must not follow the recovery notice.
    state["pr_outbox"] = [
        item
        for item in state.get("pr_outbox", [])
        if not (item.get("kind") == "withdrawn" and item.get("generation") == receipt["reopened_generation"])
    ]
    for key in ("operation", "design_feedback", "revision_wait", "implementation_feedback"):
        bundle_resume.retire(state, key, "Writer recovered a description-only repair")
    state.update(stage="implement", input_revision=state.get("input_revision", 0) + 1)
    state.pop("retry_stage", None)
    state.pop("reason", None)
