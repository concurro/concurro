"""Scope provider transcripts to one role and one cooperative loop."""

from policy import Blocked, digest, selected_model
from model_config import resolve


def prepare(state, config, provider, stage, context):
    """Select only cooperative sessions; fresh approval never receives a session."""
    if stage not in ("plan", "plan_qc", "develop") or context.get("review_phase") == "fresh":
        return None
    model = context.get("reviewer_identity", {}).get("model") or selected_model(config, provider, stage)
    scope = digest([config["repository"], state["issue"], provider, model, stage,
                    state["base"], state["requirements"], state["bundle_id"],
                    state["config_digest"], state.get("design_reopens", 0),
                    state.get("plan_id") if stage == "develop" else None])
    role = role_key(config, provider, stage, model)
    if "agents" in config:
        identity = resolve(config, provider, stage).record()
        scope = digest([scope, identity])
    sessions = state.setdefault("sessions", {})
    current = sessions.get(role, {})
    if current.get("binding", current.get("scope")) != scope:
        sessions[role] = {"binding": scope, "scope": scope, "turn": 0, "restarts": 0}
    return {**sessions[role], "role": role}


def restart(state, session):
    """Rotate only the failed role while retaining its consumed task budget."""
    current = state["sessions"][session["role"]]
    if current["scope"] != session["scope"]:
        raise Blocked("session identity changed during recovery")
    binding = current.get("binding", current["scope"])
    count = current.get("restarts", 0) + 1
    state["sessions"][session["role"]] = {
        "binding": binding, "scope": digest([binding, count]), "turn": 0, "restarts": count,
        "previous": {"scope": current["scope"], "turn": current["turn"]}}


def role_key(config, provider, stage, model):
    """The same transcript key for recording turns and inspecting their first-pass count."""
    key = stage + ":" + provider + ":" + model
    if "agents" in config:
        key += ":" + digest(resolve(config, provider, stage).record())[:16]
    return key
