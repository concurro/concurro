"""Complete prompt accounting matches the runner and refuses overflow before model spend."""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from support import CONFIG, setUpModule as setUpModule, tearDownModule as tearDownModule, transport_size

import feedback
import prompts
from agents import Agents
from bundle import Bundle
from controller import Controller
from fakes import FakeAgents, FakeGitHub
from pipeline_fixtures import PipelineFixture
from policy import Blocked, plan_output_contract
from test_turns import MODELS, FakeExecutor


class AccountingTests(PipelineFixture, unittest.TestCase):
    """Admission and execution share instructions, schemas and prompt serialization."""

    def test_every_stage_matches_the_runner_prompt_exactly(self):
        controller = self.reach("plan")
        with tempfile.TemporaryDirectory() as directory:
            agents = Agents(
                {**self.config, "models": MODELS}, FakeExecutor(), bundle=controller.bundle, retained=Path(directory)
            )
            for stage in ("intake", "plan", "plan_qc", "develop", "review"):
                context = controller.prepare_context("codex", stage, controller.context())
                context["issue"] = {**context["issue"], "title": "Review café 🌍"}
                if stage == "plan":
                    context["plan_output_contract"] = plan_output_contract(self.config)
                if stage == "develop":
                    context["controller_candidate"] = {"docs/guide.md": "Candidate"}
                    context["design_authorization"] = {"binding": "controller-proof"}
                context["own_findings"] = [{"detail": "Full evidence " * 5000}]
                context = feedback.prepare(context)
                turn = agents.prepare("codex", stage, context)
                request = {
                    **turn["request"],
                    "workspace": "/arbitrary/runner/path/work",
                    "feedback_directory": "/arbitrary/runner/path/feedback",
                }
                original = deepcopy(request)
                rendered = prompts.prompt_for(request, agents.bundle)
                with self.subTest(stage=stage):
                    schema_bytes = len(prompts.serialize(agents.bundle.schema(stage)).encode())
                    self.assertEqual(prompts.size(stage, context, agents.bundle), len(rendered.encode()) + schema_bytes)
                    value = json.loads(rendered)
                    self.assertNotIn("response_schema", value)
                    self.assertIn('"title":"Review café 🌍"', rendered)
                    self.assertEqual(value["untrusted_context"]["issue"], context["issue"])
                    if stage == "plan":
                        self.assertEqual(
                            value["untrusted_context"]["plan_output_contract"], context["plan_output_contract"]
                        )
                    for key in ("edit_policy", "limits"):
                        if stage == "plan":
                            self.assertNotIn(key, value["untrusted_context"])
                        else:
                            self.assertEqual(value["untrusted_context"][key], context[key])
                    self.assertEqual(value["repository_snapshot"]["workspace"], ".")
                    self.assertEqual(value["feedback_files"]["directory"], "../feedback")
                    self.assertFalse(any(k.startswith("controller_") for k in value["untrusted_context"]))
                    self.assertEqual(request, original)
                    self.assertEqual(value["instructions"]["charter"], controller.bundle.charter)
                    for key in ("charter", "repository", "repository_commit", "local_commit"):
                        self.assertNotIn(key, value["untrusted_context"])
                    self.assertEqual(value["repository_snapshot"]["commit"], context["repository_commit"])
                    self.assertEqual(value["repository_snapshot"]["refs"], context["repository"]["refs"])
                    self.assertEqual(value["repository_snapshot"]["head"], context["repository"]["head"])
                    self.assertEqual(value["repository_snapshot"]["local_checkpoint"], context.get("local_commit"))

    def test_formerly_fitting_context_is_rejected_before_call_or_dispatch(self):
        controller = self.reach("plan")
        context = controller.prepare_context("claude", "plan", controller.context())
        size = prompts.size("plan", context, controller.bundle)
        controller.config["max_context_bytes"] = size - 1
        self.assertLess(transport_size(context), size - 1)
        calls, dispatched = controller.state["calls"], len(self.agents.calls)
        with self.assertRaisesRegex(Blocked, "prompt .*instructions and schema.*size limit"):
            controller.ask("claude", "plan", context)
        self.assertEqual(controller.state["calls"], calls)
        self.assertEqual(len(self.agents.calls), dispatched)
        controller.config["max_context_bytes"] = size
        self.assertEqual(controller.prepare_context("claude", "plan", context), context)

    def test_large_role_instructions_are_counted_in_the_single_budget(self):
        original = Bundle()
        instructions = {".concurro/instructions/shared.md": "Target authority. " * 6000}
        bundle = Bundle(charter_text=original.charter_text, instruction_texts=instructions)
        config = deepcopy(CONFIG)
        gh, agents = FakeGitHub(config), FakeAgents()
        controller = Controller(gh, agents, config, bundle=bundle)
        controller.advance(7)
        context = controller.context()
        config["max_context_bytes"] = 50000
        self.assertLess(transport_size(context), 50000)
        calls = controller.state["calls"]
        with self.assertRaisesRegex(Blocked, "instructions and schema"):
            controller.ask("claude", "plan", context)
        self.assertEqual(controller.state["calls"], calls)

    def test_large_feedback_remains_in_files_while_its_index_is_counted(self):
        controller = self.reach("plan")
        context = controller.context()
        context["design_feedback"] = [{"rationale": "Evidence 🌍 " * 30000}]
        prepared = controller.prepare_context("claude", "plan", context)
        self.assertIn("controller_feedback", prepared)
        self.assertEqual(feedback.restore(prepared)["design_feedback"], context["design_feedback"])
        self.assertLess(prompts.size("plan", prepared, controller.bundle), self.config["max_context_bytes"])
