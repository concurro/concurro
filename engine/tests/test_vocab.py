"""Compatibility of controller stage groups and native turn results."""

import unittest
from copy import deepcopy

from support import CONFIG, setUpModule as setUpModule, tearDownModule as tearDownModule

import actions
import bundle
from agents import Agents
from controller import Controller
from github import STAGES
from policy import Blocked, SessionFailure
from runner_fixtures import TURN, ExecutorFixture


class VocabularyCompatibilityTests(ExecutorFixture, unittest.TestCase):
    """Pin existing public collections and fallback behavior before replacing literals."""

    def test_public_stage_collections(self):
        """Public collections retain their distinct memberships and ordered agent roles."""
        self.assertEqual(
            STAGES,
            {
                "intake",
                "clarify",
                "plan",
                "plan_qc",
                "planned",
                "implement",
                "publish",
                "review",
                "gates",
                "merging",
                "merged",
                "observed_merged",
                "closed",
                "blocked",
                "stopped",
                "rejected",
                "escalated",
            },
        )
        self.assertEqual(
            actions.RESUMED_STAGES,
            {
                "intake",
                "plan",
                "plan_qc",
                "planned",
                "clarify",
                "implement",
                "publish",
                "review",
                "gates",
                "merging",
            },
        )
        self.assertEqual(bundle.STAGES, ("intake", "plan", "plan_qc", "develop", "review"))

    def test_unknown_result_keeps_decode_failure_and_session_semantics(self):
        """Unrecognized outcomes stay generic failures, not uncertain calls or enum errors."""
        agents = Agents(CONFIG, None)
        for stage in ("intake", "plan", "plan_qc", "develop", "review"):
            for session in (None, {"scope": "legacy"}):
                with self.subTest(stage=stage, session=session):
                    expected = SessionFailure if session else Blocked
                    with self.assertRaises(expected) as caught:
                        agents.decode_result(
                            session,
                            "claude",
                            stage,
                            {
                                "outcome": "future-result",
                                "response": {"private": "discard"},
                            },
                        )
                    self.assertIs(type(caught.exception), expected)
                    self.assertEqual(
                        str(caught.exception),
                        "provider session failed; output suppressed to protect credentials"
                        if session
                        else "claude failed; output suppressed to protect credentials",
                    )
                    if session:
                        self.assertFalse(caught.exception.uncertain)

    def test_unknown_result_is_returned_without_executor_cancellation(self):
        """Serial cleanup only withdraws the specific outcomes it already recognizes."""
        for stage in ("plan_qc", "develop", "review"):
            with self.subTest(stage=stage):
                result = {"outcome": "future-result", "response": None}
                executor, operator, _ = self.runner([{"status": "done", "result": result}])
                turn = deepcopy(TURN)
                turn["request"]["stage"] = stage
                self.assertIs(executor.run(turn, "test"), result)
                self.assertFalse(any(call[0] in ("cancel", "end") for call in operator.calls))

    def test_scheduler_preserves_unknown_stage_fallback(self):
        """Unknown persisted stages still yield the scheduler's existing continuation."""
        controller = Controller.__new__(Controller)
        controller.foreign, controller.issue = False, {}
        controller.state = {"stage": "legacy-future-stage"}
        result = controller.outcome(False)
        self.assertEqual((result.kind, result.progressed, result.retry_after), ("wait-until", False, 300))

    def test_scheduler_stage_groups(self):
        """Terminal completion, retryable decisions and human waits are distinct concepts."""
        controller = Controller.__new__(Controller)
        controller.foreign, controller.issue = False, {}
        expected = {
            "intake": "wait-until",
            "clarify": "await-human",
            "plan": "wait-until",
            "plan_qc": "wait-until",
            "planned": "await-human",
            "implement": "wait-until",
            "publish": "wait-until",
            "review": "wait-until",
            "gates": "wait-until",
            "merging": "wait-until",
            "merged": "terminal",
            "observed_merged": "terminal",
            "closed": "terminal",
            "blocked": "blocked",
            "stopped": "blocked",
            "rejected": "blocked",
            "escalated": "blocked",
        }
        for stage, kind in expected.items():
            with self.subTest(stage=stage):
                controller.state = {"stage": stage}
                self.assertEqual(controller.outcome(False).kind, kind)


class VocabularyTests(unittest.TestCase):
    """Enum names add meaning without changing serialized values or memberships."""

    def test_values_and_serialization(self):
        """Every member keeps its original spelling, JSON bytes, formatting and digest."""
        import json

        from policy import digest
        from vocab import Stage, TurnOutcome

        for enum, values, key in (
            (
                Stage,
                (
                    "intake",
                    "clarify",
                    "plan",
                    "plan_qc",
                    "planned",
                    "implement",
                    "publish",
                    "review",
                    "gates",
                    "merging",
                    "merged",
                    "observed_merged",
                    "closed",
                    "blocked",
                    "stopped",
                    "rejected",
                    "escalated",
                ),
                "stage",
            ),
            (
                TurnOutcome,
                ("ok", "failed", "uncertain", "lost", "timed_out", "terminated", "admission_unknown", "queue_expired"),
                "outcome",
            ),
        ):
            self.assertEqual(tuple(member.value for member in enum), values)
            for member, value in zip(enum, values, strict=True):
                with self.subTest(enum=enum.__name__, value=value):
                    self.assertEqual(member, value)
                    self.assertEqual(str(member), value)
                    self.assertEqual(hash(member), hash(value))
                    self.assertEqual(json.dumps({key: member}), json.dumps({key: value}))
                    self.assertEqual(digest({key: member}), digest({key: value}))
        self.assertEqual(json.dumps({"stage": Stage.PLAN}), '{"stage": "plan"}')
        self.assertEqual(Stage("plan"), "plan")

    def test_stage_sets_keep_distinct_meanings(self):
        """A closed PR is terminal but not merged; pauses and judgments remain separate."""
        from vocab import JUDGED_STAGES, MERGED_STAGES, SUSPENDED_STAGES, TERMINAL_STAGES

        self.assertEqual(MERGED_STAGES, {"merged", "observed_merged"})
        self.assertEqual(TERMINAL_STAGES, {"merged", "observed_merged", "closed"})
        self.assertEqual(SUSPENDED_STAGES, {"blocked", "stopped"})
        self.assertEqual(JUDGED_STAGES, {"rejected", "escalated"})
        for group in (MERGED_STAGES, TERMINAL_STAGES, SUSPENDED_STAGES, JUDGED_STAGES):
            self.assertNotIn("legacy-future-stage", group)
