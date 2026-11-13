"""Nonsecret recovery intents; task ownership and live admission remain authoritative."""

from copy import deepcopy

import admissions
import sponsorship
from policy import digest

RECOVERABLE_STAGES = frozenset(("intake", "plan", "plan_qc", "implement", "publish", "review", "gates"))


def intent(state, config, now, observations=None):
    """Describe one paused attempt without granting consent or resetting any attempt allowance."""
    if admissions.mode(config) != "contributed" or state.get("stage") != "awaiting_credentials":
        return None
    previous = state.get("credential_recovery") or {}
    failures = deepcopy(observations if observations is not None else previous.get("failures", []))
    if observations is None and "failures" not in previous and state.get("provider_failure"):
        failures = [deepcopy(state["provider_failure"])]
    requirements = {}
    eligible = state.get("retry_stage") in RECOVERABLE_STAGES and state.get("calls", 0) < config["max_model_calls"]
    waits = []
    for requirement in sponsorship.selected_routes(config):
        provider, route = requirement["provider"], requirement["route"]
        slot = state.get("sponsorships", {}).get(provider, {})
        actor = slot.get("sponsor_id")
        if not actor or route not in sponsorship.ROUTES or slot.get("status") != "active":
            eligible = False
        key = provider, route, actor
        item = requirements.setdefault(
            key,
            {
                "provider": provider,
                "route": route,
                "sponsor": actor,
                "epoch": slot.get("epoch"),
                "grant": slot.get("grant_id"),
                "minimum_version": 0,
                "models": [],
                "roles": [],
            },
        )
        for field, value in (("models", requirement["model"]), ("roles", requirement["role"])):
            if value not in item[field]:
                item[field].append(value)
    for failure in failures:
        category, binding = failure.get("category"), failure.get("binding", {})
        if (
            binding.get("route") not in sponsorship.ROUTES
            or sponsorship.ROUTES.get(binding.get("route"), {}).get("sponsorship_provider")
            != binding.get("sponsorship_provider")
            or not isinstance(binding.get("sponsor"), str)
            or not binding["sponsor"].isdigit()
            or type(binding.get("credential_version")) is not int
            or binding["credential_version"] < 1
        ):
            eligible = False
        if category not in ("auth", "quota_exhausted", "rate_limited"):
            eligible = False
        if category == "rate_limited":
            hint = failure.get("retry_after")
            if type(hint) is not int or not 0 <= hint <= 999999999:
                eligible = False
            else:
                waits.append(hint)
        for item in requirements.values():
            if (item["sponsor"], item["route"]) == (binding.get("sponsor"), binding.get("route")) and category in (
                "auth",
                "quota_exhausted",
            ):
                version = binding.get("credential_version")
                if type(version) is not int or version < 1:
                    eligible = False
                else:
                    item["minimum_version"] = max(item["minimum_version"], version)
    rows = list(requirements.values())
    identifier = digest([state.get("calls"), state.get("retry_stage"), rows, failures])
    failure_id = digest(failures)
    same = previous.get("id") == identifier
    return {
        "id": identifier,
        "requirements": rows,
        "failures": failures,
        "failure_id": failure_id,
        "eligible": eligible and bool(rows),
        "not_before": previous["not_before"]
        if previous.get("failure_id") == failure_id
        else now + max(waits, default=0),
        "consumed": previous.get("consumed") if same else None,
        **({"status": previous["status"]} if same and "status" in previous else {}),
    }


def receipt(plan, versions):
    """Require usable validated versions for every current slot; an old failed version never suffices."""
    if not plan or not plan.get("eligible"):
        return None
    selected = []
    for requirement in plan["requirements"]:
        value = versions.get((requirement["sponsor"], requirement["route"]))
        if (
            not value
            or value.get("status") != "valid"
            or not value.get("validation_sequence")
            or value.get("version", 0) <= requirement["minimum_version"]
        ):
            return None
        selected.append((requirement["sponsor"], requirement["route"], value["validation_sequence"]))
    identifier = digest([plan["id"], selected])
    return None if identifier == plan.get("consumed") else {"id": identifier, "due_at": plan["not_before"]}
