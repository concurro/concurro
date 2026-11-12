"""Native workspaces and deterministic runner/exchange stand-ins without test cases."""

import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from support import CONFIG

import repository
import turns
from agents import RunnerExecutor
from gitfixture import Upstream, mirror_of
from relay import ExchangeError

GATEWAY = {"url": "https://gateway.invalid/concurro", "headers": {"codex": {"x-concurro-plan": "chatgpt"}}}


IMAGE = "sha256:" + "a" * 64


MODELS = {**CONFIG["models"], "codex": "gpt-test", "claude": "claude-test"}


# Reads the request, edits the workspace and answers as the native entry point does.
AGENT = """
import json, os, pathlib, sys
root = pathlib.Path(sys.argv[1])
request = json.load(sys.stdin)
assert os.environ["CONCURRO_GATEWAY_KEY"] == "sk-agw-test" and "GH_TOKEN" not in os.environ
assert request["workspace"] == str(root / "work") and request["timeout"] == (None if request["stage"] == "develop" else 1800)
if request.get("feedback_directory"):
    feedback = pathlib.Path(request["feedback_directory"])
    assert feedback.parent == root and feedback.name == "feedback"
    assert not str(feedback).startswith(str(root / "work"))
    (root / "scratch/feedback-read.json").write_text(json.dumps(
        [json.loads(path.read_text()) for path in feedback.glob("*.json")]))
work = root / "work"
(work / "docs/guide.md").write_text("Guide\\nEdited\\n")
(work / "docs/new.md").write_text("New\\n")
(work / "target").mkdir()
(work / "target/build.out").write_text("ignored")
if request.get("session"):
    (root / "retained/turn.json").write_text(json.dumps({"turn": request["session"]["turn"]}))
print(json.dumps({"summary": "done", "checks": [], "design_issue": None}))
"""


class Workspace:
    """A real upstream and mirror, and a turn directory prepared the way `turns.execute` prepares one."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name).resolve()
        self.upstream = Upstream(self.tmp / "upstream.git")
        self.base = self.upstream.commit(
            {"docs/guide.md": "Guide\n", "src/lib.rs": "fn main() {}\n", ".gitignore": "target/\n"},
            branch="main",
            message="base",
        )
        self.mirror = mirror_of(self.upstream, self.tmp / "mirrors")
        self.spec = {"refs": {"main": self.base}, "head": "main", "base_branch": "main", "candidate": None}

    def prepare(self, candidate=None):
        root = turns.directory(self.tmp / "turns")
        (root / "work").mkdir()
        self.mirror.borrow(root.name, "main", [self.base])
        repository.snapshot({**self.spec, "candidate": candidate}, root=root / "work", mirror=self.mirror.path)
        shutil.copytree(root / "work/.git", root / "pristine.git", symlinks=True)
        return root


class FakeExecutor:
    """Record submitted turns and return a configured result."""

    gateway = GATEWAY

    def __init__(self, result=None):
        self.turns, self.result = [], result or {"outcome": "ok", "response": {"ok": True}}

    def run(self, turn, label):
        self.turns.append((turn, label))
        return self.result


class FakeOperator:
    """Replay scripted exchange replies while recording controller requests."""

    def __init__(self, replies=()):
        self.replies, self.calls = list(replies), []

    def mint(self, *args):
        self.calls.append(("mint", args))
        return {"session": "s-1", "key": "sk-agw-host"}

    def end(self, session):
        self.calls.append(("end", session))

    def submit(self, *args):
        self.calls.append(("submit", args))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def poll(self, turn, wait=None):
        self.calls.append(("poll", turn))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def cancel(self, turn):
        self.calls.append(("cancel", turn))


TASK = {
    "repository": "up/project",
    "issue": 7,
    "generation": 1,
    "config_sha": "b" * 40,
    "upstream": "up/project",
    "fork": "owner/project",
    "engine": "e" * 40,
}


TURN = {
    "request": {"model": "claude-test", "provider": "claude", "stage": "plan", "context": {"base": "a" * 40}},
    "spec": {},
    "retained": None,
    "timeout": 600,
}


class FakeRunner:
    """Serve queued batches and record acknowledged turn results."""

    def __init__(self, batches):
        self.batches, self.completed = list(batches), []

    def next(self, capacity, wait):
        if capacity == 0:
            return []
        if not self.batches:
            raise ExchangeError(410, "session has ended")
        batch = self.batches.pop(0)
        return batch[:capacity]

    def complete(self, turn, result):
        self.completed.append((turn, result))


class ExecutorFixture:
    """Build a runner executor with a scripted operator and deterministic clock."""

    def runner(self, replies, dispatch=None, clock=None):
        operator = FakeOperator(replies)
        started = []
        identity = patch("agents.uuid.uuid4", return_value="t")
        identity.start()
        self.addCleanup(identity.stop)
        executor = RunnerExecutor(
            operator,
            GATEWAY,
            {"repository_id": "1"},
            ["claude-*"],
            5000,
            dispatch or (lambda session, labels: started.append(session)),
            clock=clock or (lambda: 0),
            sleep=lambda _: None,
            task=TASK,
        )
        return executor, operator, started
