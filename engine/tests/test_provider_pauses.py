"""Trusted provider conditions pause without paid restarts and drain admitted peers."""

import unittest
from copy import deepcopy
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import admissions
from controller import Controller
from credentials import Credential
from fakes import BatchAgents
from pipeline_fixtures import ConsensusFixture
from policy import Blocked, ProviderFailure, design_roster, digest
from vocab import SUSPENDED_STAGES


def choice(provider):
    """Freeze independent synthetic provider consent using production admission validation."""
    canonical = "openai" if provider == "codex" else "anthropic"
    actor = "101" if canonical == "openai" else "202"
    route = canonical + "-api-key"
    credential = Credential(
        digest(["contributed", actor, route, digest([actor]), 1]),
        provider,
        "contributed",
        "gateway-key",
        actor,
        route,
        digest([actor]),
        1,
    )
    slot = {
        "sponsor_id": actor,
        "epoch": 1,
        "grant_id": digest([actor, 1]),
        "status": "active",
        "provenance": "explicit_comment",
    }
    return admissions.selection(credential, canonical, slot)


class PauseTests(ConsensusFixture, unittest.TestCase):
    """Use the real controller/review coordinator and checkpoint retention boundaries."""

    def setUp(self):
        """Reach a persisted plan with controllable asynchronous reviewers."""
        super().setUp()
        self.agents = BatchAgents()
        self.reach("plan_qc")
        self.agents.events.clear()
        self.first, self.second = [m["provider"] for m in design_roster(self.config)]
        selected = choice(self.first)
        self.failure = ProviderFailure(
            {"category": "auth", "binding": {"sponsorship_provider": selected["sponsorship_provider"]}}
        )

    def selections(self, controller, requests):
        """Stand in only for external metadata/consent reads; preserve actual freezing and charging."""
        return [choice(provider) for provider, _stage, _context in requests]

    def test_mixed_provider_failure_drains_and_retains_peer_without_admitting_more(self):
        """A trusted failure cannot cancel a different payer's running admitted review or restart either."""
        self.agents.effects[self.first] = [self.failure]
        self.agents.delay[self.second] = 3
        before = self.loaded().state["calls"]
        with patch.object(
            Controller, "select_admissions", lambda controller, requests: self.selections(controller, requests)
        ):
            with self.assertRaises(ProviderFailure) as error:
                self.tick()
        self.assertIs(error.exception, self.failure)
        self.assertEqual(len(self.agents.handles), 2)
        first, second = self.agents.handles
        self.assertTrue(first.cancelled)
        self.assertFalse(second.cancelled)
        self.assertGreater(second.polls, 3)
        state = self.loaded().state
        self.assertEqual(state["calls"], before + 2)
        members = list(state["review_batch"]["members"].values())
        self.assertTrue(any(m["operation"]["status"] == "consumed" for m in members))
        self.assertEqual(len(state["design_votes"]), 1)

    def test_all_failed_members_are_retained_in_one_pause(self):
        """A single writer recovery acknowledges both independently observed roster failures."""
        failures = []
        for index, provider in enumerate((self.first, self.second), 1):
            failure = ProviderFailure(
                {
                    "category": "auth",
                    "turn": "failed-" + provider,
                    "session": "session-" + provider,
                    "sequence": index,
                    "binding": {"sponsorship_provider": choice(provider)["sponsorship_provider"]},
                }
            )
            failures.append(failure)
            self.agents.effects[provider] = [failure]
        with patch.object(
            Controller, "select_admissions", lambda controller, requests: self.selections(controller, requests)
        ):
            with self.assertRaises(ProviderFailure) as raised:
                self.tick()
        self.assertEqual([o["turn"] for o in raised.exception.observations], [f.observation["turn"] for f in failures])
        self.assertTrue(all(h.cancelled for h in self.agents.handles))
        self.assertEqual(len(self.agents.handles), 2)

    def test_reservation_time_failure_drains_existing_peer(self):
        """Failure discovered after a poll cannot cancel admitted peers during correction reservation."""
        calls = []

        def select(controller, requests):
            """Expose one trusted late row at the real second reservation boundary."""
            calls.append(requests)
            if len(calls) > 1:
                raise self.failure
            return self.selections(controller, requests)

        self.agents.delay[self.second] = 3
        # Force the completed first member to need a correction without changing admission machinery.
        from design_batch import Design

        apply = Design.apply

        def correction(adapter, key):
            """Keep the first ready vote as a correction request until the preflight refusal."""
            result = apply(adapter, key)
            if len(calls) == 1 and adapter.batch["members"][key]["identity"]["provider"] == self.first:
                adapter.controller.state["design_votes"].pop(key, None)
            return result

        with patch.object(Controller, "select_admissions", select), patch.object(Design, "apply", correction):
            with self.assertRaises(ProviderFailure):
                self.tick()
        self.assertFalse(self.agents.handles[1].cancelled)
        self.assertEqual(len(self.agents.handles), 2)

    def test_ownership_loss_while_draining_cancels_every_remaining_member(self):
        """The drain exception never relaxes task ownership or terminal cancellation."""
        self.agents.effects[self.first] = [self.failure]
        self.agents.delay[self.second] = 3
        idle = self.agents.idle

        def lose():
            """Lose ownership at the first drain wait, after the provider failure."""
            idle()
            self.agents.owner = False

        with (
            patch.object(self.agents, "idle", side_effect=lose),
            patch.object(
                Controller, "select_admissions", lambda controller, requests: self.selections(controller, requests)
            ),
        ):
            with self.assertRaisesRegex(Blocked, "ownership"):
                self.tick()
        self.assertTrue(all(handle.cancelled for handle in self.agents.handles))
        self.assertEqual(len(self.agents.handles), 2)

    def test_terminal_peer_verdict_takes_precedence_over_provider_pause(self):
        """A saved escalation still terminates the batch instead of becoming a credential pause."""
        self.agents.effects[self.first] = [self.failure]
        self.agents.effects[self.second] = [lambda report: {**report, "decision": "escalate"}]
        with patch.object(
            Controller, "select_admissions", lambda controller, requests: self.selections(controller, requests)
        ):
            state = self.tick()
        self.assertEqual(state["stage"], "escalated")
        self.assertEqual(len(self.agents.handles), 2)

    def test_new_pause_stages_wait_without_calls_and_retry_retains_resume_target(self):
        """Existing writer retry transport handles both new pauses without automatic scheduling."""
        for stage in ("awaiting_credentials", "budget_exhausted"):
            self.assertIn(stage, SUSPENDED_STAGES)
            controller = self.loaded()
            controller.suspend(stage, "provider correction required")
            before = controller.state["calls"]
            paused = self.tick()
            self.assertEqual(paused["stage"], stage)
            self.assertEqual(paused["calls"], before)
            self.assertEqual(paused["retry_stage"], "plan_qc")
            self.gh.comment("/concurro retry")
            resumed = self.tick()
            self.assertNotEqual(resumed["stage"], stage)
            # Restore the same persisted phase for the next vocabulary case.
            state = deepcopy(paused)
            state["stage"] = "plan_qc"
            state["command_id"] = resumed["command_id"]
            state.pop("retry_stage", None)
            self.persist(state)
