"""Live updates: the relay follows the service's changes feed and each browser stream shows only what its user may see."""

import asyncio
import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import uvicorn
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from test_backoffice import USER, FakeOAuth, settings  # noqa: E402
from test_list_freshness import TOKEN, Cached  # noqa: E402

from backoffice import app as app_module, live  # noqa: E402
from backoffice.auth import Sessions  # noqa: E402
from backoffice.commands import IndexUnavailable  # noqa: E402
from backoffice.live import RELOAD, Relay, shown, stream  # noqa: E402
from backoffice.read_cache import LISTING  # noqa: E402
from service.targets import Target  # noqa: E402

TARGET = Target("up/project", "owner/project", "upstream")
OTHER = Target("up/other", "owner/other", "upstream")


def change(sequence, kind="tasks", item="7", repository="up/project"):
    return {"sequence": sequence, "repository": repository, "kind": kind, "item": item}


class Feed:
    """The service's feed, scripted: each answer or failure in turn, then the relay is stopped."""

    def __init__(self, relay, *answers):
        self.relay, self.answers, self.asked = relay, list(answers), []
        relay.feed = self

    def __call__(self, incarnation, since):
        self.asked.append((incarnation, since))
        if not self.answers:
            self.relay.stop()
            raise IndexUnavailable("stopped")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class RelayTests(Cached):
    def setUp(self):
        super().setUp()
        self.relay, self.events = Relay(None, self.cache), []
        self.relay.stopped.wait = lambda delay: self.waits.append(delay)
        self.waits = []
        # Collect what a browser stream would be offered.
        self.relay.subscribers[Mock(put_nowait=self.events.append)] = Mock(
            call_soon_threadsafe=lambda put, waiting, event: put(waiting, event)
        )

    def saved(self, issue, repository="up/project"):
        self.cache.read("s", repository, issue, lambda: {"state": 1})
        return lambda: self.cache.read("s", repository, issue, lambda: {"state": 2})[1]

    def test_a_task_change_makes_its_snapshot_due_without_settling(self):
        task, other, listing = self.saved(7), self.saved(8), self.saved(LISTING + "issues")
        Feed(self.relay, {"incarnation": "a", "sequence": 3, "changes": [change(3), change(4, "issues", None)]})
        self.relay.run()
        status = task()
        self.assertEqual((status["refreshing"], status["pendingCommand"], status["stale"]), (True, False, True))
        self.assertEqual(
            (other()["refreshing"], listing()["refreshing"]), (False, False), "lists follow their revisions"
        )
        self.assertEqual(
            self.events,
            [
                {"kind": "tasks", "repository": "up/project", "issue": 7},
                {"kind": "issues", "repository": "up/project", "issue": None},
            ],
        )

    def test_resynchronization_makes_everything_saved_due_and_reloads(self):
        reads = [self.saved(7), self.saved(LISTING + "issues"), self.saved(9, "up/other")]
        Feed(self.relay, {"incarnation": "a", "sequence": 5, "changes": [change(5, None, None)]})
        self.relay.run()
        self.assertEqual([read()["refreshing"] for read in reads], [True, True, False], "one repository's signals")
        self.assertEqual(self.events, [RELOAD | {"repository": "up/project"}])
        self.settle()
        Feed(self.relay, {"incarnation": "b", "sequence": 0, "resynchronize": True, "changes": []})
        self.relay.stopped.clear()
        self.relay.run()
        self.assertEqual(
            [read()["refreshing"] for read in reads], [True, True, True], "the feed could not be continued"
        )
        self.assertEqual(self.events[-1], RELOAD)

    def test_the_relay_starts_without_a_cursor_and_recovers_from_the_same_one(self):
        feed = Feed(
            self.relay,
            IndexUnavailable("down"),
            {"incarnation": "a", "sequence": 9, "resynchronize": True, "changes": []},
            {"incarnation": "a", "sequence": 10, "changes": [change(10)]},
            IndexUnavailable("restarting"),
            {"changes": "malformed"},
            {"incarnation": "b", "sequence": 2, "resynchronize": True, "changes": []},
            {"incarnation": "b", "sequence": 2, "changes": []},
        )
        self.relay.run()
        self.assertEqual(
            feed.asked,
            [(None, None), (None, None), ("a", 9), ("a", 10), ("a", 10), ("a", 10), ("b", 2), ("b", 2)],
            "a lost answer is asked for again from the same cursor",
        )
        self.assertEqual([event["kind"] for event in self.events], ["reload", "tasks", "reload"])
        self.assertEqual(self.waits[:3], [1, 1, 2], "backing off while the service is away")

    def test_a_stream_that_fell_behind_gets_one_reload(self):
        async def overflow():
            relay = Relay(None)
            with patch.object(live, "QUEUED", 2):
                waiting = relay.subscribe()
            for number in range(3):
                relay.put(waiting, change(number))
            return [waiting.get_nowait() for _ in range(waiting.qsize())]

        self.assertEqual(asyncio.run(overflow()), [RELOAD])


class StreamTests(unittest.TestCase):
    def test_a_user_receives_only_changes_for_targets_they_can_see(self):
        event = {"kind": "tasks", "repository": "Owner/Project", "issue": 7}
        self.assertEqual(shown(event, [TARGET, OTHER]), event | {"targets": ["up/project"]}, "the fork's target")
        self.assertIsNone(shown(event, [OTHER]))
        self.assertIsNone(shown(RELOAD, []))
        self.assertEqual(shown(RELOAD, [OTHER])["targets"], ["up/other"])

    def test_a_stream_filters_keeps_alive_and_ends_with_its_session(self):
        async def read():
            relay, visible, lines = Relay(None), [[TARGET]], []
            events = stream(relay, lambda: visible[0], keepalive=0.05)
            lines.append(await anext(events))
            for repository in ("up/other", "up/project"):
                relay.apply(repository, "tasks", "7")
            await asyncio.sleep(0)
            lines.append(await anext(events))
            lines.append(await anext(events))
            visible[0] = None
            relay.apply("up/project", "tasks", "8")
            await asyncio.sleep(0)
            ended = [line async for line in events]
            return lines, ended, relay.subscribers

        lines, ended, subscribers = asyncio.run(read())
        self.assertEqual(lines[0], "retry: 5000\n\n")
        self.assertTrue(
            lines[1].startswith('event: change\ndata: {"kind": "tasks", "repository": "up/project"'), lines[1]
        )
        self.assertEqual((lines[2], ended, subscribers), (": keepalive\n\n", [], {}))

    def test_the_endpoint_needs_a_session_and_the_list_index(self):
        def client(token, signed_in=True):
            sessions = Sessions()
            sid = sessions.create("user-token", USER, 60)
            app = app_module.create_app(
                settings(lists_token=token),
                oauth=FakeOAuth(),  # type: ignore[arg-type]
                vault=Mock(),
                sessions=sessions,
                commands=Mock(),
            )
            return TestClient(app, cookies={app_module.SESSION_COOKIE: sid} if signed_in else {})

        self.assertEqual(client(None).get("/api/events").status_code, 404)
        self.assertEqual(client(TOKEN, signed_in=False).get("/api/events").status_code, 401)

    def test_a_signed_in_browser_receives_relayed_changes_over_http(self):
        answers, sessions = queue.Queue(), Sessions()
        sid = sessions.create("user-token", USER, 60)
        access = Mock()
        access.writable.side_effect = lambda repository, login, user: repository == "up/project"
        access.repository.return_value = {"full_name": "up/project", "description": ""}
        self.enterContext(patch.object(app_module, "Access", return_value=access))

        def feed(incarnation, since):
            try:
                return answers.get(timeout=0.05)
            except queue.Empty:
                return {"incarnation": "a", "sequence": since or 0, "changes": []}

        app = app_module.create_app(
            settings(targets=(TARGET, OTHER), lists_token=TOKEN),
            oauth=FakeOAuth(),  # type: ignore[arg-type]
            vault=Mock(),
            sessions=sessions,
            commands=Mock(list_changes=feed),
        )
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(setattr, server, "should_exit", True)
        while not server.started:
            time.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        cookies = {app_module.SESSION_COOKIE: sid}
        with httpx.stream("GET", f"http://127.0.0.1:{port}/api/events", cookies=cookies, timeout=5) as response:
            self.assertEqual(
                (response.status_code, response.headers["content-type"]), (200, "text/event-stream; charset=utf-8")
            )
            lines = response.iter_lines()
            self.assertEqual(next(lines), "retry: 5000")
            changes = [change(1, repository="up/other"), change(2), change(3, None, None, "owner/project")]
            answers.put({"incarnation": "a", "sequence": 3, "changes": changes})
            received = [line for line in (next(lines) for _ in range(6)) if line]
        self.assertEqual(
            received,
            [
                "event: change",
                'data: {"kind": "tasks", "repository": "up/project", "issue": 7, "targets": ["up/project"]}',
                "event: reload",
                'data: {"kind": "reload", "repository": "owner/project", "issue": null, "targets": ["up/project"]}',
            ],
            "nothing for the target this user cannot write to",
        )

    def test_the_app_follows_the_feed_while_it_runs(self):
        asked = threading.Event()

        def feed(incarnation, since):
            asked.set()
            raise IndexUnavailable("down")

        app = app_module.create_app(
            settings(lists_token=TOKEN),
            oauth=FakeOAuth(),
            vault=Mock(),
            commands=Mock(list_changes=feed),  # type: ignore[arg-type]
        )
        with TestClient(app):
            self.assertTrue(asked.wait(5))
