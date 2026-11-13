"""Private metadata prerequisite: no user impersonation, ciphertext or token-returning API."""

import unittest

from fastapi.testclient import TestClient

from vault_fixtures import ALICE, ANTHROPIC, BOB, OPENAI, VaultFixture

import vault

SERVICE = "a" * 64


class MetadataTests(VaultFixture, unittest.TestCase):
    """Exercise the real HTTP dependencies and versioned metadata store with synthetic tokens."""

    def setUp(self):
        """Enable a separate fake service bearer without changing normal user registration."""
        super().setUp()
        self.client = TestClient(vault.create_app(self.store, self.identity, vault.Validator(self.transport), SERVICE))

    def test_only_service_bearer_can_read_other_user_metadata(self):
        """User tokens cannot select another owner; the service receives only the metadata allow-list."""
        self.put("openai-api-key", OPENAI)
        self.put("anthropic-api-key", ANTHROPIC, token=BOB)
        for token in (ALICE, BOB, "wrong"):
            self.assertEqual(self.request("GET", "/metadata/101", token=token).status_code, 401)
        result = self.request("GET", "/metadata/101", token=SERVICE).json()
        self.assertTrue(result["history_complete"])
        self.assertEqual([m["route"] for m in result["credentials"]], ["openai-api-key"])
        self.assertEqual(
            set(result["credentials"][0]),
            {
                "record_id",
                "user_id",
                "route",
                "version",
                "status",
                "models",
                "created",
                "updated",
                "kind",
                "status_reason",
                "validated",
                "expires_at",
                "expiry_known",
                "token_revision",
            },
        )
        self.assertEqual(result["credentials"][0]["models"], [])
        for secret in (ALICE, BOB, OPENAI, ANTHROPIC, SERVICE, vault.fingerprint(OPENAI)):
            self.assertNotIn(secret, str(result))

    def test_service_token_is_never_forwarded_to_github_or_provider(self):
        """The metadata principal cannot use user credential routes, including mistaken writes."""
        for method, path, kwargs in (
            ("GET", "/credentials", {}),
            ("PUT", "/credentials/openai-api-key", {"json": {"secret": OPENAI}}),
        ):
            self.assertEqual(self.request(method, path, token=SERVICE, **kwargs).status_code, 401)
        self.assertEqual(self.transport.calls, [])

    def test_asof_replacement_preserves_original_route_evidence(self):
        """A later replacement cannot make credentials available retroactively at task initiation."""
        user = vault.User(101, "alice")
        self.store.put(user, "openai-api-key", OPENAI, ["model-old"])
        self.store.put(user, "openai-api-key", OPENAI + "2", ["model-new"])
        self.store.db.execute("UPDATE credentials SET created='2000-01-01T00:00:00Z' WHERE version=1")
        self.store.db.execute("UPDATE credentials SET created='2000-01-03T00:00:00Z' WHERE version=2")
        self.store.db.execute("UPDATE credential_metadata SET retired='2000-01-03T00:00:00Z' WHERE version=1")
        old = self.request("GET", "/metadata/101?at=2000-01-02T00:00:00Z", token=SERVICE).json()["credentials"]
        self.assertEqual([(m["version"], m["status"], m["models"]) for m in old], [(1, "valid", ["model-old"])])
        before = self.request("GET", "/metadata/101?at=1999-01-01T00:00:00Z", token=SERVICE).json()
        self.assertEqual(before["credentials"], [])

    def test_unknown_legacy_history_does_not_invent_an_available_grant(self):
        """Destroyed pre-migration material without retirement evidence remains explicitly unknown."""
        self.put("openai-api-key", OPENAI)
        self.store.db.execute("DELETE FROM credential_metadata")
        self.store.db.execute("UPDATE credentials SET ciphertext=NULL,created='2000-01-01T00:00:00Z'")
        result = self.request("GET", "/metadata/101?at=2001-01-01T00:00:00Z", token=SERVICE).json()
        self.assertFalse(result["history_complete"])
        self.assertEqual(result["credentials"][0]["status"], "unknown")

    def test_malformed_metadata_and_subscription_writes_never_echo_inputs(self):
        """Reject missing candidate metadata and preserve sanitized validation/error responses."""
        for path in ("/metadata/not-a-user", "/metadata/101?at=synthetic-sensitive-input"):
            response = self.request("GET", path, token=SERVICE)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("synthetic-sensitive-input", response.text)
        for route in ("chatgpt-subscription", "claude-subscription"):
            self.assertEqual(self.put(route, OPENAI).status_code, 422)
