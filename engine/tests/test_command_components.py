"""Contracts at the command parser and revision-budget extraction boundaries."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import Mock

import support  # noqa: F401 -- initializes engine imports for standalone discovery
from commands import Command, CommandKind, parse
from revisions import RevisionBudget


class CommandParsingTests(unittest.TestCase):
    """Keep typed parsing compatible with the existing writer-command grammar."""

    def test_arguments_and_command_kinds(self):
        """Only grants convert their argument; approval IDs stay exact strings."""
        plan = "a0" * 32
        cases = {
            "/concurro start": Command(CommandKind.START),
            "/concurro stop": Command(CommandKind.STOP),
            "/concurro retry": Command(CommandKind.RETRY),
            "/concurro invalidate": Command(CommandKind.INVALIDATE),
            "/concurro approve " + plan: Command(CommandKind.APPROVE, plan),
            "/concurro retry --plan-revisions 003": Command(CommandKind.EXTEND, 3),
            "/concurro retry --plan-revisions 0": Command(CommandKind.EXTEND, 0),
            "/concurro retry --plan-revisions 999": Command(CommandKind.EXTEND, 999),
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                self.assertEqual(parse(" \n" + body + "\t"), expected)
        command = parse("/concurro start")
        with self.assertRaises(FrozenInstanceError):
            command.kind = CommandKind.STOP

    def test_invalid_syntax_is_not_reinterpreted(self):
        """Case, interior whitespace, decoration and numeric syntax stay strict."""
        for body in (
            "/concurro Start",
            "/concurro  start",
            "/concurro\tstart",
            "/concurro start\nplease",
            "`/concurro start`",
            "/concurro approve " + "A" * 64,
            "/concurro approve " + "a" * 63,
            "/concurro retry --plan-revisions -1",
            "/concurro retry --plan-revisions 1000",
            "/concurro retry --plan-revisions １",
            "/concurro retry --plan-revisions 1 extra",
        ):
            with self.subTest(body=body):
                self.assertIsNone(parse(body))


class RevisionBudgetTests(unittest.TestCase):
    """Pin invalidation and save boundaries independently of the controller."""

    def test_success_invalidates_before_reserving_without_saving(self):
        """Successful revisions consume one grant and leave persistence to the caller."""
        state = {
            "stage": "plan_qc",
            "plan_revisions": 2,
            "plan_revision_extra": 1,
            "revision_wait": {"stage": "plan"},
            "calls": 9,
        }
        before = deepcopy(state)
        seen = []
        save = Mock()
        budget = RevisionBudget(state, {"max_plan_revisions": 2})
        self.assertTrue(
            budget.revise(
                "plan", "Revise.", invalidate_design=lambda reason: seen.append((reason, deepcopy(state))), save=save
            )
        )
        self.assertEqual(seen, [("Revise.", before)])
        self.assertEqual(
            state, {"stage": "plan", "plan_revisions": 3, "plan_revision_extra": 1, "calls": 9, "reason": "Revise."}
        )
        save.assert_not_called()

    def test_exhaustion_callbacks_observe_the_original_order_and_flags(self):
        """Invalidation precedes escalation; an optional save sees the complete waiting state."""
        for invalidate in (False, True):
            for save_exhausted in (False, True):
                with self.subTest(invalidate=invalidate, save_exhausted=save_exhausted):
                    state = {"stage": "plan_qc", "plan_revisions": 3, "plan_revision_extra": 1}
                    before = deepcopy(state)
                    seen = []
                    budget = RevisionBudget(state, {"max_plan_revisions": 2})
                    self.assertFalse(
                        budget.revise(
                            "intake",
                            "Changed inputs.",
                            invalidate_design=lambda reason: seen.append(("invalidate", reason, deepcopy(state))),
                            save=lambda: seen.append(("save", deepcopy(state))),
                            invalidate=invalidate,
                            save_exhausted=save_exhausted,
                        )
                    )
                    expected = {
                        **before,
                        "stage": "escalated",
                        "retry_stage": "intake",
                        "revision_wait": {"stage": "intake"},
                        "reason": "Plan revision budget exhausted: Changed inputs.",
                    }
                    self.assertEqual(state, expected)
                    self.assertEqual(
                        seen,
                        ([("invalidate", "Changed inputs.", before)] if invalidate else [])
                        + ([("save", expected)] if save_exhausted else []),
                    )

    def test_invalidation_failure_prevents_reservation_and_save(self):
        """An invalidation failure must not spend capacity or persist a partial revision."""
        state = {"stage": "plan_qc", "plan_revisions": 0}
        before = deepcopy(state)
        invalidate = Mock(side_effect=RuntimeError("interrupted"))
        save = Mock()
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            RevisionBudget(state, {"max_plan_revisions": 2}).revise(
                "plan", "Revise.", invalidate_design=invalidate, save=save
            )
        self.assertEqual(state, before)
        save.assert_not_called()
