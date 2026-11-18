"""Browser-facing lifecycle follows the real vault with synthetic GitHub/provider transports."""

import json
import sys
import unittest
import uuid
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from test_backoffice import USER, FakeOAuth, settings

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gateway/tests"))
from vault_fixtures import ALICE, BOB, OPENAI, VaultFixture

from backoffice.app import CSRF_HEADER, SESSION_COOKIE, create_app  # noqa: E402
from backoffice.auth import Sessions, User  # noqa: E402
from backoffice.credentials import Vault  # noqa: E402
from test_subscription_storage import TOKENS
from vault import create_app as create_vault_app


class PortalLifecycleTests(VaultFixture, unittest.TestCase):
    """Owner-bound writes, validation preservation and candidate validation cross both real HTTP apps."""

    def browser(self, vault_client=None):
        """Reuse real owner/session/CSRF adapters while forwarding to the selected local vault app."""
        vault_client = vault_client or self.client

        def forward(request):
            """Forward BFF HTTP into the actual vault app, without replacing lifecycle decisions."""
            reply = vault_client.request(
                request.method, request.url.raw_path.decode(), headers=dict(request.headers), content=request.content
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

    def remove(self, client, entry):
        """Remove an entry at the revision last returned for it."""
        return client.delete(
            "/api/me/credentials/" + entry["id"], params={"revision": entry["revision"]}, headers={CSRF_HEADER: "1"}
        )

    def write(self, client, method, path, **body):
        """One CSRF-protected owner write with a fresh mutation ID."""
        body.setdefault("mutation_id", uuid.uuid4().hex)
        return client.request(method, "/api/me/credentials" + path, headers={CSRF_HEADER: "1"}, json=body)

    def test_paired_device_lookup_status_and_replacement_through_the_portal(self):
        """Lookup names the code's routes, status stays owner-scoped and one code replaces both approvals."""
        approvals, asked = [], []

        def approve(code, bindings):
            approvals.append((code, bindings))
            return "relay-" + str(len(approvals))

        def pending(code):
            return {"name": "studio", "routes": ["chatgpt-subscription", "claude-subscription"], "expires_at": None}

        def status(user_id, relays):
            asked.append((user_id, relays))
            lease = {
                "route": "claude-subscription",
                "entry_id": "e" * 32,
                "version": 1,
                "since": "2026-11-17T00:00:00+00:00",
                "task": {"repository": "acme/demo", "issue": 4},
            }
            # Two sessions of one approval in the same second differ only by link slot.
            return {
                "started": "2026-11-17T00:00:00+00:00",
                "checked": "2026-11-17T00:01:00+00:00",
                "relays": [
                    {
                        "relay": relay,
                        "idle": 1,
                        "last_seen": "2026-11-17T00:01:00+00:00",
                        "active": [{**lease, "slot": 1}, {**lease, "slot": 2}],
                    }
                    for relay in relays
                ],
            }

        vault_client = TestClient(
            create_vault_app(
                self.store, self.identity, relay_approve=approve, relay_pending=pending, relay_status=status
            )
        )
        client, alice, bob = self.browser(vault_client)
        client.cookies.set(SESSION_COOKIE, alice)
        found = client.post(
            "/api/me/credentials/relay/pending", headers={CSRF_HEADER: "1"}, json={"code": "BCDF-GHJK"}
        ).json()
        self.assertEqual((found["name"], found["routes"]), ("studio", ["chatgpt-subscription", "claude-subscription"]))
        empty = client.get("/api/me/relays").json()
        self.assertEqual(
            (empty["service"], empty["relays"], empty["setup"]),
            ("available", [], {"server": None, "download": None, "command": None, "login": None}),
        )
        added = self.write(
            client,
            "POST",
            "/relay",
            code="BCDF-GHJK",
            name="Studio",
            routes=["claude-subscription", "chatgpt-subscription"],
        ).json()["entries"]
        self.assertEqual({entry["relay"] for entry in added.values()}, {"relay-1"})
        live = client.get("/api/me/relays").json()
        self.assertEqual(asked, [(101, ["relay-1"])])
        self.assertEqual(live["relays"][0]["active"][0]["task"], {"owner": "acme", "repo": "demo", "issue": 4})
        self.assertEqual([lease["slot"] for lease in live["relays"][0]["active"]], [1, 2])
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, bob)
        self.assertEqual(client.get("/api/me/relays").json()["relays"], [])
        entries = [{"id": entry["id"], "revision": entry["revision"]} for entry in added.values()]
        self.assertEqual(self.write(client, "PUT", "/relay/device", code="LMNP-QRST", entries=entries).status_code, 404)
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, alice)
        replaced = self.write(client, "PUT", "/relay/device", code="LMNP-QRST", entries=entries)
        self.assertEqual(replaced.status_code, 200, replaced.text)
        self.assertEqual({entry["relay"] for entry in replaced.json()["entries"].values()}, {"relay-2"})
        self.assertEqual({entry["id"] for entry in replaced.json()["entries"].values()}, {e["id"] for e in entries})
        self.assertEqual(len(approvals[-1][1]), 2)

    def test_unavailable_vault_reports_unknown_relay_status_with_configured_setup(self):
        """A vault that cannot be reached leaves live status unavailable rather than offline."""
        client, alice, _ = self.browser()
        client.cookies.set(SESSION_COOKIE, alice)
        self.assertEqual(client.get("/api/me/relays").json()["service"], "unconfigured")
        api = Vault("http://vault.invalid", httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503))))
        sessions = Sessions()
        configured = settings(public_url="http://testserver", relay_server="relay.invalid:7443")
        portal = TestClient(create_app(configured, oauth=FakeOAuth(), vault=api, sessions=sessions))
        portal.cookies.set(SESSION_COOKIE, sessions.create(ALICE, USER, 3600))
        reply = portal.get("/api/me/relays").json()
        self.assertEqual((reply["service"], reply["relays"]), ("unavailable", []))
        self.assertEqual(
            reply["setup"],
            {
                "server": "relay.invalid:7443",
                "download": None,
                "command": "concurro-relay --server relay.invalid:7443",
                "login": "concurro-relay login --server relay.invalid:7443",
            },
        )

    def test_one_sponsor_approval_appears_in_both_lists_and_keeps_owner_bound_lifecycle(self):
        """The portal forwards one authenticated approval and reads both relay entries through the normal lists."""
        approvals = []

        def approve(code, bindings):
            approvals.append((code, bindings))
            return "relay-" + str(len(approvals))

        vault_client = TestClient(create_vault_app(self.store, self.identity, relay_approve=approve))
        client, alice, bob = self.browser(vault_client)
        client.cookies.set(SESSION_COOKIE, alice)
        body = {
            "code": "BCDF-GHJK",
            "name": "Laptop Sponsor",
            "routes": ["claude-subscription", "chatgpt-subscription"],
            "mutation_id": "a" * 16,
        }
        self.assertEqual(client.post("/api/me/credentials/relay", json=body).status_code, 403)
        added = self.write(client, "POST", "/relay", **body)
        self.assertEqual(added.status_code, 200, added.text)
        self.assertEqual(len(approvals), 1)
        self.assertEqual(
            {binding["route"] for binding in approvals[0][1]}, {"claude-subscription", "chatgpt-subscription"}
        )
        entries = added.json()["entries"]
        self.assertEqual(set(entries), {"claude-subscription", "chatgpt-subscription"})
        self.assertEqual({item["custody"] for item in entries.values()}, {"relay"})
        self.assertEqual(self.write(client, "POST", "/relay", **body).json(), added.json())
        listed = client.get("/api/me/credentials").json()["providers"]
        self.assertEqual(
            [(group["provider"], len(group["entries"])) for group in listed], [("anthropic", 1), ("openai", 1)]
        )
        claude = entries["claude-subscription"]
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, bob)
        self.assertEqual(
            self.write(
                client, "PUT", "/" + claude["id"] + "/relay", code="LMNP-QRST", revision=claude["revision"]
            ).status_code,
            404,
        )
        self.assertEqual(self.remove(client, claude).status_code, 404)
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, alice)
        replaced = self.write(
            client, "PUT", "/" + claude["id"] + "/relay", code="LMNP-QRST", revision=claude["revision"]
        )
        self.assertEqual((replaced.status_code, replaced.json()["version"]), (200, 2))
        self.assertEqual(self.remove(client, claude).status_code, 409)
        self.assertEqual(self.remove(client, replaced.json()).status_code, 200)
        self.assertEqual(self.remove(client, entries["chatgpt-subscription"]).status_code, 200)
        self.assertEqual(
            [group["entries"] for group in client.get("/api/me/credentials").json()["providers"]], [[], []]
        )

    def test_browser_to_vault_entry_lifecycle_and_private_separation(self):
        """No browser owner field or service bearer can select another person's entry or secret version."""
        client, alice, bob = self.browser()
        client.cookies.set(SESSION_COOKIE, alice)
        new = {"type": "openai_api", "name": "OpenAI credits", "secret": OPENAI, "mutation_id": "m" * 16}
        self.assertEqual(client.post("/api/me/credentials", json=new).status_code, 403)
        reply = self.write(client, "POST", "", **new, owner=202)
        self.assertEqual(reply.status_code, 200)
        entry = reply.json()
        self.assertEqual(
            (entry["version"], entry["type"], entry["name"], entry["enabled"], entry["status"], entry["expiryKnown"]),
            (1, "openai_api", "OpenAI credits", True, "valid", False),
        )
        self.assertNotIn(OPENAI, reply.text)
        # A lost reply replays to the same entry; the ID cannot carry a different request.
        self.assertEqual(self.write(client, "POST", "", **new, owner=202).json(), entry)
        self.assertEqual(self.write(client, "POST", "", **{**new, "name": "Other"}).status_code, 409)
        path = "/" + entry["id"]
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, bob)
        self.assertEqual(self.remove(client, entry).status_code, 404)
        self.assertEqual(self.write(client, "PATCH", path, revision=entry["revision"], enabled=False).status_code, 404)
        self.assertEqual([g["entries"] for g in client.get("/api/me/credentials").json()["providers"]], [[], []])
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, alice)
        before = self.store.events()
        stale = self.write(client, "PUT", path + "/secret", revision=entry["revision"], secret="invalid header value")
        self.assertEqual(stale.status_code, 422)
        self.assertEqual(
            self.write(client, "PATCH", path, revision=entry["revision"], roles=["evaluator"]).status_code, 422
        )
        self.assertEqual(self.store.events(), before)
        self.assertEqual(
            self.write(client, "POST", "", **{**new, "secret": {"synthetic-secret": "never-echo"}}).text,
            '{"detail":"malformed request"}',
        )
        for name in ("claude_subscription", "chatgpt_subscription"):
            saved = self.write(client, "POST", "", type=name, name=name, secret="synthetic-cli-token")
            self.assertEqual(saved.status_code, 422 if name == "chatgpt_subscription" else 200)
        filters = {"roles": ["triage"], "profiles": [{"target": "acme/demo", "profile": "sonnet55-high"}]}
        edited = self.write(client, "PATCH", path, revision=entry["revision"], name="Credits", enabled=False, **filters)
        self.assertEqual(
            (edited.json()["name"], edited.json()["enabled"], edited.json()["roles"], edited.json()["profiles"]),
            ("Credits", False, ["triage"], filters["profiles"]),
        )
        self.assertEqual(edited.json()["version"], 1)
        self.assertEqual(self.write(client, "PATCH", path, revision=entry["revision"], enabled=True).status_code, 409)
        replaced = self.write(
            client, "PUT", path + "/secret", revision=edited.json()["revision"], secret=OPENAI + "replacement"
        )
        self.assertEqual((replaced.json()["version"], replaced.json()["name"]), (2, "Credits"))
        # Stale route-addressed clients get a reload instruction instead of a guessed entry.
        for method in ("PUT", "DELETE"):
            old = self.write(client, method, "/openai_api", secret=OPENAI)
            self.assertEqual((old.status_code, "Reload" in old.json()["detail"]), (410, True))
        # Removing at a revision read before the replacement cannot destroy the newer version.
        self.assertEqual(self.remove(client, edited.json()).status_code, 409)
        self.assertEqual(client.delete("/api/me/credentials" + path, headers={CSRF_HEADER: "1"}).status_code, 410)
        self.assertEqual(self.store.group(101, "openai")["entries"][0]["record"]["status"], "valid")
        removed = self.remove(client, replaced.json())
        self.assertEqual((removed.json()["status"], removed.json()["removed"]), ("revoked", True))
        self.assertEqual(self.remove(client, replaced.json()).json(), removed.json())
        listing = client.get("/api/me/credentials").json()
        self.assertEqual(
            [(g["provider"], len(g["entries"])) for g in listing["providers"]], [("anthropic", 1), ("openai", 0)]
        )
        self.assertEqual(len(listing["types"]), 4)
        self.assertEqual([r["key"] for r in listing["roles"]][0], "triage")
        for marker in (OPENAI, ALICE, BOB, "synthetic-cli-token"):
            self.assertNotIn(marker, json.dumps(listing))

    def test_reordering_commits_exact_lists_and_refuses_concurrent_changes(self):
        """The provider list revision fences reorders; entries of other providers and owners stay put."""
        client, alice, _ = self.browser()
        client.cookies.set(SESSION_COOKIE, alice)
        ids = [
            self.write(client, "POST", "", type=kind, name=name, secret="synthetic-" + name).json()["id"]
            for kind, name in (
                ("anthropic_api", "credits"),
                ("claude_subscription", "personal"),
                ("claude_subscription", "work"),
            )
        ]
        order = [ids[1], ids[2], ids[0]]
        reply = self.write(client, "PUT", "/order/anthropic", revision=3, entries=order, mutation_id="o" * 16)
        self.assertEqual((reply.json()["revision"], [e["id"] for e in reply.json()["entries"]]), (4, order))
        self.assertEqual(
            self.write(client, "PUT", "/order/anthropic", revision=3, entries=order, mutation_id="o" * 16).json(),
            reply.json(),
        )
        for entries in (ids, ids[:2], [*ids, ids[0]]):
            stale = self.write(client, "PUT", "/order/anthropic", revision=3 if entries == ids else 4, entries=entries)
            self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.write(client, "PUT", "/order/gemini", revision=0, entries=[]).status_code, 404)
        listed = client.get("/api/me/credentials").json()["providers"][0]
        self.assertEqual([e["name"] for e in listed["entries"]], ["personal", "work", "credits"])

    def test_both_subscription_forms_cross_real_bff_and_vault_with_two_owners(self):
        """Token/account candidate fields and replacement/removal remain owner-bound through both apps."""
        client, alice, bob = self.browser()
        routes = {"chatgpt_subscription": "chatgpt-subscription", "claude_subscription": "claude-subscription"}
        for ui, route in routes.items():
            for index, sid in enumerate((alice, bob)):
                client.cookies.clear()
                client.cookies.set(SESSION_COOKIE, sid)
                token = TOKENS[route][index]
                body = {"secret": token, "models": ["model-a", "model-b", "model-a"], "owner": 999}
                if ui == "chatgpt_subscription":
                    body["account_id"] = "account-alice" if index == 0 else "account-bob"
                result = self.write(client, "POST", "", type=ui, name=ui, **body)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertNotIn("models", result.json())
                self.assertFalse(result.json()["expiryKnown"])
                self.assertFalse(result.json()["accountVerified"])
                self.assertNotIn(token, result.text)
                path = "/" + result.json()["id"]
                replacement = self.write(client, "PUT", path + "/secret", revision=result.json()["revision"], **body)
                self.assertEqual(replacement.json()["version"], 2)
                current = self.store.group(101 if index == 0 else 202, result.json()["provider"])["entries"]
                self.assertTrue(any(row["route"] == route and row["record"]["version"] == 2 for row in current))
                removed = self.remove(client, replacement.json())
                self.assertEqual(removed.json()["status"], "revoked")
                self.assertEqual(self.remove(client, replacement.json()).json(), removed.json())
        self.assertEqual({row["user_id"] for row in self.rows()}, {101, 202})
        self.assertNotIn(999, {row["user_id"] for row in self.rows()})
