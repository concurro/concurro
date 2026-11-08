"""Model calls keep a stable logical ID; uncertain attempts are bounded across process restarts."""

import subprocess
import sys
import unittest
from copy import deepcopy
from unittest.mock import patch

from support import (
    ENGINE,  # unittest module fixtures
    setUpModule as setUpModule,
    tearDownModule as tearDownModule,
)

import controller as controller_module
import turns
from agents import Agents
from checkpoints import Checkpoints
from controller import Controller
from fakes import CONFIG, FakeAgents, FakeGitHub
from policy import TIMEOUT_EXIT, Blocked, RunnerTerminated, SessionFailure, UncertainOutcome


class Interrupted(BaseException):
    """Process death: escapes every handler, so nothing after the last save persists."""


class OperationTests(unittest.TestCase):
    """Interruptions before or during dispatch, and lost write responses after completion."""

    def setUp(self):
        """Start a manual task whose first tick makes one intake model call."""
        self.config = deepcopy(CONFIG)
        self.config["autonomous"] = False
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()

    def advance(self):
        """Run one controller process; an interruption ends it like a crash."""
        controller = Controller(self.gh, self.agents, self.config)
        try:
            controller.advance(7)
        except Interrupted:
            pass
        return controller

    def until(self, stage):
        """Advance normally until the stored task reaches a stage."""
        for _ in range(10):
            if (self.stored() or {}).get("stage") == stage:
                return
            Controller(self.gh, self.agents, self.config).advance(7)
        self.fail("stage not reached: " + stage)

    def dispatches(self, stage, effect):
        """Patch provider calls for one stage, recording each dispatch."""
        original, seen = self.agents.ask, []

        def ask(provider, name, context):
            """Apply the effect only to the stage under test."""
            if name != stage:
                return original(provider, name, context)
            seen.append(context.get("review_output_budget"))
            return effect(provider, name, context) if callable(effect) else effect.pop(0)(provider, name, context)

        return seen, patch.object(self.agents, "ask", side_effect=ask)

    def stored(self):
        """Read the durable state a fresh process would see."""
        controller = Controller(self.gh, self.agents, self.config)
        controller.issue = self.gh.repo("issues/7")
        controller.load(self.gh.pages("issues/7/comments"))
        return controller.state

    def test_uncertain_attempts_are_retried_once_then_block(self):
        """A logical call interrupted twice blocks before a third dispatch; retry restores the allowance."""
        original = self.agents.ask
        with patch.object(self.agents, "ask", side_effect=Interrupted):
            self.advance()
            first = self.stored()["operation"]
            self.assertEqual((first["attempts"], first["uncertain"], first["status"]), (1, 0, "started"))
            self.advance()
            second = self.stored()["operation"]
            self.assertEqual(second["id"], first["id"])
            self.assertNotEqual(second["attempt"], first["attempt"])
            self.assertEqual((second["attempts"], second["uncertain"]), (2, 1))
            controller = Controller(self.gh, self.agents, self.config)
            with self.assertRaisesRegex(Blocked, "uncertain twice"):
                controller.advance(7)
            self.assertEqual(self.agents.ask.call_count, 2)
        self.assertEqual(controller.state["calls"], 2)
        controller.suspend("blocked", "model call outcome was uncertain twice", publish_events=False)
        self.gh.comment("/concurro retry")
        with patch.object(self.agents, "ask", side_effect=original) as ask:
            state = self.advance().state
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(state["operation"]["status"], "consumed")
        self.assertNotEqual(state["stage"], "blocked")

    def test_reported_failures_are_not_uncertain(self):
        """A provider failure the controller observed never consumes the uncertain allowance."""
        controller = Controller(self.gh, self.agents, self.config)
        with patch.object(self.agents, "ask", side_effect=Blocked("provider failed")):
            with self.assertRaises(Blocked):
                controller.advance(7)
        # main() persists the observed failure through the same suspend path.
        controller.suspend("blocked", "provider failed", publish_events=False)
        operation = self.stored()["operation"]
        self.assertEqual((operation["status"], operation["uncertain"]), ("failed", 0))

    def test_lost_write_response_after_completion_does_not_repeat_the_call(self):
        """A completed call whose next state write applied but lost its response is not re-dispatched."""
        original = self.gh.repo
        called, lost = [], []

        def repo(path, method="GET", data=None):
            """Apply the first post-call artifact write, then lose its response."""
            result = original(path, method, data)
            if called and not lost and method in ("POST", "PATCH") and "/artifacts" in path:
                lost.append(path)
                raise RuntimeError("response lost")
            return result

        ask = self.agents.ask

        def answer(provider, stage, context):
            """Record that the provider completed."""
            called.append(stage)
            return ask(provider, stage, context)

        with patch.object(self.gh, "repo", side_effect=repo), patch.object(self.agents, "ask", side_effect=answer):
            with self.assertRaises(Exception):
                Controller(self.gh, self.agents, self.config).advance(7)
        self.assertTrue(lost)
        operation = self.stored()["operation"]
        self.assertEqual(operation["status"], "completed")
        calls = len(called)
        with patch.object(self.agents, "ask", side_effect=answer):
            self.advance()
        self.assertTrue(all(stage != "intake" for stage in called[calls:]))

    def test_incidental_request_changes_keep_the_operation(self):
        """A review budget derived from checkpoint size does not reset the uncertain allowance."""
        self.until("plan_qc")

        def interrupt(*_):
            raise Interrupted()

        seen, patched = self.dispatches("plan_qc", interrupt)
        # Each restart sees a different checkpoint size, as recording an attempt grows it.
        budgets = ({"max_json_bytes": 9000 - n, "encoding": "JSON"} for n in range(10))
        with patched, patch.object(Controller, "review_output_budget", side_effect=lambda *_: next(budgets)):
            self.advance()
            self.advance()
            with self.assertRaisesRegex(Blocked, "uncertain twice"):
                Controller(self.gh, self.agents, self.config).advance(7)
        # The retry keeps the original response contract despite the recomputed budget.
        self.assertEqual(seen, [{"max_json_bytes": 9000, "encoding": "JSON"}] * 2)
        controller = Controller(self.gh, self.agents, self.config)
        work = {"plan": {"files": ["docs/guide.md"]}, "review_output_budget": {"max_json_bytes": 1}}
        controller.state = {"issue": 7}
        first = controller.operation("claude", "plan_qc", work)["id"]
        controller.state = {"issue": 7}
        self.assertEqual(controller.operation("claude", "plan_qc", {**work, "review_output_budget": 2})["id"], first)
        controller.state = {"issue": 7}
        self.assertNotEqual(controller.operation("claude", "plan_qc", {**work, "plan": {}})["id"], first)

    def test_completed_result_survives_a_crash_before_the_stage_saves_it(self):
        """The retained result is reused once; the provider is not called again."""
        original_save, saves = Checkpoints.save, []

        def crash_after_completion(checkpoints, state):
            """Persist the completion checkpoint, then die on the stage's own save."""
            operation = state.get("operation") or {}
            if operation.get("status") == "consumed" and not saves:
                saves.append(True)
                raise Interrupted()
            return original_save(checkpoints, state)

        seen, patched = self.dispatches("intake", lambda *a: FakeAgents.ask(self.agents, *a))
        with patched, patch.object(Checkpoints, "save", crash_after_completion):
            self.advance()
            stored = self.stored()["operation"]
            self.assertEqual((stored["status"], "response" in stored), ("completed", True))
            self.advance()
        self.assertEqual(len(seen), 1)
        state = self.stored()
        self.assertNotEqual(state["stage"], "intake")
        self.assertEqual((state["operation"]["status"], "response" in state["operation"]), ("consumed", False))

    def test_unretained_results_count_as_uncertain(self):
        """A completion too large to retain is recovered by redispatch within the same allowance."""
        original_save = Checkpoints.save

        def crash_after_completion(checkpoints, state):
            """Lose every stage save after a completion checkpoint."""
            if (state.get("operation") or {}).get("status") == "consumed":
                raise Interrupted()
            return original_save(checkpoints, state)

        seen, patched = self.dispatches("intake", lambda *a: FakeAgents.ask(self.agents, *a))
        with (
            patched,
            patch.object(Checkpoints, "save", crash_after_completion),
            patch.object(controller_module, "RESULT_MARGIN", controller_module.CHECKPOINT_LIMIT),
        ):
            self.advance()
            self.assertNotIn("response", self.stored()["operation"])
            self.advance()
            with self.assertRaisesRegex(Blocked, "uncertain twice"):
                Controller(self.gh, self.agents, self.config).advance(7)
        self.assertEqual(len(seen), 2)

    def test_timeouts_share_the_uncertain_allowance(self):
        """An interruption then a timeout-like session failure blocks before a third dispatch."""
        self.until("plan")

        def interrupt(*_):
            raise Interrupted()

        def timeout(*_):
            raise SessionFailure("provider session failed", uncertain=True)

        seen, patched = self.dispatches("plan", [interrupt, timeout, interrupt])
        with patched:
            self.advance()
            with self.assertRaisesRegex(Blocked, "uncertain twice"):
                Controller(self.gh, self.agents, self.config).advance(7)
        self.assertEqual(len(seen), 2)

    def test_reported_session_failures_do_not_consume_the_allowance(self):
        """A definite session failure after an interruption still permits the session retry."""
        self.until("plan")

        def interrupt(*_):
            raise Interrupted()

        def failed(*_):
            raise SessionFailure("provider session failed")

        seen, patched = self.dispatches("plan", [interrupt, failed, lambda *a: FakeAgents.ask(self.agents, *a)])
        with patched:
            self.advance()
            self.advance()
        self.assertEqual(len(seen), 3)
        self.assertEqual(self.stored()["operation"]["uncertain"], 1)

    def test_uncertain_transport_errors_are_classified(self):
        """Non-session timeouts surface as uncertain; reported failures do not."""
        self.assertTrue(UncertainOutcome("x").uncertain)
        self.assertTrue(SessionFailure("x", uncertain=True).uncertain)
        self.assertFalse(SessionFailure("x").uncertain)
        self.assertFalse(getattr(Blocked("x"), "uncertain", False))

    def test_retained_review_replays_under_its_original_contract(self):
        """A recovered design review is validated against the budget it was requested with."""
        self.until("plan_qc")
        original_save, crashed = Checkpoints.save, []

        def crash_after_completion(checkpoints, state):
            """Die on the first stage save after a design review completes."""
            if (state.get("operation") or {}).get("status") == "consumed" and not crashed:
                crashed.append(True)
                raise Interrupted()
            return original_save(checkpoints, state)

        budgets = iter(
            [{"max_json_bytes": 9000, "encoding": "JSON"}] + [{"max_json_bytes": 10, "encoding": "JSON"}] * 20
        )
        seen, patched = self.dispatches("plan_qc", lambda *a: FakeAgents.ask(self.agents, *a))
        with (
            patched,
            patch.object(Checkpoints, "save", crash_after_completion),
            patch.object(Controller, "review_output_budget", side_effect=lambda *_: next(budgets)),
        ):
            self.advance()
            self.assertEqual(self.stored()["operation"]["status"], "completed")
            self.advance()
        state = self.stored()
        self.assertTrue(crashed)
        self.assertFalse(state.get("review_repair"))
        self.assertTrue(state["design_votes"])
        self.assertEqual(seen[0], {"max_json_bytes": 9000, "encoding": "JSON"})

    def test_review_capacity_ignores_the_temporary_retained_copy(self):
        """Checkpoint room for a review excludes a result that the stage's next save drops."""
        self.until("plan_qc")
        controller = Controller(self.gh, self.agents, self.config)
        controller.issue = self.gh.repo("issues/7")
        controller.load(self.gh.pages("issues/7/comments"))
        before = controller.review_output_budget()
        controller.state["operation"] = {"status": "completed", "response": {"text": "x" * 20000}}
        self.assertEqual(controller.review_output_budget(), before)

    def test_saved_design_votes_consume_their_operation(self):
        """Design review saves a speculative state copy; the saved vote still consumes the result."""
        self.until("plan_qc")
        self.advance()
        state = self.stored()
        self.assertTrue(state["design_votes"])
        self.assertEqual(state["operation"]["stage"], "plan_qc")
        self.assertEqual((state["operation"]["status"], "response" in state["operation"]), ("consumed", False))


class ContainerBoundaryTests(unittest.TestCase):
    """The entry point's exit status carries provider uncertainty to the host classification."""

    PROBE = (
        "import subprocess, sys, agent_entry\n"
        "def timeout():\n"
        "    subprocess.run([sys.executable, '-c', 'import time; time.sleep(5)'], timeout=0.2)\n"
        "def fails():\n"
        "    raise RuntimeError('provider invocation failed')\n"
        "agent_entry.main = {main}\n"
        "sys.exit(agent_entry.run())\n"
    )

    def entry(self, main):
        """Run the real entry-point exit mapping in a separate process."""
        return subprocess.run(
            [sys.executable, "-c", self.PROBE.format(main=main)], cwd=ENGINE, capture_output=True, text=True, timeout=30
        )

    def host(self, completed, session=None):
        """Feed an entry-point result through the turn outcome mapping and the agents, in place of Docker."""
        result, response = turns.outcome(completed.returncode, completed.stdout)

        class Executor:
            gateway = {"url": "https://gateway.invalid", "headers": {}}

            def run(self, turn, label):
                return {"outcome": result, "response": response, "retained": None, "workspace": None}

        agents = Agents(CONFIG, Executor(), retained=self.retained())
        context = {
            "bundle_id": agents.bundle.identity,
            "repository_commit": "a" * 40,
            "repository": {"refs": {"main": "a" * 40}, "head": "main"},
        }
        if session:
            context["controller_session"] = session
        agents.ask("codex", "review", context)

    def retained(self):
        import tempfile

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return directory.name

    def test_provider_timeout_is_identified_and_requires_writer_recovery(self):
        """A timeout crosses the subprocess boundary without becoming a generic provider failure."""
        completed = self.entry("timeout")
        self.assertEqual(completed.returncode, TIMEOUT_EXIT)
        self.assertNotIn("sleep", completed.stderr)
        for session in (None, {"scope": "a" * 64, "turn": 0}):
            with self.assertRaisesRegex(RunnerTerminated, "execution time limit") as raised:
                self.host(completed, session=session)
            self.assertTrue(raised.exception.result_uncertain)
            self.assertFalse(getattr(raised.exception, "uncertain", False), "no automatic repetition")

    def test_reported_provider_failure_stays_definite(self):
        """Other entry-point failures remain reported failures on the host."""
        completed = self.entry("fails")
        self.assertEqual(completed.returncode, 1)
        with self.assertRaises(Blocked) as raised:
            self.host(completed)
        self.assertFalse(getattr(raised.exception, "uncertain", False))


if __name__ == "__main__":
    unittest.main()
