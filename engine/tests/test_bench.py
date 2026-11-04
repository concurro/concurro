"""The trusted workbench on a runner session: the runner's side, the controller's handle, and both over the exchange."""

import io
from pathlib import Path
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from support import setUpModule, tearDownModule  # unittest module fixtures
from test_turns import IMAGE, Workspace
from agents import RunnerExecutor
from bench import BenchLost, Benches, RemoteBench
from policy import Blocked
from relay import Operator, Runner
import runner
import turns

CONFIG = {"base": "main"}


class FakeLocal:
    """Stands in for `LocalWorkspace`: reads its archives, records its calls."""

    def __init__(self, config, archive, branch, execution=None, base_archive=None):
        self.config, self.branch, self.execution = config, branch, execution
        self.source, self.base = self.read(archive), self.read(base_archive)
        self.calls, self.closed = [], False

    @staticmethod
    def read(archive):
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            roots = {Path(m.name).parts[0] for m in bundle.getmembers()}
            assert roots == {"source"}, roots
            return {m.name.removeprefix("source/"): bundle.extractfile(m).read().decode()
                    for m in bundle.getmembers() if m.isfile()}

    def __enter__(self):
        return self

    def files(self, paths):
        return {p: self.source.get(p) for p in paths}

    def prepare(self, selected, timeout):
        self.calls.append(("prepare", selected, timeout))

    def checkpoint(self, edits):
        self.source.update({e["path"]: e["content"] for e in edits})
        return {"local_commit": "c1", "tree": "t1", "branch": self.branch}

    def scope(self, packages):
        self.calls.append(("scope", packages))

    def configure_validation(self, spec, originals, scope):
        self.calls.append(("validation", spec))

    def run_checks(self, required, requested, deadline):
        self.calls.append(("checks", round(deadline - time.monotonic())))
        if "boom" in requested:
            raise Blocked("agent requested an unknown local check")
        return [{"name": "test", "exit_code": 0}]

    def export(self):
        return {"edits": [{"path": "docs/guide.md", "content": self.source["docs/guide.md"]}]}

    def close(self):
        self.closed = True


class BenchesTests(Workspace):
    """The runner's side: workbenches from its mirror, one per ID, each call answered or refused."""

    def call(self, benches, op, args, bench="a" * 32):
        return benches.handle({"kind": "bench", "bench": bench, "op": op, "args": args})["response"]

    def test_a_workbench_is_opened_from_the_mirror_and_released_on_close(self):
        benches = Benches("session-1", self.mirror, factory=FakeLocal)
        opening = {"config": CONFIG, "parent": self.base, "base": self.base, "branch": "concurro/7", "execution": {}}
        self.assertEqual(self.call(benches, "open", opening), {"value": {"session": "session-1"}})
        workspace = benches.open["a" * 32]
        self.assertEqual(workspace.source["docs/guide.md"], "Guide\n")
        self.assertEqual(workspace.base, workspace.source)
        self.assertEqual(self.call(benches, "files", {"paths": ["docs/guide.md"]}), {"value": {"docs/guide.md": "Guide\n"}})
        self.call(benches, "run_checks", {"required": ["test"], "requested": [], "seconds": 300})
        self.assertEqual(workspace.calls[-1], ("checks", 300), "the deadline is the controller's, in this clock")
        pins = self.mirror.git("for-each-ref", "refs/").stdout.decode()
        self.assertIn("bench-" + "a" * 32, pins)
        self.assertEqual(self.call(benches, "close", {}), {"value": None})
        self.assertTrue(workspace.closed)
        self.assertNotIn("bench-", self.mirror.git("for-each-ref", "refs/").stdout.decode())

    def test_calls_are_refused_with_their_reason(self):
        benches = Benches("session-1", self.mirror, factory=FakeLocal)
        self.assertIn("unknown workbench", self.call(benches, "files", {"paths": []})["error"])
        self.assertIn("invalid workbench call", self.call(benches, "files", {}, bench="../x")["error"])
        self.assertIn("invalid workbench call", self.call(benches, "shell", {})["error"])
        self.assertIn("full SHAs", self.call(benches, "open", {"config": CONFIG, "parent": "main", "base": "main",
                                                               "branch": "b", "execution": {}})["error"])


class FakeExecutor:
    """Routes workbench turns straight to a runner's `Benches`, recording each pin."""

    def __init__(self, benches, outcome="ok"):
        self.benches, self.outcome, self.pins = benches, outcome, []

    def run(self, turn, label, pin=None):
        self.pins.append((turn["op"], pin))
        if self.outcome != "ok":
            return {"outcome": self.outcome, "response": None}
        return self.benches.handle(turn)


class RemoteTests(Workspace):
    """The controller's handle: the `LocalWorkspace` interface, pinned to the session that opened it."""

    def test_calls_after_opening_are_pinned_and_errors_are_refusals(self):
        executor = FakeExecutor(Benches("session-1", self.mirror, factory=FakeLocal))
        with RemoteBench(executor)(CONFIG, self.base, "concurro/7", {}, self.base) as workspace:
            self.assertEqual(workspace.files(["docs/guide.md"]), {"docs/guide.md": "Guide\n"})
            workspace.checkpoint([{"path": "docs/guide.md", "content": "New\n"}])
            self.assertEqual(workspace.run_checks(["test"], [], time.monotonic() + 120), [{"name": "test", "exit_code": 0}])
            with self.assertRaisesRegex(Blocked, "unknown local check"):
                workspace.run_checks(["test"], ["boom"], time.monotonic() + 120)
            self.assertEqual(workspace.export()["edits"][0]["content"], "New\n")
        self.assertEqual(executor.pins[0], ("open", None))
        self.assertEqual({pin for _, pin in executor.pins[1:]}, {"session-1"})
        self.assertEqual(executor.pins[-1], ("close", "session-1"))
        self.assertEqual(executor.benches.open, {})

    def test_a_lost_workbench_is_transient(self):
        for outcome in ("lost", "uncertain", "failed"):
            with self.subTest(outcome=outcome), self.assertRaises(BenchLost):
                RemoteBench(FakeExecutor(None, outcome))(CONFIG, self.base, "b", {}, self.base).__enter__()


class OverExchangeTests(Workspace):
    """A workbench opened, used and closed through the real exchange and a runner loop."""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gateway/tests"))
        import test_exchange
        import exchange as exchange_module
        self.signer = test_exchange.Signer(str(self.tmp))
        self.exchange = exchange_module.Exchange(str(self.tmp / "exchange.db"), test_exchange.FakeGateway(),
                                                 exchange_module.Keys(fetch=lambda: {"keys": [self.signer.jwk]}),
                                                 test_exchange.AUDIENCE)
        self.server = exchange_module.server(self.exchange, "127.0.0.1:0", "operator-secret")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.expected = dict(test_exchange.EXPECTED)

    def test_every_call_reaches_the_session_holding_the_workbench(self):
        loops, served = [], []

        def dispatch(session):
            def serve():
                client = Runner(self.url, session)
                grant = client.key(self.signer.token(run_id=str(len(loops))))
                benches = Benches(session, self.mirror, factory=FakeLocal)
                loop = runner.Loop(client, grant["key"], IMAGE, self.mirror, self.tmp / "runner", 2, 0,
                                   turns.Limits(), benches)
                original = benches.handle
                benches.handle = lambda payload: served.append((session, payload["op"])) or original(payload)
                loop.serve()
                client.end()
            thread = threading.Thread(target=serve)
            thread.start()
            loops.append(thread)

        executor = RunnerExecutor(Operator(self.url, "operator-secret"), {"url": self.url, "headers": {}},
                                  self.expected, ["gpt-*"], 1000, dispatch)
        with patch.object(runner, "HEARTBEAT", 1):
            with RemoteBench(executor)(CONFIG, self.base, "concurro/7", {}, self.base) as workspace:
                self.assertEqual(workspace.files(["src/lib.rs"]), {"src/lib.rs": "fn main() {}\n"})
                workspace.prepare(["test"], 60)
            for thread in loops:
                thread.join(30)
        self.assertEqual(len(loops), 1)
        self.assertEqual([op for _, op in served], ["open", "files", "prepare", "close"])
        self.assertEqual(len({session for session, _ in served}), 1)
        self.assertEqual(self.exchange.gateway.list(), [], "the session ended once its workbench closed")


if __name__ == "__main__":
    unittest.main()
