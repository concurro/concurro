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
from policy import digest

from .errors import RegistryError, RegistryFailure

SCHEMA_VERSION = 3
DATABASE = "registry.db"
LOCK = "lock"
STATUSES = ("in-flight", "done", "failed", "unknown")
DISPOSITIONS = {"applied": "done", "not-applied": "failed"}
BINDINGS = """CREATE TABLE bindings (
    repository TEXT NOT NULL, issue INTEGER NOT NULL, upstream TEXT NOT NULL, fork TEXT NOT NULL,
    config_sha TEXT NOT NULL, PRIMARY KEY (repository, issue))"""
RUNNER_SESSIONS = """CREATE TABLE runner_sessions (
    id TEXT PRIMARY KEY, repository TEXT NOT NULL, issue INTEGER NOT NULL, generation INTEGER NOT NULL,
    config_sha TEXT NOT NULL, removed INTEGER NOT NULL DEFAULT 0)"""
SCHEMA = BINDINGS + ";" + RUNNER_SESSIONS + ";" + """
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
CREATE TABLE deferred (repository TEXT NOT NULL, issue INTEGER NOT NULL, snapshot TEXT NOT NULL,
                       PRIMARY KEY (repository, issue));
CREATE TABLE pulls (repository TEXT NOT NULL, issue INTEGER NOT NULL,
                    pull_repository TEXT NOT NULL, pull INTEGER NOT NULL,
                    PRIMARY KEY (repository, issue), UNIQUE (pull_repository, pull));
"""


@dataclass(frozen=True)
class TaskBinding:
    """The immutable publication destinations and configuration commit for a task."""

    upstream: str
    fork: str
    config_sha: str


@dataclass(frozen=True)
class PullRequest:
    """The repository and number of a task's published PR."""

    repository: str
    number: int


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


def human_input(snapshot):
    """Separate task inputs from CI-only observation and saved stage metadata."""
    if snapshot is None:
        return None
    result = {k: v for k, v in snapshot.items() if k != "_stage"}
    if result.get("pull"):
        result["pull"] = {k: v for k, v in result["pull"].items()
                          if k not in ("checks", "statuses", "suites", "runs", "merge_head")}
    return result


def poll_cause(snapshot):
    """The poll cause for human/PR input; CI has its own non-model wakeup."""
    return "poll:" + digest(human_input(snapshot))


def drop_polls(db, key, snapshot):
    """Drop queued polls of input a tick has handled or deferred; every other cause and its due time stay."""
    row = db.execute("SELECT causes FROM queue WHERE repository = ? AND issue = ?", key).fetchone()
    if not row:
        return
    causes = [cause for cause in json.loads(row[0]) if cause not in (poll_cause(snapshot), "ci")]
    if causes:
        db.execute("UPDATE queue SET causes = ? WHERE repository = ? AND issue = ?", (json.dumps(causes), *key))
    else:
        db.execute("DELETE FROM queue WHERE repository = ? AND issue = ?", key)


def stored(db, table, key):
    """A stored snapshot, or None."""
    row = db.execute(f"SELECT snapshot FROM {table} WHERE repository = ? AND issue = ?", key).fetchone()
    return json.loads(row[0]) if row else None


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
            if meta.get("registry_id") != registry_id or set(meta) != {"registry_id", "schema"}:
                raise RegistryError("registry identity or schema version mismatch")
            if meta["schema"] == "2":
                # Old PRs lived beside their issues. Preserve that identity and all journals.
                db.execute("BEGIN IMMEDIATE")
                db.execute("ALTER TABLE pulls RENAME TO pulls_v2")
                db.execute("CREATE TABLE pulls (repository TEXT NOT NULL, issue INTEGER NOT NULL, "
                           "pull_repository TEXT NOT NULL, pull INTEGER NOT NULL, "
                           "PRIMARY KEY (repository, issue), UNIQUE (pull_repository, pull))")
                db.execute("INSERT INTO pulls SELECT repository, issue, repository, pull FROM pulls_v2")
                db.execute("DROP TABLE pulls_v2")
                db.execute(BINDINGS)
                db.execute(RUNNER_SESSIONS)
                db.execute("UPDATE meta SET value = ? WHERE name = 'schema'", (str(SCHEMA_VERSION),))
                db.execute("COMMIT")
            elif meta["schema"] != str(SCHEMA_VERSION):
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

    def enqueue_ci(self, key):
        """CI may wake completed reviews, never a model stage or a deferred failure."""
        with self.transaction() as db:
            snapshot = stored(db, "snapshots", key)
            if (snapshot or {}).get("_stage") == "gates" and stored(db, "deferred", key) is None:
                queue(db, key, "ci", self.clock())

    def due(self, now):
        """Work items due by `now`, oldest first."""
        with self.transaction() as db:
            rows = db.execute("SELECT repository, issue, causes, due_at FROM queue WHERE due_at <= ? "
                              "ORDER BY due_at, repository, issue", (now,)).fetchall()
        return [WorkItem((r[0], r[1]), tuple(json.loads(r[2])), r[3]) for r in rows]

    # Ownership and mutations: called only inside the gateway's critical section

    def grant(self, key, worker, lease_seconds, consume=True):
        """Grant the next generation, consuming the task's work item in the same commit.

        Administrative grants (`consume=False`) leave queued work for the next tick.
        """
        with self.transaction() as db:
            row = db.execute("SELECT generation FROM ownership WHERE repository = ? AND issue = ?", key).fetchone()
            generation = (row[0] if row else 0) + 1
            db.execute("INSERT OR REPLACE INTO ownership VALUES (?, ?, ?, ?, ?, 1)",
                       (*key, generation, worker, self.clock() + lease_seconds))
            if consume:
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
        """End that generation only, recording its outcome; return whether it was live.

        A `continue` outcome is due again at once and a `wait-until` at its time, in the
        same commit, so no restart can lose the follow-up.
        """
        now = self.clock()
        with self.transaction() as db:
            if db.execute("UPDATE ownership SET live = 0 WHERE repository = ? AND issue = ? "
                          "AND generation = ? AND live = 1", (*key, generation)).rowcount != 1:
                return False
            if outcome is not None:
                db.execute("INSERT OR REPLACE INTO outcomes VALUES (?, ?, ?, ?, ?, ?)",
                           (*key, outcome.kind, int(outcome.progressed), outcome.retry_after, now))
                if outcome.kind == "continue":
                    queue(db, key, "continue", now)
                elif outcome.kind == "wait-until":
                    queue(db, key, "wait-until", now + (outcome.retry_after or 0))
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

    def mutation(self, intent):
        """One occurrence by its intent ID, or None."""
        with self.transaction() as db:
            row = db.execute("SELECT * FROM mutations WHERE intent = ?", (intent,)).fetchone()
        return MutationRecord(row[0], (row[1], row[2]), *row[3:11], json.loads(row[11]) if row[11] else None) \
            if row else None

    def in_flight(self, key):
        """Occurrences sent or about to be sent and not yet settled."""
        return self.mutations(key, "in-flight")

    def unresolved(self, key):
        """Occurrences of unknown outcome; while any exist the task is never scheduled."""
        return self.mutations(key, "unknown")

    def resolve(self, intent, disposition, evidence, operator):
        """Record an operator's disposition of an unknown occurrence; nothing is resent.

        Resolving the task's last unknown write makes it due in the same commit, so the
        next tick reconstructs the result even across a crash.
        """
        if disposition not in DISPOSITIONS or not all(isinstance(v, str) and v.strip() for v in (evidence, operator)):
            raise ValueError("resolution needs a disposition, evidence and the resolving operator")
        now = self.clock()
        resolution = {"disposition": disposition, "evidence": evidence, "operator": operator, "at": now}
        with self.transaction() as db:
            if db.execute("UPDATE mutations SET status = ?, resolution = ? WHERE intent = ? AND status = 'unknown'",
                          (DISPOSITIONS[disposition], json.dumps(resolution), intent)).rowcount != 1:
                raise ValueError("no unresolved mutation with that intent ID")
            key = db.execute("SELECT repository, issue FROM mutations WHERE intent = ?", (intent,)).fetchone()
            if not db.execute("SELECT 1 FROM mutations WHERE repository = ? AND issue = ? AND status = 'unknown'",
                              key).fetchone():
                queue(db, tuple(key), "resolved", now)

    # Observation and outcomes

    def acknowledged(self, key):
        """The last snapshot a completed tick acknowledged."""
        with self.transaction() as db:
            return stored(db, "snapshots", key)

    def acknowledge(self, key, snapshot):
        """Acknowledge exactly the snapshot taken before the tick; polls of that input are dropped."""
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?)",
                       (*key, json.dumps(snapshot, sort_keys=True)))
            db.execute("DELETE FROM deferred WHERE repository = ? AND issue = ?", key)
            drop_polls(db, key, snapshot)

    def defer(self, key, snapshot):
        """Record input a failed tick already saw, dropping any poll of it queued during the tick.

        Its retry is queued at its own delay, which polling must not hasten.
        """
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO deferred VALUES (?, ?, ?)",
                       (*key, json.dumps(snapshot, sort_keys=True)))
            drop_polls(db, key, snapshot)

    def deferred(self, key):
        """The input awaiting a retry, or None."""
        with self.transaction() as db:
            return stored(db, "deferred", key)

    def observe(self, key, snapshot):
        """Enqueue a polled snapshot if it is new input; return whether it was.

        New means neither acknowledged nor deferred; a terminal task wakes only when its
        issue is reopened. The check and the enqueue share one transaction, so a tick that
        defers or acknowledges this input meanwhile cannot leave a stale poll queued.
        """
        snapshot = json.loads(json.dumps(snapshot))
        with self.transaction() as db:
            acknowledged = stored(db, "snapshots", key)
            outcome = db.execute("SELECT kind FROM outcomes WHERE repository = ? AND issue = ?", key).fetchone()
            if outcome and outcome[0] == "terminal":
                new = (acknowledged is not None and acknowledged["issue"]["state"] == "closed"
                       and snapshot["issue"]["state"] == "open")
            else:
                deferred = stored(db, "deferred", key)
                new = human_input(snapshot) != human_input(acknowledged) and human_input(snapshot) != human_input(deferred)
                if (not new and deferred is None and (acknowledged or {}).get("_stage") == "gates"
                        and {k: v for k, v in snapshot.items() if k != "_stage"}
                            != {k: v for k, v in acknowledged.items() if k != "_stage"}):
                    queue(db, key, "ci", self.clock())
                    return True
            if new:
                queue(db, key, poll_cause(snapshot), self.clock())
            return new

    def outcome(self, key):
        """The outcome the task's last completed generation recorded."""
        with self.transaction() as db:
            row = db.execute("SELECT kind, progressed, retry_after FROM outcomes "
                             "WHERE repository = ? AND issue = ?", key).fetchone()
        return Outcome(row[0], bool(row[1]), row[2]) if row else None

    def binding(self, key):
        """The task's pinned configuration and destinations, or None before enrollment."""
        with self.transaction() as db:
            row = db.execute("SELECT upstream, fork, config_sha FROM bindings WHERE repository = ? AND issue = ?",
                             key).fetchone()
        return TaskBinding(*row) if row else None

    def bind(self, key, upstream, fork, config_sha):
        """Pin the first configuration; refuse retargeting and reuse it on subsequent ticks."""
        with self.transaction() as db:
            row = db.execute("SELECT upstream, fork, config_sha FROM bindings WHERE repository = ? AND issue = ?",
                             key).fetchone()
            if row is not None:
                if tuple(row[:2]) != (upstream, fork):
                    raise ValueError("task target changed; reconcile before continuing")
                return TaskBinding(*row)
            db.execute("INSERT INTO bindings VALUES (?, ?, ?, ?, ?)", (*key, upstream, fork, config_sha))
            return TaskBinding(upstream, fork, config_sha)

    def link(self, key, pull, repository=None):
        """Record one PR identity; a different task cannot take over the association.

        The default repository preserves old single-repository callers during migration.
        """
        repository = repository or key[0]
        with self.transaction() as db:
            owner = db.execute("SELECT repository, issue FROM pulls WHERE pull_repository = ? AND pull = ?",
                               (repository, pull)).fetchone()
            if owner is not None and tuple(owner) != key:
                raise ValueError("pull request already belongs to another task")
            current = db.execute("SELECT pull_repository, pull FROM pulls WHERE repository = ? AND issue = ?",
                                 key).fetchone()
            if current is not None and tuple(current) != (repository, pull):
                raise ValueError("task already has another pull request")
            db.execute("INSERT OR IGNORE INTO pulls VALUES (?, ?, ?, ?)", (*key, repository, pull))

    def pull(self, key):
        """The task's recorded PR identity, or None."""
        with self.transaction() as db:
            row = db.execute("SELECT pull_repository, pull FROM pulls WHERE repository = ? AND issue = ?",
                             key).fetchone()
        return PullRequest(*row) if row else None

    def task(self, repository, pull):
        """The issue identity owning a PR in `repository`, or None."""
        with self.transaction() as db:
            row = db.execute("SELECT repository, issue FROM pulls WHERE pull_repository = ? AND pull = ?",
                             (repository, pull)).fetchone()
        return tuple(row) if row else None

    def status(self):
        """Every task the registry knows: queue, ownership, outcome and unknown writes."""
        with self.transaction() as db:
            keys = sorted(set(db.execute("SELECT repository, issue FROM queue UNION SELECT repository, issue "
                                         "FROM ownership UNION SELECT repository, issue FROM outcomes").fetchall()))
            tasks = []
            for key in keys:
                queued = db.execute("SELECT causes, due_at FROM queue WHERE repository = ? AND issue = ?", key).fetchone()
                owner = db.execute("SELECT generation, live, lease_until FROM ownership "
                                   "WHERE repository = ? AND issue = ?", key).fetchone()
                outcome = db.execute("SELECT kind, retry_after, recorded FROM outcomes "
                                     "WHERE repository = ? AND issue = ?", key).fetchone()
                unknown = [row[0] for row in db.execute("SELECT intent FROM mutations WHERE repository = ? "
                                                        "AND issue = ? AND status = 'unknown'", key)]
                tasks.append({"repository": key[0], "issue": key[1],
                              "queued": {"causes": json.loads(queued[0]), "due_at": queued[1]} if queued else None,
                              "generation": owner[0] if owner else None, "live": bool(owner and owner[1]),
                              "outcome": {"kind": outcome[0], "retry_after": outcome[1], "recorded": outcome[2]}
                              if outcome else None, "unresolved": unknown})
        return tasks

    def runner_session(self, session):
        """The task and configuration authorized for a recorded run ref, or None."""
        with self.transaction() as db:
            row = db.execute("SELECT repository, issue, generation, config_sha, removed FROM runner_sessions WHERE id = ?",
                             (session,)).fetchone()
        return dict(zip(("repository", "issue", "generation", "config_sha", "removed"), row)) if row else None

    def register_runner(self, key, generation, session, config_sha):
        """Record a session before creating its branch or dispatching its job."""
        with self.transaction() as db:
            prior = db.execute("SELECT repository, issue, generation, config_sha FROM runner_sessions WHERE id = ?",
                               (session,)).fetchone()
            expected = (*key, generation, config_sha)
            if prior and tuple(prior) != expected:
                raise RegistryError("runner session is already bound to another task")
            db.execute("INSERT OR IGNORE INTO runner_sessions (id, repository, issue, generation, config_sha) "
                       "VALUES (?, ?, ?, ?, ?)", (session, *expected))

    def runner_sessions(self):
        """Run refs not yet confirmed removed, for reconciliation."""
        with self.transaction() as db:
            rows = db.execute("SELECT id, repository, issue, generation FROM runner_sessions WHERE removed = 0").fetchall()
        return [tuple(row) for row in rows]

    def remove_runner(self, session):
        """Record confirmation that the session's run ref is gone."""
        with self.transaction() as db:
            db.execute("UPDATE runner_sessions SET removed = 1 WHERE id = ?", (session,))

    def commit_tasks(self, repository, sha):
        """Tasks whose last observed upstream PR uses this head or test-merge commit."""
        with self.transaction() as db:
            rows = db.execute("SELECT p.repository, p.issue, s.snapshot FROM pulls p JOIN snapshots s "
                              "ON p.repository = s.repository AND p.issue = s.issue "
                              "WHERE p.pull_repository = ?", (repository,)).fetchall()
            return [(row[0], row[1]) for row in rows
                    if sha in ((json.loads(row[2]).get("pull") or {}).get("head"),
                               (json.loads(row[2]).get("pull") or {}).get("merge_head"))]

    def active(self, repository):
        """Every task of the repository with a non-terminal outcome."""
        with self.transaction() as db:
            rows = db.execute("SELECT repository, issue FROM outcomes WHERE repository = ? AND kind != 'terminal' "
                              "ORDER BY issue", (repository,)).fetchall()
        return [tuple(row) for row in rows]
