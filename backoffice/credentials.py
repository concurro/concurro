"""Forward the user's credential requests to the vault with their own GitHub token.

The vault resolves the user itself; the backoffice never stores or reads back a secret.
"""

import httpx

# The UI's route identifiers, the vault's, and their labels. Subscriptions aren't stored yet.
ROUTES = (
    ("anthropic_api", "anthropic-api-key", "Anthropic API", "anthropic", "API key"),
    ("claude_subscription", None, "Claude subscription", "anthropic", "Subscription"),
    ("openai_api", "openai-api-key", "OpenAI API", "openai", "API key"),
    ("chatgpt_subscription", None, "ChatGPT subscription", "openai", "Subscription"),
)
VAULT_ROUTES = {ui: vault for ui, vault, *_ in ROUTES if vault}
STATUS = {"valid": "valid", "invalid": "expired", "revoked": "revoked"}
UNUSED = "Stored and validated; runs don't use personal credentials yet."


class VaultError(Exception):
    """The vault refused or failed; `status` is the HTTP status to return, `reason` is safe to show."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


class Vault:
    def __init__(self, url: str | None, client: httpx.Client | None = None):
        self.url = url.rstrip("/") if url else None
        self.client = client or httpx.Client(timeout=30)

    def call(self, method: str, path: str, token: str, body: dict | None = None):
        if not self.url:
            raise VaultError(503, "the credentials vault is not configured")
        try:
            response = self.client.request(
                method, self.url + path, headers={"Authorization": "Bearer " + token}, json=body
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

    def routes(self, token: str) -> list[dict]:
        """Every UI route with its stored status; routes the vault doesn't hold show as missing."""
        stored = {entry["route"]: entry for entry in self.call("GET", "/credentials", token) or []}
        return [route(ui, stored.get(vault) if vault else None) for ui, vault, *_ in ROUTES]

    def put(self, token: str, ui_route: str, secret: str) -> dict:
        return route(
            ui_route, self.call("PUT", f"/credentials/{self.vault_route(ui_route)}", token, {"secret": secret})
        )

    def revoke(self, token: str, ui_route: str) -> dict:
        return route(ui_route, self.call("DELETE", f"/credentials/{self.vault_route(ui_route)}", token))

    @staticmethod
    def vault_route(ui_route: str) -> str:
        if ui_route not in VAULT_ROUTES:
            raise VaultError(404, "this credential type can't be stored yet")
        return VAULT_ROUTES[ui_route]


def route(ui_route: str, entry: dict | None) -> dict:
    """One `CredentialRoute` as the frontend expects it."""
    _, vault, label, provider, kind = next(r for r in ROUTES if r[0] == ui_route)
    result: dict = {"id": ui_route, "label": label, "provider": provider, "kind": kind}
    if entry is None:
        result["status"] = "missing"
        result["note"] = UNUSED if vault else "Subscription credentials aren't supported yet."
        return result
    result["status"] = STATUS.get(entry.get("status"), "expired")
    result["fingerprint"] = entry.get("fingerprint")
    result["lastValidated"] = entry.get("updated")
    result["note"] = UNUSED
    return result
