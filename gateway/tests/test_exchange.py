"""The key exchange: OIDC verification, session binding, key lifecycle, the turn relay and reconciliation."""

import base64
import http.client
import json
import os
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "exchange"))
import exchange
from exchange import ISSUER, Exchange, Keys, Refused, server

AUDIENCE = "concurro-gateway"
# Sessions take their deadlines from the real clock; tokens are dated relative to it.
NOW = int(time.time())
EXPECTED = {"repository_id": "1001", "repository_owner_id": "2002",
            "job_workflow_ref": "concurro/concurro/.github/workflows/gateway-proof.yml@refs/heads/main",
            "job_workflow_sha": "a" * 40, "event_name": "workflow_dispatch", "ref": "refs/heads/main"}


def segment(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


class Signer:
    """A local RSA key standing in for GitHub's OIDC signing key."""

    def __init__(self, directory):
        self.key = Path(directory) / "key.pem"
        subprocess.run(["openssl", "genrsa", "-out", str(self.key), "2048"], check=True, capture_output=True)
        modulus = subprocess.run(["openssl", "rsa", "-in", str(self.key), "-noout", "-modulus"], check=True,
                                 capture_output=True, text=True).stdout.strip().split("=")[1]
        self.jwk = {"kty": "RSA", "kid": "test-kid", "n": segment(bytes.fromhex(modulus)), "e": segment((65537).to_bytes(3, "big"))}

    def token(self, header=None, **overrides):
        payload = {"iss": ISSUER, "aud": AUDIENCE, "exp": NOW + 300, "nbf": NOW - 5, "iat": NOW - 5,
                   "jti": overrides.pop("jti") if "jti" in overrides else os.urandom(8).hex(),
                   "runner_environment": "github-hosted", "run_id": "77", "run_attempt": "1", **EXPECTED}
        payload.update(overrides)
        payload = {k: v for k, v in payload.items() if v is not None}
        head = segment(json.dumps(header or {"alg": "RS256", "kid": "test-kid", "typ": "JWT"}).encode())
        body = segment(json.dumps(payload).encode())
        signature = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(self.key)],
                                   input=(head + "." + body).encode(), check=True, capture_output=True).stdout
        return head + "." + body + "." + segment(signature)


class FakeGateway:
    """agentgateway's admin API as the exchange sees it."""

    def __init__(self):
        self.resources, self.next = {}, 0

    def find(self, key):
        return next((i for i, v in self.resources.items() if v["key"] == key), None)

    def create(self, key, name, models, budget):
        self.next += 1
        self.resources[str(self.next)] = {"key": key, "metadata": {"name": name, "concurro.session": name},
                                          "allowedModels": models, "budget": budget}
        return str(self.next)

    def activated(self, key):
        assert self.find(key)

    def list(self):
        return [{"id": i, "value": v} for i, v in self.resources.items()]

    def delete(self, key_id):
        self.resources.pop(key_id, None)
        return True


class ExchangeTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.signer = Signer(directory.name)
        self.gateway = FakeGateway()
        self.exchange = Exchange(str(Path(directory.name) / "exchange.db"), self.gateway,
                                 Keys(fetch=lambda: {"keys": [self.signer.jwk]}), AUDIENCE)
        self.session = self.exchange.issue(dict(EXPECTED), ["gpt-*", "claude-*"], 100000, 3600)

    def mint(self, **overrides):
        return self.exchange.key(self.session, self.signer.token(**overrides), now=NOW)

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
            "issuer": {"iss": "https://evil.example"}, "audience": {"aud": "other"},
            "expired": {"exp": NOW - 120}, "too old": {"iat": NOW - 3600}, "not yet valid": {"nbf": NOW + 600},
            "no id": {"jti": None}, "empty id": {"jti": ""},
        }
        for name, overrides in cases.items():
            with self.subTest(name=name), self.assertRaises(Refused) as raised:
                self.mint(**overrides)
            self.assertEqual(raised.exception.status, 401)
        token = self.signer.token()
        head, body, signature = token.split(".")
        tampered = head + "." + segment(json.dumps({**json.loads(base64.urlsafe_b64decode(body + "==")),
                                                    "repository_id": "9"}).encode()) + "." + signature
        for name, value in (("tampered", tampered), ("malformed", "a.b"),
                            ("unknown key", self.signer.token(header={"alg": "RS256", "kid": "other"})),
                            ("no algorithm", self.signer.token(header={"alg": "none", "kid": "test-kid"}))):
            with self.subTest(name=name), self.assertRaises(Refused) as raised:
                self.exchange.key(self.session, value, now=NOW)
            self.assertEqual(raised.exception.status, 401)
        self.assertEqual(self.gateway.list(), [])

    def test_tokens_must_match_the_session_and_its_run(self):
        for name, overrides in (("repository", {"repository_id": "9"}), ("owner", {"repository_owner_id": "9"}),
                                ("workflow", {"job_workflow_ref": "x/y/.github/workflows/z.yml@main"}),
                                ("commit", {"job_workflow_sha": "b" * 40}), ("event", {"event_name": "push"}),
                                ("ref", {"ref": "refs/heads/other"}), ("self-hosted", {"runner_environment": "self-hosted"}),
                                ("re-run", {"run_attempt": "2"})):
            with self.subTest(name=name), self.assertRaises(Refused) as raised:
                self.mint(**overrides)
            self.assertEqual(raised.exception.status, 403)
        self.mint()
        with self.assertRaisesRegex(Refused, "another run"):
            self.mint(run_id="78")

    def test_a_token_is_used_once(self):
        token = self.signer.token(jti="once")
        self.exchange.key(self.session, token, now=NOW)
        with self.assertRaisesRegex(Refused, "already used"):
            self.exchange.key(self.session, token, now=NOW)

    def test_ending_deletes_the_key_and_the_session_stays_ended(self):
        self.mint()
        self.exchange.end(self.session, self.signer.token(), now=NOW)
        self.assertEqual(self.gateway.list(), [])
        self.assertIsNone(self.exchange.row(self.session)["key"], "a deleted key is not kept")
        with self.assertRaisesRegex(Refused, "ended"):
            self.mint()

    def test_reconciliation_deletes_expired_and_orphaned_keys(self):
        self.mint()
        live = self.exchange.issue(dict(EXPECTED), ["gpt-*"], 10, 7200)
        live_key = self.exchange.key(live, self.signer.token(run_id="90"), now=NOW)["key"]
        self.gateway.create("sk-agw-orphan", "unknown", ["*"], 1)
        # The live session's runner keeps calling; the first session's has stopped.
        self.exchange.db.execute("UPDATE sessions SET seen = ? WHERE id = ?", (NOW + 5000, live))
        removed = self.exchange.reconcile(now=NOW + 5000)
        self.assertEqual(sorted(removed), sorted([self.session, "unknown"]))
        self.assertEqual([r["value"]["key"] for r in self.gateway.list()], [live_key])
        later = {"iat": NOW + 4990, "nbf": NOW + 4990, "exp": NOW + 5300}
        with self.assertRaisesRegex(Refused, "ended"):
            self.exchange.key(self.session, self.signer.token(**later), now=NOW + 5000)

    def test_a_partial_creation_is_adopted_or_completed(self):
        self.exchange.db.execute("UPDATE sessions SET key = 'sk-agw-pending' WHERE id = ?", (self.session,))
        self.gateway.create("sk-agw-pending", self.session, ["gpt-*"], 1)
        self.assertEqual(self.mint()["key"], "sk-agw-pending")
        self.assertEqual(len(self.gateway.list()), 1, "adopted, not duplicated")
        other = self.exchange.issue(dict(EXPECTED), ["gpt-*"], 10, 3600)
        self.exchange.db.execute("UPDATE sessions SET key = 'sk-agw-lost' WHERE id = ?", (other,))
        self.assertEqual(self.exchange.key(other, self.signer.token(run_id="91"), now=NOW)["key"], "sk-agw-lost")
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
        operator = Exchange(self.exchange.db.execute("PRAGMA database_list").fetchone()[2], self.gateway,
                            Keys(fetch=lambda: {"keys": [self.signer.jwk]}), AUDIENCE)
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
        for expected, models, budget, lease in (({"unknown": "x"}, ["*"], 1, 1), (dict(EXPECTED), [], 1, 1),
                                                (dict(EXPECTED), ["*"], 0, 1), (dict(EXPECTED), ["*"], 1, 0)):
            with self.assertRaises(ValueError):
                self.exchange.issue(expected, models, budget, lease)


class RelayTests(unittest.TestCase):
    """Turns queued by the operator, served to a live runner session of their target."""

    def setUp(self):
        ExchangeTests.setUp(self)
        self.runner = self.exchange.key(self.session, self.signer.token(), now=NOW)["token"]

    def submit(self, expected=None, payload=None, turn=None, pin=None):
        return self.exchange.submit(turn or str(uuid.uuid4()), dict(expected or EXPECTED), ["claude-*"], 1000, 3600,
                                    payload or {"stage": "plan"}, pin=pin, now=NOW)

    def test_a_pinned_turn_is_served_only_to_its_session_and_lost_with_it(self):
        other = self.exchange.issue(dict(EXPECTED), ["claude-*"], 1000, 3600)
        second = self.exchange.key(other, self.signer.token(run_id="91"), now=NOW)["token"]
        pinned = self.submit(pin=self.session)
        self.assertIsNone(pinned["dispatch"], "a pinned turn never staffs a new session")
        self.assertEqual(self.exchange.next(other, second, 4, 0, now=NOW), {"turns": []})
        self.assertEqual([t["id"] for t in self.exchange.next(self.session, self.runner, 4, 0, now=NOW)["turns"]],
                         [pinned["turn"]])
        waiting = self.submit(pin=self.session)
        later = NOW + exchange.HEARTBEAT_SECONDS + 1
        self.assertEqual(self.exchange.poll(waiting["turn"], 0, now=later)["status"], "lost",
                         "only its session could serve it")
        with self.assertRaises(Refused) as raised:
            self.submit(expected={**EXPECTED, "repository_id": "9"}, pin=other)
        self.assertEqual(raised.exception.status, 409, "a pin names a session of the turn's own target")
        with self.assertRaises(ValueError):
            self.submit(pin="not-a-session")

    def test_an_older_database_gains_the_new_turn_columns(self):
        database = Path(tempfile.mkdtemp(dir=self.directory)) / "exchange.db"
        with sqlite3.connect(database) as db:
            db.executescript("""
                CREATE TABLE turns (id TEXT PRIMARY KEY, expected TEXT NOT NULL, models TEXT NOT NULL,
                    budget INTEGER NOT NULL, lease REAL NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                    session TEXT, result TEXT, created REAL NOT NULL, started REAL, finished REAL);""")
            db.execute("INSERT INTO turns (id, expected, models, budget, lease, payload, status, created) "
                       "VALUES ('old', ?, '[\"claude-*\"]', 1000, 3600, '{}', 'queued', ?)",
                       (json.dumps(EXPECTED, sort_keys=True), NOW))
        for _ in range(2):
            self.exchange = Exchange(str(database), self.gateway, Keys(fetch=lambda: {"keys": [self.signer.jwk]}),
                                     AUDIENCE)
        self.session = self.exchange.issue(dict(EXPECTED), ["claude-*"], 1000, 3600)
        self.runner = self.exchange.key(self.session, self.signer.token(), now=NOW)["token"]
        pinned = self.submit(pin=self.session)
        served = self.exchange.next(self.session, self.runner, 4, 0, now=NOW)["turns"]
        self.assertEqual({t["id"] for t in served}, {"old", pinned["turn"]})

    def test_a_resubmitted_turn_returns_its_original_admission(self):
        self.exchange.end(self.session, runner=self.runner, now=NOW)
        turn = str(uuid.uuid4())
        first = self.submit(turn=turn)
        self.assertIsNotNone(first["dispatch"])
        self.assertEqual(self.submit(turn=turn), first, "the lost reply is recovered, with its session")
        self.assertEqual(self.exchange.db.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 1)
        with self.assertRaises(Refused) as raised:
            self.submit(turn=turn, payload={"stage": "review"})
        self.assertEqual(raised.exception.status, 409)
        runner = self.exchange.key(first["dispatch"], self.signer.token(run_id="90"), now=NOW)["token"]
        self.assertIsNone(self.submit(turn=turn)["dispatch"], "a bound session is not handed out again")
        served = self.exchange.next(first["dispatch"], runner, 1, 0, now=NOW)
        self.assertEqual([t["id"] for t in served["turns"]], [turn])
        self.exchange.complete(first["dispatch"], runner, turn, {"exit": 0}, now=NOW)
        self.assertEqual(self.submit(turn=turn), {"turn": turn, "dispatch": None, "status": "done", "result": {"exit": 0}},
                         "a turn completed before its admission was recovered returns its result")
        late = str(uuid.uuid4())
        self.exchange.cancel(late, now=NOW)
        self.assertEqual(self.submit(turn=late), {"turn": late, "dispatch": None, "status": "cancelled"},
                         "an admission arriving after its withdrawal stays withdrawn")
        with self.assertRaises(ValueError):
            self.submit(turn="not-a-uuid")

    def test_a_turn_is_served_to_its_targets_runner_and_its_result_returned(self):
        queued = self.submit(payload={"stage": "plan", "n": 1})
        self.assertIsNone(queued["dispatch"], "the target already has a live session")
        other = self.submit(expected={**EXPECTED, "repository_id": "9"})
        self.assertIsNotNone(other["dispatch"], "another target gets its own session")
        served = self.exchange.next(self.session, self.runner, 4, 0, now=NOW)
        self.assertEqual(served["turns"], [{"id": queued["turn"], "payload": {"stage": "plan", "n": 1}}])
        self.assertEqual(self.exchange.poll(queued["turn"], 0, now=NOW), {"status": "running"})
        self.exchange.complete(self.session, self.runner, queued["turn"], {"exit": 0}, now=NOW)
        self.assertEqual(self.exchange.poll(queued["turn"], 0, now=NOW), {"status": "done", "result": {"exit": 0}})
        with self.assertRaises(Refused) as raised:
            self.exchange.complete(self.session, self.runner, queued["turn"], {"exit": 1}, now=NOW)
        self.assertEqual(raised.exception.status, 409, "a result is accepted once")
        self.assertEqual(self.exchange.next(self.session, self.runner, 4, 0, now=NOW), {"turns": []})

    def test_runner_calls_need_the_sessions_runner_token(self):
        for token in ("0" * 64, "short"):
            with self.subTest(token=token), self.assertRaises(Refused) as raised:
                self.exchange.next(self.session, token, 1, 0, now=NOW)
            self.assertEqual(raised.exception.status, 401)
        with self.assertRaises(Refused) as raised:
            self.exchange.next(self.session, self.runner, 9, 0, now=NOW)
        self.assertEqual(raised.exception.status, 400)
        self.exchange.end(self.session, runner=self.runner, now=NOW)
        self.assertEqual(self.gateway.list(), [])
        with self.assertRaises(Refused) as raised:
            self.exchange.next(self.session, self.runner, 1, 0, now=NOW)
        self.assertEqual(raised.exception.status, 410, "an ended session tells its runner to stop")

    def test_a_silent_runner_loses_its_turns_and_its_target_gets_a_new_session(self):
        lost = self.submit()
        self.exchange.next(self.session, self.runner, 1, 0, now=NOW)
        waiting = self.submit()
        later = NOW + exchange.HEARTBEAT_SECONDS + 1
        self.assertEqual(self.exchange.poll(lost["turn"], 0, now=later), {"status": "lost"})
        self.assertEqual(self.gateway.list(), [], "the silent session's key is deleted")
        polled = self.exchange.poll(waiting["turn"], 0, now=later)
        self.assertEqual(polled["status"], "queued")
        self.assertIsNotNone(polled["dispatch"])
        self.assertIsNone(self.exchange.poll(waiting["turn"], 0, now=later)["dispatch"], "dispatched once")
        replacement = self.exchange.row(polled["dispatch"])
        self.assertEqual(json.loads(replacement["expected"]), EXPECTED)

    def test_a_refused_runner_is_reported_to_the_operator(self):
        self.exchange.end(self.session, runner=self.runner, now=NOW)
        queued = self.submit()
        session = queued["dispatch"]
        with self.assertRaises(Refused):
            self.exchange.key(session, self.signer.token(repository_id="9", run_id="79"), now=NOW)
        self.assertIsNone(self.exchange.poll(queued["turn"], 0, now=NOW)["refusal"],
                          "another repository's token is not the target's problem")
        with self.assertRaises(Refused):
            self.exchange.key(session, self.signer.token(job_workflow_sha="b" * 40, run_id="80"), now=NOW)
        polled = self.exchange.poll(queued["turn"], 0, now=NOW)
        self.assertEqual(polled["refusal"], "token does not match the session: job_workflow_sha")

    def test_a_cancelled_turn_is_not_served_and_its_result_refused(self):
        waiting, running = self.submit(), self.submit()
        self.exchange.cancel(waiting["turn"], now=NOW)
        served = self.exchange.next(self.session, self.runner, 4, 0, now=NOW)
        self.assertEqual([t["id"] for t in served["turns"]], [running["turn"]])
        self.exchange.cancel(running["turn"], now=NOW)
        with self.assertRaises(Refused) as raised:
            self.exchange.complete(self.session, self.runner, running["turn"], {}, now=NOW)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.exchange.poll(running["turn"], 0, now=NOW), {"status": "cancelled"})

    def test_host_keys_are_minted_at_once_and_refuse_runners(self):
        minted = self.exchange.mint(["claude-*"], 500, 600, "owner/repo#5:plan")
        self.assertTrue(minted["key"].startswith("sk-agw-"))
        self.assertIsNotNone(self.gateway.find(minted["key"]))
        with self.assertRaisesRegex(Refused, "not a runner session"):
            self.exchange.key(minted["session"], self.signer.token(run_id="81"), now=NOW)
        self.exchange.end(minted["session"])
        self.assertIsNone(self.gateway.find(minted["key"]))
        for label in ("", "bad label", "x" * 201):
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.exchange.mint(["*"], 1, 1, label)


class HttpTests(unittest.TestCase):
    """The runner-facing HTTP surface."""

    def setUp(self):
        ExchangeTests.setUp(self)
        clock = patch.object(exchange.time, "time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.server = server(self.exchange, "127.0.0.1:0", "operator-secret")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def post(self, path, token=None, body=None, method="POST"):
        request = urllib.request.Request(self.base + path, method=method,
                                         data=None if method == "GET" else json.dumps(body or {}).encode(),
                                         headers={"Authorization": "Bearer " + token} if token else {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_key_and_end_over_http(self):
        status, body = self.post(f"/exchange/sessions/{self.session}/key", self.signer.token())
        self.assertEqual(status, 200)
        self.assertTrue(body["key"].startswith("sk-agw-"))
        self.assertEqual(self.post(f"/exchange/sessions/{self.session}/key")[0], 401)
        self.assertEqual(self.post("/exchange/other", self.signer.token())[0], 404)
        self.assertEqual(self.post(f"/exchange/sessions/{self.session}/end", self.signer.token())[0], 200)
        self.assertEqual(self.gateway.list(), [])

    def test_a_turn_round_trip_over_http(self):
        submit = {"turn": str(uuid.uuid4()), "expected": EXPECTED, "models": ["claude-*"], "budget": 1000, "lease": 3600,
                  "payload": {"stage": "plan"}}
        self.assertEqual(self.post("/exchange/operator/turns", None, submit)[0], 401)
        self.assertEqual(self.post("/exchange/operator/turns", "wrong", submit)[0], 401)
        status, queued = self.post("/exchange/operator/turns", "operator-secret", submit)
        self.assertEqual((status, queued["dispatch"]), (200, None))
        runner = self.post(f"/exchange/sessions/{self.session}/key", self.signer.token())[1]["token"]
        status, served = self.post(f"/exchange/sessions/{self.session}/next", runner, {"capacity": 1, "wait": 0})
        self.assertEqual((status, [t["id"] for t in served["turns"]]), (200, [queued["turn"]]))
        status, _ = self.post(f"/exchange/sessions/{self.session}/turns/{queued['turn']}", runner, {"result": {"exit": 0}})
        self.assertEqual(status, 200)
        status, polled = self.post(f"/exchange/operator/turns/{queued['turn']}?wait=0", "operator-secret", method="GET")
        self.assertEqual((status, polled), (200, {"status": "done", "result": {"exit": 0}}))
        status, minted = self.post("/exchange/operator/keys", "operator-secret",
                                   {"models": ["claude-*"], "budget": 10, "lease": 60, "label": "host"})
        self.assertEqual(status, 200)
        self.assertEqual(self.post(f"/exchange/operator/sessions/{minted['session']}/end", "operator-secret")[0], 200)
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
        request = urllib.request.Request(f"http://127.0.0.1:{busy.server_address[1]}/exchange/x", method="POST", data=b"")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(raised.exception.code, 503)


if __name__ == "__main__":
    unittest.main()
