"""The list index: a persistent, eventually consistent read model of each repository's lists.

GitHub and the registry stay authoritative. Webhook deliveries and registry signals raise a
revision per repository and list kind. A kind in mode `signals` holds nothing else: the
backoffice re-reads its list from GitHub when the revision moved. A kind in mode `rows` holds
the list itself, built from GitHub once and kept current from delivery payloads.

Applying is asynchronous and may lose a delivery, so `check` compares GitHub's delivery history
with the receipts kept here and repairs what is missing. Reads from GitHub (rebuilds, and
single items a delivery could not settle) run on a worker thread and are published under the
same ordering rule as deliveries, so a read never replaces what arrived during it.
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

from .github_reads import executor_run, executor_runs, run_attempt
from .settings import CHECKOUT
from .webhook_recovery import HISTORY_SECONDS

log = logging.getLogger("concurro.service")

DATABASE = "list-index.sqlite3"
VERSION = 2
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
# Forward migrations by the version they start from; a new file runs them all.
MIGRATIONS = {
    1: """
ALTER TABLE coverage ADD COLUMN built REAL;
ALTER TABLE coverage ADD COLUMN wanted REAL;
ALTER TABLE coverage ADD COLUMN failed INTEGER NOT NULL DEFAULT 0;
CREATE TABLE pending (repository TEXT NOT NULL, kind TEXT NOT NULL, item TEXT NOT NULL,
                      requested REAL NOT NULL, latest REAL NOT NULL, PRIMARY KEY (repository, kind, item));
CREATE TABLE runs (repository TEXT NOT NULL, run INTEGER NOT NULL, attempt INTEGER NOT NULL, updated TEXT,
                   document TEXT, version INTEGER NOT NULL, PRIMARY KEY (repository, run, attempt));
CREATE TABLE run_members (repository TEXT NOT NULL, run INTEGER NOT NULL, created TEXT NOT NULL,
                          added REAL NOT NULL, PRIMARY KEY (repository, run));
""",
}
# How many runs a list shows.
LISTED_RUNS = 50


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


def projected(event, payload):
    """What a delivery's payload contributes to a list's rows; None when it changes none.

    A payload without what its event must carry is refused, never taken for an unrelated event.
    """
    if event != "workflow_run":
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("workflow_run"), dict):
        raise ValueError("workflow_run delivery without its run")
    return executor_run(payload["workflow_run"])


def stamp(seconds):
    return datetime.fromtimestamp(seconds, UTC).isoformat() if seconds is not None else None


class ListIndex:
    """One SQLite file, a writer thread for signals and a worker thread for reads from GitHub.

    Every public method is safe from any thread.
    """

    def __init__(self, root, clock=time.time):
        """Open or create the index; `start` names the repositories before anything is applied."""
        self.clock = clock
        self.lock = threading.RLock()
        self.pending = queue.Queue(QUEUE_SIZE)
        self.overflowed, self.stopping = threading.Event(), threading.Event()
        self.names, self.writer, self.worker, self.reader = {}, None, None, None
        # Reads that failed since the last history check; each is retried once per check.
        self.deferred = set()
        # Whether a history check has completed in this process; before one, nothing is current.
        self.checked = False
        self.db = sqlite3.connect(Path(root) / DATABASE, isolation_level=None, check_same_thread=False)
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'meta'").fetchone():
                with self.transaction() as db:
                    for statement in filter(str.strip, SCHEMA.split(";")):
                        db.execute(statement)
                    db.execute("INSERT INTO meta VALUES ('version', '1')")
                    db.execute("INSERT INTO meta VALUES ('incarnation', ?)", (secrets.token_hex(8),))
            found = dict(self.db.execute("SELECT name, value FROM meta"))
            version = int(found.get("version") or 0)
            if not 1 <= version <= VERSION:
                raise sqlite3.DatabaseError("list index schema is newer than this service")
            for step in range(version, VERSION):
                with self.transaction() as db:
                    for statement in filter(str.strip, MIGRATIONS[step].split(";")):
                        db.execute(statement)
                    db.execute("UPDATE meta SET value = ? WHERE name = 'version'", (str(step + 1),))
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

    def start(self, repositories, rows=None, reader=None, work=True):
        """Record `{name: GitHub ID}`, establish coverage and start applying.

        `rows` maps a kind to the repositories whose list this index holds; `reader(repository)`
        returns the read transport its rebuilds and item reads use. A kind not verified within
        GitHub's delivery history is covered afresh: history can no longer show what it missed.
        Local signals lost while stopped are covered by `resync`.
        """
        now = self.clock()
        self.reader = reader
        with self.transaction() as db:
            db.execute("DELETE FROM repositories")
            db.executemany("INSERT INTO repositories VALUES (?, ?)", [(n, name) for name, n in repositories.items()])
            # A repository that left the configuration is no longer covered; its revisions stay monotonic.
            for (name,) in db.execute("SELECT DISTINCT repository FROM coverage").fetchall():
                if name not in repositories:
                    db.execute("DELETE FROM coverage WHERE repository = ?", (name,))
            for name in repositories:
                for kind in KINDS:
                    mode = "rows" if name in (rows or {}).get(kind, ()) else "signals"
                    row = db.execute(
                        "SELECT mode, verified, built FROM coverage WHERE repository = ? AND kind = ?", (name, kind)
                    ).fetchone()
                    if row is None:
                        db.execute(
                            "INSERT INTO coverage (repository, kind, mode, baseline) VALUES (?, ?, ?, ?)",
                            (name, kind, mode, now),
                        )
                    elif row[0] != mode:
                        db.execute("UPDATE coverage SET mode = ? WHERE repository = ? AND kind = ?", (mode, name, kind))
                    unverified = row is None or row[1] is None or now - row[1] >= HISTORY_SECONDS
                    if unverified or mode == "rows" and (row is None or row[2] is None or row[0] != mode):
                        self.rebase(db, name, kind, now)
            self.resync(db, repositories, now)
        self.names = {number: name for name, number in repositories.items()}
        self.writer = threading.Thread(target=self.apply, name="list-index", daemon=True)
        self.writer.start()
        if reader is not None and work:
            self.worker = threading.Thread(target=self.work, name="list-index-reads", daemon=True)
            self.worker.start()

    def close(self):
        """Stop applying and close the file; queued entries are left to the receipt check."""
        self.stopping.set()
        for thread in (self.writer, self.worker):
            if thread:
                thread.join(timeout=5)
        with self.lock:
            self.db.close()

    # Signals

    def offer(self, guid, event, repository, payload=None):
        """Queue one verified delivery without waiting; a full queue is repaired later, never raised.

        Only the fields a list shows are copied from the payload. One that cannot be projected
        raises before anything is queued, which leaves the delivery to the history check.
        """
        kind = EVENT_KINDS.get(event)
        if kind:
            self.put(("delivery", guid, repository, kind, projected(event, payload)))

    def signal(self, repository, kind):
        """Queue a change the service itself made to a list."""
        self.put(("signal", None, repository, kind, None))

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
                source, guid, repository, kind, data = self.pending.get(timeout=0.2)
            except queue.Empty:
                continue
            except sqlite3.Error as error:
                log.warning("list index resynchronization failed: %s", type(error).__name__)
                self.overflowed.set()
                self.stopping.wait(1)
                continue
            try:
                with self.transaction() as db:
                    if source == "signal":
                        self.raise_revision(db, repository, kind, self.clock())
                    else:
                        self.deliver(db, guid, repository, kind, data, "applied")
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

    def deliver(self, db, guid, repository, kind, data, outcome):
        """Apply one delivery with its receipt: a revision in mode `signals`, its rows in mode `rows`."""
        if not self.receive(db, guid, outcome):
            return
        now = self.clock()
        mode = db.execute("SELECT mode FROM coverage WHERE repository = ? AND kind = ?", (repository, kind)).fetchone()
        if mode is None or mode[0] != "rows":
            self.raise_revision(db, repository, kind, now)
        elif kind == "runs" and data is not None and self.apply_run(db, repository, data, now):
            self.raise_revision(db, repository, kind, now, str(data["id"]))

    def apply_run(self, db, repository, document, now, read=None):
        """Store one run attempt under the ordering rule; whether the list changed.

        A snapshot older than the stored one is dropped and a newer one replaces it. A delivery
        that ties with different content cannot be ordered, so that exact attempt is read. `read`
        marks a result read from GitHub and carries the row's version when the read began: it
        wins a tie only if nothing changed the row meanwhile, and is read again otherwise.
        """
        run, attempt, updated = document["id"], document["run_attempt"], document.get("updated_at") or ""
        text = json.dumps(document, sort_keys=True)
        key = (repository, run, attempt)
        row = db.execute(
            "SELECT updated, document, version FROM runs WHERE repository=? AND run=? AND attempt=?", key
        ).fetchone()
        changed = False
        if row is None:
            db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, 1)", (*key, updated, text))
            changed = True
        elif row[1] is None and read is None:
            # GitHub reported this attempt gone. A delivery, of whatever age, cannot say otherwise: ask.
            self.request(db, repository, "runs", f"{run}/{attempt}")
            return False
        elif row[1] != text:
            if updated > (row[0] or "") or updated == row[0] and read is not None and row[2] == read:
                db.execute(
                    "UPDATE runs SET updated = ?, document = ?, version = version + 1 "
                    "WHERE repository = ? AND run = ? AND attempt = ?",
                    (updated, text, *key),
                )
                changed = True
            elif updated == row[0]:
                self.request(db, repository, "runs", f"{run}/{attempt}")
        member = (repository, run, document.get("created_at") or "", now)
        changed |= bool(db.execute("INSERT OR IGNORE INTO run_members VALUES (?, ?, ?, ?)", member).rowcount)
        first = (repository, run)
        if (
            attempt > 1
            and not db.execute("SELECT 1 FROM runs WHERE repository=? AND run=? AND attempt=1", first).fetchone()
        ):
            # Only attempt 1 can have served a runner session; no other attempt stands in for it.
            self.request(db, repository, "runs", f"{run}/1")
        return changed

    def request(self, db, repository, kind, item):
        """Owe one read from GitHub; an item already owed keeps the time it was first asked for."""
        now = self.clock()
        db.execute(
            "INSERT INTO pending VALUES (?, ?, ?, ?, ?) ON CONFLICT(repository, kind, item) "
            "DO UPDATE SET latest = excluded.latest",
            (repository, kind, item, now, now),
        )

    def rebase(self, db, repository, kind, now):
        """Cover a kind afresh from now.

        In mode `signals` the baseline moves and the revision makes the backoffice read the list
        again. In mode `rows` a rebuild is owed; it moves the baseline once it has published.
        """
        row = db.execute("SELECT mode FROM coverage WHERE repository = ? AND kind = ?", (repository, kind)).fetchone()
        if row is not None and row[0] == "rows":
            # The latest request: a rebuild settles only the requests made before it began.
            db.execute(
                "UPDATE coverage SET wanted = MAX(COALESCE(wanted, 0), ?) WHERE repository = ? AND kind = ?",
                (now, repository, kind),
            )
            return
        db.execute(
            "INSERT INTO coverage (repository, kind, mode, baseline) VALUES (?, ?, 'signals', ?) "
            "ON CONFLICT(repository, kind) DO UPDATE SET baseline = excluded.baseline",
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

    def check(self, groups, started, detail=None):
        """Repair deliveries GitHub's history lists and this index has no receipt for.

        `groups` maps each delivery GUID to its attempts, as `Recovery.deliveries` returns them.
        In mode `signals` a summary names the event and repository, which is all a revision
        needs. In mode `rows` the payload is fetched with `detail(attempt)` and applied; one that
        cannot be fetched or applied, like too many at once, is covered by a rebuild instead.
        A kind last verified, or begun and never verified, longer ago than GitHub keeps history
        is covered afresh first, whatever the registry's own coverage says. A completed check
        advances every kind's verified time to `started`, when the scan began.
        """
        now = self.clock()
        replay = []
        with self.transaction() as db:
            rows = db.execute("SELECT repository, kind, baseline, verified FROM coverage").fetchall()
            for repository, kind, baseline, verified in rows:
                # Never verified: nothing since its baseline has been checked against history.
                if started - (baseline if verified is None else verified) >= HISTORY_SECONDS:
                    self.rebase(db, repository, kind, now)
            covered = {
                (r, k): (b, m) for r, k, b, m in db.execute("SELECT repository, kind, baseline, mode FROM coverage")
            }
            missing = {}
            for guid, attempts in groups.items():
                item = max(attempts, key=lambda attempt: attempt["id"])
                key = (self.names.get(item.get("repository_id")), EVENT_KINDS.get(item.get("event")))
                delivered = epoch(item.get("delivered_at"))
                if key not in covered or delivered is None or delivered <= covered[key][0]:
                    continue
                if not db.execute("SELECT 1 FROM receipts WHERE guid = ?", (guid,)).fetchone():
                    missing.setdefault(key, []).append((guid, item))
            for (repository, kind), entries in missing.items():
                if len(entries) > REPLAY_LIMIT:
                    self.rebase(db, repository, kind, now)
                elif covered[repository, kind][1] == "rows":
                    replay.append((repository, kind, entries))
                else:
                    for guid, _ in entries:
                        self.receive(db, guid, "repaired")
                    self.raise_revision(db, repository, kind, now)
        for repository, kind, entries in replay:
            try:
                for guid, item in entries:
                    data = projected(item.get("event"), detail(item))
                    with self.transaction() as db:
                        baseline = db.execute(
                            "SELECT baseline FROM coverage WHERE repository = ? AND kind = ?", (repository, kind)
                        ).fetchone()[0]
                        if epoch(item.get("delivered_at")) <= baseline:
                            # A rebuild published while the payload was fetched already accounts for it.
                            self.receive(db, guid, "covered")
                        else:
                            self.deliver(db, guid, repository, kind, data, "repaired")
            except Exception as error:
                log.warning("list index replay for %s %s failed: %s", repository, kind, type(error).__name__)
                with self.transaction() as db:
                    self.rebase(db, repository, kind, now)
        with self.transaction() as db:
            db.execute("UPDATE coverage SET verified = ?", (started,))
            db.execute("DELETE FROM receipts WHERE recorded < ?", (now - RETAINED_SECONDS,))
            db.execute("DELETE FROM changes WHERE changed < ?", (now - RETAINED_SECONDS,))
        self.deferred.clear()
        self.checked = True
        return {key: len(entries) for key, entries in missing.items()}

    # Reads from GitHub

    def work(self):
        """The worker: owed rebuilds and item reads, one GitHub request at a time."""
        while not self.stopping.wait(1):
            try:
                self.drain()
            except sqlite3.Error as error:
                log.warning("list index reads failed: %s", type(error).__name__)

    def drain(self):
        """Do every owed read once; one that fails waits for the next history check."""
        while not self.stopping.is_set():
            with self.lock:
                wanted = self.db.execute(
                    "SELECT repository, kind, NULL FROM coverage WHERE mode = 'rows' AND wanted IS NOT NULL"
                ).fetchall()
                owed = self.db.execute("SELECT repository, kind, item FROM pending ORDER BY requested").fetchall()
            job = next((job for job in (*wanted, *owed) if job not in self.deferred), None)
            if job is None:
                return
            repository, kind, item = job
            try:
                if item is None:
                    self.rebuild_runs(repository)
                else:
                    self.read_run(repository, item)
            except sqlite3.Error:
                raise
            except Exception as error:
                log.warning("list index read of %s %s failed: %s", repository, kind, type(error).__name__)
                self.deferred.add(job)
                if item is None:
                    with self.transaction() as db:
                        db.execute(
                            "UPDATE coverage SET failed = 1 WHERE repository = ? AND kind = ?", (repository, kind)
                        )

    def versions(self, repository):
        with self.lock:
            found = self.db.execute("SELECT run, attempt, version FROM runs WHERE repository = ?", (repository,))
            return {(run, attempt): version for run, attempt, version in found}

    def absent(self, db, repository, run, attempt, read):
        """Record that GitHub no longer has an attempt, under the fence of a read; whether the list changed.

        An attempt never stored is only remembered, so it is not asked for again. A stored one
        loses its document if nothing changed it since the read began, and is read again otherwise.
        """
        key = (repository, run, attempt)
        row = db.execute(
            "SELECT document, version FROM runs WHERE repository=? AND run=? AND attempt=?", key
        ).fetchone()
        if row is None:
            db.execute("INSERT INTO runs VALUES (?, ?, ?, NULL, NULL, 1)", key)
        elif row[0] is not None and row[1] == read:
            # `updated` stays as the ordering evidence of what was removed.
            db.execute(
                "UPDATE runs SET document = NULL, version = version + 1 "
                "WHERE repository = ? AND run = ? AND attempt = ?",
                key,
            )
            return True
        elif row[0] is not None:
            self.request(db, repository, "runs", f"{run}/{attempt}")
        return False

    def rebuild_runs(self, repository):
        """Read the fork's executor runs and publish them as the list.

        The listing replaces the displayed membership except for runs deliveries created while
        it was read. It covers every delivery before it began, which becomes the baseline.
        """
        began, versions = self.clock(), self.versions(repository)
        reader = self.reader(repository)
        listed = executor_runs(reader, repository, LISTED_RUNS)
        firsts = {run["id"]: run_attempt(reader, repository, run["id"], 1) for run in listed if run["run_attempt"] != 1}
        with self.transaction() as db:
            for document in (*listed, *filter(None, firsts.values())):
                self.apply_run(
                    db, repository, document, began, versions.get((document["id"], document["run_attempt"]), 0)
                )
            for run, first in firsts.items():
                if first is None:
                    self.absent(db, repository, run, 1, versions.get((run, 1), 0))
            marks = ",".join("?" * len(listed))
            db.execute(
                f"DELETE FROM run_members WHERE repository = ? AND added < ? AND run NOT IN ({marks})",
                (repository, began, *(run["id"] for run in listed)),
            )
            db.execute(
                "UPDATE coverage SET built = ?, baseline = ?, failed = 0, "
                "wanted = CASE WHEN wanted <= ? THEN NULL ELSE wanted END WHERE repository = ? AND kind = 'runs'",
                (began, began, began, repository),
            )
            self.raise_revision(db, repository, "runs", self.clock())

    def read_run(self, repository, item):
        """Read one exact run attempt and publish it; requests made before the read began are settled."""
        run, attempt = map(int, item.split("/"))
        began, version = self.clock(), self.versions(repository).get((run, attempt), 0)
        document = run_attempt(self.reader(repository), repository, run, attempt)
        with self.transaction() as db:
            if document is None:
                changed = self.absent(db, repository, run, attempt, version)
            else:
                changed = self.apply_run(db, repository, document, began, version)
            db.execute(
                "DELETE FROM pending WHERE repository = ? AND kind = 'runs' AND item = ? AND latest <= ?",
                (repository, item, began),
            )
            if changed:
                self.raise_revision(db, repository, "runs", self.clock(), str(run))

    # Reads

    def revisions(self, repositories):
        """`{repository: {kind: {revision, mode, state, verified, built}}}` for the configured ones asked for.

        A kind is `current` once a history check completed in this process, while its verified
        time is recent and no read it owes is overdue. In mode `rows`, an owed or first rebuild
        makes it `rebuilding`, or `failed` when the last attempt failed.
        """
        now = self.clock()
        with self.lock:
            counters = {(r, k): c for r, k, c in self.db.execute("SELECT repository, kind, counter FROM revisions")}
            rows = self.db.execute(
                "SELECT repository, kind, mode, verified, built, wanted, failed FROM coverage"
            ).fetchall()
            oldest = dict(
                ((r, k), t)
                for r, k, t in self.db.execute("SELECT repository, kind, MIN(requested) FROM pending GROUP BY 1, 2")
            )
        found = {}
        for repository, kind, mode, verified, built, wanted, failed in rows:
            if repository not in repositories:
                continue
            overdue = now - oldest.get((repository, kind), now) > FRESH_SECONDS
            if mode == "rows" and (built is None or wanted is not None):
                state = "failed" if failed else "rebuilding"
            elif self.checked and verified is not None and now - verified <= FRESH_SECONDS and not overdue:
                state = "current"
            else:
                state = "catching-up"
            found.setdefault(repository, {})[kind] = {
                "revision": f"{self.incarnation}:{counters.get((repository, kind), 0)}",
                "mode": mode,
                "state": state,
                "verified": stamp(verified),
                "built": stamp(built),
            }
        return found

    def rows(self, repository, kind):
        """The list a kind in mode `rows` holds, with the entry `revisions` reports for it; None otherwise."""
        with self.lock:
            entry = self.revisions({repository}).get(repository, {}).get(kind)
            if entry is None or entry["mode"] != "rows" or kind != "runs":
                return None
            members = self.db.execute(
                "SELECT run FROM run_members WHERE repository = ? ORDER BY created DESC, run DESC LIMIT ?",
                (repository, LISTED_RUNS),
            ).fetchall()
            items = []
            for (run,) in members:
                attempts = self.db.execute(
                    "SELECT attempt, document FROM runs WHERE repository = ? AND run = ? ORDER BY attempt",
                    (repository, run),
                ).fetchall()
                # The latest attempt, never an older one in its place: a run whose latest is gone is not listed.
                if attempts and attempts[-1][1] is not None:
                    first = attempts[0][1] if attempts[0][0] == 1 else None
                    items.append({"run": json.loads(attempts[-1][1]), "first": first and json.loads(first)})
        return {**entry, "items": items}


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
            if path == "/lists":
                found = self.index.rows(request["repository"], request["kind"])
                return (200, found) if found is not None else (404, {"error": "no such list"})
            if path == "/lists/refresh":
                if request["repository"] not in self.index.names.values():
                    return 404, {"error": "repository not configured"}
                self.index.rebaseline(request["repository"])
                return 200, {"refreshed": True}
        except (ValueError, KeyError, TypeError):
            return 400, {"error": "invalid list request"}
        return 404, {"error": "unknown list request"}
