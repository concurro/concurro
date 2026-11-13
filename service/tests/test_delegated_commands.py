"""Backoffice commands end to end: the service posts a signed App comment, webhooks and durable
observations wake the task, and the engine applies it as the writer's own command."""

import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

from fixtures import KEY, ServiceFixture

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import delegation
from github import NotFound, Transient
from service import transport
from service.actions import RECOVERY_SECONDS, Actions
from service.gateway import MutationGateway
from service.ingress import Ingress

DELEGATION_KEY = b"d" * 32
SECRET = b"webhook-secret"
INSTALLATION = 42
TOKEN = "Bearer writer-token"


class Writer:
    """The App's comment POST into the fake: normal, response lost after posting, or refused."""

    def __init__(self, test):
        self.test, self.mode = test, "ok"

    def send(self, method, path, body=None):
        if self.mode == "refuse":
            return transport.Response(True, 422, {}, b'{"message": "Validation Failed"}')
        self.test.fake.comment(body["body"], login=self.test.bot)
        if self.mode == "lose":
            raise TimeoutError()
        created = {"id": self.test.fake.counter, "html_url": "https://github.invalid/c", "updated_at": None}
        return transport.Response(True, 201, {}, json.dumps(created).encode())


class Permissions:
    def request(self, method, path, data=None):
        if "/collaborators/writer/" not in path:
            raise NotFound("GitHub request failed: GET " + path)
        return {"permission": "write"}


class DelegatedCommandTests(ServiceFixture, unittest.TestCase):
    """The signed, durable and backoff-preserving path, with the service's real components."""

    def setUp(self):
        super().setUp()
        self.bot = self.config["bot_login"]
        self.writer = Writer(self)
        self.deliveries = 0
        self.wire()

    def wire(self):
        """Build the daemon's components over the current registry, as `service/__main__.py` does."""
        self.service()
        self.worker.delegation_key = DELEGATION_KEY
        self.reconciler.command_comment = delegation.marked
        self.ingress = Ingress(
            SECRET,
            self.registry,
            {KEY[0]: INSTALLATION},
            {self.bot},
            clock=self.clock,
            command_comment=delegation.marked,
        )
        self.actions = Actions(
            DELEGATION_KEY,
            self.registry,
            lambda repository: Permissions(),
            lambda repository: self.writer,
            self.settings.targets,
            user=lambda token: ("writer", 42),
            clock=self.clock,
        )

    def restart(self):
        """Close the registry and rebuild every component from what it persisted."""
        self.registry.close()
        self.registry = self.open()
        self.gateway = MutationGateway(
            self.registry, lambda repository: self.backend, lambda repository: self.backend, self.bot, **self.pacing
        )
        self.wire()

    def to_planned(self):
        self.registry.enqueue(KEY, "test", self.clock())
        self.drain()
        self.assertEqual(self.stage(), "planned")

    def command(self, kind, argument=None):
        body = {"repository": KEY[0], "issue": KEY[1], "command": kind, "argument": argument}
        return self.actions.handle(TOKEN, json.dumps(body).encode())

    def webhook(self, comment, action="created", previous=None):
        """Deliver the App's own comment event, signed as GitHub would."""
        self.deliveries += 1
        data = {
            "action": action,
            "repository": {"full_name": KEY[0]},
            "installation": {"id": INSTALLATION},
            "sender": {"login": self.bot},
            "issue": {"number": KEY[1], "labels": [], "user": {"login": "author"}},
            "comment": {**comment, "user": {"login": self.bot, "type": "Bot"}},
        }
        if previous is not None:
            data["changes"] = {"body": {"from": previous}}
        body = json.dumps(data).encode()
        headers = {
            "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest(),
            "X-GitHub-Event": "issue_comment",
            "X-GitHub-Delivery": f"delivery-{self.deliveries}",
        }
        return self.ingress.handle(headers, body)[0], headers, body

    def settle(self):
        """Run due observations, then every due tick."""
        self.reconciler.observe_pending()
        return self.drain()

    def quiesce(self):
        """Let the task's own follow-up work (e.g. its PR) settle, as polling would."""
        for _ in range(10):
            polled, ticks = self.reconciler.poll(), self.drain()
            if not polled and not ticks:
                return
        self.fail("the task never settled")

    def posted(self):
        return next(
            c for c in reversed(self.fake.comments) if c["user"]["login"] == self.bot and delegation.marked(c["body"])
        )

    def test_a_signed_approval_runs_as_the_writer_and_late_or_duplicate_webhooks_change_nothing(self):
        self.to_planned()
        plan = self.state()["plan_id"]
        self.assertEqual(self.command("approve", plan)[0], 201)
        self.settle()
        state = self.state()
        self.assertEqual((state["approval"]["kind"], state["approval"]["actor"]), ("human", "writer"))
        self.assertNotEqual(state["stage"], "planned")
        self.quiesce()
        status, headers, body = self.webhook(self.posted())
        self.assertEqual(status, 202)
        self.assertEqual(self.ingress.handle(headers, body)[0], 200, "a duplicate delivery is acknowledged once")
        self.assertEqual(self.settle(), 0, "a late webhook for an applied command runs no tick")

    def test_a_lost_response_still_applies_the_command(self):
        self.to_planned()
        self.writer.mode = "lose"
        status, payload = self.command("stop")
        self.assertEqual(status, 502)
        self.assertIn("may or may not", payload["error"])
        self.settle()
        self.assertEqual(self.stage(), "stopped")

    def test_a_stale_read_keeps_observing_until_the_command_is_visible(self):
        self.to_planned()
        self.assertEqual(self.command("stop")[0], 201)
        hidden = self.fake.comments.pop()
        self.assertEqual(self.settle(), 0, "a stale list wakes nothing")
        self.fake.comments.append(hidden)
        self.clock.now += 60
        self.settle()
        self.assertEqual(self.stage(), "stopped")

    def test_a_pending_observation_survives_a_restart(self):
        self.to_planned()
        self.writer.mode = "lose"
        self.command("stop")
        hidden = self.fake.comments.pop()
        self.settle()
        self.restart()
        self.fake.comments.append(hidden)
        self.clock.now += 60
        self.settle()
        self.assertEqual(self.stage(), "stopped")

    def test_retry_backoff_holds_for_a_command_a_failed_tick_already_saw(self):
        self.to_planned()
        self.backend.fail_read("git/trees/", Transient("throttled", 7200))
        self.assertEqual(self.command("approve", self.state()["plan_id"])[0], 201)
        self.settle()
        self.assertEqual(self.registry.outcome(KEY).retry_after, 7200)
        self.webhook(self.posted())
        self.assertEqual(self.settle(), 0, "neither the late webhook nor its observation hastens the retry")
        self.assertEqual(self.registry.due(self.clock()), [])
        self.clock.now += 7201
        self.drain()
        self.assertNotEqual(self.stage(), "planned")

    def test_a_warm_cache_cannot_make_a_late_webhook_bypass_backoff(self):
        """The tick that saw the command defers fresh input, so its late webhook is not new."""
        self.to_planned()
        self.fake.issue["updated_at"] = "unchanged"
        self.reconciler.snapshot(KEY)  # a warm cache entry from before the command
        self.backend.fail_read("git/trees/", Transient("throttled", 7200))
        self.assertEqual(self.command("approve", self.state()["plan_id"])[0], 201)
        self.settle()
        self.assertEqual(self.registry.outcome(KEY).retry_after, 7200)
        self.webhook(self.posted())
        self.assertEqual(self.settle(), 0)
        self.assertEqual(self.registry.due(self.clock()), [])

    def test_a_command_posted_between_the_two_reads_keeps_backoff(self):
        """The tick acknowledges the comments its controller consumed, not the worker's earlier read."""
        self.to_planned()
        plan = self.state()["plan_id"]
        self.backend.fail_read("git/trees/", Transient("throttled", 7200))
        snapshot = self.reconciler.snapshot

        def then_post(key, *args, **kwargs):
            """The worker's pre-tick read, then the approval lands before the controller reads."""
            taken = snapshot(key, *args, **kwargs)
            self.reconciler.snapshot = snapshot
            self.assertEqual(self.command("approve", plan)[0], 201)
            return taken

        self.reconciler.snapshot = then_post
        self.registry.enqueue(KEY, "test", self.clock())
        self.drain()
        self.assertEqual(self.state()["approval"]["actor"], "writer", "the controller saw the approval")
        self.assertEqual(self.registry.outcome(KEY).retry_after, 7200)
        self.webhook(self.posted())
        self.assertEqual(self.settle(), 0, "the processed approval is not new input")
        self.assertEqual(self.registry.due(self.clock()), [])

    def test_a_known_deletion_settles_a_lost_post_observation(self):
        """An ID-less creation is the identified comment with its exact body; its deletion ends the job."""
        self.to_planned()
        self.writer.mode = "lose"
        self.assertEqual(self.command("stop")[0], 502)
        posted = self.posted()
        self.assertEqual(self.webhook(posted)[0], 202)
        self.fake.comments.remove(posted)
        self.assertEqual(self.webhook(posted, action="deleted")[0], 202)
        self.settle()
        self.clock.now += 7 * 24 * 3600
        self.assertEqual(self.registry.observations(), [], "no observation keeps reading GitHub")
        self.assertEqual(self.stage(), "planned", "the deleted command never applied")

    def approved(self):
        """A delegated approval applied, with the task past planning."""
        self.to_planned()
        self.assertEqual(self.command("approve", self.state()["plan_id"])[0], 201)
        self.settle()
        self.assertEqual(self.state()["approval"]["actor"], "writer")
        return self.posted()

    def test_deleting_a_delegated_approval_revokes_it(self):
        approval = self.approved()
        self.fake.comments.remove(approval)
        self.assertEqual(self.webhook(approval, action="deleted")[0], 202)
        self.settle()
        self.assertEqual(self.stage(), "blocked")
        self.assertIn("approval comment changed or was removed", self.state()["reason"])

    def test_editing_a_delegated_approval_revokes_it(self):
        approval = self.approved()
        previous = approval["body"]
        edited = next(c for c in self.fake.comments if c["id"] == approval["id"])
        edited["body"] = previous.replace("approved plan", "approved the plan")
        self.assertIsNone(delegation.resolve(edited, DELEGATION_KEY, KEY[0], KEY[1], self.bot))
        self.assertEqual(self.webhook(edited, action="edited", previous=previous)[0], 202)
        self.settle()
        self.assertEqual(self.stage(), "blocked")

    def untracked(self):
        """An unlabelled issue with no comments and no task."""
        self.fake.issue["labels"] = []
        self.fake.comments = []
        self.assertIsNone(self.registry.outcome(KEY))

    def test_an_untracked_start_through_its_observation(self):
        self.untracked()
        self.writer.mode = "lose"
        self.assertEqual(self.command("start")[0], 502)
        self.settle()
        self.assertEqual(self.stage(), "planned")

    def test_an_untracked_start_survives_a_crash_after_posting(self):
        self.untracked()
        with (
            patch.object(self.registry, "request_observation", side_effect=RuntimeError("crashed")),
            self.assertRaises(RuntimeError),
        ):
            self.command("start")
        self.restart()
        self.assertEqual((self.registry.observations(), self.registry.due(self.clock())), ([], []))
        self.clock.now += RECOVERY_SECONDS
        self.drain()
        self.assertEqual(self.stage(), "planned")

    def test_a_refused_post_wakes_nothing_and_keeps_the_retry_deadline(self):
        self.to_planned()
        self.backend.fail_read("git/trees/", Transient("throttled", 7200))
        self.fake.comment("/concurro approve " + self.state()["plan_id"])
        self.reconciler.poll()
        self.drain()
        self.writer.mode = "refuse"
        self.assertEqual(self.command("stop")[0], 502)
        self.assertEqual((self.settle(), self.registry.due(self.clock() + 7199)), (0, []))


if __name__ == "__main__":
    unittest.main()
