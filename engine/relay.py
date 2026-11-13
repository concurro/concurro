"""Clients of the gateway's exchange: the operator side (the service) and a runner job's side."""

import json
import re
import urllib.error
import urllib.request

# Long polls wait up to this long on the exchange; the HTTP timeout leaves room beyond it.
WAIT = 45
# A runner can no longer renew or acknowledge this session.
SESSION_GONE = frozenset({401, 403, 409, 410})


class ExchangeError(RuntimeError):
    """The exchange refused a call or could not be reached (`status` 0)."""

    def __init__(self, status, reason, code=None, transition=None):
        """Retain only recognized private refusal metadata for safe transition recovery."""
        super().__init__(f"exchange {status}: {reason}")
        self.status, self.code, self.transition = status, code, transition


def call(base, token, method, path, body=None, timeout=WAIT + 30):
    """One JSON call with a bearer token; the token never appears in an error."""
    request = urllib.request.Request(
        base.rstrip("/") + "/exchange/" + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            details = json.loads(error.read())
            reason = details.get("error", "")
        except (ValueError, AttributeError):
            reason, details = "", {}
        transition = details.get("transition")
        code = details.get("code")
        known = (
            error.code == 409
            and code == "stale_sponsorship_generation"
            and isinstance(transition, str)
            and re.fullmatch(r"[0-9a-f]{64}", transition)
        )
        raise ExchangeError(
            error.code, str(reason)[:200], code if known else None, transition if known else None
        ) from None
    except (urllib.error.URLError, OSError, ValueError):
        raise ExchangeError(0, "unreachable") from None


class Operator:
    """The service's side of task-bound runner turns."""

    def __init__(self, gateway, token):
        self.gateway, self.token = gateway, token

    def capabilities(self):
        """Read implemented funding adapters before the controller reserves a charge."""
        return call(self.gateway, self.token, "GET", "operator/capabilities")

    def sponsorship(self, request):
        """Publish an idempotent task/provider ownership transition on the private operator channel."""
        return call(self.gateway, self.token, "POST", "operator/sponsorship", request)

    def end(self, session):
        call(self.gateway, self.token, "POST", f"operator/sessions/{session}/end", {})

    def submit(self, turn, expected, models, budget, lease, payload, pin=None, binding=None):
        return call(
            self.gateway,
            self.token,
            "POST",
            "operator/turns",
            {
                "turn": turn,
                "expected": expected,
                "models": models,
                "budget": budget,
                "lease": lease,
                "payload": payload,
                "pin": pin,
                "binding": binding,
            },
        )

    def poll(self, turn, wait=WAIT):
        return call(self.gateway, self.token, "GET", f"operator/turns/{turn}?wait={wait}")

    def cancel(self, turn):
        call(self.gateway, self.token, "DELETE", f"operator/turns/{turn}")


class Runner:
    """A runner job's side: its key for an OIDC token, then turns with its runner token."""

    def __init__(self, gateway, session):
        self.gateway, self.session, self.token = gateway, session, None

    def key(self, oidc, engine):
        """Claim the session, stating the engine commit this runner runs."""
        grant = call(self.gateway, oidc, "POST", f"sessions/{self.session}/key", {"engine": engine})
        self.token = grant["token"]
        return grant

    def next(self, capacity, wait):
        return call(
            self.gateway, self.token, "POST", f"sessions/{self.session}/next", {"capacity": capacity, "wait": wait}
        )["turns"]

    def complete(self, turn, result):
        call(self.gateway, self.token, "POST", f"sessions/{self.session}/turns/{turn}", {"result": result})

    def end(self):
        call(self.gateway, self.token, "POST", f"sessions/{self.session}/end", {})
