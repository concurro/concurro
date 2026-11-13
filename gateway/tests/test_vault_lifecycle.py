"""Phase2 lifecycle evidence with synthetic material and real SQLite/envelope transactions."""

import json
import os
import sqlite3
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from vault_fixtures import ALICE, OPENAI, VaultFixture

import vault


class LifecycleTests(VaultFixture, unittest.TestCase):
    """Check atomic retirement/events, expiry, legacy migration and key rotation."""

    def test_replacement_revoke_events_replay_without_material_or_duplicates(self):
        """Only accepted replacements retire versions; repeated revoke and restart preserve event identity."""
        first = self.put("openai-api-key", OPENAI).json()
        self.assertEqual(first["version"], 1)
        self.assertEqual(self.put("openai-api-key", "invalid-candidate").status_code, 422)
        self.assertEqual([e["type"] for e in self.store.events()["events"]], ["validated"])
        self.transport.accepted.add(OPENAI + "2")
        second = self.put("openai-api-key", OPENAI + "2").json()
        self.assertEqual(second["version"], 2)
        events = self.store.events()["events"]
        self.assertEqual([e["type"] for e in events], ["validated", "retired", "validated"])
        self.request("DELETE", "/credentials/openai-api-key")
        self.request("DELETE", "/credentials/openai-api-key")
        final = self.store.events()
        self.assertEqual([e["type"] for e in final["events"]], ["validated", "retired", "validated", "revoked"])
        self.assertEqual(self.store.events(events[-1]["sequence"])["events"], final["events"][-1:])
        with self.assertRaises(vault.VaultError):
            self.store.resolve(101, "openai-api-key", first["record_id"], 1)
        self.store.close()
        reopened = vault.Store(self.directory / "vault.db", self.envelope)
        self.store = reopened
        self.assertEqual(reopened.events(), final)
        for secret in (OPENAI, OPENAI + "2", ALICE):
            self.assertNotIn(secret, json.dumps(final))

    def test_secret_and_account_are_one_exact_subscription_version_without_registration(self):
        """Future subscription schemas do not enable public PUT or split token/account ownership."""
        token, account = "synthetic-cli-access", {"chatgpt_account_id": "synthetic-account-a"}
        entry = self.store.put(
            vault.User(101, "alice"), "chatgpt-subscription", token, ["gpt-fixture"], account=account
        )
        value = self.store.resolve(101, "chatgpt-subscription", entry["record_id"], entry["version"])
        self.assertEqual((value["secret"], value["account"]), (token, account))
        with self.assertRaises(vault.VaultError):
            self.store.resolve(202, "chatgpt-subscription", entry["record_id"], entry["version"])
        self.assertEqual(self.put("chatgpt-subscription", token).status_code, 404)
        for text in (
            json.dumps(self.store.listing(101)),
            json.dumps(self.store.metadata(101)),
            json.dumps(self.store.events()),
        ):
            self.assertNotIn(token, text)
            self.assertNotIn(account["chatgpt_account_id"], text)

    def test_known_expiry_denies_and_emits_once_unknown_expiry_does_not_guess(self):
        """A private resolution commits a known expiry retirement before returning denial."""
        now = [1000.0]
        self.store.clock = lambda: now[0]
        expiry = datetime.fromtimestamp(1010, UTC).isoformat()
        entry = self.store.put(
            vault.User(101, "alice"), "openai-api-key", OPENAI, ["fixture"], expires_at=expiry, expiry_known=True
        )
        self.assertEqual(self.store.resolve(101, "openai-api-key", entry["record_id"], 1)["secret"], OPENAI)
        now[0] = 1011
        for _ in range(2):
            with self.assertRaises(vault.VaultError) as error:
                self.store.resolve(101, "openai-api-key", entry["record_id"], 1)
            self.assertEqual(error.exception.status, 410)
        self.assertEqual([e["type"] for e in self.store.events()["events"]], ["validated", "unusable"])
        self.assertIsNone(self.store.sealed(101, "openai-api-key"))
        newer = self.store.put(vault.User(101, "alice"), "openai-api-key", OPENAI, ["fixture"])
        now[0] = 10**10
        self.assertEqual(self.store.resolve(101, "openai-api-key", newer["record_id"], 2)["secret"], OPENAI)

    def test_rotation_preserves_ciphertext_identity_and_rolls_back_missing_old_key(self):
        """Wrapping-key changes neither reset versions nor rewrite the provider token."""
        old, new = os.urandom(32), os.urandom(32)
        store = vault.Store(self.directory / "rotation.db", vault.Envelope(old))
        self.addCleanup(store.close)
        entry = store.put(vault.User(101, "alice"), "openai-api-key", OPENAI, ["fixture"])
        before = dict(store.db.execute("SELECT * FROM credentials").fetchone())
        with self.assertRaises(vault.ConfigError):
            store.rotate(vault.Envelope(new, "new"))
        self.assertEqual(store.resolve(101, "openai-api-key", entry["record_id"], 1)["secret"], OPENAI)
        events = store.events()
        envelope = vault.Envelope(new, "new", {"legacy": old})
        with patch.object(store, "event", side_effect=RuntimeError("synthetic crash")):
            with self.assertRaises(RuntimeError):
                store.rotate(envelope)
        self.assertEqual(store.events(), events)
        self.assertEqual(store.envelope.current, "legacy")
        self.assertEqual(store.rotate(envelope), 1)
        self.assertEqual(store.rotate(envelope), 0)
        after = dict(store.db.execute("SELECT * FROM credentials").fetchone())
        self.assertEqual(
            (before["ciphertext"], before["nonce"], before["version"]),
            (after["ciphertext"], after["nonce"], after["version"]),
        )
        self.assertNotEqual(before["wrapped_key"], after["wrapped_key"])
        self.assertEqual(store.resolve(101, "openai-api-key", entry["record_id"], 1)["secret"], OPENAI)

    def test_legacy_migration_opens_original_aad_and_never_invents_model_history(self):
        """Schema migration supplies key IDs and snapshot events while preserving unknown evidence."""
        path = self.directory / "legacy.db"
        secret = self.envelope.seal(OPENAI, vault.aad(101, "openai-api-key", 1))
        db = sqlite3.connect(path)
        db.execute(vault.SCHEMA)
        db.execute(
            "CREATE TABLE credential_metadata(user_id INTEGER,route TEXT,version INTEGER,models TEXT,retired TEXT,PRIMARY KEY(user_id,route,version))"
        )
        db.execute(
            "INSERT INTO credentials VALUES(101,'openai-api-key',1,'alice','valid',?,?,?,?,?,?)",
            (
                vault.fingerprint(OPENAI),
                secret.wrapped_key,
                secret.nonce,
                secret.ciphertext,
                "2000-01-01T00:00:00Z",
                "2000-01-01T00:00:00Z",
            ),
        )
        db.commit()
        db.close()
        store = vault.Store(path, self.envelope)
        self.addCleanup(store.close)
        self.assertEqual(
            store.resolve(101, "openai-api-key", vault.record_id(101, "openai-api-key", 1), 1)["secret"], OPENAI
        )
        record = store.metadata(101)["credentials"][0]
        self.assertIsNone(record["models"])
        self.assertIsNone(record["validated"])
        self.assertEqual([e["type"] for e in store.events()["events"]], ["snapshot"])

    def test_paginated_models_are_complete_or_explicitly_unknown(self):
        """A second Anthropic page contributes coverage; missing/cyclic cursors never become partial truth."""
        calls = []
        pages = [
            {"data": [{"id": "first-model"}], "has_more": True, "last_id": "cursor-one"},
            {"data": [{"id": "later-model"}], "has_more": False},
        ]

        def transport(url, headers):
            """Return provider-shaped pages while retaining actual encoded cursor requests."""
            calls.append((url, headers))
            return 200, json.dumps(pages[min(len(calls) - 1, len(pages) - 1)]).encode()

        validator = vault.Validator(transport)
        self.assertEqual(validator.models("anthropic-api-key", OPENAI), (True, ["first-model", "later-model"]))
        self.assertTrue(calls[0][0].endswith("?limit=1000"))
        self.assertTrue(calls[1][0].endswith("?limit=1000&after_id=cursor-one"))
        calls.clear()
        pages[:] = [{"data": [{"id": "first-model"}], "has_more": True, "last_id": "same"}]
        self.assertEqual(validator.models("anthropic-api-key", OPENAI), (True, None))
        self.assertEqual(len(calls), 2)
        calls.clear()
        pages[:] = [{"data": [{"id": "first-model"}], "has_more": True}]
        self.assertEqual(validator.models("anthropic-api-key", OPENAI), (True, None))
        self.assertEqual(len(calls), 1)
