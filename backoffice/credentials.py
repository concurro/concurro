"""Forward the user's credential requests to the vault with their own GitHub token.

The vault resolves the user itself; the backoffice never stores or reads back a secret.
"""

import httpx

from credential_routes import VALIDATION_SECONDS

from .auth import BearerAuth, TokenCredential

# Stable UI identifiers map to exact fixed vault/provider routes.
ROUTES = (
    ("anthropic_api", "anthropic-api-key", "Anthropic API", "anthropic", "API key"),
    ("claude_subscription", "claude-subscription", "Claude subscription", "anthropic", "Subscription"),
    ("openai_api", "openai-api-key", "OpenAI API", "openai", "API key"),
    ("chatgpt_subscription", "chatgpt-subscription", "ChatGPT subscription", "openai", "Subscription"),
)
VAULT_ROUTES = {ui: vault for ui, vault, *_ in ROUTES if vault}
STATUS = {"valid": "valid", "invalid": "invalid", "revoked": "revoked"}
USAGE = "Personally funded providers use validated models from this personal version. Each issue selects funding per provider; organization-funded calls keep their organization credentials."


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

    def routes(self, token: str | TokenCredential) -> list[dict]:
        """Every UI route with its stored status; routes the vault doesn't hold show as missing."""
        stored = {entry["route"]: entry for entry in self.call("GET", "/credentials", token) or []}
        return [route(ui, stored.get(vault) if vault else None) for ui, vault, *_ in ROUTES]

    def put(
        self,
        token: str | TokenCredential,
        ui_route: str,
        secret: str,
        models: list[str] | None = None,
        account_id: str | None = None,
    ) -> dict:
        """Forward only this owner's candidate and route metadata; never retain the secret."""
        body = {"secret": secret}
        if ui_route in ("chatgpt_subscription", "claude_subscription"):
            body.update(models=models, account_id=account_id)
        timeout = max(30, VALIDATION_SECONDS * len(models or []) + 15)
        return route(
            ui_route, self.call("PUT", f"/credentials/{self.vault_route(ui_route)}", token, body, timeout=timeout)
        )

    def revoke(self, token: str | TokenCredential, ui_route: str) -> dict:
        return route(ui_route, self.call("DELETE", f"/credentials/{self.vault_route(ui_route)}", token))

    @staticmethod
    def vault_route(ui_route: str) -> str:
        if ui_route not in VAULT_ROUTES:
            raise VaultError(404, "this credential type can't be stored yet")
        return VAULT_ROUTES[ui_route]


def route(ui_route: str, entry: dict | None) -> dict:
    """One `CredentialRoute` as the frontend expects it."""
    _, vault, label, provider, kind = next(r for r in ROUTES if r[0] == ui_route)
    result: dict = {
        "id": ui_route,
        "route": vault,
        "label": label,
        "provider": provider,
        "kind": kind,
        "registrationAvailable": vault is not None,
    }
    if entry is None:
        result["status"] = "missing"
        result["note"] = USAGE if vault else "Subscription credentials aren't supported yet."
        return result
    result["status"] = (
        "expired" if entry.get("status_reason") == "expired" else STATUS.get(entry.get("status"), "invalid")
    )
    result["fingerprint"] = entry.get("fingerprint")
    result["lastValidated"] = entry.get("validated")
    result["lastUsed"] = entry.get("last_used")
    result["expiresAt"] = entry.get("expires_at")
    result["expiryKnown"] = bool(entry.get("expiry_known"))
    result["models"] = entry.get("models")
    result["version"] = entry.get("version")
    result["statusReason"] = entry.get("status_reason")
    result["accountLabel"] = entry.get("account_label")
    result["accountVerified"] = bool(entry.get("verified_account"))
    result["note"] = USAGE
    return result
