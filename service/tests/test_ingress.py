"""Ingress verifies, filters and de-duplicates deliveries, and only ever enqueues."""

from copy import deepcopy
import hashlib
import hmac
import http.client
import json
import threading
import unittest

from fixtures import KEY, ServiceFixture
from support import setUpModule, tearDownModule  # unittest module fixtures
from controller import Outcome
from service import ingress
from service.ingress import Ingress

SECRET = b"webhook-secret"
INSTALLATION = 42
BOT = "concurro[bot]"


def issue(labels=("concurro",), **fields):
    return {"number": 7, "labels": [{"name": name} for name in labels], "user": {"login": "author"}, **fields}


def payload(**fields):
    return {"repository": {"full_name": KEY[0]}, "installation": {"id": INSTALLATION},
            "sender": {"login": "writer"}, **fields}


def comment(body="A reply.", login="author", association="NONE", kind="User", **issue_fields):
    return payload(action="created", issue=issue(**issue_fields),
                   comment={"body": body, "user": {"login": login, "type": kind}, "author_association": association})


class IngressTests(ServiceFixture, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.ingress = Ingress(SECRET, self.registry, {KEY[0]: INSTALLATION}, {BOT}, clock=self.clock)
        self.deliveries = 0

    def deliver(self, event, data, delivery=None, secret=SECRET):
        """Sign and deliver one event; return the status."""
        self.deliveries += 1
        body = json.dumps(data).encode()
        headers = {"X-Hub-Signature-256": "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest(),
                   "X-GitHub-Event": event, "X-GitHub-Delivery": delivery or f"delivery-{self.deliveries}"}
        return self.ingress.handle(headers, body)[0]

    def queued(self):
        return [item.key for item in self.registry.due(self.clock())]

    def test_rejected_deliveries_change_nothing(self):
        """Bad signatures, other repositories and other installations leave no trace, not even the delivery ID."""
        event = payload(action="edited", issue=issue())
        cases = [(event, "wrong-secret".encode(), 401),
                 ({**event, "repository": {"full_name": "owner/elsewhere"}}, SECRET, 403),
                 ({**event, "installation": {"id": 7}}, SECRET, 403)]
        for data, secret, status in cases:
            self.assertEqual(self.deliver("issues", data, "same", secret), status)
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.deliver("issues", event, "same"), 202, "the rejected delivery ID was never recorded")
        self.assertEqual(self.queued(), [KEY])

    def test_redelivery_is_acknowledged_once(self):
        """A redelivered event is acknowledged without a second cause."""
        event = comment()
        self.assertEqual(self.deliver("issue_comment", event, "once"), 202)
        self.assertEqual(self.deliver("issue_comment", event, "once"), 200)
        [item] = self.registry.due(self.clock())
        self.assertEqual(item.causes, ("webhook:issue_comment",))

    def test_delivery_during_a_tick_schedules_one_follow_up(self):
        """A delivery during a tick waits in the queue; the tick's end makes it due exactly once."""
        self.service()
        self.registry.enqueue(KEY, "test", self.clock())
        self.drain()
        ask = self.agents.ask

        def deliver(provider, stage, context):
            self.agents.ask = ask
            self.assertEqual(self.deliver("issue_comment", comment(), "during"), 202)
            self.assertEqual(self.deliver("issue_comment", comment(), "during"), 200)
            self.assertIsNone(self.gateway.acquire(KEY, "other"), "one tick per task")
            return ask(provider, stage, context)

        self.agents.ask = deliver
        self.fake.comment("/concurro approve " + self.state()["plan_id"])
        self.assertEqual(self.reconciler.poll(), [KEY])
        self.assertEqual(self.scheduler.dispatch(), [KEY])
        self.assertIs(self.agents.ask, ask, "the delivery arrived during the tick")
        [item] = self.registry.due(self.clock())
        self.assertIn("webhook:issue_comment", item.causes)

    def test_issue_event_filters(self):
        """Display labels, unenrolled issues, pull requests and Concurro senders are dropped."""
        dropped = [payload(action="labeled", label={"name": "concurro:stage:plan"}, issue=issue()),
                   payload(action="labeled", label={"name": "concurro:attention"}, issue=issue()),
                   payload(action="edited", issue=issue(labels=())),
                   payload(action="edited", issue=issue(pull_request={})),
                   payload(action="assigned", issue=issue()),
                   {**payload(action="edited", issue=issue()), "sender": {"login": BOT}}]
        for data in dropped:
            self.assertEqual(self.deliver("issues", data), 202)
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.deliver("issues", payload(action="labeled", label={"name": "concurro"},
                                                        issue=issue(labels=()))), 202)
        self.assertEqual(self.queued(), [KEY])

    def test_unlabeled_active_task_is_still_observed(self):
        """Removing the enrollment label from an active task still reaches it."""
        generation = self.gateway.acquire(KEY, "test")
        self.gateway.release(KEY, generation, Outcome("await-human"))
        self.deliver("issue_comment", comment(labels=()))
        self.assertEqual(self.queued(), [KEY])

    def test_comment_filters(self):
        """Only the issue author or a writer counts; bots and Concurro never do; commands need no label."""
        for data in (comment(login="stranger"), comment(login="helper", kind="Bot", association="MEMBER"),
                     comment(login=BOT, association="MEMBER"), comment(pull_request={}),
                     comment(body="Unrelated.", labels=())):
            self.deliver("issue_comment", data)
        self.assertEqual(self.queued(), [])
        self.deliver("issue_comment", comment(body="/concurro start", login="writer", association="COLLABORATOR",
                                              labels=()))
        self.assertEqual(self.queued(), [KEY])

    def test_pull_request_and_check_events_map_through_recorded_pulls(self):
        """PR, review and check events reach the task that owns the PR; unknown PRs are dropped."""
        events = [("pull_request", payload(action="synchronize", pull_request={"number": 9})),
                  ("pull_request_review", payload(action="submitted", pull_request={"number": 9})),
                  ("check_run", payload(action="completed", check_run={"pull_requests": [{"number": 9}]})),
                  ("check_suite", payload(action="completed", check_suite={"pull_requests": [{"number": 9}]}))]
        for event, data in events:
            self.assertEqual(self.deliver(event, data), 202)
        self.assertEqual(self.queued(), [])
        self.registry.link(KEY, 9)
        for event, data in events:
            self.registry.enqueue(KEY, "reset", self.clock() + 1000)
            self.deliver(event, deepcopy(data))
            self.assertEqual(self.queued(), [KEY], event)
            generation = self.gateway.acquire(KEY, "test")
            self.gateway.release(KEY, generation, None)

    def test_other_events_are_ignored(self):
        self.assertEqual(self.deliver("push", payload()), 202)
        self.assertEqual(self.deliver("ping", {}), 200)
        self.assertEqual(self.queued(), [])

    def test_http_endpoint(self):
        """One POST endpoint and a health check."""
        server = ingress.server(self.ingress, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        body = json.dumps(comment()).encode()
        connection = http.client.HTTPConnection(*server.server_address, timeout=10)
        connection.request("POST", "/", body, {
            "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest(),
            "X-GitHub-Event": "issue_comment", "X-GitHub-Delivery": "http", "Content-Type": "application/json"})
        self.assertEqual(connection.getresponse().status, 202)
        connection.request("GET", "/health")
        self.assertEqual(connection.getresponse().read(), b"ok\n")
        connection.close()
        self.assertEqual(self.queued(), [KEY])


if __name__ == "__main__":
    unittest.main()
