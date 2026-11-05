"""Cutover moves tasks between Actions and the service; operator commands reach only the running daemon."""

from contextlib import contextmanager
from copy import deepcopy
import threading
import unittest

from fixtures import KEY, ServiceFixture
from support import setUpModule, tearDownModule  # unittest module fixtures
from test_pipeline import FakeAgents
from controller import ACTIONS, Controller
from github import NotFound
from service import control
from service.cutover import MARKER_AWARE, WORKFLOW, Cutover, CutoverError
from service.identity import IdentityError

PIN = "c" * 40


class FakeIdentity:
    """Mints a cutover token, or fails as an App without the cutover permissions does."""

    def __init__(self):
        self.permitted = True
        self.revoked = 0

    @contextmanager
    def cutover(self, repository):
        if not self.permitted:
            raise IdentityError("App token permissions differ from the requested least-privilege scope")
        try:
            yield "cutover-token"
        finally:
            self.revoked += 1


class ActionsApi:
    """Repository and environment variables and workflow runs, as the cutover token reads them."""

    def __init__(self):
        self.variables = {"actions/variables": [{"name": "CONCURRO_ENABLED", "value": "false"}],
                          "environments/concurro/variables": None}
        self.runs = []

    def request(self, method, path, data=None):
        relative = path.split("/", 3)[3]
        for prefix, variables in self.variables.items():
            if relative.startswith(prefix + "?"):
                if variables is None:
                    raise NotFound("no environment")
                return {"total_count": len(variables), "variables": variables}
        if relative.startswith("actions/runs?"):
            return {"total_count": len(self.runs), "workflow_runs": self.runs}
        raise AssertionError(path)


class CutoverFixture(ServiceFixture):

    def setUp(self):
        """A task the Actions controller created, and a disabled, idle, marker-aware Actions workflow."""
        super().setUp()
        self.service()
        Controller(self.fake, FakeAgents(), self.config).advance(7)
        self.assertEqual(self.state()["controller"], ACTIONS)
        self.backend.files[WORKFLOW] = f"jobs:\n  advance:\n    steps:\n      - uses: concurro/concurro@{PIN}\n"
        self.identity, self.actions, self.comparisons = FakeIdentity(), ActionsApi(), {PIN: "ahead"}
        self.cutover = Cutover(self.settings, self.identity, self.gateway, self.registry, lambda r: self.backend,
                               self.worker.load, clock=self.clock, compare=self.compare,
                               transport_factory=lambda repository, token, clock: self.actions)
        self.control = control.Control(self.registry, self.reconciler, self.cutover)

    def compare(self, repository, base, head):
        self.assertEqual((repository, base), ("concurro/concurro", MARKER_AWARE))
        return self.comparisons[head]


class CutoverTests(CutoverFixture, unittest.TestCase):

    def test_adopt_and_release(self):
        """Adoption rewrites the marker and enqueues the task; release hands it back and stops observing it."""
        self.assertEqual(self.cutover.adopt(KEY[0]), {"adopted": [7], "refused": {}})
        self.assertEqual(self.state()["controller"], "service:primary")
        self.assertEqual(self.identity.revoked, 1)
        self.assertIn("adopt", self.registry.due(self.clock())[0].causes)
        stage = self.state()["stage"]
        self.drain()
        self.assertNotEqual(self.state()["stage"], stage, "the service advances an adopted task")
        before = deepcopy((self.fake.comments, self.fake.artifacts, self.fake.issue["labels"]))
        Controller(self.fake, FakeAgents(), self.config).advance(7)
        self.assertEqual(deepcopy((self.fake.comments, self.fake.artifacts, self.fake.issue["labels"])), before,
                         "a re-enabled Actions controller writes nothing to an adopted task")
        self.registry.enqueue(KEY, "retry", self.clock() + 300)
        self.assertEqual(self.cutover.adopt(KEY[0], (7,))["refused"], {7: "task is not owned by actions"})
        self.assertIn("retry", self.registry.due(self.clock() + 300)[0].causes, "a refusal keeps queued work")
        self.cutover.release(*KEY)
        self.assertEqual(self.state()["controller"], ACTIONS)
        self.assertEqual(self.registry.outcome(KEY).kind, "terminal")
        self.assertEqual(self.reconciler.poll(), [])

    def test_refusals_write_nothing(self):
        """Missing permissions, an old pin, enabled or running Actions, or an ambiguous workflow refuse cutover."""
        def permissions():
            self.identity.permitted = False

        def enabled(path):
            self.actions.variables[path] = [{"name": "CONCURRO_ENABLED", "value": "true"}]

        refusals = {
            "permissions": (permissions, IdentityError),
            "old pin": (lambda: self.comparisons.update({PIN: "behind"}), CutoverError),
            "diverged pin": (lambda: self.comparisons.update({PIN: "diverged"}), CutoverError),
            "repository variable": (lambda: enabled("actions/variables"), CutoverError),
            "environment variable": (lambda: enabled("environments/concurro/variables"), CutoverError),
            "running": (lambda: self.actions.runs.append({"path": WORKFLOW}), CutoverError),
            "two pins": (lambda: self.backend.files.update({WORKFLOW: self.backend.files[WORKFLOW] + "      - uses: "
                                                            f"concurro/concurro@{'d' * 40}\n"}), CutoverError),
        }
        workflow = self.backend.files[WORKFLOW]
        for name, (change, error) in refusals.items():
            with self.subTest(refusal=name):
                self.identity.permitted, self.comparisons[PIN] = True, "ahead"
                self.actions, self.backend.files[WORKFLOW] = ActionsApi(), workflow
                change()
                with self.assertRaises(error):
                    self.cutover.adopt(KEY[0])
                self.assertEqual(self.backend.writes, [])
                self.assertEqual(self.state()["controller"], ACTIONS)
                self.assertEqual(self.registry.due(self.clock()), [])

    def test_unresolved_write_blocks_release_until_resolved(self):
        """`release` refuses a task with an unknown write; `resolve` records the disposition and reschedules it."""
        self.cutover.adopt(KEY[0])
        self.backend.fail("POST", "issues/7/comments", lost=True)
        for _ in range(6):
            self.drain()
            if self.registry.unresolved(KEY):
                break
        [record] = self.registry.unresolved(KEY)
        reply = self.control.handle({"command": "release", "repository": KEY[0], "issue": 7})
        self.assertEqual(reply, {"ok": False, "error": "resolve the task's unknown writes before releasing it"})
        self.assertEqual(self.drain(), 0, "an unresolved task is never scheduled")
        reply = self.control.handle({"command": "resolve", "intent": record.intent, "disposition": "applied",
                                     "evidence": "comment observed on the issue", "operator": "operator"})
        self.assertEqual(reply, {"ok": True, "result": {"repository": KEY[0], "issue": 7}})
        resolution = self.registry.mutation(record.intent).resolution
        self.assertEqual((resolution["disposition"], resolution["operator"]), ("applied", "operator"))
        self.assertIn("resolved", self.registry.due(self.clock())[0].causes)
        self.assertGreater(self.drain(), 0)
        self.assertEqual(self.control.handle({"command": "release", "repository": KEY[0], "issue": 7})["ok"], True)

    def test_stops_during_cutover_are_reported(self):
        """A write of unknown outcome during adoption is reported to the operator, not swallowed."""
        self.backend.fail("PATCH", "issues/7/artifacts/", lost=True)
        reply = self.control.handle({"command": "adopt", "repository": KEY[0], "issues": [7]})
        self.assertFalse(reply["ok"])
        self.assertTrue(self.registry.unresolved(KEY))
        self.assertIsNone(self.gateway.acquire(KEY, "probe"))


class ControlChannelTests(CutoverFixture, unittest.TestCase):
    """Operator commands travel over the daemon's private socket."""

    def test_round_trip(self):
        server = control.server(self.control, self.directory.name)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        status = control.request(self.directory.name, {"command": "status"})
        self.assertTrue(status["ok"])
        self.assertEqual(control.request(self.directory.name, {"command": "adopt", "repository": KEY[0]})["result"],
                         {"adopted": [7], "refused": {}})
        self.assertEqual(control.request(self.directory.name, {"command": "unknown"}),
                         {"ok": False, "error": "unknown command"})
        [task] = control.request(self.directory.name, {"command": "status"})["result"]
        self.assertEqual((task["issue"], task["queued"]["causes"]), (7, ["adopt"]))

    def test_no_daemon(self):
        with self.assertRaises(ConnectionError):
            control.request(self.directory.name, {"command": "status"})


if __name__ == "__main__":
    unittest.main()
