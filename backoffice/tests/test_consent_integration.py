"""Merged Reader identity, upstream authority and own-consent privacy contracts."""

import json
import unittest
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock, patch

import test_task_view as fixture

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

from backoffice.auth import User
from backoffice.commands import CommandError
from backoffice.tasks import TaskNotFound, Tasks
from github import Transient
from service.targets import Target
from sponsorship import own_consent


class ConsentIntegrationTests(unittest.TestCase):
    """Use actual Reader/user transports and task projection without real GitHub calls."""

    def setUp(self):
        """Reuse the existing complete fake repository and independently authenticated wire."""
        self.case = fixture.TaskViewTests("test_task_view_projects_the_checkpoint")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.target = Target("up/project", "fork/project", "fork")
        self.settings = replace(self.case.settings, targets=(self.target,))
        self.settings = replace(self.settings, reader=replace(self.settings.reader, targets=(self.target,)))
        for repository in self.target.repositories:
            self.case.wire.fixed["GET", f"repos/{repository}"] = {"full_name": repository}
            self.case.wire.fixed["GET", f"repos/{repository}/collaborators/reader/permission"] = {"permission": "read"}
        self.case.wire.fixed["GET", "repos/fork/project/issues/7"] = self.case.issue_listing()
        self.case.wire.fixed["GET", "repos/fork/project/collaborators/reader/permission"] = {"permission": "write"}
        self.projection = {
            "status": "confirmed",
            "funding": "contributed",
            "calls": 99,
            "slots": {"openai": {"sponsor_id": "2", "login": "other", "epoch": 4}},
            "facts": [
                {"actor_id": "1", "provider": "openai", "command": "sponsor"},
                {"actor_id": "2", "command": "sponsor"},
            ],
            "tombstones": {"openai": {"1": "own-withdrawal", "2": "other-withdrawal"}},
            "task": {
                "usage": [{"account": "other-private-account"}],
                "history": [
                    {
                        "actor": "1",
                        "provider": "openai",
                        "reason": "withdraw",
                        "cutoff": 3,
                        "slot": {"sponsor_id": "2", "private": "other-private-account"},
                    },
                    {"actor": "2", "provider": "anthropic", "reason": "sponsor", "slot": {"sponsor_id": "2"}},
                ],
            },
            "observations": [{"binding": {"sponsor": "2"}}],
            "coverage": {"anthropic": [{"version": 12}]},
        }

    def reader(self, projection=None):
        """Bind actual Reader credentials independently of the signed-in user's visibility token."""
        access = fixture.Access(fixture.ReaderApp(), "user-token", self.case.wire.connection)
        supplied = self.projection if projection is None else projection
        callback = lambda repository, issue=None: (
            deepcopy(supplied) if issue else {"issues": [{"issue": 7, **deepcopy(supplied)}]}
        )
        return Tasks(self.settings, access, User(1, "reader", "Reader", ""), sponsorships=callback)

    def test_fork_writer_gets_only_own_consent_after_upstream_loss(self):
        """Historical owners keep their own receipts but cannot read any checkpoint or other payer."""
        reader = self.reader()
        with patch.object(reader, "state") as state, patch.object(reader, "config") as config:
            with self.assertRaises(TaskNotFound):
                reader.task_view("fork/project", 7)
            view = reader.issue_view("fork/project", 7)
            self.assertFalse(view["issueWriter"])
            self.assertNotIn("target", view)
            self.assertNotIn("body", view["issue"])
            own = view["issue"]["sponsorships"]
            self.assertEqual(own["scope"], "own")
            self.assertEqual(own["history"][0]["cutoff"], 3)
            self.assertNotIn("slot", own["history"][0])
            self.assertEqual(len(own["history"]), 1)
            self.assertEqual([f["actor_id"] for f in own["facts"]], ["1"])
            self.assertEqual(own["tombstones"], {"openai": {"1": "own-withdrawal"}})
            self.assertFalse(any(slot.get("sponsor_id") for slot in own["slots"].values()))
            self.assertEqual([i["issue"] for i in reader.my_sponsorships()], [7])
            self.assertNotIn("other-private-account", json.dumps(own))
            for field in ("task", "calls", "coverage", "observations", "charged_calls", "funding"):
                self.assertNotIn(field, own)
            state.assert_not_called()
            config.assert_not_called()
        tokens = self.case.wire.reads
        self.assertIn(("Bearer user-token", "repos/fork/project/issues/7"), tokens)
        self.assertIn(("Bearer reader-token", "repos/up/project/collaborators/reader/permission"), tokens)

    def test_no_prior_consent_has_a_visibility_only_withdrawal_view(self):
        """No grant prerequisite is introduced for pre-enrollment or repeated withdrawal."""
        reader = self.reader({"status": "provisional", "slots": {}})
        with patch.object(reader, "state") as state:
            view = reader.issue_view("fork/project", 7)
            self.assertFalse(view["issueWriter"])
            self.assertEqual(view["issue"]["sponsorships"]["facts"], [])
            state.assert_not_called()

    def test_upstream_writer_keeps_full_projection_without_fork_write(self):
        """The new writer authority and full coverage view are independent of fork permissions."""
        self.case.wire.fixed["GET", "repos/up/project/collaborators/reader/permission"] = {"permission": "write"}
        self.case.wire.fixed["GET", "repos/fork/project/collaborators/reader/permission"] = {"permission": "read"}
        reader = self.reader()
        with patch.object(reader, "target_record", return_value={"writer": True}):
            view = reader.issue_view("fork/project", 7)
        self.assertTrue(view["issueWriter"])
        self.assertEqual(view["issue"]["sponsorships"], self.projection)

    def test_shared_redaction_is_idempotent_and_retains_only_own_transition(self):
        """Service and BFF can apply the same projection without widening or losing own receipts."""
        self.projection["task"]["history"].append(
            {
                "actor": "2",
                "provider": "anthropic",
                "reason": "permission_lost",
                "cutoff": 99,
                "slot": {"sponsor_id": "1"},
            }
        )
        own = own_consent(self.projection, "1")
        self.assertEqual(own_consent(own, "1"), own)
        self.assertEqual(own["history"][0]["actor"], "1")
        self.assertEqual(len(own["history"]), 1)

    def test_own_cutover_receipt_matches_provider_and_initialized_grant(self):
        """Automatic retirement exposes only its own grant link, never foreign cutoffs or slots."""
        self.projection["task"]["history"] = [
            {
                "provider": "openai",
                "actor": None,
                "reason": "initialize",
                "slot": {"sponsor_id": "1", "grant_id": "own-grant"},
            },
            {
                "provider": "openai",
                "actor": None,
                "reason": "initialize",
                "slot": {"sponsor_id": "2", "grant_id": "foreign-grant"},
            },
            {
                "provider": "openai",
                "actor": None,
                "reason": "cutover",
                "transition": "own-cutover",
                "cutoff": 99,
                "slot": {"sponsor_id": None, "cutover_from": "own-grant", "epoch": 2},
            },
            {"provider": "anthropic", "actor": None, "reason": "cutover", "slot": {"cutover_from": "own-grant"}},
            {"provider": "openai", "actor": None, "reason": "cutover", "slot": {"cutover_from": "foreign-grant"}},
            {"provider": "openai", "actor": "2", "reason": "cutover", "slot": {"cutover_from": "own-grant"}},
        ]
        own = own_consent(self.projection, "1")
        self.assertEqual(len(own["history"]), 2)
        self.assertEqual(
            own["history"][1],
            {"provider": "openai", "reason": "cutover", "transition": "own-cutover", "cutover_from": "own-grant"},
        )
        self.assertEqual(own_consent(own, "1"), own)
        self.assertNotIn("foreign-grant", json.dumps(own))
        self.assertNotIn("cutoff", own["history"][1])
        self.assertNotIn("slot", own["history"][1])

    def test_own_discovery_skips_only_gone_issues_and_preserves_outage(self):
        """Moved/deleted source issues do not poison lists; outages never become empty successful reads."""
        reader = self.reader()
        for status in (301, 410):
            with patch.object(reader, "issue_view", side_effect=RuntimeError(f"GET issue (HTTP {status})")):
                self.assertEqual(reader.my_sponsorships(), [])
        for failure in (RuntimeError("GET issue (HTTP 500)"), Transient("GitHub unavailable")):
            with patch.object(reader, "issue_view", side_effect=failure), self.assertRaises(type(failure)):
                reader.my_sponsorships()
        reader.sponsorships = Mock(side_effect=CommandError(503, "sponsorship service unavailable"))
        with self.assertRaises(CommandError) as refused:
            reader.my_sponsorships()
        self.assertEqual(refused.exception.status, 503)
