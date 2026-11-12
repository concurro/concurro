"""Per-turn run diagnostics: a SQLite index beside transcript archives stored by SHA-256.

The service writes; the backoffice reads. Recording is best effort: a failure is logged and
never affects the turn it describes.
"""

import base64
import binascii
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

from bundle import Bundle
from prompts import prompt_for

log = logging.getLogger("concurro.diagnostics")

DATABASE = "diagnostics.sqlite3"
BLOBS = "blobs"
SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    repository TEXT NOT NULL,
    issue INTEGER NOT NULL,
    generation INTEGER,
    stage TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    effort TEXT,
    agent TEXT,
    session_scope TEXT,
    session_turn INTEGER,
    evaluation TEXT,
    bundle_id TEXT,
    submitted REAL NOT NULL,
    created REAL,
    started REAL,
    finished REAL,
    recorded REAL,
    status TEXT NOT NULL,
    outcome TEXT,
    termination_reason TEXT,
    elapsed_seconds REAL,
    runner_sessions TEXT NOT NULL DEFAULT '[]',
    request TEXT NOT NULL,
    prompt TEXT,
    feedback TEXT,
    spec TEXT,
    response TEXT,
    workspace TEXT,
    transcript TEXT,
    transcript_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS turns_task ON turns (repository, issue, submitted);
"""
# Request fields kept out of the record: endpoint configuration, and bundle texts the prompt already holds.
OMITTED = ("gateway", "charter", "instruction_texts")


def connect(root, readonly=False):
    """Open the diagnostics database; the writer creates its schema."""
    path = Path(root) / DATABASE
    if readonly:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
    db.row_factory = sqlite3.Row
    return db


def blob_path(root, sha):
    """Where an archive with this SHA-256 is stored."""
    return Path(root) / BLOBS / sha[:2] / sha


def encoded(value):
    """JSON text for a stored document, or None."""
    return None if value is None else json.dumps(value, sort_keys=True)


def prompt(request, feedback):
    """The prompt the runner builds (`prompts.prompt_for`), from the request's own bundle texts.

    Session turns after the first send only the changed context; the transcript shows what was sent.
    """
    bundle = Bundle(charter_text=request["charter"], instruction_texts=request.get("instruction_texts") or {})
    if bundle.identity != request.get("bundle_id"):
        return None
    return json.loads(prompt_for({**request, "feedback_directory": bool(feedback)}, bundle))


class Store:
    """The writer's side of the diagnostics directory."""

    def __init__(self, root, clock=time.time):
        self.root, self.clock = Path(root), clock
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.db = connect(self.root)
        self.lock = threading.Lock()

    def close(self):
        self.db.close()

    def put_blob(self, archive):
        """Store a base64 archive once by its SHA-256; return (sha, size)."""
        data = base64.b64decode(archive, validate=True)
        sha = hashlib.sha256(data).hexdigest()
        path = blob_path(self.root, sha)
        if not path.exists():
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_bytes(data)
            os.replace(temporary, path)
        return sha, len(data)

    def submitted(self, identifier, turn, task):
        """Record a turn when it is first submitted."""
        request = turn["request"]
        session = request.get("session") or {}
        try:
            reconstructed = prompt(request, turn.get("feedback"))
        except Exception as error:  # A prompt that cannot be rebuilt still leaves the request.
            log.warning("diagnostics: prompt for turn %s not reconstructed (%s)", identifier, type(error).__name__)
            reconstructed = None
        row = {
            "id": identifier,
            "repository": task["repository"],
            "issue": task["issue"],
            "generation": task.get("generation"),
            "stage": request["stage"],
            "provider": request.get("provider"),
            "model": request.get("model"),
            "effort": request.get("effort"),
            "agent": encoded(request.get("agent")),
            "session_scope": session.get("scope"),
            "session_turn": session.get("turn"),
            "evaluation": request.get("evaluation"),
            "bundle_id": request.get("bundle_id"),
            "submitted": self.clock(),
            "status": "queued",
            "request": encoded({k: v for k, v in request.items() if k not in OMITTED}),
            "prompt": encoded(reconstructed),
            "feedback": encoded(turn.get("feedback") or None),
            "spec": encoded(turn.get("spec")),
        }
        columns = ", ".join(row)
        with self.lock:
            self.db.execute(
                f"INSERT OR IGNORE INTO turns ({columns}) VALUES ({', '.join('?' * len(row))})", tuple(row.values())
            )

    def observed(self, identifier, status, timing, sessions):
        """Record an exchange observation: status, timings and the runner sessions staffed for it."""
        timing = timing or {}
        with self.lock:
            self.db.execute(
                "UPDATE turns SET status = ?, created = coalesce(?, created), started = coalesce(?, started), "
                "finished = coalesce(?, finished), runner_sessions = ? WHERE id = ?",
                (
                    status,
                    timing.get("created"),
                    timing.get("started"),
                    timing.get("finished"),
                    json.dumps(sorted(sessions)),
                    identifier,
                ),
            )

    def finished(self, identifier, result):
        """Record a turn's result; its transcript archive is stored once by SHA-256."""
        archive = result.get("retained") or result.get("transcript")
        sha = size = None
        if archive is not None:
            try:
                sha, size = self.put_blob(archive)
            except (binascii.Error, ValueError, OSError) as error:
                log.warning("diagnostics: transcript for turn %s not stored (%s)", identifier, type(error).__name__)
        elapsed = result.get("elapsed_seconds")
        with self.lock:
            self.db.execute(
                "UPDATE turns SET outcome = ?, response = ?, workspace = ?, termination_reason = ?, "
                "elapsed_seconds = ?, transcript = ?, transcript_bytes = ?, recorded = ? WHERE id = ?",
                (
                    result.get("outcome"),
                    encoded(result.get("response")),
                    encoded(result.get("workspace")),
                    result.get("termination_reason"),
                    elapsed if type(elapsed) in (int, float) else None,
                    sha,
                    size,
                    self.clock(),
                    identifier,
                ),
            )


class Recorder:
    """Bind a store to one task; every call is best effort."""

    def __init__(self, store, task):
        self.store, self.task = store, task

    def _safely(self, name, *args):
        try:
            getattr(self.store, name)(*args)
        except Exception as error:
            log.warning("diagnostics: %s for turn %s not recorded (%s)", name, args[0], type(error).__name__)

    def submitted(self, identifier, turn):
        self._safely("submitted", identifier, turn, self.task)

    def observed(self, identifier, status, timing, sessions):
        self._safely("observed", identifier, status, timing, sessions)

    def finished(self, identifier, result):
        self._safely("finished", identifier, result)
