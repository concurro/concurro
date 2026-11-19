"""Durable behavior of the Python reference consumer."""

import asyncio
import json
import multiprocessing
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from concurro_contracts.validation import UnsupportedVersion
from consumer import Action, Consumer, Outcome, delivery_key, inspect_consumer, stream_identity

FIXTURE = (Path(__file__).resolve().parents[2] / "fixtures/github.issues.v1/valid/opened.json").read_bytes()


class ConsumerTests(unittest.TestCase):
    """Exercise SQLite effects, retries, held rows, and version filtering."""

    def test_stream_identity_preserves_nanos_and_rejects_mixed_floor(self):
        """A replaced stream cannot borrow another incarnation's ack floor."""
        first = "2026-11-19T01:02:03.123456789Z"
        second = "2026-11-19T01:03:03.987654321Z"
        response = lambda created: SimpleNamespace(
            data=json.dumps({"created": created, "state": {"first_seq": 1}}).encode()
        )
        client = Mock(request=AsyncMock(side_effect=[response(first), response(second)]))
        jetstream = Mock(
            consumer_info=AsyncMock(return_value=SimpleNamespace(ack_floor=SimpleNamespace(stream_seq=100)))
        )
        with self.assertRaisesRegex(RuntimeError, "incarnation changed"):
            asyncio.run(inspect_consumer(client, jetstream, "GITHUB", "service"))
        client.request = AsyncMock(return_value=response(first))
        self.assertEqual(asyncio.run(stream_identity(client, "GITHUB")), (first, 1))

    def test_stream_replacement_before_first_ack_is_loss(self):
        """The initialized zero floor still binds the first stream incarnation."""
        with tempfile.TemporaryDirectory() as directory:
            consumer = Consumer(Path(directory) / "consumer.sqlite", "service", 3)
            self.assertIsNone(consumer.observe_stream("first", 1, 0))
            self.assertEqual(consumer.observe_stream("replacement", 1, 0), ("first", 0))
            consumer.close()

    def test_parked_replay_blocks_coverage_until_applied(self):
        """An operator replay keeps the coverage fact stale until its effect commits."""
        with tempfile.TemporaryDirectory() as directory:
            consumer = Consumer(Path(directory) / "consumer.sqlite", "python", 1)
            self.assertEqual(
                consumer.process(FIXTURE, "park-me", lambda db, event: (Outcome.RETRY, "temporary")),
                Action.NAK,
            )
            self.assertEqual(
                consumer.process(FIXTURE, "park-me", lambda db, event: (Outcome.RETRY, "temporary")),
                Action.TERM,
            )
            consumer.observe_stream("generation", 1, 1)
            consumer.observe_stream("generation", 1, 1)
            self.assertFalse(consumer.coverage_applied("generation", 1))
            self.assertEqual(consumer.observe_stream("replacement", 1, 0), ("generation", 1))
            consumer.resolve_loss("replacement", 2)
            self.assertEqual(
                consumer.process(FIXTURE, "replacement:1", lambda db, event: (Outcome.DONE, "")),
                Action.ACK,
            )
            self.assertFalse(consumer.coverage_applied("replacement", 2))
            [parking] = consumer.parked_command("list")
            self.assertEqual(
                consumer.parked_command("show", parking["id"])["wire_message"]["type"], "github.issues.opened"
            )
            consumer.parked_command("replay", parking["id"], "operator")
            self.assertFalse(consumer.coverage_applied("replacement", 2))
            self.assertEqual(consumer.process_released(lambda db, event: (Outcome.DONE, "")), Action.ACK)
            self.assertTrue(consumer.coverage_applied("replacement", 2))
            self.assertEqual(consumer.parked_command("list"), [])
            consumer.close()

    def test_replayed_parking_resolves_after_later_discard(self):
        """A superseded replay cannot keep coverage blocked after the latest parking is discarded."""
        with tempfile.TemporaryDirectory() as directory:
            consumer = Consumer(Path(directory) / "consumer.sqlite", "python", 1)
            self.assertEqual(consumer.process(FIXTURE, "rejected", lambda db, event: (Outcome.REJECT, "no")), Action.TERM)
            consumer.observe_stream("generation", 1, 1)
            consumer.observe_stream("generation", 1, 1)
            [first] = consumer.parked_command("list")
            consumer.parked_command("replay", first["id"], "operator")
            self.assertEqual(consumer.process_released(lambda db, event: (Outcome.REJECT, "still no")), Action.TERM)
            [second] = consumer.parked_command("list")
            self.assertNotEqual(first["id"], second["id"])
            consumer.parked_command("discard", second["id"], "operator", "reviewed")
            self.assertEqual(consumer.parked_command("list"), [])
            self.assertTrue(consumer.coverage_applied("generation", 1))
            consumer.close()

    def test_malformed_depth_and_header_key_have_bounded_attempts(self):
        """Decoder depth errors and hostile header keys reach ordinary parking decisions."""
        with tempfile.TemporaryDirectory() as directory:
            consumer = Consumer(Path(directory) / "consumer.sqlite", "python", 1)
            raw = b"[" * 20_000 + b"]" * 20_000
            with self.assertRaises(RecursionError):
                json.loads(raw)
            key = "bad\0" + "x" * 1000
            for action in (Action.NAK, Action.TERM, Action.ACK):
                self.assertEqual(consumer.process(raw, key, lambda db, event: (Outcome.DONE, "")), action)
            [(alias,)] = consumer.db.execute("SELECT message_key FROM identity_aliases").fetchall()
            self.assertTrue(alias.startswith("sha256:"))
            self.assertLess(len(alias), 100)
            [parking] = consumer.parked_command("list")
            self.assertIn("wire_message", consumer.parked_command("show", parking["id"]))
            consumer.close()

        with tempfile.TemporaryDirectory() as directory:
            consumer = Consumer(
                Path(directory) / "consumer.sqlite", "python", 1, decoder=Mock(side_effect=OverflowError("clock"))
            )
            self.assertEqual(consumer.process(FIXTURE, "overflow", lambda db, event: (Outcome.DONE, "")), Action.NAK)
            self.assertEqual(consumer.process(FIXTURE, "overflow", lambda db, event: (Outcome.DONE, "")), Action.TERM)
            consumer.close()

    def test_unsupported_released_version_parks_for_operator(self):
        """An old-version held row becomes a visible parking that can be resolved."""
        with tempfile.TemporaryDirectory() as directory:
            consumer = Consumer(Path(directory) / "consumer.sqlite", "python", 1)
            wire = json.loads(FIXTURE)
            wire["version"] = 2
            consumer.db.execute(
                "INSERT INTO held (consumer, event_id, key, wire_bytes, released) VALUES (?, ?, ?, ?, 1)",
                ("python", wire["id"], "ready", json.dumps(wire).encode()),
            )
            self.assertEqual(consumer.process_released(lambda db, event: (Outcome.DONE, "")), Action.TERM)
            self.assertIsNone(consumer.process_released(lambda db, event: (Outcome.DONE, "")))
            self.assertEqual(consumer.db.execute("SELECT count(*) FROM held").fetchone()[0], 0)
            [parking] = consumer.parked_command("list")
            self.assertIn("unsupported contract version", parking["reason"])
            consumer.parked_command("replay", parking["id"], "operator")
            self.assertEqual(consumer.process_released(lambda db, event: (Outcome.DONE, "")), Action.TERM)
            self.assertEqual(consumer.db.execute("SELECT count(*) FROM held").fetchone()[0], 0)
            [latest] = consumer.parked_command("list")
            consumer.parked_command("discard", latest["id"], "operator", "version reviewed")
            self.assertEqual(consumer.parked_command("list"), [])
            consumer.close()

    def test_dual_published_unsupported_copy_skips_owned_retry(self):
        """An unreadable copy with the same event ID cannot park the readable retry."""
        with tempfile.TemporaryDirectory() as directory:
            consumer = Consumer(Path(directory) / "consumer.sqlite", "python", 3)
            self.assertEqual(
                consumer.process(FIXTURE, "old", lambda db, event: (Outcome.RETRY, "temporary")), Action.NAK
            )
            wire = json.loads(FIXTURE)
            wire["version"] = 2
            unsupported = json.dumps(wire).encode()
            self.assertEqual(
                consumer.process(unsupported, "new", lambda db, event: (Outcome.DONE, "")), Action.ACK
            )
            applying = Mock(return_value=(Outcome.DONE, ""))
            self.assertEqual(consumer.process(FIXTURE, "old", applying), Action.ACK)
            applying.assert_called_once()
            self.assertEqual(consumer.parked_command("list"), [])
            wire["id"] = str(uuid.uuid4())
            self.assertEqual(
                consumer.process(json.dumps(wire).encode(), "fresh", lambda db, event: (Outcome.DONE, "")),
                Action.ACK,
            )
            self.assertEqual(consumer.db.execute("SELECT count(*) FROM identity_aliases").fetchone()[0], 1)
            consumer.close()

    def test_unsupported_version_parks_after_decoder_upgrade(self):
        """The same old bytes and key park when a later binary drops their decoder."""
        with tempfile.TemporaryDirectory() as directory:
            consumer = Consumer(Path(directory) / "consumer.sqlite", "python", 3)
            self.assertEqual(
                consumer.process(FIXTURE, "event:1", lambda db, event: (Outcome.RETRY, "temporary")), Action.NAK
            )
            old_decoder = consumer.decoder
            consumer.decoder = Mock(side_effect=UnsupportedVersion("unsupported contract version 1"))
            self.assertEqual(
                consumer.process(FIXTURE, "event:1", lambda db, event: (Outcome.DONE, "")), Action.TERM
            )
            [parking] = consumer.parked_command("list")
            self.assertIn("version 1", parking["reason"])
            self.assertEqual(consumer.parked_command("show", parking["id"])["wire_message"], json.loads(FIXTURE))
            self.assertEqual(
                consumer.db.execute("SELECT wire_bytes FROM parked WHERE id = ?", (parking["id"],)).fetchone()[0],
                FIXTURE,
            )
            newer = json.loads(FIXTURE)
            newer["version"] = 2
            newer = json.dumps(newer).encode()
            consumer.decoder = Mock(return_value=old_decoder(FIXTURE))
            applying = Mock(return_value=(Outcome.DONE, ""))
            self.assertEqual(consumer.process(newer, "event:2", applying), Action.ACK)
            applying.assert_not_called()
            consumer.parked_command("replay", parking["id"], "operator")
            self.assertEqual(consumer.process(newer, "event:2", applying), Action.ACK)
            consumer.decoder = Mock(side_effect=UnsupportedVersion("unsupported contract version 1"))
            self.assertEqual(consumer.process_released(applying), Action.TERM)
            consumer.decoder = Mock(return_value=old_decoder(FIXTURE))
            self.assertEqual(consumer.process(newer, "event:2", applying), Action.ACK)
            applying.assert_not_called()
            [latest] = consumer.parked_command("list")
            self.assertIn("version 1", latest["reason"])
            self.assertEqual(consumer.parked_command("show", latest["id"])["wire_message"], json.loads(FIXTURE))
            self.assertEqual(
                consumer.db.execute("SELECT wire_bytes FROM parked WHERE id = ?", (latest["id"],)).fetchone()[0],
                FIXTURE,
            )
            consumer.observe_stream("generation", 1, 1)
            consumer.observe_stream("generation", 1, 1)
            self.assertFalse(consumer.coverage_applied("generation", 1))
            consumer.parked_command("discard", latest["id"], "operator", "version reviewed")
            self.assertTrue(consumer.coverage_applied("generation", 1))
            consumer.close()

    def test_effect_and_dedup_survive_restart(self):
        """A repeat delivery cannot reapply a committed handler effect."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "consumer.sqlite"
            consumer = Consumer(path, "python", 1)
            consumer.db.execute("CREATE TABLE effect (value INTEGER)")

            def handle(conn, event):
                self.assertEqual(event.data.root.issue.number, 4294967301)
                conn.execute("INSERT INTO effect VALUES (1)")
                return Outcome.DONE, ""

            self.assertEqual(consumer.process(FIXTURE, "delivery", handle), Action.ACK)
            consumer.close()
            consumer = Consumer(path, "python", 1)
            self.assertEqual(consumer.process(FIXTURE, "delivery", handle), Action.ACK)
            self.assertEqual(consumer.db.execute("SELECT count(*) FROM effect").fetchone()[0], 1)
            consumer.close()

    def test_attempts_parking_version_and_held_release(self):
        """Failed events park at the limit and held events get a fresh budget."""
        with tempfile.TemporaryDirectory() as directory:
            consumer = Consumer(Path(directory) / "consumer.sqlite", "python", 3)
            consumer.db.execute("CREATE TABLE prerequisite (key TEXT PRIMARY KEY)")
            consumer.db.execute("CREATE TABLE effect (event_id TEXT PRIMARY KEY)")
            invalid = b'{"version":1,"data":{"action":"unknown"}}'

            def done(conn, event):
                conn.execute("INSERT INTO effect VALUES (?)", (str(event.id.root),))
                return Outcome.DONE, ""

            self.assertEqual(consumer.process(invalid, "invalid", done), Action.NAK)
            self.assertEqual(consumer.process(invalid, "invalid", done), Action.NAK)
            self.assertEqual(consumer.process(invalid, "invalid", done), Action.NAK)
            self.assertEqual(consumer.process(invalid, "invalid", done), Action.TERM)
            self.assertEqual(consumer.process(invalid, "invalid", done), Action.ACK)
            self.assertEqual(consumer.db.execute("SELECT count(*) FROM parked").fetchone()[0], 1)
            version_two = json.loads(FIXTURE)
            version_two["version"] = 2
            self.assertEqual(consumer.process(json.dumps(version_two).encode(), "v2", done), Action.ACK)
            self.assertEqual(consumer.db.execute("SELECT count(*) FROM parked").fetchone()[0], 1)

            def dependent(conn, event):
                if conn.execute("SELECT 1 FROM prerequisite WHERE key = 'ready'").fetchone():
                    return done(conn, event)
                return Outcome.HOLD, "ready"

            self.assertEqual(consumer.process(FIXTURE, "dependent", dependent), Action.ACK)
            self.assertEqual(consumer.process(FIXTURE, "dependent", dependent), Action.ACK)
            prerequisite = json.loads(FIXTURE)
            prerequisite["id"] = str(uuid.uuid4())

            def provide(conn, event):
                conn.execute("INSERT INTO prerequisite VALUES ('ready')")
                consumer.release("ready")
                return Outcome.DONE, ""

            self.assertEqual(
                consumer.process(json.dumps(prerequisite).encode(), "provider", provide),
                Action.ACK,
            )
            self.assertEqual(consumer.process_released(dependent), Action.ACK)
            self.assertIsNone(consumer.process_released(dependent))
            self.assertEqual(consumer.db.execute("SELECT count(*) FROM held").fetchone()[0], 0)
            self.assertEqual(consumer.db.execute("SELECT count(*) FROM effect").fetchone()[0], 1)
            consumer.close()

    def test_crash_and_retry_attempts_park_once(self):
        """A process stop after the durable count cannot reset the attempt budget."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "consumer.sqlite"

            def crash(conn, event):
                raise SystemExit("injected consumer stop")

            for _ in range(3):
                consumer = Consumer(path, "python", 3)
                with self.assertRaises(SystemExit):
                    consumer.process(FIXTURE, "crashing", crash)
                consumer.close()
            consumer = Consumer(path, "python", 3)
            self.assertEqual(consumer.process(FIXTURE, "crashing", crash), Action.TERM)
            self.assertEqual(consumer.process(FIXTURE, "crashing", crash), Action.ACK)
            retry_event = json.loads(FIXTURE)
            retry_event["id"] = str(uuid.uuid4())
            retry_wire = json.dumps(retry_event).encode()
            for _ in range(3):
                self.assertEqual(
                    consumer.process(
                        retry_wire,
                        "retrying",
                        lambda conn, event: (Outcome.RETRY, "temporary failure"),
                    ),
                    Action.NAK,
                )
            self.assertEqual(
                consumer.process(
                    retry_wire,
                    "retrying",
                    lambda conn, event: (Outcome.RETRY, "temporary failure"),
                ),
                Action.TERM,
            )
            self.assertEqual(consumer.db.execute("SELECT count(*) FROM parked").fetchone()[0], 2)
            consumer.close()

    def test_prerequisite_commits_before_hold_confirmation(self):
        """A release after the first Hold is visible to the confirming run."""
        with tempfile.TemporaryDirectory() as directory:
            consumer = Consumer(Path(directory) / "consumer.sqlite", "python", 2)
            consumer.db.execute("CREATE TABLE prerequisite (key TEXT PRIMARY KEY)")
            consumer.db.execute("CREATE TABLE effect (event_id TEXT PRIMARY KEY)")

            def dependent(conn, event):
                if conn.execute("SELECT 1 FROM prerequisite WHERE key = 'ready'").fetchone():
                    conn.execute("INSERT INTO effect VALUES (?)", (str(event.id.root),))
                    return Outcome.DONE, ""
                return Outcome.HOLD, "ready"

            confirm = consumer._confirm_hold

            def commit_before_confirmation(*args):
                consumer.db.execute("BEGIN IMMEDIATE")
                consumer.db.execute("INSERT INTO prerequisite VALUES ('ready')")
                consumer.release("ready")
                consumer.db.execute("COMMIT")
                return confirm(*args)

            consumer._confirm_hold = commit_before_confirmation
            self.assertEqual(consumer.process(FIXTURE, "racing", dependent), Action.ACK)
            self.assertEqual(consumer.db.execute("SELECT count(*) FROM effect").fetchone()[0], 1)
            self.assertEqual(consumer.db.execute("SELECT count(*) FROM held").fetchone()[0], 0)
            consumer.close()

    def test_prerequisite_chain_outlives_attempt_limit(self):
        """Each confirmed Hold resets the budget while waiting for another key."""
        with tempfile.TemporaryDirectory() as directory:
            consumer = Consumer(Path(directory) / "consumer.sqlite", "python", 2)
            consumer.db.execute("CREATE TABLE prerequisite (id INTEGER PRIMARY KEY)")
            consumer.db.execute("CREATE TABLE effect (event_id TEXT PRIMARY KEY)")

            def dependent(conn, event):
                ready = conn.execute("SELECT count(*) FROM prerequisite").fetchone()[0]
                if ready == 5:
                    conn.execute("INSERT INTO effect VALUES (?)", (str(event.id.root),))
                    return Outcome.DONE, ""
                return Outcome.HOLD, f"key-{ready + 1}"

            self.assertEqual(consumer.process(FIXTURE, "chain", dependent), Action.ACK)
            for step in range(1, 6):
                consumer.db.execute("BEGIN IMMEDIATE")
                consumer.db.execute("INSERT INTO prerequisite VALUES (?)", (step,))
                consumer.release(f"key-{step}")
                consumer.db.execute("COMMIT")
                self.assertEqual(consumer.process_released(dependent), Action.ACK)
            self.assertEqual(consumer.db.execute("SELECT count(*) FROM effect").fetchone()[0], 1)
            self.assertEqual(consumer.db.execute("SELECT count(*) FROM parked").fetchone()[0], 0)
            self.assertEqual(consumer.db.execute("SELECT count(*) FROM held").fetchone()[0], 0)
            consumer.close()

    def test_checked_decoder_and_identity_alias(self):
        """Invalid data parks by the envelope ID and a synthetic alias survives repair."""
        with tempfile.TemporaryDirectory() as directory:
            consumer = Consumer(Path(directory) / "consumer.sqlite", "python", 1)
            consumer.db.execute("CREATE TABLE effect (event_id TEXT PRIMARY KEY)")
            calls = []

            def handle(conn, event):
                calls.append(event.id.root)
                conn.execute("INSERT INTO effect VALUES (?)", (str(event.id.root),))
                return Outcome.DONE, ""

            mismatch = json.loads(FIXTURE)
            mismatch["type"] = "github.issues.closed"
            mismatch["id"] = str(uuid.uuid4())
            invalid = json.dumps(mismatch).encode()
            self.assertEqual(consumer.process(invalid, "mismatch", handle), Action.NAK)
            self.assertEqual(consumer.process(invalid, "mismatch", handle), Action.TERM)
            self.assertEqual(calls, [])
            parked_id = consumer.db.execute(
                "SELECT event_id FROM parked WHERE reason = 'attempt limit reached'"
            ).fetchone()[0]
            self.assertEqual(parked_id, mismatch["id"])

            synthetic = json.loads(FIXTURE)
            synthetic["id"] = "invalid-id"
            invalid = json.dumps(synthetic).encode()
            self.assertEqual(consumer.process(invalid, "synthetic", handle), Action.NAK)
            self.assertEqual(consumer.process(invalid, "synthetic", handle), Action.TERM)
            alias = consumer.db.execute(
                "SELECT event_id FROM identity_aliases WHERE message_key = 'synthetic'"
            ).fetchone()[0]
            self.assertNotEqual(alias, json.loads(FIXTURE)["id"])
            consumer.db.execute("BEGIN IMMEDIATE")
            consumer.db.execute("DELETE FROM consumed WHERE event_id = ?", (alias,))
            consumer.db.execute("DELETE FROM attempts WHERE event_id = ?", (alias,))
            consumer.db.execute(
                "INSERT INTO held (consumer, event_id, key, wire_bytes, released) "
                "VALUES ('python', ?, 'operator-replay', ?, 1)",
                (alias, FIXTURE),
            )
            consumer.db.execute("COMMIT")
            self.assertEqual(consumer.process_released(handle), Action.ACK)
            self.assertEqual(consumer.process(FIXTURE, "synthetic", handle), Action.ACK)
            self.assertEqual(consumer.db.execute("SELECT count(*) FROM effect").fetchone()[0], 1)
            self.assertEqual(len(calls), 1)
            consumer.close()

    def test_no_header_key_stays_stable_on_redelivery(self):
        """JetStream reply changes do not change the fallback identity."""
        metadata = SimpleNamespace(stream="INGRESS", sequence=SimpleNamespace(stream=42))
        first = SimpleNamespace(headers=None, reply="delivery-1", metadata=metadata)
        second = SimpleNamespace(headers=None, reply="delivery-2", metadata=metadata)
        self.assertEqual(
            delivery_key(first, "2026-11-19T00:00:00Z"),
            delivery_key(second, "2026-11-19T00:00:00Z"),
        )
        self.assertEqual(
            delivery_key(first, "2026-11-19T00:00:00Z"),
            "INGRESS:2026-11-19T00:00:00Z:42",
        )
        self.assertNotEqual(
            delivery_key(first, "2026-11-19T00:00:00Z"),
            delivery_key(second, "2026-11-19T00:01:00Z"),
        )

    def test_stream_loss_and_coverage_require_settled_effects(self):
        """A replaced stream or parked event blocks freshness across restarts."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "consumer.sqlite"
            consumer = Consumer(path, "service", 1)
            self.assertIsNone(consumer.observe_stream("old", 1, 0))
            self.assertIsNone(consumer.observe_stream("old", 1, 2))
            self.assertTrue(consumer.coverage_applied("old", 2))
            consumer.db.execute(
                "INSERT INTO parked (consumer, event_id, wire_bytes, reason, parked_at) "
                "VALUES ('service', 'event', ?, 'test', 0)",
                (FIXTURE,),
            )
            self.assertFalse(consumer.coverage_applied("old", 2))
            consumer.db.execute("DELETE FROM parked")
            self.assertEqual(consumer.observe_stream("new", 1, 0), ("old", 2))
            self.assertFalse(consumer.coverage_applied("old", 2))
            consumer.close()
            consumer = Consumer(path, "service", 1)
            self.assertEqual(consumer.observe_stream("new", 1, 0), ("old", 2))
            consumer.resolve_loss("new", 0)
            self.assertIsNone(consumer.observe_stream("new", 1, 3))
            self.assertTrue(consumer.coverage_applied("new", 3))
            self.assertFalse(consumer.coverage_applied("old", 2))
            consumer.close()

    def test_separate_processes_serialize_one_delivery(self):
        """A duplicate cannot exhaust the budget while the first handler runs."""
        context = multiprocessing.get_context("fork")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "consumer.sqlite"
            setup = Consumer(path, "python", 1)
            setup.db.execute("CREATE TABLE effect (event_id TEXT PRIMARY KEY)")
            setup.close()
            started = context.Event()
            release = context.Event()
            results = context.SimpleQueue()

            def worker(wait):
                """Handle the same event in a distinct SQLite connection."""
                consumer = Consumer(path, "python", 1)

                def handle(conn, event):
                    """Hold the first effect transaction until its duplicate arrives."""
                    if wait:
                        started.set()
                        assert release.wait(5)
                    conn.execute("INSERT INTO effect VALUES (?)", (str(event.id.root),))
                    return Outcome.DONE, ""

                results.put(consumer.process(FIXTURE, "shared-delivery", handle).value)
                consumer.close()

            first = context.Process(target=worker, args=(True,))
            second = context.Process(target=worker, args=(False,))
            first.start()
            self.assertTrue(started.wait(5))
            second.start()
            time.sleep(0.2)
            self.assertTrue(second.is_alive())
            release.set()
            first.join(5)
            second.join(5)
            self.assertEqual((first.exitcode, second.exitcode), (0, 0))
            self.assertEqual({results.get(), results.get()}, {Action.ACK.value})
            check = Consumer(path, "python", 1)
            self.assertEqual(check.db.execute("SELECT count(*) FROM effect").fetchone()[0], 1)
            self.assertEqual(check.db.execute("SELECT count(*) FROM parked").fetchone()[0], 0)
            check.close()
