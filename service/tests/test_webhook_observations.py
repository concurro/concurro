"""Delegated-comment integration seam: durable, uncached, retryable observations."""

import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

from fixtures import KEY, ServiceFixture

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

from controller import Outcome
from github import Transient
from service import registry
from service.ingress import Ingress

MARKER = "<!-- concurro:delegated-command:v1 "


class ObservationTests(ServiceFixture, unittest.TestCase):
    """The other branch supplies delegation.marked; this branch supplies durable scheduling."""

    def setUp(self):
        super().setUp()
        self.service()
        self.bot = self.config["bot_login"]
        self.marked = lambda body: MARKER in body
        self.reconciler.command_comment = self.marked
        self.ingress = Ingress(
            b"test", self.registry, {KEY[0]: 42}, {self.bot}, clock=self.clock, command_comment=self.marked
        )
        self.fake.issue["updated_at"] = "unchanged"
        self.fake.comments = [{"id": 101, "body": MARKER + "signed command -->", "user": {"login": self.bot}}]
        self.data = {
            "repository": {"full_name": KEY[0]},
            "installation": {"id": 42},
            "sender": {"login": self.bot},
            "action": "created",
            "issue": {"number": KEY[1]},
            "comment": dict(self.fake.comments[0]),
        }

    def deliver(self, identifier="delivery"):
        """Send a correctly authenticated App-authored comment notification."""
        body = json.dumps(self.data).encode()
        return self.ingress.handle(
            {
                "X-Hub-Signature-256": "sha256=" + hmac.new(b"test", body, hashlib.sha256).hexdigest(),
                "X-GitHub-Event": "issue_comment",
                "X-GitHub-Delivery": identifier,
            },
            body,
        )

    def test_receipt_is_atomic_with_observation_not_engine_queue(self):
        """No accepted receipt can exist without its job; receiving never calls GitHub."""
        original = registry.queue_observation

        def crash(db, key, now, expectation=None):
            """Interrupt after the job INSERT but before commit."""
            original(db, key, now, expectation)
            raise RuntimeError("crash")

        with patch("service.registry.queue_observation", crash), self.assertRaises(RuntimeError):
            self.deliver()
        self.assertEqual(self.registry.observations(), [])
        self.assertEqual(self.deliver()[0], 202)
        self.assertEqual(self.registry.due(self.clock()), [])
        self.assertEqual(len(self.registry.observations()), 1)
        self.assertEqual(self.deliver()[0], 200)

    def test_uncached_observation_detects_edits_and_removal(self):
        """Stable updated_at and a warm hint cannot hide edits or removal of a command marker."""
        before = self.reconciler.snapshot(KEY)
        self.registry.acknowledge(KEY, {**before, "_stage": "planned"})
        self.fake.comments[0]["body"] = "command withdrawn"
        self.data.update(action="edited", comment=dict(self.fake.comments[0]))
        self.assertEqual(self.deliver()[0], 202, "previously observed ID survives marker removal")
        self.reconciler.observe_pending()
        self.assertEqual([item.key for item in self.registry.due(self.clock())], [KEY])
        self.assertEqual(self.registry.observations(), [])

    def test_deleted_command_retains_targeted_wakeup(self):
        """A deleted App comment must revoke an earlier observed approval through the normal engine path."""
        self.registry.acknowledge(KEY, {**self.reconciler.snapshot(KEY), "_stage": "planned"})
        self.fake.comments.clear()
        self.data.update(action="deleted")
        self.deliver()
        self.reconciler.observe_pending()
        self.assertEqual([item.key for item in self.registry.due(self.clock())], [KEY])

    def test_failed_observation_retries_after_restart(self):
        """A 202 delivery is not completion; a failed read survives restart with its retry deadline."""
        self.deliver()
        with patch.object(self.reconciler, "snapshot", side_effect=Transient("unavailable", 90)):
            self.reconciler.observe_pending()
        self.assertEqual(self.registry.observations(), [])
        self.registry.close()
        self.registry = self.open()
        self.reconciler.registry = self.registry
        self.clock.now += 89
        self.assertEqual(self.registry.observations(), [])
        self.clock.now += 1
        self.reconciler.observe_pending()
        self.assertEqual([item.key for item in self.registry.due(self.clock())], [KEY])
        self.assertEqual(self.registry.observations(), [])

    def test_unchanged_command_does_not_shorten_engine_backoff(self):
        """Observation uses registry.observe's deferred-input comparison, never direct enqueue."""
        snapshot = self.reconciler.snapshot(KEY)
        self.registry.acknowledge(KEY, {**snapshot, "_stage": "plan"})
        generation = self.gateway.acquire(KEY, "test")
        self.registry.defer(KEY, snapshot)
        self.gateway.release(KEY, generation, Outcome("wait-until", retry_after=900))
        self.deliver()
        self.reconciler.observe_pending()
        self.assertEqual(self.registry.due(self.clock()), [])
        self.assertEqual([item.key for item in self.registry.due(self.clock() + 900)], [KEY])

    def test_delivery_during_observation_is_not_discarded(self):
        """A new job revision survives finishing the older snapshot."""
        self.deliver()
        original = self.reconciler.snapshot

        def changed(key, cached=True, comments_read=None):
            """Deliver another edit after the observation has read its inputs."""
            result = original(key, cached=cached, comments_read=comments_read)
            self.deliver("newer")
            return result

        with patch.object(self.reconciler, "snapshot", changed):
            self.reconciler.observe_pending()
        self.assertEqual(len(self.registry.observations()), 1)
        self.reconciler.observe_pending()
        self.assertEqual(self.registry.observations(), [])

    def test_crash_after_observe_before_job_removal_is_idempotent(self):
        """Re-observation merges its cause instead of queueing duplicate engine work."""
        self.deliver()
        with (
            patch.object(self.registry, "finish_observation", side_effect=RuntimeError("lost completion")),
            self.assertRaises(RuntimeError),
        ):
            self.reconciler.observe_pending()
        self.reconciler.observe_pending()
        self.assertEqual(len(self.registry.due(self.clock())), 1)
        self.assertEqual(len(self.registry.due(self.clock())[0].causes), 1)

    def test_edit_prior_body_wakes_before_first_observation(self):
        """An edit can remove the marker before the original command was ever read."""
        self.data.update(action="edited", changes={"body": {"from": self.data["comment"]["body"]}})
        self.data["comment"]["body"] = "withdrawn"
        self.assertEqual(self.deliver()[0], 202)
        self.assertEqual(len(self.registry.observations()), 1)

    def test_successful_but_stale_create_list_keeps_job(self):
        """A complete HTTP 200 missing the new command is not successful observation."""
        comment = self.fake.comments.pop()
        self.registry.acknowledge(KEY, self.reconciler.snapshot(KEY))
        self.deliver()
        self.reconciler.observe_pending()
        self.assertEqual(self.registry.due(self.clock()), [])
        self.assertEqual(self.registry.observations(), [], "the job is deferred, not immediately due")
        self.fake.comments.append(comment)
        self.clock.now += 60
        self.assertEqual(len(self.registry.observations()), 1)
        self.reconciler.observe_pending()
        self.assertEqual([item.key for item in self.registry.due(self.clock())], [KEY])
        self.assertEqual(self.registry.observations(), [])

    def test_successful_but_stale_edit_and_delete_keep_jobs(self):
        """Old body/continued presence cannot consume edited/deleted delivery evidence."""
        before = self.reconciler.snapshot(KEY)
        self.registry.acknowledge(KEY, {**before, "_stage": "planned"})
        self.data.update(action="edited", changes={"body": {"from": self.data["comment"]["body"]}})
        self.data["comment"]["body"] = MARKER + "replacement -->"
        self.deliver()
        self.reconciler.observe_pending()
        self.clock.now += 60
        self.assertEqual(len(self.registry.observations()), 1)
        self.fake.comments[0]["body"] = self.data["comment"]["body"]
        self.reconciler.observe_pending()
        self.assertEqual(self.registry.observations(), [])
        self.data["action"] = "deleted"
        self.deliver("delete")
        self.reconciler.observe_pending()
        self.clock.now += 60
        self.assertEqual(len(self.registry.observations()), 1)
        self.fake.comments.clear()
        self.reconciler.observe_pending()
        self.assertEqual(self.registry.observations(), [])

    def test_uncertain_post_without_id_matches_exact_nonce_bearing_body(self):
        """Actions can retain its exact submitted body even when the comment POST reply is lost."""
        comment = self.fake.comments.pop()
        self.registry.request_observation(KEY, comment={"body": comment["body"]})
        self.reconciler.observe_pending()
        self.clock.now += 60
        self.assertEqual(len(self.registry.observations()), 1)
        self.fake.comments.append(comment)
        self.reconciler.observe_pending()
        self.assertEqual(self.registry.observations(), [])

    def test_visible_later_edit_supersedes_creation(self):
        """A create need not reappear after its known edit is already visible."""
        self.deliver("create")
        self.data.update(action="edited", changes={"body": {"from": self.data["comment"]["body"]}})
        self.data["comment"]["body"] = MARKER + "edited -->"
        self.deliver("edit")
        self.fake.comments[0]["body"] = self.data["comment"]["body"]
        self.reconciler.observe_pending()
        self.assertEqual(self.registry.observations(), [])
