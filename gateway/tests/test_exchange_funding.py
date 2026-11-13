"""Frozen provider admissions through actual Exchange storage, runner claims and retirement."""

import sys
import threading
import unittest
import uuid
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from exchange_fixtures import BINDING, FakeGateway

from test_exchange_structure import EXPECTED_RUNNER, NOW, HttpFixture, StateFixture

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "engine"))
import admissions
from credentials import Credential
from exchange import Gateway, Refused, Vault
from policy import digest
from relay import ExchangeError, Operator
from test_exchange_sponsorship import transition


class FundedGateway(FakeGateway):
    """Isolated credential-aware adapter; it never calls a subscription backend."""

    contributed_routes = ("openai-api-key", "anthropic-api-key")

    def create(self, key, name, models, budget, binding=None):
        """Retain exact selected identity alongside the existing fake enforcement inputs."""
        identifier = super().create(key, name, models, budget)
        self.resources[identifier]["binding"] = deepcopy(binding)
        return identifier


class MetadataVault:
    """Two synthetic accounts plus exact-version retirement events, without secret resolution."""

    def __init__(self):
        """Use different immutable records for independently funded provider slots."""
        self.records = [
            {
                "user_id": actor,
                "route": route,
                "record_id": mark * 64,
                "version": 1,
                "kind": "api_key",
                "status": "valid",
                "models": ["synthetic-model"],
                "expiry_known": False,
                "expires_at": None,
            }
            for actor, route, mark in (("101", "openai-api-key", "a"), ("202", "anthropic-api-key", "b"))
        ]
        self.log = []

    def read(self, actor):
        """Return copies so callers cannot mutate the authoritative fixture's versions."""
        return deepcopy([r for r in self.records if r["user_id"] == actor])

    def events(self, after):
        """Return durable nonsecret events in vault sequence order."""
        values = [e for e in self.log if e["sequence"] > after]
        return {"events": deepcopy(values), "next": values[-1]["sequence"] if values else after, "more": False}

    def revoke(self, actor="101"):
        """Retire exactly one user's record/version without changing the other provider."""
        record = next(r for r in self.records if r["user_id"] == actor)
        record["status"] = "revoked"
        self.log.append({**record, "user_id": int(actor), "sequence": len(self.log) + 1, "type": "revoked"})


def funded_turn(provider="openai", actor=None, epoch=1, generation=1, grant=None):
    """Use production admission freezing and credential identity for a synthetic native turn."""
    actor = actor or ("101" if provider == "openai" else "202")
    route = provider + "-api-key"
    record = ("c" if actor == "303" else "a" if provider == "openai" else "b") * 64
    harness = "codex" if provider == "openai" else "claude"
    slot = {
        "sponsor_id": actor,
        "epoch": epoch,
        "grant_id": grant or digest([actor, provider, epoch]),
        "status": "active",
        "provenance": "explicit_comment",
    }
    credential = Credential(
        digest(["contributed", actor, route, record, 1]), harness, "contributed", "gateway", actor, route, record, 1
    )
    admission = admissions.freeze(admissions.selection(credential, provider, slot), 1, generation)
    attempt = uuid.uuid4().hex
    binding = {
        **BINDING,
        "role": "plan:" + harness,
        "generation": generation,
        "version": 2,
        "funding": "contributed",
        "sponsorship_provider": provider,
        "sponsor": actor,
        "epoch": epoch,
        "grant_id": slot["grant_id"],
        "route": route,
        "credential": record,
        "credential_version": 1,
    }
    payload = {
        "request": {
            "provider": harness,
            "stage": "plan",
            "model": "synthetic-model",
            "admission": admission,
            "attempt": attempt,
        }
    }
    return admissions.turn_id(attempt), binding, payload


def org_turn(generation=2, task=None):
    """Freeze a real organization admission used to advance task ownership in race tests."""
    credential = Credential("gateway:claude", "claude", "org", "gateway-key")
    choice = admissions.selection(credential)
    binding = {
        **BINDING,
        **(task or {}),
        "generation": generation,
        "version": 2,
        **{key: choice[key] for key in ("funding", "sponsorship_provider", "sponsor", "epoch", "grant_id", "route")},
        "credential": credential.id,
        "credential_version": None,
    }
    attempt = uuid.uuid4().hex
    payload = {
        "request": {
            "provider": "claude",
            "stage": "plan",
            "model": "synthetic-model",
            "admission": admissions.freeze(choice, 1, generation),
            "attempt": attempt,
        }
    }
    return admissions.turn_id(attempt), binding, payload


def publish(exchange, binding, slot, reason="sponsor", actor=None, cutoff=None):
    """Publish a genuine provider CAS transition using the stored current epoch."""
    row = exchange.db.execute(
        "SELECT epoch FROM sponsorship WHERE repository=? AND issue=? AND provider=?",
        (binding["repository"], binding["issue"], binding["sponsorship_provider"]),
    ).fetchone()
    value = {
        "repository": binding["repository"],
        "issue": binding["issue"],
        "provider": binding["sponsorship_provider"],
        "generation": binding["generation"],
        "expected_epoch": row[0] if row else -1,
        "slot": slot,
        "reason": reason,
        "actor": actor,
        "cutoff": cutoff,
    }
    value["transition"] = digest(value)
    return exchange.sponsorship(value)


class FundingTests(StateFixture, HttpFixture, unittest.TestCase):
    """Exercise durable payer identity and key lifecycle without live provider credentials."""

    def setUp(self):
        """Retain existing OIDC/state fixture with only gateway and vault boundaries replaced."""
        super().setUp()
        self.gateway = FundedGateway()
        self.vault = MetadataVault()
        self.exchange.gateway, self.exchange.vault = self.gateway, self.vault

    def send(self, case=None, instance=None):
        """Submit an already frozen request using its exact attempt UUID."""
        case = case or funded_turn()
        turn, binding, payload = case
        return (instance or self.exchange).submit(
            turn, EXPECTED_RUNNER, ["synthetic-model"], 1000, 3600, payload, binding=binding, now=NOW
        )

    def test_exact_replay_precedes_outage_and_does_not_rebind(self):
        """A lost admission reply and saved completion retain their original payer and dispatch."""
        case = funded_turn()
        first = self.send(case)
        self.vault.read = lambda actor: self.fail("replay read current metadata")
        self.exchange.gateway.contributed_routes = ()
        self.assertEqual(self.send(case), first)
        changed = deepcopy(case)
        changed[2]["request"]["admission"]["call"] = 2
        with self.assertRaisesRegex(Refused, "turn ID"):
            self.send(changed)
        self.assertEqual(self.exchange.db.execute("SELECT count(*) FROM turns").fetchone()[0], 1)

    def test_metadata_version_model_owner_expiry_and_adapter_checks(self):
        """A new turn cannot select latest credentials, absent models or the organization adapter."""
        for field, value in (
            ("version", 2),
            ("status", "revoked"),
            ("models", None),
            ("user_id", "202"),
            ("kind", "subscription"),
            ("expires_at", "1970-01-01T00:00:01Z"),
        ):
            with self.subTest(field=field):
                original = deepcopy(self.vault.records)
                self.vault.records[0][field] = value
                if field == "expires_at":
                    self.vault.records[0]["expiry_known"] = True
                with self.assertRaises(Refused):
                    self.send()
                self.vault.records = original
        self.exchange.gateway.contributed_routes = ()
        with self.assertRaisesRegex(Refused, "adapter"):
            self.send()
        self.assertEqual(self.exchange.db.execute("SELECT count(*) FROM turns").fetchone()[0], 0)
        with self.assertRaisesRegex(Refused, "adapter"):
            Gateway.create(self.gateway, "fake", "session", ["synthetic-model"], 1000, binding=funded_turn()[1])

    def test_wire_rejects_changed_payer_attempt_and_transcript(self):
        """The admission cannot disagree with session binding or transport UUID."""
        for mutate in (
            lambda r: r.update(attempt=uuid.uuid4().hex),
            lambda r: r["admission"].update(sponsor="202"),
            lambda r: r.update(session={"scope": "f" * 64}),
            lambda r: r["admission"]["credential"].update(version=2),
        ):
            case = funded_turn()
            mutate(case[2]["request"])
            with self.assertRaises(ValueError):
                self.send(case)
        self.assertEqual(self.exchange.db.execute("SELECT count(*) FROM turns").fetchone()[0], 0)

    def test_transfer_withdraw_older_own_epoch_and_other_provider(self):
        """Transfer is graceful; old caller withdrawal affects only their own provider history."""
        old = funded_turn()
        other = funded_turn("anthropic")
        a, b = self.send(old), self.send(other)
        ta, tb = self.claim(a["dispatch"]), self.claim(b["dispatch"])
        takeover = funded_turn(actor="303", epoch=2)
        publish(self.exchange, takeover[1], takeover[2]["request"]["admission"]["grant"])
        self.vault.records.append({**self.vault.records[0], "user_id": "303", "record_id": "c" * 64})
        current = self.send(takeover)
        current_token = self.claim(current["dispatch"])
        self.assertEqual(len(self.exchange.next(a["dispatch"], ta, 1, 0, now=NOW)["turns"]), 1)
        slot = deepcopy(takeover[2]["request"]["admission"]["grant"])
        publish(self.exchange, takeover[1], slot, "withdraw", "101", 2)
        with self.assertRaises(Refused):
            self.exchange.next(a["dispatch"], ta, 1, 0, now=NOW)
        self.assertEqual(len(self.exchange.next(b["dispatch"], tb, 1, 0, now=NOW)["turns"]), 1)
        self.assertEqual(len(self.exchange.next(current["dispatch"], current_token, 1, 0, now=NOW)["turns"]), 1)
        self.assertEqual(
            self.exchange.db.execute("SELECT epoch FROM sponsorship WHERE provider='openai'").fetchone()[0], 2
        )
        with self.assertRaises(Refused):
            self.send(funded_turn(epoch=1))
        slot = {"sponsor_id": None, "epoch": 3, "status": "withdrawn"}
        publish(self.exchange, takeover[1], slot, "withdraw", "303", 2)
        later = funded_turn(actor="101", epoch=4)
        publish(self.exchange, later[1], later[2]["request"]["admission"]["grant"])
        self.assertTrue(self.send(later)["dispatch"])

    def test_revocation_cursor_restart_and_failed_key_delete(self):
        """Event retirement commits before cleanup and repeats safely across process restart."""
        first, other = self.send(), self.send(funded_turn("anthropic"))
        self.claim(first["dispatch"])
        self.claim(other["dispatch"])
        self.gateway.delete = lambda identifier: False
        self.vault.revoke()
        self.exchange.consume_events()
        self.assertIsNotNone(self.exchange.row(first["dispatch"])["ended"])
        self.assertIsNone(self.exchange.row(other["dispatch"])["ended"])
        restarted = self.open()
        restarted.gateway, restarted.vault = self.gateway, self.vault
        self.assertEqual(restarted.db.execute("SELECT sequence FROM vault_cursor").fetchone()[0], 1)
        self.assertEqual(restarted.db.execute("SELECT count(*) FROM credential_revocations").fetchone()[0], 1)
        self.gateway.delete = lambda identifier: bool(self.gateway.resources.pop(identifier, None))
        restarted.reconcile(now=NOW)
        self.assertIsNone(restarted.row(first["dispatch"])["key"])
        self.assertIsNotNone(restarted.row(other["dispatch"])["key"])

    def test_event_cursor_and_retirement_insert_rollback_together(self):
        """A failed transaction cannot consume an event without its exact-version cutoff."""
        self.vault.revoke()
        self.exchange.db.execute(
            "CREATE TRIGGER fail_cursor BEFORE UPDATE ON vault_cursor BEGIN SELECT RAISE(ABORT, 'fixture crash'); END"
        )
        with self.assertRaises(Exception):
            self.exchange.consume_events()
        self.assertEqual(self.exchange.db.execute("SELECT sequence FROM vault_cursor").fetchone()[0], 0)
        self.assertEqual(self.exchange.db.execute("SELECT count(*) FROM credential_revocations").fetchone()[0], 0)
        self.exchange.db.execute("DROP TRIGGER fail_cursor")
        self.exchange.consume_events()
        self.assertEqual(self.exchange.db.execute("SELECT sequence FROM vault_cursor").fetchone()[0], 1)

    def test_revocation_during_activation_never_returns_a_key(self):
        """Revocation proceeds during contributed gateway I/O; its late key is deleted before reply."""
        queued = self.send()
        session = queued["dispatch"]
        entered, release, outcomes = threading.Event(), threading.Event(), []
        original = self.gateway.create

        def paused(*args, **kwargs):
            """Hold only the fake external gateway operation."""
            entered.set()
            if not release.wait(5):
                raise AssertionError("fixture timeout")
            return original(*args, **kwargs)

        def claim():
            """Retain the actual runner refusal without hiding background exceptions."""
            try:
                outcomes.append(self.claim(session))
            except BaseException as error:
                outcomes.append(error)

        self.gateway.create = paused
        thread = threading.Thread(target=claim)
        thread.start()
        self.addCleanup(lambda: (release.set(), thread.join(5)))
        self.assertTrue(entered.wait(5))
        self.vault.revoke()
        self.exchange.consume_events()
        self.assertIsNotNone(self.exchange.row(session)["ended"])
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcomes[0], Refused)
        self.assertEqual(self.gateway.list(), [])

    def test_partial_activation_adopts_existing_budget(self):
        """Lost activation reply reuses the recorded key without resetting already spent budget."""
        session = self.send()["dispatch"]
        original = self.gateway.create

        def lost(*args, **kwargs):
            """Create, consume budget, then lose the external reply."""
            identifier = original(*args, **kwargs)
            self.gateway.resources[identifier]["budget"] = 7
            raise Refused(503, "synthetic lost reply")

        self.gateway.create = lost
        with self.assertRaises(Refused):
            self.claim(session)
        saved = self.exchange.row(session)["key"]
        self.gateway.create = lambda *args, **kwargs: self.fail("retry reset budget")
        self.claim(session)
        self.assertEqual(self.exchange.row(session)["key"], saved)
        self.assertEqual(next(iter(self.gateway.resources.values()))["budget"], 7)

    def test_generation_fence_does_not_rewrite_org_or_other_task(self):
        """New ownership invalidates contributed execution, with legacy org cleanup semantics retained."""
        case = funded_turn()
        first = self.send(case)
        token = self.claim(first["dispatch"])
        later = funded_turn(generation=2)
        self.send(later)
        with self.assertRaises(Refused):
            self.exchange.next(first["dispatch"], token, 1, 0, now=NOW)
        self.assertEqual(self.send(case)["turn"], first["turn"])
        with self.assertRaises(Refused):
            self.send(funded_turn(generation=1))

    def test_direct_personal_key_issue_cannot_bypass_turn_admission(self):
        """Only admitted model turns can staff personal sessions; host keys remain organization-only."""
        binding = funded_turn()[1]
        with self.assertRaisesRegex(Refused, "admitted turn"):
            self.exchange.issue(EXPECTED_RUNNER, ["synthetic-model"], 1000, 3600, binding=binding)
        with self.exchange.locked():
            with self.assertRaisesRegex(ValueError, "host sessions"):
                self.exchange.create(EXPECTED_RUNNER, ["synthetic-model"], 1000, 3600, kind="host", binding=binding)

    def test_deadline_crossed_during_activation_is_rechecked(self):
        """A late external key cannot escape after the session's recorded deadline."""
        session = self.send()["dispatch"]
        original = self.gateway.create

        def late(*args, **kwargs):
            """Advance only the controlled clock while the fake key is being activated."""
            identifier = original(*args, **kwargs)
            import exchange

            exchange.time.time.return_value = NOW + 4000
            return identifier

        self.gateway.create = late
        with self.assertRaisesRegex(Refused, "authority ended"):
            self.exchange.key(session, self.token(session), engine=BINDING["engine"])
        self.assertEqual(self.gateway.list(), [])

    def test_org_cleanup_continues_during_event_outage(self):
        """An optional private-channel outage cannot prevent ordinary organization key cleanup."""
        queued = super().submit()
        self.claim(queued["dispatch"])
        self.vault.events = lambda after: (_ for _ in ()).throw(Refused(503, "synthetic event outage"))
        with self.assertRaises(Refused):
            self.exchange.reconcile(now=NOW + 4000)
        self.assertIsNotNone(self.exchange.row(queued["dispatch"])["ended"])
        self.assertEqual(self.gateway.list(), [])

    def test_stale_transition_refusal_identifies_only_unapplied_id_over_http(self):
        """The private wire preserves exact non-application proof instead of treating every409 alike."""
        self.send(org_turn())
        pending = transition(generation=1)
        host, _ = self.start(self.exchange, budget=1000000)
        operator = Operator("http://127.0.0.1:" + str(host.server_address[1]), "operator")
        with self.assertRaises(ExchangeError) as caught:
            operator.sponsorship({"transitions": [pending]})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "stale_sponsorship_generation")
        self.assertEqual(caught.exception.transition, pending["transition"])
        self.assertIsNone(
            self.exchange.db.execute(
                "SELECT id FROM sponsorship_transitions WHERE id=?", (pending["transition"],)
            ).fetchone()
        )

    def test_private_client_principals_and_sanitized_errors(self):
        """Metadata cannot resolve secrets and provider/private bearer text never enters exceptions."""
        vault = Vault("http://public", "a" * 64, "http://private", "b" * 64)
        self.assertFalse(hasattr(vault, "resolve"))
        with patch("exchange.urllib.request.urlopen", side_effect=OSError("synthetic-secret-marker")):
            with self.assertRaises(Refused) as caught:
                vault.read("101")
        self.assertNotIn("synthetic-secret-marker", str(caught.exception))
        with self.assertRaises(ValueError):
            Vault("http://public", "a" * 64, "http://private", "a" * 64)
