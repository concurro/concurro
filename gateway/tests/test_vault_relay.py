"""Relay custody within the current ordered Vault entry and version lifecycle."""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from vault_fixtures import ALICE, BOB, VaultFixture
from vault import Envelope, Store, User, VaultError, create_app


class RelayStoreTests(unittest.TestCase):
    """Exercise exact record resolution without provider material."""

    def setUp(self):
        """Open an isolated schema 6 Vault."""
        self.root = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.root.name) / "vault.db", Envelope(b"k" * 32))
        self.owner = User(7, "owner")

    def tearDown(self):
        """Close the store before removing its WAL directory."""
        self.store.close()
        self.root.cleanup()

    def test_relay_entry_resolves_without_secret_and_retires_exact_versions(self):
        """Relay versions retain list policy while old versions fail closed."""
        approvals = []

        def approve(code, binding):
            approvals.append((code, binding))
            return "relay-" + code.replace("-", "").lower()

        first = self.store.add_relay(
            self.owner, "BCDF-GHJK", approve, name="Personal Claude", roles=[], profiles=[], mutation="m" * 16
        )
        record = first["record"]
        self.assertEqual(record["custody"], "relay")
        self.assertEqual(record["status"], "valid")
        self.assertEqual(self.store.metadata(7)["credentials"][0]["custody"], "relay")
        self.assertEqual(
            self.store.resolve(7, "claude-subscription", record["record_id"], record["version"]),
            {
                "user_id": "7",
                "route": "claude-subscription",
                "record_id": record["record_id"],
                "version": 1,
                "custody": "relay",
                "relay_id": "relay-bcdfghjk",
                "entry_id": first["entry_id"],
            },
        )
        self.assertEqual(approvals[0][1]["entry_id"], first["entry_id"])
        self.assertEqual(approvals[0][1]["record_id"], record["record_id"])
        self.assertEqual(self.store.order(7, "anthropic")[1], [first["entry_id"]])
        replay = self.store.add_relay(
            self.owner, "BCDF-GHJK", approve, name="Personal Claude", roles=[], profiles=[], mutation="m" * 16
        )
        self.assertEqual(replay, first)
        self.assertEqual(len(approvals), 1)
        replaced = self.store.replace_relay(
            self.owner, first["entry_id"], "LMNP-QRST", approve, revision=first["revision"], mutation="n" * 16
        )
        self.assertEqual(replaced["record"]["version"], 2)
        with self.assertRaises(VaultError) as old:
            self.store.resolve(7, "claude-subscription", record["record_id"], 1)
        self.assertEqual(old.exception.status, 410)
        self.store.remove(7, first["entry_id"], replaced["revision"])
        with self.assertRaises(VaultError) as removed:
            self.store.resolve(7, "claude-subscription", replaced["record"]["record_id"], 2)
        self.assertEqual(removed.exception.status, 410)

    def test_lost_approval_reply_retries_reserved_binding_after_other_write_and_reopen(self):
        """A remote approval can roll forward after its HTTP reply and process are lost."""
        binding = []

        def lose_reply(code, selected):
            """Model Rust committing approval before the Vault receives its reply."""
            binding.append(selected)
            raise VaultError(503, "relay verifier unavailable")

        request = dict(name="Personal Claude", roles=[], profiles=[], mutation="r" * 16)
        with self.assertRaises(VaultError) as lost:
            self.store.add_relay(self.owner, "BCDF-GHJK", lose_reply, **request)
        self.assertEqual(lost.exception.status, 503)
        self.assertEqual(binding[0]["version"], 1)
        self.assertEqual(self.store.order(7, "anthropic")[1], [])

        other = Store(Path(self.root.name) / "vault.db", Envelope(b"k" * 32))
        try:
            newer = other.add(self.owner, "claude-subscription", "X" * 44, mutation="s" * 16)
        finally:
            other.close()
        self.assertEqual(newer["record"]["version"], 2)

        self.store.close()
        self.store = Store(Path(self.root.name) / "vault.db", Envelope(b"k" * 32))

        def retry(code, selected):
            """Return the already approved relay only for the original exact binding."""
            self.assertEqual(code, "BCDF-GHJK")
            self.assertEqual(selected, binding[0])
            return "relay-recovered"

        restored = self.store.add_relay(self.owner, "BCDF-GHJK", retry, **request)
        self.assertEqual(restored["record"]["version"], 1)
        self.assertEqual(restored["record"]["record_id"], binding[0]["record_id"])
        self.assertEqual(self.store.resolve(7, "claude-subscription", binding[0]["record_id"], 1)["relay_id"],
                         "relay-recovered")

    def test_mixed_routes_share_one_approval_and_commit_both_records(self):
        """One durable owner approval creates both provider records in one transaction."""
        approvals = []

        def approve(code, bindings):
            approvals.append((code, bindings))
            return "relay-mixed"

        request = dict(name="Personal", roles=[], profiles=[], mutation="r" * 16,
                       routes=["claude-subscription", "chatgpt-subscription"])
        result = self.store.add_relay(self.owner, "BCDF-GHJK", approve, **request)
        self.assertEqual(len(approvals), 1)
        self.assertEqual([binding["route"] for binding in approvals[0][1]],
                         ["chatgpt-subscription", "claude-subscription"])
        self.assertEqual(set(result["entries"]), set(request["routes"]))
        for route, entry in result["entries"].items():
            record = entry["record"]
            self.assertEqual(self.store.resolve(7, route, record["record_id"], record["version"])["relay_id"],
                             "relay-mixed")
            self.assertEqual(self.store.order(7, "anthropic" if route == "claude-subscription" else "openai")[1],
                             [entry["entry_id"]])
        self.assertEqual(self.store.add_relay(self.owner, "BCDF-GHJK", approve, **request), result)
        self.assertEqual(len(approvals), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM relay_approval_intents").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM relay_approval_routes").fetchone()[0], 0)

    def test_replace_relay_replay_after_revision_change_and_reopen(self):
        """A completed replacement replays before checking its now-stale entry revision."""
        approvals = []

        def approve(code, binding):
            approvals.append((code, binding))
            return "relay-" + str(len(approvals))

        entry = self.store.add_relay(
            self.owner, "BCDF-GHJK", approve, name="Personal", roles=[], profiles=[], mutation="a" * 16,
            routes=["chatgpt-subscription"],
        )
        request = dict(revision=entry["revision"], mutation="b" * 16)
        result = self.store.replace_relay(self.owner, entry["entry_id"], "LMNP-QRST", approve, **request)
        self.assertEqual(self.store.replace_relay(self.owner, entry["entry_id"], "LMNP-QRST", approve,
                                                  **request), result)
        self.assertEqual(len(approvals), 2)
        self.store.close()
        self.store = Store(Path(self.root.name) / "vault.db", Envelope(b"k" * 32))
        self.assertEqual(self.store.replace_relay(self.owner, entry["entry_id"], "LMNP-QRST", approve,
                                                  **request), result)
        self.assertEqual(len(approvals), 2)

    def test_mixed_approval_reply_loss_reserves_both_versions(self):
        """An approved code can roll forward with the original two versions after newer writes."""
        bindings = []

        def lose_reply(code, selected):
            bindings.append(selected)
            raise VaultError(503, "relay verifier unavailable")

        request = dict(name="Personal", roles=[], profiles=[], mutation="r" * 16,
                       routes=["claude-subscription", "chatgpt-subscription"])
        with self.assertRaises(VaultError):
            self.store.add_relay(self.owner, "BCDF-GHJK", lose_reply, **request)
        self.assertEqual([selected["version"] for selected in bindings[0]], [1, 1])
        self.assertEqual(self.store.order(7, "anthropic")[1], [])
        self.assertEqual(self.store.order(7, "openai")[1], [])
        for route in request["routes"]:
            self.assertEqual(self.store.add(self.owner, route, "X" * 44, mutation=route[:16])["record"]["version"], 2)

        def retry(code, selected):
            self.assertEqual(selected, bindings[0])
            return "relay-mixed"

        result = self.store.add_relay(self.owner, "BCDF-GHJK", retry, **request)
        self.assertEqual({entry["record"]["version"] for entry in result["entries"].values()}, {1})


class RelayOwnerApiTests(VaultFixture, unittest.TestCase):
    """Approve a displayed relay code only through the owner authenticated API."""

    def test_approval_requires_owner_identity_and_preserves_entry_owner(self):
        """The private registry sees the verified GitHub owner and exact new record."""
        approved = []

        def approve(code, binding):
            approved.append((code, binding))
            return "relay-example"

        self.client = TestClient(create_app(self.store, self.identity, relay_approve=approve))
        body = {"code": "BCDF-GHJK", "name": "Claude", "mutation_id": "m" * 16}
        self.assertEqual(self.client.post("/credentials/relay", json=body).status_code, 401)
        self.assertEqual(self.request("POST", "/credentials/relay", token="invalid", json=body).status_code, 401)
        self.assertEqual(approved, [])
        entry = self.request("POST", "/credentials/relay", token=ALICE, json=body).json()
        self.assertEqual(approved[0][1]["user_id"], 101)
        self.assertEqual(approved[0][1]["login"], "alice")
        self.assertEqual(approved[0][1]["entry_id"], entry["entry_id"])
        self.assertEqual(approved[0][1]["record_id"], entry["record"]["record_id"])
        self.assertEqual(self.request("GET", "/credentials", token=BOB).json()["providers"][0]["entries"], [])


if __name__ == "__main__":
    unittest.main()
