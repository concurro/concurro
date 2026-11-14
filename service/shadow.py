"""Issue consent, historical source policy and checkpoint-backed admission selection."""

import json
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from datetime import datetime

import admissions
import conversation
import delegation
import private
import sponsorship
from agents import ADAPTER
from commands import CommandKind, parse
from controller import config_digest
from credentials import Credential
from github import NotFound, Transient
from model_config import resolve
from policy import Blocked, CredentialsUnavailable, ProviderFailure, digest
from relay import ExchangeError

from . import credential_recovery
from .failures import backoff
from .reconciler import pages
from .settings import CHECKOUT


def retryable(error):
    """Whether pre-dispatch shadow or admission I/O can use the existing retry/backoff."""
    return (
        isinstance(error, Transient)
        or isinstance(error, ExchangeError)
        and (error.status in (0, 429) or error.status >= 500)
    )


class Metadata:
    """Read allow-listed vault metadata using a dedicated private service credential."""

    def __init__(self, url, token_path):
        """Keep token bytes outside configuration, checkpoints and exceptions."""
        self.url, self.token_path = url.rstrip("/"), token_path

    def get(self, path):
        """Fetch one private nonsecret response without caching or exposing credential bytes."""
        try:
            token = bytes.fromhex(private.read(self.token_path, (CHECKOUT,)).decode().strip())
            if len(token) != 32:
                raise ValueError()
            token = token.hex()
            request = urllib.request.Request(
                self.url + path,
                headers={"Authorization": "Bearer " + token},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                value = json.load(response)
            return value
        except (Blocked, OSError, ValueError, urllib.error.URLError):
            raise Transient("sponsorship metadata unavailable") from None

    def read(self, actor, at=None):
        """Return allow-listed evidence at the initiation boundary, never provider material."""
        try:
            value = self.get("/metadata/" + str(int(actor)) + ("?" + urllib.parse.urlencode({"at": at}) if at else ""))
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("history_complete"), bool)
                or not isinstance(value.get("credentials"), list)
                or any(
                    not isinstance(record, dict) or record.get("user_id") != str(actor)
                    for record in value.get("credentials", [])
                )
            ):
                raise ValueError()
            keys = {
                "record_id",
                "user_id",
                "route",
                "version",
                "status",
                "models",
                "created",
                "updated",
                "kind",
                "expiry_known",
                "expires_at",
            }
            return [
                {key: record.get(key) for key in keys} for record in value["credentials"] if isinstance(record, dict)
            ]
        except (Blocked, OSError, ValueError, urllib.error.URLError):
            raise Transient("sponsorship metadata unavailable") from None

    def events(self, after):
        """Read strictly typed lifecycle metadata with the existing service principal only."""
        value = self.get("/metadata/events?after=" + str(after))
        try:
            rows, cursor, more = value["events"], value["next"], value["more"]
            if not isinstance(rows, list) or type(cursor) is not int or cursor < after or type(more) is not bool:
                raise ValueError()
            fields = {"sequence", "id", "record_id", "user_id", "route", "version", "type", "status", "reason", "at"}
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or type(row.get("sequence")) is not int
                    or row["sequence"] < 1
                    or row["sequence"] > cursor
                    or type(row.get("version")) is not int
                    or row["version"] < 1
                    or not str(row.get("user_id", "")).isdigit()
                    or row.get("route") not in sponsorship.ROUTES
                    or row.get("status") not in ("valid", "invalid", "revoked", "retired")
                    or row.get("type") not in ("validated", "retired", "revoked", "unusable", "rewrapped", "snapshot")
                ):
                    raise ValueError()
            if cursor != max([after] + [row["sequence"] for row in rows]) or more and cursor == after:
                raise ValueError()
            return {
                "events": [{**{k: row.get(k) for k in fields}, "user_id": str(row["user_id"])} for row in rows],
                "next": cursor,
                "more": more,
            }
        except (ValueError, TypeError, KeyError):
            raise Transient("sponsorship events unavailable") from None


class Shadow:
    """Reuse registry observations and controller persistence for provider grants and admissions."""

    def __init__(self, registry, reader, operator, metadata=None, key=None):
        """Inject existing GitHub reads, operator transitions and optional vault metadata."""
        self.registry, self.reader, self.operator, self.metadata, self.key = registry, reader, operator, metadata, key

    def writer(self, repository, actor, login):
        """Resolve a stable actor and verify current collaborator permission without login-cache reuse."""
        reader = self.reader(repository)
        try:
            user = reader.request("GET", f"user/{actor}")
            if (
                not isinstance(user, dict)
                or str(user.get("id")) != str(actor)
                or not isinstance(user.get("login"), str)
            ):
                raise Transient("sponsorship actor lookup incomplete")
            login = user["login"]
            permission = reader.request(
                "GET", f"repos/{repository}/collaborators/{urllib.parse.quote(login, safe='')}/permission"
            )
        except NotFound:
            return False
        if not isinstance(permission, dict) or permission.get("permission") not in (
            "admin",
            "maintain",
            "write",
            "read",
            "none",
            "triage",
        ):
            raise Transient("sponsorship permission lookup incomplete")
        if isinstance(permission.get("user"), dict) and str(permission["user"].get("id")) != str(actor):
            raise Transient("sponsorship actor lookup incomplete")
        return permission["permission"] in ("admin", "maintain", "write")

    def begin(self, controller):
        """Capture the registry fence before the controller starts its uncached comment read."""
        key = controller.config["repository"], controller.issue["number"]
        controller.shadow_source_revision = self.registry.sponsorship(key)["source_revision"]

    def coverage(self, routes, slots):
        """Read current nonsecret coverage separately from historical initiation availability."""
        metadata = {}
        for actor in {slot.get("sponsor_id") for slot in slots.values()} - {None}:
            try:
                metadata[actor] = self.metadata.read(actor) if self.metadata else None
            except Transient:
                metadata[actor] = None
        return sponsorship.coverage(routes, slots, metadata)

    def view(self, key):
        """Refresh coverage on a read without advancing a task or changing confirmed consent."""
        projection = self.registry.sponsorship(key)
        if projection.get("task") and projection["task"]["slots"]:
            projection["slots"] = projection["task"]["slots"]
            projection["pending"] = projection["task"]["pending"]
        routes = projection.get("routes")
        if routes:
            slots = projection.get("task", {}).get("slots") or projection.get("slots", {})
            projection["coverage"] = self.coverage(routes, slots)
        projection["recovery"] = projection.get("task", {}).get("recovery")
        projection["charged_calls"] = [
            {
                "id": row.get("id"),
                "kind": row.get("kind"),
                "calls": row.get("calls"),
                "profile": row.get("provider"),
                "route": row.get("route"),
                "version": row.get("version"),
                "provider": sponsorship.ROUTES.get(row.get("route"), {}).get("sponsorship_provider")
                or {"codex": "openai", "claude": "anthropic"}.get(row.get("provider")),
                "sponsor": row.get("account") if row.get("kind") == "contributed" else None,
            }
            for row in projection.get("task", {}).get("usage", [])
        ]
        projection["calls"] = projection.get("task", {}).get("calls")
        projection["task_stage"] = projection.get("task", {}).get("stage")
        projection["task_status"] = projection.get("task", {}).get("status")
        if projection.get("task"):
            projection["funding"] = projection["task"]["funding"]
            projection["provider_funding"] = projection["task"].get(
                "provider_funding", {p: projection["funding"] for p in sponsorship.PROVIDERS}
            )
            pending = projection["task"].get("pending_slots", {})
            projection["source_slots"] = {**projection["task"].get("slots", {}), **pending}
            projection["source_provider_funding"] = {
                **projection["provider_funding"],
                **{provider: slot["funding"] for provider, slot in pending.items() if "funding" in slot},
            }
            projection["observations"] = projection["task"].get("observations", [])
        slots = projection.get("task", {}).get("slots") or projection.get("slots", {})
        if projection.get("funding") in ("contributed", "mixed") or any(
            row["kind"] == "contributed" for row in projection["charged_calls"]
        ):
            try:
                reply = self.operator().observations(*key)
                projection["observations"] = reply.get("events", [])
                projection["native_status"] = reply.get("native_status", "unknown")
                projection["observations_status"] = "available"
            except ExchangeError:
                projection["observations"] = None
                projection["observations_status"] = "unknown"
        projection["badges"] = [
            {
                "provider": p,
                "label": ("OpenAI" if p == "openai" else "Anthropic") + " sponsored",
                "status": projection["status"],
                "sponsor_id": slot["sponsor_id"],
                "login": slot.get("login"),
            }
            for p, slot in slots.items()
            if slot.get("sponsor_id")
        ]
        return projection

    def poll_credentials(self):
        """Consume nonsecret pages on the existing observation poll; never resolve a token."""
        if self.metadata is None:
            return
        while True:
            after = self.registry.credential_cursor()
            batch = self.metadata.events(after)
            self.registry.credential_events(batch)
            if not batch["more"]:
                return
            if batch["next"] <= after:
                raise Transient("sponsorship event pagination did not advance")

    def prepare_recovery(self, controller, error=None):
        """Freeze current consent and failure requirements without changing spent work or authority."""
        state = controller.state
        if error is not None and getattr(error, "pause_stage", None) != "awaiting_credentials":
            state.pop("credential_recovery", None)
            return
        view = state
        if error is not None:
            view = {
                **state,
                "stage": "awaiting_credentials",
                "retry_stage": state.get("retry_stage")
                if state["stage"] in ("awaiting_credentials", "blocked", "stopped", "budget_exhausted")
                else state["stage"],
            }
        plan = credential_recovery.intent(
            view,
            controller.config,
            self.registry.clock(),
            getattr(error, "observations", []) if error is not None else None,
        )
        if plan is not None:
            state["credential_recovery"] = plan
        else:
            state.pop("credential_recovery", None)

    def recover(self, controller, comments):
        """Resume only a ready recorded receipt after current authority and whole-roster coverage checks."""
        state = controller.state
        if not state or state.get("stage") != "awaiting_credentials":
            return
        prior = deepcopy(state.get("credential_recovery"))
        self.prepare_recovery(controller)
        if state.get("credential_recovery") != prior:
            controller.save()
        key = controller.config["repository"], state["issue"]
        self.registry.record_shadow(key, state, funding=admissions.task_mode(controller.config, state))
        plan = state.get("credential_recovery")
        projected = self.registry.sponsorship(key).get("task", {}).get("recovery") or {}
        delivery = projected.get("delivery")
        if not plan or not delivery or projected.get("id") != plan["id"] or delivery["id"] == plan.get("consumed"):
            return
        if self.registry.clock() < delivery["due_at"]:
            return
        if state["calls"] >= controller.config["max_model_calls"]:
            plan.update(consumed=delivery["id"], status="budget requires writer attention")
            controller.save()
            return
        target = state["retry_stage"]
        try:
            self.preflight(controller, [], recovering=True)
            if not controller._validate_authority(comments, stage=target):
                if state.get("stage") == "awaiting_credentials":
                    plan.update(consumed=delivery["id"], status="authority requires writer attention")
                    controller.save()
                return
            if state.get("stage") != "awaiting_credentials":
                return
        except ProviderFailure:
            raise
        except CredentialsUnavailable as error:
            state.update(stage="awaiting_credentials", reason=str(error))
            plan.update(consumed=delivery["id"], status="coverage still unavailable")
            controller.save()
            return
        state["stage"] = target
        state.pop("provider_pause_pending", None)
        state.pop("retry_stage", None)
        state.pop("reason", None)
        plan.update(consumed=delivery["id"], status="resumed")
        controller.save()

    def retire_pending(self, controller):
        """Confirm exact failed-session retirement before any replacement admission."""
        pending = controller.state.get("provider_retire_sessions", [])
        for session in list(pending):
            self.operator().end(session)
            pending.remove(session)
            controller.save()

    def preflight(self, controller, requests, *, recovering=False):
        """Select all required frozen payer versions before any reservation is charged."""
        try:
            key = controller.config["repository"], controller.state["issue"]
            controller.state.pop("provider_observations", None)  # The durable outbox owns full history.
            self.retire_pending(controller)
            if controller.state.get("provider_pause_pending") and not recovering:
                raise ProviderFailure(controller.state["provider_failure"])
            if hasattr(self.operator(), "observations"):
                observation_reply = self.operator().observations(*key)
                if observation_reply.get("pending"):
                    raise Transient("native request observations pending")
                observed = observation_reply.get("events", [])
                seen = controller.state.get("provider_observation", 0)
                fresh = [event for event in observed if event["sequence"] > seen]
                failures = [
                    event
                    for event in observed
                    if event.get("category") in ("auth", "quota_exhausted", "rate_limited", "budget_denied")
                    and event.get("turn") not in controller.state.get("provider_paused_turns", [])
                ]
                if fresh or failures:
                    controller.state["provider_observation"] = max([seen] + [e["sequence"] for e in fresh])
                    if failures:
                        controller.state["provider_failure"] = failures[0]
                        controller.state["provider_pause_pending"] = True
                        paused = controller.state.setdefault("provider_paused_turns", [])
                        paused.extend(event["turn"] for event in failures if event["turn"] not in paused)
                        retiring = controller.state.setdefault("provider_retire_sessions", [])
                        retiring.extend(
                            event["session"] for event in failures if event.get("session") not in (None, *retiring)
                        )
                    controller.save()
                    if failures:
                        self.retire_pending(controller)
                        error = ProviderFailure(failures[0])
                        error.observations = failures
                        raise error
            capabilities = self.operator().capabilities()
            projection = getattr(controller, "shadow_projection", None)
            self.source_fence(key, projection)
            intended = {
                **admissions.provider_modes(controller.config, controller.state),
                **sponsorship.project(projection["facts"])["provider_funding"],
            }
            if (
                not controller.state.get("funding_initialization")
                and "contributed" in intended.values()
                and any(fact["command"] == "funding" for fact in projection["facts"])
            ):
                raise Transient("provider funding initialization is pending")
            self.flush(controller)
            self.apply(controller, projection)
            funding = admissions.provider_modes(controller.config, controller.state)
            selected = []
            for profile, stage, context in requests:
                route = sponsorship.credential_route(
                    controller.config, profile, stage, context.get("reviewer_identity")
                )
                agent = resolve(controller.config, profile, stage, context.get("reviewer_identity"))
                role = next((name for name, value in sponsorship.ROLES.items() if value == stage), stage)
                requirement = {
                    "provider": agent.profile.provider,
                    "route": route,
                    "role": role,
                    "profile": profile,
                    "model": agent.profile.model,
                }
                detail = ", ".join(f"{name}={value}" for name, value in requirement.items())
                if funding[agent.profile.provider] == "org":
                    selected.append((None, agent.profile.provider, agent.profile, role, profile))
                    continue
                if route not in sponsorship.ROUTES or route not in capabilities.get("contributed_routes", []):
                    raise CredentialsUnavailable(
                        f"{detail}: contributed credential route adapter is unavailable", requirement
                    )
                provider = sponsorship.ROUTES[route]["sponsorship_provider"]
                if agent.profile.provider != provider:
                    raise CredentialsUnavailable(
                        f"{detail}: credential route does not support selected profile", requirement
                    )
                selected.append((route, provider, agent.profile, role, profile))
            requested_count = len(selected)
            # Every configured role is required in the current pipeline. No optional evaluator
            # participates in admission; the separate design_eval tool is diagnostic only.
            for requirement in sponsorship.selected_routes(controller.config):
                route = requirement["route"]
                if funding[requirement["provider"]] == "org":
                    continue
                if route not in sponsorship.ROUTES or route not in capabilities.get("contributed_routes", []):
                    detail = ", ".join(f"{name}={value}" for name, value in requirement.items())
                    raise CredentialsUnavailable(
                        f"{detail}: required role credential route adapter is unavailable", requirement
                    )
                agent = resolve(controller.config, requirement["profile"], sponsorship.ROLES[requirement["role"]])
                selected.append(
                    (route, requirement["provider"], agent.profile, requirement["role"], requirement["profile"])
                )
            projection = getattr(controller, "shadow_projection", None)
            self.source_fence(key, projection)
            self.flush(controller, {p for _, p, _, _, _ in selected})
            self.cutover(controller, {p for _, p, _, _, _ in selected if funding[p] == "contributed"})
            self.apply(controller, projection, {p for _, p, _, _, _ in selected})
            owners, metadata, choices = {}, {}, []
            for route, provider, profile, role, profile_name in selected:
                if funding[provider] == "org":
                    choices.append(
                        admissions.selection(
                            controller.agents.credential(profile_name, sponsorship.ROLES.get(role, role), None)
                        )
                    )
                    continue
                requirement = {
                    "provider": provider,
                    "route": route,
                    "role": role,
                    "profile": profile_name,
                    "model": profile.model,
                }
                detail = ", ".join(f"{name}={value}" for name, value in requirement.items())
                if provider in controller.state.get("pending_sponsorships", {}):
                    raise CredentialsUnavailable(f"{detail}: provider sponsorship transition is pending", requirement)
                slot = controller.state.get("sponsorships", {}).get(provider, {})
                actor = slot.get("sponsor_id")
                if (
                    slot.get("status") != "active"
                    or not actor
                    or not (
                        slot.get("provenance") == "explicit_comment"
                        or slot.get("provenance") in ("task_start", "label")
                        and slot.get("source_funding_mode") == "contributed"
                    )
                ):
                    raise CredentialsUnavailable(
                        f"{detail}: provider requires current contributed sponsorship", requirement
                    )
                if actor not in owners:
                    owners[actor] = self.writer(controller.config.get("upstream", key[0]), actor, slot.get("login"))
                    if not owners[actor]:
                        self.revoke_authority(controller, projection, actor)
                        raise CredentialsUnavailable(
                            f"{detail}: provider sponsor no longer has repository write permission", requirement
                        )
                    metadata[actor] = self.metadata.read(actor) if self.metadata else None
                rows = metadata[actor]
                if rows is None:
                    raise Transient(f"{detail}: sponsorship metadata unavailable")
                record = next((m for m in rows if m.get("route") == route), None)
                if (
                    record is None
                    or record.get("status") != "valid"
                    or record.get("kind") != sponsorship.ROUTES[route]["kind"]
                ):
                    raise CredentialsUnavailable(f"{detail}: provider credential is unavailable", requirement)
                if recovering:
                    minimum = max(
                        (
                            item["minimum_version"]
                            for item in controller.state["credential_recovery"]["requirements"]
                            if item["sponsor"] == actor and item["route"] == route
                        ),
                        default=0,
                    )
                    if type(record.get("version")) is not int or record["version"] <= minimum:
                        raise Transient(f"{detail}: validated credential version is not yet current")
                if not isinstance(record.get("models"), list) or any(
                    not isinstance(model, str) for model in record["models"]
                ):
                    raise Transient(f"{detail}: sponsorship model coverage unknown")
                if profile.model not in record["models"]:
                    raise CredentialsUnavailable(
                        f"{detail}: provider credential does not cover selected model", requirement
                    )
                if record.get("expiry_known") and (
                    not isinstance(record.get("expires_at"), str)
                    or datetime.fromisoformat(record["expires_at"].replace("Z", "+00:00")).timestamp()
                    <= self.registry.clock()
                ):
                    raise CredentialsUnavailable(f"{detail}: provider credential expired", requirement)
                credential = Credential(
                    digest(["contributed", actor, route, record.get("record_id"), record.get("version")]),
                    profile.harness,
                    "contributed",
                    ADAPTER,
                    actor,
                    route,
                    record.get("record_id"),
                    record.get("version"),
                )
                choice = admissions.selection(credential, provider, slot)
                admissions.validate(choice)
                choices.append(choice)
            self.source_fence(key, projection)
            if getattr(controller.agents, "executor", None):
                controller.agents.executor.check_owner()
            return choices[:requested_count]
        except ExchangeError as error:
            if retryable(error):
                raise Transient("sponsorship exchange unavailable") from None
            raise

    def source_fence(self, key, projection):
        """Require the exact confirmed source projection read before payer selection."""
        latest = self.registry.sponsorship(key)
        if (
            not projection
            or not projection.get("history_complete")
            or latest["status"] != "confirmed"
            or any(latest.get(k) != projection.get(k) for k in ("source_revision", "revision"))
        ):
            raise Transient("sponsorship source changed before admission")

    def cutover(self, controller, providers=None):
        """Retire historical implicit attribution without treating it as personal payment consent."""
        for provider, slot in list(controller.state.get("sponsorships", {}).items()):
            if providers is not None and provider not in providers:
                continue
            if (
                slot.get("sponsor_id")
                and slot.get("provenance") in ("task_start", "label")
                and slot.get("source_funding_mode") != "contributed"
            ):
                new = {
                    "sponsor_id": None,
                    "status": "unassigned",
                    "epoch": slot["epoch"] + 1,
                    "cutover_from": slot.get("grant_id"),
                    **{k: deepcopy(slot[k]) for k in ("funding", "funding_order") if k in slot},
                }
                self.pending(controller, provider, new, slot["epoch"], "cutover")
                self.flush(controller, {provider})

    def source_policy(self, controller, boundary):
        """Use authenticated effective-interval evidence, never current settings at delayed delivery."""
        at = boundary.get("at")
        if not at or boundary.get("provenance") == "legacy":
            return {"funding_mode": "unknown", "config_digest": None}
        return self.registry.funding_policy(
            controller.config["repository"],
            config_digest(controller.config),
            datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp(),
        )

    def before_attempt(self, controller):
        """Mark racing shadow evidence pending without blocking or rebinding an org-paid attempt."""
        key = controller.config["repository"], controller.issue["number"]
        latest = self.registry.sponsorship(key)
        adopted = getattr(controller, "shadow_projection", None)
        if not adopted or latest["status"] != "confirmed" or latest.get("revision") != adopted["revision"]:
            controller.state["sponsorship_status"] = "pending"

    def confirm(self, key, comments, events, author, revision=None, config=None):
        """Confirm complete source history against known revisions and the local observation fence."""
        previous = self.registry.sponsorship(key)
        if revision is not None and revision != previous["source_revision"]:
            raise Transient("sponsorship source changed during read")
        revision = previous["source_revision"]
        hints = self.registry.sponsor_sources(key)
        old = {f["grant_id"]: f for f in previous.get("facts", [])}
        current = {c["id"]: c for c in comments}
        verified = {}
        permissions = {}
        complete = True

        def authorized(actor, login):
            """One fresh ID-bound permission read per actor in this source observation."""
            if actor not in permissions:
                permissions[actor] = self.writer(config.get("upstream", key[0]) if config else key[0], actor, login)
            return permissions[actor]

        for raw in comments:
            delegated = delegation.resolve(raw, self.key, *key, author)
            comment = delegated or raw
            if comment.get("user", {}).get("login") == author:
                continue
            item = sponsorship.fact(comment, authorized)
            if item is None:
                continue
            revisions = [
                h
                for h in hints
                if h["comment_id"] == raw["id"]
                and h["action"] == "edited"
                and h["body_digest"] == digest(raw.get("body") or "")
                and h["updated_at"] == raw.get("updated_at")
            ]
            if not delegated and (revisions or raw.get("created_at") != raw.get("updated_at")):
                if not any(h.get("editor_id") == comment.get("user", {}).get("id") for h in revisions):
                    if not revisions or any(h.get("editor_id") is None for h in revisions):
                        complete = False
                    continue
            if item:
                verified[item["grant_id"]] = item
                created, updated = raw.get("created_at"), raw.get("updated_at")
                if (
                    created
                    and updated
                    and created != updated
                    and not any(
                        h["comment_id"] == raw["id"]
                        and (
                            h["action"] == "created"
                            or h["action"] == "edited"
                            and h.get("previous_digest")
                            and h.get("previous") is None
                        )
                        for h in hints
                    )
                ):
                    complete = False
        for source in hints:
            raw = {
                "id": source["comment_id"],
                "body": source["body"],
                "user": {"id": source["actor_id"], "login": source["login"]},
                "created_at": source["created_at"],
                "updated_at": source["updated_at"],
            }
            delegated = delegation.resolve(raw, self.key, *key, author)
            comment = delegated or raw
            if comment.get("user", {}).get("login") == author:
                continue
            item = sponsorship.fact(comment, authorized)
            if item is None:
                continue
            if not delegated and source["action"] == "edited" and source.get("editor_id") != source["actor_id"]:
                if source.get("editor_id") is None:
                    complete = False
                continue
            if item:
                old[item["grant_id"]] = item
        facts = {**old, **verified}
        # Source removal cannot erase a durable withdrawal. Its own timestamp is used only
        # when an edit is observed; an unprovable deletion is ordered after initialization.
        for item in list(facts.values()):
            observed = current.get(item["source_id"])
            effective = delegation.resolve(observed, self.key, *key, author) or observed if observed else None
            removed = effective is None or digest(effective.get("body") or "") != item["body_digest"]
            if removed and (
                item["command"] == "sponsor" or item["command"] == "funding" and item["funding"] == "contributed"
            ):
                when = (observed or {}).get("updated_at") or "9999-12-31T23:59:59Z"
                value = {
                    **item,
                    "command": "withdraw",
                    "removed_source": item["source_id"],
                    "source_order": [when, item["source_id"], -1],
                }
                value["grant_id"] = digest(value)
                facts[value["grant_id"]] = value
        for item in facts.values():
            raw = current.get(item["source_id"])
            observed = (delegation.resolve(raw, self.key, *key, author) or raw) if raw else None
            if (
                observed
                and (item["command"] == "sponsor" or item["command"] == "funding" and item["funding"] == "contributed")
                and item["body_digest"] != digest(observed.get("body") or "")
                and item["source_order"][0] == observed.get("updated_at")
            ):
                related = [h for h in hints if h["comment_id"] == item["source_id"]]
                if not any(
                    h.get("previous") is not None and digest(h["previous"]) == item["body_digest"] for h in related
                ):
                    complete = False
                item["source_order"][2] = -2
                item["grant_id"] = digest({k: v for k, v in item.items() if k != "grant_id"})
        ordered = sorted(facts.values(), key=lambda f: f["source_order"])
        document = {
            "facts": ordered,
            **sponsorship.project(ordered),
            "history_complete": complete,
            "source_digest": digest(
                [[(c["id"], digest(c.get("body") or ""), c.get("updated_at")) for c in comments], events]
            ),
            "permissions": {str(k): v for k, v in permissions.items()},
            "funding": admissions.mode(config) if config is not None else "unknown",
            "provider_funding": {
                **({p: admissions.mode(config) for p in sponsorship.PROVIDERS} if config else {}),
                **sponsorship.project(ordered)["provider_funding"],
            },
        }
        document["revision"] = digest([revision, document])
        document["digest"] = digest(document)
        if config is not None:
            document["routes"] = sponsorship.selected_routes(config)
            document["coverage"] = self.coverage(sponsorship.selected_routes(config), document["slots"])
        if not self.registry.confirm_sponsorship(key, revision, document, comments):
            raise Transient("sponsorship source changed during observation")
        return {**document, "status": "confirmed" if complete else "provisional", "source_revision": revision}

    def usage(self, controller, comments):
        """Reply to a pre-task bare command once by its exact App-authored source marker."""
        for raw in comments:
            effective = (
                delegation.resolve(
                    raw,
                    self.key,
                    controller.config["repository"],
                    controller.issue["number"],
                    controller.identities.author("state"),
                )
                or raw
            )
            command = parse(effective.get("body") or "")
            if command is None or command.kind != CommandKind.SPONSOR or command.argument is not None:
                continue
            user = effective.get("user") or {}
            if not user.get("id") or not self.writer(
                controller.config.get("upstream", controller.config["repository"]), user["id"], user["login"]
            ):
                continue
            marker = f"<!-- concurro:sponsorship-usage:{raw['id']} -->"
            if any(
                c.get("user", {}).get("login") == controller.identities.author("state")
                and marker in (c.get("body") or "")
                for c in comments
            ):
                continue
            body = (
                marker
                + "\nUse `/concurro funding <provider> personal` to select your credentials or `/concurro funding <provider> org` to select organization funding. `/concurro sponsor <provider>` records prospective consent; `/concurro withdraw <provider>` withdraws your consent. "
                + admissions.funding_notice(
                    controller.config,
                    controller.state
                    or {"provider_funding": getattr(controller, "shadow_projection", {}).get("provider_funding", {})},
                )
            )
            controller.gh.repo(f"issues/{controller.issue['number']}/comments", "POST", {"body": body})

    def sync(self, controller, raw_comments):
        """Project intent without creating a task; apply acknowledged transitions on existing tasks."""
        key = (controller.config["repository"], controller.issue["number"])
        controller.shadow_projection = None
        try:
            if controller.state and "provider_funding" not in controller.state:
                old_task = self.registry.sponsorship(key).get("task", {})
                baseline = old_task.get("funding", admissions.mode(controller.config))
                controller.state["provider_funding"] = old_task.get("provider_funding") or {
                    p: baseline for p in sponsorship.PROVIDERS
                }
                controller.save()
            events = pages(self.reader(key[0]), f"repos/{key[0]}/issues/{key[1]}/events")
            projection = self.confirm(
                key,
                raw_comments,
                events,
                controller.identities.author("state"),
                getattr(controller, "shadow_source_revision", None),
                controller.config,
            )
            controller.shadow_projection = projection
            if not controller.state:
                self.usage(controller, raw_comments)
                return
            if not projection["history_complete"]:
                raise ValueError("sponsorship source history incomplete")
            if not controller.state.get("funding_initialization") and not controller.state.get(
                "funding_initialization_pending"
            ):
                self.initialize(controller, [], "legacy")
            self.flush(controller)
            if controller.state.get("funding_initialization_pending"):
                self.initialize(controller, raw_comments, controller.state["mode"])
            self.apply(controller, projection)
            self.permissions(controller, projection)
            if controller.state.get("funding_initialization"):
                controller.state["sponsorship_projection_revision"] = projection["revision"]
                controller.state["sponsorship_projection_source_revision"] = projection["source_revision"]
                if not controller.state.get("pending_sponsorships"):
                    changed = (
                        controller.state.get("sponsorship_status") != "confirmed"
                        or "sponsorship_retry_attempts" in controller.state
                    )
                    controller.state["sponsorship_status"] = "confirmed"
                    controller.state.pop("sponsorship_retry_attempts", None)
                    if changed:
                        controller.save()
        except (Transient, ExchangeError, ValueError) as error:
            if controller.state:
                self.defer(controller, error)
        finally:
            if controller.state:
                self.registry.record_shadow(
                    key,
                    controller.state,
                    getattr(controller, "shadow_projection", None),
                    funding=admissions.task_mode(controller.config, controller.state),
                )

    def initialize(self, controller, comments, mode, command_id=0, enrollment=None):
        """Keep unavailable shadow history/metadata separate from org-paid execution."""
        try:
            self._initialize(controller, comments, mode, command_id, enrollment)
        except (Transient, ExchangeError, ValueError) as error:
            self.defer(controller, error)
        finally:
            key = controller.config["repository"], controller.state["issue"]
            self.registry.record_shadow(
                key,
                controller.state,
                getattr(controller, "shadow_projection", None),
                funding=admissions.task_mode(controller.config, controller.state),
            )

    def defer(self, controller, error):
        """Retry transient shadow I/O through the existing queue/backoff, independently of org work."""
        state = controller.state
        changed = state.get("sponsorship_status") != "pending"
        state["sponsorship_status"] = "pending"
        if retryable(error):
            attempt = state.get("sponsorship_retry_attempts", 0)
            state["sponsorship_retry_attempts"] = attempt + 1
            controller.save()
            self.registry.enqueue(
                (controller.config["repository"], state["issue"]),
                "shadow-reconcile",
                self.registry.clock() + backoff(attempt),
            )
        elif changed:
            controller.save()

    def _initialize(self, controller, comments, mode, command_id=0, enrollment=None):
        """Persist the first source boundary even when shadow metadata/history is temporarily unavailable."""
        state = controller.state
        if state.get("funding_initialization"):
            return
        boundary = state.get("funding_initialization_pending")
        if boundary is None:
            if mode == "manual":
                comment = next((c for c in comments if c["id"] == command_id), None)
                if not comment:
                    return
                boundary = {
                    "source_id": command_id,
                    "actor_id": str(comment["user"].get("id", "")),
                    "login": comment["user"]["login"],
                    "at": comment.get("updated_at") or comment.get("created_at"),
                    "provenance": "task_start",
                }
                raw = next((c for c in controller.consumed_comments if c["id"] == command_id), comment)
                edited = raw.get("created_at") != raw.get("updated_at") or any(
                    h["comment_id"] == command_id and h["action"] == "edited"
                    for h in self.registry.sponsor_sources((controller.config["repository"], state["issue"]))
                )
                if edited:
                    boundary["initiator_revision"] = {
                        "body_digest": digest(raw.get("body") or ""),
                        "delegated": bool(
                            delegation.resolve(
                                raw,
                                self.key,
                                controller.config["repository"],
                                state["issue"],
                                controller.identities.author("state"),
                            )
                        ),
                    }
            elif mode == "autonomous":
                boundary = {
                    "source_id": enrollment["event"],
                    "actor_id": str(enrollment.get("actor_id", "")),
                    "login": enrollment["actor"],
                    "at": enrollment.get("at"),
                    "provenance": "label",
                }
            else:
                boundary = {
                    "source_id": None,
                    "actor_id": None,
                    "login": None,
                    "at": "9999-12-31T23:59:59Z",
                    "provenance": "legacy",
                }
            boundary["source_policy"] = self.source_policy(controller, boundary)
            state["funding_initialization_pending"] = deepcopy(boundary)
        projection = getattr(controller, "shadow_projection", None)
        if not projection or not projection["history_complete"]:
            return
        if boundary["provenance"] == "legacy":
            routes, metadata = [], []
        else:
            if not boundary["at"] or not boundary["actor_id"]:
                return
            routes = sponsorship.selected_routes(controller.config)
            earlier = sponsorship.project([f for f in projection["facts"] if f["source_order"][0] <= boundary["at"]])
            explicit_funding = any(fact["command"] == "funding" for fact in projection["facts"])
            implicit = (
                admissions.mode(controller.config) == "org"
                and not explicit_funding
                or boundary.get("source_policy", {}).get("funding_mode") == "contributed"
            )
            if explicit_funding or any(route["route"] is not None for route in routes):
                routes = [route for route in routes if route["route"] is not None]
            if explicit_funding:
                current = {
                    **admissions.provider_modes(controller.config, state),
                    **sponsorship.project(projection["facts"])["provider_funding"],
                }
                routes = [route for route in routes if current[route["provider"]] == "contributed"]
            filling = {route["provider"] for route in routes}
            needs_fill = implicit and any(
                provider in filling
                and not slot.get("sponsor_id")
                and boundary["actor_id"] not in earlier["tombstones"][provider]
                for provider, slot in earlier["slots"].items()
            )
            initiator = boundary.get("initiator_revision")
            if (
                needs_fill
                and initiator
                and not initiator["delegated"]
                and not any(
                    h["comment_id"] == boundary["source_id"]
                    and h["action"] == "edited"
                    and h["updated_at"] == boundary["at"]
                    and h["body_digest"] == initiator["body_digest"]
                    and str(h.get("editor_id")) == boundary["actor_id"]
                    for h in self.registry.sponsor_sources((controller.config["repository"], state["issue"]))
                )
            ):
                return
            if needs_fill and (not self.metadata or any(r["route"] is None for r in routes)):
                return
            metadata = self.metadata.read(boundary["actor_id"], boundary["at"]) if needs_fill else []
            if not implicit:
                routes = []
                boundary = {**boundary, "initializer_status": "unknown"}
            selected = {
                r["route"] for r in routes if r["provider"] and not earlier["slots"][r["provider"]].get("sponsor_id")
            }
            metadata = [record for record in metadata if record["route"] in selected]
            if any(
                record.get("status") == "unknown" or record.get("status") == "valid" and record.get("models") is None
                for record in metadata
            ):
                if not any(
                    f["command"] == "sponsor" or f["command"] == "funding" and f["funding"] == "contributed"
                    for f in projection["facts"]
                ):
                    raise ValueError("sponsorship credential history incomplete")
                routes, metadata = [], []
                boundary = {**boundary, "initializer_status": "unknown"}
        latest = self.registry.sponsorship((controller.config["repository"], state["issue"]))
        if latest["source_revision"] != projection["source_revision"] or (
            latest.get("revision") is not None
            and (latest["status"] != "confirmed" or latest["revision"] != projection["revision"])
        ):
            raise Transient("sponsorship source changed during metadata read")
        frozen = sponsorship.initialize(projection, boundary, routes, metadata)
        if boundary.get("initializer_status") == "unknown":
            frozen.update(initializer_status="unknown", available=None, metadata=None)
        frozen["observed_sources"] = [f["grant_id"] for f in projection["facts"]]
        state.setdefault("provider_funding", admissions.provider_modes(controller.config, state))
        state["provider_funding"].update(frozen["provider_funding"])
        state["funding_initialization"] = frozen
        conversation.queue(
            state,
            "Provider sponsorship is org-funded"
            if admissions.task_mode(controller.config, state) == "org"
            else "Provider sponsorship recorded",
            admissions.funding_notice(
                controller.config,
                controller.state
                or {"provider_funding": getattr(controller, "shadow_projection", {}).get("provider_funding", {})},
            ),
        )
        state["sponsorship_projection_revision"] = projection["revision"]
        state["sponsorship_projection_source_revision"] = projection["source_revision"]
        state.pop("funding_initialization_pending", None)
        state["sponsorships"] = {}
        state["sponsorship_applied"] = list(frozen["applied"])
        state["sponsorship_sources"] = {
            str(f["source_id"]): f["grant_id"] for f in projection["facts"] if f["grant_id"] in frozen["applied"]
        }
        for provider, slot in frozen["slots"].items():
            self.pending(
                controller,
                provider,
                {**slot, "funding": state["provider_funding"][provider], "epoch": 1 if slot.get("sponsor_id") else 0},
                -1,
                "initialize",
                save=False,
            )
        controller.save()
        self.flush(controller)
        self.apply(controller, projection)

    def pending(self, controller, provider, slot, expected, reason, source=None, actor=None, cutoff=None, save=True):
        """Persist one exact transition before any Exchange I/O, retaining replay identity across crashes."""
        state = controller.state
        body = {
            "repository": controller.config["repository"],
            "issue": state["issue"],
            "provider": provider,
            "generation": controller.gh.generation,
            "expected_epoch": expected,
            "slot": deepcopy(slot),
            "reason": reason,
            "actor": actor,
            "cutoff": cutoff,
        }
        body["transition"] = digest(body)
        state.setdefault("pending_sponsorships", {})[provider] = {"request": body, "source": source}
        if save:
            controller.save()

    def flush(self, controller, providers=None):
        """Acknowledge only a durably accepted provider transition before consuming its source."""
        state = controller.state
        while state.get("pending_sponsorships"):
            available = [
                (p, v) for p, v in state["pending_sponsorships"].items() if providers is None or p in providers
            ]
            if not available:
                return
            provider, pending = available[0]
            group = [
                (p, value)
                for p, value in state["pending_sponsorships"].items()
                if p == provider
                or pending["request"]["reason"] == "permission_loss"
                and value["request"]["reason"] == "permission_loss"
                and value["request"]["actor"] == pending["request"]["actor"]
            ]
            try:
                self.operator().sponsorship({"transitions": [value["request"] for _, value in group]})
            except ExchangeError as error:
                rejected = next(
                    (value for _, value in group if value["request"]["transition"] == error.transition), None
                )
                if (
                    error.status != 409
                    or error.code != "stale_sponsorship_generation"
                    or rejected is None
                    or rejected["request"]["generation"] >= controller.gh.generation
                ):
                    raise
                # Only this ID was proven unapplied; other group members may already have receipts.
                request = {k: deepcopy(v) for k, v in rejected["request"].items() if k != "transition"}
                request["generation"] = controller.gh.generation
                request["transition"] = digest(request)
                rejected["request"] = request
                controller.save()
                continue
            for provider, pending in group:
                state["sponsorships"][provider] = deepcopy(pending["request"]["slot"])
                if (
                    pending["request"]["reason"] in ("initialize", "funding")
                    and "funding" in pending["request"]["slot"]
                ):
                    state.setdefault("provider_funding", admissions.provider_modes(controller.config, state))[
                        provider
                    ] = pending["request"]["slot"]["funding"]
                state.setdefault("sponsorship_history", []).append(pending["request"])
                if pending["source"]:
                    source = pending["source"]
                    state.setdefault("sponsorship_applied", []).append(source["grant_id"])
                    state.setdefault("sponsorship_sources", {})[str(source["source_id"])] = source["grant_id"]
                del state["pending_sponsorships"][provider]
            state["sponsorship_status"] = "pending" if state.get("pending_sponsorships") else "confirmed"
            controller.save()

    def apply(self, controller, projection, providers=None):
        """Apply each new source once; late evidence never rewrites a charged org attempt."""
        state = controller.state
        if not state.get("funding_initialization"):
            return
        for source in projection["facts"]:
            if source["grant_id"] in state.get("sponsorship_applied", []):
                continue
            provider = source["provider"]
            if providers is not None and provider not in providers:
                continue
            cutoff = state.get("sponsorship_authority_revoked", {}).get(source["actor_id"])
            if (
                cutoff is not None
                and datetime.fromisoformat(source["source_order"][0].replace("Z", "+00:00")).timestamp()
                <= cutoff["observed_at"]
            ):
                state.setdefault("sponsorship_applied", []).append(source["grant_id"])
                controller.save()
                continue
            if provider in state.get("pending_sponsorships", {}):
                continue
            if str(source["source_id"]) in state.get("sponsorship_sources", {}) and source["grant_id"] not in state[
                "funding_initialization"
            ].get("observed_sources", []):
                continue  # Adopted source is immutable; edits/deletions cannot rewrite consent.
            frozen = state["funding_initialization"]
            if source["source_order"][0] <= frozen["boundary"]["at"] and source["grant_id"] not in frozen.get(
                "observed_sources", []
            ):
                state.setdefault("sponsorship_ordering_diagnostics", []).append(
                    {
                        "source": source["grant_id"],
                        "adopted_revision": frozen["projection_revision"],
                        "calls_already_charged": state["calls"],
                        "recorded_grant": state["sponsorships"][provider].get("grant_id"),
                    }
                )
            if (
                source.get("removed_source")
                and state["sponsorships"][provider].get("source_id") != source["removed_source"]
            ):
                state.setdefault("sponsorship_applied", []).append(source["grant_id"])
                controller.save()
                continue
            if source["command"] == "withdraw" and any(
                (f["command"] == "sponsor" or f["command"] == "funding" and f["funding"] == "contributed")
                and f["actor_id"] == source["actor_id"]
                and f["provider"] == provider
                and f["source_order"] > source["source_order"]
                and f["grant_id"] in state.get("sponsorship_applied", [])
                for f in projection["facts"]
            ):
                state.setdefault("sponsorship_applied", []).append(source["grant_id"])
                controller.save()
                continue
            slot = state["sponsorships"][provider]
            funding = state.get("provider_funding", {}).get(provider, admissions.mode(controller.config))
            if source["command"] == "funding":
                if slot.get("funding_order", []) > source["source_order"]:
                    state.setdefault("sponsorship_applied", []).append(source["grant_id"])
                    controller.save()
                    continue
                funding = source["funding"]
            older_consent = slot.get("source_order", []) > source["source_order"] and slot.get("provenance") not in (
                "task_start",
                "label",
            )
            if source["command"] == "funding" and (funding == "org" or older_consent):
                new = deepcopy(slot)
                cutoff = None
            elif (
                source["command"] == "sponsor" or source["command"] == "funding" and source["funding"] == "contributed"
            ):
                if older_consent:
                    state.setdefault("sponsorship_applied", []).append(source["grant_id"])
                    controller.save()
                    continue
                new = {
                    **deepcopy(source),
                    "sponsor_id": source["actor_id"],
                    "status": "active",
                    "epoch": slot["epoch"] + 1,
                }
                cutoff = None
            else:
                new = (
                    {
                        "sponsor_id": None,
                        "status": "withdrawn",
                        "epoch": slot["epoch"] + 1,
                        "source_order": source["source_order"],
                        "grant_id": source["grant_id"],
                    }
                    if slot.get("sponsor_id") == source["actor_id"]
                    else deepcopy(slot)
                )
                cutoff = slot["epoch"]
            if source["command"] != "withdraw" or slot.get("sponsor_id") == source["actor_id"]:
                new["funding"] = funding
            if source["command"] == "funding":
                new["funding_order"] = source["source_order"]
            elif "funding_order" in slot:
                new["funding_order"] = slot["funding_order"]
            self.pending(
                controller, provider, new, slot["epoch"], source["command"], source, source["actor_id"], cutoff
            )
            self.flush(controller, providers)

    def permissions(self, controller, projection):
        """Writer loss publishes all that user's task/provider cutoffs together, leaving org work alone."""
        state = controller.state
        revoked = state.setdefault("sponsorship_authority_revoked", {})
        slots = state.get("sponsorships", {})
        identities = {item["actor_id"]: item["login"] for item in projection["facts"]}
        identities.update({slot["sponsor_id"]: slot["login"] for slot in slots.values() if slot.get("sponsor_id")})
        for actor, login in identities.items():
            owns = any(slot.get("sponsor_id") == actor for slot in slots.values())
            if actor in revoked and not owns:
                continue
            if self.writer(controller.config.get("upstream", controller.config["repository"]), actor, login):
                continue
            self.revoke_authority(controller, projection, actor)

    def revoke_authority(self, controller, projection, actor):
        """Publish all of one user's task/provider cutoffs atomically after a fresh writer denial."""
        state = controller.state
        revoked = state.setdefault("sponsorship_authority_revoked", {})
        slots = state.get("sponsorships", {})
        for provider, slot in slots.items():
            new = (
                {
                    "sponsor_id": None,
                    "status": "withdrawn",
                    "epoch": slot["epoch"] + 1,
                    "source_order": slot.get("source_order", []),
                    "authority_revoked": True,
                    **{k: deepcopy(slot[k]) for k in ("funding", "funding_order") if k in slot},
                }
                if slot.get("sponsor_id") == actor
                else deepcopy(slot)
            )
            self.pending(
                controller,
                provider,
                new,
                slot["epoch"],
                "permission_loss",
                actor=actor,
                cutoff=slot["epoch"],
                save=False,
            )
        observed = max(
            [self.registry.clock()]
            + [
                datetime.fromisoformat(f["source_order"][0].replace("Z", "+00:00")).timestamp()
                for f in projection["facts"]
                if not f.get("removed_source")
            ]
        )
        revoked[actor] = {"observed_at": observed, "source_revision": projection["source_revision"]}
        controller.save()
        self.flush(controller)
