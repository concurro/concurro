"""Forward the user's credential requests to the vault with their own GitHub token.

The vault resolves the user itself; the backoffice never stores or reads back a secret.
"""

import httpx

import sponsorship

from .auth import BearerAuth, TokenCredential

# Stable UI type identifiers for the vault's fixed routes.
TYPES = {
    "anthropic_api": "anthropic-api-key",
    "claude_subscription": "claude-subscription",
    "openai_api": "openai-api-key",
    "chatgpt_subscription": "chatgpt-subscription",
}
PROVIDERS = {"anthropic": "Anthropic", "openai": "OpenAI"}
ROLES = {
    "triage": "Triage / intake",
    "planner": "Planning",
    "plan_reviewers": "Plan review",
    "implementer": "Implementation",
    "pr_reviewers": "PR review",
}
STATUS = {"valid": "valid", "invalid": "invalid", "revoked": "revoked"}
STALE = "Credentials are now managed as a list. Reload the page to continue."


class VaultError(Exception):
    """The vault refused or failed; `status` is the HTTP status to return, `reason` is safe to show."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


class Vault:
    def __init__(self, url: str | None, client: httpx.Client | None = None):
        self.url = url.rstrip("/") if url else None
        self.client = client or httpx.Client(timeout=30)

    def call(
        self,
        method: str,
        path: str,
        token: str | TokenCredential,
        body: dict | None = None,
        *,
        params: dict | None = None,
        timeout: float | None = None,
    ):
        if not self.url:
            raise VaultError(503, "the credentials vault is not configured")
        try:
            response = self.client.request(
                method,
                self.url + path,
                auth=BearerAuth(token),
                json=body,
                params=params,
                **({"timeout": timeout} if timeout is not None else {}),
            )
        except httpx.HTTPError:
            raise VaultError(503, "the credentials vault is unavailable") from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code >= 400:
            reason = payload.get("error") if isinstance(payload, dict) else None
            raise VaultError(response.status_code, reason if isinstance(reason, str) else "vault request failed")
        return payload

    def listing(self, token: str | TokenCredential) -> dict:
        """The owner's ordered provider lists with the types and role filters a new entry may use."""
        stored = self.call("GET", "/credentials", token)
        return {
            "providers": [group(value) for value in stored["providers"]],
            "types": [
                {
                    "id": ui,
                    "provider": kind["provider"],
                    "kind": kind["kind"],
                    "label": kind["label"],
                    "accountId": route == "chatgpt-subscription",
                }
                for ui, route in TYPES.items()
                for kind in stored["types"]
                if kind["route"] == route
            ],
            "roles": [{"key": key, "label": ROLES[key]} for key in sponsorship.ROLES],
        }

    def add(self, token: str | TokenCredential, kind: str, fields: dict) -> dict:
        """Forward only this owner's candidate and policy; never retain the secret."""
        if kind not in TYPES:
            raise VaultError(422, "this credential type can't be stored")
        return entry(self.call("POST", "/credentials", token, {"route": TYPES[kind], **policy(fields)}))

    def edit(self, token: str | TokenCredential, entry_id: str, fields: dict) -> dict:
        return entry(self.call("PATCH", f"/credentials/{entry_id}", token, policy(fields)))

    def replace(self, token: str | TokenCredential, entry_id: str, fields: dict) -> dict:
        return entry(self.call("PUT", f"/credentials/{entry_id}/secret", token, fields))

    def reorder(self, token: str | TokenCredential, provider: str, fields: dict) -> dict:
        return group(self.call("PUT", f"/credentials/order/{provider}", token, fields))

    def remove(self, token: str | TokenCredential, entry_id: str, revision: int) -> dict:
        return entry(self.call("DELETE", f"/credentials/{entry_id}", token, params={"revision": revision}))


def policy(fields: dict) -> dict:
    """Refuse role filters outside the engine's role keys before they could silently match nothing."""
    if any(role not in sponsorship.ROLES for role in fields.get("roles") or ()):
        raise VaultError(422, "unknown role filter")
    return fields


def group(value: dict) -> dict:
    """One provider's `CredentialList` as the frontend expects it."""
    return {
        "provider": value["provider"],
        "label": PROVIDERS[value["provider"]],
        "revision": value["revision"],
        "entries": [entry(item) for item in value["entries"]],
    }


def entry(value: dict) -> dict:
    """One `CredentialEntry` as the frontend expects it; `Saved` never claims provider authentication."""
    record = value["record"]
    return {
        "id": value["entry_id"],
        "provider": value["provider"],
        "type": next(ui for ui, route in TYPES.items() if route == value["route"]),
        "kind": value["kind"],
        "name": value["name"],
        "enabled": value["enabled"],
        "roles": value["roles"],
        "profiles": value["profiles"],
        "revision": value["revision"],
        "removed": value["removed"] is not None,
        "created": value["created"],
        "status": "expired"
        if record.get("status_reason") == "expired"
        else STATUS.get(record.get("status"), "invalid"),
        "statusReason": record.get("status_reason"),
        "fingerprint": record.get("fingerprint"),
        "version": record.get("version"),
        "lastUpdated": record.get("updated"),
        "lastUsed": record.get("last_used"),
        "expiresAt": record.get("expires_at"),
        "expiryKnown": bool(record.get("expiry_known")),
        "accountLabel": record.get("account_label"),
        "accountVerified": bool(record.get("verified_account")),
    }
