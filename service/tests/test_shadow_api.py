"""Private daemon projection/action transport verifies the requesting GitHub identity and visibility."""

import json
import unittest
from unittest.mock import Mock

from fixtures import KEY, ServiceFixture

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import delegation
from controller import Outcome
from service.actions import Actions
from service.shadow import Shadow
from service.targets import Target
from service.transport import Response


class ShadowApiTests(ServiceFixture, unittest.TestCase):
    """No task/checkpoint or model admission is needed to inspect or withdraw an issue intent."""

    def actions(self, visible=True, permission="read"):
        """Use independent user authentication and a synthetic App comment writer."""
        reader = Mock()
        reader.request.return_value = {"permission": permission}
        self.poster = Mock()
        self.poster.send.return_value = Response(
            True, 201, {}, json.dumps({"id": 77, "html_url": "https://github.invalid/comment"}).encode()
        )
        return Actions(
            b"synthetic-key",
            self.registry,
            lambda repo: reader,
            lambda repo: self.poster,
            [Target(KEY[0], KEY[0], "both")],
            user=lambda token: ("alice", 202) if token == "user-token" else None,
            visible=lambda token, repo, issue: visible,
            clock=self.clock,
        )

    def test_pre_task_projection_and_hidden_issue_are_independent_of_writes(self):
        """Authenticated visibility, not writer permission, controls read access."""
        self.registry.confirm_sponsorship(
            KEY,
            0,
            {
                "slots": {"openai": {"sponsor_id": "202"}},
                "facts": [{"provider": "openai", "actor_id": "202"}],
                "revision": "fixed",
            },
            [],
        )
        body = json.dumps({"repository": KEY[0], "issue": 7}).encode()
        status, payload = self.actions().sponsorships("Bearer user-token", body)
        self.assertEqual((status, payload["status"], payload["scope"]), (200, "confirmed", "own"))
        self.assertNotIn("funding", payload)
        self.assertNotIn("calls", payload)
        self.assertEqual(self.poster.send.call_count, 0)
        self.assertEqual(self.actions(False).sponsorships("Bearer user-token", body)[0], 404)
        self.assertEqual(self.actions().sponsorships("Bearer bad", body)[0], 401)

    def test_self_withdraw_after_writer_loss_signs_only_the_authenticated_actor(self):
        """Withdrawal can revoke consent after writer loss, without gaining sponsorship authority."""
        actions = self.actions()
        body = {"repository": KEY[0], "issue": 7, "command": "withdraw", "argument": "openai", "actor_id": 999}
        status, _ = actions.handle("Bearer user-token", json.dumps(body).encode())
        self.assertEqual(status, 201)
        text = self.poster.send.call_args.args[2]["body"]
        effective = delegation.resolve(
            {"id": 77, "user": {"id": 999, "login": self.config["bot_login"]}, "body": text},
            b"synthetic-key",
            *KEY,
            self.config["bot_login"],
        )
        self.assertEqual(effective["user"], {"id": 202, "login": "alice"})
        self.assertEqual(effective["body"], "/concurro withdraw openai")
        body["command"] = "sponsor"
        self.assertEqual(actions.handle("Bearer user-token", json.dumps(body).encode())[0], 403)

    def test_projection_reads_do_not_require_a_command_signing_key(self):
        """An absent signing key disables writes while authenticated issue visibility still works."""
        actions = self.actions()
        actions.key = None
        body = json.dumps({"repository": KEY[0], "issue": 7}).encode()
        self.assertEqual(actions.sponsorships("Bearer user-token", body)[0], 200)
        write = json.dumps({"repository": KEY[0], "issue": 7, "command": "sponsor", "argument": "openai"}).encode()
        self.assertEqual(actions.handle("Bearer user-token", write)[0], 503)
        self.poster.send.assert_not_called()

    def test_unknown_issue_read_requests_discovery_without_direct_tick_or_projection_row(self):
        """A first view uses the coalesced observation path without creating a recurring poll target."""
        body = json.dumps({"repository": KEY[0], "issue": 7}).encode()
        status, value = self.actions().sponsorships("Bearer user-token", body)
        self.assertEqual((status, value["status"]), (200, "provisional"))
        self.assertEqual(self.registry.sponsor_issues(KEY[0]), [])
        self.assertEqual(self.registry.due(self.clock()), [])
        self.assertEqual(len(self.registry.observations()), 1)

    def test_empty_terminal_bookkeeping_does_not_expand_issue_discovery(self):
        """Unsponsored historical tasks remain directly readable without extending lists or polling."""
        self.registry.record_shadow(
            KEY, {"sponsorships": {}, "sponsorship_status": "pending"}, {"revision": "fixed", "status": "confirmed"}
        )
        generation = self.gateway.acquire(KEY, "terminal-fixture")
        self.gateway.release(KEY, generation, Outcome("terminal"))
        self.assertEqual(self.registry.sponsor_issues(KEY[0]), [])
        self.assertEqual(self.registry.sponsor_issues(KEY[0], active=True), [])
        body = json.dumps({"repository": KEY[0], "issue": 7}).encode()
        status, value = self.actions().sponsorships("Bearer user-token", body)
        self.assertEqual(status, 200)
        self.assertNotIn("task", value)
        self.assertEqual(value["scope"], "own")

    def test_task_only_initializer_and_takeover_are_redacted_without_privileged_reads(self):
        """Actual Registry task slots survive own discovery; another owner's transitions never leak."""
        self.registry.confirm_sponsorship(KEY, 0, {"slots": {}, "facts": []}, [])
        initial = {"sponsor_id": "202", "login": "alice", "epoch": 1, "origin": "task_start"}
        state = {
            "sponsorships": {"openai": initial, "anthropic": {"sponsor_id": "303", "epoch": 1}},
            "sponsorship_history": [{"provider": "openai", "actor": None, "reason": "initialize", "slot": initial}],
        }
        self.registry.record_shadow(KEY, state)
        shadow = Shadow(self.registry, Mock(), Mock())
        self.assertEqual(shadow.view(KEY)["slots"]["openai"], initial)
        actions = self.actions()
        actions.projection = Mock(side_effect=AssertionError("nonwriter must not read coverage or observations"))
        body = json.dumps({"repository": KEY[0], "issue": 7}).encode()
        status, own = actions.sponsorships("Bearer user-token", body)
        self.assertEqual((status, own["slots"]["openai"]), (200, initial))
        self.assertIsNone(own["slots"]["anthropic"]["sponsor_id"])
        self.assertEqual(own["history"][0]["reason"], "initialize")
        state["sponsorships"]["openai"] = {"sponsor_id": "303", "epoch": 2}
        state["sponsorship_history"] += [
            {"provider": "openai", "actor": "303", "reason": "permission_lost", "cutoff": 9, "slot": initial},
            {"provider": "openai", "actor": "202", "reason": "withdraw", "cutoff": 1, "slot": {"sponsor_id": "303"}},
        ]
        self.registry.record_shadow(KEY, state)
        status, own = actions.sponsorships("Bearer user-token", body)
        self.assertIsNone(own["slots"]["openai"]["sponsor_id"])
        self.assertEqual([r["reason"] for r in own["history"]], ["initialize", "withdraw"])
        self.assertEqual(own["history"][-1]["cutoff"], 1)
        self.assertNotIn("303", json.dumps(own))
        status, listing = actions.sponsorships("Bearer user-token", json.dumps({"repository": KEY[0]}).encode())
        self.assertEqual((status, len(listing["issues"])), (200, 1))
        actions.projection.assert_not_called()
