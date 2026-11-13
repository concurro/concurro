"""An isolated vault with fake GitHub and provider endpoints."""

import json
import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vault"))
from vault import GITHUB_USER, ROUTES, Envelope, Identity, Store, Validator, create_app

ALICE, BOB = "ghu_alice-token", "ghu_bob-token"
USERS = {ALICE: {"id": 101, "login": "alice"}, BOB: {"id": 202, "login": "bob"}}
ANTHROPIC = "sk-ant-api03-validsecretAAAA"
OPENAI = "sk-proj-validsecretBBBB"


class FakeTransport:
    """GitHub's /user and the providers' model listings; secrets in `accepted` validate."""

    def __init__(self):
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.accepted = {ANTHROPIC, OPENAI, ANTHROPIC + "2"}
        self.provider_status: int | None = None

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if url == GITHUB_USER:
            user = USERS.get(headers["Authorization"].removeprefix("Bearer "))
            return (200, json.dumps(user).encode()) if user else (401, b'{"message": "Bad credentials"}')
        if self.provider_status is not None:
            return self.provider_status, b""
        secret = headers.get("x-api-key") or headers["Authorization"].removeprefix("Bearer ")
        return (200, b'{"data": [], "has_more": false}') if secret in self.accepted else (401, b"{}")

    def github_calls(self):
        return [call for call in self.calls if call[0] == GITHUB_USER]


class VaultFixture:
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.now = 1000.0
        self.transport = FakeTransport()
        self.envelope = Envelope(os.urandom(32))
        self.store = Store(self.directory / "vault.db", self.envelope)
        self.addCleanup(self.store.close)
        self.identity = Identity(self.transport, clock=lambda: self.now)
        self.client = TestClient(create_app(self.store, self.identity, Validator(self.transport, ROUTES)))

    def request(self, method, path, token=ALICE, **kwargs):
        return self.client.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs)

    def put(self, route, secret, token=ALICE):
        return self.request("PUT", f"/credentials/{route}", token, json={"secret": secret})

    def rows(self):
        return [dict(row) for row in self.store.db.execute("SELECT * FROM credentials ORDER BY route, version")]
