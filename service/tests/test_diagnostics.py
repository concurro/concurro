"""Run diagnostics are recorded per turn, outside the registry, and never affect the turn."""

import base64
import hashlib
import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

import fixtures  # noqa: F401 -- initializes the service and engine test paths

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

from agents import RunnerExecutor
from bundle import Bundle
from prompts import prompt_for
from runner_fixtures import GATEWAY, TASK, TURN, FakeOperator
from service import diagnostics
from service.settings import CHECKOUT

TURN_ID = "12345678-1234-1234-1234-123456789abc"
ARCHIVE = b"archive bytes"


def bundled_turn():
    """A plan turn carrying the example target's bundle texts, as `Agents.prepare` builds it."""
    bundle = Bundle(target=CHECKOUT / "examples" / "stacks-core")
    turn = deepcopy(TURN)
    commit = "a" * 40
    turn["request"].update(
        gateway=GATEWAY,
        bundle_id=bundle.identity,
        charter=bundle.charter_text,
        instruction_texts=bundle.instruction_texts,
        effort="high",
        session={"scope": "b" * 64, "turn": 2},
        context={
            "base": commit,
            "repository_commit": commit,
            "repository": {"refs": {"main": commit}, "head": "main"},
        },
    )
    turn["feedback"] = {"index.json": "{}"}
    return turn


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "diagnostics"
        self.store = diagnostics.Store(self.root, clock=lambda: 100.0)
        self.addCleanup(self.store.close)

    def row(self):
        db = diagnostics.connect(self.root, readonly=True)
        self.addCleanup(db.close)
        return db.execute("SELECT * FROM turns WHERE id = ?", (TURN_ID,)).fetchone()

    def test_records_a_turn_from_submission_to_result(self):
        turn = bundled_turn()
        self.store.submitted(TURN_ID, turn, TASK)
        self.store.observed(TURN_ID, "running", {"created": 1.0, "started": 2.0, "finished": None}, {"s-2", "s-1"})
        encoded = base64.b64encode(ARCHIVE).decode()
        self.store.finished(TURN_ID, {"outcome": "ok", "response": {"answer": 1}, "retained": encoded})
        row = self.row()
        self.assertEqual((row["repository"], row["issue"], row["stage"]), ("up/project", 7, "plan"))
        self.assertEqual((row["session_scope"], row["session_turn"], row["effort"]), ("b" * 64, 2, "high"))
        self.assertEqual((row["status"], row["created"], row["started"]), ("running", 1.0, 2.0))
        self.assertEqual(json.loads(row["runner_sessions"]), ["s-1", "s-2"])
        self.assertEqual((row["outcome"], json.loads(row["response"])), ("ok", {"answer": 1}))
        request = json.loads(row["request"])
        self.assertFalse({"gateway", "charter", "instruction_texts"} & set(request))
        prompt = json.loads(row["prompt"])
        bundle = Bundle(charter_text=turn["request"]["charter"], instruction_texts=turn["request"]["instruction_texts"])
        self.assertEqual(prompt, json.loads(prompt_for({**turn["request"], "feedback_directory": True}, bundle)))
        self.assertIn("feedback_files", prompt)
        self.assertEqual(prompt["instructions"]["role"], "plan")
        sha = hashlib.sha256(ARCHIVE).hexdigest()
        self.assertEqual((row["transcript"], row["transcript_bytes"]), (sha, len(ARCHIVE)))
        self.assertEqual(diagnostics.blob_path(self.root, sha).read_bytes(), ARCHIVE)

    def test_a_mismatched_bundle_keeps_the_request_without_a_prompt(self):
        turn = bundled_turn()
        turn["request"]["bundle_id"] = "other"
        self.store.submitted(TURN_ID, turn, TASK)
        row = self.row()
        self.assertIsNone(row["prompt"])
        self.assertEqual(json.loads(row["request"])["bundle_id"], "other")

    def test_a_fresh_turn_transcript_is_stored(self):
        self.store.submitted(TURN_ID, deepcopy(TURN), TASK)
        encoded = base64.b64encode(ARCHIVE).decode()
        self.store.finished(TURN_ID, {"outcome": "failed", "response": None, "retained": None, "transcript": encoded})
        self.assertEqual(self.row()["transcript"], hashlib.sha256(ARCHIVE).hexdigest())

    def test_old_database_migrates_and_preserves_the_assigned_runner(self):
        """Opening a pre-run-identity database retains old turns and records reused sessions."""
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db = sqlite3.connect(root / diagnostics.DATABASE)
        db.executescript(diagnostics.SCHEMA.replace("    runner TEXT,\n", ""))
        db.execute(
            "INSERT INTO turns (id, repository, issue, stage, submitted, status, request) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (TURN_ID, "owner/project", 7, "plan", 1.0, "queued", "{}"),
        )
        db.commit()
        db.close()
        store = diagnostics.Store(root)
        self.addCleanup(store.close)
        store.submitted(TURN_ID, deepcopy(TURN), TASK)
        runner = {"session": TURN_ID, "run_id": "77/1", "secret": "never record"}
        store.observed(TURN_ID, "running", {}, set(), runner)
        store.observed(TURN_ID, "done", {}, set())
        row = store.db.execute("SELECT * FROM turns").fetchone()
        self.assertEqual(json.loads(row["runner"]), {"session": TURN_ID, "run_id": "77/1"})
        self.assertEqual(json.loads(row["runner_sessions"]), [])
        self.assertEqual((row["repository"], row["submitted"]), ("owner/project", 1.0))

    def test_recorder_failures_never_reach_the_turn(self):
        recorder = diagnostics.Recorder(self.store, TASK)
        self.store.close()
        with self.assertLogs("concurro.diagnostics", "WARNING"):
            recorder.submitted(TURN_ID, deepcopy(TURN))
            recorder.finished(TURN_ID, {"outcome": "ok", "retained": "not base64!"})


class ExecutorRecordingTests(unittest.TestCase):
    """The executor reports submission, observations and the result it returns."""

    def test_executor_records_each_turn(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = diagnostics.Store(root)
        self.addCleanup(store.close)
        observation = {"turn": TURN_ID, "status": "queued", "created": 5.0, "dispatch": "s-1"}
        result = {"outcome": "ok", "response": {"done": True}, "retained": None, "workspace": None}
        operator = FakeOperator(
            [
                observation,
                {
                    **observation,
                    "status": "done",
                    "started": 6.0,
                    "finished": 9.0,
                    "result": result,
                    "runner": {"session": TURN_ID, "run_id": "77/1"},
                },
            ]
        )
        executor = RunnerExecutor(
            operator,
            GATEWAY,
            {},
            ["claude-*"],
            5,
            lambda session, labels: None,
            task=TASK,
            recorder=diagnostics.Recorder(store, TASK),
        )
        handle = executor.submit(deepcopy(TURN), identifier=TURN_ID)
        self.assertEqual(executor.observe(handle), result)
        row = store.db.execute("SELECT * FROM turns").fetchone()
        self.assertEqual((row["id"], row["status"], row["outcome"]), (TURN_ID, "done", "ok"))
        self.assertEqual((row["created"], row["started"], row["finished"]), (5.0, 6.0, 9.0))
        self.assertEqual(json.loads(row["runner_sessions"]), ["s-1"])
        self.assertEqual(json.loads(row["runner"]), {"session": TURN_ID, "run_id": "77/1"})


if __name__ == "__main__":
    unittest.main()
