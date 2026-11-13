"""Frozen nonsecret payer identity shared by serial and batch attempt boundaries."""

import re
import uuid
from copy import deepcopy

from credential_routes import ROUTES
from credentials import Credential
from policy import Blocked

FIELDS = {"funding", "credential", "sponsorship_provider", "sponsor", "epoch", "grant_id", "route", "grant"}


def mode(config):
    """Return the explicit task funding policy; legacy configurations retain organization funding."""
    value = config.get("funding_mode", "org")
    if value not in ("org", "contributed"):
        raise Blocked("unknown inference funding policy")
    return value


def selection(credential, provider=None, slot=None):
    """Snapshot one selected provider grant without copying the whole task's slot map."""
    slot = slot or {}
    return {
        "funding": credential.kind,
        "credential": credential.record(),
        "sponsorship_provider": provider,
        "sponsor": credential.account if credential.kind == "contributed" else None,
        "epoch": slot.get("epoch"),
        "grant_id": slot.get("grant_id"),
        "route": credential.route,
        "grant": deepcopy(slot) if credential.kind == "contributed" else None,
    }


def validate(value):
    """Validate the exact selection and its route/provider/owner identity without resolving material."""
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise Blocked("invalid admission selection")
    credential = Credential.parse(value["credential"])
    if value["funding"] != credential.kind or value["route"] != credential.route:
        raise Blocked("admission credential differs from its funding policy")
    if credential.kind == "org":
        if any(
            value[key] is not None for key in ("sponsorship_provider", "sponsor", "epoch", "grant_id", "route", "grant")
        ):
            raise Blocked("organization admission cannot name a personal payer")
    elif (
        credential.route not in ROUTES
        or ROUTES[credential.route]["sponsorship_provider"] != value["sponsorship_provider"]
        or value["sponsor"] != credential.account
        or type(value["epoch"]) is not int
        or value["epoch"] < 1
        or not isinstance(value["grant_id"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["grant_id"])
        or not isinstance(value["grant"], dict)
        or any(value["grant"].get(key) != value[key] for key in ("epoch", "grant_id"))
        or value["grant"].get("sponsor_id") != value["sponsor"]
        or value["grant"].get("status") != "active"
    ):
        raise Blocked("invalid provider grant for admission")
    return credential


def freeze(value, call, generation, session=None):
    """Bind the selected payer and transcript to one already-identified attempt's charge."""
    credential = validate(value)
    if type(call) is not int or call < 1:
        raise Blocked("invalid admission call number")
    if credential.kind == "contributed" and (type(generation) is not int or generation < 1):
        raise Blocked("contributed admission requires current task ownership")
    return {
        "version": 1,
        **deepcopy(value),
        "call": call,
        "owner_generation": generation,
        "transcript_scope": session.get("scope") if session else None,
    }


def turn_id(attempt):
    """Normalize a persisted attempt UUID only at transport, without changing checkpoint identity."""
    try:
        if not isinstance(attempt, str):
            raise ValueError()
        return str(uuid.UUID(attempt))
    except ValueError:
        raise Blocked("invalid persisted attempt identity") from None
