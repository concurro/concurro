"""Bus adaptation preserves the existing webhook routing and list projections."""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from consumer import Consumer, Outcome, decode_bus_event

from service.bus import BusWorker, route_event, webhook_payload
from service.ingress import Ingress
from service.list_index import projected

FIXTURES = Path(__file__).resolve().parents[2] / "contracts/fixtures"
REPOSITORY = "example/project"


class Registry:
    """Record route decisions with stable task lookups."""

    def __init__(self):
        self.received = []

    def active(self, repository):
        return {(repository, 3)}

    def task(self, repository, number):
        return (repository, number)

    def kind(self, key):
        return "contribution"

    def base_tasks(self, repository, reference):
        return {(repository, 4)}

    def commit_tasks(self, repository, sha):
        return {(repository, 4)}

    def sponsor_sources(self, key):
        return []

    def receive(self, delivery, keys, event, **fields):
        self.received.append((keys, event, fields))
        return True

    def review_event(self, delivery, key, request, target):
        """Record the reviewer enrollment path for raw and mapped deliveries."""
        self.received.append((delivery, key, request, target))
        return "accepted"


class BusParityTests(unittest.TestCase):
    """Compare raw GitHub payloads with decoded fact reconstruction."""

    def test_route_uses_canonical_repository_for_list_index(self):
        """A mixed-case webhook repository selects the configured list projection."""
        event = decode_bus_event((FIXTURES / "github.issue_comment.v1/valid/representative.json").read_bytes())
        ingress = Mock(route=Mock(return_value="accepted"), repositories={REPOSITORY: REPOSITORY})
        index = Mock()
        with patch("service.bus.webhook_payload") as adapter:
            adapter.return_value = ("issue_comment", {"repository": {"full_name": "Example/Project"}})
            self.assertEqual(route_event(ingress, event, index), "accepted")
        self.assertEqual(index.apply_delivery.call_args.args[2], REPOSITORY)

    def test_missing_adapter_field_is_rejected(self):
        """A KeyError is a malformed delivery, not an unserved repository."""
        event = decode_bus_event((FIXTURES / "github.issue_comment.v1/valid/representative.json").read_bytes())
        worker = BusWorker(Mock(), Mock(route=Mock(side_effect=KeyError("missing"))), Mock(), Mock(), Mock())
        self.assertEqual(worker.handle(None, event, "generation", 1)[0], Outcome.REJECT)

    def test_released_coverage_cannot_claim_current_generation(self):
        """A local replay has no verified JetStream sequence for producer coverage."""
        event = decode_bus_event((FIXTURES / "ingress.coverage.advanced.v1/valid/advanced.json").read_bytes())
        worker = BusWorker(Mock(), Mock(), Mock(), Mock(), Mock())
        with sqlite3.connect(":memory:") as db:
            db.execute(
                "CREATE TABLE coverage_fact (consumer TEXT, covered_until TEXT, stream_created TEXT, stream_sequence INTEGER)"
            )
            self.assertEqual(worker.handle(db, event, "replacement", 0)[0], Outcome.DONE)
            self.assertEqual(db.execute("SELECT count(*) FROM coverage_fact").fetchone()[0], 0)

    def test_replacement_during_inspection_cannot_advance_old_floor(self):
        """A new stream's ack floor cannot overwrite the old generation's position."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials = root / "credentials.json"
            credentials.write_text('{"user":"service","password":"test"}')
            prior = Consumer(root / "github-bus.sqlite", "service", 5)
            prior.observe_stream("old", 1, 0)
            prior.observe_stream("old", 1, 5)
            prior.close()
            response = lambda created: SimpleNamespace(
                data=json.dumps({"created": created, "state": {"first_seq": 1}}).encode()
            )
            jetstream = Mock(
                pull_subscribe=AsyncMock(return_value=SimpleNamespace(fetch=AsyncMock())),
                consumer_info=AsyncMock(return_value=SimpleNamespace(ack_floor=SimpleNamespace(stream_seq=100))),
            )
            client = Mock(
                jetstream=Mock(return_value=jetstream),
                request=AsyncMock(side_effect=[response("old"), response("new")]),
                close=AsyncMock(),
            )
            settings = SimpleNamespace(
                state_dir=root,
                bus_url="nats://localhost:4222",
                bus_credentials_path=credentials,
                bus_max_attempts=5,
                bus_backoff_seconds=1,
            )
            worker = BusWorker(settings, Mock(), Mock(), Mock(), Mock(is_set=Mock(return_value=False)))
            with patch("nats.connect", new=AsyncMock(return_value=client)):
                with self.assertRaisesRegex(RuntimeError, "incarnation changed"):
                    asyncio.run(worker.session())
            with sqlite3.connect(root / "github-bus.sqlite") as db:
                self.assertEqual(
                    db.execute("SELECT stream_created, floor_sequence FROM positions").fetchone(),
                    ("old", 5),
                )
                self.assertEqual(db.execute("SELECT count(*) FROM losses").fetchone()[0], 0)

    def test_replacement_after_fetch_discards_message_before_effect(self):
        """A fetched message is not attributed to an earlier stream incarnation."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials = root / "credentials.json"
            credentials.write_text('{"user":"service","password":"test"}')
            response = lambda created: SimpleNamespace(
                data=json.dumps({"created": created, "state": {"first_seq": 1}}).encode()
            )
            message = SimpleNamespace(data=b"{}", ack=AsyncMock())
            jetstream = Mock(
                pull_subscribe=AsyncMock(return_value=SimpleNamespace(fetch=AsyncMock(return_value=[message]))),
                consumer_info=AsyncMock(return_value=SimpleNamespace(ack_floor=SimpleNamespace(stream_seq=0))),
            )
            client = Mock(
                jetstream=Mock(return_value=jetstream),
                request=AsyncMock(side_effect=[response("old"), response("old"), response("new")]),
                close=AsyncMock(),
            )
            settings = SimpleNamespace(
                state_dir=root,
                bus_url="nats://localhost:4222",
                bus_credentials_path=credentials,
                bus_max_attempts=5,
                bus_backoff_seconds=1,
            )
            worker = BusWorker(settings, Mock(), Mock(), Mock(), Mock(is_set=Mock(return_value=False)))
            worker.handle = Mock()
            with patch("nats.connect", new=AsyncMock(return_value=client)):
                with self.assertRaisesRegex(RuntimeError, "incarnation changed"):
                    asyncio.run(worker.session())
            worker.handle.assert_not_called()
            message.ack.assert_not_awaited()

    def test_routing_and_projection(self):
        for root in sorted(FIXTURES.glob("github.*.v1")):
            for raw_file in sorted((root / "raw").glob("*.json")):
                with self.subTest(family=root.name, fixture=raw_file.name):
                    raw = json.loads(raw_file.read_text())
                    event = decode_bus_event((root / "valid" / raw_file.name).read_bytes())
                    family, adapted = webhook_payload(event)
                    self.assertEqual(family, root.name.removeprefix("github.").removesuffix(".v1"))
                    self.assertEqual(projected(family, raw), projected(family, adapted))
                    first, second = Registry(), Registry()
                    options = {
                        "reviewer": {"id": 10, "login": "writer"},
                        "review_targets": {REPOSITORY: SimpleNamespace(upstream="upstream", fork="fork")},
                    }
                    raw_ingress = Ingress(b"secret", first, {REPOSITORY: 7}, (), **options)
                    bus_ingress = Ingress(b"secret", second, {REPOSITORY: 7}, (), **options)
                    self.assertEqual(
                        raw_ingress.route("delivery", family, raw, strict_list_index=True),
                        bus_ingress.route("delivery", family, adapted, strict_list_index=True),
                    )
                    self.assertEqual(first.received, second.received)

    def test_restart_waits_for_new_coverage_with_backlog(self):
        """A prior process's coverage row cannot verify a restarted consumer."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials = root / "credentials.json"
            credentials.write_text('{"user":"service","password":"test"}')
            created = "2026-11-19T00:00:00Z"
            consumer = Consumer(root / "github-bus.sqlite", "service", 5)
            consumer.db.execute(
                "CREATE TABLE coverage_fact (consumer TEXT PRIMARY KEY, covered_until TEXT NOT NULL, "
                "stream_created TEXT NOT NULL, stream_sequence INTEGER NOT NULL)"
            )
            consumer.db.execute("INSERT INTO coverage_fact VALUES (?, ?, ?, ?)", ("service", created, created, 1))
            consumer.observe_stream(created, 1, 0)
            consumer.observe_stream(created, 1, 1)
            consumer.close()
            stream = SimpleNamespace(data=json.dumps({"created": created, "state": {"first_seq": 1}}).encode())
            info = SimpleNamespace(ack_floor=SimpleNamespace(stream_seq=1))
            jetstream = Mock(
                consumer_info=AsyncMock(return_value=info),
                pull_subscribe=AsyncMock(return_value=SimpleNamespace(fetch=AsyncMock(return_value=[]))),
            )
            client = Mock(
                jetstream=Mock(return_value=jetstream), request=AsyncMock(return_value=stream), close=AsyncMock()
            )
            stopping = Mock(is_set=Mock(side_effect=[False, True]))
            index = Mock()
            settings = SimpleNamespace(
                state_dir=root,
                bus_url="nats://localhost:4222",
                bus_credentials_path=credentials,
                bus_max_attempts=5,
                bus_backoff_seconds=1,
            )
            worker = BusWorker(settings, Mock(), index, Mock(), stopping)
            with patch("nats.connect", new=AsyncMock(return_value=client)):
                asyncio.run(worker.session())
            index.covered.assert_not_called()

    def test_newer_pending_coverage_keeps_last_applied_boundary(self):
        """A pending newer event does not promote its coverage or hide the prior applied boundary."""
        for hold, resolve in ((False, False), (True, False), (True, True)):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                credentials = root / "credentials.json"
                credentials.write_text('{"user":"service","password":"test"}')
                created = "2026-11-19T00:00:00Z"
                consumer = Consumer(root / "github-bus.sqlite", "service", 5, decode_bus_event)
                consumer.db.execute(
                    "CREATE TABLE coverage_fact (consumer TEXT PRIMARY KEY, covered_until TEXT NOT NULL, "
                    "stream_created TEXT NOT NULL, stream_sequence INTEGER NOT NULL)"
                )
                consumer.db.execute(
                    "INSERT INTO coverage_fact VALUES (?, ?, ?, ?)",
                    ("service", "2026-11-18T23:00:00Z", created, 1),
                )
                consumer.observe_stream(created, 1, 0)
                consumer.observe_stream(created, 1, 1)
                consumer.close()
                raw = json.loads((FIXTURES / "ingress.coverage.advanced.v1/valid/advanced.json").read_text())
                newer = {**raw, "id": "019acb8d-c0c0-7000-8000-000000000003", "data": {"covered_until": created}}
                messages = [
                    SimpleNamespace(
                        subject="ingress.coverage.advanced",
                        data=json.dumps(event).encode(),
                        headers={},
                        metadata=SimpleNamespace(stream="GITHUB", sequence=SimpleNamespace(stream=sequence)),
                        ack=AsyncMock(),
                    )
                    for sequence, event in ((2, raw), (3, newer))
                ]
                response = SimpleNamespace(data=json.dumps({"created": created, "state": {"first_seq": 1}}).encode())
                floors = iter((1, 2, 3, 3))
                fetches = 0

                async def fetch(*_args, **_kwargs):
                    """Resolve the held row after the session has observed its stale state."""
                    nonlocal fetches
                    fetches += 1
                    if resolve and fetches == 3:
                        with sqlite3.connect(root / "github-bus.sqlite") as database:
                            database.execute("DELETE FROM held WHERE consumer = 'service'")
                    return [[messages[0]], [messages[1]]][fetches - 1] if fetches <= 2 else []

                jetstream = Mock(
                    pull_subscribe=AsyncMock(return_value=SimpleNamespace(fetch=AsyncMock(side_effect=fetch))),
                    consumer_info=AsyncMock(
                        side_effect=lambda *_: SimpleNamespace(ack_floor=SimpleNamespace(stream_seq=next(floors)))
                    ),
                )
                client = Mock(
                    jetstream=Mock(return_value=jetstream), request=AsyncMock(return_value=response), close=AsyncMock()
                )
                settings = SimpleNamespace(
                    state_dir=root,
                    bus_url="nats://localhost:4222",
                    bus_credentials_path=credentials,
                    bus_max_attempts=5,
                    bus_backoff_seconds=1,
                )
                index = Mock()
                worker = BusWorker(
                    settings,
                    Mock(),
                    index,
                    Mock(),
                    Mock(is_set=Mock(side_effect=[False] * (4 if resolve else 3) + [True])),
                )
                original = worker.handle

                def hold_after_newer_coverage(db, event, generation, sequence):
                    """Leave one newer local effect unresolved after recording its coverage fact."""
                    outcome = original(db, event, generation, sequence)
                    if hold and sequence == 3:
                        db.execute(
                            "INSERT INTO held (consumer, event_id, key, wire_bytes, released) VALUES (?, ?, ?, ?, 0)",
                            ("service", "019acb8d-c0c0-7000-8000-000000000099", "later", b"{}"),
                        )
                    return outcome

                worker.handle = hold_after_newer_coverage
                with patch("nats.connect", new=AsyncMock(return_value=client)):
                    asyncio.run(worker.session())
                self.assertEqual(index.covered.call_count, 1 if hold and not resolve else 2)
                index.covered.assert_any_call(raw["data"]["covered_until"])
                states = [call.args[0] for call in index.consumer_completeness.call_args_list]
                if resolve:
                    self.assertIn(False, states[:-2])
                    self.assertEqual(states[-2:], [True, False])
                else:
                    self.assertEqual(states[-3:], [False, False, False] if hold else [True, True, False])

    def test_loss_waits_for_new_coverage_ack_floor(self):
        """Replay recovery closes only after a later coverage fact is acknowledged."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials = root / "credentials.json"
            credentials.write_text('{"user":"service","password":"test"}')
            prior = Consumer(root / "github-bus.sqlite", "service", 5)
            prior.observe_stream("old-generation", 1, 0)
            prior.observe_stream("old-generation", 1, 1)
            prior.close()
            created = "2026-11-19T00:00:00Z"
            raw = (FIXTURES / "ingress.coverage.advanced.v1/valid/advanced.json").read_bytes()
            old = json.loads(raw)
            old["occurred_at"] = "2026-11-19T00:03:00Z"
            old["id"] = "019acb8d-c0c0-7000-8000-000000000002"
            message = SimpleNamespace(
                subject="ingress.coverage.advanced",
                data=json.dumps(old).encode(),
                headers={},
                metadata=SimpleNamespace(stream="GITHUB", sequence=SimpleNamespace(stream=1)),
                ack=AsyncMock(),
            )
            new_message = SimpleNamespace(
                subject="ingress.coverage.advanced",
                data=raw,
                headers={},
                metadata=SimpleNamespace(stream="GITHUB", sequence=SimpleNamespace(stream=2)),
                ack=AsyncMock(),
            )
            stream = SimpleNamespace(data=json.dumps({"created": created, "state": {"first_seq": 1}}).encode())
            jetstream = Mock(
                consumer_info=AsyncMock(
                    side_effect=[
                        SimpleNamespace(ack_floor=SimpleNamespace(stream_seq=0)),
                        SimpleNamespace(ack_floor=SimpleNamespace(stream_seq=1)),
                        SimpleNamespace(ack_floor=SimpleNamespace(stream_seq=2)),
                    ]
                ),
                pull_subscribe=AsyncMock(
                    return_value=SimpleNamespace(fetch=AsyncMock(side_effect=[[message], [new_message], []]))
                ),
            )
            client = Mock(
                jetstream=Mock(return_value=jetstream), request=AsyncMock(return_value=stream), close=AsyncMock()
            )
            stopping = Mock(is_set=Mock(side_effect=[False, False, False, True]))
            index = Mock()
            settings = SimpleNamespace(
                state_dir=root,
                bus_url="nats://localhost:4222",
                bus_credentials_path=credentials,
                bus_max_attempts=5,
                bus_backoff_seconds=1,
            )
            worker = BusWorker(settings, Mock(), index, Mock(), stopping)
            worker.replay = Mock(return_value={"requested_at": "2026-11-19T00:04:00Z", "unrecoverable": None})
            with patch("nats.connect", new=AsyncMock(return_value=client)):
                asyncio.run(worker.session())
            worker.replay.assert_called_once_with(("old-generation", 1), created)
            message.ack.assert_awaited_once()
            new_message.ack.assert_awaited_once()
            index.covered.assert_called_once_with(created)
            with sqlite3.connect(root / "github-bus.sqlite") as db:
                self.assertEqual(db.execute("SELECT count(*) FROM losses WHERE resolved_at IS NULL").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
