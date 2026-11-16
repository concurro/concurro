"""Task-scoped credential failures and the nonsecret recovery plan they pause on.

Ownership, consent and live admission remain authoritative: a plan only says when the paused
call is worth selecting again.
"""

from copy import deepcopy

import admissions
from policy import digest

from .failures import backoff

RECOVERABLE_STAGES = frozenset(("intake", "plan", "plan_qc", "implement", "publish", "review", "gates"))
# Failures that end without a deadline unless the provider supplies one, and those that only cool down.
DEFINITE = ("auth", "model_denied", "quota_exhausted")
TEMPORARY = ("rate_limited", "unavailable")
FAILURES = DEFINITE + TEMPORARY
VERSION = 2


def identity(observation):
    """The exact sponsor, record and version a trusted failure names, or None when any part is missing."""
    binding = observation.get("binding") or {}
    sponsor, record, version = binding.get("sponsor"), binding.get("credential"), binding.get("credential_version")
    if isinstance(sponsor, str) and isinstance(record, str) and type(version) is int and version >= 1:
        return sponsor, record, version
    return None


def exclude(state, observation, now):
    """Record one trusted failure against its exact record version; a repeated turn changes nothing.

    Failures of one record merge: one without a deadline outlasts any cooldown, and cooldowns keep
    the latest deadline.

    A model-access denial excludes the record for that model only. Temporary failures cool down until
    the provider's own deadline, or a local backoff that doubles per consecutive failure of the record.
    """
    category, named = observation.get("category"), identity(observation)
    if category not in FAILURES or not named:
        return None
    sponsor, record, version = named
    model = observation.get("model") if category == "model_denied" else None
    key = ":".join(str(part) for part in (sponsor, record, version, *((model,) if model else ())))
    item = state.setdefault("credential_exclusions", {}).setdefault(
        key,
        {
            "sponsor": sponsor,
            "record_id": record,
            "version": version,
            "route": (observation.get("binding") or {}).get("route"),
            "model": model,
            "turns": [],
            "failures": 0,
        },
    )
    if observation.get("turn") in item["turns"]:
        return item
    item["turns"].append(observation.get("turn"))
    if item.get("category") in DEFINITE and category in TEMPORARY:
        return item
    if category in DEFINITE:
        item.update(category=category, until=None, source=None)
        return item
    hint = observation.get("retry_after")
    provided = type(hint) is int and 0 <= hint <= 999999999
    until = now + (hint if provided else backoff(item["failures"]))
    item["failures"] += 1
    # Failures of one record on concurrent turns keep its latest deadline.
    if until >= (item.get("until") or 0):
        item.update(category=category, until=until, source="provider" if provided else "local")
    return item


def excluded(state, record, model, now):
    """The exclusion that keeps a listed record from this task's selection for `model` now, if any."""
    identity = record.get("user_id"), record.get("record_id"), record.get("version")
    for item in state.get("credential_exclusions", {}).values():
        if (
            (item["sponsor"], item["record_id"], item["version"]) == identity
            and item.get("model") in (None, model)
            and (item.get("until") is None or now < item["until"])
        ):
            return item
    return None


def succeeded(state, credential):
    """A version that completed a call starts its local backoff count afresh."""
    exclusions = state.get("credential_exclusions", {})
    for key, item in list(exclusions.items()):
        if item.get("category") in TEMPORARY and (item["sponsor"], item["record_id"], item["version"]) == (
            credential.get("account"),
            credential.get("record_id"),
            credential.get("version"),
        ):
            del exclusions[key]


def adopt(state, previous):
    """Carry a route-era plan's failed versions and retry deadline onto their exact records."""
    for failure in previous.get("failures", []):
        item = exclude(state, failure, 0)
        if item and item.get("source") and "not_before" in previous:
            item.update(until=previous["not_before"], source="provider")


def intent(state, config, now, selection, observations=None):
    """Describe the paused call's next selection without granting consent or resetting any allowance.

    `selection` names the call's requirement and sponsor with the candidate found for it, the time a
    cooling record is due, or the time to look again when the list could not be read.
    """
    if (
        "contributed" not in admissions.provider_modes(config, state).values()
        or state.get("stage") != "awaiting_credentials"
    ):
        return None
    previous = state.get("credential_recovery") or {}
    failures = deepcopy(observations if observations is not None else previous.get("failures", []))
    if observations is None and "failures" not in previous and state.get("provider_failure"):
        failures = [deepcopy(state["provider_failure"])]
    exclusions = sorted(
        (key, item.get("until"), len(item["turns"])) for key, item in state.get("credential_exclusions", {}).items()
    )
    identifier = digest([state.get("calls"), state.get("retry_stage"), selection["requirement"], exclusions, failures])
    same = previous.get("id") == identifier
    return {
        "version": VERSION,
        "id": identifier,
        "requirement": deepcopy(selection["requirement"]),
        "owners": [selection["sponsor"]] if selection.get("sponsor") else [],
        "failures": failures,
        "failure_id": digest(failures),
        "eligible": state.get("retry_stage") in RECOVERABLE_STAGES
        and state.get("calls", 0) < config["max_model_calls"]
        and bool(selection.get("sponsor"))
        and all(failure.get("category") in FAILURES and identity(failure) for failure in failures),
        **{key: deepcopy(selection.get(key)) for key in ("candidate", "wake_at")},
        # One later look stays scheduled until it is due, however often the plan is recomputed.
        "recheck_at": previous["recheck_at"]
        if same and (previous.get("recheck_at") or 0) > now
        else selection.get("recheck_at"),
        "consumed": previous.get("consumed", []) if same else [],
        **({"status": previous["status"]} if same and "status" in previous else {}),
    }


def receipt(plan):
    """The next unconsumed reason to select again and when it is due; none while nothing can change.

    A present candidate or a list change seen since the plan was made is due at once; a cooling
    record or an unreadable list is due at its recorded time.
    """
    if not plan or plan.get("version") != VERSION or not plan.get("eligible"):
        return None
    reasons = (
        ("candidate", plan.get("candidate"), 0),
        ("observed", plan.get("observed"), 0),
        ("wake", plan.get("wake_at"), plan.get("wake_at")),
        ("recheck", plan.get("recheck_at"), plan.get("recheck_at")),
    )
    for name, basis, due in reasons:
        identifier = digest([plan["id"], name, basis])
        if basis is not None and identifier not in plan.get("consumed", []):
            return {"id": identifier, "due_at": due, "reason": name}
    return None
