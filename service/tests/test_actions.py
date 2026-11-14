"""Backoffice commands: the service verifies the writer, posts the signed App comment and wakes the task."""

import http.client
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import fixtures  # initializes the service and engine test paths

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import delegation
from controller import Outcome
from github import NotFound
from service import actions, ingress, transport
from service.targets import Target

KEY = b"k" * 32
REPOSITORY = "up/project"

TOKEN = "Bearer user-token"


TASK = (REPOSITORY, 7)


class Registry:
    def __init__(self):
        self.queued, self.scheduled, self.observations = [], set(), []

    def request_observation(self, key, comment=None, action="created"):
        self.observations.append((key, comment, action))

    def enqueue_unscheduled(self, key, cause, due_at):
        if key not in self.scheduled:
            self.queued.append((key, cause, due_at))


class Reader:
    def __init__(self, permissions):
        self.permissions = permissions

    def request(self, method, path, data=None):
        login = path.split("/collaborators/")[1].split("/")[0]
        if login not in self.permissions:
            raise NotFound("GitHub request failed: GET " + path)
        return {"permission": self.permissions[login]}


class Writer:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.posts = response, error, []

    def send(self, method, path, body=None):
        self.posts.append((method, path, body))
        if self.error:
            raise self.error
        return self.response


def created(url="https://github.invalid/up/project/issues/7#issuecomment-1"):
    return transport.Response(True, 201, {}, json.dumps({"id": 1, "html_url": url}).encode())


class ActionsTests(unittest.TestCase):
    def setUp(self):
        self.registry = Registry()
        self.reader = Reader({"writer": "write", "reader": "read"})
        self.writer = Writer(created())
        self.users = {"user-token": ("writer", 42), "reader-token": ("reader", 43)}

    def actions(self):
        return actions.Actions(
            KEY,
            self.registry,
            lambda repository: self.reader,
            lambda repository: self.writer,
            [Target(REPOSITORY, REPOSITORY, "both")],
            user=self.users.get,
            clock=lambda: 100.0,
        )

    def observed(self):
        return [key for key, _, _ in self.registry.observations]

    def request(self, body=None, authorization=TOKEN, **fields):
        data = {"repository": REPOSITORY, "issue": 7, "command": "approve", "argument": "a" * 64, **fields}
        return self.actions().handle(authorization, json.dumps(body if body is not None else data).encode())

    def test_historical_delegated_consent_notice_still_verifies(self):
        """Legacy signed comments remain valid while new receipts avoid claiming an org payer."""
        body = delegation.render(KEY, REPOSITORY, 7, "writer", 42, "/concurro sponsor openai", "a" * 16)
        self.assertIn("Provider funding is unchanged", body)
        historical = body.replace(" Provider funding is unchanged.", " Current calls remain org-funded.")
        effective = delegation.resolve(
            {"id": 1, "body": historical, "user": {"login": "app[bot]"}}, KEY, REPOSITORY, 7, "app[bot]"
        )
        self.assertEqual(effective["body"], "/concurro sponsor openai")

    def test_provider_funding_command_is_signed_and_requires_writer(self):
        """The authenticated command API binds personal consent to the caller and checks both choices."""
        for argument in ("openai personal", "anthropic org"):
            with self.subTest(argument=argument):
                status, payload = self.request(command="funding", argument=argument)
                self.assertEqual(status, 201)
                self.assertEqual(payload["posted"], "/concurro funding " + argument)
                body = self.writer.posts[-1][2]["body"]
                effective = delegation.resolve(
                    {"id": 1, "body": body, "user": {"login": "app[bot]"}}, KEY, REPOSITORY, 7, "app[bot]"
                )
                self.assertEqual(effective["body"], "/concurro funding " + argument)
                self.assertEqual(effective["user"]["id"], 42)
                self.assertEqual(
                    self.request(command="funding", argument=argument, authorization="Bearer reader-token")[0], 403
                )
        self.assertEqual(self.request(command="funding", argument="openai unknown")[0], 422)

    def test_a_writer_command_is_posted_as_a_signed_app_comment_and_wakes_the_task(self):
        status, payload = self.request(repository="UP/Project")
        self.assertEqual(status, 201)
        self.assertEqual(payload["posted"], "/concurro approve " + "a" * 64)
        self.assertEqual((payload["actor"], payload["url"][:19]), ("writer", "https://github.inva"))
        [(method, path, body)] = self.writer.posts
        self.assertEqual((method, path), ("POST", "repos/up/project/issues/7/comments"))
        comment = {"id": 9, "user": {"login": "concurro[bot]"}, "body": body["body"]}
        resolved = delegation.resolve(comment, KEY, REPOSITORY, 7, "concurro[bot]")
        self.assertEqual((resolved["user"]["login"], resolved["body"]), ("writer", payload["posted"]))
        # Never ticked: a durable recovery entry, then a durable observation of the exact comment.
        self.assertEqual(self.registry.queued, [(TASK, "backoffice:approve", 100.0 + actions.RECOVERY_SECONDS)])
        [(key, observed, action)] = self.registry.observations
        self.assertEqual((key, observed["id"], observed["body"], action), (TASK, 1, body["body"], "created"))

    def test_a_ticked_task_is_only_observed_never_queued_directly(self):
        self.registry.scheduled.add(TASK)
        self.assertEqual(self.request(command="stop", argument=None)[0], 201)
        self.assertEqual((self.registry.queued, self.observed()), ([], [TASK]))

    def test_upstream_authority_applies_to_both_issue_destinations(self):
        """Upstream-only writers may command fork tasks; fork-only writers cannot command either destination."""
        target = Target(REPOSITORY, "fork/project", "both")
        upstream = Mock(wraps=Reader({"writer": "write", "reader": "read"}))
        fork = Mock(wraps=Reader({"writer": "read", "reader": "write"}))
        handler = actions.Actions(
            KEY,
            self.registry,
            {target.upstream: upstream, target.fork: fork}.__getitem__,
            lambda repository: self.writer,
            [target],
            user=self.users.get,
        )
        for repository in target.issue_repositories:
            body = json.dumps({"repository": repository.upper(), "issue": 7, "command": "stop"}).encode()
            self.assertEqual(handler.handle(TOKEN, body)[0], 201)
            self.assertEqual(self.writer.posts[-1][1], f"repos/{repository}/issues/7/comments")
            count = len(self.writer.posts)
            status, payload = handler.handle("Bearer reader-token", body)
            self.assertEqual(status, 403)
            self.assertIn(target.upstream, payload["error"])
            self.assertEqual(len(self.writer.posts), count)
        fork.request.assert_not_called()
        self.assertEqual(upstream.request.call_count, 4)
        upstream.request.return_value = {"permission": "read"}
        self.assertEqual(handler.handle(TOKEN, body)[0], 403, "commands recheck revoked upstream permission")
        self.assertEqual(len(self.writer.posts), 2)

    def test_refusals_post_nothing(self):
        cases = {
            "no token": (self.request(authorization=""), 401),
            "malformed body": (self.request(body=["not", "an", "object"]), 400),
            "unknown repository": (self.request(repository="other/project"), 400),
            "bad issue": (self.request(issue="7"), 400),
            "invalid command": (self.request(command="merge"), 422),
            "invalid approval": (self.request(argument="nope"), 422),
            "rejected token": (self.request(authorization="Bearer stolen"), 401),
            "reader": (self.request(authorization="Bearer reader-token"), 403),
        }
        for name, ((status, payload), expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(status, expected)
                self.assertIsInstance(payload["error"], str)
        self.users["outsider-token"] = ("outsider", 44)
        self.assertEqual(self.request(authorization="Bearer outsider-token")[0], 403)
        self.assertEqual((self.writer.posts, self.registry.queued, self.observed()), ([], [], []))

    def test_github_outcomes_are_settled_as_for_any_write(self):
        def response(status, body=b"{}"):
            return Writer(transport.Response(True, status, {}, body))

        self.registry.scheduled.add(TASK)
        unknown, refused, unavailable = "may or may not", "refused", "nothing was posted"
        for writer, expected in (
            (Writer(error=TimeoutError()), (502, unknown)),
            (response(502), (502, unknown)),
            (response(408), (502, unknown)),
            (Writer(transport.Response(sent=False)), (503, unavailable)),
            (response(401), (503, unavailable)),
            (response(403), (502, refused)),
            (response(422), (502, refused)),
        ):
            with self.subTest(expected=expected, status=writer.response and writer.response.status):
                self.writer = writer
                status, payload = self.request(command="stop", argument=None)
                self.assertEqual(status, expected[0])
                self.assertIn(expected[1], payload["error"])
        # Only posts that may have applied are observed, each with its exact posted body; nothing is queued.
        self.assertEqual((self.registry.queued, self.observed()), ([], [TASK] * 3))
        for _, observed, _ in self.registry.observations:
            self.assertIsNone(observed["id"])
            self.assertIn(delegation.MARKER, observed["body"])
        self.writer = response(201, b"")
        self.assertEqual(
            self.request(command="stop", argument=None),
            (201, {"posted": "/concurro stop", "actor": "writer", "url": None}),
        )
        self.assertEqual(self.observed(), [TASK] * 4)
        self.assertIn(delegation.MARKER, self.registry.observations[-1][1]["body"], "a lost body keeps the posted one")

    def test_github_user_lookup(self):
        def connection(reply):
            class Connection:
                def connect(self):
                    pass

                def request(self, method, target, body=None, headers=None):
                    assert (method, target, headers["Authorization"]) == ("GET", "/user", "Bearer t")

                def getresponse(self):
                    return reply

                def close(self):
                    pass

            return lambda host, timeout: Connection()

        self.assertEqual(actions.github_user("t", connection(fixtures.Reply(200, {"login": "w", "id": 1}))), ("w", 1))
        self.assertIsNone(actions.github_user("t", connection(fixtures.Reply(401, {"message": "Bad credentials"}))))
        for status in (403, 429, 502):
            with self.subTest(status=status), self.assertRaises(actions.Refused) as refused:
                actions.github_user("t", connection(fixtures.Reply(status, {})))
            self.assertEqual(refused.exception.status, 503)


class RecoveryTests(fixtures.ServiceFixture, unittest.TestCase):
    """Commands use the real registry's queue without disturbing retry backoff."""

    def send(self, send):
        handler = actions.Actions(
            KEY,
            self.registry,
            lambda repository: Reader({"writer": "write"}),
            lambda repository: SimpleNamespace(send=send),
            [Target(REPOSITORY, REPOSITORY, "both")],
            user=lambda token: ("writer", 42),
            clock=self.clock,
        )
        body = json.dumps({"repository": REPOSITORY, "issue": 7, "command": "start"}).encode()
        return handler.handle(TOKEN, body)[0]

    def test_a_never_ticked_task_is_queued_durably_before_posting(self):
        def lost(method, path, body=None):
            raise TimeoutError()

        self.assertEqual(self.send(lost), 502)
        self.registry.close()
        registry = self.open()
        self.assertEqual(registry.due(self.clock()), [])
        [item] = registry.due(self.clock() + actions.RECOVERY_SECONDS)
        self.assertEqual((item.key, item.causes), (TASK, ("backoffice:start",)))

    def test_a_first_tick_in_flight_keeps_its_retry_deadline(self):
        generation = self.registry.grant(TASK, "worker", 60)
        self.assertEqual(self.send(lambda method, path, body=None: transport.Response(True, 422, {}, b"{}")), 502)
        self.registry.end(TASK, generation, Outcome("wait-until", False, 1800))
        self.assertEqual(self.registry.due(self.clock() + 1799), [])

    def test_an_ended_administrative_grant_still_allows_recovery(self):
        generation = self.registry.grant(TASK, "admin", 60, consume=False)
        self.registry.end(TASK, generation, None)
        self.assertEqual(self.send(lambda method, path, body=None: transport.Response(True, 422, {}, b"{}")), 502)
        [item] = self.registry.due(self.clock() + actions.RECOVERY_SECONDS)
        self.assertEqual(item.causes, ("backoffice:start",))

    def test_a_refused_post_keeps_an_existing_retry_deadline(self):
        generation = self.registry.grant(TASK, "worker", 60)
        self.registry.end(TASK, generation, Outcome("wait-until", False, 1800))
        self.assertEqual(self.send(lambda method, path, body=None: transport.Response(True, 422, {}, b"{}")), 502)
        self.assertEqual(self.registry.due(self.clock() + 1799), [])
        [item] = self.registry.due(self.clock() + 1800)
        self.assertEqual(item.causes, ("wait-until",))


class EndpointTests(unittest.TestCase):
    def serve(self, actions_handler):
        server = ingress.server(None, "127.0.0.1", 0, actions=actions_handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return http.client.HTTPConnection(*server.server_address, timeout=10)

    def post(self, connection, body=b"{}"):
        connection.request("POST", "/actions", body, {"Authorization": TOKEN, "Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def test_actions_route_to_the_handler_and_are_refused_when_unconfigured(self):
        seen = []

        class Handler:
            def handle(self, authorization, body):
                seen.append((authorization, body))
                return 201, {"posted": "/concurro stop"}

        connection = self.serve(Handler())
        self.assertEqual(self.post(connection), (201, {"posted": "/concurro stop"}))
        self.assertEqual(seen, [(TOKEN, b"{}")])
        connection.close()
        unconfigured = self.serve(None)
        status, payload = self.post(unconfigured)
        self.assertEqual(status, 503)
        self.assertIn("not configured", payload["error"])
        unconfigured.close()


if __name__ == "__main__":
    unittest.main()
