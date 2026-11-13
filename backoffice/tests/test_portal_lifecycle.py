"""Browser-facing lifecycle follows the real vault with synthetic GitHub/provider transports."""

import json
import sys
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from test_backoffice import USER, FakeOAuth, settings

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gateway/tests"))
from vault_fixtures import ALICE, BOB, OPENAI, VaultFixture  # noqa: E402

from backoffice.app import CSRF_HEADER, SESSION_COOKIE, create_app  # noqa: E402
from backoffice.auth import Sessions, User  # noqa: E402
from backoffice.credentials import Vault  # noqa: E402


class PortalLifecycleTests(VaultFixture, unittest.TestCase):
    """Owner-bound writes, validation preservation and subscription exclusions cross both real HTTP apps."""

    def test_browser_to_vault_replacement_revoke_and_private_separation(self):
        """No browser owner field or service bearer can select another person's secret version."""
        vault_client = self.client

        def forward(request):
            """Forward BFF HTTP into the actual vault app, without replacing lifecycle decisions."""
            reply = vault_client.request(
                request.method, request.url.path, headers=dict(request.headers), content=request.content
            )
            return httpx.Response(reply.status_code, content=reply.content, headers=dict(reply.headers))

        api = Vault("http://vault.invalid", httpx.Client(transport=httpx.MockTransport(forward)))
        sessions = Sessions()
        alice = sessions.create(ALICE, USER, 3600)
        bob = sessions.create(BOB, User(202, "bob", "Bob", ""), 3600)
        client = TestClient(
            create_app(settings(public_url="http://testserver"), oauth=FakeOAuth(), vault=api, sessions=sessions)
        )
        client.cookies.set(SESSION_COOKIE, alice)
        path = "/api/me/credentials/openai_api"
        self.assertEqual(client.put(path, json={"secret": OPENAI}).status_code, 403)
        reply = client.put(path, headers={CSRF_HEADER: "1"}, json={"secret": OPENAI, "owner": 202})
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json()["version"], 1)
        self.assertTrue(reply.json()["registrationAvailable"])
        self.assertFalse(reply.json()["expiryKnown"])
        self.assertNotIn(OPENAI, reply.text)
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, bob)
        self.assertEqual(client.delete(path, headers={CSRF_HEADER: "1"}).status_code, 404)
        self.assertEqual(client.get("/api/me/credentials").json()["routes"][2]["status"], "missing")
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, alice)
        before = self.store.events()
        self.assertEqual(
            client.put(path, headers={CSRF_HEADER: "1"}, json={"secret": "wrong-candidate"}).status_code, 422
        )
        self.assertEqual(self.store.events(), before)
        self.assertEqual(
            client.put(path, headers={CSRF_HEADER: "1"}, json={"secret": {"synthetic-secret": "never-echo"}}).text,
            '{"detail":"malformed request"}',
        )
        for name in ("claude_subscription", "chatgpt_subscription"):
            self.assertEqual(
                client.put(
                    "/api/me/credentials/" + name, headers={CSRF_HEADER: "1"}, json={"secret": "synthetic-cli-token"}
                ).status_code,
                404,
            )
        self.transport.accepted.add(OPENAI + "replacement")
        replaced = client.put(path, headers={CSRF_HEADER: "1"}, json={"secret": OPENAI + "replacement"})
        self.assertEqual(replaced.json()["version"], 2)
        self.assertEqual(client.delete(path, headers={CSRF_HEADER: "1"}).json()["status"], "revoked")
        self.assertEqual(client.delete(path, headers={CSRF_HEADER: "1"}).json()["status"], "revoked")
        listing = client.get("/api/me/credentials").json()
        self.assertEqual(len(listing["routes"]), 4)
        self.assertEqual(listing["usage"], [])
        for marker in (OPENAI, ALICE, BOB, "synthetic-cli-token"):
            self.assertNotIn(marker, json.dumps(listing))
