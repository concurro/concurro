"""Request-start identity, exact secret resolution and durable sanitized observations."""

import json
import sqlite3
import sys
import unittest
from contextlib import closing, contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "engine"))

# isort: split
from exchange_fixtures import AUDIENCE, NOW

import review_batch
from agents import RunnerExecutor, TurnHandle
from exchange import Exchange, Refused, Vault
from inference import Observation, category
from policy import ProviderFailure, digest
from test_exchange_funding import FundedGateway, MetadataVault, funded_turn, publish
from test_exchange_structure import EXPECTED_RUNNER, StateFixture


class SecretVault(MetadataVault):
    """Synthetic exact-version resolver; production private-principal HTTP is tested separately."""

    def __init__(self):
        """Retain resolution calls and one controllable trusted denial."""
        super().__init__()
        self.calls = []
        self.denied = None

    def resolve(self, binding):
        """Return only this fixture's exact recorded owner/version identity."""
        self.calls.append(deepcopy(binding))
        if self.denied:
            raise Refused(self.denied, "synthetic private detail")
        return {
            "user_id": binding["sponsor"],
            "record_id": binding["credential"],
            "version": binding["credential_version"],
            "route": binding["route"],
            "secret": "SYNTHETIC_SECRET_SENTINEL",
            "account": None,
        }


class InferenceTests(StateFixture, unittest.TestCase):
    """Run real Exchange/session/turn transitions behind the private inference boundary."""

    def setUp(self):
        """Create a real admitted, claimed running turn with synthetic vault material."""
        super().setUp()
        self.exchange.gateway = FundedGateway()
        self.exchange.vault = SecretVault()
        self.turn, self.binding, self.payload = funded_turn()
        reply = self.exchange.submit(
            self.turn, EXPECTED_RUNNER, ["synthetic-model"], 1000, 3600, self.payload, binding=self.binding
        )
        self.session = reply["dispatch"]
        self.grant = self.exchange.key(self.session, self.token(self.session), engine=self.binding["engine"])
        self.exchange.next(self.session, self.grant["token"], 1, 0)
        self.adapter = self.exchange.inference

    def start(self):
        """Use the actual virtual-key authentication path, with no client payer/turn input."""
        return self.adapter.start(self.grant["key"], "POST", "/v1/responses")["request"]

    def test_start_authenticates_key_and_probes_are_non_turn(self):
        """An unknown key cannot create attribution; activation cannot resolve or spend upstream."""
        self.assertEqual(self.adapter.start("forged", "POST", "/v1/responses"), {"request": "", "funding": "org"})
        self.assertEqual(
            self.adapter.start(self.grant["key"], "GET", "/v1/models"), {"request": "", "funding": "probe"}
        )
        self.assertEqual(self.exchange.db.execute("SELECT count(*) FROM inference_requests").fetchone()[0], 0)
        self.assertEqual(self.exchange.vault.calls, [])
        self.exchange.db.execute("UPDATE sessions SET ended=? WHERE id=?", (NOW, self.session))
        with self.assertRaises(Refused):
            self.start()

    def test_start_reads_small_model_column_and_task_pending_uses_index(self):
        """A large retained transcript is never decoded while authenticating an inference request."""
        payload = {**self.payload, "archive": "x" * (3 * 1024 * 1024)}
        self.exchange.db.execute("UPDATE turns SET payload=? WHERE id=?", (json.dumps(payload), self.turn))
        queries = []
        self.exchange.db.set_trace_callback(queries.append)
        try:
            identifier = self.start()
        finally:
            self.exchange.db.set_trace_callback(None)
        self.assertEqual(self.adapter.snapshot(identifier)["model"], "synthetic-model")
        self.assertFalse(any("payload" in query.lower() for query in queries))
        plan = self.exchange.db.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM inference_requests WHERE repository=? AND issue=?",
            (self.binding["repository"], self.binding["issue"]),
        ).fetchall()
        self.assertTrue(any("inference_request_task" in row[-1] for row in plan))

    def test_turn_cancel_and_dead_session_reconcile_interrupt_only_matching_sockets(self):
        """Existing turn cancellation and heartbeat retirement stop private streams independently."""
        from unittest.mock import Mock

        for ending in ("cancel", "reconcile"):
            with self.subTest(ending=ending):
                identifier = self.start()
                snapshot = self.adapter.snapshot(identifier)
                stream = Mock()
                self.adapter.attach(snapshot, stream)
                other = Mock()
                self.adapter.connections["other"] = ("other-session", "other-turn", other)
                if ending == "cancel":
                    self.exchange.cancel(self.turn)
                    self.exchange.db.execute("UPDATE turns SET status='running' WHERE id=?", (self.turn,))
                else:
                    self.exchange.db.execute("UPDATE sessions SET seen=?,run_id=77 WHERE id=?", (1, self.session))
                    self.exchange.reconcile()
                stream.shutdown.assert_called_once()
                stream.close.assert_called_once()
                other.close.assert_not_called()
                self.adapter.connections.pop("other")

    def test_active_turn_snapshot_is_immutable_across_completion_and_restart(self):
        """Delayed evidence belongs to the captured turn after its session advances or restarts."""
        identifier = self.start()
        self.exchange.complete(self.session, self.grant["token"], self.turn, {"outcome": "ok"})
        self.adapter.finish(identifier, "provider", {"category": "unknown", "usage": None, "completed": False})
        reopened = Exchange(
            self.exchange.lockfile.removesuffix(".lock"),
            self.exchange.gateway,
            self.exchange.keys,
            AUDIENCE,
            vault=self.exchange.vault,
        )
        self.addCleanup(reopened.db.close)
        events = reopened.inference.events(self.binding["repository"], self.binding["issue"])
        self.assertEqual(events[0]["turn"], self.turn)
        self.assertEqual(events[0]["binding"], self.binding)
        self.assertEqual(reopened.inference.events("other/project", 7), [])
        with self.assertRaises(Refused):
            self.start()

    def test_resolve_every_request_exact_version_and_recheck_revoke_race(self):
        """No secret cache, payer re-selection, repeated nonce or retirement race can resolve anew."""
        first = self.start()
        second = self.start()
        for identifier in (first, second):
            value = self.adapter.resolve(identifier, self.session)
            self.assertEqual(value["token"], "SYNTHETIC_SECRET_SENTINEL")
            self.adapter.claim(identifier, self.binding["route"])
            with self.assertRaises(Refused):
                self.adapter.claim(identifier, self.binding["route"])
            with self.assertRaises(Refused):
                self.adapter.resolve(identifier, self.session)
        self.assertEqual(len(self.exchange.vault.calls), 2)
        self.assertTrue(all(call == self.binding for call in self.exchange.vault.calls))
        identifier = self.start()
        original = self.exchange.vault.resolve

        def retire(binding):
            """End authority during the resolver round trip, before any native secret response."""
            value = original(binding)
            self.exchange.end(self.session)
            return value

        with patch.object(self.exchange.vault, "resolve", side_effect=retire):
            with self.assertRaises(Refused):
                self.adapter.resolve(identifier, self.session)

    def test_trusted_expiry_denial_has_fixed_auth_attribution_and_outage_stays_unknown(self):
        """Only exact-version gone/expired responses classify auth, never network failures."""
        for status in (503, 410):
            identifier = self.start()
            self.exchange.vault.denied = status
            with self.assertRaises(Refused):
                self.adapter.resolve(identifier, self.session)
        events = self.adapter.events(self.binding["repository"], self.binding["issue"])
        self.assertEqual([event["category"] for event in events], ["unknown", "auth"])
        self.assertNotIn("synthetic private detail", json.dumps(events))
        self.assertNotIn("SYNTHETIC_SECRET_SENTINEL", json.dumps(events))

    def test_native_budget_join_requires_exact_nonce_and_native_only_flag(self):
        """A spoofed429 or absent request cannot become a budget pause; replay is idempotent."""
        identifier = self.start()
        path = Path(self.database).with_name("native.db")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE request_logs(http_status INTEGER,attributes_json TEXT)")
            for value in (
                {"concurro_request": "absent-false", "concurro_native_budget": False},
                {"concurro_request": "forged", "concurro_native_budget": True},
                {"concurro_request": identifier, "concurro_native_budget": True},
            ):
                db.execute("INSERT INTO request_logs VALUES(429,?)", (json.dumps(value),))
        self.adapter.native_database = path
        self.adapter.import_native()
        self.adapter.import_native()
        events = self.adapter.events(self.binding["repository"], self.binding["issue"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["request"], identifier)
        self.assertEqual(events[0]["turn"], self.turn)
        self.assertEqual(events[0]["category"], "budget_denied")
        self.assertIsNone(events[0]["usage"])
        self.assertEqual(self.exchange.poll(self.turn, 0)["provider_failure"]["request"], identifier)

    def test_delayed_native_write_survives_restart_without_generic_retry(self):
        """A generic CLI failure waits under503/backoff until exact native evidence survives a restart."""
        identifier = self.start()
        self.exchange.complete(self.session, self.grant["token"], self.turn, {"outcome": "failed"})
        self.assertTrue(self.adapter.pending(turn=self.turn))
        with self.assertRaises(Refused) as pending:
            self.exchange.poll(self.turn, 0)
        self.assertEqual(pending.exception.status, 503)
        with self.assertRaises(Refused):
            self.exchange.submit(
                self.turn, EXPECTED_RUNNER, ["synthetic-model"], 1000, 3600, self.payload, binding=self.binding
            )
        reopened = Exchange(
            self.exchange.lockfile.removesuffix(".lock"),
            self.exchange.gateway,
            self.exchange.keys,
            AUDIENCE,
            vault=self.exchange.vault,
        )
        self.addCleanup(reopened.db.close)
        path = Path(self.database).with_name("delayed-native.db")
        reopened.inference.native_database = path
        with self.assertRaises(Refused):
            reopened.poll(self.turn, 0)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE request_logs(attributes_json TEXT)")
            db.execute(
                "INSERT INTO request_logs VALUES(?)",
                (json.dumps({"http.status": 429, "concurro_request": identifier, "concurro_native_budget": True}),),
            )
        reply = reopened.poll(self.turn, 0)
        self.assertEqual(reply["provider_failure"]["category"], "budget_denied")
        self.assertFalse(reopened.inference.pending(turn=self.turn))
        self.assertEqual(self.exchange.db.execute("SELECT count(*) FROM turns").fetchone()[0], 1)

    def test_completed_native_unknown_retains_existing_generic_result(self):
        """Known native completion with no trusted category is distinct from missing evidence."""
        identifier = self.start()
        self.exchange.complete(self.session, self.grant["token"], self.turn, {"outcome": "failed"})
        self.adapter.finish(identifier, "native", {"status": 400, "category": None, "completed": False})
        reply = self.exchange.poll(self.turn, 0)
        self.assertNotIn("provider_failure", reply)
        self.assertEqual(reply["result"], {"outcome": "failed"})
        self.assertFalse(self.adapter.pending(turn=self.turn))

    def drain_peer(self, provider="anthropic", actor=None, epoch=1, budget=False):
        """Exercise real retirement/failure receipts and runner draining with one independently admitted peer."""
        turn, binding, payload = funded_turn(provider, actor=actor, epoch=epoch)
        if provider == "openai":
            if actor == "303":
                self.exchange.vault.records.append(
                    {**self.exchange.vault.records[0], "user_id": "303", "record_id": "c" * 64}
                )
            publish(self.exchange, binding, payload["request"]["admission"]["grant"])
        reply = self.exchange.submit(turn, EXPECTED_RUNNER, ["synthetic-model"], 1000, 3600, payload, binding=binding)
        session = reply["dispatch"]
        grant = self.exchange.key(session, self.token(session), engine=binding["engine"])
        self.exchange.next(session, grant["token"], 1, 0)
        operator = SimpleNamespace(poll=self.exchange.poll, cancel=self.exchange.cancel, end=self.exchange.end)
        executor = RunnerExecutor(
            operator, "http://fixture", EXPECTED_RUNNER, ["*"], 1000, lambda *_: None, clock=lambda: NOW
        )
        cases = {"failed": (self.turn, self.session, self.payload), "peer": (turn, session, payload)}
        handles = {
            key: TurnHandle(identifier, {**body, "timeout": None}, None, digest([key]), {staff})
            for key, (identifier, staff, body) in cases.items()
        }
        completed = {}
        applied = []
        polls = {key: 0 for key in cases}

        def observe(handle):
            """Complete only the unaffected fake provider after two genuine Exchange polls."""
            key = next(key for key, value in handles.items() if value is handle)
            polls[key] += 1
            if key == "peer" and polls[key] == 2:
                self.exchange.complete(session, grant["token"], turn, {"outcome": "ok", "response": {"retained": True}})
            return executor.observe(handle)

        def complete(key, _turn, result, _timing):
            """Retain a peer result before verdict application, exactly as the coordinator protocol requires."""
            completed[key] = result

        def failed(_key, error):
            """Preserve the actual typed provider authority outcome."""
            raise error

        transport = SimpleNamespace(
            check_owner=lambda: None,
            submit=lambda turn: handles[turn["key"]],
            observe=observe,
            cancel=executor.cancel,
            idle=lambda: None,
        )
        adapter = SimpleNamespace(
            transport=transport,
            open=lambda: list(cases),
            retained=lambda key: key in completed and key not in applied,
            reserve=lambda keys, initial: {key: {**cases[key][2], "key": key} for key in keys},
            status=lambda *_: None,
            valid=lambda: True,
            complete=complete,
            failed=failed,
            apply=applied.append,
            finished=lambda: len(applied) == 2,
            needs_attempt=lambda key: key not in completed,
            close=lambda: None,
        )
        if budget:
            identifier = self.start()
            self.adapter.finish(identifier, "native", {"category": "budget_denied", "completed": False, "usage": None})
        else:
            publish(
                self.exchange,
                self.binding,
                {"epoch": 2, "status": "withdrawn", "sponsor_id": None, "grant_id": "f" * 64},
                reason="withdraw",
                actor="101",
                cutoff=1,
            )
        with self.assertRaises(ProviderFailure) as failure:
            review_batch.run(adapter)
        self.assertEqual(failure.exception.provider, "openai")
        self.assertEqual(failure.exception.category, "budget_denied" if budget else "authority_ended")
        self.assertEqual(applied, ["peer"])
        self.assertIsNone(self.exchange.row(session)["ended"])
        self.assertIsNotNone(self.exchange.row(self.session)["ended"])
        self.assertEqual(self.exchange.db.execute("SELECT count(*) FROM turns").fetchone()[0], 2)

    def test_actual_retirement_runner_outcome_drains_other_provider(self):
        """An actual OpenAI withdrawal does not cancel an independently funded Anthropic review."""
        self.drain_peer()

    def test_same_provider_new_owner_epoch_keeps_admitted_work(self):
        """A former owner's failed token budget cannot cancel a healthy transferred owner's attempt."""
        self.drain_peer("openai", "303", 2, True)

    def test_same_provider_session_budget_isolation(self):
        """Independent admitted session budgets remain isolated even for the same provider/owner/version."""
        self.drain_peer("openai", "101", 2, True)

    def test_running_evidence_does_not_block_other_member_admission(self):
        """Healthy in-flight requests are distinct from terminal unresolved native completion."""
        self.start()
        self.assertFalse(self.exchange.observations(self.binding["repository"], self.binding["issue"])["pending"])
        self.exchange.complete(self.session, self.grant["token"], self.turn, {"outcome": "failed"})
        self.assertTrue(self.exchange.observations(self.binding["repository"], self.binding["issue"])["pending"])

    def test_successful_saved_work_precedes_late_failure_but_cancelled_work_never_revives(self):
        """Already accepted success remains recoverable; cancelled/authority-ended attempts stay rejected."""
        identifier = self.start()
        self.exchange.complete(
            self.session, self.grant["token"], self.turn, {"outcome": "ok", "response": {"saved": True}}
        )
        self.adapter.finish(identifier, "native", {"category": "budget_denied", "usage": None, "completed": False})
        reply = self.exchange.poll(self.turn, 0)
        self.assertEqual(reply["result"]["response"], {"saved": True})
        self.assertNotIn("provider_failure", reply)
        self.assertTrue(self.adapter.failure(self.turn))
        executor = RunnerExecutor(SimpleNamespace(), "http://fixture", EXPECTED_RUNNER, ["*"], 1000, lambda *_: None)
        handle = TurnHandle(self.turn, {**self.payload, "timeout": None}, None, digest(["saved"]), {self.session})
        executor.receive(handle, reply)
        self.assertTrue(handle.result["response"]["saved"])
        other, binding, payload = funded_turn("anthropic")
        pending = self.exchange.submit(
            other, EXPECTED_RUNNER, ["synthetic-model"], 1000, 3600, payload, binding=binding
        )
        staff = pending["dispatch"]
        grant = self.exchange.key(staff, self.token(staff), engine=binding["engine"])
        self.exchange.next(staff, grant["token"], 1, 0)
        self.exchange.cancel(other)
        with self.assertRaises(Refused):
            self.exchange.complete(staff, grant["token"], other, {"outcome": "ok"})
        self.assertNotIn("result", self.exchange.poll(other, 0))

    def test_resolve_claim_once_and_turn_completion_races(self):
        """Concurrent resolver calls consume one nonce, and completion blocks later secret handoff."""
        identifier = self.start()
        original = self.exchange.vault.resolve

        def race(binding):
            """Try the same nonce while the first exact version lookup is in flight."""
            with self.assertRaises(Refused):
                self.adapter.resolve(identifier, self.session)
            return original(binding)

        with patch.object(self.exchange.vault, "resolve", side_effect=race):
            self.adapter.resolve(identifier, self.session)
        self.assertEqual(len(self.exchange.vault.calls), 1)
        identifier = self.start()

        def complete(binding):
            """Complete after resolver I/O started but before its second authority check."""
            value = original(binding)
            self.exchange.complete(self.session, self.grant["token"], self.turn, {"outcome": "ok"})
            return value

        with patch.object(self.exchange.vault, "resolve", side_effect=complete):
            with self.assertRaises(Refused):
                self.adapter.resolve(identifier, self.session)
        with self.assertRaises(Refused):
            self.adapter.claim(identifier, self.binding["route"])

    def test_native_import_batches_locks_and_persists_append_cursor(self):
        """Ten thousand retained rows cost constant lifecycle locks and no repeated row processing."""
        identifier = self.start()
        path = Path(self.database).with_name("many-native.db")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE request_logs(attributes_json TEXT)")
            db.executemany("INSERT INTO request_logs VALUES(?)", [(json.dumps({"concurro_request": ""}),)] * 9999)
            db.execute(
                "INSERT INTO request_logs VALUES(?)",
                (json.dumps({"concurro_request": identifier, "concurro_native_budget": True}),),
            )
        self.adapter.native_database = path
        calls = []
        original = self.exchange.locked

        @contextmanager
        def locked():
            """Count actual lifecycle locks without changing their cross-process exclusion."""
            calls.append(1)
            with original():
                yield

        with patch.object(self.exchange, "locked", locked):
            self.adapter.import_native()
        self.assertEqual(len(calls), 2)
        calls.clear()
        with patch.object(self.exchange, "locked", locked):
            self.adapter.import_native()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.exchange.db.execute("SELECT last_row FROM native_cursor").fetchone()[0], 10000)
        self.assertEqual(self.exchange.db.execute("SELECT count(*) FROM inference_observations").fetchone()[0], 1)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("INSERT INTO request_logs VALUES(NULL)")
            db.execute("INSERT INTO request_logs VALUES('[]')")
        self.adapter.import_native()
        self.assertEqual(self.exchange.db.execute("SELECT last_row FROM native_cursor").fetchone()[0], 10002)

    def test_private_resolver_http_uses_only_gateway_principal_and_fixed_errors(self):
        """The metadata token never authorizes material; HTTP failures cannot echo secrets."""
        vault = Vault("http://metadata", "a" * 64, "http://private", "b" * 64)

        def inspect(request, timeout):
            """Validate the exact private request without touching any real endpoint."""
            self.assertEqual(request.full_url, "http://private/resolve")
            self.assertEqual(request.headers["Authorization"], "Bearer " + "b" * 64)
            self.assertEqual(json.loads(request.data)["record_id"], self.binding["credential"])
            raise OSError("SYNTHETIC_SECRET_SENTINEL")

        with patch("exchange.urllib.request.urlopen", side_effect=inspect):
            with self.assertRaises(Refused) as error:
                vault.resolve(self.binding)
        self.assertNotIn("SYNTHETIC_SECRET_SENTINEL", str(error.exception))


class ParserTests(unittest.TestCase):
    """Production taxonomy never inherits fixture-only subscription assumptions."""

    def test_route_specific_codes_and_ambiguous_rate_limit(self):
        """429 and spending-limit messages are not quota/rate evidence on either subscription route."""
        error = {"error": {"code": "insufficient_quota", "type": "rate_limit_error", "message": "secret"}}
        self.assertEqual(category(error, "openai-api-key"), "quota_exhausted")
        for code in (
            "credit_balance_exhausted",
            "organization_spend_limit_exceeded",
            "project_spend_limit_exceeded",
            "organization_usage_limit_exceeded",
        ):
            self.assertEqual(category({"error": {"code": code}}, "openai-api-key"), "quota_exhausted")
        for route in ("anthropic-api-key", "claude-subscription", "chatgpt-subscription"):
            self.assertIsNone(category(error, route))
        self.assertEqual(category({"error": {"type": "authentication_error"}}, "anthropic-api-key"), "auth")

    def test_non_null_structural_errors_are_redacted_unknown(self):
        """Scalar/list error shapes are failures even without a documented code; null remains success."""
        for error in ("synthetic-secret", ["synthetic-secret"], False, 0):
            parser = Observation("chatgpt-subscription", 200)
            self.assertTrue(parser.accept({"error": error}))
            self.assertEqual(parser.finish()["category"], "unknown")
            self.assertNotIn("synthetic-secret", json.dumps(parser.result))
        parser = Observation("openai-api-key", 200)
        self.assertFalse(
            parser.accept(
                {
                    "object": "response",
                    "status": "completed",
                    "error": None,
                    "usage": {"input_tokens": 2, "output_tokens": 3},
                }
            )
        )
        self.assertEqual(parser.finish()["usage"], {"input_tokens": 2, "output_tokens": 3})

    def test_stream_200_error_partial_disconnect_usage_and_unknown(self):
        """Only complete structured usage counts; ambiguous tails never fabricate zero."""
        parser = Observation("openai-api-key", 200, "60")
        self.assertTrue(parser.accept({"type": "error", "error": {"code": "rate_limit_exceeded", "message": "secret"}}))
        self.assertEqual(parser.finish()["category"], "rate_limited")
        self.assertEqual(parser.result["retry_after"], 60)
        self.assertNotIn("secret", json.dumps(parser.result))
        parser = Observation("claude-subscription", 200)
        parser.accept({"type": "message_start", "message": {"usage": {"input_tokens": 2}}})
        self.assertIsNone(parser.finish()["usage"])
        parser = Observation("anthropic-api-key", 200)
        parser.accept({"type": "message_start", "message": {"usage": {"input_tokens": 2}}})
        parser.accept({"type": "message_delta", "usage": {"output_tokens": 3}})
        parser.accept({"type": "message_stop"})
        self.assertEqual(parser.finish()["usage"], {"input_tokens": 2, "output_tokens": 3})
