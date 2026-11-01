"""The service's only durable state: one exclusive writer, synchronous commits, fail closed."""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

from controller import Outcome

from .errors import RegistryError, RegistryFailure

SCHEMA_VERSION = 1
DATABASE = "registry.db"
LOCK = "lock"
STATUSES = ("in-flight", "done", "failed", "unknown")
DISPOSITIONS = {"applied": "done", "not-applied": "failed"}
SCHEMA = """
CREATE TABLE meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE deliveries (id TEXT PRIMARY KEY, received REAL NOT NULL);
CREATE TABLE queue (repository TEXT NOT NULL, issue INTEGER NOT NULL, causes TEXT NOT NULL,
                    due_at REAL NOT NULL, PRIMARY KEY (repository, issue));
CREATE TABLE ownership (repository TEXT NOT NULL, issue INTEGER NOT NULL, generation INTEGER NOT NULL,
                        worker TEXT NOT NULL, lease_until REAL NOT NULL, live INTEGER NOT NULL,
                        PRIMARY KEY (repository, issue));
CREATE TABLE mutations (intent TEXT PRIMARY KEY, repository TEXT NOT NULL, issue INTEGER NOT NULL,
                        generation INTEGER NOT NULL, sequence INTEGER NOT NULL, method TEXT NOT NULL,
                        path TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL, admitted REAL NOT NULL,
                        settled REAL, resolution TEXT, UNIQUE (repository, issue, generation, sequence));
CREATE TABLE snapshots (repository TEXT NOT NULL, issue INTEGER NOT NULL, snapshot TEXT NOT NULL,
                        PRIMARY KEY (repository, issue));
CREATE TABLE outcomes (repository TEXT NOT NULL, issue INTEGER NOT NULL, kind TEXT NOT NULL,
                       progressed INTEGER NOT NULL, retry_after INTEGER, recorded REAL NOT NULL,
                       PRIMARY KEY (repository, issue));
"""


@dataclass(frozen=True)
class WorkItem:
    """Due work for one task; several causes collapse into one item."""

    key: tuple
    causes: tuple
    due_at: float


@dataclass(frozen=True)
class Ownership:
    """The latest generation granted for a task."""

    generation: int
    worker: str
    lease_until: float
    live: bool


@dataclass(frozen=True)
class MutationRecord:
    """One write occurrence, recorded before it was sent."""

    intent: str
    key: tuple
    generation: int
    sequence: int
    method: str
    path: str
    digest: str
    status: str
    admitted: float
    settled: float | None
    resolution: dict | None


def queue(db, key, cause, due_at):
    """Merge one cause into a task's work item within the caller's transaction."""
    row = db.execute("SELECT causes, due_at FROM queue WHERE repository = ? AND issue = ?", key).fetchone()
    causes = sorted(set(json.loads(row[0]) if row else ()) | {cause})
    db.execute("INSERT OR REPLACE INTO queue VALUES (?, ?, ?, ?)",
               (*key, json.dumps(causes), min(row[1], due_at) if row else due_at))


class Registry:
    """Local transactional store; open it only through `open`."""

    def __init__(self, db, lock_fd, clock):
        """Hold the connection and the state directory's exclusive lock."""
        self.db, self.lock_fd, self.clock = db, lock_fd, clock
        self.guard = threading.RLock()
        self.failed = False

    @classmethod
    def open(cls, state_dir, registry_id, initialize=False, clock=time.time):
        """Lock the state directory and verify the store; never replace lost state silently."""
        root = Path(state_dir)
        if not root.is_dir() or root.stat().st_mode & 0o077:
            raise RegistryError("state directory must exist and be private to its owner")
        fd = os.open(root / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise RegistryError("state directory is in use by another process") from None
        path = root / DATABASE
        db = None
        try:
            if initialize:
                if path.exists():
                    raise RegistryError("registry already exists; refusing to initialize over it")
                os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            elif not path.exists() or not path.stat().st_size:
                raise RegistryError("registry is missing or empty; initialize a new one explicitly")
            db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            for pragma in ("journal_mode=WAL", "synchronous=FULL", "fullfsync=ON", "checkpoint_fullfsync=ON"):
                db.execute("PRAGMA " + pragma)
            if initialize:
                db.execute("BEGIN IMMEDIATE")
                for statement in filter(str.strip, SCHEMA.split(";")):
                    db.execute(statement)
                db.executemany("INSERT INTO meta VALUES (?, ?)",
                               [("registry_id", registry_id), ("schema", str(SCHEMA_VERSION))])
                db.execute("COMMIT")
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RegistryError("registry integrity check failed")
            meta = dict(db.execute("SELECT name, value FROM meta").fetchall())
            if meta != {"registry_id": registry_id, "schema": str(SCHEMA_VERSION)}:
                raise RegistryError("registry identity or schema version mismatch")
        except (RegistryError, sqlite3.Error) as error:
            if db is not None:
                db.close()
            os.close(fd)
            if isinstance(error, RegistryError):
                raise
            raise RegistryError("registry is unreadable") from None
        registry = cls(db, fd, clock)
        registry.recover()
        return registry

    def close(self):
        """Release the store and the state directory lock; closing twice is harmless."""
        with self.guard:
            if self.lock_fd is not None:
                self.db.close()
                os.close(self.lock_fd)
                self.lock_fd = None

    @contextmanager
    def transaction(self):
        """Run one durable transaction; any storage failure stops all further use."""
        with self.guard:
            if self.failed:
                raise RegistryFailure("registry unavailable")
            try:
                self.db.execute("BEGIN IMMEDIATE")
                yield self.db
                self.db.execute("COMMIT")
            except sqlite3.Error:
                self.failed = True
                raise RegistryFailure("registry storage failed") from None
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def recover(self):
        """After a restart, interrupted writes are unknown and interrupted ticks are due again."""
        now = self.clock()
        with self.transaction() as db:
            db.execute("UPDATE mutations SET status = 'unknown', settled = ? WHERE status = 'in-flight'", (now,))
            for key in db.execute("SELECT repository, issue FROM ownership WHERE live = 1").fetchall():
                queue(db, tuple(key), "restart", now)
            db.execute("UPDATE ownership SET live = 0 WHERE live = 1")

    # Delivery de-duplication

    def seen(self, delivery_id):
        """Record a delivery; return whether it had already been recorded."""
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM deliveries WHERE id = ?", (delivery_id,)).fetchone():
                return True
            db.execute("INSERT INTO deliveries VALUES (?, ?)", (delivery_id, self.clock()))
            return False

    # Queue

    def enqueue(self, key, cause, due_at):
        """Add a cause to the task's work item, keeping its earliest due time."""
        with self.transaction() as db:
            queue(db, key, cause, due_at)

    def due(self, now):
        """Work items due by `now`, oldest first."""
        with self.transaction() as db:
            rows = db.execute("SELECT repository, issue, causes, due_at FROM queue WHERE due_at <= ? "
                              "ORDER BY due_at, repository, issue", (now,)).fetchall()
        return [WorkItem((r[0], r[1]), tuple(json.loads(r[2])), r[3]) for r in rows]

    # Ownership and mutations: called only inside the gateway's critical section

    def grant(self, key, worker, lease_seconds):
        """Grant the next generation and consume the task's work item in the same commit."""
        with self.transaction() as db:
            row = db.execute("SELECT generation FROM ownership WHERE repository = ? AND issue = ?", key).fetchone()
            generation = (row[0] if row else 0) + 1
            db.execute("INSERT OR REPLACE INTO ownership VALUES (?, ?, ?, ?, ?, 1)",
                       (*key, generation, worker, self.clock() + lease_seconds))
            db.execute("DELETE FROM queue WHERE repository = ? AND issue = ?", key)
        return generation

    def current(self, key):
        """The task's latest generation, live or ended."""
        with self.transaction() as db:
            row = db.execute("SELECT generation, worker, lease_until, live FROM ownership "
                             "WHERE repository = ? AND issue = ?", key).fetchone()
        return Ownership(row[0], row[1], row[2], bool(row[3])) if row else None

    def renew(self, key, generation, lease_seconds):
        """Extend a live generation's lease; return whether it is still live."""
        with self.transaction() as db:
            return db.execute("UPDATE ownership SET lease_until = ? WHERE repository = ? AND issue = ? "
                              "AND generation = ? AND live = 1",
                              (self.clock() + lease_seconds, *key, generation)).rowcount == 1

    def end(self, key, generation, outcome):
        """End that generation only, recording its outcome; return whether it was live."""
        with self.transaction() as db:
            if db.execute("UPDATE ownership SET live = 0 WHERE repository = ? AND issue = ? "
                          "AND generation = ? AND live = 1", (*key, generation)).rowcount != 1:
                return False
            if outcome is not None:
                db.execute("INSERT OR REPLACE INTO outcomes VALUES (?, ?, ?, ?, ?, ?)",
                           (*key, outcome.kind, int(outcome.progressed), outcome.retry_after, self.clock()))
            return True

    def admit(self, key, generation, method, path, request_digest):
        """Record a new occurrence as in flight before it is sent; return its intent ID."""
        with self.transaction() as db:
            sequence = db.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM mutations WHERE repository = ? "
                                  "AND issue = ? AND generation = ?", (*key, generation)).fetchone()[0]
            intent = f"{key[0]}#{key[1]}/{generation}/{sequence}"
            db.execute("INSERT INTO mutations VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in-flight', ?, NULL, NULL)",
                       (intent, *key, generation, sequence, method, path, request_digest, self.clock()))
        return intent

    def settle(self, intent, status):
        """Settle an in-flight occurrence as done, failed or unknown."""
        if status not in STATUSES[1:]:
            raise ValueError("invalid mutation status")
        with self.transaction() as db:
            if db.execute("UPDATE mutations SET status = ?, settled = ? WHERE intent = ? AND status = 'in-flight'",
                          (status, self.clock(), intent)).rowcount != 1:
                raise ValueError("mutation is not in flight")

    def mutations(self, key, status):
        """The task's occurrences with one status, in admission order."""
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM mutations WHERE repository = ? AND issue = ? AND status = ? "
                              "ORDER BY generation, sequence", (*key, status)).fetchall()
        return [MutationRecord(r[0], (r[1], r[2]), *r[3:11], json.loads(r[11]) if r[11] else None) for r in rows]

    def in_flight(self, key):
        """Occurrences sent or about to be sent and not yet settled."""
        return self.mutations(key, "in-flight")

    def unresolved(self, key):
        """Occurrences of unknown outcome; while any exist the task is never scheduled."""
        return self.mutations(key, "unknown")

    def resolve(self, intent, disposition, evidence, operator):
        """Record an operator's disposition of an unknown occurrence; nothing is resent."""
        if disposition not in DISPOSITIONS or not all(isinstance(v, str) and v.strip() for v in (evidence, operator)):
            raise ValueError("resolution needs a disposition, evidence and the resolving operator")
        resolution = {"disposition": disposition, "evidence": evidence, "operator": operator, "at": self.clock()}
        with self.transaction() as db:
            if db.execute("UPDATE mutations SET status = ?, resolution = ? WHERE intent = ? AND status = 'unknown'",
                          (DISPOSITIONS[disposition], json.dumps(resolution), intent)).rowcount != 1:
                raise ValueError("no unresolved mutation with that intent ID")

    # Observation and outcomes

    def acknowledged(self, key):
        """The last snapshot a completed tick acknowledged."""
        with self.transaction() as db:
            row = db.execute("SELECT snapshot FROM snapshots WHERE repository = ? AND issue = ?", key).fetchone()
        return json.loads(row[0]) if row else None

    def acknowledge(self, key, snapshot):
        """Acknowledge exactly the snapshot taken before the tick."""
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?)",
                       (*key, json.dumps(snapshot, sort_keys=True)))

    def outcome(self, key):
        """The outcome the task's last completed generation recorded."""
        with self.transaction() as db:
            row = db.execute("SELECT kind, progressed, retry_after FROM outcomes "
                             "WHERE repository = ? AND issue = ?", key).fetchone()
        return Outcome(row[0], bool(row[1]), row[2]) if row else None

    def active(self, repository):
        """Every task of the repository with a non-terminal outcome."""
        with self.transaction() as db:
            rows = db.execute("SELECT repository, issue FROM outcomes WHERE repository = ? AND kind != 'terminal' "
                              "ORDER BY issue", (repository,)).fetchall()
        return [tuple(row) for row in rows]
