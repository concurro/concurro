"""Credential saving never contacts a provider or requires model coverage."""

import json
import unittest

from vault_fixtures import ALICE, BOB, VaultFixture

import vault

TOKENS = {
    "chatgpt-subscription": ("SYNTHETIC_CHATGPT_ALICE", "SYNTHETIC_CHATGPT_BOB"),
    "claude-subscription": ("SYNTHETIC_CLAUDE_ALICE", "SYNTHETIC_CLAUDE_BOB"),
}


class SubscriptionTests(VaultFixture, unittest.TestCase):
    """Exercise owner isolation, atomic replacement and account storage without inference."""

    def test_save_without_models_or_provider_requests(self):
        """Both subscription types store arbitrary tokens without claiming validation."""
        for route, tokens in TOKENS.items():
            for token, github, owner in zip(tokens, (ALICE, BOB), (101, 202), strict=True):
                body = {"secret": token}
                if route == "chatgpt-subscription":
                    body["account_id"] = "supplied-account-" + str(owner)
                reply = self.request("PUT", "/credentials/" + route, token=github, json=body)
                self.assertEqual(reply.status_code, 200, reply.text)
                entry = reply.json()
                self.assertIsNone(entry["models"])
                self.assertIsNone(entry["validated"])
                self.assertEqual(entry["status_reason"], "stored")
                stored = self.store.resolve(owner, route, entry["record_id"], entry["version"])
                self.assertEqual(stored["secret"], token)
                self.assertEqual(
                    stored["account"], {"chatgpt_account_id": body["account_id"]} if "account_id" in body else None
                )
                self.assertNotIn(token, reply.text)
        self.assertTrue(all(url == vault.GITHUB_USER for url, _ in self.transport.calls))
        self.assertTrue(all(event["type"] == "stored" for event in self.store.events()["events"]))

    def test_replacement_ignores_old_model_field_and_retires_previous_version(self):
        """Older clients' model selections cannot constrain or certify newly stored material."""
        first = self.put("claude-subscription", "SYNTHETIC_OLD").json()
        reply = self.request(
            "PUT", "/credentials/claude-subscription", json={"secret": "SYNTHETIC_NEW", "models": ["irrelevant-model"]}
        )
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json()["version"], 2)
        self.assertIsNone(reply.json()["models"])
        with self.assertRaises(vault.VaultError):
            self.store.resolve(101, "claude-subscription", first["record_id"], 1)

    def test_account_header_is_required_and_expiry_claims_are_ignored(self):
        """Only header syntax is checked; callers cannot assert trusted expiry or account identity."""
        for account in (None, "", "bad\r\nheader"):
            reply = self.request(
                "PUT", "/credentials/chatgpt-subscription", json={"secret": "SYNTHETIC", "account_id": account}
            )
            self.assertEqual(reply.status_code, 422)
        reply = self.request(
            "PUT",
            "/credentials/chatgpt-subscription",
            json={
                "secret": "SYNTHETIC",
                "account_id": "supplied",
                "expires_at": "2099-01-01T00:00:00Z",
                "expiry_known": True,
            },
        )
        self.assertEqual(reply.status_code, 200)
        self.assertFalse(reply.json()["expiry_known"])
        self.assertIsNone(reply.json()["verified_account"])
        self.assertNotIn('"supplied"', json.dumps(self.store.metadata(101)))

    def test_revision_fence_preserves_concurrent_revoke_and_replacement(self):
        """Removing network probes preserves the store's stale-write fence across connections."""
        route = "claude-subscription"
        revision = self.store.revision(101, route)
        other = vault.Store(self.directory / "vault.db", self.envelope)
        self.addCleanup(other.close)
        other.revoke(101, route)
        with self.assertRaises(vault.VaultError) as error:
            self.store.put(vault.User(101, "alice"), route, "STALE", expected_revision=revision)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.store.listing(101), [])
        revision = self.store.revision(101, route)
        other.put(vault.User(101, "alice"), route, "NEW")
        with self.assertRaises(vault.VaultError) as error:
            self.store.put(vault.User(101, "alice"), route, "STALE", expected_revision=revision)
        self.assertEqual(error.exception.status, 409)
        entry = self.store.listing(101)[0]
        self.assertEqual(self.store.resolve(101, route, entry["record_id"], 1)["secret"], "NEW")
