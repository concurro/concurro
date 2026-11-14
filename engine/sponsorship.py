"""Provider grants and immutable explicit-first source ordering."""

from copy import deepcopy
from datetime import datetime

import delegation
from commands import CommandKind, parse
from credential_routes import ROUTES
from model_config import assigned, resolve
from policy import design_roster, digest

PROVIDERS = ("openai", "anthropic")

ROLES = {
    "triage": "intake",
    "planner": "plan",
    "plan_reviewers": "plan_qc",
    "implementer": "develop",
    "pr_reviewers": "review",
}


def credential_route(config, profile, stage, identity=None):
    """Use a profile override before its provider's default credential route."""
    provider = resolve(config, profile, stage, identity).profile.provider
    defaults = config.get("provider_credential_routes", {})
    overrides = config.get("credential_routes", {})
    if (
        not isinstance(defaults, dict)
        or not isinstance(overrides, dict)
        or any(
            p not in PROVIDERS or r not in ROUTES or ROUTES[r]["sponsorship_provider"] != p for p, r in defaults.items()
        )
        or any(not isinstance(name, str) or route not in ROUTES for name, route in overrides.items())
    ):
        raise ValueError("invalid provider credential routes")
    route = overrides.get(profile, defaults.get(provider))
    if route and ROUTES[route]["sponsorship_provider"] != provider:
        raise ValueError("credential route does not support this profile")
    return route


def selected_routes(config):
    """Resolve only selected roles through explicit profile-to-route settings and the trusted catalog."""
    result = []
    for role, stage in ROLES.items():
        if role == "plan_reviewers":
            members = design_roster(config)
        else:
            names = assigned(config, role)
            members = [{"provider": name} for name in (names if isinstance(names, (tuple, list)) else [names])]
        for member in members:
            name = member["provider"]
            agent = resolve(config, name, stage, member)
            route = credential_route(config, name, stage, member)
            result.append(
                {
                    "role": role,
                    "profile": name,
                    "model": agent.profile.model,
                    "route": route,
                    "provider": agent.profile.provider,
                }
            )
    return result


def hint(comment, action="created", previous=None, editor=None):
    """Keep immutable command revision evidence, including removals, without trusting delivery order."""
    body = comment.get("body") or ""
    command = parse(body)
    keep = (
        command
        and command.kind in (CommandKind.SPONSOR, CommandKind.FUNDING, CommandKind.WITHDRAW, CommandKind.START)
        or delegation.sponsorship_hint(body)
    )
    prior = parse(previous or "")
    keep_prior = (
        prior
        and prior.kind in (CommandKind.SPONSOR, CommandKind.FUNDING, CommandKind.WITHDRAW, CommandKind.START)
        or delegation.sponsorship_hint(previous or "")
    )
    user = comment.get("user") or {}
    value = {
        "comment_id": comment.get("id"),
        "actor_id": user.get("id"),
        "editor_id": editor if type(editor) is int and editor > 0 else None,
        "login": user.get("login"),
        "body": body if keep else "",
        "body_digest": digest(body),
        "created_at": comment.get("created_at"),
        "updated_at": comment.get("updated_at"),
        "action": action,
        "previous": previous if keep_prior else None,
        "previous_digest": digest(previous) if previous is not None else None,
    }
    return {**value, "revision": digest(value)}


def fact(comment, authorized):
    """Normalize a verified effective command and stable actor into immutable consent evidence."""
    command = parse(comment.get("body") or "")
    funding = None
    provider = command.argument if command else None
    if command and command.kind == CommandKind.FUNDING:
        provider, funding = command.argument.split()
        funding = "contributed" if funding == "personal" else "org"
    user = comment.get("user") or {}
    if (
        command is None
        or command.kind not in (CommandKind.SPONSOR, CommandKind.FUNDING, CommandKind.WITHDRAW)
        or provider not in PROVIDERS
    ):
        return None
    if type(user.get("id")) is not int or user["id"] < 1 or not user.get("login"):
        raise ValueError("sponsorship actor identity unavailable")
    if command.kind in (CommandKind.SPONSOR, CommandKind.FUNDING) and not authorized(user["id"], user["login"]):
        return None
    stamp = comment.get("updated_at") or comment.get("created_at")
    if not isinstance(stamp, str):
        raise ValueError("sponsorship source timestamp unavailable")
    datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    value = {
        "source_id": comment["id"],
        "actor_id": str(user["id"]),
        "login": user["login"],
        "provider": provider,
        "command": command.kind.value,
        "source_order": [stamp, comment["id"], 0],
        "provenance": "explicit_comment",
        "source_funding_mode": "org",
        "body_digest": digest(comment["body"]),
    }
    if funding is not None:
        value["funding"] = funding
    return {**value, "grant_id": digest(value)}


def project(facts):
    """Fold ordered consent and payer choices independently; withdrawal never restores a payer."""
    slots = {p: {"status": "unassigned", "sponsor_id": None} for p in PROVIDERS}
    tombstones = {p: {} for p in PROVIDERS}
    funding = {}
    for item in sorted(facts, key=lambda f: f["source_order"]):
        provider, actor = item["provider"], item["actor_id"]
        slot = slots[provider]
        if item["command"] == "funding":
            funding[provider] = item["funding"]
            slot["funding_order"] = item["source_order"]
            if item["funding"] == "org":
                continue
        if item["command"] == "sponsor" or item["command"] == "funding" and item["funding"] == "contributed":
            slots[provider] = {**deepcopy(item), "sponsor_id": actor, "status": "active"}
            tombstones[provider].pop(actor, None)
        else:
            tombstones[provider][actor] = item["grant_id"]
            if slot.get("sponsor_id") == actor and (
                not item.get("removed_source") or slot.get("source_id") == item["removed_source"]
            ):
                slots[provider] = {"sponsor_id": None, "status": "withdrawn", "grant_id": item["grant_id"]}
        if "funding_order" in slot:
            slots[provider]["funding_order"] = slot["funding_order"]
    return {"slots": slots, "tombstones": tombstones, "provider_funding": funding}


def initialize(projection, boundary, routes, metadata):
    """Freeze explicit-first slots at the source boundary, then apply observed later revisions."""
    before = [f for f in projection["facts"] if f["source_order"][0] <= boundary["at"]]
    initial = project(before)
    slots = initial["slots"]
    evidence = []
    actor = boundary["actor_id"]
    for provider in PROVIDERS:
        matching = [r for r in routes if r["provider"] == provider]
        available = [
            r
            for r in matching
            if any(
                m.get("route") == r["route"] and m.get("status") == "valid" and r["model"] in (m.get("models") or [])
                for m in metadata
            )
        ]
        if not slots[provider].get("sponsor_id") and actor not in initial["tombstones"][provider] and available:
            value = {
                "sponsor_id": actor,
                "actor_id": actor,
                "login": boundary["login"],
                "provider": provider,
                "status": "active",
                "provenance": boundary["provenance"],
                "source_id": boundary["source_id"],
                "source_order": [boundary["at"], boundary["source_id"], -1],
                "source_funding_mode": boundary.get("source_policy", {}).get("funding_mode", "unknown"),
                "source_policy": deepcopy(boundary.get("source_policy")),
            }
            slots[provider] = {**value, "grant_id": digest([value, available, metadata])}
            evidence.extend(available)
    return {
        "slots": slots,
        "provider_funding": initial["provider_funding"],
        "tombstones": initial["tombstones"],
        "boundary": deepcopy(boundary),
        "projection_revision": projection["revision"],
        "projection_digest": projection["digest"],
        "routes": deepcopy(routes),
        "available": evidence,
        "metadata": [
            {
                **deepcopy(record),
                "models": sorted(
                    set(record.get("models") or [])
                    & {item["model"] for item in evidence if item["route"] == record["route"]}
                ),
            }
            for record in metadata
            if record["route"] in {item["route"] for item in evidence}
        ],
        "applied": [f["grant_id"] for f in before],
        "source_funding_mode": boundary.get("source_policy", {}).get("funding_mode", "unknown"),
    }


def attribution(state, config, profile, stage):
    """Snapshot prospective attribution separately from the org credential and actual payment binding."""
    route = credential_route(config, profile, stage)
    provider = resolve(config, profile, stage).profile.provider
    slot = state.get("sponsorships", {}).get(provider, {})
    return {
        "funding": state.get("provider_funding", {}).get(provider, config.get("funding_mode", "org")),
        "status": state.get("sponsorship_status", "pending"),
        "route": route,
        "sponsorship_provider": provider,
        "prospective_sponsor": slot.get("sponsor_id"),
        "epoch": slot.get("epoch"),
        "grant_id": slot.get("grant_id"),
        "projection_revision": state.get("funding_initialization", {}).get("projection_revision"),
    }


def coverage(routes, slots, metadata):
    """Project selected-route/model coverage without changing a payer, model or effort."""
    result = {provider: [] for provider in PROVIDERS}
    for route in routes:
        provider = route["provider"]
        if provider is None:
            continue
        actor = slots.get(provider, {}).get("sponsor_id")
        records = metadata.get(actor)
        record = next((m for m in records or [] if m["route"] == route["route"]), None)
        if actor is None:
            status = "unassigned"
        elif records is None:
            status = "unknown"
        elif record is None:
            status = "missing"
        elif record.get("status") != "valid":
            status = "reconnect"
        elif record.get("models") is None:
            status = "unknown"
        else:
            status = "available" if route["model"] in record["models"] else "missing_model"
        result[provider].append({**route, "status": status, "version": record.get("version") if record else None})
    return result


def own_consent(projection: dict, actor: str) -> dict:
    """Expose only this actor's consent evidence; omit task state, other owners and accounting."""
    result = {"scope": "own", "status": projection.get("status", "unavailable"), "slots": {}}
    for provider in ("openai", "anthropic"):
        slot = (projection.get("task", {}).get("slots") or projection.get("slots") or {}).get(provider) or {}
        result["slots"][provider] = (
            deepcopy(slot) if slot.get("sponsor_id") == actor else {"status": "not shown", "sponsor_id": None}
        )
    result["facts"] = [deepcopy(f) for f in projection.get("facts", []) if f.get("actor_id") == actor]
    result["tombstones"] = {
        p: {actor: rows[actor]} for p, rows in (projection.get("tombstones") or {}).items() if actor in rows
    }
    result["history"] = []
    history = projection.get("history", projection.get("task", {}).get("history", []))
    initialized = {
        (row.get("provider"), row["slot"]["grant_id"])
        for row in history
        if row.get("reason") == "initialize"
        and row.get("actor") in (None, actor)
        and row.get("slot", {}).get("sponsor_id") == actor
        and row["slot"].get("grant_id")
    }
    for row in history:
        retired = row.get("cutover_from") or row.get("slot", {}).get("cutover_from")
        if (
            row.get("actor") is None
            and row.get("reason") == "cutover"
            and (row.get("provider"), retired) in initialized
        ):
            result["history"].append(
                {
                    **{k: deepcopy(row[k]) for k in ("transition", "provider", "reason") if k in row},
                    "cutover_from": retired,
                }
            )
            continue
        if row.get("actor") != actor and not (
            row.get("actor") is None and row.get("slot", {}).get("sponsor_id") == actor
        ):
            continue
        receipt = {
            k: deepcopy(row[k])
            for k in ("transition", "provider", "reason", "actor", "cutoff", "expected_epoch")
            if k in row
        }
        if row.get("slot", {}).get("sponsor_id") == actor:
            receipt["slot"] = deepcopy(row["slot"])
        result["history"].append(receipt)
    return result


def own_consent_present(view: dict) -> bool:
    """Select own history for discovery; this is never a prerequisite for withdrawal."""
    return any(view.get(k) for k in ("facts", "history", "tombstones")) or any(
        slot.get("sponsor_id") for slot in view.get("slots", {}).values()
    )
