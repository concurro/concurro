"""Relay custody within the current ordered Vault entry and version lifecycle."""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from vault_fixtures import ALICE, BOB, VaultFixture

from vault import Envelope, Store, User, VaultError, create_app, relay_monitor


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
        self.assertEqual(
            self.store.resolve(7, "claude-subscription", binding[0]["record_id"], 1)["relay_id"], "relay-recovered"
        )

    def test_mixed_routes_share_one_approval_and_commit_both_records(self):
        """One durable owner approval creates both provider records in one transaction."""
        approvals = []

        def approve(code, bindings):
            approvals.append((code, bindings))
            return "relay-mixed"

        request = dict(
            name="Personal",
            roles=[],
            profiles=[],
            mutation="r" * 16,
            routes=["claude-subscription", "chatgpt-subscription"],
        )
        result = self.store.add_relay(self.owner, "BCDF-GHJK", approve, **request)
        self.assertEqual(len(approvals), 1)
        self.assertEqual(
            [binding["route"] for binding in approvals[0][1]], ["chatgpt-subscription", "claude-subscription"]
        )
        self.assertEqual(set(result["entries"]), set(request["routes"]))
        for route, entry in result["entries"].items():
            record = entry["record"]
            self.assertEqual(
                self.store.resolve(7, route, record["record_id"], record["version"])["relay_id"], "relay-mixed"
            )
            self.assertEqual(
                self.store.order(7, "anthropic" if route == "claude-subscription" else "openai")[1], [entry["entry_id"]]
            )
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
            self.owner,
            "BCDF-GHJK",
            approve,
            name="Personal",
            roles=[],
            profiles=[],
            mutation="a" * 16,
            routes=["chatgpt-subscription"],
        )
        request = dict(revision=entry["revision"], mutation="b" * 16)
        result = self.store.replace_relay(self.owner, entry["entry_id"], "LMNP-QRST", approve, **request)
        self.assertEqual(
            self.store.replace_relay(self.owner, entry["entry_id"], "LMNP-QRST", approve, **request), result
        )
        self.assertEqual(len(approvals), 2)
        self.store.close()
        self.store = Store(Path(self.root.name) / "vault.db", Envelope(b"k" * 32))
        self.assertEqual(
            self.store.replace_relay(self.owner, entry["entry_id"], "LMNP-QRST", approve, **request), result
        )
        self.assertEqual(len(approvals), 2)

    def test_mixed_approval_reply_loss_reserves_both_versions(self):
        """An approved code can roll forward with the original two versions after newer writes."""
        bindings = []

        def lose_reply(code, selected):
            bindings.append(selected)
            raise VaultError(503, "relay verifier unavailable")

        request = dict(
            name="Personal",
            roles=[],
            profiles=[],
            mutation="r" * 16,
            routes=["claude-subscription", "chatgpt-subscription"],
        )
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

    def test_device_replacement_rebinds_both_routes_and_keeps_policy(self):
        """One new code replaces a paired device's two entries without touching their policy or order."""
        approvals = []

        def approve(code, binding):
            approvals.append((code, binding))
            return "relay-" + str(len(approvals))

        both = ["claude-subscription", "chatgpt-subscription"]
        paired = self.store.add_relay(
            self.owner, "BCDF-GHJK", approve, name="Studio", roles=[], profiles=[], mutation="a" * 16, routes=both
        )["entries"]
        claude, codex = paired["claude-subscription"], paired["chatgpt-subscription"]
        token = self.store.add(self.owner, "claude-subscription", "X" * 44, mutation="t" * 16)
        claude = self.store.edit(7, claude["entry_id"], revision=claude["revision"], enabled=False, roles=["planner"])
        self.store.reorder(
            7, "anthropic", [token["entry_id"], claude["entry_id"]], revision=self.store.order(7, "anthropic")[0]
        )
        claude = self.store.view(self.store.owned(7, claude["entry_id"]))
        self.assertEqual(self.store.relays(7), ["relay-1"])

        entries = {claude["entry_id"]: claude["revision"], codex["entry_id"]: codex["revision"]}
        with self.assertRaises(VaultError) as stale:
            self.store.replace_device(
                self.owner,
                "LMNP-QRST",
                approve,
                mutation="s" * 16,
                entries={**entries, codex["entry_id"]: codex["revision"] - 1},
            )
        self.assertEqual(stale.exception.status, 409)
        self.assertEqual(len(approvals), 1)

        result = self.store.replace_device(self.owner, "LMNP-QRST", approve, entries=entries, mutation="b" * 16)
        code, bindings = approvals[-1]
        self.assertEqual(code, "LMNP-QRST")
        self.assertEqual(
            [(b["route"], b["entry_id"]) for b in bindings],
            [("chatgpt-subscription", codex["entry_id"]), ("claude-subscription", claude["entry_id"])],
        )
        replaced = result["entries"]
        self.assertEqual(replaced["claude-subscription"]["roles"], ["planner"])
        self.assertFalse(replaced["claude-subscription"]["enabled"])
        self.assertEqual(replaced["claude-subscription"]["position"], 1)
        self.assertEqual(replaced["claude-subscription"]["record"]["relay_id"], "relay-2")
        self.assertEqual(self.store.relays(7), ["relay-2"])
        for route, old in (("claude-subscription", claude), ("chatgpt-subscription", codex)):
            with self.assertRaises(VaultError) as retired:
                self.store.resolve(7, route, old["record"]["record_id"], old["record"]["version"])
            self.assertEqual(retired.exception.status, 410)
            current = replaced[route]["record"]
            self.assertEqual(
                self.store.resolve(7, route, current["record_id"], current["version"])["relay_id"], "relay-2"
            )
        self.assertEqual(
            self.store.replace_device(self.owner, "LMNP-QRST", approve, entries=entries, mutation="b" * 16), result
        )
        self.assertEqual(len(approvals), 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM relay_approval_routes").fetchone()[0], 0)

        self.store.remove(7, replaced["claude-subscription"]["entry_id"], replaced["claude-subscription"]["revision"])
        self.assertEqual(self.store.relays(7), ["relay-2"])
        self.store.remove(7, replaced["chatgpt-subscription"]["entry_id"], replaced["chatgpt-subscription"]["revision"])
        self.assertEqual(self.store.relays(7), [])

    def test_device_replacement_refused_by_registry_changes_nothing(self):
        """A code whose registered routes differ is refused by the registry and leaves both entries as they were."""

        def approve(code, binding):
            if code == "LMNP-QRST":
                raise VaultError(409, "relay approval unavailable")
            return "relay-paired"

        paired = self.store.add_relay(
            self.owner,
            "BCDF-GHJK",
            approve,
            name="Studio",
            roles=[],
            profiles=[],
            mutation="a" * 16,
            routes=["claude-subscription", "chatgpt-subscription"],
        )["entries"]
        claude = paired["claude-subscription"]
        with self.assertRaises(VaultError) as refused:
            self.store.replace_device(
                self.owner, "LMNP-QRST", approve, entries={claude["entry_id"]: claude["revision"]}, mutation="c" * 16
            )
        self.assertEqual(refused.exception.status, 409)
        self.assertEqual(self.store.view(self.store.owned(7, claude["entry_id"]))["record"]["relay_id"], "relay-paired")
        with self.assertRaises(VaultError) as duplicate:
            self.store.replace_device(
                self.owner,
                "LMNP-QRST",
                approve,
                mutation="d" * 16,
                entries={
                    claude["entry_id"]: claude["revision"],
                    self.store.add(self.owner, "claude-subscription", "X" * 44, mutation="e" * 16)["entry_id"]: 1,
                },
            )
        self.assertEqual(duplicate.exception.status, 422)


class RelayMonitorTests(unittest.TestCase):
    """Translate the verifier's private status without merging distinct leases."""

    def test_same_second_leases_stay_distinct_and_unrequested_relays_are_dropped(self):
        """Second-precision times cannot merge two leases of one approval; the link slot keeps them apart."""
        asked = []

        class Verifier(BaseHTTPRequestHandler):
            def do_POST(self):
                asked.append(
                    (
                        self.path,
                        self.headers["X-Concurro-Verifier-Auth"],
                        json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                    )
                )
                lease = {"route": "claude-subscription", "entry_id": "e" * 32, "version": 2, "task": None}
                body = json.dumps(
                    {
                        "started": 1000.2,
                        "now": 1100.9,
                        "relays": [
                            {
                                "relay": "relay-a",
                                "idle": 0,
                                "last_seen": 1100.9,
                                "active": [
                                    {**lease, "slot": 1, "since": 1050.1},
                                    {**lease, "slot": 2, "since": 1050.7},
                                ],
                            },
                            {"relay": "relay-other", "idle": 1, "last_seen": 1100.9, "active": []},
                        ],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Verifier)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        status = relay_monitor(f"http://127.0.0.1:{server.server_port}", "synthetic")(7, ["relay-a"])
        self.assertEqual(asked, [("/status", "Bearer synthetic", {"user_id": 7, "relays": ["relay-a"]})])
        self.assertEqual([relay["relay"] for relay in status["relays"]], ["relay-a"])
        leases = status["relays"][0]["active"]
        self.assertEqual({lease["since"] for lease in leases}, {"1970-01-01T00:17:30+00:00"})
        self.assertEqual([lease["slot"] for lease in leases], [1, 2])
        with self.assertRaises(VaultError) as unavailable:
            relay_monitor("http://127.0.0.1:9", "synthetic")(7, ["relay-a"])
        self.assertEqual(unavailable.exception.status, 503)


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

    def test_pending_lookup_and_live_status_are_owner_scoped(self):
        """Lookup needs an owner; status asks only for the caller's relays and reports unknown service honestly."""
        looked, asked = [], []

        def pending(code):
            looked.append(code)
            return {"name": "studio", "routes": ["chatgpt-subscription", "claude-subscription"], "expires_at": None}

        def status(user_id, relays):
            asked.append((user_id, relays))
            return {
                "started": "2026-11-17T00:00:00+00:00",
                "checked": "2026-11-17T00:05:00+00:00",
                "relays": [{"relay": relays[0], "idle": 1, "last_seen": "2026-11-17T00:05:00+00:00", "active": []}],
            }

        approve = lambda code, binding: "relay-" + code[:4].lower()
        self.client = TestClient(create_app(self.store, self.identity, relay_approve=approve))
        self.assertEqual(
            self.request("GET", "/credentials/relay/status", token=ALICE).json(),
            {"service": "unconfigured", "relays": []},
        )
        self.assertEqual(
            self.request("POST", "/credentials/relay/pending", token=ALICE, json={"code": "BCDF-GHJK"}).status_code, 503
        )
        self.client = TestClient(
            create_app(self.store, self.identity, relay_approve=approve, relay_pending=pending, relay_status=status)
        )
        self.assertEqual(self.client.post("/credentials/relay/pending", json={"code": "BCDF-GHJK"}).status_code, 401)
        self.assertEqual(
            self.request("POST", "/credentials/relay/pending", token=ALICE, json={"code": "bad"}).status_code, 422
        )
        self.assertEqual(looked, [])
        found = self.request("POST", "/credentials/relay/pending", token=ALICE, json={"code": "BCDF-GHJK"}).json()
        self.assertEqual(found["routes"], ["chatgpt-subscription", "claude-subscription"])
        self.assertEqual(
            self.request("GET", "/credentials/relay/status", token=ALICE).json(), {"service": "available", "relays": []}
        )
        self.assertEqual(asked, [])
        for token, code in ((ALICE, "BCDF-GHJK"), (BOB, "LMNP-QRST")):
            self.request(
                "POST",
                "/credentials/relay",
                token=token,
                json={"code": code, "name": "Claude", "mutation_id": code.replace("-", "") * 2},
            )
        live = self.request("GET", "/credentials/relay/status", token=ALICE).json()
        self.assertEqual(asked, [(101, ["relay-bcdf"])])
        self.assertEqual([relay["relay"] for relay in live["relays"]], ["relay-bcdf"])
        listed = self.request("GET", "/credentials", token=ALICE).json()["providers"][0]["entries"][0]
        self.assertEqual(listed["record"]["relay_id"], "relay-bcdf")

        def unavailable(user_id, relays):
            raise VaultError(503, "relay verifier unavailable")

        self.client = TestClient(create_app(self.store, self.identity, relay_status=unavailable))
        self.assertEqual(
            self.request("GET", "/credentials/relay/status", token=ALICE).json(),
            {"service": "unavailable", "relays": []},
        )

    def test_device_endpoint_refuses_other_owners_entries(self):
        """A device replacement names only the caller's entries, once each."""
        approve = lambda code, binding: "relay-" + code[:4].lower()
        self.client = TestClient(create_app(self.store, self.identity, relay_approve=approve))
        bob = self.request(
            "POST",
            "/credentials/relay",
            token=BOB,
            json={"code": "BCDF-GHJK", "name": "Claude", "mutation_id": "b" * 16},
        ).json()
        body = {
            "code": "LMNP-QRST",
            "mutation_id": "c" * 16,
            "entries": [{"entry_id": bob["entry_id"], "revision": bob["revision"]}],
        }
        self.assertEqual(self.request("PUT", "/credentials/relay/device", token=ALICE, json=body).status_code, 404)
        body["entries"] *= 2
        self.assertEqual(self.request("PUT", "/credentials/relay/device", token=BOB, json=body).status_code, 422)
        body["entries"] = body["entries"][:1]
        replaced = self.request("PUT", "/credentials/relay/device", token=BOB, json=body).json()
        self.assertEqual(replaced["entries"]["claude-subscription"]["record"]["relay_id"], "relay-lmnp")


if __name__ == "__main__":
    unittest.main()
