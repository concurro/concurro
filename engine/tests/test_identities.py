"""The controller alone authors control-plane state; conversation authors are explicit per role."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule  # unittest module fixtures
import conversation
from identities import Identities
from policy import Blocked
from fakes import CONFIG, FakeGitHub

CLAUDE = "concurro-claude[bot]"


class IdentityContractTests(unittest.TestCase):
    """Each event type and role has exactly one trusted author."""

    def test_control_events_stay_with_the_controller(self):
        """Publishers can never author state or merges, whatever role is named."""
        identities = Identities(CONFIG, publishers={"review:claude": CLAUDE})
        for event in ("state", "merge"):
            for role in (None, "review:claude"):
                with self.subTest(event=event, role=role):
                    self.assertEqual(identities.author(event, role), CONFIG["bot_login"])
        self.assertEqual(identities.author("conversation", "review:claude"), CLAUDE)
        self.assertEqual(identities.author("conversation", "review:codex"), CONFIG["bot_login"])
        self.assertEqual(identities.author("conversation"), CONFIG["bot_login"])
        with self.assertRaises(Blocked):
            identities.author("label")

    def test_every_concurro_identity_is_excluded_from_human_input(self):
        """Comments by any configured identity are never treated as discussion."""
        identities = Identities(CONFIG, publishers={"review:claude": CLAUDE})
        self.assertTrue(identities.ours(CONFIG["bot_login"]))
        self.assertTrue(identities.ours(CLAUDE))
        self.assertFalse(identities.ours("writer"))

    def test_publishers_must_be_distinct_bots(self):
        """The controller identity and human accounts cannot be conversation publishers."""
        valid = Identities(CONFIG, publishers={"review:claude": CLAUDE, "plan-qc:codex:gpt-6.1": "concurro-codex[bot]"})
        self.assertEqual(valid.author("conversation", "plan-qc:codex:gpt-6.1"), "concurro-codex[bot]")
        for publishers in ({"review:claude": CONFIG["bot_login"]}, {"review:claude": "alice"}, {"review": 1},
                           {"": CLAUDE}, {"review:claude": "[bot]"}, {"review:claude": "not a login[bot]"},
                           {"review:claude": "Concurro[bot]"}, {"review:claude": "-claude[bot]"},
                           {"review:claude": CLAUDE + " "}, {"Review": CLAUDE}, {"review:": CLAUDE},
                           {"review claude": CLAUDE}, {1: CLAUDE}):
            with self.subTest(publishers=publishers), self.assertRaises(Blocked):
                Identities(CONFIG, publishers=publishers)


class RoleRecoveryTests(unittest.TestCase):
    """Outbox recovery accepts a role's comment only from that role's expected author."""

    def test_recovery_follows_the_expected_author(self):
        """The controller's copy of a role event does not acknowledge it; the publisher's does."""
        config = deepcopy(CONFIG)
        gh = FakeGitHub(config)
        gh.comments = []
        state = {"issue": 7}
        conversation.queue(state, "Review", "Details")
        state["conversation"]["pending"][0]["role"] = "review:claude"
        body = state["conversation"]["pending"][0]["body"]
        gh.comment(body, config["bot_login"])
        gh.comment(body, CLAUDE)
        with patch("conversation.Identities", lambda c: Identities(c, publishers={"review:claude": CLAUDE})):
            conversation.publish(state, gh, config)
        self.assertEqual(len(gh.comments), 2)
        self.assertEqual(state["conversation"]["latest"], gh.comments[1]["id"])


if __name__ == "__main__":
    unittest.main()
