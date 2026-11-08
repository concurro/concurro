"""Complete feedback reaches native runner tools without overflowing prompts or crossing roles."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule
import feedback
from agent_entry import prompt_for
from agents import Agents
from controller import Controller, transport_size
from policy import digest, design_roster
from fakes import CONFIG, FakeAgents, FakeGitHub
from runner_fixtures import FakeExecutor, MODELS


class FeedbackTests(unittest.TestCase):
    """Small indexes bind complete feedback files and stable replay identities."""

    def setUp(self):
        """Reach planning with only fake transports."""
        self.config = {**deepcopy(CONFIG), "max_context_bytes": 350000}
        self.gh, self.agents = FakeGitHub(self.config), FakeAgents()
        self.gh.comments = []
        self.controller = Controller(self.gh, self.agents, self.config)
        self.controller.advance(7)
        self.controller.advance(7)

    def test_large_feedback_fits_and_completed_turn_replays_without_another_call(self):
        """Two valid large reports are full runner files, not a reason to narrow the change."""
        reports = [
            {
                "decision": "revise",
                "rationale": f"Reviewer {i} " + "evidence " * 25000,
                "objections": [{"id": f"O{i}", "status": "open", "reason": "Check boundary cases."}],
            }
            for i in range(2)
        ]
        context = {**self.controller.context(), "design_feedback": reports}
        self.assertGreater(transport_size(context), self.config["max_context_bytes"])
        response = self.controller.ask("claude", "plan", context)
        sent = self.agents.calls[-1][2]
        self.assertLess(transport_size(sent), self.config["max_context_bytes"])
        self.assertEqual(feedback.restore(sent)["design_feedback"], reports)
        self.assertEqual([r["objections"][0]["id"] for r in sent["design_feedback"]["index"]], ["O0", "O1"])
        calls = len(self.agents.calls)
        recovered = Controller(self.gh, self.agents, self.config)
        recovered.issue = self.gh.issue
        recovered.load(self.gh.comments)
        self.assertEqual(recovered.ask("claude", "plan", context), response)
        self.assertEqual(len(self.agents.calls), calls)
        changed = deepcopy(context)
        changed["design_feedback"][0]["rationale"] += " A different finding."
        recovered.ask("claude", "plan", changed)
        self.assertEqual(len(self.agents.calls), calls + 1)

    def test_projection_preserves_operation_identity_and_does_not_mutate_evidence(self):
        """File transport changes neither replay identity nor classification's advisory status."""
        context = {"design_feedback": [{"rationale": "important " * 3000, "classification": {"complexity": "high"}}]}
        original = deepcopy(context)
        projected = feedback.prepare(context)
        raw_id = self.controller.operation("claude", "plan", context)["id"]
        self.assertEqual(self.controller.operation("claude", "plan", projected)["id"], raw_id)
        revised = deepcopy(context)
        revised["design_feedback"][0]["classification"] = {"complexity": "low"}
        self.assertEqual(self.controller.operation("claude", "plan", feedback.prepare(revised))["id"], raw_id)
        self.assertEqual(context, original)
        self.assertEqual(feedback.prepare(projected), projected)

    def test_pr_feedback_files_contain_only_that_reviewers_findings(self):
        """Full files preserve all own findings while exposing no peer reviews."""
        self.controller.state["findings"] = [
            {
                "id": "F" + str(i),
                "provider": provider,
                "status": "open",
                "path": "docs/guide.md",
                "line": 1,
                "start_line": None,
                "detail": provider + " private reasoning " * 2000,
            }
            for i, provider in enumerate(("claude", "codex"))
        ]
        self.controller.state["reviews"] = {"codex": {"summary": "PEER REPORT SENTINEL"}}
        for provider in ("claude", "codex"):
            context = self.controller.pr_review_context(
                provider, {"docs/guide.md": "a"}, {"docs/guide.md": "b"}, "f" * 40
            )
            self.assertIn("controller_feedback", context)
            self.assertEqual(
                feedback.restore(context)["own_findings"][0]["detail"],
                next(f["detail"] for f in self.controller.state["findings"] if f["provider"] == provider),
            )
            files = json.dumps(context["controller_feedback"])
            self.assertNotIn("PEER REPORT SENTINEL", files)
            self.assertNotIn(("codex" if provider == "claude" else "claude") + " private reasoning", files)

    def test_fresh_design_review_has_no_old_objection_files(self):
        """Projection cannot make absent feedback available through a second route."""
        state = self.controller.state
        member = design_roster(self.config)[0]
        state["objections"] = [
            {"id": "O1", "reviewer": member["id"], "reason": "OLD OBJECTION " * 2000, "resolved": False}
        ]
        state.update(design_votes={}, design_phase="fresh")

        class Captured(Exception):
            """Stop after observing the production request without validating a fake answer."""

        requests = []

        def capture(provider, stage, context):
            """Observe exactly what this isolated review would receive."""
            requests.append(context)
            raise Captured()

        with patch.object(self.agents, "ask", side_effect=capture), self.assertRaises(Captured):
            self.controller.quality_control("plan_qc")
        self.assertNotIn("OLD OBJECTION", json.dumps(requests[0]))
        self.assertNotIn("own_objections", requests[0])

    def test_cooperative_review_exports_only_its_own_objections(self):
        """Own complete evidence is present without giving the role its peer's reasoning."""
        state = self.controller.state
        roster = design_roster(self.config)
        state["objections"] = [
            {"id": f"O{i}", "reviewer": member["id"], "reason": f"PRIVATE-{i} " * 2000, "resolved": False}
            for i, member in enumerate(roster)
        ]
        state.update(design_votes={}, design_phase="cooperative")

        class Captured(Exception):
            """Stop after recording the real selected request."""

        requests = []

        def capture(provider, stage, context):
            """Keep this review's selected file transport for inspection."""
            requests.append(context)
            raise Captured()

        with patch.object(self.agents, "ask", side_effect=capture), self.assertRaises(Captured):
            self.controller.quality_control("plan_qc")
        context = requests[0]
        self.assertEqual(feedback.restore(context)["own_objections"], [state["objections"][0]])
        self.assertIn("PRIVATE-0", json.dumps(context["controller_feedback"]))
        self.assertNotIn("PRIVATE-1", json.dumps(context))

    def test_native_agent_transport_and_prompt_reference_verified_files(self):
        """The payload carries full JSON outside model context; native tools get its directory."""
        executor = FakeExecutor({"outcome": "ok", "response": {}, "retained": None})
        agents = Agents({**CONFIG, "models": MODELS}, executor)
        value = [
            {"id": "F1", "status": "open", "path": "src/" + "long-name" * 25 + ".rs", "detail": "évidence\n" * 3000}
        ]
        context = feedback.prepare(
            {
                "bundle_id": agents.bundle.identity,
                "repository_commit": "a" * 40,
                "repository": {"refs": {"main": "a" * 40}, "head": "main"},
                "own_findings": value,
            }
        )
        agents.ask("codex", "review", context)
        [(turn, _)] = executor.turns
        self.assertNotIn("controller_feedback", turn["request"]["context"])
        with tempfile.TemporaryDirectory() as root:
            directory = feedback.materialize(turn["feedback"], Path(root) / "feedback")
            reference = turn["request"]["context"]["own_findings"]
            self.assertEqual(reference["index"][0]["path"], value[0]["path"])
            raw = json.loads((Path(directory) / reference["feedback_file"]).read_text())
            self.assertEqual(raw, value)
            self.assertEqual(digest(raw), reference["digest"])
            request = {**turn["request"], "feedback_directory": directory, "workspace": "/work", "schema": {}}
            prompt = json.loads(prompt_for(request, agents.bundle))
            self.assertEqual(prompt["feedback_files"]["directory"], directory)
            self.assertNotIn(value[0]["detail"], json.dumps(prompt))
        for key, data in [("../escape", value), ("a" * 64, value)]:
            with tempfile.TemporaryDirectory() as root, self.assertRaises(ValueError):
                feedback.materialize({key: data}, Path(root) / "feedback")
