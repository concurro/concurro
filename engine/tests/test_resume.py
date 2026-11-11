"""Resume adopts current execution settings without throwing away checkpointed work."""

import unittest
from copy import deepcopy
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import commands
import test_bundle_lifecycle as lifecycle
from checkpoints import legacy_state
from controller import config_digest
from github import Transient
from policy import approval_valid, design_roster


class ResumeTests(unittest.TestCase):
    """Real command, checkpoint and stage transitions with fake external services."""

    setUp = lifecycle.BundleLifecycleTests.setUp
    controller = lifecycle.BundleLifecycleTests.controller
    loaded = lifecycle.BundleLifecycleTests.loaded
    tick = lifecycle.BundleLifecycleTests.tick
    reach = lifecycle.BundleLifecycleTests.reach
    upgrade = lifecycle.BundleLifecycleTests.upgrade

    def resume(self, controller=None):
        """Process the command without dispatching the next tick's model work."""
        self.gh.comment("/concurro resume")
        controller = controller or self.loaded()
        controller.commands(self.gh.comments)
        return controller

    def test_arbitrary_bundle_and_policy_update_preserves_approved_work(self):
        """Resume needs no predeclared old/new bundle pair or unchanged configuration."""
        controller = self.reach("implement")
        controller.suspend("blocked", "old failure")
        before = deepcopy(controller.state)
        self.upgrade()
        new = deepcopy(self.config)
        new["paths"] = {"protected_extensions": [], "protected_files": [], "protected_prefixes": []}
        new["limits"] = {"changed_files": None, "workspace_files": None}
        controller = self.loaded()
        controller.resume_target = lambda: (new, self.controller().bundle)
        self.resume(controller)
        self.assertEqual(controller.config, new)
        self.assertEqual(controller.state["execution"], new["paths"])
        self.assertEqual(controller.state["stage"], "implement")
        for key in (
            "plan",
            "plan_id",
            "approval",
            "design_votes",
            "calls",
            "credentials",
            "objections",
            "findings",
            "round",
            "plan_revisions",
        ):
            self.assertEqual(controller.state.get(key), before.get(key), key)
        self.assertTrue(approval_valid(controller.state, new))
        self.assertEqual(controller.state["bundle_resume"]["config_digest"], config_digest(new))
        controller.require_current_bundle()
        self.assertEqual(legacy_state(controller.checkpoints.audit_body(controller.state))["state"]["version"], 7)

    def test_exhausted_allowance_does_not_refuse_resume_or_reset_spend(self):
        """Dispatch enforces its budget normally; resume itself is not another budget gate."""
        controller = self.reach("implement")
        controller.state["calls"] = self.config["max_model_calls"]
        controller.suspend("blocked", "model-call budget exhausted")
        self.resume(controller)
        self.assertEqual(controller.state["stage"], "implement")
        self.assertEqual(controller.state["calls"], self.config["max_model_calls"])
        self.assertEqual(
            controller.state["command_id"],
            next(c["id"] for c in reversed(self.gh.comments) if c["body"] == "/concurro resume"),
        )

    def test_raise_configured_budget_keeps_spent_calls(self):
        """A newly configured allowance is used without resetting the original spending ledger."""
        controller = self.reach("implement")
        controller.state["calls"] = self.config["max_model_calls"]
        controller.suspend("blocked", "budget")
        new = {**deepcopy(self.config), "max_model_calls": self.config["max_model_calls"] + 5}
        controller.resume_target = lambda: (new, controller.bundle)
        self.resume(controller)
        self.assertEqual(controller.state["calls"], self.config["max_model_calls"])
        self.assertEqual(controller.config["max_model_calls"], new["max_model_calls"])
        self.assertTrue(approval_valid(controller.state, controller.config))

    def test_writer_required_and_exact_grammar(self):
        """Only a writer's exact command adopts new settings."""
        controller = self.reach("implement")
        controller.suspend("stopped")
        before = deepcopy(controller.state)
        self.gh.comment("/concurro resume", login="reader")
        controller.commands(self.gh.comments)
        self.assertEqual(controller.state, before)
        self.assertEqual(commands.parse("/concurro resume").kind, commands.CommandKind.RESUME)
        self.assertIsNone(commands.parse("/concurro resume anything"))

    def test_retained_result_and_uncertainty_survive_resume(self):
        """Resume does not discard a result or erase the uncertain-attempt count."""
        for status in ("started", "uncertain", "completed", "failed"):
            with self.subTest(status=status):
                self.setUp()
                controller = self.reach("implement")
                operation = {
                    "id": "old",
                    "attempt": "attempt",
                    "attempts": 3,
                    "status": status,
                    "uncertain": 1,
                    "provider": self.config["implementer"],
                    "stage": "develop",
                    "contract": {},
                }
                if status == "completed":
                    operation["response"] = {"summary": "saved response"}
                controller.state["operation"] = operation
                controller.release_result()
                controller.suspend("blocked", "transport")
                self.upgrade()
                controller = self.loaded()
                self.resume(controller)
                saved = controller.state["operation"]
                self.assertEqual(saved["status"], status)
                self.assertEqual(saved["uncertain"], 1)
                self.assertEqual(saved["attempts"], 3)
                self.assertEqual(saved.get("response"), operation.get("response"))
                rebound = controller.operation(self.config["implementer"], "develop", controller.context())
                self.assertEqual(rebound["uncertain"], 1)
                self.assertEqual(rebound["attempts"], 3)
                if status == "completed":
                    self.assertEqual(controller.recover(rebound), operation["response"])
                    self.assertEqual(rebound["id"], "old")

    def test_pending_publication_is_kept(self):
        """Publication continues through its existing idempotent recovery path."""
        controller = self.reach("publish")
        controller.suspend("blocked", "publication interrupted", outboxes=False)
        before = deepcopy(controller.state)
        self.resume(controller)
        self.assertEqual(controller.state["stage"], "publish")
        for key in ("pending", "candidate", "approval", "calls"):
            self.assertEqual(controller.state[key], before[key])

    def test_questions_and_manual_approval_are_not_fabricated(self):
        """Adopting settings leaves unanswered questions and waiting approval available to their normal flows."""
        self.config["autonomous"] = False
        self.gh.comment("/concurro start")
        controller = self.reach("planned")
        self.resume(controller)
        self.assertEqual(controller.state["stage"], "planned")
        self.assertNotIn("approval", controller.state)
        controller.state["stage"] = "clarify"
        controller.state["plan"]["questions"] = ["Which behavior?"]
        self.resume(controller)
        self.assertEqual(controller.state["stage"], "clarify")
        self.assertEqual(controller.state["plan"]["questions"], ["Which behavior?"])

    def test_lost_save_reply_does_not_reapply(self):
        """A replay uses the saved adoption receipt rather than rotating sessions twice."""
        controller = self.reach("implement")
        controller.suspend("blocked", "upgrade")
        self.upgrade()
        controller = self.loaded()
        save = controller.save

        def lost(*args, **kwargs):
            """Lose the reply after the real checkpoint is durable."""
            save(*args, **kwargs)
            raise Transient("lost reply")

        with patch.object(controller, "save", side_effect=lost), self.assertRaises(Transient):
            self.resume(controller)
        controller = self.loaded()
        before = deepcopy(controller.state)
        controller.commands(self.gh.comments)
        self.assertEqual(controller.state, before)

    def test_new_implementer_keeps_original_design_approval(self):
        """An execution assignment change does not make the engine repeat completed design review."""
        controller = self.reach("implement")
        controller.suspend("blocked", "upgrade")
        before = deepcopy(controller.state["approval"])
        new = deepcopy(self.config)
        new["implementer"] = "codex" if new["implementer"] == "claude" else "claude"
        controller.resume_target = lambda: (new, controller.bundle)
        self.resume(controller)
        self.assertEqual(controller.config["implementer"], new["implementer"])
        self.assertEqual(controller.state["approval"], before)
        self.assertTrue(approval_valid(controller.state, new))
        controller.design_authorization()

    def test_changed_charter_is_adopted_without_rewriting_old_approval(self):
        """Current role instructions use the new charter while old votes retain their provenance."""
        controller = self.reach("implement")
        controller.suspend("blocked", "upgrade")
        before = deepcopy(controller.state)
        new = deepcopy(controller.bundle)
        new.charter["constraints"].append("New operator guidance")
        new.identity = "f" * 64
        controller.resume_target = lambda: (controller.config, new)
        self.resume(controller)
        self.assertEqual(controller.bundle.charter, new.charter)
        self.assertEqual(controller.state["charter"], before["charter"])
        self.assertEqual(controller.state["approval"], before["approval"])
        self.assertTrue(approval_valid(controller.state, controller.config))
        controller.require_current_bundle()

    def test_changed_pr_review_team_rechecks_without_erasing_findings(self):
        """New reviewers use the current config and old findings remain unresolved evidence."""
        controller = self.reach("gates")
        prior = deepcopy(controller.state)
        new = deepcopy(self.config)
        new["models"][new["reviewers"][0]] += "-updated"
        controller.resume_target = lambda: (new, controller.bundle)
        self.resume(controller)
        self.assertEqual(controller.state["stage"], "review")
        self.assertEqual(controller.state["reviews"], {})
        self.assertEqual(controller.state.get("findings"), prior.get("findings"))
        self.assertEqual(controller.state["approval"], prior["approval"])
        saved = next(v for v in controller.state["resume_history"] if v.get("field") == "reviews")
        self.assertEqual(saved["value"], prior["reviews"])

    def test_later_invalidation_uses_adopted_configuration(self):
        """An explicit new assessment is not wedged by the original approval's config digest."""
        controller = self.reach("implement")
        new = {**self.config, "max_model_calls": self.config["max_model_calls"] + 5}
        controller.resume_target = lambda: (new, controller.bundle)
        self.resume(controller)
        self.gh.comment("/concurro invalidate")
        controller.commands(self.gh.comments)
        self.assertEqual(controller.state["stage"], "intake")
        self.assertEqual(controller.state["config_digest"], config_digest(new))
        self.assertNotIn("bundle_resume", controller.state)

    def test_runner_termination_is_not_an_immortal_failure_marker(self):
        """The writer resumes ordinary recovery while retaining attempt and spending records."""
        controller = self.reach("implement")
        controller.state["operation"] = {
            "id": "old",
            "status": "uncertain",
            "attempts": 1,
            "uncertain": 0,
            "runner_termination": "runner ended",
        }
        controller.suspend("blocked", "runner ended")
        self.resume(controller)
        self.assertNotIn("runner_termination", controller.state["operation"])
        self.assertEqual(controller.state["operation"]["status"], "uncertain")
        self.assertTrue(any(v.get("runner_termination") == "runner ended" for v in controller.state["resume_history"]))

    def test_standalone_tick_adopts_new_local_config_before_path_drift_check(self):
        """The direct engine entry point can resume a changed policy, just like the service."""
        from controller import Controller
        from policy import path_allowed

        controller = self.reach("implement")
        controller.suspend("blocked", "old policy")
        new = deepcopy(self.config)
        new["paths"] = {"protected_extensions": [], "protected_files": [], "protected_prefixes": []}
        self.gh.comment("/concurro resume")
        current = Controller(self.gh, self.agents, new, self.root, self.root)
        current.advance(7)
        self.assertEqual(current.state["stage"], "implement")
        self.assertTrue(path_allowed(".github/workflows/new.yml", current.config))
        self.assertFalse(path_allowed(".concurro/config.yaml", current.config))

    def test_resume_next_step_runs_under_new_settings(self):
        """A reloaded controller accepts adopted policy and reaches publication without reassessing."""
        from controller import Controller

        controller = self.reach("implement")
        controller.suspend("blocked", "old policy")
        new = deepcopy(self.config)
        new["paths"] = {"protected_extensions": [], "protected_files": [], "protected_prefixes": []}
        controller.resume_target = lambda: (new, controller.bundle)
        self.resume(controller)
        current = Controller(self.gh, self.agents, new, self.root, self.root)
        current.advance(7)
        self.assertEqual(current.state["stage"], "publish")
        self.assertEqual(self.agents.calls[-1][1], "develop")

    def test_repeat_resume_does_not_rotate_an_already_adopted_session(self):
        """A second writer command with unchanged settings preserves the active session."""
        import sessions

        controller = self.reach("implement")
        role = sessions.prepare(
            controller.state, controller.config, controller.config["implementer"], "develop", controller.context()
        )
        controller.suspend("blocked", "upgrade")
        self.upgrade()
        controller = self.loaded()
        self.resume(controller)
        once = deepcopy(controller.state["sessions"][role["role"]])
        self.resume(controller)
        self.assertEqual(controller.state["sessions"][role["role"]], once)
        self.assertTrue(approval_valid(controller.state, controller.config))

    def test_new_assessment_binds_current_settings_after_resume(self):
        """An explicit design revision records current governance instead of inheriting old approval."""
        controller = self.reach("implement")
        new = deepcopy(self.config)
        new["max_model_calls"] += 5
        controller.resume_target = lambda: (new, controller.bundle)
        self.resume(controller)
        self.assertIn("approval_roster", controller.state["bundle_resume"])
        controller.state["stage"] = "plan"
        controller.plan()
        self.assertEqual(controller.state["stage"], "plan_qc")
        self.assertNotIn("approval_roster", controller.state["bundle_resume"])
        self.assertEqual(controller.state["config_digest"], config_digest(new))
        self.assertEqual(controller.state["bundle_id"], controller.bundle.identity)

    def test_incomplete_design_with_changed_team_keeps_objections_and_spend(self):
        """New review identities do not inherit votes or delete prior objections."""
        controller = self.reach("plan_qc")
        controller.state["objections"] = [
            {
                "id": "O1",
                "reviewer": design_roster(self.config)[1]["id"],
                "resolved": False,
                "text": "Still needs an answer",
            }
        ]
        before = deepcopy(controller.state)
        new = deepcopy(self.config)
        new["models"][new["reviewers"][0]] += "-updated"
        controller.resume_target = lambda: (new, controller.bundle)
        self.resume(controller)
        self.assertEqual(controller.state["stage"], "plan_qc")
        self.assertEqual(controller.state["design_votes"], {})
        self.assertEqual(controller.state["objections"], before["objections"])
        self.assertEqual(controller.state["calls"], before["calls"])
        self.assertEqual(controller.state["design_generation"], before["design_generation"] + 1)
