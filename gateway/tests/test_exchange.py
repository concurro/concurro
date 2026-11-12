"""The key exchange: OIDC verification, session binding, key lifecycle, the turn relay and reconciliation."""

import base64
import http.client
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "exchange"))
from exchange_fixtures import AUDIENCE, BINDING, ENGINE, EXPECTED, NOW, TURN, ExchangeFixture, segment

import exchange
from exchange import Exchange, Keys, Refused, server


class ExchangeTests(ExchangeFixture, unittest.TestCase):
    def test_unbound_session_cannot_mint_a_key(self):
        unbound = self.exchange.issue(dict(EXPECTED), ["gpt-*"], 10, 3600)
        with self.assertRaisesRegex(Refused, "no task/role binding"):
            self.exchange.key(unbound, self.signer.token(ref=EXPECTED["ref"]), engine=ENGINE, now=NOW)
        self.assertEqual(self.gateway.list(), [])

    def test_a_valid_token_mints_a_scoped_key_once(self):
        first = self.mint()
        self.assertTrue(first["key"].startswith("sk-agw-"))
        [resource] = self.gateway.list()
        self.assertEqual(resource["value"]["allowedModels"], ["gpt-*", "claude-*"])
        self.assertEqual(resource["value"]["budget"], 100000)
        self.assertEqual(self.mint()["key"], first["key"], "a retry from the bound run gets the same key")
        self.assertEqual(len(self.gateway.list()), 1)

    def test_invalid_tokens_are_refused(self):
        cases = {
            "issuer": {"iss": "https://evil.example"},
            "audience": {"aud": "other"},
            "expired": {"exp": NOW - 120},
            "too old": {"iat": NOW - 3600},
            "not yet valid": {"nbf": NOW + 600},
            "no id": {"jti": None},
            "empty id": {"jti": ""},
        }
        for name, overrides in cases.items():
            with self.subTest(name=name), self.assertRaises(Refused) as raised:
                self.mint(**overrides)
            self.assertEqual(raised.exception.status, 401)
        token = self.signer.token()
        head, body, signature = token.split(".")
        tampered = (
            head
            + "."
            + segment(json.dumps({**json.loads(base64.urlsafe_b64decode(body + "==")), "repository_id": "9"}).encode())
            + "."
            + signature
        )
        for name, value in (
            ("tampered", tampered),
            ("malformed", "a.b"),
            ("unknown key", self.signer.token(header={"alg": "RS256", "kid": "other"})),
            ("no algorithm", self.signer.token(header={"alg": "none", "kid": "test-kid"})),
        ):
            with self.subTest(name=name), self.assertRaises(Refused) as raised:
                self.exchange.key(self.session, value, engine=ENGINE, now=NOW)
            self.assertEqual(raised.exception.status, 401)
        self.assertEqual(self.gateway.list(), [])

    def test_tokens_must_match_the_session_and_its_run(self):
        for name, overrides in (
            ("repository", {"repository_id": "9"}),
            ("owner", {"repository_owner_id": "9"}),
            ("workflow", {"job_workflow_ref": "x/y/.github/workflows/z.yml@main"}),
            ("event", {"event_name": "push"}),
            ("ref", {"ref": "refs/heads/other"}),
            ("self-hosted", {"runner_environment": "self-hosted"}),
            ("re-run", {"run_attempt": "2"}),
        ):
            with self.subTest(name=name), self.assertRaises(Refused) as raised:
                self.mint(**overrides)
            self.assertEqual(raised.exception.status, 403)
        self.mint()
        with self.assertRaisesRegex(Refused, "another run"):
            self.mint(run_id="78")

    def test_a_launcher_at_any_commit_is_served_the_sessions_engine(self):
        launcher = {"job_workflow_sha": "b" * 40}
        self.assertEqual(
            self.exchange.engine(self.session, self.signer.token(**launcher), now=NOW),
            {"repository": "concurro/concurro", "engine": ENGINE},
        )
        self.assertEqual(self.mint(**launcher)["binding"]["engine"], ENGINE)

    def test_only_the_sessions_workflow_learns_its_engine(self):
        for name, overrides in (
            ("repository", {"repository_id": "9"}),
            ("workflow", {"job_workflow_ref": "x/y/.github/workflows/agent.yml@refs/heads/main"}),
            (
                "fork of the launcher",
                {"job_workflow_ref": "x/concurro/.github/workflows/gateway-proof.yml@refs/heads/main"},
            ),
            ("ref", {"ref": "refs/heads/other"}),
            ("self-hosted", {"runner_environment": "self-hosted"}),
        ):
            with self.subTest(name=name), self.assertRaises(Refused) as raised:
                self.exchange.engine(self.session, self.signer.token(**overrides), now=NOW)
            self.assertEqual(raised.exception.status, 403)

    def test_a_runner_on_another_engine_cannot_claim(self):
        for stated in (None, "b" * 40):
            with self.subTest(engine=stated), self.assertRaisesRegex(Refused, "runner engine differs"):
                self.exchange.key(self.session, self.signer.token(), engine=stated, now=NOW)
        self.assertEqual(self.gateway.list(), [])
        self.assertIn("runner engine differs", self.refusal())
        self.mint()

    def test_a_session_without_an_engine_cannot_be_claimed(self):
        legacy = {k: v for k, v in BINDING.items() if k != "engine"}
        session = self.exchange.issue(dict(EXPECTED), ["gpt-*"], 10, 3600, binding=legacy)
        token = lambda: self.signer.token(ref="refs/heads/concurro/runs/" + session)
        with self.assertRaisesRegex(Refused, "no engine binding"):
            self.exchange.engine(session, token(), now=NOW)
        with self.assertRaisesRegex(Refused, "no engine binding"):
            self.exchange.key(session, token(), engine=None, now=NOW)

    def refusal(self):
        return self.exchange.db.execute("SELECT refusal FROM sessions WHERE id = ?", (self.session,)).fetchone()[0]

    def test_a_token_is_used_once(self):
        token = self.signer.token(jti="once")
        self.exchange.key(self.session, token, engine=ENGINE, now=NOW)
        with self.assertRaisesRegex(Refused, "already used"):
            self.exchange.key(self.session, token, engine=ENGINE, now=NOW)

    def test_ending_deletes_the_key_and_the_session_stays_ended(self):
        self.mint()
        self.exchange.end(self.session, self.signer.token(), now=NOW)
        self.assertEqual(self.gateway.list(), [])
        self.assertIsNone(self.exchange.row(self.session)["key"], "a deleted key is not kept")
        with self.assertRaisesRegex(Refused, "ended"):
            self.mint()

    def test_reconciliation_deletes_expired_and_orphaned_keys(self):
        self.mint()
        live = self.exchange.issue(dict(EXPECTED), ["gpt-*"], 10, 7200, binding=BINDING)
        live_key = self.exchange.key(
            live, self.signer.token(run_id="90", ref="refs/heads/concurro/runs/" + live), engine=ENGINE, now=NOW
        )["key"]
        self.gateway.create("sk-agw-orphan", "unknown", ["*"], 1)
        # The live session's runner keeps calling; the first session's has stopped.
        self.exchange.db.execute("UPDATE sessions SET seen = ? WHERE id = ?", (NOW + 5000, live))
        removed = self.exchange.reconcile(now=NOW + 5000)
        self.assertEqual(sorted(removed), sorted([self.session, "unknown"]))
        self.assertEqual([r["value"]["key"] for r in self.gateway.list()], [live_key])
        later = {"iat": NOW + 4990, "nbf": NOW + 4990, "exp": NOW + 5300}
        with self.assertRaisesRegex(Refused, "ended"):
            self.exchange.key(self.session, self.signer.token(**later), engine=ENGINE, now=NOW + 5000)

    def test_a_partial_creation_is_adopted_or_completed(self):
        self.exchange.db.execute("UPDATE sessions SET key = 'sk-agw-pending' WHERE id = ?", (self.session,))
        self.gateway.create("sk-agw-pending", self.session, ["gpt-*"], 1)
        self.assertEqual(self.mint()["key"], "sk-agw-pending")
        self.assertEqual(len(self.gateway.list()), 1, "adopted, not duplicated")
        other = self.exchange.issue(dict(EXPECTED), ["gpt-*"], 10, 3600, binding=BINDING)
        self.exchange.db.execute("UPDATE sessions SET key = 'sk-agw-lost' WHERE id = ?", (other,))
        self.assertEqual(
            self.exchange.key(
                other, self.signer.token(run_id="91", ref="refs/heads/concurro/runs/" + other), engine=ENGINE, now=NOW
            )["key"],
            "sk-agw-lost",
        )
        self.assertIsNotNone(self.gateway.find("sk-agw-lost"))

    def test_a_failed_deletion_still_ends_the_session_and_is_retried(self):
        self.mint()
        working = self.gateway.delete
        self.gateway.delete = lambda key_id: False
        with self.assertRaisesRegex(Refused, "reconciliation will retry"):
            self.exchange.end(self.session, self.signer.token(), now=NOW)
        with self.assertRaisesRegex(Refused, "ended"):
            self.mint()
        self.assertEqual(len(self.gateway.list()), 1)
        self.assertEqual(self.exchange.reconcile(now=NOW), [])
        self.assertIsNotNone(self.exchange.row(self.session)["key"], "kept until the gateway deletes it")
        self.gateway.delete = working
        self.assertEqual(self.exchange.reconcile(now=NOW), [self.session])
        self.assertEqual(self.gateway.list(), [])
        self.assertIsNone(self.exchange.row(self.session)["key"])

    def test_operator_commands_wait_for_an_issuance_in_another_instance(self):
        """A second Exchange on the same database, as the operator CLI process opens one.

        Each instance opens the lock file itself, so their flocks are independent, as across processes.
        """
        operator = Exchange(
            self.exchange.db.execute("PRAGMA database_list").fetchone()[2],
            self.gateway,
            Keys(fetch=lambda: {"keys": [self.signer.jwk]}),
            AUDIENCE,
        )
        entered, release, events = threading.Event(), threading.Event(), []
        create = self.gateway.create

        def paused(*args):
            entered.set()
            release.wait(10)
            return create(*args)

        self.gateway.create = paused
        issuing = threading.Thread(target=lambda: events.append(("key", self.mint()["key"])))
        ending = threading.Thread(target=lambda: (operator.end(self.session), events.append(("end", None))))
        issuing.start()
        self.assertTrue(entered.wait(10))
        ending.start()
        ending.join(0.5)
        self.assertTrue(ending.is_alive(), "the operator's end waits for the issuance")
        release.set()
        issuing.join(10)
        ending.join(10)
        self.assertEqual([kind for kind, _ in events], ["key", "end"])
        self.assertEqual(self.gateway.list(), [], "the key issued first is deleted by the end")

    def test_issue_validates_its_inputs(self):
        for expected, models, budget, lease in (
            ({"unknown": "x"}, ["*"], 1, 1),
            (dict(EXPECTED), [], 1, 1),
            (dict(EXPECTED), ["*"], 0, 1),
            (dict(EXPECTED), ["*"], 1, 0),
        ):
            with self.assertRaises(ValueError):
                self.exchange.issue(expected, models, budget, lease)


class RelayTests(ExchangeFixture, unittest.TestCase):
    """Task/role staffing, run-ref authentication, sequential turns and durable recovery."""

    def setUp(self):
        super().setUp()
        self.expected = {**EXPECTED, "actor_id": "3003"}
        self.session = self.exchange.issue(self.expected, ["claude-*"], 1000, 3600, binding=BINDING)
        self.runner = self.claim(self.session)["token"]

    def claim(self, session, **overrides):
        """Present the exact session ref in a separately signed hosted-runner identity."""
        return self.exchange.key(
            session,
            self.signer.token(**{**self.expected, "ref": "refs/heads/concurro/runs/" + session, **overrides}),
            engine=ENGINE,
            now=NOW,
        )

    def submit(self, binding=None, payload=None, turn=None, pin=None):
        """Queue a model turn through the operator contract."""
        bound = binding or BINDING
        stage, provider = bound["role"].split(":")
        return self.exchange.submit(
            turn or str(uuid.uuid4()),
            self.expected,
            ["claude-*"],
            1000,
            3600,
            payload or {"request": {"provider": provider, "stage": stage}},
            pin=pin,
            now=NOW,
            binding=bound,
        )

    def test_operator_observation_keeps_bound_run_after_heartbeat_loss(self):
        """A lost implementation can be diagnosed without trusting runner-reported conclusions."""
        queued = self.submit()
        self.assertNotIn("runner", queued)
        self.exchange.next(self.session, self.runner, 1, 0, now=NOW)
        running = self.exchange.poll(queued["turn"], 0, now=NOW)
        self.assertNotIn("runner", running)
        lost = self.exchange.poll(queued["turn"], 0, now=NOW + 181)
        self.assertEqual(lost["status"], "lost")
        self.assertEqual(lost["runner"], {"session": self.session, "run_id": "77/1"})
        self.assertNotIn("key", json.dumps(lost))

    def test_task_role_generation_config_and_phase_cannot_share_jobs(self):
        own = self.submit()
        self.assertIsNone(own["dispatch"])
        for changes in (
            {"issue": 8},
            {"repository": "owner/project"},
            {"role": "review:claude"},
            {"role": "plan:codex"},
            {"generation": 2},
            {"config_sha": "d" * 40},
            {"phase": "fresh"},
        ):
            other = self.submit(binding={**BINDING, **changes})
            self.assertIsNotNone(other["dispatch"])
            record = self.exchange.row(other["dispatch"])
            self.assertEqual(json.loads(record["binding"]), {**BINDING, **changes})
            self.assertEqual(json.loads(record["expected"])["ref"], "refs/heads/concurro/runs/" + other["dispatch"])
        served = self.exchange.next(self.session, self.runner, 8, 0, now=NOW)["turns"]
        self.assertEqual([turn["id"] for turn in served], [own["turn"]])

    def test_other_session_ref_and_rerun_cannot_claim(self):
        other = self.submit(binding={**BINDING, "role": "review:claude"})["dispatch"]
        for overrides in ({"ref": "refs/heads/concurro/runs/" + self.session}, {"run_attempt": "2"}):
            with self.assertRaises(Refused):
                self.claim(other, **overrides)
        self.assertIsNone(self.exchange.row(other)["key"])
        self.assertEqual(self.claim(other)["binding"]["role"], "review:claude")
        with self.assertRaises(Refused):
            self.claim(other, run_id="78")

    def test_turns_run_sequentially_and_result_is_returned(self):
        first, second = self.submit(), self.submit()
        [turn] = self.exchange.next(self.session, self.runner, 8, 0, now=NOW)["turns"]
        self.assertEqual(turn["id"], first["turn"])
        self.assertEqual(self.exchange.next(self.session, self.runner, 8, 0, now=NOW), {"turns": []})
        self.exchange.complete(self.session, self.runner, first["turn"], {"ok": True}, now=NOW)
        self.exchange.complete(self.session, self.runner, first["turn"], {"ok": True}, now=NOW)
        self.assertEqual(
            self.exchange.poll(first["turn"], 0, now=NOW),
            {
                "turn": first["turn"],
                "status": "done",
                "result": {"ok": True},
                "created": NOW,
                "started": NOW,
                "finished": NOW,
            },
        )
        [turn] = self.exchange.next(self.session, self.runner, 8, 0, now=NOW)["turns"]
        self.assertEqual(turn["id"], second["turn"])
        with self.assertRaises(Refused):
            self.exchange.complete(self.session, self.runner, first["turn"], {"ok": False}, now=NOW)

    def test_resubmission_after_lost_reply_and_completion(self):
        identifier = str(uuid.uuid4())
        first = self.submit(turn=identifier)
        self.assertEqual(self.submit(turn=identifier), first)
        with self.assertRaises(Refused):
            self.submit(turn=identifier, binding={**BINDING, "issue": 8})
        self.exchange.next(self.session, self.runner, 1, 0, now=NOW)
        self.exchange.complete(self.session, self.runner, identifier, {"ok": True}, now=NOW)
        self.assertEqual(
            self.submit(turn=identifier),
            {
                "turn": identifier,
                "dispatch": None,
                "status": "done",
                "result": {"ok": True},
                "created": NOW,
                "started": NOW,
                "finished": NOW,
            },
        )
        late = str(uuid.uuid4())
        self.exchange.cancel(late, now=NOW)
        self.assertEqual(self.submit(turn=late)["status"], "cancelled")

    def test_operator_timing_is_stable_across_statuses_and_replays(self):
        """Timing comes from persisted exchange observations, not response or replay time."""
        first = self.submit()
        self.assertEqual((first["created"], first["started"], first["finished"]), (NOW, None, None))
        self.assertEqual(self.exchange.poll(first["turn"], 0, now=NOW)["started"], None)
        self.exchange.next(self.session, self.runner, 1, 0, now=NOW + 5)
        running = self.exchange.poll(first["turn"], 0, now=NOW + 8)
        self.assertEqual((running["status"], running["started"], running["finished"]), ("running", NOW + 5, None))
        self.exchange.complete(self.session, self.runner, first["turn"], {"ok": True}, now=NOW + 15)
        self.exchange.complete(self.session, self.runner, first["turn"], {"ok": True}, now=NOW + 20)
        done = self.exchange.poll(first["turn"], 0, now=NOW + 25)
        replay = self.submit(turn=first["turn"])
        for field in ("turn", "status", "created", "started", "finished"):
            self.assertEqual(replay[field], done[field])
        self.assertEqual((done["created"], done["started"], done["finished"]), (NOW, NOW + 5, NOW + 15))
        queued = self.submit()
        self.exchange.cancel(queued["turn"], now=NOW + 30)
        cancelled = self.exchange.poll(queued["turn"], 0, now=NOW + 35)
        self.assertEqual(
            (cancelled["status"], cancelled["started"], cancelled["finished"]), ("cancelled", None, NOW + 30)
        )
        self.exchange.cancel(queued["turn"], now=NOW + 40)
        self.assertEqual(self.exchange.poll(queued["turn"], 0, now=NOW + 45), cancelled)
        lost = self.submit()
        self.exchange.next(self.session, self.runner, 1, 0, now=NOW + 50)
        observed = self.exchange.poll(lost["turn"], 0, now=NOW + exchange.HEARTBEAT_SECONDS + 60)
        self.assertEqual(observed["status"], "lost")
        self.assertEqual(observed["started"], NOW + 50)
        self.assertIsNotNone(observed["finished"])

    def test_dead_job_loses_running_turn_and_replaces_only_queued_turn(self):
        running, waiting = self.submit(), self.submit()
        self.exchange.next(self.session, self.runner, 1, 0, now=NOW)
        later = NOW + exchange.HEARTBEAT_SECONDS + 1
        self.assertEqual(self.exchange.poll(running["turn"], 0, now=later)["status"], "lost")
        replacement = self.exchange.poll(waiting["turn"], 0, now=later)["dispatch"]
        self.assertIsNotNone(replacement)
        self.assertEqual(json.loads(self.exchange.row(replacement)["binding"]), BINDING)
        self.assertNotEqual(replacement, self.session)
        self.assertEqual(self.gateway.list(), [])

    def test_pin_cannot_cross_role_and_dies_with_its_job(self):
        other = self.submit(binding={**BINDING, "role": "review:claude"})["dispatch"]
        with self.assertRaises(Refused):
            self.submit(pin=other)
        waiting = self.submit(pin=self.session)
        self.exchange.end(self.session, runner=self.runner, now=NOW)
        self.assertEqual(self.exchange.poll(waiting["turn"], 0, now=NOW)["status"], "lost")
        with self.assertRaises(Refused):
            self.exchange.next(self.session, self.runner, 1, 0, now=NOW)

    def test_invalid_binding_and_workbench_are_refused(self):
        for changes in (
            {"issue": 0},
            {"role": "local_review:claude"},
            {"generation": 0},
            {"config_sha": "main"},
            {"engine": "main"},
        ):
            with self.assertRaises(ValueError):
                self.submit(binding={**BINDING, **changes})
        with self.assertRaises(ValueError):
            self.submit(payload={"kind": "bench", "op": "open"})
        with self.assertRaises(ValueError):
            self.submit(payload={"request": {"stage": "review", "provider": "claude"}})

    def test_a_long_queued_job_stays_live_between_engine_discovery_and_claim(self):
        """Discovery binds the run; polling and cleanup before the claim must not replace or end it."""
        queued = self.submit(binding={**BINDING, "role": "review:claude"})
        session, later = queued["dispatch"], NOW + 240
        self.assertGreater(later - NOW, exchange.HEARTBEAT_SECONDS)
        token = lambda: self.signer.token(**{**self.expected, "ref": "refs/heads/concurro/runs/" + session})
        self.exchange.engine(session, token(), now=later)
        self.assertIsNone(self.exchange.poll(queued["turn"], 0, now=later + 60)["dispatch"])
        self.exchange.reconcile(now=later + 60)
        self.assertIsNone(self.exchange.row(session)["ended"])
        self.assertTrue(self.exchange.key(session, token(), engine=ENGINE, now=later + 60)["key"])
        replaced = self.exchange.poll(queued["turn"], 0, now=later + 60 + exchange.HEARTBEAT_SECONDS + 1)
        self.assertIsNotNone(replaced.get("dispatch"), "a runner silent after discovery is still replaced")

    def test_legacy_turns_are_preserved_but_never_served(self):
        self.exchange.db.execute(
            "INSERT INTO turns (id,expected,models,budget,lease,payload,status,created) "
            "VALUES ('old',?,'[]',0,1,'{}','queued',?)",
            (json.dumps(EXPECTED, sort_keys=True), NOW),
        )
        self.assertEqual(self.exchange.next(self.session, self.runner, 1, 0, now=NOW)["turns"], [])
        self.assertIsNone(self.exchange.staffed("old", NOW))
        self.assertIsNotNone(self.exchange.db.execute("SELECT id FROM turns WHERE id='old'").fetchone())


class HttpTests(ExchangeFixture, unittest.TestCase):
    """The runner-facing HTTP surface."""

    def setUp(self):
        super().setUp()
        clock = patch.object(exchange.time, "time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.server = server(self.exchange, "127.0.0.1:0", "operator-secret")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def post(self, path, token=None, body=None, method="POST"):
        request = urllib.request.Request(
            self.base + path,
            method=method,
            data=None if method == "GET" else json.dumps(body or {}).encode(),
            headers={"Authorization": "Bearer " + token} if token else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_key_and_end_over_http(self):
        status, body = self.post(f"/exchange/sessions/{self.session}/engine", self.signer.token())
        self.assertEqual((status, body), (200, {"repository": "concurro/concurro", "engine": ENGINE}))
        self.assertEqual(self.post(f"/exchange/sessions/{self.session}/engine")[0], 401)
        self.assertEqual(self.post(f"/exchange/sessions/{self.session}/key", self.signer.token())[0], 403)
        status, body = self.post(f"/exchange/sessions/{self.session}/key", self.signer.token(), {"engine": ENGINE})
        self.assertEqual(status, 200)
        self.assertTrue(body["key"].startswith("sk-agw-"))
        self.assertEqual(self.post(f"/exchange/sessions/{self.session}/key")[0], 401)
        self.assertEqual(self.post("/exchange/other", self.signer.token())[0], 404)
        self.assertEqual(self.post(f"/exchange/sessions/{self.session}/end", self.signer.token())[0], 200)
        self.assertEqual(self.gateway.list(), [])

    def test_a_turn_round_trip_over_http(self):
        submit = {
            "turn": str(uuid.uuid4()),
            "expected": {**EXPECTED, "actor_id": "3003"},
            "models": ["claude-*"],
            "budget": 1000,
            "lease": 3600,
            "payload": TURN,
            "binding": BINDING,
        }
        self.assertEqual(self.post("/exchange/operator/turns", None, submit)[0], 401)
        self.assertEqual(self.post("/exchange/operator/turns", "wrong", submit)[0], 401)
        status, queued = self.post("/exchange/operator/turns", "operator-secret", submit)
        self.assertEqual(status, 200)
        self.session = queued["dispatch"]
        runner = self.post(
            f"/exchange/sessions/{self.session}/key",
            self.signer.token(actor_id="3003", ref="refs/heads/concurro/runs/" + self.session),
            {"engine": ENGINE},
        )[1]["token"]
        status, served = self.post(f"/exchange/sessions/{self.session}/next", runner, {"capacity": 1, "wait": 0})
        self.assertEqual((status, [t["id"] for t in served["turns"]]), (200, [queued["turn"]]))
        status, _ = self.post(
            f"/exchange/sessions/{self.session}/turns/{queued['turn']}", runner, {"result": {"exit": 0}}
        )
        self.assertEqual(status, 200)
        status, polled = self.post(f"/exchange/operator/turns/{queued['turn']}?wait=0", "operator-secret", method="GET")
        self.assertEqual(
            (status, polled),
            (
                200,
                {
                    "turn": queued["turn"],
                    "status": "done",
                    "result": {"exit": 0},
                    "created": NOW,
                    "started": NOW,
                    "finished": NOW,
                },
            ),
        )
        self.assertEqual(self.post("/exchange/operator/keys", "operator-secret", {})[0], 404)
        self.assertEqual(self.post(f"/exchange/sessions/{self.session}/end", runner)[0], 200)
        self.assertEqual(self.gateway.list(), [])

    def test_the_operator_api_is_off_without_a_token(self):
        closed = server(self.exchange, "127.0.0.1:0")
        threading.Thread(target=closed.serve_forever, daemon=True).start()
        self.addCleanup(closed.shutdown)
        self.base = f"http://127.0.0.1:{closed.server_address[1]}"
        self.assertEqual(self.post("/exchange/operator/keys", "", {})[0], 401)

    def declare(self, path, token, size):
        """Declare a large body without sending it: only a refusal before reading can answer in time."""
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        self.addCleanup(connection.close)
        headers = {"Content-Length": str(size), **({"Authorization": "Bearer " + token} if token else {})}
        connection.request("POST", path, headers=headers)
        return connection.getresponse().status

    def test_bodies_are_read_only_for_authenticated_callers_and_within_bounds(self):
        large = 40 * 1024 * 1024
        self.assertEqual(self.declare("/exchange/operator/turns", None, large), 401)
        self.assertEqual(self.declare("/exchange/operator/turns", "wrong", large), 401)
        self.assertEqual(self.declare(f"/exchange/sessions/{self.session}/turns/{uuid.uuid4()}", "0" * 64, large), 401)
        self.assertEqual(self.declare(f"/exchange/sessions/{self.session}/key", self.signer.token(), large), 413)
        self.assertEqual(self.declare("/exchange/operator/keys", "operator-secret", large), 413)
        self.assertEqual(self.declare("/exchange/operator/turns", "operator-secret", exchange.MAX_BODY + 1), 413)

    def test_bodies_held_at_once_share_a_budget(self):
        budget = exchange.Budget(100)
        with budget.reserve(60):
            with self.assertRaises(exchange.Refused) as raised, budget.reserve(60, wait=0):
                pass
            self.assertEqual(raised.exception.status, 503)
        with budget.reserve(100):
            pass


class BusyTests(unittest.TestCase):
    def test_a_busy_exchange_answers_503(self):
        with patch.object(exchange, "MAX_REQUESTS", 0):
            busy = server(None, "127.0.0.1:0")
        threading.Thread(target=busy.serve_forever, daemon=True).start()
        self.addCleanup(busy.shutdown)
        request = urllib.request.Request(
            f"http://127.0.0.1:{busy.server_address[1]}/exchange/x", method="POST", data=b""
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(raised.exception.code, 503)


if __name__ == "__main__":
    unittest.main()
