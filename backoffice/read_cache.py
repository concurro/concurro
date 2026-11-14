"""Persistent, rebuildable issue and list snapshots with coalesced background GitHub refreshes."""

import json
import logging
import math
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from checkpoints import CheckpointError
from github import NotFound, Transient
from policy import Blocked

log = logging.getLogger("concurro.backoffice")
FRESH_SECONDS = 60
RETRY_SECONDS = 60
COMMAND_SECONDS = 5
# Prefix of the `issue` key of a repository-wide list read, which a command on any of its issues outdates.
LISTING = "list:"
INCOMPLETE = "The latest refresh was incomplete."


class ReadCache:
    """One API process owns refreshes; SQLite retains verified display snapshots across restarts."""

    def __init__(self, root: Path, clock=time.time):
        """Open the display database independently of the service's scheduling registry."""
        root.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.lock = threading.RLock()
        self.pending = {}
        self.closed = False
        self.db = sqlite3.connect(root / "issue-reads.sqlite3", isolation_level=None, check_same_thread=False)
        try:
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("""CREATE TABLE IF NOT EXISTS snapshots (
                scope TEXT NOT NULL, repository TEXT NOT NULL, issue TEXT NOT NULL,
                data TEXT, observed REAL, due REAL NOT NULL DEFAULT 0, error TEXT,
                error_kind TEXT, error_code TEXT, settling TEXT, settle_until REAL,
                epoch INTEGER NOT NULL DEFAULT 0, revision TEXT, PRIMARY KEY(scope, repository, issue))""")
            if "revision" not in {column[1] for column in self.db.execute("PRAGMA table_info(snapshots)")}:
                self.db.execute("ALTER TABLE snapshots ADD COLUMN revision TEXT")
        except sqlite3.Error:
            self.db.close()
            raise
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="issue-refresh")

    def close(self):
        """Drain refreshes before closing their database connection."""
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=True)
        self.db.close()

    def read(self, scope, repository, issue, load, *, refresh=False, revision=None, saved=None):
        """Return saved data immediately; cold and explicit refreshes share one read and respect cooldowns.

        Freshness is by age unless the caller knows better. With `revision`, the source's revision
        as read before this call, saved data is fresh until that revision moves or the entry is
        invalidated here. With `saved`, the reason the source cannot be asked, nothing is refreshed:
        saved data is returned flagged with that reason, and `LookupError` raised when there is none.
        """
        key = (scope, repository.lower(), str(issue))
        while True:
            with self.lock:
                if self.closed:
                    raise RuntimeError("issue read cache is closed")
                self.db.execute("INSERT OR IGNORE INTO snapshots(scope, repository, issue) VALUES (?, ?, ?)", key)
                row = self.db.execute(
                    "SELECT * FROM snapshots WHERE scope = ? AND repository = ? AND issue = ?", key
                ).fetchone()
                now = self.clock()
                future, epoch = self.pending.get(key, (None, row["epoch"]))
                retrying = bool(row["error"] and row["due"] > now)
                # A revision other than the one saved with the data also covers switching freshness rules.
                due = row["due"] <= now or row["revision"] != revision
                if saved is not None:
                    future, refresh, retrying = None, False, False
                elif future is None and not retrying and (refresh or due):
                    future = self.pool.submit(self.update, key, epoch, load, revision)
                    self.pending[key] = (future, epoch)
                settling = row["settling"] is not None and row["settle_until"] > now
                if row["data"] is not None and (not refresh or retrying):
                    return json.loads(row["data"]), {
                        "observedAt": datetime.fromtimestamp(row["observed"], UTC).isoformat(),
                        "stale": bool(row["error"] or due or settling or saved),
                        "refreshing": future is not None,
                        "pendingCommand": settling,
                        "error": row["error"] or saved,
                    }
                if saved is not None:
                    raise LookupError("nothing saved")
                if retrying and future is None:
                    if row["error_kind"] == "missing":
                        raise NotFound("Issue snapshot is no longer available")
                    if row["error_kind"] == "checkpoint":
                        raise CheckpointError(row["error_code"])
                    if row["error_kind"] == "blocked":
                        raise Blocked(row["error"])
                    raise Transient(row["error_code"], retry_after=max(1, row["due"] - now))
            try:
                future.result()
            except Exception:
                with self.lock:
                    current = self.db.execute(
                        "SELECT epoch FROM snapshots WHERE scope = ? AND repository = ? AND issue = ?", key
                    ).fetchone()
                if current is not None and current["epoch"] != epoch:
                    continue
                raise
            with self.lock:
                current = self.db.execute(
                    "SELECT epoch FROM snapshots WHERE scope = ? AND repository = ? AND issue = ?", key
                ).fetchone()
            if current is not None and current["epoch"] != epoch:
                continue
            refresh = False

    def update(self, key, epoch, load, revision=None):
        """Publish verified reads; command invalidation rejects late data but preserves GitHub retry deadlines.

        A read marked `degraded` is retried like a failed one: it never replaces a complete saved
        copy, and is saved only when there is nothing better to show.
        """
        try:
            value = load()
            encoded = json.dumps(value)
            with self.lock:
                row = self.db.execute(
                    "SELECT * FROM snapshots WHERE scope = ? AND repository = ? AND issue = ? AND epoch = ?",
                    (*key, epoch),
                ).fetchone()
                if row is None:
                    return
                now = self.clock()
                baseline = json.loads(row["settling"]) if row["settling"] is not None else None
                settling = (
                    baseline is not None
                    and row["settle_until"] > now
                    and baseline.get("state", baseline) == value.get("state", value)
                )
                degraded = bool(value.get("degraded"))
                if degraded and row["data"] is not None and not json.loads(row["data"]).get("degraded"):
                    self.db.execute(
                        "UPDATE snapshots SET due = ?, error = ?, error_kind = 'internal', error_code = NULL "
                        "WHERE scope = ? AND repository = ? AND issue = ? AND epoch = ?",
                        (now + RETRY_SECONDS, INCOMPLETE, *key, epoch),
                    )
                    return
                # Data saved against a revision never ages; only that revision or an invalidation outdates it.
                fresh = COMMAND_SECONDS if settling else FRESH_SECONDS if revision is None else math.inf
                self.db.execute(
                    "UPDATE snapshots SET data = ?, observed = ?, due = ?, error = ?, error_kind = ?, "
                    "error_code = NULL, settling = ?, revision = ? "
                    "WHERE scope = ? AND repository = ? AND issue = ? AND epoch = ?",
                    (
                        encoded,
                        now,
                        now + (RETRY_SECONDS if degraded else fresh),
                        INCOMPLETE if degraded else None,
                        "internal" if degraded else None,
                        row["settling"] if settling else None,
                        revision,
                        *key,
                        epoch,
                    ),
                )
                # Retire superseded configurations after a successful read, preserving active refreshes.
                others = self.db.execute(
                    "SELECT scope FROM snapshots WHERE repository = ? AND issue = ? AND scope != ?",
                    (key[1], key[2], key[0]),
                ).fetchall()
                for other in others:
                    previous = (other["scope"], key[1], key[2])
                    if previous not in self.pending:
                        self.db.execute(
                            "DELETE FROM snapshots WHERE scope = ? AND repository = ? AND issue = ?", previous
                        )
        except sqlite3.Error:
            raise
        except Exception as error:
            kind, code, delay = "internal", None, RETRY_SECONDS
            message = "The latest issue refresh failed."
            if isinstance(error, Transient):
                kind, code, delay, message = (
                    "transient",
                    error.code,
                    max(RETRY_SECONDS, error.retry_after or 0),
                    str(error),
                )
            elif isinstance(error, NotFound):
                kind, delay, message = "missing", RETRY_SECONDS, "Issue snapshot is no longer available."
            elif isinstance(error, CheckpointError):
                kind, code, delay, message = "checkpoint", error.code, 2, error.public_detail()
            elif isinstance(error, Blocked):
                kind, delay, message = "blocked", 2, str(error)
            with self.lock:
                # A GitHub retry delay still applies if a command arrived while this read was in flight.
                self.db.execute(
                    "UPDATE snapshots SET due = ?, error = ?, error_kind = ?, error_code = ?, "
                    "data = CASE WHEN ? THEN NULL ELSE data END "
                    "WHERE scope = ? AND repository = ? AND issue = ? AND (epoch = ? OR ?)",
                    (self.clock() + delay, message, kind, code, kind == "missing", *key, epoch, kind == "transient"),
                )
            log.warning(
                "issue snapshot refresh failed for %s#%s (%s%s)",
                key[1],
                key[2],
                type(error).__name__,
                f": {code}" if code else "",
            )
            raise
        finally:
            with self.lock:
                self.pending.pop(key, None)

    def outdate(self, prefix, repositories=None):
        """Make saved entries whose key starts with `prefix` due and reject refreshes already in flight."""
        try:
            with self.lock:
                for repository in [None] if repositories is None else repositories:
                    self.db.execute(
                        "UPDATE snapshots SET epoch = epoch + 1, due = CASE WHEN error_kind = 'transient' THEN due "
                        "ELSE 0 END WHERE substr(issue, 1, ?) = ? AND (? IS NULL OR repository = ?)",
                        (len(prefix), prefix, repository, repository and repository.lower()),
                    )
        except sqlite3.Error:
            log.warning("issue read cache invalidation unavailable")

    def invalidate(self, repository, issue):
        """Mark command-adjacent data stale, retain fallback content, and reject pre-command refreshes."""
        try:
            with self.lock:
                self.db.execute(
                    "UPDATE snapshots SET epoch = epoch + 1, settling = data, settle_until = ?, "
                    "due = CASE WHEN error_kind = 'transient' THEN due ELSE 0 END, "
                    "error = CASE WHEN error_kind = 'transient' THEN error ELSE NULL END, "
                    "error_kind = CASE WHEN error_kind = 'transient' THEN error_kind ELSE NULL END "
                    "WHERE repository = ? AND (issue = ? OR substr(issue, 1, ?) = ?)",
                    (self.clock() + FRESH_SECONDS, repository.lower(), str(issue), len(LISTING), LISTING),
                )
        except sqlite3.Error:
            log.warning("issue read cache invalidation unavailable; command delivery is unaffected")
