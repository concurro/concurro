"""Backoffice commands posted by the App act exactly as their writer's own command comments."""

import unittest
from copy import deepcopy

from support import (
    setUpModule as setUpModule,  # unittest module fixtures
    tearDownModule as tearDownModule,
)

import delegation
from controller import Controller
from fakes import CONFIG, FakeAgents, FakeGitHub

KEY = b"k" * 32
NONCE = "0123456789abcdef"


class RenderingTests(unittest.TestCase):
    """Only an exact, signed, App-authored rendering resolves to a command."""

    REPOSITORY, ISSUE, BOT = "up/project", 7, "concurro[bot]"

    def comment(self, body, login=None, identifier=5):
        return {"id": identifier, "user": {"login": login or self.BOT}, "body": body}

    def body(self, command="/concurro stop", actor="writer", key=KEY, issue=None, repository=None):
        return delegation.render(key, repository or self.REPOSITORY, issue or self.ISSUE, actor, 42, command, NONCE)

    def resolve(self, comment, key=KEY):
        return delegation.resolve(comment, key, self.REPOSITORY, self.ISSUE, self.BOT)

    def test_a_signed_rendering_stands_in_for_the_writers_comment(self):
        body = self.body("/concurro approve " + "a" * 64)
        self.assertTrue(body.startswith("@writer approved plan `aaaaaaaaaaaa` from the Concurro backoffice."))
        resolved = self.resolve(self.comment(body))
        self.assertEqual(resolved, {"id": 5, "user": {"login": "writer"}, "body": "/concurro approve " + "a" * 64})

    def test_descriptions_cover_every_command(self):
        for command in ("start", "stop", "resume", "retry", "invalidate"):
            self.assertIn("@writer", self.body(delegation.command_text(command)))
        self.assertIn("granted 2 more plan revisions", self.body(delegation.command_text("retry", 2)))
        with self.assertRaises(ValueError):
            delegation.command_text("merge")
        with self.assertRaises(ValueError):
            delegation.command_text("approve", "not-a-digest")

    def test_anything_else_is_left_alone(self):
        body = self.body()
        self.assertIsNone(self.resolve(self.comment(body, login="writer")), "a human posting the rendering")
        altered = {
            "another key": self.body(key=b"x" * 32),
            "another issue": self.body(issue=8),
            "another repository": self.body(repository="other/project"),
            "a quote inside a longer App comment": "Reviewer notes:\n> " + body,
            "text appended to the rendering": body + "\n",
            "an edited actor": body.replace('"actor": "writer"', '"actor": "intruder"'),
            "an edited description": body.replace("stopped", "approved"),
        }
        for name, text in altered.items():
            with self.subTest(name):
                self.assertNotEqual(text, body)
                self.assertIsNone(self.resolve(self.comment(text)))
        self.assertIsNone(self.resolve(self.comment(body), key=None))
        comments = [self.comment(body), self.comment("plain", login="writer", identifier=6)]
        self.assertIs(delegation.effective(comments, None, self.REPOSITORY, self.ISSUE, self.BOT), comments)
        effective = delegation.effective(comments, KEY, self.REPOSITORY, self.ISSUE, self.BOT)
        self.assertEqual([c["user"]["login"] for c in effective], ["writer", "writer"])
        self.assertEqual(effective[1], comments[1])


class ControllerTests(unittest.TestCase):
    """The controller applies a delegated command as the writer's, with the usual writer re-check."""

    def setUp(self):
        self.config = deepcopy(CONFIG)
        self.gh = FakeGitHub(self.config)
        self.gh.comments = []
        self.agents = FakeAgents()
        self.bot = self.config["bot_login"]

    def delegate(self, command, actor="writer", key=KEY):
        body = delegation.render(key, self.config["repository"], 7, actor, 42, command, NONCE)
        self.gh.comment(body, login=self.bot)

    def tick(self, key=KEY):
        controller = Controller(self.gh, self.agents, self.config, delegation_key=key)
        controller.advance(7)
        return controller.state

    def test_delegated_stop_and_retry_act_as_the_writer(self):
        self.delegate("/concurro stop")
        self.assertEqual(self.tick()["stage"], "stopped")
        self.assertFalse(self.agents.calls)
        self.delegate("/concurro retry")
        self.assertEqual(self.tick()["stage"], "plan")

    def test_without_the_key_or_from_a_non_writer_nothing_happens(self):
        self.delegate("/concurro stop", key=b"x" * 32)
        self.delegate("/concurro stop", actor="intruder")
        self.assertEqual(self.tick()["stage"], "plan")

    def test_a_controller_without_the_key_ignores_delegated_commands(self):
        self.delegate("/concurro stop")
        self.assertEqual(self.tick(key=None)["stage"], "plan")

    def test_a_delegated_approval_binds_the_writer_and_stays_valid(self):
        self.config["autonomous"] = False
        self.gh = FakeGitHub(self.config)
        self.gh.comments = []
        self.delegate("/concurro start")
        state = None
        for _ in range(6):
            state = self.tick()
            if state["stage"] == "planned":
                break
        self.assertEqual(state["stage"], "planned")
        self.delegate("/concurro approve " + state["plan_id"])
        state = self.tick()
        self.assertEqual(state["approval"]["kind"], "human")
        self.assertEqual(state["approval"]["actor"], "writer")
        self.assertNotEqual(state["stage"], "planned")
        # Later ticks re-verify the approval comment by author and exact command.
        self.assertNotEqual(self.tick()["stage"], "blocked")
