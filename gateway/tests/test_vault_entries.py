"""Ordered credential entries: schema 4 migration, list policy, as-of reconstruction and replay-safe writes."""

import json
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from vault_fixtures import ALICE, ANTHROPIC, OPENAI, VaultFixture, legacy_vault

import vault

SERVICE = "a" * 64
OLD, LATER = "2000-01-01T00:00:00+00:00", "2000-01-03T00:00:00+00:00"
# Alice holds both Anthropic types and a replaced OpenAI key; Bob revoked his only credential.
LEGACY = (
    (101, "anthropic-api-key", 1, ANTHROPIC, "valid", OLD),
    (101, "claude-subscription", 1, None, "valid", OLD),
    (101, "claude-subscription", 2, "claude-token", "valid", LATER),
    (101, "openai-api-key", 1, OPENAI, "valid", OLD),
    (202, "openai-api-key", 1, None, "revoked", LATER),
)


class MigrationTests(VaultFixture, unittest.TestCase):
    """Schema 3 databases adopt one entry per owner/route without touching records, events or secrets."""

    def migrate(self, manifest=None, records=LEGACY):
        path = self.directory / "legacy.db"
        if not path.exists():
            legacy_vault(path, self.envelope, records)
        store = vault.Store(path, self.envelope, manifest=lambda: manifest or {})
        self.addCleanup(store.close)
        return store

    def test_adoption_preserves_record_identities_events_and_material(self):
        path = self.directory / "legacy.db"
        legacy_vault(path, self.envelope, LEGACY)
        raw = vault.sqlite3.connect(path)
        before = raw.execute("SELECT * FROM credentials ORDER BY user_id,route,version").fetchall()
        events = raw.execute("SELECT sequence,id,record_id,type,at FROM credential_events ORDER BY sequence").fetchall()
        raw.close()
        store = self.migrate()
        self.assertEqual(store.db.execute("SELECT value FROM vault_meta WHERE key='schema'").fetchone()[0], "4")
        after = [tuple(r) for r in store.db.execute("SELECT * FROM credentials ORDER BY user_id,route,version")]
        self.assertEqual(after, before)
        feed = store.db.execute("SELECT sequence,id,record_id,type,at FROM credential_events ORDER BY sequence")
        self.assertEqual([tuple(r) for r in feed], events)
        claude = vault.record_id(101, "claude-subscription", 2)
        self.assertEqual(store.resolve(101, "claude-subscription", claude, 2)["secret"], "claude-token")
        entries = {e["route"]: e for g in store.listing(101)["providers"] for e in g["entries"]}
        self.assertEqual(
            {
                route: (e["name"], e["enabled"], e["roles"], e["profiles"], e["record"]["version"])
                for route, e in entries.items()
            },
            {
                "anthropic-api-key": ("Anthropic API key", True, [], [], 1),
                "claude-subscription": ("Claude subscription", True, [], [], 2),
                "openai-api-key": ("OpenAI API key", True, [], [], 1),
            },
        )
        # Every historical version belongs to its route's entry; legacy fences become entry revisions.
        linked = store.db.execute(
            "SELECT version,entry_id FROM credential_metadata WHERE user_id=101 AND route='claude-subscription'"
        ).fetchall()
        self.assertEqual({row["entry_id"] for row in linked}, {entries["claude-subscription"]["entry_id"]})
        self.assertEqual((entries["claude-subscription"]["revision"], entries["openai-api-key"]["revision"]), (2, 1))
        self.assertEqual(
            {e["entry_id"] for e in store.events()["events"] if e["user_id"] == 202},
            {store.db.execute("SELECT entry_id FROM credential_entries WHERE user_id=202").fetchone()[0]},
        )

    def test_revoked_routes_stay_removed_and_expired_material_stays_unusable(self):
        store = self.migrate()
        self.assertEqual([g["entries"] for g in store.listing(202)["providers"]], [[], []])
        tombstone = store.db.execute("SELECT * FROM credential_entries WHERE user_id=202").fetchone()
        self.assertEqual(tombstone["removed"], LATER)
        with self.assertRaises(vault.VaultError) as error:
            store.replace(vault.User(202, "bob"), tombstone["entry_id"], OPENAI)
        self.assertEqual(error.exception.status, 410)
        record = store.metadata(202)["credentials"][0]
        self.assertEqual(
            (record["status"], record["removed"], record["entry_id"]), ("revoked", True, tombstone["entry_id"])
        )
        added = store.add(vault.User(202, "bob"), "openai-api-key", OPENAI)
        self.assertEqual((added["record"]["version"], added["position"]), (2, 0))

    def test_manifest_seeds_order_once_and_default_is_subscriptions_first(self):
        manifest = {"format": 1, "owners": {"101": {"anthropic": {"first": "api_key", "evidence": "agreement"}}}}
        store = self.migrate(manifest)
        order = lambda s: [e["route"] for e in s.group(101, "anthropic")["entries"]]  # noqa: E731
        self.assertEqual(order(store), ["anthropic-api-key", "claude-subscription"])
        self.assertIn(
            {
                "user_id": "101",
                "provider": "anthropic",
                "order": ["anthropic-api-key", "claude-subscription"],
                "source": "manifest",
                "evidence": "agreement",
            },
            store.migration,
        )
        self.assertIn(
            {
                "user_id": "101",
                "provider": "openai",
                "order": ["openai-api-key"],
                "source": "default",
                "evidence": "unavailable",
            },
            store.migration,
        )
        store.close()

        # A later start never reads a manifest, even an unavailable one, nor rewrites the owner's order.
        def unavailable():
            raise vault.ConfigError("invalid credential order manifest")

        reopened = vault.Store(self.directory / "legacy.db", self.envelope, manifest=unavailable)
        self.addCleanup(reopened.close)
        self.assertIsNone(reopened.migration)
        self.assertEqual(order(reopened), ["anthropic-api-key", "claude-subscription"])
        self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM credential_entries").fetchone()[0], 4)

    def test_conflicting_or_absent_evidence_uses_the_reported_default(self):
        manifest = {"format": 1, "owners": {"101": {"anthropic": {"first": None, "evidence": "conflict"}}}}
        store = self.migrate(manifest)
        self.assertEqual(
            [e["route"] for e in store.group(101, "anthropic")["entries"]], ["claude-subscription", "anthropic-api-key"]
        )
        report = next(r for r in store.migration if r["provider"] == "anthropic")
        self.assertEqual((report["source"], report["evidence"]), ("default", "conflict"))

    def test_interrupted_adoption_rolls_back_and_completes_on_the_next_start(self):
        path = self.directory / "legacy.db"
        legacy_vault(path, self.envelope, LEGACY)
        with patch.object(vault.Store, "snapshot", side_effect=RuntimeError("synthetic crash")):
            with self.assertRaises(RuntimeError):
                vault.Store(path, self.envelope)
        raw = vault.sqlite3.connect(path)
        self.assertEqual(raw.execute("SELECT value FROM vault_meta WHERE key='schema'").fetchone()[0], "3")
        self.assertEqual(raw.execute("SELECT name FROM sqlite_master WHERE name='credential_entries'").fetchall(), [])
        raw.close()
        store = self.migrate()
        self.assertEqual(len(store.migration), 3)
        self.assertEqual(
            store.resolve(101, "openai-api-key", vault.record_id(101, "openai-api-key", 1), 1)["secret"], OPENAI
        )

    def test_boundaries_before_adoption_read_the_legacy_list_and_rewrap_survives(self):
        store = self.migrate()
        old = store.metadata(101, "2000-01-02T00:00:00Z")
        self.assertEqual({p: v["revision"] for p, v in old["lists"].items()}, {"anthropic": 0, "openai": 0})
        by_route = {c["route"]: c for c in old["credentials"]}
        self.assertEqual(by_route["claude-subscription"]["version"], 1)
        self.assertEqual(by_route["claude-subscription"]["status"], "valid")
        self.assertTrue(all(c["enabled"] and not c["removed"] for c in old["credentials"]))
        bob = store.metadata(202, "2000-01-02T00:00:00Z")
        self.assertEqual(bob["credentials"], [])
        self.assertEqual({v["revision"] for v in store.metadata(101)["lists"].values()}, {1})
        events = len(store.events()["events"])
        self.assertEqual(store.rotate(vault.Envelope(os.urandom(32), "new", {"legacy": self.envelope_key})), 3)
        self.assertEqual(len(store.events()["events"]), events + 3)
        self.assertEqual(
            store.resolve(101, "anthropic-api-key", vault.record_id(101, "anthropic-api-key", 1), 1)["secret"],
            ANTHROPIC,
        )

    def setUp(self):
        super().setUp()
        self.envelope_key = os.urandom(32)
        self.envelope = vault.Envelope(self.envelope_key)


class PolicyTests(VaultFixture, unittest.TestCase):
    """List order, filters and toggles change selection policy only; history reconstructs each boundary."""

    def setUp(self):
        super().setUp()
        self.clock = [1000.0]
        self.store.clock = lambda: self.clock[0]
        self.alice = vault.User(101, "alice")
        self.client = TestClient(vault.create_app(self.store, self.identity, SERVICE))

    def edit(self, entry, **fields):
        body = {"revision": entry["revision"], "mutation_id": os.urandom(8).hex(), **fields}
        return self.request("PATCH", "/credentials/" + entry["entry_id"], json=body)

    def test_policy_edits_keep_material_and_emit_selection_events_only(self):
        entry = self.put("anthropic-api-key", ANTHROPIC, roles=["triage", "triage"]).json()
        self.assertEqual(entry["roles"], ["triage"])
        profiles = [
            {"target": "acme/demo", "profile": "sonnet55-high"},
            {"target": "other/demo", "profile": "sonnet55-high"},
        ]
        edited = self.edit(entry, name="Credits", roles=[], profiles=profiles).json()
        self.assertEqual((edited["name"], edited["roles"], edited["profiles"]), ("Credits", [], profiles))
        disabled = self.edit(edited, enabled=False).json()
        enabled = self.edit(disabled, enabled=True).json()
        self.assertEqual(
            (disabled["enabled"], enabled["enabled"], enabled["revision"]), (False, True, entry["revision"] + 3)
        )
        self.assertEqual(enabled["record"], entry["record"])
        events = self.store.events()["events"]
        self.assertEqual(
            [(e["type"], e["reason"], e["revision"]) for e in events],
            [
                ("stored", "stored", None),
                ("policy", "added", 1),
                ("policy", "edited", 2),
                ("policy", "disabled", 3),
                ("policy", "enabled", 4),
            ],
        )
        self.assertTrue(all(e["record_id"] == "" and e["version"] == 0 for e in events[1:]))
        record = self.store.metadata(101)["credentials"][0]
        self.assertEqual(
            (record["entry_id"], record["name"], record["enabled"], record["profiles"], record["position"]),
            (entry["entry_id"], "Credits", True, profiles, 0),
        )

    def test_malformed_policy_is_refused_without_echo_or_change(self):
        entry = self.put("anthropic-api-key", ANTHROPIC).json()
        before = self.store.events()
        for fields in (
            {"name": ""},
            {"name": "   "},
            {"name": "x" * 81},
            {"name": "line\nbreak"},
            {"roles": ["Triage"]},
            {"roles": "triage"},
            {"profiles": [{"target": "no-slash", "profile": "p"}]},
            {"profiles": [{"target": "acme/demo"}]},
            {"enabled": "synthetic-sensitive-input"},
            {},
        ):
            reply = self.edit(entry, **fields)
            self.assertEqual(reply.status_code, 422, fields)
            self.assertNotIn("synthetic-sensitive-input", reply.text)
        self.assertEqual(self.put("anthropic-api-key", ANTHROPIC, name="").status_code, 422)
        self.assertEqual(self.put("anthropic-api-key", ANTHROPIC, mutation_id="short").status_code, 422)
        self.assertEqual(self.store.events(), before)

    def test_history_reconstructs_order_filters_and_availability_at_a_boundary(self):
        first = self.store.add(self.alice, "anthropic-api-key", ANTHROPIC, name="Credits", roles=["triage"])
        self.clock[0] = 2000.0
        second = self.store.add(self.alice, "claude-subscription", "claude-token", name="Personal")
        self.clock[0] = 3000.0
        self.store.reorder(101, "anthropic", [second["entry_id"], first["entry_id"]], revision=2)
        self.store.edit(101, first["entry_id"], enabled=False, roles=[])
        self.clock[0] = 4000.0
        self.store.remove(101, second["entry_id"], second["revision"])
        at = lambda t: self.store.metadata(101, vault.datetime.fromtimestamp(t, vault.UTC).isoformat())  # noqa: E731
        view = lambda value: [  # noqa: E731
            (c["name"], c["position"], c["enabled"], c["roles"], c["status"], c["removed"])
            for c in sorted(value["credentials"], key=lambda c: (c["position"] is None, c["position"]))
        ]
        self.assertEqual(view(at(1500)), [("Credits", 0, True, ["triage"], "valid", False)])
        self.assertEqual(at(1500)["lists"], {"anthropic": {"revision": 1, "entries": [first["entry_id"]]}})
        self.assertEqual(
            view(at(2500)),
            [("Credits", 0, True, ["triage"], "valid", False), ("Personal", 1, True, [], "valid", False)],
        )
        self.assertEqual(
            view(at(3500)), [("Personal", 0, True, [], "valid", False), ("Credits", 1, False, [], "valid", False)]
        )
        now = self.store.metadata(101)
        self.assertEqual(
            view(now), [("Credits", 0, False, [], "valid", False), ("Personal", None, False, [], "revoked", True)]
        )
        # A removed record keeps its entry identity for correlation with lifecycle events.
        self.assertEqual({c["entry_id"] for c in now["credentials"]}, {first["entry_id"], second["entry_id"]})
        self.assertEqual(now["lists"], {"anthropic": {"revision": 5, "entries": [first["entry_id"]]}})
        self.assertEqual(at(500), {"history_complete": True, "credentials": [], "lists": {}})

    def test_edits_of_one_entry_do_not_fence_writes_to_another(self):
        first, second = (self.put("claude-subscription", token, name=token).json() for token in ("one", "two"))
        self.assertEqual(self.edit(first, name="Renamed").status_code, 200)
        self.assertEqual(self.edit(second, enabled=False).status_code, 200)
        self.assertEqual(self.replace(second, "three").status_code, 409)
        self.assertEqual(self.edit(first, name="Again").status_code, 409)


class ReplayTests(VaultFixture, unittest.TestCase):
    """A lost reply replays to the first result; the journal holds no plaintext."""

    def test_replayed_add_and_replace_create_no_second_entry_or_version(self):
        body = {"route": "openai-api-key", "name": "Key", "secret": OPENAI, "mutation_id": "add-" + "0" * 16}
        first = self.request("POST", "/credentials", json=body)
        self.assertEqual(self.request("POST", "/credentials", json=body).json(), first.json())
        self.assertEqual(self.request("POST", "/credentials", json={**body, "secret": OPENAI + "x"}).status_code, 409)
        entry = first.json()
        replace = {"secret": OPENAI + "2", "revision": entry["revision"], "mutation_id": "replace-" + "0" * 16}
        path = f"/credentials/{entry['entry_id']}/secret"
        second = self.request("PUT", path, json=replace)
        self.assertEqual(second.json()["record"]["version"], 2)
        # The replay carries the now-stale revision yet returns the committed result.
        self.assertEqual(self.request("PUT", path, json=replace).json(), second.json())
        self.assertEqual([row["version"] for row in self.rows()], [1, 2])
        self.assertEqual(len(self.entries()), 1)
        # The same ID from another owner is that owner's own mutation.
        other = self.request("POST", "/credentials", token="ghu_bob-token", json=body)
        self.assertNotEqual(other.json()["entry_id"], entry["entry_id"])
        self.assertEqual([e["type"] for e in self.store.events()["events"]].count("stored"), 3)

    def test_replayed_edit_reorder_and_removal_are_safe_after_a_lost_reply(self):
        first, second = (self.put("claude-subscription", token).json() for token in ("one", "two"))
        edit = {"revision": first["revision"], "enabled": False, "mutation_id": "edit-" + "0" * 16}
        path = "/credentials/" + first["entry_id"]
        done = self.request("PATCH", path, json=edit).json()
        self.assertEqual(self.request("PATCH", path, json=edit).json(), done)
        order = {"revision": 3, "entries": [second["entry_id"], first["entry_id"]], "mutation_id": "order-" + "0" * 16}
        moved = self.request("PUT", "/credentials/order/anthropic", json=order).json()
        self.assertEqual(self.request("PUT", "/credentials/order/anthropic", json=order).json(), moved)
        self.assertEqual(moved["revision"], 4)
        removed = self.request("DELETE", path, params={"revision": done["revision"]}).json()
        self.assertEqual(self.request("DELETE", path, params={"revision": done["revision"]}).json(), removed)
        events = [(e["type"], e["reason"]) for e in self.store.events()["events"]]
        self.assertEqual(events.count(("policy", "disabled")), 1)
        self.assertEqual(events.count(("policy", "reordered")), 1)
        self.assertEqual(events.count(("revoked", "revoked")), 1)

    def test_journal_and_policy_tables_hold_no_plaintext(self):
        self.put("openai-api-key", OPENAI, mutation_id="journal-" + "0" * 16)
        self.store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        raw = (self.directory / "vault.db").read_bytes()
        self.assertNotIn(OPENAI.encode(), raw)
        self.assertNotIn(ALICE.encode(), raw)
        journal = self.store.db.execute("SELECT * FROM credential_mutations").fetchall()
        self.assertEqual([(row["user_id"], row["id"]) for row in journal], [(101, "journal-" + "0" * 16)])
        self.assertNotIn("secret", json.loads(journal[0]["response"]))


class ManifestTests(unittest.TestCase):
    """Only a well-formed nonsecret manifest reaches the migration."""

    def test_missing_path_is_empty_and_malformed_files_refuse_startup(self):
        import tempfile
        from pathlib import Path

        self.assertEqual(vault.load_manifest(None), {})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            for content in ("not json", "[]", '{"format":2,"owners":{}}', '{"format":1,"owners":{"101":[]}}'):
                path.write_text(content)
                with self.assertRaisesRegex(vault.ConfigError, "manifest"):
                    vault.load_manifest(path)
            with self.assertRaisesRegex(vault.ConfigError, "manifest"):
                vault.load_manifest(Path(directory) / "absent.json")
            path.write_text('{"format":1,"owners":{"101":{"openai":{"first":"api_key"}}}}')
            self.assertEqual(vault.load_manifest(path)["owners"]["101"]["openai"]["first"], "api_key")


if __name__ == "__main__":
    unittest.main()
