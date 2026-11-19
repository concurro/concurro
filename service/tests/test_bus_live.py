"""Real JetStream delivery into the service registry and list index."""

import asyncio
import hashlib
import hmac
import json
import os
import socket
import sqlite3
import subprocess
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from wsgiref.simple_server import make_server

import nats
from consumer import Consumer, decode_bus_event
from nats.aio.msg import Msg
from nats.js.errors import NotFoundError

from fixtures import KEY, ServiceFixture

from service.bus import BusWorker
from service.ingress import Ingress
from service.list_index import ListIndex

FIXTURE = Path(__file__).resolve().parents[2] / "contracts/fixtures/github.issue_comment.v1/valid/representative.json"


class BusLiveTests(ServiceFixture, unittest.TestCase):
    """Prove durable bus effects over a disposable NATS container."""

    def setUp(self):
        """Bind a real NATS stream to the service's registry and list index."""
        super().setUp()
        container = subprocess.run(
            ["docker", "run", "--rm", "-d", "-P", "nats:2.15.3", "-js", "-user", "service", "-pass", "test"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.nats_container = container
        self.addCleanup(lambda: subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=True))
        mapped = subprocess.run(
            ["docker", "port", container, "4222/tcp"], capture_output=True, text=True, check=True
        ).stdout.splitlines()[0]
        self.nats_url = f"nats://127.0.0.1:{mapped.rsplit(':', 1)[1]}"
        credentials = Path(self.directory.name) / "nats.json"
        credentials.write_text('{"user":"service","password":"test"}')
        self.bus_settings = SimpleNamespace(
            state_dir=Path(self.directory.name),
            bus_url=self.nats_url,
            bus_credentials_path=credentials,
            bus_max_attempts=3,
            bus_backoff_seconds=1,
        )
        self.start_time = datetime(2026, 11, 19, 10, tzinfo=UTC)
        self.now = [self.start_time.timestamp()]
        self.index = self.open_index()
        self.ingress = Ingress(self.registry, {KEY[0]: 7}, ())
        self.worker = None
        self.addCleanup(self.stop_worker)

    def open_index(self):
        """Open the persistent issue rows with an empty baseline."""
        index = ListIndex(self.directory.name, clock=lambda: self.now[0])
        self.addCleanup(index.close)
        index.start(
            {KEY[0]: 8},
            rows={"issues": {KEY[0]}},
            reader=lambda _: Mock(request=Mock(return_value=[])),
            work=False,
        )
        index.drain()
        return index

    async def publish(self, events):
        """Publish checked fixtures through the real GITHUB stream."""
        for attempt in range(50):
            try:
                client = await nats.connect(self.nats_url, user="service", password="test", connect_timeout=1)
                break
            except OSError:
                if attempt == 49:
                    raise
                await asyncio.sleep(0.1)
        try:
            jetstream = client.jetstream()
            try:
                await jetstream.stream_info("GITHUB")
            except NotFoundError:
                await jetstream.add_stream(name="GITHUB", subjects=["github.>", "ingress.>"])
            for event in events:
                await jetstream.publish(event["type"], json.dumps(event).encode())
        finally:
            await client.close()

    async def replace_stream(self):
        """Recreate GITHUB while preserving the service's stored floor."""
        client = await nats.connect(self.nats_url, user="service", password="test")
        try:
            jetstream = client.jetstream()
            await jetstream.delete_stream("GITHUB")
            await jetstream.add_stream(name="GITHUB", subjects=["github.>", "ingress.>"])
        finally:
            await client.close()

    def github_event(self, delivery, identifier):
        """Return one enrolled comment fact with a distinct delivery and event ID."""
        event = json.loads(FIXTURE.read_text())
        event["id"] = f"c045e324-ee3d-51f1-a8bb-a23bf3d{identifier:05x}"
        event["data"]["repository"]["full_name"] = KEY[0]
        event["data"]["installation_id"] = 7
        event["data"]["issue"]["number"] = KEY[1]
        event["data"]["issue"]["labels"] = [{"name": "concurro"}]
        event["data"]["delivery_id"] = delivery
        return event

    def coverage_event(self, minute, identifier):
        """Return a producer coverage fact at a controlled time."""
        when = (self.start_time + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")
        return {
            "id": f"019acb8d-c0c0-7000-8000-{identifier:012x}",
            "type": "ingress.coverage.advanced",
            "version": 1,
            "kind": "fact",
            "producer": "ingress",
            "occurred_at": when,
            "data": {"covered_until": when},
        }

    def start_worker(self, replay=None):
        """Start the production bus loop on the current persistent files."""
        stopping = threading.Event()
        self.worker = BusWorker(self.bus_settings, self.ingress, self.index, Mock(), stopping)
        if replay is not None:
            self.worker.replay = replay
        self.worker.start()

    def stop_worker(self):
        """Let the bus loop close its SQLite connection before restarting."""
        if self.worker is not None and self.worker.thread.is_alive():
            self.worker.stopping.set()
            self.worker.thread.join(timeout=5)
            self.assertFalse(self.worker.thread.is_alive())

    def wait_for(self, predicate, timeout=10):
        """Wait for a persisted effect without relying on delivery timing."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("bus effect did not arrive")

    def verified(self):
        """Return the issue list's persisted producer coverage time."""
        value = self.index.revisions({KEY[0]})[KEY[0]]["issues"]["verified"]
        return datetime.fromisoformat(value).timestamp() if value is not None else None

    def test_backlog_survives_consumer_restart_once(self):
        """A stopped reader catches up without duplicating task or list receipts."""
        first = self.github_event("bus-first", 6)
        asyncio.run(self.publish([first]))
        self.start_worker()
        self.wait_for(lambda: self.registry.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 1)
        self.stop_worker()

        second = self.github_event("bus-second", 7)
        asyncio.run(self.publish([second, second]))
        self.start_worker()
        self.wait_for(lambda: self.registry.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 2)
        self.wait_for(lambda: self.index.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 2)
        self.assertEqual([item["number"] for item in self.index.rows(KEY[0], "issues")["items"]], [KEY[1]])

    def test_committed_effect_survives_lost_ack_and_writer_retry(self):
        """A lost ack and one list storage error each lead to one durable effect."""
        original_ack = Msg.ack
        ack_failures = [0]

        async def fail_first_ack(message):
            """Drop the first GitHub ack after the service has committed its effect."""
            if message.subject.startswith("github.") and ack_failures[0] == 0:
                ack_failures[0] += 1
                raise ConnectionError("ack lost after commit")
            await original_ack(message)

        with patch.object(Msg, "ack", fail_first_ack):
            asyncio.run(self.publish([self.github_event("ack-lost", 601)]))
            self.start_worker()
            self.wait_for(lambda: ack_failures[0] == 1)
            self.wait_for(lambda: self.registry.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 1)
            operator = Consumer(Path(self.directory.name) / "github-bus.sqlite", "service", 3, decode_bus_event)
            self.addCleanup(operator.close)
            self.wait_for(
                lambda: (
                    operator.db.execute("SELECT floor_sequence FROM positions WHERE consumer = 'service'").fetchone()[0]
                    >= 1
                ),
                timeout=45,
            )
            self.assertEqual(self.index.db.execute("SELECT count(*) FROM receipts").fetchone()[0], 1)

        original_transaction = self.index.transaction
        writer_failures = [0]

        def fail_first_write():
            """Raise before the list receipt transaction starts on one delivery."""
            if writer_failures[0] == 0:
                writer_failures[0] += 1
                raise sqlite3.OperationalError("temporary list writer error")
            return original_transaction()

        with patch.object(self.index, "transaction", fail_first_write):
            asyncio.run(self.publish([self.github_event("writer-retry", 602)]))
            self.wait_for(lambda: writer_failures[0] == 1)
            self.wait_for(lambda: self.index.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 2)
        self.assertEqual(self.registry.db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 2)
        self.assertEqual(operator.db.execute("SELECT count(*) FROM consumed").fetchone()[0], 2)
        [task] = self.registry.due(self.clock())
        self.assertEqual(task.causes, ("webhook:issue_comment",))

    def test_idle_coverage_and_restart_backlog_freshness(self):
        """Periodic coverage keeps an idle list current; restart waits for a new fact."""
        asyncio.run(self.publish([]))
        self.start_worker()
        for minute in range(0, 61, 5):
            self.now[0] = (self.start_time + timedelta(minutes=minute)).timestamp()
            asyncio.run(self.publish([self.coverage_event(minute, minute + 1)]))
            self.wait_for(lambda: self.verified() is not None and self.verified() >= self.now[0])
            self.assertEqual(self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"], "current")
        self.stop_worker()
        self.index.close()
        self.index = self.open_index()
        self.assertEqual(self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"], "catching-up")
        asyncio.run(self.publish([self.github_event("idle-backlog", 8)]))
        self.start_worker()
        self.wait_for(lambda: self.registry.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 1)
        self.assertEqual(self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"], "catching-up")
        self.now[0] = (self.start_time + timedelta(minutes=65)).timestamp()
        asyncio.run(self.publish([self.coverage_event(65, 99)]))
        self.wait_for(lambda: self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"] == "current")
        self.assertEqual([item["number"] for item in self.index.rows(KEY[0], "issues")["items"]], [KEY[1]])

    def test_parked_effect_invalidates_current_until_operator_replay(self):
        """A known blocked effect changes list status before the freshness timer expires."""
        self.bus_settings.bus_max_attempts = 1
        asyncio.run(self.publish([self.coverage_event(0, 101)]))
        self.start_worker()
        self.wait_for(lambda: self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"] == "current")
        original_route = self.ingress.route
        self.ingress.route = Mock(side_effect=RuntimeError("temporary registry failure"))
        asyncio.run(self.publish([self.github_event("parked-live", 102)]))
        path = Path(self.directory.name) / "github-bus.sqlite"
        operator = Consumer(path, "service", 1, decode_bus_event)
        self.addCleanup(operator.close)
        self.wait_for(lambda: operator.db.execute("SELECT count(*) FROM parked").fetchone()[0] == 1)
        self.wait_for(lambda: self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"] == "catching-up")
        self.stop_worker()
        asyncio.run(self.replace_stream())

        def replay_parked(floor, target_created):
            """Republish the blocked event and a fresh coverage fact after replacement."""
            asyncio.run(self.publish([self.github_event("parked-live", 102), self.coverage_event(1, 103)]))
            return {
                "requested_at": (self.start_time - timedelta(minutes=1)).isoformat(),
                "unrecoverable": None,
            }

        self.start_worker(replay=Mock(side_effect=replay_parked))
        self.wait_for(
            lambda: (
                operator.db.execute("SELECT stream_sequence FROM coverage_fact WHERE consumer = 'service'").fetchone()[
                    0
                ]
                >= 2
            )
        )
        self.assertEqual(self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"], "catching-up")
        self.ingress.route = original_route
        [parking] = operator.parked_command("list")
        operator.parked_command("replay", parking["id"], "operator")
        self.wait_for(lambda: self.registry.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 1)
        self.wait_for(lambda: self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"] == "current")

    def test_recreated_stream_replays_backlog_before_fresh_coverage(self):
        """A lost old-generation delivery is replayed once into a fresh stream."""
        asyncio.run(self.publish([self.github_event("stream-first", 301), self.coverage_event(0, 302)]))
        self.start_worker()
        self.wait_for(lambda: self.registry.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 1)
        self.wait_for(lambda: self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"] == "current")
        self.stop_worker()
        missing = self.github_event("stream-missing", 303)
        asyncio.run(self.publish([missing]))

        asyncio.run(self.replace_stream())
        self.now[0] = (self.start_time + timedelta(minutes=1)).timestamp()

        def replay(floor, target_created):
            """Stand in for ingress after its retained journal selects the lost delivery."""
            asyncio.run(self.publish([missing, self.coverage_event(1, 304)]))
            return {
                "requested_at": (self.start_time - timedelta(minutes=1)).isoformat(),
                "unrecoverable": None,
            }

        requested = Mock(side_effect=replay)
        self.start_worker(replay=requested)
        self.wait_for(lambda: self.registry.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 2)
        self.wait_for(lambda: self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"] == "current")
        requested.assert_called_once()
        self.assertGreaterEqual(requested.call_args.args[0][1], 2)
        self.stop_worker()
        asyncio.run(self.replace_stream())

        def replay_with_pruned_history(floor, target_created):
            """Report a retained-journal boundary after another replacement."""
            asyncio.run(self.publish([self.coverage_event(2, 305)]))
            return {
                "requested_at": (self.start_time - timedelta(minutes=1)).isoformat(),
                "unrecoverable": {"before": "2026-11-19T00:00:00Z"},
            }

        self.start_worker(replay=Mock(side_effect=replay_with_pruned_history))
        self.wait_for(lambda: self.worker.reconciler.poll.call_count == 1)
        self.worker.reconciler.poll.assert_called_once_with(strict=True)

    def test_detected_loss_invalidates_current_before_replay_returns(self):
        """A blocked replay cannot leave an already current list marked current."""
        asyncio.run(self.publish([self.coverage_event(0, 306)]))
        self.start_worker()
        self.wait_for(lambda: self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"] == "current")
        entered, release = threading.Event(), threading.Event()

        def blocked_replay(floor, target_created):
            """Hold ingress recovery after the service observes the new stream."""
            entered.set()
            if not release.wait(5):
                raise TimeoutError("replay release timed out")
            return {
                "requested_at": (self.start_time - timedelta(minutes=1)).isoformat(),
                "unrecoverable": None,
            }

        requested = Mock(side_effect=blocked_replay)
        self.worker.replay = requested
        asyncio.run(self.replace_stream())
        self.wait_for(entered.is_set)
        self.assertEqual(self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"], "catching-up")
        release.set()
        self.wait_for(lambda: requested.call_count == 1)

    def test_initial_and_expired_gaps_reconcile_once_each(self):
        """Each producer gap forces one strict repair and list rebaseline."""
        self.start_worker()
        first = json.loads(
            (
                Path(__file__).resolve().parents[2] / "contracts/fixtures/ingress.coverage.gap.v1/valid/gap.json"
            ).read_text()
        )
        first["data"]["installation_id"] = 7
        first["data"]["from"] = (self.start_time - timedelta(days=3)).isoformat()
        first["data"]["to"] = self.start_time.isoformat()
        first["occurred_at"] = self.start_time.isoformat()
        before = self.index.db.execute(
            "SELECT wanted FROM coverage WHERE repository = ? AND kind = 'issues'", (KEY[0],)
        ).fetchone()[0]
        asyncio.run(self.publish([first, first]))
        path = Path(self.directory.name) / "github-bus.sqlite"
        operator = Consumer(path, "service", 3, decode_bus_event)
        self.addCleanup(operator.close)
        self.wait_for(lambda: operator.db.execute("SELECT count(*) FROM applied_gaps").fetchone()[0] == 1)
        wanted = self.index.db.execute(
            "SELECT wanted FROM coverage WHERE repository = ? AND kind = 'issues'", (KEY[0],)
        ).fetchone()[0]
        self.assertNotEqual(wanted, before)
        self.assertEqual(self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"], "rebuilding")

        second = json.loads(json.dumps(first))
        second["id"] = "019acb8d-c0c0-7000-8000-000000000103"
        second["data"]["from"] = self.start_time.isoformat()
        second["data"]["to"] = (self.start_time + timedelta(days=4)).isoformat()
        second["occurred_at"] = second["data"]["to"]
        self.now[0] = (self.start_time + timedelta(days=4)).timestamp()
        before = wanted
        asyncio.run(self.publish([second]))
        self.wait_for(lambda: operator.db.execute("SELECT count(*) FROM applied_gaps").fetchone()[0] == 2)
        wanted = self.index.db.execute(
            "SELECT wanted FROM coverage WHERE repository = ? AND kind = 'issues'", (KEY[0],)
        ).fetchone()[0]
        self.assertGreater(wanted, before)
        self.assertEqual(self.worker.reconciler.poll.call_count, 2)
        self.worker.reconciler.poll.assert_any_call(strict=True)

    def test_expired_stream_delivery_replays_once(self):
        """A delivery lost beyond max_age is processed once after a replay response."""
        asyncio.run(self.publish([self.github_event("age-first", 401), self.coverage_event(0, 402)]))
        self.start_worker()
        self.wait_for(lambda: self.registry.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 1)
        self.wait_for(lambda: self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"] == "current")
        self.stop_worker()

        async def age(seconds):
            """Change only the test stream's retention clock."""
            client = await nats.connect(self.nats_url, user="service", password="test")
            try:
                await client.jetstream().update_stream(
                    name="GITHUB", subjects=["github.>", "ingress.>"], max_age=seconds
                )
            finally:
                await client.close()

        asyncio.run(age(2))
        missing = self.github_event("age-missing", 403)
        asyncio.run(self.publish([missing]))
        time.sleep(2.3)
        asyncio.run(age(0))
        self.now[0] = (self.start_time + timedelta(minutes=1)).timestamp()

        def replay(floor, target_created):
            """Republish the delivery selected after the expired service floor."""
            asyncio.run(self.publish([missing, self.coverage_event(1, 404)]))
            return {
                "requested_at": (self.start_time - timedelta(minutes=1)).isoformat(),
                "unrecoverable": None,
            }

        requested = Mock(side_effect=replay)
        self.start_worker(replay=requested)
        self.wait_for(lambda: self.registry.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 2)
        self.wait_for(lambda: self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"] == "current")
        requested.assert_called_once()

    def test_rust_ingress_replays_replaced_stream_into_service(self):
        """The real stack recovers stream loss, NATS/PG outages, and a remap."""
        self.start_time = datetime.now(UTC)
        self.now[0] = self.start_time.timestamp()
        self.index.close()
        self.index = self.open_index()
        subprocess.run(["cargo", "build", "-q", "--locked", "-p", "concurro-ingress"], check=True)
        postgres = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "-P",
                "-e",
                "POSTGRES_PASSWORD=test",
                "-e",
                "POSTGRES_DB=concurro_test",
                "postgres:18.7",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.addCleanup(lambda: subprocess.run(["docker", "rm", "-f", postgres], capture_output=True, check=True))
        mapped = subprocess.run(
            ["docker", "port", postgres, "5432/tcp"], capture_output=True, text=True, check=True
        ).stdout.splitlines()[0]
        database_url = f"postgres://postgres:test@127.0.0.1:{mapped.rsplit(':', 1)[1]}/concurro_test"

        def query_postgres(statement):
            """Return a scalar from the disposable ingress database."""
            result = subprocess.run(
                ["docker", "exec", postgres, "psql", "-U", "postgres", "-d", "concurro_test", "-Atc", statement],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()

        for _ in range(100):
            ready = subprocess.run(["docker", "exec", postgres, "pg_isready", "-U", "postgres"], capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(0.1)
        else:
            self.fail("Postgres did not start")
        root = Path(self.directory.name)
        (root / "database-url").write_text(database_url)
        (root / "webhook-secret").write_text("stack-secret")
        (root / "recovery-bearer").write_text("recovery-test")
        (root / "replay-bearer").write_text("replay-test")
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            public_port = candidate.getsockname()[1]
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            internal_port = candidate.getsockname()[1]
        settings = root / "ingress.toml"
        settings.write_text(
            "\n".join(
                [
                    f'public_listen = "127.0.0.1:{public_port}"',
                    f'internal_listen = "127.0.0.1:{internal_port}"',
                    f'database_url_file = "{root / "database-url"}"',
                    f'nats_url = "{self.nats_url}"',
                    f'nats_credentials_file = "{self.bus_settings.bus_credentials_path}"',
                    f'webhook_secret_file = "{root / "webhook-secret"}"',
                    "accepted_installations = [7]",
                    "max_message_bytes = 1000000",
                    "published_retention_seconds = 2592000",
                    "payload_retention_seconds = 2592000",
                    "stream_max_age_seconds = 604800",
                    "duplicate_window_seconds = 120",
                    "forwarder_poll_ms = 20",
                    'delivery_recovery_url = "http://127.0.0.1:1"',
                    f'delivery_recovery_bearer_file = "{root / "recovery-bearer"}"',
                    "recovery_interval_seconds = 300",
                    "[replay_bearer_files]",
                    f'service = "{root / "replay-bearer"}"',
                    "",
                ]
            )
        )
        full_settings = settings.read_text()
        env = dict(os.environ, CONCURRO_INGRESS_CONFIG=str(settings))
        process = None

        def restart_ingress():
            """Start ingress with the current settings and wait for its public listener."""
            nonlocal process
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            with (root / "ingress.log").open("a") as output:
                process = subprocess.Popen(["target/debug/concurro-ingress"], env=env, stdout=output, stderr=output)
            for _ in range(300):
                if process.poll() is not None:
                    self.fail((root / "ingress.log").read_text())
                try:
                    with urlopen(f"http://127.0.0.1:{public_port}/health", timeout=0.2):
                        return
                except OSError:
                    time.sleep(0.1)
            self.fail(
                f"Rust ingress did not start (exit={process.poll()}): {(root / 'ingress.log').read_text()[-3000:]}"
            )

        restart_ingress()
        self.addCleanup(lambda: process.poll() is None and (process.terminate(), process.wait(timeout=5)))
        self.bus_settings.bus_replay_url = f"http://127.0.0.1:{internal_port}"
        self.bus_settings.bus_replay_bearer_path = root / "replay-bearer"
        raw = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "contracts/fixtures/github.issue_comment.v1/raw/representative.json"
            ).read_text()
        )
        raw["repository"]["full_name"] = KEY[0]
        raw["issue"]["number"] = KEY[1]
        raw["issue"]["labels"] = [{"name": "concurro"}]
        self.index.reader = lambda _: Mock(
            request=Mock(side_effect=lambda method, path: raw["issue"] if path.endswith(f"/issues/{KEY[1]}") else [])
        )
        body = json.dumps(raw).encode()
        signature = "sha256=" + hmac.new(b"stack-secret", body, hashlib.sha256).hexdigest()

        def signed_delivery(guid, timeout=5):
            """Send one exact signed body with a distinct GitHub delivery GUID."""
            request = Request(
                f"http://127.0.0.1:{public_port}/webhook",
                data=body,
                headers={
                    "X-Hub-Signature-256": signature,
                    "X-GitHub-Event": "issue_comment",
                    "X-GitHub-Delivery": guid,
                },
                method="POST",
            )
            with urlopen(request, timeout=timeout) as response:
                return response.status

        self.assertEqual(signed_delivery("stack-first"), 202)
        self.assertEqual(query_postgres("SELECT count(*) FROM events.outbox"), "1", (root / "ingress.log").read_text())
        self.start_worker()
        try:
            self.wait_for(lambda: self.registry.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 1)
        except AssertionError:
            self.fail((root / "ingress.log").read_text())
        operator = Consumer(root / "github-bus.sqlite", "service", 3, decode_bus_event)
        self.addCleanup(operator.close)
        self.wait_for(
            lambda: (
                operator.db.execute("SELECT floor_sequence FROM positions WHERE consumer = 'service'").fetchone()[0]
                >= 1
            )
        )
        self.assertEqual(signed_delivery("stack-first"), 200)
        self.assertEqual(self.registry.db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 1)
        self.stop_worker()
        self.assertEqual(signed_delivery("stack-second"), 202)

        async def published_second():
            """Observe the original stream only after ingress's forwarder publishes."""
            client = await nats.connect(self.nats_url, user="service", password="test")
            try:
                return (await client.jetstream().stream_info("GITHUB")).state.last_seq >= 2
            finally:
                await client.close()

        self.wait_for(lambda: asyncio.run(published_second()))
        asyncio.run(self.replace_stream())
        self.start_worker()
        self.wait_for(lambda: self.registry.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 2)
        self.wait_for(lambda: self.index.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 2)
        self.assertEqual(
            query_postgres("SELECT count(*) FROM events.replay_requests WHERE completed_at IS NOT NULL"), "1"
        )
        to = datetime.now(UTC) - timedelta(seconds=1)
        before = to - timedelta(minutes=2)
        subprocess.run(
            [
                "target/debug/concurro-ingress",
                "gap",
                "--from",
                before.isoformat(),
                "--to",
                to.isoformat(),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.wait_for(
            lambda: operator.db.execute("SELECT count(*) FROM losses WHERE resolved_at IS NOT NULL").fetchone()[0] == 1
        )
        self.assertEqual(self.registry.db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 2)
        subprocess.run(["docker", "pause", self.nats_container], capture_output=True, check=True)
        try:
            self.assertEqual(signed_delivery("stack-third"), 202)
            self.assertEqual(query_postgres("SELECT count(*) FROM events.outbox WHERE published_at IS NULL"), "1")
        finally:
            subprocess.run(["docker", "unpause", self.nats_container], capture_output=True, check=True)
        self.wait_for(lambda: self.registry.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 3)

        recovery = {"failed": False, "redeliveries": 0}

        def delivery_api(environ, start_response):
            """List a failed App attempt and redeliver it to the running ingress."""
            if environ["REQUEST_METHOD"] == "GET" and environ["PATH_INFO"] == "/v1/deliveries":
                deliveries = (
                    [
                        {
                            "id": 11,
                            "guid": "stack-recovered",
                            "installation_id": 7,
                            "status": "failed",
                            "status_code": 503,
                        }
                    ]
                    if recovery["failed"]
                    else []
                )
                data = json.dumps({"deliveries": deliveries, "next_cursor": None}).encode()
                start_response("200 OK", [("Content-Type", "application/json"), ("Content-Length", str(len(data)))])
                return [data]
            if environ["REQUEST_METHOD"] == "POST" and environ["PATH_INFO"] == "/v1/deliveries/11/redeliver":
                recovery["redeliveries"] += 1
                status = signed_delivery("stack-recovered", timeout=10)
                start_response(f"{status} Accepted", [("Content-Length", "0")])
                return [b""]
            start_response("404 Not Found", [("Content-Length", "0")])
            return [b""]

        api = make_server("127.0.0.1", 0, delivery_api)
        api_thread = threading.Thread(target=api.serve_forever, daemon=True)
        api_thread.start()
        self.addCleanup(lambda: (api.shutdown(), api.server_close(), api_thread.join(timeout=5)))
        api_settings = full_settings.replace(
            'delivery_recovery_url = "http://127.0.0.1:1"',
            f'delivery_recovery_url = "http://127.0.0.1:{api.server_port}"',
        ).replace("recovery_interval_seconds = 300", "recovery_interval_seconds = 1")
        settings.write_text(api_settings)
        restart_ingress()
        self.now[0] = time.time()
        self.index.drain()
        try:
            self.wait_for(lambda: self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"] == "current")
        except AssertionError:
            self.fail(
                f"index={self.index.revisions({KEY[0]})} "
                f"coverage={query_postgres('SELECT covered_until FROM ingress.coverage WHERE id')} "
                f"log={(root / 'ingress.log').read_text()[-2000:]}"
            )

        wire_size = int(query_postgres("SELECT octet_length(wire_bytes) FROM events.outbox WHERE sequence = 1"))
        small_cap = len(body) + 1
        self.assertLess(small_cap, wire_size)
        settings.write_text(api_settings.replace("max_message_bytes = 1000000", f"max_message_bytes = {small_cap}"))
        restart_ingress()
        self.assertEqual(signed_delivery("stack-oversize"), 202)
        self.assertEqual(
            query_postgres("SELECT outcome || ':' || error FROM ingress.deliveries WHERE guid = 'stack-oversize'"),
            "unmappable:event_too_large",
        )
        self.wait_for(
            lambda: (
                query_postgres(
                    "SELECT started_at > (SELECT received_at FROM ingress.deliveries WHERE guid = 'stack-oversize') "
                    "FROM ingress.recovery_scan WHERE id"
                )
                == "t"
            )
        )
        self.assertEqual(
            query_postgres(
                "SELECT covered_until < (SELECT received_at FROM ingress.deliveries WHERE guid = 'stack-oversize') "
                "FROM ingress.coverage WHERE id"
            ),
            "t",
        )
        self.now[0] += 3600
        self.assertEqual(self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"], "catching-up")
        self.now[0] = time.time()
        settings.write_text(api_settings)
        remap_from = datetime.now(UTC) - timedelta(minutes=5)
        remap_to = datetime.now(UTC) + timedelta(minutes=1)
        subprocess.run(
            [
                "target/debug/concurro-ingress",
                "remap",
                "--from",
                remap_from.isoformat(),
                "--to",
                remap_to.isoformat(),
                "--event",
                "issue_comment",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(
            query_postgres("SELECT outcome FROM ingress.deliveries WHERE guid = 'stack-oversize'"), "published"
        )
        self.wait_for(lambda: self.registry.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 4)
        self.wait_for(lambda: self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"] == "current")

        restart_ingress()
        subprocess.run(["docker", "stop", postgres], capture_output=True, check=True)
        with self.assertRaises(HTTPError) as unavailable:
            signed_delivery("stack-recovered", timeout=40)
        self.assertEqual(unavailable.exception.code, 503)
        process.terminate()
        process.wait(timeout=5)
        recovery["failed"] = True
        subprocess.run(["docker", "start", postgres], capture_output=True, check=True)
        for _ in range(100):
            ready = subprocess.run(["docker", "exec", postgres, "pg_isready", "-U", "postgres"], capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(0.1)
        else:
            self.fail("Postgres did not restart")
        remapped = subprocess.run(
            ["docker", "port", postgres, "5432/tcp"], capture_output=True, text=True, check=True
        ).stdout.splitlines()[0]
        (root / "database-url").write_text(
            f"postgres://postgres:test@127.0.0.1:{remapped.rsplit(':', 1)[1]}/concurro_test"
        )
        restart_ingress()
        self.wait_for(lambda: recovery["redeliveries"] == 1)
        self.wait_for(lambda: self.registry.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 5)
        self.assertEqual(query_postgres("SELECT count(*) FROM ingress.deliveries WHERE guid = 'stack-recovered'"), "1")
        time.sleep(1.2)
        self.assertEqual(recovery["redeliveries"], 1)
        self.assertEqual(self.registry.db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 5)
