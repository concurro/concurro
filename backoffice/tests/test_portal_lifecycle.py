"""Browser-facing lifecycle follows the real vault with synthetic GitHub/provider transports."""

import json
import sys
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from test_backoffice import USER, FakeOAuth, settings

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gateway/tests"))
from vault_fixtures import ALICE, BOB, OPENAI, VaultFixture

import vault  # noqa: E402
from backoffice.app import CSRF_HEADER, SESSION_COOKIE, create_app  # noqa: E402
from backoffice.auth import Sessions, User  # noqa: E402
from backoffice.credentials import Vault  # noqa: E402
from test_subscription_validation import TOKENS, Probes


class PortalLifecycleTests(VaultFixture, unittest.TestCase):
    """Owner-bound writes, validation preservation and candidate validation cross both real HTTP apps."""

    def browser(self, vault_client=None):
        """Reuse real owner/session/CSRF adapters while forwarding to the selected local vault app."""
        vault_client = vault_client or self.client

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
        return client, alice, bob

    def test_browser_to_vault_replacement_revoke_and_private_separation(self):
        """No browser owner field or service bearer can select another person's secret version."""
        client, alice, bob = self.browser()
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
                422,
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

    def test_both_subscription_forms_cross_real_bff_and_vault_with_two_owners(self):
        """Model/account candidate fields and replacement/revoke remain owner-bound through both apps."""
        probes = Probes()
        candidate_vault = TestClient(
            vault.create_app(self.store, self.identity, vault.Validator(self.transport, probe=probes))
        )
        client, alice, bob = self.browser(candidate_vault)
        routes = {"chatgpt_subscription": "chatgpt-subscription", "claude_subscription": "claude-subscription"}
        for ui, route in routes.items():
            for index, sid in enumerate((alice, bob)):
                client.cookies.clear()
                client.cookies.set(SESSION_COOKIE, sid)
                token = TOKENS[route][index]
                body = {"secret": token, "models": ["model-a", "model-b", "model-a"], "owner": 999}
                if ui == "chatgpt_subscription":
                    body["account_id"] = "account-alice" if index == 0 else "account-bob"
                path = "/api/me/credentials/" + ui
                self.assertEqual(client.put(path, json=body).status_code, 403)
                result = client.put(path, headers={CSRF_HEADER: "1"}, json=body)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertTrue(result.json()["registrationAvailable"])
                self.assertEqual(result.json()["models"], ["model-a", "model-b"])
                self.assertFalse(result.json()["expiryKnown"])
                self.assertFalse(result.json()["accountVerified"])
                self.assertNotIn(token, result.text)
                before = self.store.events()
                probes.mode[(token, "model-b")] = 503
                failure = client.put(path, headers={CSRF_HEADER: "1"}, json=body)
                self.assertEqual(failure.status_code, 502)
                self.assertEqual(self.store.events(), before)
                probes.mode.clear()
                replacement = client.put(path, headers={CSRF_HEADER: "1"}, json=body)
                self.assertEqual(replacement.json()["version"], 2)
                current = self.store.listing(101 if index == 0 else 202)
                self.assertTrue(any(row["route"] == route and row["version"] == 2 for row in current))
                self.assertEqual(client.delete(path, headers={CSRF_HEADER: "1"}).json()["status"], "revoked")
                self.assertEqual(client.delete(path, headers={CSRF_HEADER: "1"}).json()["status"], "revoked")
        self.assertEqual({row["user_id"] for row in self.rows()}, {101, 202})
        self.assertNotIn(999, {row["user_id"] for row in self.rows()})

    def test_candidate_timeout_exceeds_shared_total_model_deadline(self):
        """Actual HTTPX candidate requests allow every distinct model's full shared deadline plus margin."""
        from credential_routes import VALIDATION_SECONDS

        seen = []

        def record(request):
            """Inspect effective transport timeout without contacting a provider or vault."""
            seen.append(request.extensions["timeout"]["read"])
            return httpx.Response(200, json={"route": "claude-subscription", "status": "valid"})

        client = Vault("http://synthetic", httpx.Client(transport=httpx.MockTransport(record)))
        for count in (1, 3, 10):
            client.put(ALICE, "claude_subscription", "synthetic", ["model-" + str(i) for i in range(count)])
            self.assertGreater(seen[-1], VALIDATION_SECONDS * count)
