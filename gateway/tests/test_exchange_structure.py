"""Pre-extraction contracts for HTTP isolation, locks and durable exchange ordering."""

import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from exchange_fixtures import AUDIENCE, BINDING, EXPECTED, TURN, FakeGateway

import exchange
from exchange import Exchange, Refused

NOW = 1000.0
EXPECTED_RUNNER = {**EXPECTED, "actor_id": "3003"}
SESSION = "01234567-89ab-cdef-0123-456789abcdef"
TURN_ID = "11234567-89ab-cdef-0123-456789abcdef"


class StateFixture:
    """Real temporary SQLite state, with only signed-token verification stubbed."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = str(Path(directory.name) / "exchange.db")
        self.gateway = FakeGateway()
        clock = patch.object(exchange.time, "time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        verifier = patch.object(exchange, "claims", side_effect=lambda token, *args: json.loads(token))
        verifier.start()
        self.addCleanup(verifier.stop)
        self.exchange = self.open()

    def open(self):
        """Open another instance on the same persisted state, as after restart."""
        instance = Exchange(self.database, self.gateway, None, AUDIENCE)
        self.addCleanup(instance.db.close)
        return instance

    def submit(self, instance=None, turn=None, **kwargs):
        """Queue a standard model turn, optionally through a second instance."""
        return (instance or self.exchange).submit(
            turn or str(uuid.uuid4()),
            EXPECTED_RUNNER,
            ["claude-*"],
            1000,
            3600,
            TURN,
            binding=BINDING,
            now=NOW,
            **kwargs,
        )

    def token(self, session):
        """A fresh already-verified identity for one exact session ref."""
        return json.dumps(
            {
                **EXPECTED_RUNNER,
                "ref": "refs/heads/concurro/runs/" + session,
                "runner_environment": "github-hosted",
                "run_id": "77",
                "run_attempt": "1",
                "jti": str(uuid.uuid4()),
                "exp": NOW + 300,
            }
        )

    def claim(self, session, instance=None):
        """Bind a runner and mint its key using the real lifecycle path."""
        return (instance or self.exchange).key(session, self.token(session), now=NOW)["token"]

    def running(self):
        """One admitted and claimed turn with a live runner."""
        queued = self.submit()
        session = queued["dispatch"]
        token = self.claim(session)
        self.exchange.next(session, token, 1, 0, now=NOW)
        return queued["turn"], session, token


class HttpFixture:
    """Small real HTTP servers, all shut down and closed after each test."""

    def start(self, api, operator="operator", slots=2, budget=100):
        """Capture the per-server budget without depending on closure or attribute layout."""
        budgets = []
        budget_type = exchange.Budget

        def allocate(total):
            value = budget_type(total)
            budgets.append(value)
            return value

        with (
            patch.object(exchange, "MAX_REQUESTS", slots),
            patch.object(exchange, "BODY_BUDGET", budget),
            patch.object(exchange, "Budget", side_effect=allocate),
        ):
            host = exchange.server(api, "127.0.0.1:0", operator)
        thread = threading.Thread(target=host.serve_forever, daemon=True)
        thread.start()

        def close():
            host.shutdown()
            host.server_close()
            thread.join(5)

        self.addCleanup(close)
        return host, budgets[0]

    def request(self, host, path, token="operator", body=b"{}", method="POST", length=None):
        """Return the complete stable response contract, excluding server/date headers."""
        connection = http.client.HTTPConnection(*host.server_address, timeout=5)
        try:
            headers = {} if token is None else {"Authorization": "Bearer " + token}
            if length is not None:
                headers["Content-Length"] = str(length)
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read()
            self.assertEqual(response.getheader("Content-Length"), str(len(data)))
            if data:
                self.assertEqual(response.getheader("Content-Type"), "application/json")
            return response.status, data
        finally:
            connection.close()


class HttpContractTests(HttpFixture, unittest.TestCase):
    """Branch-specific admission, query parsing and independent server resources."""

    def test_route_and_authentication_precedence(self):
        api = Mock()
        api.prove.side_effect = Refused(401, "invalid runner token")
        host, _ = self.start(api)
        result = f"/exchange/sessions/{SESSION}/turns/{TURN_ID}"
        rows = [
            ("/exchange/unknown", None, b"[", None, 404, "not found"),
            ("/exchange/operator/turns", None, b"[", exchange.MAX_BODY + 1, 401, "operator token required"),
            ("/exchange/operator/turns", "wrong", b"[", None, 401, "operator token required"),
            (result, "wrong", b"", exchange.MAX_BODY + 1, 401, "invalid runner token"),
            (result, None, b"[", None, 401, "a bearer token is required"),
            ("/exchange/operator/turns", "operator", b"", exchange.MAX_BODY + 1, 413, "request too large"),
            ("/exchange/operator/turns", "operator", b"[]", None, 400, "a JSON object is required"),
        ]
        for action in ("key", "next", "end"):
            rows.append(
                (f"/exchange/sessions/{SESSION}/{action}", "wrong", b"[]", None, 400, "a JSON object is required")
            )
        for path, token, body, length, status, reason in rows:
            with self.subTest(path=path, token=token, length=length):
                self.assertEqual(
                    self.request(host, path, token, body, length=length),
                    (status, json.dumps({"error": reason}).encode()),
                )
        api.key.assert_not_called()
        api.next.assert_not_called()
        api.end.assert_not_called()
        api.complete.assert_not_called()
        api.prove.assert_called_once_with(SESSION, "wrong")
        api.prove.side_effect = None
        self.assertEqual(self.request(host, result, "valid", b"", length=exchange.MAX_BODY + 1)[0], 413)
        self.assertEqual(api.prove.call_count, 2)
        disabled, _ = self.start(api, operator=None)
        self.assertEqual(
            self.request(disabled, "/exchange/operator/turns"), (401, b'{"error": "operator token required"}')
        )

    def test_query_parsing_table_and_dispatch_precedence(self):
        api = Mock()
        api.poll.return_value = {"status": "queued"}
        host, _ = self.start(api)
        for query, value in (("wait=1&wait=2", 2.0), ("", 0.0), ("other=1", 0.0)):
            self.assertEqual(
                self.request(host, f"/exchange/operator/turns/{TURN_ID}?{query}", method="GET"),
                (200, b'{"status": "queued"}'),
            )
            api.poll.assert_called_with(TURN_ID, value)
        for query, value in (("wait", ""), ("wait=", ""), ("wait=%31", "%31")):
            for method, path in (
                ("GET", f"turns/{TURN_ID}"),
                ("POST", "turns"),
                ("DELETE", f"turns/{TURN_ID}"),
                ("POST", f"sessions/{SESSION}/end"),
            ):
                with self.subTest(query=query, method=method, path=path):
                    self.assertEqual(
                        self.request(host, f"/exchange/operator/{path}?{query}", method=method),
                        (400, json.dumps({"error": f"could not convert string to float: '{value}'"}).encode()),
                    )
        api.submit.assert_not_called()
        api.cancel.assert_not_called()
        api.end.assert_not_called()

    def test_per_server_semaphore_budget_and_token(self):
        first, first_budget = self.start(Mock(), operator="first")
        second, second_budget = self.start(Mock(), operator="second")
        self.assertIsNot(first.slots, second.slots)
        self.assertIsNot(first_budget, second_budget)
        for _ in range(2):
            self.assertTrue(first.slots.acquire(blocking=False))
        try:
            self.assertEqual(self.request(first, "/exchange/unknown"), (503, b""))
            self.assertEqual(self.request(second, "/exchange/unknown"), (404, b'{"error": "not found"}'))
        finally:
            for _ in range(2):
                first.slots.release()
        with first_budget.reserve(100):
            self.assertEqual(second_budget.free, 100)
            self.assertEqual(
                self.request(second, "/exchange/operator/unknown", "second"), (404, b'{"error": "not found"}')
            )
        for host, good, bad in ((first, "first", "second"), (second, "second", "first")):
            self.assertEqual(self.request(host, "/exchange/operator/unknown", bad)[0], 401)
            self.assertEqual(self.request(host, "/exchange/operator/unknown", good)[0], 404)

    def test_resources_released_after_failures_and_before_reply(self):
        api = Mock()
        host, budget = self.start(api, slots=1)
        observed = []
        original = host.RequestHandlerClass.reply

        def reply(handler, status, body):
            observed.append(budget.free)
            return original(handler, status, body)

        def fail(error):
            def invoke(*args, **kwargs):
                self.assertEqual(budget.free, 98)
                raise error

            return invoke

        with patch.object(host.RequestHandlerClass, "reply", reply):
            for body, error, expected in (
                (b"[]", None, (400, b'{"error": "a JSON object is required"}')),
                (b"{}", RuntimeError("private"), (500, b'{"error": "internal error"}')),
                (b"{}", Refused(409, "conflict"), (409, b'{"error": "conflict"}')),
            ):
                api.submit.side_effect = fail(error) if error else None
                self.assertEqual(self.request(host, "/exchange/operator/turns", body=body), expected)
                self.assertEqual(budget.free, 100)
                self.assertTrue(host.slots.acquire(timeout=2))
                host.slots.release()
        self.assertEqual(observed, [100, 100, 100])

    def test_budget_is_held_through_long_poll_handler(self):
        api = Mock()
        host, budget = self.start(api)
        entered, release = threading.Event(), threading.Event()

        def polling(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test release timed out")
            return {"turns": []}

        api.next.side_effect = polling
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.request, host, f"/exchange/sessions/{SESSION}/next", "runner", b'{"wait": 1}')
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(budget.free, 89)
            finally:
                release.set()
            self.assertEqual(pending.result(3), (200, b'{"turns": []}'))
        self.assertEqual(budget.free, 100)


class HttpLifecycleTests(StateFixture, HttpFixture, unittest.TestCase):
    """Real lifecycle through HTTP, with OIDC cryptography covered by test_exchange."""

    def test_full_http_lifecycle_and_replays(self):
        host, _ = self.start(self.exchange, budget=10000)
        payload = {
            "turn": TURN_ID,
            "expected": EXPECTED_RUNNER,
            "models": ["claude-*"],
            "budget": 1000,
            "lease": 3600,
            "payload": TURN,
            "binding": BINDING,
        }
        status, raw = self.request(host, "/exchange/operator/turns", body=json.dumps(payload).encode())
        self.assertEqual(status, 200)
        queued = json.loads(raw)
        session = queued["dispatch"]
        root = f"/exchange/sessions/{session}"
        status, raw = self.request(host, root + "/key", self.token(session))
        self.assertEqual(status, 200)
        runner = json.loads(raw)["token"]
        status, raw = self.request(host, root + "/next", runner)
        self.assertEqual((status, json.loads(raw)), (200, {"turns": [{"id": TURN_ID, "payload": TURN}]}))
        result = root + "/turns/" + TURN_ID
        for _ in range(2):
            self.assertEqual(self.request(host, result, runner, b'{"result":{"ok":true}}'), (200, b"{}"))
        self.assertEqual(
            self.request(host, result, runner, b'{"result":{}}'),
            (409, b'{"error": "turn is not running in this session or its result conflicts"}'),
        )
        self.assertEqual(
            self.request(host, f"/exchange/operator/turns/{TURN_ID}", method="GET"),
            (
                200,
                json.dumps(
                    {
                        "turn": TURN_ID,
                        "status": "done",
                        "created": NOW,
                        "started": NOW,
                        "finished": NOW,
                        "result": {"ok": True},
                    }
                ).encode(),
            ),
        )
        # Real timeout loops, shortened only at their sleep interval.
        with patch.object(exchange, "POLL_SECONDS", 0.001):
            self.assertEqual(self.request(host, root + "/next", runner, b'{"wait":0.005}'), (200, b'{"turns": []}'))
            waiting = self.submit()
            status, raw = self.request(host, f"/exchange/operator/turns/{waiting['turn']}?wait=0.005", method="GET")
            self.assertEqual((status, json.loads(raw)["status"]), (200, "queued"))
        self.assertEqual(
            self.request(host, f"/exchange/operator/turns/{waiting['turn']}", method="DELETE"), (200, b"{}")
        )
        self.assertEqual(self.request(host, root + "/end", runner), (200, b"{}"))
        for operator in (False, True):
            new = self.submit()["dispatch"]
            self.claim(new)
            path = f"/exchange/operator/sessions/{new}/end" if operator else f"/exchange/sessions/{new}/end"
            self.assertEqual(self.request(host, path, "operator" if operator else self.token(new)), (200, b"{}"))


def race(*actions):
    """Start competing operations together and retain explicit refusal outcomes."""
    barrier = threading.Barrier(len(actions))

    def invoke(action):
        barrier.wait(5)
        try:
            return action()
        except Refused as error:
            return error

    with ThreadPoolExecutor(len(actions)) as pool:
        futures = [pool.submit(invoke, action) for action in actions]
        return [future.result(10) for future in futures]


class ConcurrencyTests(StateFixture, unittest.TestCase):
    """Lifecycle races through both the thread lock and independent file-lock handles."""

    def test_two_instances_claim_a_turn_only_once(self):
        queued = self.submit()
        first = queued["dispatch"]
        second = self.exchange.issue(EXPECTED_RUNNER, ["claude-*"], 1000, 3600, binding=BINDING)
        tokens = [self.claim(first), self.claim(second)]
        other = self.open()
        replies = race(
            lambda: self.exchange.next(first, tokens[0], 8, 0, now=NOW),
            lambda: other.next(second, tokens[1], 8, 0, now=NOW),
        )
        self.assertEqual([turn["id"] for reply in replies for turn in reply["turns"]], [queued["turn"]])
        self.assertEqual(self.exchange.db.execute("SELECT status FROM turns").fetchall(), [("running",)])

    def test_submit_and_poll_staff_once_across_instances(self):
        queued = self.submit()
        self.exchange.end(queued["dispatch"], now=NOW)
        other = self.open()
        replies = race(lambda: self.submit(other), lambda: self.exchange.poll(queued["turn"], 0, now=NOW))
        dispatched = [reply["dispatch"] for reply in replies if reply["dispatch"]]
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(
            self.exchange.db.execute("SELECT id FROM sessions WHERE ended IS NULL").fetchall(), [(dispatched[0],)]
        )
        self.assertEqual(self.exchange.db.execute("SELECT status FROM turns").fetchall(), [("queued",), ("queued",)])

    def test_complete_races_cancel(self):
        turn, session, token = self.running()
        other = self.open()
        replies = race(
            lambda: self.exchange.complete(session, token, turn, {"ok": True}, now=NOW),
            lambda: other.cancel(turn, now=NOW),
        )
        observed = other.poll(turn, 0, now=NOW)
        self.assertIn(observed["status"], ("done", "cancelled"))
        if observed["status"] == "done":
            self.assertEqual(replies, [None, None])
            self.assertEqual(observed["result"], {"ok": True})
        else:
            self.assertIsInstance(replies[0], Refused)
            self.assertEqual(replies[0].status, 409)
            self.assertNotIn("result", observed)

    def test_end_and_reconcile_race_claim_and_completion(self):
        for operation in ("end", "reconcile"):
            for action in ("claim", "complete"):
                with self.subTest(operation=operation, action=action):
                    queued = self.submit()
                    session = queued["dispatch"]
                    token = self.claim(session)
                    if action == "complete":
                        self.exchange.next(session, token, 1, 0, now=NOW)
                    other = self.open()
                    # Reconciliation uses a time beyond the session deadline, so either
                    # serialization order ends the session even if the runner heartbeats.
                    stop = (
                        (lambda: other.end(session, now=NOW))
                        if operation == "end"
                        else (lambda: other.reconcile(now=NOW + 4000))
                    )
                    work = (
                        (lambda: self.exchange.next(session, token, 1, 0, now=NOW))
                        if action == "claim"
                        else (lambda: self.exchange.complete(session, token, queued["turn"], {"ok": True}, now=NOW))
                    )
                    result, _ = race(work, stop)
                    status = self.exchange.db.execute(
                        "SELECT status FROM turns WHERE id=?", (queued["turn"],)
                    ).fetchone()[0]
                    self.assertTrue(other.row(session)["ended"])
                    self.assertIsNone(other.row(session)["key"])
                    if action == "claim":
                        self.assertIn(status, ("queued", "lost"))
                        self.assertEqual(status, "queued" if isinstance(result, Refused) else "lost")
                    else:
                        self.assertEqual(status, "lost" if isinstance(result, Refused) else "done")
                    # Keep the next subcase independent of a queued survivor.
                    self.exchange.cancel(queued["turn"], now=NOW)

    def test_long_polls_release_lock_and_sample_clock_after_acquisition(self):
        for operation in ("next", "poll"):
            with self.subTest(operation=operation):
                queued = self.submit()
                session = queued["dispatch"]
                token = self.claim(session)
                original = self.exchange.locked
                events, ticks = [], iter((0.0, 0.0, 2.0))
                held = False

                @contextmanager
                def locked():
                    nonlocal held
                    with original():
                        held = True
                        events.append("lock")
                        try:
                            yield
                        finally:
                            events.append("unlock")
                            held = False

                def clock():
                    self.assertTrue(held)
                    events.append("clock")
                    return NOW

                def sleep(seconds):
                    self.assertFalse(held)
                    self.assertEqual(seconds, 1)
                    events.append("sleep")
                    # Prove another thread can acquire the original non-reentrant lock.
                    acquired = threading.Event()

                    def enter():
                        with original():
                            acquired.set()

                    thread = threading.Thread(target=enter, daemon=True)
                    thread.start()
                    self.assertTrue(acquired.wait(2))
                    thread.join(2)

                with (
                    patch.object(self.exchange, "locked", locked),
                    patch.object(exchange.time, "time", side_effect=clock),
                    patch.object(exchange.time, "monotonic", side_effect=lambda: next(ticks)),
                    patch.object(exchange.time, "sleep", side_effect=sleep),
                ):
                    if operation == "next":
                        self.exchange.next(session, token, 0, 1)
                    else:
                        self.exchange.poll(queued["turn"], 1)
                self.assertEqual(events, ["lock", "clock", "unlock", "sleep", "lock", "clock", "unlock"])
                self.exchange.end(session, now=NOW)
                self.exchange.cancel(queued["turn"], now=NOW)


class Crash(BaseException):
    """Interrupt a boundary, including paths that bypass ordinary Exception handling."""


class TracedDatabase:
    """Record successful statements; inside a transaction only COMMIT makes writes durable."""

    def __init__(self, db, record):
        self.db, self.record = db, record

    def execute(self, sql, parameters=()):
        """Record mutations and transaction control after execution, before returning."""
        result = self.db.execute(sql, parameters)
        if not sql.startswith(("SELECT", "PRAGMA")):
            self.record(sql)
        return result

    def __getattr__(self, name):
        return getattr(self.db, name)


# Literal SQL contracts, independent of the production helpers being extracted.
ENDED = "UPDATE sessions SET ended = COALESCE(ended, ?) WHERE id = ?"
LOST = "UPDATE turns SET status = 'lost', finished = ? WHERE (session = ? AND status = 'running') OR (pin = ? AND status = 'queued')"
FORGET = "UPDATE sessions SET key = NULL, key_id = NULL WHERE id = ?"
TURN_INSERT = "INSERT INTO turns (id, expected, models, budget, lease, payload, status, created, digest, pin, binding, staffing) VALUES (?,?,?,?,?,?,'queued',?,?,?,?,?)"
SESSION_INSERT = "INSERT INTO sessions (id, expected, models, budget, deadline, created, kind, binding, staffing) VALUES (?,?,?,?,?,?,?,?,?)"
STAFF = "UPDATE turns SET staff = ? WHERE id = ?"
KEY_TRACE = [
    "DELETE FROM seen WHERE exp < ?",
    "INSERT INTO seen (jti, exp) VALUES (?, ?)",
    "UPDATE sessions SET run_id = ? WHERE id = ? AND run_id IS NULL",
    "UPDATE sessions SET token = ?, seen = ? WHERE id = ?",
    "UPDATE sessions SET key = ? WHERE id = ?",
    "gateway.find",
    "gateway.create",
    "UPDATE sessions SET key_id = ? WHERE id = ?",
]


class RecoveryTests(StateFixture, unittest.TestCase):
    """Failure injection after successful statements, followed by fresh-connection recovery."""

    @contextmanager
    def trace(self, fail=None):
        """Count every mutation, BEGIN/COMMIT/ROLLBACK and gateway call after it succeeds."""
        events = []

        def record(value):
            if value == FORGET:
                self.assertFalse(self.exchange.db.in_transaction)
            events.append(value)
            if fail is not None and len(events) == fail:
                raise Crash()

        db = self.exchange.db
        self.exchange.db = TracedDatabase(db, record)
        patches = []
        depth = 0
        for name in ("find", "create", "activated", "list", "delete"):
            original = getattr(self.gateway, name)

            def invoke(*args, _name=name, _original=original, **kwargs):
                # FakeGateway.activated calls find internally. Record the public
                # exchange/gateway boundary once, not the fake's implementation.
                nonlocal depth
                self.assertFalse(self.exchange.db.in_transaction)
                depth += 1
                try:
                    result = _original(*args, **kwargs)
                finally:
                    depth -= 1
                self.assertFalse(self.exchange.db.in_transaction)
                if depth == 0:
                    record("gateway." + _name)
                return result

            patcher = patch.object(self.gateway, name, side_effect=invoke)
            patcher.start()
            patches.append(patcher)
        try:
            yield events
        finally:
            self.exchange.db = db
            for patcher in reversed(patches):
                patcher.stop()

    def test_key_order_and_recovery_at_each_boundary(self):
        for boundary in range(1, len(KEY_TRACE) + 1):
            with self.subTest(boundary=boundary):
                session = self.exchange.issue(EXPECTED_RUNNER, ["claude-*"], 1000, 3600, binding=BINDING)
                with self.trace(boundary) as events, self.assertRaises(Crash):
                    self.claim(session)
                self.assertEqual(events, KEY_TRACE[:boundary])
                restarted = self.open()
                stored = restarted.row(session)["key"]
                reply = restarted.key(session, self.token(session), now=NOW)
                if stored:
                    self.assertEqual(reply["key"], stored)
                self.assertEqual(restarted.row(session)["key_id"], self.gateway.find(reply["key"]))
                self.assertFalse(restarted.db.in_transaction)
                restarted.end(session, now=NOW)
        with self.trace() as events:
            session = self.exchange.issue(EXPECTED_RUNNER, ["claude-*"], 1000, 3600, binding=BINDING)
            self.claim(session)
        self.assertEqual(events, [SESSION_INSERT, *KEY_TRACE])

    def test_activation_retry_boundary(self):
        session = self.exchange.issue(EXPECTED_RUNNER, ["claude-*"], 1000, 3600, binding=BINDING)
        # Leave a created gateway key without a recorded key_id.
        with self.trace(7), self.assertRaises(Crash):
            self.claim(session)
        with self.trace() as events:
            self.claim(session)
        self.assertEqual(
            events,
            [
                KEY_TRACE[0],
                KEY_TRACE[1],
                KEY_TRACE[3],
                "gateway.find",
                "gateway.activated",
                KEY_TRACE[-1],
            ],
        )
        # A failure after activation must also retain the same key on retry.
        self.exchange.db.execute("UPDATE sessions SET key_id=NULL WHERE id=?", (session,))
        stored = self.exchange.row(session)["key"]
        with self.trace(5), self.assertRaises(Crash):
            self.claim(session)
        self.assertEqual(self.open().key(session, self.token(session), now=NOW)["key"], stored)

    def test_end_order_and_atomic_retirement_recovery(self):
        """Before commit a live session survives; after commit turn loss is already durable."""
        expected = ["BEGIN IMMEDIATE", ENDED, LOST, "COMMIT", "gateway.delete", FORGET]
        for boundary in range(1, len(expected) + 1):
            with self.subTest(boundary=boundary):
                turn, session, _ = self.running()
                pinned = self.submit(pin=session)["turn"]
                with self.trace(boundary) as events, self.assertRaises(Crash):
                    self.exchange.end(session, now=NOW)
                committed = boundary >= 4
                self.assertEqual(events, expected[:boundary] + ([] if committed else ["ROLLBACK"]))
                self.assertFalse(self.exchange.db.in_transaction)
                restarted = self.open()
                self.assertEqual(restarted.row(session)["ended"], NOW if committed else None)
                for identifier, prior in ((turn, "running"), (pinned, "queued")):
                    status = restarted.db.execute("SELECT status FROM turns WHERE id=?", (identifier,)).fetchone()[0]
                    self.assertEqual(status, "lost" if committed else prior)
                restarted.reconcile(now=NOW)
                if not committed:
                    self.assertTrue(restarted.live(restarted.row(session), NOW))
                    self.assertEqual(restarted.poll(turn, 0, now=NOW)["status"], "running")
                    self.assertEqual(restarted.poll(pinned, 0, now=NOW)["status"], "queued")
                    restarted.end(session, now=NOW)
                self.assertEqual(restarted.poll(turn, 0, now=NOW)["status"], "lost")
                self.assertEqual(restarted.poll(pinned, 0, now=NOW)["status"], "lost")
                self.assertIsNone(restarted.row(session)["key"])
                self.assertEqual(self.gateway.list(), [])

    def test_reconcile_order_and_restart_after_each_boundary(self):
        """A failed retirement rolls back; a committed retirement needs only key cleanup."""
        expected = [
            "BEGIN IMMEDIATE",
            "UPDATE sessions SET ended = ? WHERE id = ?",
            LOST,
            "COMMIT",
            "DELETE FROM turns WHERE finished IS NOT NULL AND finished < ?",
            ENDED,
            "gateway.list",
            "gateway.delete",
            FORGET,
        ]
        for boundary in range(1, len(expected) + 1):
            with self.subTest(boundary=boundary):
                turn, session, _ = self.running()
                pinned = self.submit(pin=session)["turn"]
                with self.trace(boundary) as events, self.assertRaises(Crash):
                    self.exchange.reconcile(now=NOW + 181)
                committed = boundary >= 4
                self.assertEqual(events, expected[:boundary] + ([] if committed else ["ROLLBACK"]))
                self.assertFalse(self.exchange.db.in_transaction)
                restarted = self.open()
                self.assertEqual(restarted.row(session)["ended"], NOW + 181 if committed else None)
                for identifier, prior in ((turn, "running"), (pinned, "queued")):
                    status = restarted.db.execute("SELECT status FROM turns WHERE id=?", (identifier,)).fetchone()[0]
                    self.assertEqual(status, "lost" if committed else prior)
                restarted.reconcile(now=NOW + 181)
                self.assertEqual(restarted.poll(turn, 0, now=NOW + 181)["status"], "lost")
                self.assertEqual(restarted.poll(pinned, 0, now=NOW + 181)["status"], "lost")
                self.assertIsNone(restarted.row(session)["key"])
                self.assertEqual(self.gateway.list(), [])
                # Keep the next trace independent of older ended session rows.
                self.exchange.db.execute("DELETE FROM sessions")
                self.exchange.db.execute("DELETE FROM turns")

    def test_end_failed_deletion_keeps_committed_retirement(self):
        """Gateway refusal cannot revive work; reconciliation retries deletion outside the transaction."""
        for missing_key_id in (False, True):
            with self.subTest(missing_key_id=missing_key_id):
                turn, session, _ = self.running()
                pinned = self.submit(pin=session)["turn"]
                if missing_key_id:
                    self.exchange.db.execute("UPDATE sessions SET key_id=NULL WHERE id=?", (session,))
                with patch.object(self.gateway, "delete", return_value=False), self.trace() as events:
                    with self.assertRaisesRegex(
                        Refused, "gateway did not delete the key; reconciliation will retry"
                    ) as caught:
                        self.exchange.end(session, now=NOW)
                self.assertEqual(caught.exception.status, 502)
                self.assertEqual(
                    events,
                    ["BEGIN IMMEDIATE", ENDED, LOST, "COMMIT"]
                    + (["gateway.find"] if missing_key_id else [])
                    + ["gateway.delete"],
                )
                self.assertFalse(self.exchange.db.in_transaction)
                restarted = self.open()
                self.assertEqual(restarted.row(session)["ended"], NOW)
                self.assertIsNotNone(restarted.row(session)["key"])
                for identifier in (turn, pinned):
                    self.assertEqual(
                        restarted.db.execute("SELECT status FROM turns WHERE id=?", (identifier,)).fetchone()[0], "lost"
                    )
                self.assertEqual(len(self.gateway.list()), 1)
                with self.trace() as cleanup:
                    self.exchange.reconcile(now=NOW)
                self.assertEqual(
                    cleanup,
                    [
                        "DELETE FROM turns WHERE finished IS NOT NULL AND finished < ?",
                        ENDED,
                        "gateway.list",
                        "gateway.delete",
                        FORGET,
                    ],
                )
                self.assertIsNone(restarted.row(session)["key"])
                self.assertEqual(self.gateway.list(), [])

    def test_reconcile_commits_each_session_separately(self):
        """A later session's failure cannot undo the retirement of an earlier session."""
        sessions = [self.exchange.issue(EXPECTED_RUNNER, ["claude-*"], 1000, 3600, binding=BINDING) for _ in range(2)]
        order = [
            row[0]
            for row in self.exchange.db.execute("SELECT id FROM sessions WHERE kind = 'runner' AND ended IS NULL")
        ]
        self.assertCountEqual(order, sessions)
        with self.trace(6) as events, self.assertRaises(Crash):
            self.exchange.reconcile(now=NOW + 1200)
        self.assertEqual(
            events,
            [
                "BEGIN IMMEDIATE",
                "UPDATE sessions SET ended = ? WHERE id = ?",
                LOST,
                "COMMIT",
                "BEGIN IMMEDIATE",
                "UPDATE sessions SET ended = ? WHERE id = ?",
                "ROLLBACK",
            ],
        )
        self.assertFalse(self.exchange.db.in_transaction)
        restarted = self.open()
        self.assertEqual(restarted.row(order[0])["ended"], NOW + 1200)
        self.assertIsNone(restarted.row(order[1])["ended"])
        restarted.reconcile(now=NOW + 1200)
        self.assertTrue(all(restarted.row(session)["ended"] == NOW + 1200 for session in sessions))

    def test_submit_order_and_atomic_admission_recovery(self):
        expected = ["BEGIN IMMEDIATE", TURN_INSERT, SESSION_INSERT, STAFF, "COMMIT"]
        for boundary in range(1, len(expected) + 1):
            with self.subTest(boundary=boundary):
                turn = str(uuid.uuid4())
                with self.trace(boundary) as events, self.assertRaises(Crash):
                    self.submit(turn=turn)
                committed = boundary == len(expected)
                self.assertEqual(events, expected[:boundary] + ([] if committed else ["ROLLBACK"]))
                self.assertFalse(self.exchange.db.in_transaction)
                restarted = self.open()
                sessions = restarted.db.execute("SELECT id FROM sessions").fetchall()
                turns = restarted.db.execute("SELECT id FROM turns").fetchall()
                self.assertEqual(len(sessions), int(committed))
                self.assertEqual(turns, [(turn,)] if committed else [])
                replay = self.submit(restarted, turn=turn)
                self.assertEqual(replay["status"], "queued")
                self.assertIsNotNone(replay["dispatch"])
                self.assertEqual(restarted.db.execute("SELECT id FROM sessions").fetchall(), [(replay["dispatch"],)])
                self.assertEqual(
                    restarted.db.execute("SELECT staff FROM turns WHERE id=?", (turn,)).fetchone()[0],
                    replay["dispatch"],
                )
                if committed:
                    self.assertEqual(replay["dispatch"], sessions[0][0])
                with self.trace() as replay_events:
                    self.assertEqual(self.submit(turn=turn), replay)
                self.assertEqual(replay_events, [])
                self.exchange.db.execute("DELETE FROM sessions")
                self.exchange.db.execute("DELETE FROM turns")

    def test_submit_staffing_outcomes_are_transactional(self):
        for kind in ("new", "pinned", "live"):
            with self.subTest(kind=kind):
                session = self.submit()["dispatch"] if kind != "new" else None
                staffed, observation = self.exchange.staffed, self.exchange.observation

                def staff(*args):
                    self.assertTrue(self.exchange.db.in_transaction)
                    return staffed(*args)

                def observe(*args):
                    self.assertFalse(self.exchange.db.in_transaction)
                    return observation(*args)

                with (
                    patch.object(self.exchange, "staffed", side_effect=staff),
                    patch.object(self.exchange, "observation", side_effect=observe),
                    self.trace() as events,
                ):
                    reply = self.submit(pin=session if kind == "pinned" else None)
                self.assertEqual(
                    events,
                    ["BEGIN IMMEDIATE", TURN_INSERT] + ([SESSION_INSERT] if kind == "new" else []) + [STAFF, "COMMIT"],
                )
                self.assertEqual(reply["dispatch"] is None, kind != "new")
                self.assertEqual(
                    self.exchange.db.execute("SELECT id FROM sessions").fetchall(), [(session or reply["dispatch"],)]
                )
                self.exchange.db.execute("DELETE FROM turns")
                self.exchange.db.execute("DELETE FROM sessions")

    def test_submit_failed_begin_preserves_error(self):
        original = sqlite3.OperationalError("begin refused")
        with self.trace() as events:
            execute = self.exchange.db.execute

            def fail_begin(sql, parameters=()):
                if sql == "BEGIN IMMEDIATE":
                    raise original
                return execute(sql, parameters)

            with patch.object(self.exchange.db, "execute", side_effect=fail_begin):
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    self.submit()
        self.assertIs(caught.exception, original)
        self.assertEqual(events, [])
        self.assertFalse(self.exchange.db.in_transaction)
        self.assertIsNotNone(self.submit(self.open())["dispatch"])

    def test_submit_refusal_rolls_back(self):
        original = Refused(409, "staffing refused")
        with self.trace() as events, patch.object(self.exchange, "staffed", side_effect=original):
            with self.assertRaises(Refused) as caught:
                self.submit()
        self.assertIs(caught.exception, original)
        self.assertEqual(events, ["BEGIN IMMEDIATE", TURN_INSERT, "ROLLBACK"])
        self.assertFalse(self.exchange.db.in_transaction)
        restarted = self.open()
        self.assertEqual(restarted.db.execute("SELECT id FROM turns").fetchall(), [])
        self.assertIsNotNone(self.submit(restarted)["dispatch"])

    def test_submit_rollback_failure_preserves_original_error(self):
        original = Crash()
        db = self.exchange.db
        try:
            with self.trace() as events:
                execute = self.exchange.db.execute

                def fail_rollback(sql, parameters=()):
                    if sql == "ROLLBACK":
                        raise sqlite3.OperationalError("rollback failed")
                    return execute(sql, parameters)

                with (
                    patch.object(self.exchange.db, "execute", side_effect=fail_rollback) as calls,
                    patch.object(self.exchange, "staffed", side_effect=original),
                    self.assertRaises(Crash) as caught,
                ):
                    self.submit()
                self.assertIs(caught.exception, original)
                self.assertEqual(calls.call_args.args[0], "ROLLBACK")
                self.assertEqual(events, ["BEGIN IMMEDIATE", TURN_INSERT])
                self.assertTrue(db.in_transaction)
        finally:
            if db.in_transaction:
                db.execute("ROLLBACK")

    def test_transaction_refuses_nesting_without_rolling_back_outer_work(self):
        db = self.exchange.db
        with self.exchange.locked():
            db.execute("BEGIN IMMEDIATE")
            try:
                with self.assertRaisesRegex(RuntimeError, "must not nest"), self.exchange._transaction():
                    self.fail("nested transaction entered")
                self.assertTrue(db.in_transaction)
            finally:
                db.execute("ROLLBACK")

    def test_submit_failed_commit_with_unlocked_reader(self):
        db = self.exchange.db
        self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        # Only shorten this test's wait; production retains Python sqlite3's default 5-second timeout.
        db.execute("PRAGMA busy_timeout=0")
        self.addCleanup(db.execute, "PRAGMA busy_timeout=5000")
        turn = str(uuid.uuid4())
        with closing(sqlite3.connect(self.database, isolation_level=None)) as reader:
            self.assertEqual(reader.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            reader.execute("BEGIN")
            reader.execute("SELECT id FROM turns").fetchall()
            with self.trace() as events, self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                self.submit(turn=turn)
            # COMMIT failed before the harness could record it; rollback succeeded.
            self.assertEqual(events, ["BEGIN IMMEDIATE", TURN_INSERT, SESSION_INSERT, STAFF, "ROLLBACK"])
            self.assertFalse(db.in_transaction)
            self.assertEqual(reader.execute("SELECT id FROM turns").fetchall(), [])
            self.assertEqual(reader.execute("SELECT id FROM sessions").fetchall(), [])
            reader.execute("ROLLBACK")
        restarted = self.open()
        reply = self.submit(restarted, turn=turn)
        self.assertEqual(self.submit(restarted, turn=turn), reply)
        self.assertEqual(restarted.db.execute("SELECT id FROM turns").fetchall(), [(turn,)])
        self.assertEqual(restarted.db.execute("SELECT id FROM sessions").fetchall(), [(reply["dispatch"],)])

    def test_expected_claims_retained_and_overwritten(self):
        """Both retained and overwritten claims must be strings before any write."""
        for field in ("repository_id", "ref"):
            expected = {**EXPECTED_RUNNER, field: 42}
            with self.subTest(field=field), self.trace() as events:
                with self.assertRaisesRegex(ValueError, "^unknown, missing or non-string expected claims$"):
                    self.exchange.issue(expected, ["claude-*"], 1000, 3600, binding=BINDING)
                with self.assertRaisesRegex(ValueError, "^unknown, missing or non-string expected claims$"):
                    self.exchange.submit(
                        str(uuid.uuid4()), expected, ["claude-*"], 1000, 3600, TURN, binding=BINDING, now=NOW
                    )
            self.assertEqual(events, [])
        admitted = self.submit()
        session = admitted["dispatch"]
        self.assertEqual(
            json.loads(self.exchange.row(session)["expected"]),
            {**EXPECTED_RUNNER, "ref": "refs/heads/concurro/runs/" + session},
        )
        self.assertTrue(self.claim(session))

    def test_issue_keeps_optional_actor_and_empty_string_claims(self):
        """Unbound issue accepts optional actor_id and empty strings without rewriting claims."""
        for expected in (
            EXPECTED,
            EXPECTED_RUNNER,
            {**EXPECTED, "actor_id": ""},
            dict.fromkeys(EXPECTED, ""),
            dict.fromkeys(EXPECTED_RUNNER, ""),
        ):
            with self.subTest(expected=expected):
                session = self.exchange.issue(expected, ["claude-*"], 1000, 3600)
                self.assertEqual(self.exchange.row(session)["expected"], json.dumps(expected, sort_keys=True))

    def test_submit_actor_claim_validation(self):
        """Missing/empty actors reach the runner rule; non-string actors fail the shared validator."""
        rows = [
            (EXPECTED, "runner turns require workflow_dispatch and the App actor"),
            ({**EXPECTED, "actor_id": ""}, "runner turns require workflow_dispatch and the App actor"),
        ]
        rows += [
            ({**EXPECTED, "actor_id": value}, "unknown, missing or non-string expected claims")
            for value in (42, 0, None, True, [], {})
        ]
        for expected, message in rows:
            with self.subTest(expected=expected), self.trace() as events:
                with self.assertRaises(ValueError) as caught:
                    self.exchange.submit(
                        str(uuid.uuid4()), expected, ["claude-*"], 1000, 3600, TURN, binding=BINDING, now=NOW
                    )
                self.assertEqual(str(caught.exception), message)
                if "non-string" in message:
                    with self.assertRaisesRegex(ValueError, "^unknown, missing or non-string expected claims$"):
                        self.exchange.issue(expected, ["claude-*"], 1000, 3600)
            self.assertEqual(events, [])

    def test_submit_validation_precedence(self):
        """Competing invalid inputs retain the ordered first-error contract and admit nothing."""
        claims_error = "unknown, missing or non-string expected claims"
        session_error = "a session needs models, a positive budget and a positive lease"
        actor_error = "runner turns require workflow_dispatch and the App actor"
        rows = [
            ({"turn": "bad", "pin": "bad", "expected": None}, "a turn ID must be a UUID"),
            ({"pin": "bad", "expected": None}, "a pin must be a session ID"),
            ({"expected": None, "models": []}, claims_error),
            ({"expected": {k: v for k, v in EXPECTED_RUNNER.items() if k != "ref"}, "models": []}, claims_error),
            ({"expected": {**EXPECTED_RUNNER, "extra": "value"}, "models": []}, claims_error),
            ({"expected": {**EXPECTED_RUNNER, "repository_id": 42}, "models": []}, claims_error),
            ({"expected": EXPECTED, "models": [], "binding": None}, session_error),
            ({"budget": 0, "binding": None}, session_error),
            ({"lease": 0, "binding": None}, session_error),
            ({"binding": None, "expected": EXPECTED, "payload": {"kind": "bench"}}, "invalid runner task/role binding"),
            ({"expected": EXPECTED, "payload": {"kind": "bench"}}, actor_error),
            ({"expected": {**EXPECTED_RUNNER, "event_name": "push"}, "payload": {"kind": "bench"}}, actor_error),
            (
                {"payload": {"kind": "bench"}, "binding": {**BINDING, "role": "review:claude"}},
                "only model turns are supported",
            ),
            ({"payload": None, "binding": {**BINDING, "role": "review:claude"}}, "only model turns are supported"),
            ({"binding": {**BINDING, "role": "review:claude"}}, "turn does not match its role"),
        ]
        for changes, message in rows:
            with self.subTest(changes=changes), self.trace() as events:
                arguments = {
                    "turn": str(uuid.uuid4()),
                    "expected": EXPECTED_RUNNER,
                    "models": ["claude-*"],
                    "budget": 1000,
                    "lease": 3600,
                    "payload": TURN,
                    "binding": BINDING,
                    "now": NOW,
                }
                with self.assertRaises(ValueError) as caught:
                    self.exchange.submit(**(arguments | changes))
                self.assertEqual(str(caught.exception), message)
            self.assertEqual(events, [])


class DataContractTests(StateFixture, unittest.TestCase):
    """Pin the two read-query contracts and numeric acceptance boundaries."""

    def test_row_and_observation_key_order_and_values(self):
        queued = self.submit()
        session = queued["dispatch"]
        record = self.exchange.row(session)
        expected = {
            "expected": json.dumps({**EXPECTED_RUNNER, "ref": "refs/heads/concurro/runs/" + session}, sort_keys=True),
            "models": '["claude-*"]',
            "budget": 1000,
            "deadline": NOW + 3600,
            "run_id": None,
            "key": None,
            "key_id": None,
            "ended": None,
            "kind": "runner",
            "token": None,
            "seen": None,
            "created": NOW,
            "binding": json.dumps(BINDING, sort_keys=True),
            "staffing": exchange.staffing(EXPECTED_RUNNER, BINDING, ["claude-*"], 1000, 3600),
        }
        self.assertEqual(list(record.items()), list(expected.items()))
        self.claim(session)
        for status in ("queued", "running", "done", "lost", "cancelled"):
            for assigned in (None, session):
                self.exchange.db.execute(
                    "UPDATE turns SET status=?, session=?, started=?, finished=? WHERE id=?",
                    (status, assigned, NOW + 1, NOW + 2, queued["turn"]),
                )
                expected = {
                    "turn": queued["turn"],
                    "status": status,
                    "created": NOW,
                    "started": NOW + 1,
                    "finished": NOW + 2,
                }
                if assigned and status in ("lost", "cancelled"):
                    expected["runner"] = {"session": session, "run_id": "77/1"}
                self.assertEqual(list(self.exchange.observation(queued["turn"]).items()), list(expected.items()))

    def test_capacity_is_readiness_with_one_active_turn_per_session(self):
        """Zero pauses claims; any positive legacy capacity admits exactly one turn."""
        queued = self.submit()
        session = queued["dispatch"]
        token = self.claim(session)
        second = self.submit()
        self.assertEqual(self.exchange.next(session, token, 0, 0, now=NOW), {"turns": []})
        reply = self.exchange.next(session, token, 1000, 0, now=NOW)
        self.assertEqual(len(reply["turns"]), 1)
        self.assertEqual(reply["turns"][0]["id"], queued["turn"])
        self.assertEqual(self.exchange.next(session, token, 1000, 0, now=NOW), {"turns": []})
        self.assertEqual(self.exchange.observation(second["turn"])["status"], "queued")

    def test_capacity_and_wait_boundaries(self):
        turn, session, token = self.running()
        for capacity, accepted in ((True, False), (-1, False), (0, True), (1, True), (8, True), (9, True)):
            if accepted:
                self.assertEqual(self.exchange.next(session, token, capacity, 0, now=NOW), {"turns": []})
            else:
                with self.assertRaisesRegex(Refused, "invalid capacity or wait"):
                    self.exchange.next(session, token, capacity, 0, now=NOW)
        for wait, accepted in ((True, True), (0, True), (50, True), (50.1, False), (-1, False), ("1", False)):
            for name in ("next", "poll"):

                def invoke():
                    if name == "next":
                        return self.exchange.next(session, token, 0, wait, now=NOW)
                    return self.exchange.poll(turn, wait, now=NOW)

                with self.subTest(wait=wait, name=name), patch.object(exchange.time, "monotonic", side_effect=(0, 100)):
                    if accepted:
                        invoke()
                    else:
                        with self.assertRaisesRegex(Refused, "invalid"):
                            invoke()
