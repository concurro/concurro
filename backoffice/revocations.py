"""Encrypted GitHub logout retry jobs; these contain no provider credentials or task consent."""

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import private
from service.failures import backoff
from service.settings import CHECKOUT


class RevocationError(RuntimeError):
    """Durable logout state is unavailable; the text never includes token material."""


class Revocations:
    """Lease and retry single-access-token revocations without retaining plaintext on disk."""

    def __init__(self, directory: Path, key: bytes, clock=time.time):
        """Use an owner-only state directory and a separately provisioned encryption key."""
        directory = private.outside(directory, (CHECKOUT,))
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        private.directory(directory, (CHECKOUT,))
        self.clock, self.cipher = clock, AESGCM(key)
        self.lock = threading.Lock()
        self.unknown: set[str] = set()  # Failed journal cleanup stays uncertain until its durable marker or restart.
        path = directory / "logout.db"
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise RevocationError("logout state must be an owner-only regular file")
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS logout_meta(id INTEGER PRIMARY KEY,nonce BLOB,sealed BLOB);"
            "CREATE TABLE IF NOT EXISTS logout_groups(id TEXT PRIMARY KEY,waiting INTEGER NOT NULL,"
            "unknown INTEGER NOT NULL,created REAL NOT NULL);"
            "CREATE TABLE IF NOT EXISTS logout_jobs(id TEXT PRIMARY KEY,group_id TEXT NOT NULL,nonce BLOB,sealed BLOB,"
            "state TEXT NOT NULL,attempts INTEGER NOT NULL,due REAL NOT NULL,lease REAL,claim TEXT,updated REAL NOT NULL);"
        )
        try:
            with self.transaction():
                marker = self.db.execute("SELECT nonce,sealed FROM logout_meta WHERE id=1").fetchone()
                if marker:
                    self.cipher.decrypt(marker["nonce"], marker["sealed"], b"concurro-logout-key")
                else:
                    nonce = os.urandom(12)
                    self.db.execute(
                        "INSERT INTO logout_meta VALUES(1,?,?)",
                        (nonce, self.cipher.encrypt(nonce, b"concurro-logout-v1", b"concurro-logout-key")),
                    )
        except (InvalidTag, ValueError, sqlite3.Error):
            self.db.close()
            raise RevocationError("logout state cannot be opened with the configured key") from None
        # A restart cannot recover the outcome of an in-memory, in-flight refresh.
        self.db.execute("UPDATE logout_groups SET waiting=0,unknown=1 WHERE waiting=1")

    @contextmanager
    def transaction(self):
        """Commit one state transition or preserve the previous retry/lease atomically."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def close(self):
        """Close after the retry worker has stopped."""
        with self.lock:
            self.db.close()

    def prepare(self, group: str, token: str, waiting: bool):
        """Persist known token material and the logout/refresh fence before attempting remote I/O."""
        try:
            with self.lock, self.transaction():
                self.db.execute(
                    "INSERT OR IGNORE INTO logout_groups VALUES(?,?,0,?)", (group, int(waiting), self.clock())
                )
                self._append(group, token)
        except (OSError, sqlite3.Error):
            raise RevocationError("logout retry state unavailable") from None

    def _append(self, group: str, token: str):
        """Seal a known token once per logout receipt inside the caller's transaction."""
        identifier = hashlib.sha256((group + "/" + token).encode()).hexdigest()
        nonce = os.urandom(12)
        sealed = self.cipher.encrypt(
            nonce, json.dumps({"token": token}).encode(), ("concurro-logout/" + identifier).encode()
        )
        self.db.execute(
            "INSERT OR IGNORE INTO logout_jobs VALUES(?,?,?,?,'pending',0,?,NULL,NULL,?)",
            (identifier, group, nonce, sealed, self.clock(), self.clock()),
        )

    def refreshed(self, group: str, grant, uncertain: bool):
        """Retain a late issued token after local logout, without resurrecting its browser session."""
        try:
            with self.lock, self.transaction():
                if grant:
                    self._append(group, grant.token)
                self.db.execute(
                    "UPDATE logout_groups SET waiting=0,unknown=MAX(unknown,?) WHERE id=?", (int(uncertain), group)
                )
        except (OSError, sqlite3.Error):
            raise RevocationError("logout retry state unavailable") from None

    def uncertain(self, group: str):
        """Latch failed late-pair journalling as unknown, persisting the marker when storage permits."""
        with self.lock:
            self.unknown.add(group)
            try:
                with self.transaction():
                    self.db.execute("UPDATE logout_groups SET waiting=0,unknown=1 WHERE id=?", (group,))
            except (OSError, sqlite3.Error):
                # Restart independently turns a still-waiting refresh into unknown.
                pass

    def status(self, group: str | None):
        """Expose only anonymous receipt status, never token values, hashes or account identities."""
        with self.lock:
            if group in self.unknown:
                return {"remote": "unknown"}
            row = self.db.execute("SELECT waiting,unknown FROM logout_groups WHERE id=?", (group or "",)).fetchone()
            if row is None:
                return {"remote": "none"}
            pending = self.db.execute(
                "SELECT 1 FROM logout_jobs WHERE group_id=? AND state='pending' LIMIT 1", (group,)
            ).fetchone()
        if row["waiting"] or pending:
            return {"remote": "pending"}
        return {"remote": "unknown" if row["unknown"] else "revoked"}

    def drain(self, oauth, group=None):
        """Attempt due jobs under expiring leases; a stale worker cannot complete a newer claim."""
        for _ in range(100):
            with self.lock, self.transaction():
                query = "SELECT * FROM logout_jobs WHERE state='pending' AND due<=? AND (lease IS NULL OR lease<=?)"
                parameters = [self.clock(), self.clock()]
                if group is not None:
                    query += " AND group_id=?"
                    parameters.append(group)
                row = self.db.execute(query + " ORDER BY due,id LIMIT 1", parameters).fetchone()
                if row is None:
                    return
                claim = secrets.token_hex(16)
                self.db.execute(
                    "UPDATE logout_jobs SET lease=?,claim=? WHERE id=?", (self.clock() + 60, claim, row["id"])
                )
            broken = False
            try:
                token = json.loads(
                    self.cipher.decrypt(row["nonce"], row["sealed"], ("concurro-logout/" + row["id"]).encode())
                )["token"]
                if not isinstance(token, str) or not token:
                    raise ValueError("invalid encrypted job")
            except (InvalidTag, ValueError, KeyError, TypeError):
                broken = True
            done = False if broken else oauth.revoke(token)
            with self.lock, self.transaction():
                if broken:
                    changed = self.db.execute(
                        "UPDATE logout_jobs SET state='unknown',nonce=NULL,sealed=NULL,lease=NULL,claim=NULL,updated=?"
                        " WHERE id=? AND claim=?",
                        (self.clock(), row["id"], claim),
                    ).rowcount
                    if changed:
                        self.db.execute("UPDATE logout_groups SET unknown=1 WHERE id=?", (row["group_id"],))
                elif done:
                    self.db.execute(
                        "UPDATE logout_jobs SET state='done',nonce=NULL,sealed=NULL,lease=NULL,claim=NULL,updated=?"
                        " WHERE id=? AND claim=?",
                        (self.clock(), row["id"], claim),
                    )
                else:
                    self.db.execute(
                        "UPDATE logout_jobs SET attempts=attempts+1,due=?,lease=NULL,claim=NULL,updated=?"
                        " WHERE id=? AND claim=?",
                        (self.clock() + backoff(row["attempts"]), self.clock(), row["id"], claim),
                    )
