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
                body = {}
                if route == "chatgpt-subscription":
                    body["account_id"] = "supplied-account-" + str(owner)
                reply = self.put(route, token, token=github, **body)
                self.assertEqual(reply.status_code, 200, reply.text)
                entry = reply.json()["record"]
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
        self.assertTrue(all(event["type"] in ("stored", "policy") for event in self.store.events()["events"]))

    def test_replacement_ignores_old_model_field_and_retires_previous_version(self):
        """Older clients' model selections cannot constrain or certify newly stored material."""
        first = self.put("claude-subscription", "SYNTHETIC_OLD").json()
        reply = self.replace(first, "SYNTHETIC_NEW", models=["irrelevant-model"])
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json()["record"]["version"], 2)
        self.assertIsNone(reply.json()["record"]["models"])
        with self.assertRaises(vault.VaultError):
            self.store.resolve(101, "claude-subscription", first["record"]["record_id"], 1)

    def test_account_header_is_required_and_expiry_claims_are_ignored(self):
        """Only header syntax is checked; callers cannot assert trusted expiry or account identity."""
        for account in (None, "", "bad\r\nheader"):
            self.assertEqual(self.put("chatgpt-subscription", "SYNTHETIC", account_id=account).status_code, 422)
        reply = self.put(
            "chatgpt-subscription",
            "SYNTHETIC",
            account_id="supplied",
            expires_at="2099-01-01T00:00:00Z",
            expiry_known=True,
        )
        self.assertEqual(reply.status_code, 200)
        self.assertFalse(reply.json()["record"]["expiry_known"])
        self.assertIsNone(reply.json()["record"]["verified_account"])
        self.assertNotIn('"supplied"', json.dumps(self.store.metadata(101)))
        # A replacement keeps the token and its account header one version.
        self.assertEqual(self.replace(reply.json(), "SYNTHETIC_2").status_code, 422)
        renewed = self.replace(reply.json(), "SYNTHETIC_2", account_id="other").json()["record"]
        stored = self.store.resolve(101, "chatgpt-subscription", renewed["record_id"], renewed["version"])
        self.assertEqual((stored["secret"], stored["account"]), ("SYNTHETIC_2", {"chatgpt_account_id": "other"}))

    def test_entry_revisions_fence_stale_writes_without_blocking_other_entries(self):
        """A write compares only its own entry's revision, across connections; removal stays final."""
        alice, route = vault.User(101, "alice"), "claude-subscription"
        first, second = (self.store.add(alice, route, token, name=token) for token in ("FIRST", "SECOND"))
        other = vault.Store(self.directory / "vault.db", self.envelope)
        self.addCleanup(other.close)
        other.edit(101, first["entry_id"], revision=first["revision"], name="Renamed")
        with self.assertRaises(vault.VaultError) as error:
            self.store.replace(alice, first["entry_id"], "STALE", revision=first["revision"])
        self.assertEqual(error.exception.status, 409)
        renewed = self.store.replace(alice, second["entry_id"], "NEW", revision=second["revision"])["record"]
        self.assertEqual(self.store.resolve(101, route, renewed["record_id"], renewed["version"])["secret"], "NEW")
        other.remove(101, first["entry_id"], first["revision"] + 1)
        for write in (
            lambda: self.store.replace(alice, first["entry_id"], "STALE", revision=first["revision"] + 1),
            lambda: self.store.edit(101, first["entry_id"], revision=first["revision"] + 1, enabled=True),
        ):
            with self.assertRaises(vault.VaultError) as error:
                write()
            self.assertEqual(error.exception.status, 410)
        self.assertEqual([e["name"] for e in self.store.group(101, "anthropic")["entries"]], ["SECOND"])
