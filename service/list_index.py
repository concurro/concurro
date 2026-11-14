"""The list index: a persistent record of when each repository's lists changed.

GitHub and the registry stay authoritative. Webhook deliveries and registry signals raise a
revision per repository and list kind; the backoffice re-reads a list when its revision moved.
Applying is asynchronous and may lose a delivery, so `check` compares GitHub's delivery history
with the receipts kept here and repairs what is missing.
"""

import hmac
import json
import logging
import queue
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import private

from .settings import CHECKOUT
from .webhook_recovery import HISTORY_SECONDS

log = logging.getLogger("concurro.service")

DATABASE = "list-index.sqlite3"
VERSION = 1
# Webhook events that change a list, and the list each changes.
EVENT_KINDS = {
    "issues": "issues",
    "issue_comment": "issues",
    "pull_request": "pulls",
    "check_run": "pulls",
    "check_suite": "pulls",
    "workflow_run": "runs",
}
# Kinds signalled by the service itself; GitHub's delivery history cannot repair them.
LOCAL_KINDS = ("sponsorships",)
KINDS = (*dict.fromkeys(EVENT_KINDS.values()), *LOCAL_KINDS)
QUEUE_SIZE = 1024
# More missing deliveries than this for one kind are covered by a new baseline instead.
REPLAY_LIMIT = 20
# A kind is current while its deliveries were verified against GitHub's history this recently.
FRESH_SECONDS = 600
# Receipts and changes outlive GitHub's delivery history.
RETAINED_SECONDS = 2 * HISTORY_SECONDS
SCHEMA = """
CREATE TABLE meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE repositories (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE revisions (repository TEXT NOT NULL, kind TEXT NOT NULL, counter INTEGER NOT NULL,
                        PRIMARY KEY (repository, kind));
CREATE TABLE receipts (guid TEXT PRIMARY KEY, recorded REAL NOT NULL, outcome TEXT NOT NULL);
CREATE TABLE coverage (repository TEXT NOT NULL, kind TEXT NOT NULL, mode TEXT NOT NULL,
                       baseline REAL NOT NULL, verified REAL, PRIMARY KEY (repository, kind));
CREATE TABLE changes (sequence INTEGER PRIMARY KEY AUTOINCREMENT, repository TEXT NOT NULL,
                      kind TEXT, item TEXT, changed REAL NOT NULL);
"""


def read_token(path):
    """The shared bearer as hex text: 32 random bytes, hex-encoded, in an owner-only file."""
    token = bytes.fromhex(private.read(path, (CHECKOUT,)).decode().strip())
    if len(token) != 32:
        raise ValueError("the list index token must be 32 bytes")
    return token.hex()


def epoch(stamp):
    """Seconds for one of GitHub's ISO timestamps, or None when it isn't one."""
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except (TypeError, ValueError):
        return None


class ListIndex:
    """One SQLite file, one writer thread; every public method is safe from any thread."""

    def __init__(self, root, clock=time.time):
        """Open or create the index; `start` names the repositories before anything is applied."""
        self.clock = clock
        self.lock = threading.RLock()
        self.pending = queue.Queue(QUEUE_SIZE)
        self.overflowed, self.stopping = threading.Event(), threading.Event()
        self.names, self.writer = {}, None
        # Whether a history check has completed in this process; before one, nothing is current.
        self.checked = False
        self.db = sqlite3.connect(Path(root) / DATABASE, isolation_level=None, check_same_thread=False)
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'meta'").fetchone():
                with self.transaction() as db:
                    for statement in filter(str.strip, SCHEMA.split(";")):
                        db.execute(statement)
                    db.execute("INSERT INTO meta VALUES ('version', ?)", (str(VERSION),))
                    db.execute("INSERT INTO meta VALUES ('incarnation', ?)", (secrets.token_hex(8),))
            found = dict(self.db.execute("SELECT name, value FROM meta"))
            if found.get("version") != str(VERSION):
                raise sqlite3.DatabaseError("list index schema is newer than this service")
            self.incarnation = found["incarnation"]
        except sqlite3.Error:
            self.db.close()
            raise

    @contextmanager
    def transaction(self):
        """One atomic write under the index's lock, rolled back on any error."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    def start(self, repositories):
        """Record `{name: GitHub ID}`, establish coverage and start applying.

        A kind not verified within GitHub's delivery history gets a new baseline: history can no
        longer show what it missed. Local signals lost while stopped are covered by `resynchronize`.
        """
        now = self.clock()
        with self.transaction() as db:
            db.execute("DELETE FROM repositories")
            db.executemany("INSERT INTO repositories VALUES (?, ?)", [(n, name) for name, n in repositories.items()])
            # A repository that left the configuration is no longer covered; its revisions stay monotonic.
            for (name,) in db.execute("SELECT DISTINCT repository FROM coverage").fetchall():
                if name not in repositories:
                    db.execute("DELETE FROM coverage WHERE repository = ?", (name,))
            for name in repositories:
                for kind in KINDS:
                    row = db.execute(
                        "SELECT verified FROM coverage WHERE repository = ? AND kind = ?", (name, kind)
                    ).fetchone()
                    if row is None or row[0] is None or now - row[0] >= HISTORY_SECONDS:
                        self.rebase(db, name, kind, now)
            self.resync(db, repositories, now)
        self.names = {number: name for name, number in repositories.items()}
        self.writer = threading.Thread(target=self.apply, name="list-index", daemon=True)
        self.writer.start()

    def close(self):
        """Stop applying and close the file; queued entries are left to the receipt check."""
        self.stopping.set()
        if self.writer:
            self.writer.join(timeout=5)
        with self.lock:
            self.db.close()

    # Signals

    def offer(self, guid, event, repository):
        """Queue one verified delivery without waiting; a full queue is repaired later, never raised."""
        kind = EVENT_KINDS.get(event)
        if kind:
            self.put(("delivery", guid, repository, kind))

    def signal(self, repository, kind):
        """Queue a change the service itself made to a list."""
        self.put(("signal", None, repository, kind))

    def put(self, entry):
        try:
            self.pending.put_nowait(entry)
        except queue.Full:
            self.overflowed.set()

    def apply(self):
        """The writer: one transaction per queued entry, and one resynchronization per overflow."""
        while not self.stopping.is_set():
            try:
                if self.overflowed.is_set():
                    self.overflowed.clear()
                    with self.transaction() as db:
                        self.resync(db, self.names.values(), self.clock())
                source, guid, repository, kind = self.pending.get(timeout=0.2)
            except queue.Empty:
                continue
            except sqlite3.Error as error:
                log.warning("list index resynchronization failed: %s", type(error).__name__)
                self.overflowed.set()
                self.stopping.wait(1)
                continue
            try:
                with self.transaction() as db:
                    if source == "signal" or self.receive(db, guid, "applied"):
                        self.raise_revision(db, repository, kind, self.clock())
            except sqlite3.Error as error:
                # Without a receipt the next history check repairs a delivery; a local signal needs this.
                log.warning("list index write failed: %s", type(error).__name__)
                if source == "signal":
                    self.overflowed.set()
            finally:
                self.pending.task_done()

    # Writes, inside a transaction

    def receive(self, db, guid, outcome):
        """Record a delivery's receipt; False when it already had one."""
        row = (guid, self.clock(), outcome)
        return bool(db.execute("INSERT OR IGNORE INTO receipts VALUES (?, ?, ?)", row).rowcount)

    @staticmethod
    def raise_revision(db, repository, kind, now, item=None):
        db.execute(
            "INSERT INTO revisions VALUES (?, ?, 1) ON CONFLICT(repository, kind) DO UPDATE SET counter = counter + 1",
            (repository, kind),
        )
        db.execute(
            "INSERT INTO changes (repository, kind, item, changed) VALUES (?, ?, ?, ?)", (repository, kind, item, now)
        )

    def rebase(self, db, repository, kind, now):
        """Start coverage afresh: deliveries up to now are covered by the re-read this revision causes."""
        db.execute(
            "INSERT INTO coverage VALUES (?, ?, 'signals', ?, NULL) ON CONFLICT(repository, kind) "
            "DO UPDATE SET baseline = excluded.baseline",
            (repository, kind, now),
        )
        self.raise_revision(db, repository, kind, now)

    def resync(self, db, repositories, now):
        """Cover local signals that may have been lost: raise their revisions and tell the feed to resynchronize."""
        for repository in repositories:
            for kind in LOCAL_KINDS:
                db.execute(
                    "INSERT INTO revisions VALUES (?, ?, 1) ON CONFLICT(repository, kind) "
                    "DO UPDATE SET counter = counter + 1",
                    (repository, kind),
                )
            db.execute(
                "INSERT INTO changes (repository, kind, item, changed) VALUES (?, NULL, NULL, ?)", (repository, now)
            )

    # Recovery

    def rebaseline(self, repository=None, kinds=KINDS):
        """New baselines for one repository or all of them, as a refresh or a full reconciliation needs."""
        now = self.clock()
        with self.transaction() as db:
            for name in [repository] if repository else list(self.names.values()):
                for kind in kinds:
                    self.rebase(db, name, kind, now)

    def check(self, groups, started):
        """Repair deliveries GitHub's history lists and this index has no receipt for.

        `groups` maps each delivery GUID to its attempts, as `Recovery.deliveries` returns them.
        A summary names the event and repository, which is all a revision needs. A kind last
        verified, or begun and never verified, longer ago than GitHub keeps history gets a new
        baseline first, whatever the
        registry's own coverage says. A completed check advances every kind's verified time to
        `started`, when the scan began.
        """
        now = self.clock()
        with self.transaction() as db:
            rows = db.execute("SELECT repository, kind, baseline, verified FROM coverage").fetchall()
            for repository, kind, baseline, verified in rows:
                # Never verified: nothing since its baseline has been checked against history.
                if started - (baseline if verified is None else verified) >= HISTORY_SECONDS:
                    self.rebase(db, repository, kind, now)
            baselines = {(r, k): b for r, k, b in db.execute("SELECT repository, kind, baseline FROM coverage")}
            missing = {}
            for guid, attempts in groups.items():
                item = max(attempts, key=lambda attempt: attempt["id"])
                key = (self.names.get(item.get("repository_id")), EVENT_KINDS.get(item.get("event")))
                delivered = epoch(item.get("delivered_at"))
                if key not in baselines or delivered is None or delivered <= baselines[key]:
                    continue
                if not db.execute("SELECT 1 FROM receipts WHERE guid = ?", (guid,)).fetchone():
                    missing.setdefault(key, []).append(guid)
            for (repository, kind), guids in missing.items():
                if len(guids) > REPLAY_LIMIT:
                    self.rebase(db, repository, kind, now)
                    continue
                for guid in guids:
                    self.receive(db, guid, "repaired")
                self.raise_revision(db, repository, kind, now)
            db.execute("UPDATE coverage SET verified = ?", (started,))
            db.execute("DELETE FROM receipts WHERE recorded < ?", (now - RETAINED_SECONDS,))
            db.execute("DELETE FROM changes WHERE changed < ?", (now - RETAINED_SECONDS,))
        self.checked = True
        return {key: len(guids) for key, guids in missing.items()}

    # Reads

    def revisions(self, repositories):
        """`{repository: {kind: {revision, mode, state, verified}}}` for the configured ones asked for."""
        now = self.clock()
        with self.lock:
            counters = {(r, k): c for r, k, c in self.db.execute("SELECT repository, kind, counter FROM revisions")}
            rows = self.db.execute("SELECT repository, kind, mode, verified FROM coverage").fetchall()
        found = {}
        for repository, kind, mode, verified in rows:
            if repository in repositories:
                found.setdefault(repository, {})[kind] = {
                    "revision": f"{self.incarnation}:{counters.get((repository, kind), 0)}",
                    "mode": mode,
                    "state": "current"
                    if self.checked and verified is not None and now - verified <= FRESH_SECONDS
                    else "catching-up",
                    "verified": datetime.fromtimestamp(verified, UTC).isoformat() if verified is not None else None,
                }
        return found


class Lists:
    """The list endpoints for the backoffice, on the service's private listener."""

    def __init__(self, index, token):
        """`token` is the shared bearer as hex text; without one every request is refused."""
        self.index, self.token = index, token

    def handle(self, path, authorization, body):
        """`(HTTP status, JSON payload)` for one request; never raises for a refused one."""
        if not self.token:
            return 503, {"error": "the list index is not configured"}
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip().encode(), self.token.encode()):
            return 401, {"error": "invalid list index token"}
        try:
            request = json.loads(body)
            if path == "/lists/revisions":
                wanted = request["repositories"]
                if not isinstance(wanted, list) or not all(isinstance(name, str) for name in wanted):
                    raise TypeError
                return 200, {"repositories": self.index.revisions(set(wanted))}
            if path == "/lists/refresh":
                if request["repository"] not in self.index.names.values():
                    return 404, {"error": "repository not configured"}
                self.index.rebaseline(request["repository"])
                return 200, {"refreshed": True}
        except (ValueError, KeyError, TypeError):
            return 400, {"error": "invalid list request"}
        return 404, {"error": "unknown list request"}
