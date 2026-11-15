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
        self.assertEqual(self.entries(), [])
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
        entry = self.put("anthropic-api-key", ANTHROPIC).json()
        self.assertEqual(self.entries(BOB), [])
        path = "/credentials/" + entry["entry_id"]
        edit = {"revision": entry["revision"], "name": "Taken", "mutation_id": "m" * 16}
        self.assertEqual(self.remove(entry, token=BOB).status_code, 404)
        self.assertEqual(self.request("PATCH", path, token=BOB, json=edit).status_code, 404)
        self.assertEqual(self.replace(entry, OPENAI, token=BOB).status_code, 404)
        order = {"revision": 1, "entries": [entry["entry_id"]], "mutation_id": "o" * 16}
        self.assertEqual(self.request("PUT", "/credentials/order/anthropic", token=BOB, json=order).status_code, 409)
        self.assertEqual(self.entries(), [entry])

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


class SaveTest(VaultFixture, unittest.TestCase):
    def test_saves_arbitrary_keys_without_contacting_provider(self):
        """API key saves neither test authentication nor discover models."""
        for route in ("anthropic-api-key", "openai-api-key"):
            entry = self.put(route, "unverified-token")
            self.assertEqual(entry.status_code, 200)
            reply = self.replace(entry.json(), "replacement-token")
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(reply.json()["record"]["version"], 2)
            self.assertIsNone(reply.json()["record"]["validated"])
        self.assertTrue(all(url == vault.GITHUB_USER for url, _ in self.transport.calls))

    def test_unsupported_characters_are_422_without_a_provider_call(self):
        for secret in ("", "   ", "sk ant", "sk-ant\r\nX-Injected: 1"):
            self.assertEqual(self.put("anthropic-api-key", secret).status_code, 422)
        self.assertEqual([c for c in self.transport.calls if c[0] != vault.GITHUB_USER], [])
        self.assertEqual(self.rows(), [])

    def test_unknown_route_or_entry_is_refused(self):
        self.assertEqual(self.put("gemini-api-key", ANTHROPIC).status_code, 422)
        self.assertEqual(self.remove({"entry_id": "gemini-api-key", "revision": 1}).status_code, 404)
        self.assertEqual(
            self.request("PUT", "/credentials/anthropic-api-key", json={"secret": ANTHROPIC}).status_code, 405
        )
        self.assertEqual(self.rows(), [])


class StorageTest(VaultFixture, unittest.TestCase):
    def test_add_returns_listing_entry(self):
        entry = self.put("anthropic-api-key", ANTHROPIC, name="  Anthropic credits ").json()
        self.assertEqual(
            set(entry),
            {
                "entry_id",
                "provider",
                "route",
                "kind",
                "name",
                "enabled",
                "roles",
                "profiles",
                "revision",
                "position",
                "created",
                "updated",
                "removed",
                "record",
            },
        )
        self.assertEqual(
            set(entry["record"]),
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
        self.assertEqual(entry["record"]["fingerprint"], f"AAAA:{digest[:8]}")
        self.assertEqual(
            (entry["name"], entry["provider"], entry["kind"], entry["enabled"], entry["roles"], entry["profiles"]),
            ("Anthropic credits", "anthropic", "api_key", True, [], []),
        )
        self.assertEqual((entry["record"]["status"], entry["record"]["version"], entry["position"]), ("valid", 1, 0))
        listing = self.request("GET", "/credentials").json()
        self.assertEqual(
            [(g["provider"], g["revision"], g["entries"]) for g in listing["providers"]],
            [("anthropic", 1, [entry]), ("openai", 0, [])],
        )
        self.assertEqual(len(listing["types"]), 4)

    def test_versioning(self):
        first = self.put("anthropic-api-key", ANTHROPIC).json()
        second = self.replace(first, ANTHROPIC + "2").json()
        self.put("openai-api-key", OPENAI)
        self.assertEqual((second["entry_id"], second["record"]["version"]), (first["entry_id"], 2))
        self.assertNotEqual(first["record"]["fingerprint"], second["record"]["fingerprint"])
        self.assertEqual(
            [(e["route"], e["record"]["version"]) for e in self.entries()],
            [("anthropic-api-key", 2), ("openai-api-key", 1)],
        )
        anthropic = [row for row in self.rows() if row["route"] == "anthropic-api-key"]
        self.assertEqual([row["version"] for row in anthropic], [1, 2])
        # Only the current version keeps ciphertext.
        self.assertIsNone(anthropic[0]["ciphertext"])
        self.assertIsNone(anthropic[0]["wrapped_key"])
        self.assertEqual(
            self.envelope.open(self.sealed(anthropic[1]), aad(101, "anthropic-api-key", 2)), ANTHROPIC + "2"
        )

    def test_same_route_entries_keep_independent_material_and_one_version_sequence(self):
        """Versions are allocated per owner/route, so each entry's record identity and AAD stay unique."""
        personal = self.put("claude-subscription", "personal-token", name="Claude personal").json()
        work = self.put("claude-subscription", "work-token", name="Claude work").json()
        self.assertEqual([e["record"]["version"] for e in (personal, work)], [1, 2])
        self.assertNotEqual(personal["record"]["record_id"], work["record"]["record_id"])
        renewed = self.replace(personal, "personal-token-2").json()
        self.assertEqual(renewed["record"]["version"], 3)
        resolve = lambda entry: self.store.resolve(  # noqa: E731
            101, "claude-subscription", entry["record"]["record_id"], entry["record"]["version"]
        )["secret"]
        self.assertEqual((resolve(work), resolve(renewed)), ("work-token", "personal-token-2"))
        removed = self.remove(renewed).json()
        self.assertEqual((removed["record"]["status"], removed["position"]), ("revoked", None))
        self.assertEqual(resolve(work), "work-token")
        self.assertEqual([e["name"] for e in self.entries()], ["Claude work"])
        self.assertEqual(self.replace(self.entries()[0], "work-token-2").json()["record"]["version"], 4)

    def test_removal_destroys_ciphertext_and_leaves_a_tombstone(self):
        first = self.put("anthropic-api-key", ANTHROPIC).json()
        second = self.replace(first, ANTHROPIC + "2").json()
        path = "/credentials/" + first["entry_id"]
        self.assertEqual(self.request("DELETE", path, params={"revision": first["revision"]}).status_code, 409)
        entry = self.request("DELETE", path, params={"revision": second["revision"]}).json()
        self.assertEqual((entry["record"]["status"], entry["record"]["version"]), ("revoked", 2))
        self.assertIsNotNone(entry["removed"])
        self.assertEqual(self.entries(), [])
        for row in self.rows():
            self.assertEqual((row["wrapped_key"], row["nonce"], row["ciphertext"]), (None, None, None))
        # Removal always names the revision last read; repeating it returns the same tombstone.
        self.assertEqual(self.request("DELETE", path).status_code, 422)
        self.assertEqual(self.remove(second).json(), entry)
        # An older write cannot resurrect the entry; a new save is a new entry with the next route version.
        self.assertEqual(self.replace(second, ANTHROPIC).status_code, 410)
        edit = {"revision": second["revision"], "enabled": True, "mutation_id": "m" * 16}
        self.assertEqual(self.request("PATCH", path, json=edit).status_code, 410)
        renewed = self.put("anthropic-api-key", ANTHROPIC).json()
        self.assertNotEqual(renewed["entry_id"], first["entry_id"])
        self.assertEqual((renewed["record"]["status"], renewed["record"]["version"]), ("valid", 3))

    def test_removing_missing_credential_is_404(self):
        self.assertEqual(self.remove({"entry_id": "0" * 32, "revision": 1}).status_code, 404)

    def test_ciphertext_is_bound_to_its_record(self):
        self.put("anthropic-api-key", ANTHROPIC)
        sealed = self.sealed(self.rows()[0])
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
                self.put("openai-api-key", ["sk-proj-leakDDDD"]),
                self.replace(self.entries()[0], ["sk-proj-leakDDDD"]),
                self.request("GET", "/credentials"),
                self.request("GET", "/credentials/" + self.entries()[0]["entry_id"]),
                self.remove(self.entries()[0]),
            ]
        self.assertEqual([r.status_code for r in responses[2:4]], [422, 422])
        self.assertEqual(responses[5].status_code, 405)
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
