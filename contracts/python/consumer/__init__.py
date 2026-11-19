"""SQLite reference implementation of Concurro's durable event consumer."""

from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from enum import Enum
from pathlib import Path

from concurro_contracts.github_issues_v1 import Envelope
from concurro_contracts.validation import (
    UnsupportedVersion,
    decode_coverage_advanced,
    decode_coverage_gap,
    decode_github_issues,
    decode_github_issue_comment,
    decode_github_pull_request,
    decode_github_pull_request_review,
    decode_github_pull_request_review_comment,
    decode_github_pull_request_review_thread,
    decode_github_push,
    decode_github_status,
    decode_github_check_run,
    decode_github_check_suite,
    decode_github_workflow_run,
)
from nats.aio.msg import Msg
from nats.errors import TimeoutError as NatsTimeoutError


class Outcome(Enum):
    """A handler's decision for one event."""

    DONE = "done"
    RETRY = "retry"
    HOLD = "hold"
    REJECT = "reject"


class Action(Enum):
    """The transport response to a durable consumer decision."""

    ACK = "ack"
    NAK = "nak"
    TERM = "term"


Handler = Callable[[sqlite3.Connection, Envelope], tuple[Outcome, str]]

GITHUB_DECODERS = {
    "github.issues": decode_github_issues,
    "github.issue_comment": decode_github_issue_comment,
    "github.pull_request": decode_github_pull_request,
    "github.pull_request_review": decode_github_pull_request_review,
    "github.pull_request_review_comment": decode_github_pull_request_review_comment,
    "github.pull_request_review_thread": decode_github_pull_request_review_thread,
    "github.push": decode_github_push,
    "github.status": decode_github_status,
    "github.check_run": decode_github_check_run,
    "github.check_suite": decode_github_check_suite,
    "github.workflow_run": decode_github_workflow_run,
}


async def stream_identity(client, name: str) -> tuple[str, int]:
    """Read the exact broker creation timestamp and first retained sequence."""
    response = await client.request(f"$JS.API.STREAM.INFO.{name}", timeout=5)
    data = json.loads(response.data)
    if "error" in data:
        raise RuntimeError(f"stream info failed: {data['error']}")
    return data["created"], data["state"]["first_seq"]


async def inspect_consumer(client, jetstream, stream_name: str, durable: str) -> tuple[str, int, int]:
    """Return a consumer floor only when both stream samples name one incarnation."""
    before = await stream_identity(client, stream_name)
    info = await jetstream.consumer_info(stream_name, durable)
    after = await stream_identity(client, stream_name)
    if before[0] != after[0]:
        raise RuntimeError("stream incarnation changed during consumer inspection")
    return after[0], after[1], info.ack_floor.stream_seq if info.ack_floor else 0


def delivery_key(message: Msg, stream_created: str) -> str:
    """Return one identity stable across JetStream redeliveries."""
    header = (message.headers or {}).get("Nats-Msg-Id")
    if header:
        return header
    metadata = message.metadata
    return f"{metadata.stream}:{stream_created}:{metadata.sequence.stream}"


def decode_bus_event(raw: bytes):
    """Select a checked GitHub or ingress decoder by the envelope type."""
    header = json.loads(raw)
    if not isinstance(header, dict):
        raise ValueError("event envelope must be an object")
    event_type = header.get("type")
    if isinstance(event_type, str) and event_type.startswith("github."):
        family = ".".join(event_type.split(".")[:2])
        decoder = GITHUB_DECODERS.get(family)
        if decoder is not None:
            return decoder(raw)
    if event_type == "ingress.coverage.advanced":
        return decode_coverage_advanced(raw)
    if event_type == "ingress.coverage.gap":
        return decode_coverage_gap(raw)
    raise UnsupportedVersion(f"unsupported event type: {event_type!r}")


class Consumer:
    """Apply events once, with attempts and parking durable across restarts."""

    def __init__(
        self, path: str | Path, name: str, max_attempts: int, decoder=decode_github_issues, *, backoff_seconds: int = 1
    ):
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.name = name
        self.max_attempts = max_attempts
        self.decoder = decoder
        self.backoff_seconds = backoff_seconds
        self.retry_delay = 1.0
        database_path = Path(path).resolve()
        self.lock_path = database_path.with_name(database_path.name + ".lock")
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS consumed (
                consumer TEXT NOT NULL, event_id TEXT NOT NULL,
                disposition TEXT NOT NULL, PRIMARY KEY (consumer, event_id)
            );
            CREATE TABLE IF NOT EXISTS attempts (
                consumer TEXT NOT NULL, event_id TEXT NOT NULL,
                count INTEGER NOT NULL, last_error TEXT,
                PRIMARY KEY (consumer, event_id)
            );
            CREATE TABLE IF NOT EXISTS identity_aliases (
                consumer TEXT NOT NULL, message_key TEXT NOT NULL,
                event_id TEXT NOT NULL, PRIMARY KEY (consumer, message_key)
            );
            CREATE TABLE IF NOT EXISTS held (
                consumer TEXT NOT NULL, event_id TEXT NOT NULL,
                key TEXT NOT NULL, wire_bytes BLOB NOT NULL,
                released INTEGER NOT NULL DEFAULT 0, retry_at REAL,
                PRIMARY KEY (consumer, event_id)
            );
            CREATE TABLE IF NOT EXISTS parked (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                consumer TEXT NOT NULL, event_id TEXT NOT NULL,
                wire_bytes BLOB NOT NULL, reason TEXT NOT NULL,
                parked_at REAL NOT NULL, replayed_at REAL, replayed_by TEXT,
                discarded_at REAL, discarded_by TEXT, discard_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS positions (
                consumer TEXT PRIMARY KEY, stream_created TEXT NOT NULL,
                floor_sequence INTEGER NOT NULL, observed_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS losses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                consumer TEXT NOT NULL, floor_created TEXT NOT NULL,
                floor_sequence INTEGER NOT NULL, detected_at REAL NOT NULL,
                resolved_at REAL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS open_loss ON losses (consumer)
                WHERE resolved_at IS NULL;
            CREATE TABLE IF NOT EXISTS applied_gaps (
                consumer TEXT NOT NULL, event_id TEXT NOT NULL,
                PRIMARY KEY (consumer, event_id)
            );
            """
        )

    def close(self) -> None:
        """Close the consumer's SQLite connection."""
        self.db.close()

    def process(self, raw: bytes, message_key: str, handler: Handler, *, released: bool = False) -> Action:
        """Decide and durably apply one delivery or released held row."""
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                return self._process_locked(raw, message_key, handler, released=released)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _process_locked(self, raw: bytes, message_key: str, handler: Handler, *, released: bool) -> Action:
        """Hold the cross-process lock across attempt and handler transactions."""
        if not released and (len(message_key.encode()) > 512 or "\0" in message_key or message_key.startswith("sha256:")):
            message_key = "sha256:" + hashlib.sha256(message_key.encode()).hexdigest()
        try:
            event = self.decoder(raw)
            unsupported = None
        except UnsupportedVersion as error:
            event = None
            unsupported = str(error)
        except (ValueError, RecursionError, OverflowError):
            event = None
            unsupported = None
        try:
            header = json.loads(raw)
            raw_id = header.get("id") if isinstance(header, dict) else None
            candidate = str(uuid.UUID(raw_id)) if isinstance(raw_id, str) else None
        except (UnicodeDecodeError, ValueError, RecursionError, OverflowError):
            candidate = None
        if candidate is None:
            candidate = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{self.name}:raw:{message_key}"))

        self.db.execute("BEGIN IMMEDIATE")
        if released:
            event_id = message_key
            known_alias = True
        else:
            alias = self.db.execute(
                "SELECT event_id FROM identity_aliases WHERE consumer = ? AND message_key = ?",
                (self.name, message_key),
            ).fetchone()
            known_alias = alias is not None
            event_id = alias[0] if alias else candidate
            if alias is None and unsupported is None:
                self.db.execute(
                    "INSERT INTO identity_aliases VALUES (?, ?, ?)",
                    (self.name, message_key, event_id),
                )
        if self.db.execute(
            "SELECT 1 FROM consumed WHERE consumer = ? AND event_id = ?",
            (self.name, event_id),
        ).fetchone():
            if released:
                self.db.execute(
                    "DELETE FROM held WHERE consumer = ? AND event_id = ?",
                    (self.name, event_id),
                )
                self.db.execute("COMMIT")
            else:
                self.db.execute("ROLLBACK")
            return Action.ACK
        held = self.db.execute(
            "SELECT released, retry_at, wire_bytes = ? FROM held WHERE consumer = ? AND event_id = ?",
            (raw, self.name, event_id),
        ).fetchone()
        if unsupported is not None:
            if released:
                owned = bool(held and held[0] and held[2] and (held[1] is None or held[1] <= time.time()))
            else:
                owned = known_alias and held is None and bool(
                    self.db.execute(
                        "SELECT 1 FROM attempts WHERE consumer = ? AND event_id = ?",
                        (self.name, event_id),
                    ).fetchone()
                )
            self.db.execute("ROLLBACK")
            if owned:
                self._park(event_id, raw, f"unsupported contract version: {unsupported}")
                return Action.TERM
            return Action.ACK
        if (held is not None and not released) or (
            released and (held is None or not held[0] or (held[1] is not None and held[1] > time.time()))
        ):
            self.db.execute("ROLLBACK")
            return Action.ACK
        self.db.execute(
            "INSERT INTO attempts (consumer, event_id, count) VALUES (?, ?, 1) "
            "ON CONFLICT (consumer, event_id) DO UPDATE SET count = count + 1",
            (self.name, event_id),
        )
        count = self.db.execute(
            "SELECT count FROM attempts WHERE consumer = ? AND event_id = ?",
            (self.name, event_id),
        ).fetchone()[0]
        self.db.execute("COMMIT")
        if count > self.max_attempts or event is None:
            if count > self.max_attempts:
                self._park(event_id, raw, "attempt limit reached")
                return Action.TERM
            return self._retry(event_id, "invalid event envelope", count, released)
        return self._apply(event_id, raw, event, handler, count, released)

    def _apply(
        self,
        event_id: str,
        raw: bytes,
        event: Envelope,
        handler: Handler,
        count: int,
        released: bool,
    ) -> Action:
        """Commit the effect and dedup row in one transaction."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("INSERT INTO consumed VALUES (?, ?, 'applied')", (self.name, event_id))
            outcome, detail = handler(self.db, event)
            if outcome is Outcome.DONE:
                if released:
                    self.db.execute(
                        "DELETE FROM held WHERE consumer = ? AND event_id = ?",
                        (self.name, event_id),
                    )
                self.db.execute("COMMIT")
                return Action.ACK
        except Exception as error:
            self.db.execute("ROLLBACK")
            return self._retry(event_id, str(error), count, released)
        self.db.execute("ROLLBACK")
        if outcome is Outcome.RETRY:
            return self._retry(event_id, detail, count, released)
        if outcome is Outcome.REJECT:
            self._park(event_id, raw, detail)
            return Action.TERM
        if outcome is Outcome.HOLD:
            return self._confirm_hold(event_id, raw, event, handler, detail, count, released)
        raise ValueError(f"unknown handler outcome: {outcome}")

    def _confirm_hold(
        self,
        event_id: str,
        raw: bytes,
        event: Envelope,
        handler: Handler,
        key: str,
        count: int,
        released: bool,
    ) -> Action:
        """Recheck a prerequisite after a concurrent release can commit."""
        while True:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("SAVEPOINT confirming")
            try:
                self.db.execute("INSERT INTO consumed VALUES (?, ?, 'applied')", (self.name, event_id))
                outcome, detail = handler(self.db, event)
                if outcome is Outcome.DONE:
                    self.db.execute(
                        "DELETE FROM held WHERE consumer = ? AND event_id = ?",
                        (self.name, event_id),
                    )
                    self.db.execute("COMMIT")
                    return Action.ACK
            except Exception as error:
                self.db.execute("ROLLBACK TO confirming")
                self.db.execute("COMMIT")
                return self._retry(event_id, str(error), count, released)
            self.db.execute("ROLLBACK TO confirming")
            if outcome is Outcome.HOLD:
                if detail != key:
                    key = detail
                    self.db.execute("COMMIT")
                    continue
                self.db.execute(
                    "INSERT INTO held (consumer, event_id, key, wire_bytes) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (consumer, event_id) DO UPDATE SET key = excluded.key, "
                    "wire_bytes = excluded.wire_bytes, released = 0, retry_at = NULL",
                    (self.name, event_id, key, raw),
                )
                self.db.execute(
                    "DELETE FROM attempts WHERE consumer = ? AND event_id = ?",
                    (self.name, event_id),
                )
                self.db.execute("COMMIT")
                return Action.ACK
            self.db.execute("COMMIT")
            if outcome is Outcome.REJECT:
                self._park(event_id, raw, detail)
                return Action.TERM
            return self._retry(event_id, detail, count, released)

    def _retry(self, event_id: str, reason: str, count: int, released: bool) -> Action:
        """Keep the attempt and schedule another delivery or held re-run."""
        delay = self.backoff_seconds * min(2 ** min(count - 1, 10), 1024)
        self.retry_delay = float(delay)
        self.db.execute("BEGIN IMMEDIATE")
        self.db.execute(
            "UPDATE attempts SET last_error = ? WHERE consumer = ? AND event_id = ?",
            (reason, self.name, event_id),
        )
        if released:
            self.db.execute(
                "UPDATE held SET retry_at = ? WHERE consumer = ? AND event_id = ?",
                (time.time() + delay, self.name, event_id),
            )
        self.db.execute("COMMIT")
        return Action.NAK

    def _park(self, event_id: str, raw: bytes, reason: str) -> None:
        """Mark an event handled and retain its failed delivery for an operator."""
        self.db.execute("BEGIN IMMEDIATE")
        if not self.db.execute(
            "SELECT 1 FROM consumed WHERE consumer = ? AND event_id = ?",
            (self.name, event_id),
        ).fetchone():
            self.db.execute("INSERT INTO consumed VALUES (?, ?, 'parked')", (self.name, event_id))
            self.db.execute(
                "INSERT INTO parked (consumer, event_id, wire_bytes, reason, parked_at) VALUES (?, ?, ?, ?, ?)",
                (self.name, event_id, raw, reason, time.time()),
            )
        self.db.execute(
            "DELETE FROM held WHERE consumer = ? AND event_id = ?",
            (self.name, event_id),
        )
        self.db.execute("COMMIT")

    def release(self, key: str) -> None:
        """Release held rows inside the caller's handler transaction."""
        self.db.execute(
            "UPDATE held SET released = 1, retry_at = NULL WHERE consumer = ? AND key = ? AND released = 0",
            (self.name, key),
        )

    def observe_stream(self, created: str, first_sequence: int, ack_floor: int) -> tuple[str, int] | None:
        """Persist a continuous floor or return the old floor of an open loss."""
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                self.db.execute("BEGIN IMMEDIATE")
                prior = self.db.execute(
                    "SELECT stream_created, floor_sequence FROM positions WHERE consumer = ?",
                    (self.name,),
                ).fetchone()
                open_loss = self.db.execute(
                    "SELECT 1 FROM losses WHERE consumer = ? AND resolved_at IS NULL",
                    (self.name,),
                ).fetchone()
                if prior is None:
                    self.db.execute(
                        "INSERT INTO positions VALUES (?, ?, ?, ?)",
                        (self.name, created, max(0, first_sequence - 1), time.time()),
                    )
                    self.db.execute("COMMIT")
                    return None
                if open_loss or prior[0] != created or first_sequence > prior[1] + 1:
                    self.db.execute(
                        "INSERT OR IGNORE INTO losses "
                        "(consumer, floor_created, floor_sequence, detected_at) VALUES (?, ?, ?, ?)",
                        (self.name, prior[0], prior[1], time.time()),
                    )
                    self.db.execute("COMMIT")
                    return prior
                self.db.execute(
                    "UPDATE positions SET floor_sequence = ?, observed_at = ? WHERE consumer = ?",
                    (max(prior[1], ack_floor), time.time(), self.name),
                )
                self.db.execute("COMMIT")
                return None
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def resolve_loss(self, created: str, floor: int) -> None:
        """Record recovery after replay and strict reconciliation have finished."""
        self.db.execute("BEGIN IMMEDIATE")
        self.db.execute(
            "UPDATE losses SET resolved_at = ? WHERE consumer = ? AND resolved_at IS NULL",
            (time.time(), self.name),
        )
        self.db.execute(
            "INSERT INTO positions VALUES (?, ?, ?, ?) ON CONFLICT(consumer) DO UPDATE SET "
            "stream_created = excluded.stream_created, floor_sequence = excluded.floor_sequence, "
            "observed_at = excluded.observed_at",
            (self.name, created, floor, time.time()),
        )
        self.db.execute("COMMIT")

    def coverage_applied(self, created: str, sequence: int) -> bool:
        """Whether a coverage fact is acknowledged and no local effect remains blocked."""
        position = self.db.execute(
            "SELECT stream_created, floor_sequence FROM positions WHERE consumer = ?",
            (self.name,),
        ).fetchone()
        if position is None or position[0] != created or position[1] < sequence:
            return False
        for table, condition in (
            ("held", ""),
            (
                "parked",
                "AND discarded_at IS NULL AND (replayed_at IS NULL OR NOT EXISTS "
                "(SELECT 1 FROM consumed c WHERE c.consumer = parked.consumer "
                "AND c.event_id = parked.event_id AND c.disposition = 'applied') "
                "AND NOT EXISTS (SELECT 1 FROM parked newer WHERE newer.consumer = parked.consumer "
                "AND newer.event_id = parked.event_id AND newer.id > parked.id))",
            ),
            ("losses", "AND resolved_at IS NULL"),
        ):
            if self.db.execute(
                f"SELECT 1 FROM {table} WHERE consumer = ? {condition} LIMIT 1",
                (self.name,),
            ).fetchone():
                return False
        return True

    def parked_command(
        self, action: str, parking_id: int | None = None, operator: str | None = None, reason: str | None = None
    ):
        """List, inspect, replay or discard a parked event under the consumer lock."""
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if action == "list":
                    rows = self.db.execute(
                        "SELECT id, event_id, reason, parked_at FROM parked WHERE consumer = ? "
                        "AND discarded_at IS NULL AND (replayed_at IS NULL OR NOT EXISTS "
                        "(SELECT 1 FROM consumed c WHERE c.consumer = parked.consumer "
                        "AND c.event_id = parked.event_id AND c.disposition = 'applied') "
                        "AND NOT EXISTS (SELECT 1 FROM parked newer WHERE newer.consumer = parked.consumer "
                        "AND newer.event_id = parked.event_id AND newer.id > parked.id)) "
                        "ORDER BY id",
                        (self.name,),
                    ).fetchall()
                    return [dict(zip(("id", "event_id", "reason", "parked_at"), row)) for row in rows]
                row = self.db.execute(
                    "SELECT id, event_id, wire_bytes, reason, parked_at, replayed_at, "
                    "replayed_by, discarded_at, discarded_by, discard_reason FROM parked "
                    "WHERE consumer = ? AND id = ?",
                    (self.name, parking_id),
                ).fetchone()
                if row is None:
                    raise ValueError("parking not found")
                fields = (
                    "id",
                    "event_id",
                    "wire_bytes",
                    "reason",
                    "parked_at",
                    "replayed_at",
                    "replayed_by",
                    "discarded_at",
                    "discarded_by",
                    "discard_reason",
                )
                detail = dict(zip(fields, row))
                if action == "show":
                    wire = detail.pop("wire_bytes")
                    try:
                        detail["wire_message"] = json.loads(wire)
                    except (UnicodeDecodeError, ValueError, RecursionError, OverflowError):
                        detail["wire_message"] = wire.decode(errors="replace")
                    return detail
                if action not in ("replay", "discard") or not operator:
                    raise ValueError("invalid parked command")
                if row[5] is not None or row[7] is not None:
                    raise ValueError("parking already resolved")
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    if action == "replay":
                        self.db.execute(
                            "UPDATE parked SET replayed_at = ?, replayed_by = ? WHERE id = ?",
                            (time.time(), operator, parking_id),
                        )
                        self.db.execute(
                            "DELETE FROM consumed WHERE consumer = ? AND event_id = ?",
                            (self.name, row[1]),
                        )
                        self.db.execute(
                            "DELETE FROM attempts WHERE consumer = ? AND event_id = ?",
                            (self.name, row[1]),
                        )
                        self.db.execute(
                            "INSERT INTO held (consumer, event_id, key, wire_bytes, released) "
                            "VALUES (?, ?, ?, ?, 1) ON CONFLICT(consumer, event_id) DO UPDATE SET "
                            "wire_bytes = excluded.wire_bytes, released = 1, retry_at = NULL",
                            (self.name, row[1], row[1], row[2]),
                        )
                    else:
                        if not reason:
                            raise ValueError("discard reason required")
                        self.db.execute(
                            "UPDATE parked SET discarded_at = ?, discarded_by = ?, discard_reason = ? WHERE id = ?",
                            (time.time(), operator, reason, parking_id),
                        )
                    self.db.execute("COMMIT")
                except BaseException:
                    self.db.execute("ROLLBACK")
                    raise
                return {"id": parking_id, "action": action, "operator": operator}
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def process_released(self, handler: Handler) -> Action | None:
        """Run one released held row whose retry time has arrived."""
        row = self.db.execute(
            "SELECT event_id, wire_bytes FROM held WHERE consumer = ? AND released = 1 "
            "AND (retry_at IS NULL OR retry_at <= ?) ORDER BY event_id LIMIT 1",
            (self.name, time.time()),
        ).fetchone()
        if row is None:
            return None
        return self.process(row[1], row[0], handler, released=True)

    async def run(self, subscription: object, handler: Handler, stream_created: str) -> None:
        """Fetch JetStream messages and acknowledge only after durable decisions."""
        while True:
            try:
                messages = await subscription.fetch(1, timeout=1)
            except NatsTimeoutError:
                messages = []
            for message in messages:
                key = delivery_key(message, stream_created)
                action = self.process(message.data, key, handler)
                if action is Action.ACK:
                    await message.ack()
                elif action is Action.TERM:
                    await message.term()
                else:
                    await message.nak(delay=self.retry_delay)
            while self.process_released(handler) is not None:
                pass
