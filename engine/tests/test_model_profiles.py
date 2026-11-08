"""Named profiles stay distinct through validation, consensus, findings and runner dispatch."""

import os
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from support import CONFIG, setUpModule as setUpModule, tearDownModule as tearDownModule

import sessions
from agent_entry import command_for, provider_environment
from agents import Agents, RunnerExecutor
from controller import Controller, config_digest, validate_config
from fakes import FINDING, FakeAgents, FakeGitHub, named_config
from model_config import parse, resolve
from policy import Blocked, approval_binding, design_roster, digest
from readiness import review_reasons
from runner_fixtures import GATEWAY, TASK, TURN, FakeExecutor, FakeOperator


class ConfigurationTests(unittest.TestCase):
    """Typed profiles are reused and reject ambiguous or misspelled assignments."""

    def test_named_profiles_are_typed_and_only_assigned_reviewers_join_consensus(self):
        config = named_config()
        validate_config(config)
        parsed = parse(config)
        self.assertIs(parsed, parse(deepcopy(config)))
        with self.assertRaises(FrozenInstanceError):
            parsed.resolve("sol-high").profile.effort = "low"
        roster = design_roster(config)
        self.assertEqual([r["provider"] for r in roster], ["sol-high", "astra-high"])
        self.assertEqual([r["agent"]["harness"] for r in roster], ["codex", "codex"])
        self.assertEqual(len({r["id"] for r in roster}), 2)

    def test_plan_and_pr_reviewers_are_independently_assigned(self):
        config = named_config()
        config["agents"]["plan_reviewers"] = ["opus-high", "sol-medium"]
        self.assertEqual([m["provider"] for m in design_roster(config)], ["opus-high", "sol-medium"])
        self.assertEqual(parse(config).agents.pr_reviewers, ("sol-high", "astra-high"))

    def test_reviewer_lists_reject_model_aliases_and_effort_only_differences(self):
        for role in ("plan_reviewers", "pr_reviewers"):
            for effort in ("high", "medium"):
                for additional in ([], ["astra-high"]):
                    with self.subTest(role=role, effort=effort, additional=additional):
                        config = named_config()
                        config["models"]["sol-alias"] = {**config["models"]["sol-high"], "effort": effort}
                        config["agents"][role] = ["sol-high", "sol-alias", *additional]
                        with self.assertRaisesRegex(Blocked, role + " requires distinct provider/model"):
                            validate_config(config)

    def test_reviewer_identity_includes_the_provider(self):
        config = named_config()
        config["models"]["opus-high"]["model"] = "shared-route-name"
        config["models"]["sol-high"]["model"] = "shared-route-name"
        for role in ("plan_reviewers", "pr_reviewers"):
            config["agents"][role] = ["opus-high", "sol-high"]
        validate_config(config)

    def test_autonomous_triage_and_planner_can_share_a_profile(self):
        config = named_config()
        config["autonomous"] = True
        config["agents"]["triage"] = config["agents"]["planner"]
        validate_config(config)
        assignments = parse(config).agents
        self.assertEqual(assignments.triage, assignments.planner)

    def test_invalid_profiles_and_assignments_are_refused(self):
        mutations = [
            lambda c: c["models"]["sol-high"].pop("effort"),
            lambda c: c["models"]["sol-high"].update(effort="hgh"),
            lambda c: c["models"]["sol-high"].update(temperature=0.3),
            lambda c: c["models"]["sol-high"].update(provider="codex"),
            lambda c: c["models"]["sol-high"].update(model=""),
            lambda c: c["agents"].update(triage="missing"),
            lambda c: c["agents"].update(plan_reviewers=["sol-high", "sol-high"]),
            lambda c: c["agents"].update(pr_reviewers=["sol-high"]),
            lambda c: c["agents"].update(reviewer="sol-high"),
            lambda c: c.update(qc="codex"),
        ]
        for mutate in mutations:
            config = named_config()
            mutate(config)
            with self.subTest(config=config), self.assertRaises(Blocked):
                validate_config(config)

    def test_effort_changes_invalidate_config_and_design_identity(self):
        config = named_config()
        changed = deepcopy(config)
        changed["models"]["sol-high"]["effort"] = "medium"
        self.assertNotEqual(config_digest(config), config_digest(changed))
        self.assertNotEqual(design_roster(config), design_roster(changed))
        state = {"plan_id": "p", "plan": {}, "base": "b", "requirements": {}, "config_digest": "old"}
        self.assertNotEqual(approval_binding(state, config), approval_binding(state, changed))

    def test_legacy_roster_and_digest_remain_unchanged(self):
        expected = []
        for provider in ("claude", "codex"):
            model = CONFIG["models"][provider]
            roles = ["plan", "review"] if provider == "claude" else ["develop", "intake", "review"]
            expected.append({"id": digest([provider, model]), "provider": provider, "model": model, "roles": roles})
        self.assertEqual(design_roster(CONFIG), expected)
        self.assertEqual(config_digest(CONFIG), digest({k: v for k, v in CONFIG.items() if k != "engine"}))


class InvocationTests(unittest.TestCase):
    """Resolve aliases at the transport boundary, preserving explicit efforts across resumed turns."""

    def test_turns_and_credentials_use_the_resolved_harness(self):
        config, executor = named_config(), FakeExecutor()
        agents = Agents(config, executor)
        for name in ("sol-high", "astra-high", "opus-high"):
            resolved = resolve(config, name, "review")
            context = {
                "bundle_id": agents.bundle.identity,
                "repository_commit": "a" * 40,
                "repository": {"refs": {"main": "a" * 40}, "head": "main"},
            }
            agents.ask(name, "review", context)
            request = executor.turns[-1][0]["request"]
            self.assertEqual(request["agent"], resolved.record())
            self.assertEqual(
                (request["provider"], request["model"], request["effort"]),
                (resolved.profile.harness, resolved.profile.model, "high"),
            )
            self.assertEqual(agents.credential(name, "review", None).provider, resolved.profile.harness)

    def test_explicit_effort_is_sent_on_fresh_and_resumed_cli_calls(self):
        for harness in ("codex", "claude"):
            for resume in (False, True):
                request = {
                    "provider": harness,
                    "model": "model",
                    "schema": {},
                    "gateway": GATEWAY,
                    "effort": "high",
                    **({"resume_id": "thread", "session": {"turn": 1}} if resume else {}),
                }
                command = command_for(request, "schema", "out")
                if harness == "codex":
                    self.assertIn('model_reasoning_effort="high"', command)
                else:
                    self.assertEqual(command[command.index("--effort") + 1], "high")
                    with patch.dict(os.environ, {"CONCURRO_GATEWAY_KEY": "dummy", "CLAUDE_CODE_EFFORT_LEVEL": "low"}):
                        self.assertEqual(provider_environment(request)["CLAUDE_CODE_EFFORT_LEVEL"], "high")

    def test_evaluation_uses_the_selected_profile(self):
        from bundle import Bundle
        from design_eval import cases, request

        config = named_config()
        context = request(cases()[0], "astra-high", config, Bundle(), "a" * 40)
        self.assertEqual(context["reviewer_identity"]["agent"], resolve(config, "astra-high", "plan_qc").record())
        self.assertEqual(context["reviewer_identity"]["model"], "gpt-6-astra")

    def test_retained_sessions_are_separate_for_profile_and_effort(self):
        config = named_config()
        state = {"issue": 1, "base": "a" * 40, "requirements": {}, "bundle_id": "b", "config_digest": "c"}
        first = sessions.prepare(state, config, "sol-high", "plan_qc", {})
        other = sessions.prepare(state, config, "sol-medium", "plan_qc", {})
        state["sessions"][first["role"]]["turn"] = 1
        self.assertEqual(sessions.prepare(state, config, "sol-high", "plan_qc", {})["turn"], 1)
        self.assertNotEqual(first["scope"], other["scope"])
        changed = deepcopy(config)
        changed["models"]["sol-high"]["effort"] = "medium"
        newer = sessions.prepare(state, changed, "sol-high", "plan_qc", {})
        self.assertNotEqual(first["scope"], newer["scope"])
        self.assertEqual(newer["turn"], 0)
        self.assertIsNone(sessions.prepare(state, config, "sol-high", "plan_qc", {"review_phase": "fresh"}))

    def test_fresh_runner_staffing_binds_profile_and_effort(self):
        operator = FakeOperator([{}, {}, {}])
        runner = RunnerExecutor(operator, GATEWAY, {}, ["*"], 5, lambda _: None, task=TASK)
        config = named_config()
        for name in ("sol-high", "sol-medium", "sol-high"):
            turn = deepcopy(TURN)
            agent = resolve(config, name, "review")
            turn["request"].update(provider="codex", model=agent.profile.model, stage="review", agent=agent.record())
            runner.admit(name, turn)
        bindings = [args[-1] for name, args in operator.calls if name == "submit"]
        self.assertNotEqual(bindings[0], bindings[1])
        self.assertEqual(bindings[0], bindings[2])
        self.assertEqual(bindings[0]["role"], "review:codex")


class PipelineTests(unittest.TestCase):
    """The real controller reaches gates with profile-specific votes, reports and finding dispositions."""

    def run_pipeline(self, repair=False):
        config = named_config()
        fake, agents = FakeGitHub(config), FakeAgents()
        original = agents.ask
        raised = False

        def ask(name, stage, context):
            nonlocal raised
            report = original(name, stage, context)
            if repair and not raised and name == "sol-high" and stage == "review":
                raised = True
                report.update(verdict="changes_requested", findings=[deepcopy(FINDING)])
            return report

        agents.ask = ask
        for _ in range(35):
            controller = Controller(fake, agents, config)
            controller.advance(7)
            if controller.state["stage"] == "gates":
                return controller, fake, agents
        self.fail("pipeline did not reach gates")

    def test_only_assigned_reviewers_vote_and_first_pass_can_skip_fresh_round(self):
        controller, fake, agents = self.run_pipeline()
        names = [(name, stage) for name, stage, _ in agents.calls]
        self.assertIn(("sol-medium", "intake"), names)
        self.assertIn(("opus-high", "plan"), names)
        self.assertIn(("opus-high", "develop"), names)
        self.assertEqual([name for name, stage in names if stage == "plan_qc"], ["sol-high", "astra-high"])
        self.assertEqual(set(controller.state["reviews"]), {"sol-high", "astra-high"})
        for name, report in controller.state["reviews"].items():
            self.assertEqual(report["agent"], resolve(controller.config, name, "review").record())
        self.assertTrue(all(v["first_pass"] for v in controller.state["design_votes"].values()))
        self.assertEqual(controller.state["operation"]["agent"]["profile"], "astra-high")
        self.assertEqual(controller.state["model_configuration"]["agents"], controller.config["agents"])
        bodies = "\n".join(fake.archives())
        self.assertIn("effort high", bodies)
        self.assertIn("effort medium", bodies)
        changed = deepcopy(controller.state)
        changed["reviews"].pop("astra-high")
        self.assertIn(
            "astra-high approval missing or stale",
            review_reasons(changed, fake.pr, changed["changed_files"], [], controller.config),
        )

    def test_same_vendor_findings_belong_only_to_the_raising_profile(self):
        controller, fake, agents = self.run_pipeline(repair=True)
        findings = controller.state["findings"]
        self.assertEqual([(f["provider"], f["status"]) for f in findings], [("sol-high", "fixed")])
        seen = [(name, c.get("own_findings", [])) for name, stage, c in agents.calls if stage == "review"]
        self.assertTrue(any(name == "sol-high" and fs for name, fs in seen))
        self.assertTrue(all(not fs for name, fs in seen if name == "astra-high"))
        self.assertEqual(controller.state["round"], 1)
