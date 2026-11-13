"""The credentials vault: identity, validation, versioned envelope storage and revocation."""

import hashlib
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.exceptions import InvalidTag

from vault_fixtures import ALICE, ANTHROPIC, BOB, OPENAI, VaultFixture

import vault
from vault import ConfigError, aad, fingerprint, load_master_key


class IdentityTest(VaultFixture, unittest.TestCase):
    def test_resolves_user_and_caches_by_token_digest(self):
        self.assertEqual(self.request("GET", "/credentials").json(), [])
        self.request("GET", "/credentials")
        self.assertEqual(len(self.transport.github_calls()), 1)
        self.assertEqual(list(self.identity.cache), [hashlib.sha256(ALICE.encode()).hexdigest()])
        self.assertEqual(self.identity.cache[hashlib.sha256(ALICE.encode()).hexdigest()][1].login, "alice")

    def test_cache_expires_after_five_minutes(self):
        self.request("GET", "/credentials")
        self.now += 299
        self.request("GET", "/credentials")
        self.assertEqual(len(self.transport.github_calls()), 1)
        self.now += 2
        self.request("GET", "/credentials")
        self.assertEqual(len(self.transport.github_calls()), 2)

    def test_users_are_isolated(self):
        self.put("anthropic-api-key", ANTHROPIC)
        self.assertEqual(self.request("GET", "/credentials", token=BOB).json(), [])
        self.assertEqual(self.request("DELETE", "/credentials/anthropic-api-key", token=BOB).status_code, 404)

    def test_invalid_token_is_401_and_not_cached(self):
        response = self.request("GET", "/credentials", token="ghu_bogus")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(self.identity.cache, {})
        self.assertEqual(self.put("anthropic-api-key", ANTHROPIC, token="ghu_bogus").status_code, 401)
        self.assertEqual(self.rows(), [])

    def test_missing_or_malformed_authorization_is_401(self):
        self.assertEqual(self.client.get("/credentials").status_code, 401)
        self.assertEqual(self.client.get("/credentials", headers={"Authorization": f"token {ALICE}"}).status_code, 401)
        self.assertEqual(self.transport.github_calls(), [])

    def test_github_outage_is_502(self):
        self.identity.transport = lambda url, headers: (500, b"")
        self.assertEqual(self.request("GET", "/credentials").status_code, 502)

    def test_health_needs_no_token(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})


class ValidationTest(VaultFixture, unittest.TestCase):
    def test_provider_calls(self):
        self.put("anthropic-api-key", ANTHROPIC)
        self.put("openai-api-key", OPENAI)
        anthropic, openai = [call for call in self.transport.calls if call[0] != vault.GITHUB_USER]
        self.assertEqual(
            anthropic,
            (
                "https://api.anthropic.com/v1/models?limit=1000",
                {"x-api-key": ANTHROPIC, "anthropic-version": "2023-06-01"},
            ),
        )
        self.assertEqual(openai, ("https://api.openai.com/v1/models", {"Authorization": f"Bearer {OPENAI}"}))

    def test_rejected_secret_is_422_and_stores_nothing(self):
        response = self.put("anthropic-api-key", "sk-ant-wrong")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.request("GET", "/credentials").json(), [])

    def test_rejected_replacement_keeps_current_version(self):
        self.put("anthropic-api-key", ANTHROPIC)
        before = self.rows()
        self.assertEqual(self.put("anthropic-api-key", "sk-ant-wrong").status_code, 422)
        self.assertEqual(self.rows(), before)

    def test_unsupported_characters_are_422_without_a_provider_call(self):
        for secret in ("", "   ", "sk ant", "sk-ant\r\nX-Injected: 1"):
            self.assertEqual(self.put("anthropic-api-key", secret).status_code, 422)
        self.assertEqual([c for c in self.transport.calls if c[0] != vault.GITHUB_USER], [])
        self.assertEqual(self.rows(), [])

    def test_provider_outage_is_502_and_stores_nothing(self):
        self.transport.provider_status = 503
        self.assertEqual(self.put("openai-api-key", OPENAI).status_code, 502)
        self.assertEqual(self.rows(), [])

    def test_unknown_route_is_404(self):
        self.assertEqual(self.put("gemini-api-key", ANTHROPIC).status_code, 404)
        self.assertEqual(self.request("DELETE", "/credentials/gemini-api-key").status_code, 404)


class StorageTest(VaultFixture, unittest.TestCase):
    def test_put_returns_listing_entry(self):
        entry = self.put("anthropic-api-key", ANTHROPIC).json()
        self.assertEqual(
            set(entry),
            {
                "route",
                "status",
                "fingerprint",
                "version",
                "updated",
                "record_id",
                "kind",
                "status_reason",
                "validated",
                "expires_at",
                "expiry_known",
                "last_used",
                "account_label",
                "verified_account",
                "models",
            },
        )
        digest = hashlib.sha256(ANTHROPIC.encode()).hexdigest()
        self.assertEqual(entry["fingerprint"], f"AAAA:{digest[:8]}")
        self.assertEqual((entry["route"], entry["status"], entry["version"]), ("anthropic-api-key", "valid", 1))
        self.assertEqual(self.request("GET", "/credentials").json(), [entry])

    def test_versioning(self):
        first = self.put("anthropic-api-key", ANTHROPIC).json()
        second = self.put("anthropic-api-key", ANTHROPIC + "2").json()
        self.put("openai-api-key", OPENAI)
        self.assertEqual(second["version"], 2)
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        listing = self.request("GET", "/credentials").json()
        self.assertEqual(
            [(e["route"], e["version"]) for e in listing], [("anthropic-api-key", 2), ("openai-api-key", 1)]
        )
        anthropic = [row for row in self.rows() if row["route"] == "anthropic-api-key"]
        self.assertEqual([row["version"] for row in anthropic], [1, 2])
        # Only the current version keeps ciphertext.
        self.assertIsNone(anthropic[0]["ciphertext"])
        self.assertIsNone(anthropic[0]["wrapped_key"])
        version, sealed = self.store.sealed(101, "anthropic-api-key") or (0, None)
        self.assertEqual(version, 2)
        assert sealed is not None
        self.assertEqual(self.envelope.open(sealed, aad(101, "anthropic-api-key", 2)), ANTHROPIC + "2")

    def test_revocation_destroys_ciphertext(self):
        self.put("anthropic-api-key", ANTHROPIC)
        self.put("anthropic-api-key", ANTHROPIC + "2")
        entry = self.request("DELETE", "/credentials/anthropic-api-key").json()
        self.assertEqual((entry["status"], entry["version"]), ("revoked", 2))
        self.assertEqual(self.request("GET", "/credentials").json(), [entry])
        for row in self.rows():
            self.assertEqual((row["wrapped_key"], row["nonce"], row["ciphertext"]), (None, None, None))
        self.assertIsNone(self.store.sealed(101, "anthropic-api-key"))
        self.assertEqual(self.request("DELETE", "/credentials/anthropic-api-key").json(), entry)
        renewed = self.put("anthropic-api-key", ANTHROPIC).json()
        self.assertEqual((renewed["status"], renewed["version"]), ("valid", 3))

    def test_revoking_missing_credential_is_404(self):
        self.assertEqual(self.request("DELETE", "/credentials/openai-api-key").status_code, 404)

    def test_ciphertext_is_bound_to_its_record(self):
        self.put("anthropic-api-key", ANTHROPIC)
        _, sealed = self.store.sealed(101, "anthropic-api-key") or (0, None)
        assert sealed is not None
        with self.assertRaises(InvalidTag):
            self.envelope.open(sealed, aad(202, "anthropic-api-key", 1))
        with self.assertRaises(InvalidTag):
            vault.Envelope(os.urandom(32)).open(sealed, aad(101, "anthropic-api-key", 1))


class NoReadBackTest(VaultFixture, unittest.TestCase):
    def test_secrets_and_tokens_never_leave_the_vault(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger().debug("capture")
            responses = [
                self.put("anthropic-api-key", ANTHROPIC),
                self.put("openai-api-key", "sk-proj-rejectedCCCC"),
                self.request("PUT", "/credentials/openai-api-key", json={"secret": ["sk-proj-leakDDDD"]}),
                self.request("GET", "/credentials"),
                self.request("GET", "/credentials/anthropic-api-key"),
                self.request("DELETE", "/credentials/anthropic-api-key"),
            ]
        self.assertEqual(responses[2].status_code, 422)
        self.assertEqual(responses[4].status_code, 405)
        for text in [r.text for r in responses] + [str(dict(r.headers)) for r in responses] + logs.output:
            for secret in (ANTHROPIC, "sk-proj-rejectedCCCC", "sk-proj-leakDDDD", ALICE):
                self.assertNotIn(secret, text)

    def test_database_holds_no_plaintext(self):
        self.put("anthropic-api-key", ANTHROPIC)
        self.store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        raw = (self.directory / "vault.db").read_bytes()
        self.assertNotIn(ANTHROPIC.encode(), raw)
        self.assertNotIn(ALICE.encode(), raw)
        self.assertIn(fingerprint(ANTHROPIC).encode(), raw)


class MasterKeyTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "master-key"

    def write(self, content, mode=0o600):
        self.path.unlink(missing_ok=True)
        self.path.write_text(content)
        self.path.chmod(mode)

    def test_loads_owner_only_hex_key(self):
        key = os.urandom(32)
        for mode in (0o400, 0o600):
            self.write(key.hex() + "\n", mode)
            self.assertEqual(load_master_key(self.path), key)

    def test_missing_file_is_refused(self):
        with self.assertRaisesRegex(ConfigError, "missing"):
            load_master_key(self.path)

    def test_loose_permissions_are_refused(self):
        for mode in (0o640, 0o604, 0o644, 0o660):
            self.write(os.urandom(32).hex(), mode)
            with self.assertRaisesRegex(ConfigError, "must be owned"):
                load_master_key(self.path)

    def test_other_owner_is_refused(self):
        self.write(os.urandom(32).hex())
        with patch.object(vault.os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(ConfigError, "must be owned"):
                load_master_key(self.path)

    def test_symlink_and_bad_content_are_refused(self):
        self.write("not hex")
        with self.assertRaisesRegex(ConfigError, "64 hex"):
            load_master_key(self.path)
        self.write(os.urandom(16).hex())
        with self.assertRaisesRegex(ConfigError, "64 hex"):
            load_master_key(self.path)
        link = self.path.with_name("link")
        link.symlink_to(self.path)
        with self.assertRaisesRegex(ConfigError, "cannot be opened"):
            load_master_key(link)

    def test_main_exits_without_a_valid_key(self):
        env = {"VAULT_MASTER_KEY_FILE": str(self.path), "VAULT_STATE_DIR": str(self.path.parent / "state")}
        with patch.dict(os.environ, env), patch.object(vault.os, "umask"):
            with self.assertRaisesRegex(SystemExit, "missing"):
                vault.main()
            self.write(os.urandom(32).hex(), 0o644)
            with self.assertRaisesRegex(SystemExit, "must be owned"):
                vault.main()
        self.assertFalse((self.path.parent / "state").exists())


if __name__ == "__main__":
    unittest.main()
