"""The service's only durable state: one exclusive writer, synchronous commits, fail closed."""

import fcntl
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import delegation
import sponsorship
from controller import Outcome
from policy import digest

from .errors import RegistryError, RegistryFailure
from .observations import expected, satisfied

BINDING_QUERY = "SELECT upstream, fork, config_sha FROM bindings WHERE repository = ? AND issue = ?"
SCHEMA_VERSION = 6
DATABASE = "registry.db"
LOCK = "lock"
STATUSES = ("in-flight", "done", "failed", "unknown")
DISPOSITIONS = {"applied": "done", "not-applied": "failed", "discarded": "failed"}
REVIEW_INTENTS = "CREATE TABLE review_intents (intent TEXT PRIMARY KEY, context TEXT NOT NULL, recovery TEXT)"
BINDINGS = """CREATE TABLE bindings (
    repository TEXT NOT NULL, issue INTEGER NOT NULL, upstream TEXT NOT NULL, fork TEXT NOT NULL,
    config_sha TEXT NOT NULL, PRIMARY KEY (repository, issue))"""
RUNNER_SESSIONS = """CREATE TABLE runner_sessions (
    id TEXT PRIMARY KEY, repository TEXT NOT NULL, issue INTEGER NOT NULL, generation INTEGER NOT NULL,
    config_sha TEXT NOT NULL, removed INTEGER NOT NULL DEFAULT 0)"""
WEBHOOK_RECOVERY = "CREATE TABLE webhook_recovery (scope TEXT PRIMARY KEY, checked REAL NOT NULL)"
OBSERVATIONS = """CREATE TABLE observations (
    repository TEXT NOT NULL, issue INTEGER NOT NULL, version INTEGER NOT NULL,
    due_at REAL NOT NULL, attempts INTEGER NOT NULL, expectations TEXT NOT NULL, PRIMARY KEY (repository, issue))"""
SPONSOR_SOURCES = """CREATE TABLE IF NOT EXISTS issue_sponsor_sources (
    repository TEXT NOT NULL, issue INTEGER NOT NULL, revision TEXT NOT NULL, source TEXT NOT NULL,
    PRIMARY KEY(repository,issue,revision))"""
SPONSOR_PROJECTIONS = """CREATE TABLE IF NOT EXISTS issue_sponsorships (
    repository TEXT NOT NULL, issue INTEGER NOT NULL, source_revision INTEGER NOT NULL,
    confirmed INTEGER NOT NULL, document TEXT, PRIMARY KEY(repository,issue))"""
SCHEMA = (
    SPONSOR_SOURCES
    + ";"
    + SPONSOR_PROJECTIONS
    + ";"
    + OBSERVATIONS
    + ";"
    + WEBHOOK_RECOVERY
    + ";"
    + REVIEW_INTENTS
    + ";"
    + BINDINGS
    + ";"
    + RUNNER_SESSIONS
    + ";"
    + """
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
)


@dataclass(frozen=True)
class TaskBinding:
    """The immutable destinations and current execution configuration commit for a task."""

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

    def owns(self, generation):
        """Whether this live owner holds the generation, independently of lease expiry."""
        return self.live and self.generation == generation


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


def _lock_state_dir(root):
    """Acquire the exclusive writer lock in an owner-private directory."""
    if not root.is_dir() or root.stat().st_mode & 0o077:
        raise RegistryError("state directory must exist and be private to its owner")
    fd = os.open(root / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise RegistryError("state directory is in use by another process") from None
    return fd


def _connect(path):
    """Open the store with durable pragmas, closing a partially configured connection on failure."""
    db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    try:
        for pragma in ("journal_mode=WAL", "synchronous=FULL", "fullfsync=ON", "checkpoint_fullfsync=ON"):
            db.execute("PRAGMA " + pragma)
    except (RegistryError, sqlite3.Error):
        db.close()
        raise
    return db


def _initialize(db, registry_id):
    """Create the current schema and identity in one explicit transaction."""
    db.execute("BEGIN IMMEDIATE")
    for statement in filter(str.strip, SCHEMA.split(";")):
        db.execute(statement)
    db.executemany("INSERT INTO meta VALUES (?, ?)", [("registry_id", registry_id), ("schema", str(SCHEMA_VERSION))])
    db.execute("COMMIT")


def _migrate_v2(db):
    """Preserve old PR identities and journals while adding target bindings."""
    # Old PRs lived beside their issues. Preserve that identity and all journals.
    db.execute("BEGIN IMMEDIATE")
    db.execute("ALTER TABLE pulls RENAME TO pulls_v2")
    db.execute(
        "CREATE TABLE pulls (repository TEXT NOT NULL, issue INTEGER NOT NULL, "
        "pull_repository TEXT NOT NULL, pull INTEGER NOT NULL, "
        "PRIMARY KEY (repository, issue), UNIQUE (pull_repository, pull))"
    )
    db.execute("INSERT INTO pulls SELECT repository, issue, repository, pull FROM pulls_v2")
    db.execute("DROP TABLE pulls_v2")
    db.execute(BINDINGS)
    db.execute(RUNNER_SESSIONS)
    db.execute(REVIEW_INTENTS)
    db.execute(WEBHOOK_RECOVERY)
    db.execute(OBSERVATIONS)
    db.execute(SPONSOR_SOURCES)
    db.execute(SPONSOR_PROJECTIONS)
    db.execute("UPDATE meta SET value = ? WHERE name = 'schema'", (str(SCHEMA_VERSION),))
    db.execute("COMMIT")


def _migrate_v3(db):
    """Add review recovery records without changing existing write outcomes."""
    db.execute("BEGIN IMMEDIATE")
    db.execute(REVIEW_INTENTS)
    db.execute(WEBHOOK_RECOVERY)
    db.execute(OBSERVATIONS)
    db.execute(SPONSOR_SOURCES)
    db.execute(SPONSOR_PROJECTIONS)
    db.execute("UPDATE meta SET value = ? WHERE name = 'schema'", (str(SCHEMA_VERSION),))
    db.execute("COMMIT")


def _mutation_record(row):
    """Decode one stored write occurrence, including optional resolution evidence."""
    return MutationRecord(row[0], (row[1], row[2]), *row[3:11], json.loads(row[11]) if row[11] else None)


def without_stage(snapshot):
    """Remove private scheduling metadata while retaining all observed input and CI fields."""
    return {k: v for k, v in snapshot.items() if k not in ("_stage", "_sponsorship_discovery")}


def queue(db, key, cause, due_at):
    """Merge one cause into a task's work item within the caller's transaction."""
    row = db.execute("SELECT causes, due_at FROM queue WHERE repository = ? AND issue = ?", key).fetchone()
    causes = sorted(set(json.loads(row[0]) if row else ()) | {cause})
    db.execute(
        "INSERT OR REPLACE INTO queue VALUES (?, ?, ?, ?)",
        (*key, json.dumps(causes), min(row[1], due_at) if row else due_at),
    )


def queue_observation(db, key, now, expectation=None):
    """Coalesce expected changes while retaining a revision for in-flight races."""
    row = db.execute("SELECT expectations FROM observations WHERE repository = ? AND issue = ?", key).fetchone()
    expectations = json.loads(row[0]) if row else []
    if expectation is not None and expectation not in expectations:
        expectations.append(expectation)
    db.execute(
        "INSERT INTO observations VALUES (?, ?, 1, ?, 0, ?) ON CONFLICT(repository, issue) "
        "DO UPDATE SET version = version + 1, due_at = min(due_at, excluded.due_at), expectations = excluded.expectations",
        (*key, now, json.dumps(expectations)),
    )


def queue_ci(db, key, now):
    """Wake readiness for CI without bypassing a deferred failure."""
    snapshot = stored(db, "snapshots", key)
    if (snapshot or {}).get("_stage") == "gates" and stored(db, "deferred", key) is None:
        queue(db, key, "ci", now)


def human_input(snapshot):
    """Separate task inputs from CI-only observation and saved stage metadata."""
    if snapshot is None:
        return None
    result = without_stage(snapshot)
    if result.get("pull"):
        result["pull"] = {
            k: v for k, v in result["pull"].items() if k not in ("checks", "statuses", "suites", "runs", "merge_head")
        }
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
        fd = _lock_state_dir(root)
        path = root / DATABASE
        db = None
        try:
            if initialize:
                if path.exists():
                    raise RegistryError("registry already exists; refusing to initialize over it")
                os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            elif not path.exists() or not path.stat().st_size:
                raise RegistryError("registry is missing or empty; initialize a new one explicitly")
            db = _connect(path)
            if initialize:
                _initialize(db, registry_id)
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RegistryError("registry integrity check failed")
            meta = dict(db.execute("SELECT name, value FROM meta").fetchall())
            if meta.get("registry_id") != registry_id or set(meta) != {"registry_id", "schema"}:
                raise RegistryError("registry identity or schema version mismatch")
            if meta["schema"] == "2":
                _migrate_v2(db)
            elif meta["schema"] == "3":
                _migrate_v3(db)
            elif meta["schema"] not in ("4", "5", str(SCHEMA_VERSION)):
                raise RegistryError("registry identity or schema version mismatch")
            if meta["schema"] in ("4", "5"):
                db.execute("BEGIN IMMEDIATE")
                if meta["schema"] == "4":
                    db.execute(WEBHOOK_RECOVERY)
                    db.execute(OBSERVATIONS)
                db.execute(SPONSOR_SOURCES)
                db.execute(SPONSOR_PROJECTIONS)
                db.execute("UPDATE meta SET value = ? WHERE name = 'schema'", (str(SCHEMA_VERSION),))
                db.execute("COMMIT")
        except (RegistryError, sqlite3.Error) as error:
            if db is not None:
                db.close()
            os.close(fd)
            if isinstance(error, RegistryError):
                raise
            raise RegistryError("registry is unreadable") from None
        registry = cls(db, fd, clock)
        try:
            registry.recover()
        except BaseException:
            try:
                registry.close()
            except BaseException:
                pass
            raise
        return registry

    def close(self):
        """Release the directory lock even if closing the store fails; closing twice is harmless."""
        with self.guard:
            if self.lock_fd is not None:
                try:
                    self.db.close()
                finally:
                    fd, self.lock_fd = self.lock_fd, None
                    os.close(fd)

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

    def webhook_check(self, scope, checked=None):
        """Read or advance delivery-history coverage for this App and target configuration."""
        with self.transaction() as db:
            if checked is not None:
                db.execute("INSERT OR REPLACE INTO webhook_recovery VALUES (?, ?)", (scope, checked))
            row = db.execute("SELECT checked FROM webhook_recovery WHERE scope = ?", (scope,)).fetchone()
            return row[0] if row else None

    # Delivery de-duplication

    def seen(self, delivery_id):
        """Record a delivery; return whether it had already been recorded."""
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM deliveries WHERE id = ?", (delivery_id,)).fetchone():
                return True
            db.execute("INSERT INTO deliveries VALUES (?, ?)", (delivery_id, self.clock()))
            return False

    def receive(self, delivery_id, keys, event, ci=False, observation=False, expectation=None, sponsorship_hint=None):
        """Commit the delivery receipt and every affected task's wakeup together."""
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM deliveries WHERE id = ?", (delivery_id,)).fetchone():
                return False
            now = self.clock()
            for key in keys:
                if sponsorship_hint is not None:
                    self._sponsor_hint(db, key, sponsorship_hint)
                if observation:
                    queue_observation(db, key, now, expectation)
                elif ci:
                    queue_ci(db, key, now)
                else:
                    queue(db, key, "webhook:" + event, now)
            db.execute("INSERT INTO deliveries VALUES (?, ?)", (delivery_id, now))
        return True

    def _sponsor_hint(self, db, key, source):
        """Commit a new source revision and invalidate its projection within the receipt transaction."""
        result = db.execute(
            "INSERT OR IGNORE INTO issue_sponsor_sources VALUES (?,?,?,?)",
            (*key, source["revision"], json.dumps(source, sort_keys=True)),
        )
        if result.rowcount:
            db.execute(
                "INSERT INTO issue_sponsorships VALUES (?,?,1,0,NULL) ON CONFLICT(repository,issue) DO UPDATE SET source_revision=source_revision+1,confirmed=0",
                key,
            )

    def sponsorship(self, key):
        """Current issue projection, with a local source fence even before a task exists."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT source_revision,confirmed,document FROM issue_sponsorships WHERE repository=? AND issue=?", key
            ).fetchone()
        document = json.loads(row[2]) if row and row[2] else {}
        return {
            **document,
            "source_revision": row[0] if row else 0,
            "status": "confirmed" if row and row[1] else "provisional",
        }

    def sponsor_sources(self, key):
        """Immutable webhook revision facts retained for edits/deletions and source reconstruction."""
        with self.transaction() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT source FROM issue_sponsor_sources WHERE repository=? AND issue=? ORDER BY revision", key
                )
            ]

    def sponsor_issues(self, repository, active=False):
        """Poll facts/live tasks; show provisional hints until confirmation, retaining direct history reads."""
        with self.transaction() as db:
            rows = db.execute(
                "SELECT s.issue,s.document,EXISTS(SELECT 1 FROM outcomes o "
                "WHERE o.repository=s.repository AND o.issue=s.issue AND o.kind='terminal'),s.confirmed "
                "FROM issue_sponsorships s WHERE s.repository=? ORDER BY s.issue",
                (repository,),
            ).fetchall()
            result = []
            for issue, document, terminal, confirmed in rows:
                value = json.loads(document or "{}")
                facts, task = value.get("facts"), value.get("task")
                visible = (
                    not terminal and bool(facts or task)
                    if active
                    else bool(facts or not terminal and task or not confirmed)
                )
                if visible:
                    result.append(issue)
            return result

    def confirm_sponsorship(self, key, revision, document, comments):
        """Publish only a complete read at the captured local source revision, atomically."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT source_revision,document FROM issue_sponsorships WHERE repository=? AND issue=?", key
            ).fetchone()
            current = row[0] if row else 0
            job = db.execute("SELECT expectations FROM observations WHERE repository=? AND issue=?", key).fetchone()
            if current != revision or job and not satisfied(json.loads(job[0]), comments):
                return False
            if row is None and not document.get("facts"):
                return True
            old = json.loads(row[1]) if row and row[1] else {}
            if old.get("task"):
                document = {**document, "task": old["task"]}
            db.execute(
                "INSERT INTO issue_sponsorships VALUES (?,?,?,?,?) ON CONFLICT(repository,issue) DO UPDATE SET confirmed=excluded.confirmed,document=excluded.document",
                (*key, current, int(document.get("history_complete", True)), json.dumps(document, sort_keys=True)),
            )
            return True

    def record_shadow(self, key, state, projection=None):
        """Expose checkpoint-owned slots and pending acknowledgments without opening task state in the BFF."""
        with self.transaction() as db:
            row = db.execute("SELECT document FROM issue_sponsorships WHERE repository=? AND issue=?", key).fetchone()
            document = json.loads(row[0]) if row and row[0] else dict(projection or {})
            document["task"] = {
                "funding": "org",
                "slots": state.get("sponsorships", {}),
                "pending": list(state.get("pending_sponsorships", {})),
                "initialization": state.get("funding_initialization"),
                "initialization_pending": state.get("funding_initialization_pending"),
                "status": state.get("sponsorship_status", "pending"),
            }
            db.execute(
                "INSERT INTO issue_sponsorships VALUES (?,?,?,?,?) ON CONFLICT(repository,issue) DO UPDATE SET document=excluded.document",
                (
                    *key,
                    document.get("source_revision", 0),
                    int(document.get("status") == "confirmed"),
                    json.dumps(document, sort_keys=True),
                ),
            )

    def request_observation(self, key, comment=None, action="created"):
        """Durably request an uncached observation, including after posting a delegated command."""
        with self.transaction() as db:
            if comment and delegation.sponsorship_hint(comment.get("body") or ""):
                self._sponsor_hint(db, key, sponsorship.hint(comment, action))
            queue_observation(db, key, self.clock(), expected(comment, action) if comment is not None else None)

    def observations(self):
        """Due targeted observations with revisions and previous failure counts."""
        with self.transaction() as db:
            return [
                ((repo, issue), version, attempts, json.loads(expectations))
                for repo, issue, version, attempts, expectations in db.execute(
                    "SELECT repository, issue, version, attempts, expectations FROM observations WHERE due_at <= ? ORDER BY due_at",
                    (self.clock(),),
                )
            ]

    def finish_observation(self, key, version, retry_after=None):
        """Finish only the observed revision; newer deliveries always retain their job."""
        with self.transaction() as db:
            if retry_after is None:
                db.execute(
                    "DELETE FROM observations WHERE repository = ? AND issue = ? AND version = ?", (*key, version)
                )
            else:
                db.execute(
                    "UPDATE observations SET attempts = attempts + 1, due_at = ? "
                    "WHERE repository = ? AND issue = ? AND version = ?",
                    (self.clock() + retry_after, *key, version),
                )

    def observed_comment(self, key, identifier):
        """Whether an acknowledged or deferred snapshot included this command comment."""
        with self.transaction() as db:
            return any(
                identifier == comment[0]
                for table in ("snapshots", "deferred")
                for comment in (stored(db, table, key) or {}).get("comments", [])
            )

    # Queue

    def enqueue(self, key, cause, due_at):
        """Add a cause to the task's work item, keeping its earliest due time."""
        with self.transaction() as db:
            queue(db, key, cause, due_at)

    def enqueue_unscheduled(self, key, cause, due_at):
        """Queue a cause only for a task with no outcome, no live tick and no deferred retry.

        Such a task has no retry deadline the cause could shorten; the cause backs up a command
        observation the service could not request before stopping.
        The check shares a transaction with `grant`, `defer` and `end`, so a first tick either
        consumes the cause or is already live. Return whether it was queued.
        """
        with self.transaction() as db:
            for query in (
                "SELECT 1 FROM outcomes WHERE repository = ? AND issue = ?",
                "SELECT 1 FROM ownership WHERE repository = ? AND issue = ? AND live = 1",
                "SELECT 1 FROM deferred WHERE repository = ? AND issue = ?",
            ):
                if db.execute(query, key).fetchone():
                    return False
            queue(db, key, cause, due_at)
            return True

    def enqueue_ci(self, key):
        """CI may wake completed reviews, never a model stage or a deferred failure."""
        with self.transaction() as db:
            queue_ci(db, key, self.clock())

    def due(self, now):
        """Work items due by `now`, oldest first."""
        with self.transaction() as db:
            rows = db.execute(
                "SELECT repository, issue, causes, due_at FROM queue WHERE due_at <= ? "
                "ORDER BY due_at, repository, issue",
                (now,),
            ).fetchall()
        return [WorkItem((r[0], r[1]), tuple(json.loads(r[2])), r[3]) for r in rows]

    # Ownership and mutations: called only inside the gateway's critical section

    def grant(self, key, worker, lease_seconds, consume=True):
        """Grant the next generation, consuming the task's work item in the same commit.

        Administrative grants (`consume=False`) leave queued work for the next tick.
        """
        with self.transaction() as db:
            row = db.execute("SELECT generation FROM ownership WHERE repository = ? AND issue = ?", key).fetchone()
            generation = (row[0] if row else 0) + 1
            db.execute(
                "INSERT OR REPLACE INTO ownership VALUES (?, ?, ?, ?, ?, 1)",
                (*key, generation, worker, self.clock() + lease_seconds),
            )
            if consume:
                db.execute("DELETE FROM queue WHERE repository = ? AND issue = ?", key)
        return generation

    def current(self, key):
        """The task's latest generation, live or ended."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT generation, worker, lease_until, live FROM ownership WHERE repository = ? AND issue = ?", key
            ).fetchone()
        return Ownership(row[0], row[1], row[2], bool(row[3])) if row else None

    def renew(self, key, generation, lease_seconds):
        """Extend a live generation's lease; return whether it is still live."""
        with self.transaction() as db:
            return (
                db.execute(
                    "UPDATE ownership SET lease_until = ? WHERE repository = ? AND issue = ? "
                    "AND generation = ? AND live = 1",
                    (self.clock() + lease_seconds, *key, generation),
                ).rowcount
                == 1
            )

    def end(self, key, generation, outcome):
        """End that generation only, recording its outcome; return whether it was live.

        A `continue` outcome is due again at once and a `wait-until` at its time, in the
        same commit, so no restart can lose the follow-up.
        """
        now = self.clock()
        with self.transaction() as db:
            if (
                db.execute(
                    "UPDATE ownership SET live = 0 WHERE repository = ? AND issue = ? AND generation = ? AND live = 1",
                    (*key, generation),
                ).rowcount
                != 1
            ):
                return False
            if outcome is not None:
                db.execute(
                    "INSERT OR REPLACE INTO outcomes VALUES (?, ?, ?, ?, ?, ?)",
                    (*key, outcome.kind, int(outcome.progressed), outcome.retry_after, now),
                )
                if outcome.kind == "continue":
                    queue(db, key, "continue", now)
                elif outcome.kind == "wait-until":
                    queue(db, key, "wait-until", now + (outcome.retry_after or 0))
            return True

    def admit(self, key, generation, method, path, request_digest, review=None, recovery_of=None):
        """Record a new occurrence as in flight before it is sent; return its intent ID."""
        with self.transaction() as db:
            sequence = db.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM mutations WHERE repository = ? "
                "AND issue = ? AND generation = ?",
                (*key, generation),
            ).fetchone()[0]
            intent = f"{key[0]}#{key[1]}/{generation}/{sequence}"
            db.execute(
                "INSERT INTO mutations VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in-flight', ?, NULL, NULL)",
                (intent, *key, generation, sequence, method, path, request_digest, self.clock()),
            )
            if review is not None:
                db.execute("INSERT INTO review_intents VALUES (?, ?, NULL)", (intent, json.dumps(review)))
            if recovery_of is not None:
                db.execute("UPDATE review_intents SET recovery = ? WHERE intent = ?", (intent, recovery_of))
        return intent

    def review_intent(self, intent):
        """Read immutable review context and the linked compensating write, if any."""
        with self.transaction() as db:
            row = db.execute("SELECT context, recovery FROM review_intents WHERE intent = ?", (intent,)).fetchone()
        return {**json.loads(row[0]), "recovery": row[1]} if row else None

    def review_submissions(self, key, review_id):
        """Read every journaled submit for this review, including manually resolved attempts."""
        with self.transaction() as db:
            rows = db.execute(
                "SELECT m.*, r.context FROM mutations m JOIN review_intents r ON m.intent = r.intent "
                "WHERE m.repository = ? AND m.issue = ? ORDER BY m.generation, m.sequence",
                key,
            ).fetchall()
        return [
            _mutation_record(row[:-1])
            for row in rows
            if (context := json.loads(row[-1])).get("kind") == "submitPullRequestReview"
            and context.get("review") == review_id
        ]

    def settle(self, intent, status, review_id=None):
        """Settle an occurrence, atomically retaining a successfully created review's ID."""
        if status not in STATUSES[1:]:
            raise ValueError("invalid mutation status")
        with self.transaction() as db:
            if (
                db.execute(
                    "UPDATE mutations SET status = ?, settled = ? WHERE intent = ? AND status = 'in-flight'",
                    (status, self.clock(), intent),
                ).rowcount
                != 1
            ):
                raise ValueError("mutation is not in flight")
            if review_id is not None:
                row = db.execute("SELECT context FROM review_intents WHERE intent = ?", (intent,)).fetchone()
                context = json.loads(row[0]) if row else {}
                if (
                    status != "done"
                    or context.get("kind") != "addPullRequestReview"
                    or not isinstance(review_id, str)
                    or not review_id
                ):
                    raise ValueError("invalid created review identity")
                context["review"] = review_id
                db.execute("UPDATE review_intents SET context = ? WHERE intent = ?", (json.dumps(context), intent))

    def mutations(self, key, status):
        """The task's occurrences with one status, in admission order."""
        with self.transaction() as db:
            rows = db.execute(
                "SELECT * FROM mutations WHERE repository = ? AND issue = ? AND status = ? "
                "ORDER BY generation, sequence",
                (*key, status),
            ).fetchall()
        return [_mutation_record(row) for row in rows]

    def mutation(self, intent):
        """One occurrence by its intent ID, or None."""
        with self.transaction() as db:
            row = db.execute("SELECT * FROM mutations WHERE intent = ?", (intent,)).fetchone()
        return _mutation_record(row) if row else None

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
            if (
                db.execute(
                    "UPDATE mutations SET status = ?, resolution = ? WHERE intent = ? AND status = 'unknown'",
                    (DISPOSITIONS[disposition], json.dumps(resolution), intent),
                ).rowcount
                != 1
            ):
                raise ValueError("no unresolved mutation with that intent ID")
            key = db.execute("SELECT repository, issue FROM mutations WHERE intent = ?", (intent,)).fetchone()
            if not db.execute(
                "SELECT 1 FROM mutations WHERE repository = ? AND issue = ? AND status = 'unknown'", key
            ).fetchone():
                queue(db, tuple(key), "resolved", now)

    # Observation and outcomes

    def acknowledged(self, key):
        """The last snapshot a completed tick acknowledged."""
        with self.transaction() as db:
            return stored(db, "snapshots", key)

    def acknowledge(self, key, snapshot):
        """Acknowledge exactly the snapshot taken before the tick; polls of that input are dropped."""
        with self.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?)", (*key, json.dumps(snapshot, sort_keys=True))
            )
            db.execute("DELETE FROM deferred WHERE repository = ? AND issue = ?", key)
            drop_polls(db, key, snapshot)

    def defer(self, key, snapshot):
        """Record input a failed tick already saw, dropping any poll of it queued during the tick.

        Its retry is queued at its own delay, which polling must not hasten.
        """
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO deferred VALUES (?, ?, ?)", (*key, json.dumps(snapshot, sort_keys=True)))
            drop_polls(db, key, snapshot)

    def deferred(self, key):
        """The input awaiting a retry, or None."""
        with self.transaction() as db:
            return stored(db, "deferred", key)

    def observe(self, key, snapshot):
        """Enqueue a polled snapshot if it is new input; return whether it was.

        New means neither acknowledged nor deferred; a terminal task wakes for reopening
        or a newer durable consent source revision. The check and the enqueue share one transaction, so a tick that
        defers or acknowledges this input meanwhile cannot leave a stale poll queued.
        """
        snapshot = json.loads(json.dumps(snapshot))
        with self.transaction() as db:
            acknowledged = stored(db, "snapshots", key)
            outcome = db.execute("SELECT kind FROM outcomes WHERE repository = ? AND issue = ?", key).fetchone()
            if outcome and outcome[0] == "terminal":
                new = (
                    acknowledged is not None
                    and acknowledged["issue"]["state"] == "closed"
                    and snapshot["issue"]["state"] == "open"
                ) or (snapshot.get("sponsorship") or {}).get("source_revision", 0) > (
                    (acknowledged or {}).get("sponsorship") or {}
                ).get("source_revision", 0)
            else:
                deferred = stored(db, "deferred", key)
                new = human_input(snapshot) != human_input(acknowledged) and human_input(snapshot) != human_input(
                    deferred
                )
                if (
                    not new
                    and deferred is None
                    and (acknowledged or {}).get("_stage") == "gates"
                    and without_stage(snapshot) != without_stage(acknowledged)
                ):
                    queue(db, key, "ci", self.clock())
                    return True
            if new:
                queue(db, key, poll_cause(snapshot), self.clock())
            return new

    def outcome(self, key):
        """The outcome the task's last completed generation recorded."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT kind, progressed, retry_after FROM outcomes WHERE repository = ? AND issue = ?", key
            ).fetchone()
        return Outcome(row[0], bool(row[1]), row[2]) if row else None

    def binding(self, key):
        """The task's pinned configuration and destinations, or None before enrollment."""
        with self.transaction() as db:
            row = db.execute(BINDING_QUERY, key).fetchone()
        return TaskBinding(*row) if row else None

    def bind(self, key, upstream, fork, config_sha):
        """Pin the first configuration; refuse retargeting and reuse it on subsequent ticks."""
        with self.transaction() as db:
            row = db.execute(BINDING_QUERY, key).fetchone()
            if row is not None:
                if tuple(row[:2]) != (upstream, fork):
                    raise ValueError("task target changed; reconcile before continuing")
                return TaskBinding(*row)
            db.execute("INSERT INTO bindings VALUES (?, ?, ?, ?, ?)", (*key, upstream, fork, config_sha))
            return TaskBinding(upstream, fork, config_sha)

    def adopt_config(self, key, generation, previous, current):
        """Apply a checkpointed writer resume once under the current owner generation."""
        with self.transaction() as db:
            owner = db.execute(
                "SELECT generation, live, lease_until FROM ownership WHERE repository = ? AND issue = ?", key
            ).fetchone()
            if not owner or owner[0] != generation or not owner[1] or owner[2] <= self.clock():
                raise RegistryError("configuration adoption lost task ownership")
            binding = db.execute(BINDING_QUERY, key).fetchone()
            if not binding or binding[2] not in (previous, current):
                raise RegistryError("configuration adoption no longer matches the task binding")
            db.execute("UPDATE bindings SET config_sha = ? WHERE repository = ? AND issue = ?", (current, *key))

    def link(self, key, pull, repository=None):
        """Record one PR identity; a different task cannot take over the association.

        The default repository preserves old single-repository callers during migration.
        """
        repository = repository or key[0]
        with self.transaction() as db:
            owner = db.execute(
                "SELECT repository, issue FROM pulls WHERE pull_repository = ? AND pull = ?", (repository, pull)
            ).fetchone()
            if owner is not None and tuple(owner) != key:
                raise ValueError("pull request already belongs to another task")
            current = db.execute(
                "SELECT pull_repository, pull FROM pulls WHERE repository = ? AND issue = ?", key
            ).fetchone()
            if current is not None and tuple(current) != (repository, pull):
                raise ValueError("task already has another pull request")
            db.execute("INSERT OR IGNORE INTO pulls VALUES (?, ?, ?, ?)", (*key, repository, pull))

    def pull(self, key):
        """The task's recorded PR identity, or None."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT pull_repository, pull FROM pulls WHERE repository = ? AND issue = ?", key
            ).fetchone()
        return PullRequest(*row) if row else None

    def task(self, repository, pull):
        """The issue identity owning a PR in `repository`, or None."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT repository, issue FROM pulls WHERE pull_repository = ? AND pull = ?", (repository, pull)
            ).fetchone()
        return tuple(row) if row else None

    def status(self):
        """Every task the registry knows: queue, ownership, outcome and unknown writes."""
        with self.transaction() as db:
            keys = sorted(
                set(
                    db.execute(
                        "SELECT repository, issue FROM queue UNION SELECT repository, issue "
                        "FROM ownership UNION SELECT repository, issue FROM outcomes"
                    ).fetchall()
                )
            )
            tasks = []
            for key in keys:
                queued = db.execute(
                    "SELECT causes, due_at FROM queue WHERE repository = ? AND issue = ?", key
                ).fetchone()
                owner = db.execute(
                    "SELECT generation, live, lease_until FROM ownership WHERE repository = ? AND issue = ?", key
                ).fetchone()
                outcome = db.execute(
                    "SELECT kind, retry_after, recorded FROM outcomes WHERE repository = ? AND issue = ?", key
                ).fetchone()
                unknown = [
                    row[0]
                    for row in db.execute(
                        "SELECT intent FROM mutations WHERE repository = ? AND issue = ? AND status = 'unknown'", key
                    )
                ]
                tasks.append(
                    {
                        "repository": key[0],
                        "issue": key[1],
                        "queued": {"causes": json.loads(queued[0]), "due_at": queued[1]} if queued else None,
                        "generation": owner[0] if owner else None,
                        "live": bool(owner and owner[1]),
                        "outcome": {"kind": outcome[0], "retry_after": outcome[1], "recorded": outcome[2]}
                        if outcome
                        else None,
                        "unresolved": unknown,
                    }
                )
        return tasks

    def runner_session(self, session):
        """The task and configuration authorized for a recorded run ref, or None."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT repository, issue, generation, config_sha, removed FROM runner_sessions WHERE id = ?",
                (session,),
            ).fetchone()
        return dict(zip(("repository", "issue", "generation", "config_sha", "removed"), row)) if row else None

    def register_runner(self, key, generation, session, config_sha):
        """Record a session before creating its branch or dispatching its job."""
        with self.transaction() as db:
            prior = db.execute(
                "SELECT repository, issue, generation, config_sha FROM runner_sessions WHERE id = ?", (session,)
            ).fetchone()
            expected = (*key, generation, config_sha)
            if prior and tuple(prior) != expected:
                raise RegistryError("runner session is already bound to another task")
            db.execute(
                "INSERT OR IGNORE INTO runner_sessions (id, repository, issue, generation, config_sha) "
                "VALUES (?, ?, ?, ?, ?)",
                (session, *expected),
            )

    def runner_sessions(self):
        """Run refs not yet confirmed removed, for reconciliation."""
        with self.transaction() as db:
            rows = db.execute(
                "SELECT id, repository, issue, generation FROM runner_sessions WHERE removed = 0"
            ).fetchall()
        return [tuple(row) for row in rows]

    def remove_runner(self, session):
        """Record confirmation that the session's run ref is gone."""
        with self.transaction() as db:
            db.execute("UPDATE runner_sessions SET removed = 1 WHERE id = ?", (session,))

    def commit_tasks(self, repository, sha):
        """Tasks whose last observed upstream PR uses this head or test-merge commit."""
        with self.transaction() as db:
            rows = db.execute(
                "SELECT p.repository, p.issue, s.snapshot FROM pulls p JOIN snapshots s "
                "ON p.repository = s.repository AND p.issue = s.issue "
                "WHERE p.pull_repository = ?",
                (repository,),
            ).fetchall()
            return [
                (row[0], row[1])
                for row in rows
                if sha
                in (
                    (json.loads(row[2]).get("pull") or {}).get("head"),
                    (json.loads(row[2]).get("pull") or {}).get("merge_head"),
                )
            ]

    def base_tasks(self, repository, ref):
        """Wake PRs whose base branch moved; legacy snapshots lack a branch and are included."""
        if not isinstance(ref, str) or not ref.startswith("refs/heads/"):
            return []
        with self.transaction() as db:
            rows = db.execute(
                "SELECT p.repository, p.issue, s.snapshot FROM pulls p "
                "LEFT JOIN snapshots s USING(repository, issue) WHERE p.pull_repository = ?",
                (repository,),
            ).fetchall()
            return [
                (repo, issue)
                for repo, issue, snapshot in rows
                if ((json.loads(snapshot).get("pull") or {}).get("base_ref") if snapshot else None)
                in (None, ref.removeprefix("refs/heads/"))
            ]

    def active(self, repository):
        """Every task of the repository with a non-terminal outcome."""
        with self.transaction() as db:
            rows = db.execute(
                "SELECT repository, issue FROM outcomes WHERE repository = ? AND kind != 'terminal' ORDER BY issue",
                (repository,),
            ).fetchall()
        return [tuple(row) for row in rows]
