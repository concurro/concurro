"""Each tick reports what the task waits for; waiting work consumes no model calls."""

import unittest

from support import (
    setUpModule as setUpModule,  # unittest module fixtures
    tearDownModule as tearDownModule,
)

from controller import OUTCOMES, WAIT_SECONDS, Controller
from pipeline_fixtures import ManualPipelineFixture


class OutcomeTests(ManualPipelineFixture, unittest.TestCase):
    """Outcomes derive from durable stage, so schedulers can avoid re-dispatching idle tasks."""

    def outcome(self):
        """Run one tick and return its outcome."""
        outcome = Controller(self.gh, self.agents, self.config).advance(7)
        self.assertIn(outcome.kind, OUTCOMES)
        return outcome

    def idle(self, kind, retry_after=None):
        """Two further ticks report the same idle outcome without any model call."""
        calls = len(self.agents.calls)
        for _ in range(2):
            outcome = self.outcome()
            self.assertEqual((outcome.kind, outcome.retry_after, bool(outcome)), (kind, retry_after, False))
        self.assertEqual(len(self.agents.calls), calls)

    def test_progress_then_human_approval(self):
        """Stage work continues; a planned manual task waits for a writer without polling models."""
        self.assertEqual(self.outcome().kind, "continue")
        while self.tick()["stage"] != "planned":
            pass
        self.idle("await-human")

    def test_unenrolled_issue_waits_for_input(self):
        """No task yet: only enrollment or a start command can create one."""
        self.gh.comments = []
        self.idle("await-human")

    def test_pending_checks_are_a_timed_wait(self):
        """Gates waiting on CI poll on a timer and never call a model."""
        self.ready()
        self.gh.checks_green = False
        self.idle("wait-until", WAIT_SECONDS)

    def test_stopped_and_merged_tasks(self):
        """A stopped task needs a writer command; a merged task is terminal."""
        self.ready()
        self.gh.comment("/concurro stop")
        self.assertEqual(self.outcome().kind, "blocked")
        self.idle("blocked")
        self.gh.comment("/concurro retry")
        self.assertEqual(self.outcome().kind, "wait-until")
        self.gh.pr.update(merged=True, merge_commit_sha="f" * 40, state="closed", merged_by={"login": "writer"})
        self.assertEqual(self.outcome().kind, "terminal")
        self.idle("terminal")


if __name__ == "__main__":
    unittest.main()
