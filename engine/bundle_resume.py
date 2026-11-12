"""Writer-directed adoption of current execution settings, retaining prior work."""

from copy import deepcopy

import design_eligibility
import revisions
import sessions
from bundle import SKILLS
from model_config import assigned, resolve
from policy import bound_design_roster, consensus_valid, design_roster, digest

FORMAT = 6
MODEL_STAGES = {"intake": "intake", "plan": "plan", "plan_qc": "plan_qc", "implement": "develop", "review": "review"}


def accepted(state, bundle):
    """Use the bundle explicitly adopted by the latest writer resume."""
    return state.get("bundle_resume", {}).get("new_bundle") == bundle.identity


def config_current(state, config_digest):
    """Compare execution settings separately from the original approval binding."""
    return state.get("bundle_resume", {}).get("config_digest", state.get("config_digest")) == config_digest


def retire(state, field, reason):
    """Keep superseded in-progress data in the detached audit history, never as a new approval."""
    value = state.pop(field, None)
    if value:
        state.setdefault("resume_history", []).append({"field": field, "reason": reason, "value": value})


def pr_team(config):
    """Include model and effort changes behind an unchanged profile name."""
    return [resolve(config, name, "review").record() for name in assigned(config, "pr_reviewers")]


def orphaned_reviews(state, previous, config):
    """List actionable records whose actual owner cannot review under the proposed team."""
    pr_owners = set(assigned(config, "pr_reviewers"))
    design_owners = {member["id"] for member in design_roster(config)}
    previous_names = {member["id"]: member["provider"] for member in design_roster(previous)}
    records = [
        (finding["id"], finding["provider"])
        for finding in state.get("findings", [])
        if finding["status"] in ("open", "withdrawn") and finding["provider"] not in pr_owners
    ]
    records.extend(
        (objection["id"], previous_names.get(objection["reviewer"], objection["reviewer"]))
        for objection in state.get("objections", [])
        if not objection["resolved"] and objection["reviewer"] not in design_owners
    )
    return records


def adopt(controller, comment, config, bundle, config_digest, *, reassess=False):
    """Record writer intent and continue the saved stage with today's settings."""
    state, previous = controller.state, controller.config
    receipt = state.get("bundle_resume", {})
    roster = bound_design_roster(state, previous) if state.get("plan_id") else []
    complete = bool(state.get("plan_id") and consensus_valid(state, previous))
    stage = (
        state.get("retry_stage", state["stage"])
        if not reassess and state["stage"] in ("blocked", "stopped", "rejected", "escalated")
        else state["stage"]
    )
    identity = digest([bundle.identity, config_digest])
    changed = identity != receipt.get(
        "session_identity",
        digest([state.get("implementation_bundle", {}).get("new_bundle", state["bundle_id"]), state["config_digest"]]),
    )
    if receipt:
        state.setdefault("resume_history", []).append({"receipt": deepcopy(receipt)})
    state["bundle_resume"] = {
        "actor": comment["user"]["login"],
        "comment": comment["id"],
        "old_bundle": receipt.get("new_bundle", state["bundle_id"]),
        "new_bundle": bundle.identity,
        "old_config_digest": receipt.get("config_digest", state["config_digest"]),
        "config_digest": config_digest,
        "old_config_sha": state.get("config_sha"),
        "config_sha": config.get("config_sha"),
        "charter_id": digest(bundle.charter),
        "session_identity": identity,
        "plan": state.get("plan_id"),
        "generation": state.get("design_generation"),
        "phase": state.get("design_phase"),
        "roles": list(SKILLS) if changed else receipt.get("roles", []),
        **({"approval_roster": deepcopy(roster)} if complete else {}),
    }
    # A changed unfinished team gets new judgments, while its old data remains in the audit.
    # Completed consensus remains an attestation by its original reviewers.
    changed_charter = state.get("charter_id") != digest(bundle.charter)
    if not complete and (design_roster(previous) != design_roster(config) or changed_charter):
        for field in ("design_votes", "review_batch", "review_repair", "design_eligibility"):
            retire(state, field, "Review context changed on resume")
        state["design_votes"] = {}
        revisions.advance_generation(state)
        if state.get("operation", {}).get("stage") == "plan_qc":
            retire(state, "operation", "Review context changed on resume")
    if not complete and changed_charter:
        state.update(charter=deepcopy(bundle.charter), charter_id=digest(bundle.charter))
    if pr_team(previous) != pr_team(config):
        retire(state, "reviews", "PR review team changed on resume")
        state["reviews"] = {}
        if state.get("operation", {}).get("stage") == "review":
            retire(state, "operation", "PR review team changed on resume")
        if stage == "gates":
            stage = "review"
            state["handoff_ready"] = False
    controller.config, controller.bundle = config, bundle
    if hasattr(controller.agents, "config"):
        controller.agents.config = config
    if hasattr(controller.agents, "bundle"):
        controller.agents.bundle = bundle
    state["execution"] = deepcopy(config["paths"])
    if "config_sha" in config:
        state["config_sha"] = config["config_sha"]
    if changed:
        active = MODEL_STAGES.get(stage)
        done = {
            sessions.role_key(previous, v["identity"]["provider"], "plan_qc", v["identity"]["model"])
            for v in state.get("design_votes", {}).values()
        }
        for role in list(state.get("sessions", {})):
            if role.startswith(str(active) + ":") and role not in done:
                rotate(state, role, identity)
        records = [state, *state.get("review_batch", {}).get("members", {}).values()]
        for record in records:
            operation = record.get("operation", {})
            if operation and operation.get("status") != "consumed":
                operation["resume_rebind"] = bundle.identity
    for operation in [
        state.get("operation", {}),
        *[m.get("operation", {}) for m in state.get("review_batch", {}).get("members", {}).values()],
    ]:
        if operation.get("runner_termination"):
            state.setdefault("resume_history", []).append(
                {"operation": operation["id"], "runner_termination": operation.pop("runner_termination")}
            )
    state.update(stage=stage, command_id=comment["id"])
    if not reassess:
        state.pop("retry_stage", None)
    if stage not in ("clarify", "planned", "rejected", "escalated"):
        state.pop("reason", None)


def rotate(state, role, identity):
    """Start changed unfinished work in a new transcript without regaining first-pass eligibility."""
    current = state["sessions"][role]
    if current.get("execution_bundle") == identity:
        return
    session = {**current, "role": role}
    design_eligibility.restart(state, session)
    sessions.restart(state, session)
    state["sessions"][role]["execution_bundle"] = identity
