"""An isolated vault with fake GitHub identity and no provider transport."""

import json
import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vault"))
from vault import GITHUB_USER, Envelope, Identity, Store, create_app

ALICE, BOB = "ghu_alice-token", "ghu_bob-token"
USERS = {ALICE: {"id": 101, "login": "alice"}, BOB: {"id": 202, "login": "bob"}}
ANTHROPIC = "sk-ant-api03-validsecretAAAA"
OPENAI = "sk-proj-validsecretBBBB"


class FakeTransport:
    """Serve synthetic GitHub identities and reject unexpected network calls."""

    def __init__(self):
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if url == GITHUB_USER:
            user = USERS.get(headers["Authorization"].removeprefix("Bearer "))
            return (200, json.dumps(user).encode()) if user else (401, b'{"message": "Bad credentials"}')
        raise AssertionError("unexpected provider request")

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
        self.client = TestClient(create_app(self.store, self.identity))

    def request(self, method, path, token=ALICE, **kwargs):
        return self.client.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs)

    def put(self, route, secret, token=ALICE):
        return self.request("PUT", f"/credentials/{route}", token, json={"secret": secret})

    def rows(self):
        return [dict(row) for row in self.store.db.execute("SELECT * FROM credentials ORDER BY route, version")]
