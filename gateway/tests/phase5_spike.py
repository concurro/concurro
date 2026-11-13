"""Opt-in Phase5: real portal/vault/exchange and pinned gateway/CLIs, fake provider accounts only."""

import argparse
import json
import os
import sys
import time
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT),
    str(ROOT / "engine"),
    str(ROOT / "gateway/exchange"),
    str(ROOT / "gateway/vault"),
    str(ROOT / "backoffice/tests"),
]

# isort: split
from fastapi.testclient import TestClient
from test_backoffice import FakeOAuth, settings

from vault_fixtures import ALICE, BOB, FakeTransport

import admissions
import turns
import vault
from backoffice.app import CSRF_HEADER, SESSION_COOKIE, create_app as portal_app
from backoffice.auth import Sessions, User
from backoffice.credentials import Vault as PortalVault
from credentials import Credential
from exchange import Vault as GatewayVault
from phase4_spike import CLI_VERSIONS, IMAGE, RuntimeTests as InferenceTests
from policy import digest
from spike_fixtures import file_digest, request
from test_exchange_funding import funded_turn, publish
from test_vault_resolver import listeners

METADATA, GATEWAY = digest("phase5-synthetic-metadata-principal"), digest("phase5-synthetic-gateway-principal")
OWNERS = {"101": (ALICE, "alice"), "202": (BOB, "bob")}
UI_ROUTES = {"chatgpt-subscription": "chatgpt_subscription", "claude-subscription": "claude_subscription"}


class RecordingVault(GatewayVault):
    """Record only exact lookup identities; the real private HTTP client remains authoritative."""

    def __init__(self, public_url, private_url):
        """Keep synthetic scan markers separate from actual secret selection."""
        super().__init__(public_url, METADATA, private_url, GATEWAY)
        self.calls = []
        self.material = {"scan-metadata": {"secret": METADATA}, "scan-gateway": {"secret": GATEWAY}}
        self.material.update({"scan-owner-" + owner: {"secret": token} for owner, (token, _) in OWNERS.items()})

    def resolve(self, binding):
        """Resolve through the production gateway principal, then record the fixture's marker."""
        self.calls.append({key: binding[key] for key in ("sponsor", "route", "credential", "credential_version")})
        value = super().resolve(binding)
        self.material[binding["credential"]] = value
        return value


class RuntimeTests(InferenceTests):
    """Reuse native config/headers/scans, replacing the fake vault with real HTTP owner storage."""

    def setUp(self):
        """Bind separate real public/private vault sockets and a real BFF over synthetic identities."""
        super().setUp()
        self.store = vault.Store(self.root / "vault.db", vault.Envelope(os.urandom(32)))
        self.context.callback(self.store.close)
        self.validation_calls = []

        @contextmanager
        def probe(url, headers, body):
            """Change only the fixed upstream origin to this test's existing local provider."""
            route = next(route for route in UI_ROUTES if url.startswith(vault.PROTOCOL["UPSTREAMS"][route] + "/"))
            suffix = url.removeprefix(vault.PROTOCOL["UPSTREAMS"][route])
            self.validation_calls.append(
                {"route": route, "model": body["model"], "account_supplied": "ChatGPT-Account-Id" in headers}
            )
            with vault.http_probe(self.exchange.inference.upstreams[route] + suffix, headers, body) as result:
                yield result

        public = vault.create_app(
            self.store,
            vault.Identity(FakeTransport()),
            vault.Validator(FakeTransport(), probe=probe),
            METADATA,
            (GATEWAY,),
        )
        private = vault.create_resolver(self.store, GATEWAY)
        self.public_url, self.private_url = self.context.enter_context(listeners(public, private))
        self.vault = RecordingVault(self.public_url, self.private_url)
        self.exchange.vault = self.vault
        self.portal_sessions = Sessions()
        self.browser = TestClient(
            portal_app(
                settings(public_url="http://testserver"),
                oauth=FakeOAuth(),
                sessions=self.portal_sessions,
                vault=PortalVault(self.public_url),
            )
        )
        self.context.callback(self.browser.close)
        self.cookies = {
            owner: self.portal_sessions.create(token, User(int(owner), login, login, ""), 3600)
            for owner, (token, login) in OWNERS.items()
        }
        self.journey = []
        self.candidates = {}
        self.tokens = {}

    def owner(self, owner):
        """Select one browser GitHub session; the vault independently resolves its user token."""
        self.browser.cookies.clear()
        self.browser.cookies.set(SESSION_COOKIE, self.cookies[owner])

    def register(self, route, owner, revision=1, mode="ok"):
        """Paste a synthetic CLI candidate through the actual BFF, validation and encrypted vault."""
        secret = "SYNTHETIC_SUBSCRIPTION_" + route + "_" + owner + "_" + str(revision)
        self.vault.material["scan-candidate-" + secret] = {"secret": secret}
        account = "synthetic-account-" + owner if route == "chatgpt-subscription" else None
        alias = "chatgpt" if account else "claude"
        self.state.credentials[secret] = {"route": alias, "account": account}
        self.state.pending[secret] = {"token": secret, "account": account, "route": alias, "mode": mode}
        model = "gpt-test" if account else "claude-test"
        body = {"secret": secret, "models": [model, model + "-other", model]}
        if account:
            body["account_id"] = account
        self.owner(owner)
        reply = self.browser.put("/api/me/credentials/" + UI_ROUTES[route], headers={CSRF_HEADER: "1"}, json=body)
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertFalse(reply.json()["expiryKnown"])
        self.assertFalse(reply.json()["accountVerified"])
        self.assertEqual(reply.json()["models"], [model, model + "-other"])
        self.candidates[route, owner] = self.store.listing(int(owner))[-1]
        self.tokens[route, owner] = secret
        return reply.json()

    def admit(self, route, owner):
        """Freeze the real stored version; only an explicit owner change advances the provider epoch."""
        provider = "openai" if route == "chatgpt-subscription" else "anthropic"
        current = self.exchange.db.execute(
            "SELECT epoch,slot FROM sponsorship WHERE provider=?", (provider,)
        ).fetchone()
        slot = json.loads(current[1]) if current else None
        transfer = slot is not None and slot["sponsor_id"] != owner
        epoch = current[0] + int(transfer) if current else 1
        identifier, binding, payload = funded_turn(
            provider, actor=owner, epoch=epoch, grant=slot["grant_id"] if slot and not transfer else None
        )
        if slot and not transfer:
            payload["request"]["admission"]["grant"] = slot
        else:
            publish(
                self.exchange,
                binding,
                payload["request"]["admission"]["grant"],
                reason="sponsor" if current else "initialize",
            )
        entry = next(row for row in self.store.listing(int(owner)) if row["route"] == route)
        model = "gpt-test" if provider == "openai" else "claude-test"
        credential = Credential(
            digest(["contributed", owner, route, entry["record_id"], entry["version"]]),
            "codex" if provider == "openai" else "claude",
            "contributed",
            "gateway",
            owner,
            route,
            entry["record_id"],
            entry["version"],
        )
        slot = payload["request"]["admission"]["grant"]
        payload["request"].update(
            model=model,
            admission=admissions.freeze(admissions.selection(credential, provider, slot), len(self.sessions) + 1, 1),
        )
        binding.update(route=route, credential=entry["record_id"], credential_version=entry["version"])
        reply = self.exchange.submit(identifier, self.expected(), [model], 1000, 3600, payload, binding=binding)
        session = reply.get("dispatch")
        if session:
            grant = self.exchange.key(
                session,
                self.signer.token(
                    ref="refs/heads/concurro/runs/" + session, actor_id="3003", run_id=str(len(self.sessions) + 77)
                ),
                engine=binding["engine"],
            )
        else:
            staffing = self.exchange.db.execute("SELECT staffing FROM turns WHERE id=?", (identifier,)).fetchone()[0]
            session = self.exchange.db.execute(
                "SELECT id FROM sessions WHERE staffing=? AND ended IS NULL", (staffing,)
            ).fetchone()[0]
            grant = next(g for g in self.sessions if g["session"] == session)
        claimed = self.exchange.next(session, grant["token"], 1, 0)["turns"]
        self.assertEqual([turn["id"] for turn in claimed], [identifier])
        result = {
            **grant,
            "session": session,
            "turn": identifier,
            "model": model,
            "provider": credential.provider,
            "binding": binding,
            "record": entry["record_id"],
        }
        self.sessions.append(result)
        return result

    @staticmethod
    def expected():
        """Reuse the exact fixture workflow identity without importing any real deployment config."""
        from exchange_fixtures import EXPECTED

        return {**EXPECTED, "actor_id": "3003"}

    def test_both_routes_owner_lifecycle_retained_resume_takeover_and_revoke(self):
        """Real pasted versions feed pinned CLI calls, expiry, replacement and graceful ownership changes."""
        for route in UI_ROUTES:
            for owner in OWNERS:
                self.register(route, owner)
            first = self.admit(route, "101")
            native_dir = self.root / (route + "-native-first")
            thread, answer = self.native(first, native_dir, "Remember MEMORY_FIRST. Return a structured answer.")
            archive = turns.pack(native_dir / "retained")
            self.exchange.complete(
                first["session"], first["token"], first["turn"], {"outcome": "ok", "response": answer}
            )
            failure_turn = self.admit(route, "101")
            expiry = datetime.fromtimestamp(time.time() - 1, UTC).isoformat()
            self.store.db.execute(
                "UPDATE credential_metadata SET expires_at=?,expiry_known=1 WHERE user_id=101 AND route=? AND version=1",
                (expiry, route),
            )
            before = len(self.state.calls)
            self.assertGreaterEqual(self.call(failure_turn)[0], 400)
            self.assertEqual(len(self.state.calls), before)
            self.assertEqual(self.exchange.inference.failure(failure_turn["turn"])["category"], "auth")
            with self.assertRaises(AssertionError):
                self.native(failure_turn, self.root / (route + "-native-expired"), "Return a structured answer.")
            self.assertEqual(len(self.state.calls), before)
            before_turns = self.exchange.db.execute("SELECT count(*) FROM turns").fetchone()[0]
            epoch = self.exchange.db.execute(
                "SELECT epoch FROM sponsorship WHERE provider=?", (first["binding"]["sponsorship_provider"],)
            ).fetchone()[0]
            self.register(route, "101", revision=2)
            self.exchange.consume_events()
            self.assertEqual(self.exchange.db.execute("SELECT count(*) FROM turns").fetchone()[0], before_turns)
            self.assertEqual(self.exchange.poll(first["turn"], 0)["result"]["outcome"], "ok")
            retry = self.admit(route, "101")
            self.assertEqual(retry["binding"]["epoch"], epoch)
            self.assertEqual(retry["binding"]["credential_version"], 2)
            self.assertNotEqual(retry["session"], first["session"])
            retry_dir = self.root / (route + "-native-retry")
            retry_start = len(self.state.calls)
            resumed, retry_answer = self.native(
                retry, retry_dir, "Return the remembered token in the required schema.", resume=thread, archive=archive
            )
            retry_memory = any("MEMORY_FIRST" in json.dumps(c["body"]) for c in self.state.calls[retry_start:])
            self.assertTrue(retry_memory)
            self.assertEqual(retry_answer["answer"], "MEMORY_FIRST")
            self.assertEqual(resumed, thread)
            newer_archive = turns.pack(retry_dir / "retained")
            takeover = self.admit(route, "202")
            self.assertEqual(takeover["binding"]["epoch"], epoch + 1)
            self.assertEqual(self.call(retry)[0], 200)  # An old admitted owner remains valid until its own revoke.
            takeover_start = len(self.state.calls)
            taken, takeover_answer = self.native(
                takeover,
                self.root / (route + "-native-takeover"),
                "Return the remembered token in the required schema.",
                resume=thread,
                archive=newer_archive,
            )
            self.assertEqual(taken, thread)
            takeover_memory = any("MEMORY_FIRST" in json.dumps(c["body"]) for c in self.state.calls[takeover_start:])
            self.assertTrue(takeover_memory)
            self.assertEqual(takeover_answer["answer"], "MEMORY_FIRST")
            self.owner("101")
            self.assertEqual(
                self.browser.delete("/api/me/credentials/" + UI_ROUTES[route], headers={CSRF_HEADER: "1"}).status_code,
                200,
            )
            self.exchange.consume_events()
            before = len(self.state.calls)
            self.assertGreaterEqual(self.call(retry)[0], 400)
            self.assertEqual(len(self.state.calls), before)
            self.assertEqual(self.call(takeover)[0], 200)
            self.owner("202")
            self.browser.delete("/api/me/credentials/" + UI_ROUTES[route], headers={CSRF_HEADER: "1"})
            self.exchange.consume_events()
            before = len(self.state.calls)
            self.assertGreaterEqual(self.call(takeover)[0], 400)
            self.assertEqual(len(self.state.calls), before)
            self.journey.append(
                {
                    "route": route,
                    "first": first["binding"],
                    "retry": retry["binding"],
                    "takeover": takeover["binding"],
                    "retained_thread_preserved": taken == resumed == thread,
                    "retained_content_preserved": retry_memory and takeover_memory,
                    "replacement_created_no_turns": self.exchange.db.execute("SELECT count(*) FROM turns").fetchone()[0]
                    == before_turns + 2,
                }
            )
        self.assertTrue(all(call["auth_matches"] for call in self.state.calls))
        self.assertFalse(any(call["route"] == "operator" for call in self.state.calls))
        self.assertEqual(
            request(self.public_url + "/resolve", "POST", {}, [("Authorization", "Bearer " + GATEWAY)])[0], 404
        )

    def test_missing_or_malformed_versioned_account_never_calls_upstream(self):
        """Actual sealed account metadata must supply a valid fixed-route header, never an org default."""
        route = "chatgpt-subscription"
        self.register(route, "101")
        for account in (
            None,
            {"wrong_field": "synthetic"},
            {"chatgpt_account_id": 17},
            {"chatgpt_account_id": "bad\r\nheader"},
        ):
            self.store.put(vault.User(101, "alice"), route, self.tokens[route, "101"], ["gpt-test"], account=account)
            self.exchange.consume_events()
            grant = self.admit(route, "101")
            before = len(self.state.calls)
            self.assertGreaterEqual(self.call(grant)[0], 400)
            self.assertEqual(len(self.state.calls), before)
            self.exchange.end(grant["session"])

    def test_unverified_backend_auth_rejection_remains_unknown_until_local_revoke(self):
        """An actual fake401 is redacted without inventing a subscription error taxonomy."""
        for route in UI_ROUTES:
            self.register(route, "101")
            self.state.pending[self.tokens[route, "101"]]["mode"] = "auth"
            grant = self.admit(route, "101")
            result = self.call(grant)
            self.assertEqual(result[0], 401)
            events = self.exchange.inference.events(grant["binding"]["repository"], grant["binding"]["issue"])
            self.assertEqual(events[-1]["category"], "unknown")
            self.assertNotIn("FAKE_PROVIDER_DETAIL_SENTINEL", result[2])
            self.owner("101")
            self.browser.delete("/api/me/credentials/" + UI_ROUTES[route], headers={CSRF_HEADER: "1"})
            self.exchange.consume_events()
            self.assertIsNotNone(self.exchange.row(grant["session"])["ended"])

    def persist(self):
        """Preserve nonsecret journey evidence and reuse the actual gateway request/log secret scans."""
        if hasattr(self, "journey"):
            (self.root / "owner-journey.json").write_text(
                json.dumps(
                    {"steps": self.journey, "validation_calls": self.validation_calls, "events": self.store.events()},
                    indent=2,
                )
            )
        super().persist()


def main():
    """Run only Phase5 cases with immutable source identities and retained raw evidence."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--clients", type=Path, required=True)
    parser.add_argument("--case", action="append")
    options = parser.parse_args()
    options.output.mkdir(exist_ok=False)
    source_paths = [
        "gateway/tests/phase5_spike.py",
        "gateway/tests/phase4_spike.py",
        "gateway/tests/sponsorship_spike.py",
        "gateway/tests/test_vault_resolver.py",
        "gateway/tests/vault_fixtures.py",
        "backoffice/tests/test_backoffice.py",
        "gateway/tests/spike_fixtures.py",
        "gateway/tests/exchange_fixtures.py",
        "gateway/tests/test_exchange_funding.py",
        "gateway/vault/vault.py",
        "gateway/provider_protocol.py",
        "gateway/exchange/exchange.py",
        "gateway/exchange/inference.py",
        "gateway/agentgateway.yaml",
        "engine/credential_routes.py",
        "engine/admissions.py",
        "engine/agent_entry.py",
        "engine/agent-clients.txt",
        "engine/turns.py",
        "engine/processes.py",
        "backoffice/app.py",
        "backoffice/credentials.py",
        "backoffice/auth.py",
    ]
    source_hashes = {name: file_digest(ROOT / name) for name in source_paths}
    (options.output / "sources-at-launch.json").write_text(json.dumps(source_hashes, indent=2))
    RuntimeTests.options = options
    names = options.case or [
        "test_both_routes_owner_lifecycle_retained_resume_takeover_and_revoke",
        "test_unverified_backend_auth_rejection_remains_unknown_until_local_revoke",
        "test_missing_or_malformed_versioned_account_never_calls_upstream",
    ]
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(RuntimeTests(name) for name in names))
    manifest = {
        "image": IMAGE,
        "clis": CLI_VERSIONS,
        "sources": source_hashes,
        "sources_unchanged": all(file_digest(ROOT / name) == value for name, value in source_hashes.items()),
        "success": result.wasSuccessful(),
        "tests": result.testsRun,
        "artifacts": {
            str(p.relative_to(options.output)): file_digest(p)
            for p in options.output.rglob("*")
            if p.is_file() and not p.name.startswith(("exchange.db", "vault.db")) and p.name != "key.pem"
        },
    }
    (options.output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    assert manifest["sources_unchanged"], "source changed during acceptance"
    raise SystemExit(not result.wasSuccessful())


if __name__ == "__main__":
    main()
