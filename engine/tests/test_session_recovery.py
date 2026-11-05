"""Session failures and killed repair checks recover without resetting task authority."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from support import setUpModule, tearDownModule  # unittest module fixtures
from controller import Controller
from policy import Blocked, SessionFailure, approval_valid, digest
from session_entry import SessionTurn
from test_pipeline import CONFIG, FakeAgents, FakeGitHub


class JournalAgents(FakeAgents):
    """Exercise the real disk journal with deterministic provider output and failures."""

    def __init__(self, root):
        """Isolate transcripts by the same scope passed to Docker in production."""
        super().__init__()
        self.root, self.failures, self.prompts = Path(root), 0, []

    def ask(self, provider, stage, context):
        """Mirror the entry point's pending/completed journal and sanitized failure boundary."""
        report = super().ask(provider, stage, context)
        session = context.get("controller_session")
        if not session:
            return report
        root = self.root / session["scope"]
        root.mkdir(exist_ok=True)
        prompt = json.dumps({"instructions": "fixture", "response_schema": {}, "repository_snapshot": {},
                             "untrusted_context": {k: v for k, v in context.items() if k != "controller_session"}})
        turn = None
        try:
            turn = SessionTurn(session, prompt, root)
            if turn.cached is not None:
                return turn.cached
            self.prompts.append((session["scope"], json.loads(turn.start())))
            if self.failures:
                self.failures -= 1
                raise ValueError("provider overload or timeout")
            turn.finish(report, turn.identifier)
            return report
        except ValueError:
            raise SessionFailure("provider session failed") from None
        finally:
            if turn:
                turn.close()


class SessionRecoveryTests(unittest.TestCase):
    """Recreate controllers from GitHub state and transcripts after every interruption."""

    def setUp(self):
        """Start with empty checkpoints and private test transcript storage."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = deepcopy(CONFIG)
        self.gh = FakeGitHub(self.config)
        self.gh.comments = []
        self.agents = JournalAgents(self.directory.name)

    def load(self, ):
        """Reload only durable checkpoints, without reusing the previous controller object."""
        controller = Controller(self.gh, self.agents, self.config)
        controller.issue = self.gh.issue
        controller.load(self.gh.comments)
        return controller

    def reach(self, stage):
        """Advance a bounded design loop through the ordinary production methods."""
        for _ in range(15):
            controller = self.load()
            controller.advance(7)
            if controller.state["stage"] == stage:
                return controller
        self.fail("stage not reached: " + stage)

    def test_failure_on_later_turn_rotates_only_that_role_with_full_context(self):
        """A persisted first turn survives a failed continuation; the replacement starts clean."""
        controller = self.reach("plan_qc")
        before = deepcopy(controller.state)
        other = {"scope": "f" * 64, "turn": 3}
        controller.state["sessions"]["unrelated-role"] = deepcopy(other)
        self.agents.failures = 1
        context = controller.context()
        result = controller.ask("claude", "plan", context)
        self.assertTrue(result["summary"])
        state = self.load().state
        role = "plan:claude:" + CONFIG["models"]["claude"]
        old = before["sessions"][role]
        current = state["sessions"][role]
        self.assertNotEqual(current["scope"], old["scope"])
        self.assertEqual((current["turn"], current["restarts"]), (1, 1))
        self.assertEqual(state["sessions"]["unrelated-role"], other)
        self.assertEqual(state["calls"], before["calls"] + 2)
        self.assertEqual(current["completed"]["response"], digest(result))
        self.assertEqual(self.agents.prompts[-1][1]["untrusted_context"]["issue"], state["requirements"])
        self.assertNotIn("context_update", self.agents.prompts[-1][1])
        self.assertEqual(json.loads((Path(self.directory.name) / old["scope"] / "turn.json").read_text())["status"], "pending")
        self.assertEqual(state["plan_revisions"], before["plan_revisions"])
        self.assertEqual(state["design_votes"], before["design_votes"])

    def test_two_failures_stop_but_next_retry_has_a_working_fresh_session(self):
        """Bound immediate retries while keeping a supported path forward on the same issue."""
        controller = self.reach("plan")
        before = controller.state["calls"]
        self.agents.failures = 2
        with self.assertRaisesRegex(Blocked, "failed twice"):
            controller.plan()
        controller.suspend("blocked", "provider session failed twice", publish_events=False)
        self.assertEqual(self.load().state["calls"], before + 2)
        self.gh.comment("/concurro retry")
        recovered = self.load()
        recovered.advance(7)
        self.assertEqual(recovered.state["stage"], "plan_qc")
        self.assertEqual(recovered.state["calls"], before + 3)

    def test_restart_cannot_replenish_model_budget(self):
        """Even session recovery stops at the persisted per-task attempt ceiling."""
        controller = self.reach("plan")
        controller.state["calls"] = self.config["max_model_calls"] - 1
        controller.save()
        self.agents.failures = 1
        with self.assertRaisesRegex(Blocked, "budget exhausted"):
            controller.plan()
        state = self.load().state
        self.assertEqual(state["calls"], self.config["max_model_calls"])
        self.assertFalse(self.gh.refs)
