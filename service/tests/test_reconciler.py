"""Snapshots wake a task on every input change and on nothing else."""

import unittest

from fixtures import KEY, ServiceFixture
from support import setUpModule, tearDownModule  # unittest module fixtures
from controller import Outcome
from service.reconciler import HINT_SECONDS


class ReconcilerTests(ServiceFixture, unittest.TestCase):
    """Polling compares snapshots with what the last completed tick acknowledged."""

    def setUp(self):
        """Tick the seeded task to its plan, awaiting a writer's approval."""
        super().setUp()
        self.service()
        self.registry.enqueue(KEY, "test", self.clock())
        self.drain()
        self.bot = self.config["bot_login"]

    def settle(self):
        """Acknowledge current input, as a completed tick would."""
        self.registry.acknowledge(KEY, {**self.reconciler.snapshot(KEY), "_stage": "gates"})
        self.assertEqual(self.reconciler.poll(), [])

    def human(self):
        """The seeded writer's start command."""
        return next(c for c in self.fake.comments if c["user"]["login"] == "writer")

    def link_pull(self):
        """Give the task an open pull request with one human review."""
        self.fake.refs["concurro/7"] = "c" * 40
        self.fake.pr = {"number": 9, "state": "open", "merged": False, "head": {"sha": "c" * 40, "ref": "concurro/7"}}
        self.fake.human_reviews = [{"id": 1, "state": "COMMENTED", "user": {"login": "writer"}}]
        self.registry.link(KEY, 9)

    def test_pr_observation_uses_its_repository_not_the_issues(self):
        """A fork issue polls its upstream PR without confusing matching issue or PR numbers."""
        self.registry.link(KEY, 9, "upstream/project")
        calls = []

        class Upstream:
            def request(inner, method, path):
                calls.append((method, path))
                if path == "repos/upstream/project/pulls/9":
                    return {"head": {"sha": "c" * 40}, "merged": False}
                if "/status?" in path:
                    return {"statuses": []}
                if "/reviews?" in path:
                    return []
                if "/check-suites?" in path:
                    return {"check_suites": []}
                if "/actions/runs?" in path:
                    return {"workflow_runs": []}
                if "/check-runs?" in path:
                    return {"check_runs": [{"name": "CI", "status": "completed", "conclusion": "success"}]}
                raise AssertionError(path)

        self.reconciler.reader = lambda repository: (Upstream() if repository == "upstream/project" else self.backend)
        snapshot = self.reconciler.snapshot(KEY)
        self.assertEqual(snapshot["pull"]["number"], 9)
        self.assertEqual(snapshot["pull"]["checks"], [["c" * 40, None, None, "CI", "completed", "success"]])
        self.assertEqual(len(calls), 6)
        self.assertTrue(all(path.startswith("repos/upstream/project/") for _, path in calls))

    def test_status_and_test_merge_updates_wake_after_missed_webhooks(self):
        """Polling observes statuses and draft/closure state even without a check-run event."""
        self.link_pull()
        request = self.backend.request
        status = {"context": "external", "state": "pending"}
        def reading(method, path, data=None):
            if "/status?" in path:
                return {"statuses": [dict(status)]}
            return request(method, path, data)
        self.reconciler.reader = lambda _: type("Reader", (), {"request": staticmethod(reading)})()
        self.settle()
        status["state"] = "success"
        self.assertEqual(self.reconciler.poll(), [KEY])
        self.settle()
        self.fake.pr["merge_commit_sha"] = "d" * 40
        self.assertEqual(self.reconciler.poll(), [KEY])
        self.settle()
        self.fake.pr["state"] = "closed"
        self.assertEqual(self.reconciler.poll(), [KEY])

    def test_lost_webhooks_are_found(self):
        """Clarification, approval, retry, edits, deletions, requirements, reviews and checks each wake the task."""
        self.link_pull()
        changes = {
            "clarification": lambda: self.fake.comment("It means the guide's glossary."),
            "approval": lambda: self.fake.comment("/concurro approve " + "0" * 64),
            "retry": lambda: self.fake.comment("/concurro retry"),
            "comment edit": lambda: self.human().update(body="/concurro start please"),
            "comment deletion": lambda: self.fake.comments.remove(self.human()),
            "requirements": lambda: self.fake.issue.update(body="Explain this term and its history."),
            "closed": lambda: self.fake.issue.update(state="closed"),
            "enrollment": lambda: self.fake.events.append(
                {"id": 3, "event": "unlabeled", "label": {"name": "concurro"}, "actor": {"login": "writer"}}),
            "review": lambda: self.fake.human_reviews.append({"id": 2, "state": "CHANGES_REQUESTED"}),
            "review dismissal": lambda: self.fake.human_reviews[0].update(state="DISMISSED"),
            "check run": lambda: setattr(self.fake, "checks_green", False),
            "head": lambda: self.fake.pr["head"].update(sha="d" * 40),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                self.settle()
                change()
                self.assertEqual(self.reconciler.poll(), [KEY])

    def test_no_change_no_tick(self):
        """Repeated polls of unchanged input enqueue nothing and call no model."""
        calls = len(self.agents.calls)
        for _ in range(3):
            self.assertEqual(self.reconciler.poll(), [])
            self.assertEqual(self.drain(), 0)
        self.assertEqual(len(self.agents.calls), calls)

    def test_concurro_writes_never_wake_a_task(self):
        """The App's own comments and enrollment-label events are not input."""
        self.fake.comment("Concurro summary update.", self.bot)
        self.fake.events.append({"id": 9, "event": "labeled", "label": {"name": "concurro"}, "actor": {"login": self.bot}})
        self.fake.issue["labels"].append({"name": "concurro:stage:plan"})
        self.assertEqual(self.reconciler.poll(), [])

    def test_updated_at_is_only_a_hint(self):
        """An `updated_at` change alone enqueues nothing; while unchanged, scans are reused until the hint expires."""
        self.fake.issue["updated_at"] = "2026-11-02T10:00:00Z"
        self.settle()
        self.fake.issue["updated_at"] = "2026-11-02T10:05:00Z"
        self.assertEqual(self.reconciler.poll(), [])
        self.fake.comment("A reply the hint has not announced yet.")
        self.assertEqual(self.reconciler.poll(), [])
        self.fake.issue["updated_at"] = "2026-11-02T10:06:00Z"
        self.assertEqual(self.reconciler.poll(), [KEY])
        self.settle()
        self.fake.comment("Another reply.")
        self.clock.now += HINT_SECONDS + 1
        self.assertEqual(self.reconciler.poll(), [KEY])

    def test_label_removed_from_an_active_task(self):
        """The task is still observed and ticked; the engine decides what removal means."""
        self.fake.issue["labels"] = [label for label in self.fake.issue["labels"] if label["name"] != "concurro"]
        self.fake.events.append({"id": 3, "event": "unlabeled", "label": {"name": "concurro"}, "actor": {"login": "writer"}})
        self.assertEqual(self.reconciler.candidates(KEY[0]), [KEY])
        self.assertEqual(self.reconciler.poll(), [KEY])
        self.assertEqual(self.drain(), 1)

    def test_terminal_tasks_wake_only_when_reopened(self):
        """A terminal task ignores new comments while closed and wakes when its issue reopens."""
        self.fake.issue["state"] = "closed"
        generation = self.gateway.acquire(KEY, "test")
        self.gateway.release(KEY, generation, Outcome("terminal"))
        self.registry.acknowledge(KEY, {**self.reconciler.snapshot(KEY), "_stage": "gates"})
        self.fake.comment("Late remark.")
        self.assertEqual(self.reconciler.poll(KEY[:1]), [])
        self.assertEqual(self.reconciler.candidates(KEY[0]), [])
        self.fake.issue["state"] = "open"
        self.assertEqual(self.reconciler.poll(), [KEY])

    def test_input_during_a_tick_is_not_acknowledged(self):
        """A reply arriving mid-tick is left for a follow-up tick, which then acknowledges it."""
        self.fake.comment("/concurro approve " + self.state()["plan_id"])
        self.reconciler.poll()
        ask, replies = self.agents.ask, []

        def reply(provider, stage, context):
            self.agents.ask = ask
            self.fake.comment("One more detail.")
            replies.append(self.fake.comments[-1]["id"])
            return ask(provider, stage, context)

        self.agents.ask = reply
        generation = self.gateway.acquire(KEY, "test")
        self.gateway.release(KEY, generation, self.worker.run(KEY, generation))
        self.assertNotIn(replies[0], [c[0] for c in self.registry.acknowledged(KEY)["comments"]])
        self.assertIn(KEY, self.reconciler.poll())
        self.drain()
        self.assertIn(replies[0], [c[0] for c in self.registry.acknowledged(KEY)["comments"]])


if __name__ == "__main__":
    unittest.main()
