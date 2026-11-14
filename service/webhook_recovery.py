"""Recover failed App deliveries without polling idle issues and pull requests."""

import logging
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from policy import digest

from .identity import IdentityError

log = logging.getLogger("concurro.service")
HISTORY_SECONDS = 3 * 24 * 60 * 60
PAGE_SIZE = 100
PENDING = {"pending", "scheduled", "queued", "delivering", "in progress"}


def next_page(headers):
    """Use GitHub's next cursor without forwarding credentials to a supplied URL."""
    for link in headers.get("link", "").split(","):
        target, *parameters = link.strip().split(";")
        if not any(parameter.strip() == 'rel="next"' for parameter in parameters):
            continue
        parsed = urlsplit(target.removeprefix("<").removesuffix(">"))
        cursor = parse_qs(parsed.query).get("cursor")
        if (
            parsed.scheme != "https"
            or parsed.netloc != "api.github.com"
            or parsed.path != "/app/hook/deliveries"
            or not cursor
        ):
            raise IdentityError("invalid webhook delivery pagination")
        return "app/hook/deliveries?" + urlencode({"per_page": PAGE_SIZE, "cursor": cursor[0]})
    return None


class Recovery:
    """One App-wide delivery scan; reconcile tasks only when local coverage is missing."""

    def __init__(self, identity, registry, reconciler, installations, clock=time.time, list_index=None):
        """Reuse App authentication and the existing exceptional reconciliation path."""
        self.identity, self.registry, self.reconciler = identity, registry, reconciler
        self.list_index = list_index
        self.installations, self.clock = dict(installations), clock
        self.scope = digest([str(identity.app_id), sorted(installations.items()), sorted(reconciler.repositories)])

    def deliveries(self):
        """Read the retained summaries, including pending and transport-failed attempts."""
        path = f"app/hook/deliveries?per_page={PAGE_SIZE}"
        visited, groups = set(), {}
        while path:
            if path in visited:
                raise IdentityError("repeated webhook delivery page")
            visited.add(path)
            values, headers = self.identity.api("GET", path, self.identity.app_jwt(), with_headers=True)
            if not isinstance(values, list):
                raise IdentityError("invalid webhook delivery listing")
            for item in values:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("guid"), str)
                    or type(item.get("id")) is not int
                ):
                    raise IdentityError("invalid webhook delivery record")
                if item.get("installation_id") in self.installations.values():
                    groups.setdefault(item["guid"], []).append(item)
            path = next_page(headers)
        return groups

    def redeliver(self, attempts):
        """Return whether delivery remains unresolved, requesting a retry only for failures."""
        if any(
            str(attempt.get("status", "")).lower() == "ok"
            or isinstance(attempt.get("status_code"), int)
            and 200 <= attempt["status_code"] < 400
            for attempt in attempts
        ):
            return False
        if any(
            str(attempt.get("status", "")).lower() in PENDING
            or not attempt.get("status")
            and attempt.get("status_code") in (None, 0)
            for attempt in attempts
        ):
            return True
        failures = [
            attempt
            for attempt in attempts
            if isinstance(attempt.get("status_code"), int)
            and 400 <= attempt["status_code"] < 600
            or any(
                word in str(attempt.get("status", "")).lower()
                for word in ("fail", "error", "invalid", "timed out", "timeout")
            )
        ]
        if len(failures) != len(attempts):
            return True
        attempt = max(failures, key=lambda item: item["id"])
        path = f"app/hook/deliveries/{attempt['id']}"
        detail = self.identity.api("GET", path, self.identity.app_jwt())
        payload = (detail.get("request") or {}).get("payload") or {}
        repository = (payload.get("repository") or {}).get("full_name")
        installation = (payload.get("installation") or {}).get("id")
        if repository not in self.installations or installation != self.installations[repository]:
            return False
        # A retry may have succeeded between the listing and this detail read.
        code = detail.get("status_code")
        if detail.get("status") == "OK" or isinstance(code, int) and 200 <= code < 400:
            return False
        if str(detail.get("status", "")).lower() in PENDING:
            return True
        self.identity.api("POST", path + "/attempts", self.identity.app_jwt())
        return True

    def payload(self, attempt):
        """One delivery's request payload, for the list index to apply what it never received."""
        found = self.identity.api("GET", f"app/hook/deliveries/{attempt['id']}", self.identity.app_jwt())
        payload = (found.get("request") or {}).get("payload")
        if not isinstance(payload, dict) or not payload:
            raise IdentityError("webhook delivery without its payload")
        return payload

    def indexed(self, operation, *arguments):
        """Keep the list index covered by the same scan; its failures never stop delivery recovery."""
        if self.list_index is not None:
            try:
                getattr(self.list_index, operation)(*arguments)
            except Exception as error:
                log.warning("list index %s failed: %s", operation, type(error).__name__)

    def poll(self):
        """Advance coverage only after full reconciliation or a scan with no unresolved delivery."""
        started = self.clock()
        previous = self.registry.webhook_check(self.scope)
        try:
            if previous is None or started - previous >= HISTORY_SECONDS:
                self.reconciler.poll(strict=True)
                self.registry.webhook_check(self.scope, started)
                self.indexed("rebaseline")
            else:
                if getattr(self.reconciler, "policy_observer", None) is not None:
                    self.reconciler.policy_observer()
                if self.reconciler.cleanup is not None:
                    self.reconciler.cleanup()
            groups = self.deliveries()
            self.indexed("check", groups, started, self.payload)
            unresolved = False
            for attempts in groups.values():
                unresolved = self.redeliver(attempts) or unresolved
            if not unresolved:
                self.registry.webhook_check(self.scope, started)
        except Exception as error:
            log.warning("webhook recovery failed: %s", type(error).__name__)
