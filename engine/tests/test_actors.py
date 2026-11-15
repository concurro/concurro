"""Account identity by numeric and node ID, with login-bound legacy records."""

import unittest
from copy import deepcopy
from unittest.mock import patch

from support import CONFIG, setUpModule as setUpModule, tearDownModule as tearDownModule

import reviews
from controller import config_digest
from fakes import FakeGitHub
from format_samples import review_state
from identities import Actor
from policy import Blocked, digest
from standalone_fixtures import APP, REVIEWER
from vocab import STANDALONE_REVIEW


class ActorTests(unittest.TestCase):
    def test_a_renamed_account_still_matches_by_id(self):
        actor = Actor.parse(REVIEWER)
        self.assertTrue(actor.rest({"login": "Renamed", "id": REVIEWER["id"]}))
        self.assertTrue(actor.graphql({"login": "Renamed", "id": REVIEWER["node"]}))

    def test_an_old_login_reused_by_another_account_does_not_match(self):
        actor = Actor.parse(REVIEWER)
        self.assertFalse(actor.rest({"login": REVIEWER["login"], "id": 999}))
        self.assertFalse(actor.graphql({"login": REVIEWER["login"], "id": "U_other"}))

    def test_the_two_ids_are_not_interchangeable(self):
        actor = Actor.parse(REVIEWER)
        self.assertFalse(actor.rest({"login": "x", "id": REVIEWER["node"]}))
        self.assertFalse(actor.graphql({"login": "x", "id": str(REVIEWER["id"])}))

    def test_a_legacy_record_is_bound_by_login_alone(self):
        actor = Actor.parse("concurro[bot]")
        self.assertEqual(actor.record(), {"login": "concurro[bot]", "id": None, "node": None})
        self.assertTrue(actor.rest({"login": "concurro[bot]", "id": 1}))
        self.assertTrue(actor.graphql({"login": "concurro", "id": "BOT_1"}))
        self.assertFalse(actor.rest({"login": "other[bot]", "id": 1}))

    def test_payloads_without_ids_fall_back_to_the_login(self):
        actor = Actor.parse(APP)
        self.assertTrue(actor.rest({"login": APP["login"]}))
        self.assertTrue(actor.graphql({"login": APP["login"].removesuffix("[bot]")}))
        self.assertFalse(actor.rest(None))

    def test_malformed_descriptors_are_refused(self):
        for value in ({"login": ""}, {"login": "a", "id": "1"}, {"login": "a", "id": True}, {"id": 1}, 7):
            with self.subTest(value=value), self.assertRaises(Blocked):
                Actor.parse(value)

    def test_resolved_accounts_do_not_rebind_existing_tasks(self):
        self.assertEqual(config_digest({**CONFIG, "actors": {"app": APP}}), config_digest(CONFIG))


class PublisherIdentityTests(unittest.TestCase):
    def test_each_task_kind_publishes_as_its_own_account(self):
        config = {**CONFIG, "actors": {"app": APP, "reviewer": REVIEWER}}
        self.assertEqual(reviews.publisher({}, config).login, APP["login"])
        self.assertEqual(reviews.publisher({"kind": STANDALONE_REVIEW}, config).id, REVIEWER["id"])
        self.assertEqual(reviews.publisher({}, CONFIG), Actor(CONFIG["bot_login"]))
        with self.assertRaisesRegex(Blocked, "no configured reviewer"):
            reviews.publisher({"kind": STANDALONE_REVIEW}, CONFIG)


class MigrationTests(unittest.TestCase):
    """A pending publication recorded before account IDs existed is still recognized."""

    def setUp(self):
        self.state, _, _ = review_state()
        self.gh = FakeGitHub(CONFIG)
        self.gh.pr = {"node_id": "PR_9", "state": "open", "head": {"sha": "d" * 40}}
        for finding in self.state["findings"]:
            finding["format"] = 4
            finding["opening_digest"] = digest(reviews.finding_body(finding))
        old = self.state["findings"][0]
        self.gh.review_comments.append(
            {"id": 101, "path": old["path"], "body": reviews.finding_body(old), "user": {"login": CONFIG["bot_login"]}}
        )
        self.gh.thread(self.gh.review_comments[0])
        reviews.queue_review(self.state, "claude", self.state["reviews"]["claude"])
        self.config = {**CONFIG, "actors": {"app": APP}}

    def publish(self, config):
        with patch.object(
            self.gh, "pull_files", return_value=[{"filename": "docs/guide.md", "patch": "@@ -1 +1 @@\n-old\n+new"}]
        ):
            return reviews.publish(self.state, self.gh, config, lambda: None)

    def test_replay_of_a_pre_migration_pending_write(self):
        original, interrupted = self.gh.api, []

        def api(path, method="GET", data=None):
            if "addPullRequestReviewThread(" in data["query"] and not interrupted:
                interrupted.append(True)
                raise RuntimeError("stopped while building")
            return original(path, method, data)

        with patch.object(self.gh, "api", side_effect=api):
            self.assertFalse(self.publish(CONFIG))
        progress = self.state["pr_outbox"][0]["publication_state"]
        self.assertEqual((progress["phase"], "actor" in progress), ("building", False))
        # The stored record has no account ID: its abandoned build is recognized by login and rebuilt.
        self.assertTrue(self.publish(self.config))
        self.assertEqual(len(self.gh.app_reviews), 1)

    def test_new_publications_record_their_account(self):
        captured = []
        with patch.object(
            self.gh, "pull_files", return_value=[{"filename": "docs/guide.md", "patch": "@@ -1 +1 @@\n-old\n+new"}]
        ):
            reviews.publish(self.state, self.gh, self.config, lambda: captured.append(deepcopy(self.state)))
        recorded = [s["pr_outbox"][0]["publication_state"].get("actor") for s in captured if s.get("pr_outbox")]
        self.assertEqual(recorded[0], Actor.parse(APP).record())

    def test_another_accounts_review_with_our_marker_is_a_conflict(self):
        marker = reviews.review_marker(self.state, self.config, self.state["pr_outbox"][0]["sequence"])
        self.gh.pending_nodes = {
            "R1": {
                "id": "R1",
                "databaseId": 1,
                "body": marker,
                "state": "PENDING",
                "author": {"login": APP["login"].removesuffix("[bot]"), "id": "BOT_impostor"},
                "commit": {"oid": self.state["pr_outbox"][0]["head"]},
                "pullRequest": {"number": 9, "state": "OPEN", "repository": {"nameWithOwner": self.gh.repository}},
                "drafts": [],
            }
        }
        with self.assertRaisesRegex(Blocked, "differs from its recorded content"):
            self.publish(self.config)


if __name__ == "__main__":
    unittest.main()
