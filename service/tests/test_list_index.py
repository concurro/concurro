"""The list index applies deliveries off the delivery path, repairs losses from GitHub's history and never affects tasks."""

import hashlib
import hmac
import http.client
import json
import sqlite3
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from fixtures import KEY, ServiceFixture

from support import (
    setUpModule as setUpModule,  # unittest module fixtures
    tearDownModule as tearDownModule,
)

from github import NotFound, Transient
from service import ingress, list_index
from service.admission import EXECUTOR_WORKFLOW
from service.identity import IdentityError
from service.ingress import Ingress
from service.list_index import FRESH_SECONDS, REPLAY_LIMIT, ListIndex, Lists
from service.webhook_recovery import HISTORY_SECONDS, Recovery
from test_webhook_recovery import DeliveryAPI

SECRET = b"webhook-secret"
TOKEN = "ab" * 32
FORK = "owner/fork"
REPOSITORIES = {KEY[0]: 10, FORK: 11}


def summary(number, event="issues", repository=10, at=None, guid=None):
    """One delivery-history entry as GitHub lists it."""
    return {
        "id": number,
        "guid": guid or f"guid-{number}",
        "status": "OK",
        "status_code": 202,
        "installation_id": 42,
        "repository_id": repository,
        "event": event,
        "action": "edited",
        "delivered_at": datetime.fromtimestamp(at, UTC).isoformat(),
    }


class ListIndexTests(ServiceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.index = self.opened()

    def opened(self, start=True):
        index = ListIndex(self.directory.name, clock=self.clock)
        self.addCleanup(index.close)
        if start:
            index.start(REPOSITORIES)
        return index

    def revision(self, kind, repository=KEY[0], index=None):
        self.index.pending.join()
        return (index or self.index).revisions({repository})[repository][kind]["revision"]

    def changes(self):
        return self.index.db.execute("SELECT repository, kind FROM changes ORDER BY sequence").fetchall()

    def test_a_delivery_raises_its_kind_once_and_survives_a_restart(self):
        before = {kind: self.revision(kind) for kind in list_index.KINDS}
        self.index.offer("one", "issues", KEY[0], {"issue": issue(7)})
        self.index.offer("one", "issues", KEY[0], {"issue": issue(7)})
        self.index.offer("two", "push", KEY[0])
        after = {kind: self.revision(kind) for kind in list_index.KINDS}
        self.assertEqual({k for k in after if after[k] != before[k]}, {"issues"})
        self.assertEqual(self.revision("issues", FORK), before["issues"], "other repositories are unaffected")
        self.assertEqual(self.changes()[-1], (KEY[0], "issues"))
        self.index.check({}, self.clock.now)
        self.index.close()
        incarnation, self.index = self.index.incarnation, self.opened()
        self.assertEqual(self.index.incarnation, incarnation)
        self.assertEqual(self.revision("issues"), after["issues"], "a verified event kind keeps its revision")
        self.assertNotEqual(self.revision("sponsorships"), after["sponsorships"], "local signals may have been lost")
        self.assertEqual(self.changes()[-1], (FORK, None), "a restart tells the feed to resynchronize")

    def test_event_kinds(self):
        expected = {"issues": "issues", "issue_comment": "issues", "pull_request": "pulls", "check_run": "pulls"}
        for number, (event, kind) in enumerate({**expected, "check_suite": "pulls", "workflow_run": "runs"}.items()):
            before = self.revision(kind)
            self.index.offer(f"g{number}", event, KEY[0], {"workflow_run": run(number), "issue": issue(number)})
            self.assertNotEqual(self.revision(kind), before, event)

    def test_a_recreated_file_never_matches_old_revisions(self):
        old = self.revision("issues")
        self.index.close()
        for suffix in ("", "-wal", "-shm"):
            Path(self.directory.name, list_index.DATABASE + suffix).unlink(missing_ok=True)
        self.assertNotEqual(self.revision("issues", index=self.opened()), old)

    def test_a_full_queue_resynchronizes_instead_of_blocking(self):
        self.index.close()
        with patch.object(list_index, "QUEUE_SIZE", 1):
            index = self.opened(start=False)
        index.signal(KEY[0], "sponsorships")
        index.signal(KEY[0], "sponsorships")
        index.offer("dropped", "issues", KEY[0], {"issue": issue(7)})
        self.assertTrue(index.overflowed.is_set())
        index.start(REPOSITORIES)
        index.pending.join()
        for _ in range(100):
            if not index.overflowed.is_set():
                break
            threading.Event().wait(0.05)
        self.index = index
        resynchronized = [row for row in self.changes() if row[1] is None]
        self.assertGreaterEqual(
            len(resynchronized), 3 * len(REPOSITORIES) - 1, "at open, at start and for the overflow"
        )
        self.assertFalse(index.db.execute("SELECT 1 FROM receipts WHERE guid = 'dropped'").fetchone())

    def test_the_history_check_repairs_an_acknowledged_delivery_that_was_never_applied(self):
        self.index.offer("guid-1", "issues", KEY[0], {"issue": issue(7)})
        before = {kind: self.revision(kind) for kind in ("issues", "runs")}
        self.clock.now += 60
        groups = {
            "guid-1": [summary(1, at=self.clock.now - 30)],
            "guid-2": [summary(2, "workflow_run", 11, at=self.clock.now - 20)],
            "guid-3": [summary(3, at=self.clock.now - 3600)],
            "guid-4": [summary(4, "push", at=self.clock.now - 10)],
            "guid-5": [summary(5, repository=99, at=self.clock.now - 10)],
        }
        started = self.clock.now - 5
        self.assertEqual(self.index.check(groups, started), {(FORK, "runs"): 1})
        self.assertEqual(self.revision("issues"), before["issues"], "applied, before the baseline, other repository")
        self.assertNotEqual(self.revision("runs", FORK), before["runs"])
        self.assertEqual(self.index.check(groups, started), {}, "a repaired delivery has its receipt")
        self.index.offer("guid-2", "workflow_run", FORK, {"workflow_run": run(2)})
        self.index.pending.join()
        self.assertEqual(self.index.check(groups, started), {})

    def test_too_many_missing_deliveries_move_the_baseline(self):
        self.clock.now += 100
        groups = {f"guid-{n}": [summary(n, at=self.clock.now - 50)] for n in range(REPLAY_LIMIT + 1)}
        before = self.revision("issues")
        self.index.check(groups, self.clock.now)
        self.assertNotEqual(self.revision("issues"), before)
        self.assertEqual(self.index.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 0)
        self.assertEqual(self.index.check(groups, self.clock.now), {}, "the new baseline covers them")

    def test_state_follows_verification_against_history(self):
        state = lambda: self.index.revisions({KEY[0]})[KEY[0]]["issues"]
        self.assertEqual((state()["state"], state()["verified"]), ("catching-up", None))
        started = self.clock.now
        self.clock.now += 3
        self.index.check({}, started)
        self.assertEqual(state()["state"], "current")
        self.assertEqual(state()["verified"], datetime.fromtimestamp(started, UTC).isoformat(), "the scan's start")
        self.clock.now = started + FRESH_SECONDS + 1
        self.assertEqual(state()["state"], "catching-up")
        self.index.close()
        self.clock.now = started + HISTORY_SECONDS
        again = self.opened()
        baseline = again.db.execute("SELECT baseline FROM coverage WHERE kind = 'issues'").fetchone()[0]
        self.assertEqual(baseline, self.clock.now, "history can no longer cover the gap")

    def test_a_gap_older_than_history_is_rebaselined_by_the_check_itself(self):
        """The registry's coverage may be intact while this index's checks failed for days."""
        self.index.check({}, self.clock.now)
        before = self.revision("issues")
        self.clock.now += HISTORY_SECONDS
        late = {"guid-1": [summary(1, at=self.clock.now - 60)]}
        self.assertEqual(self.index.check(late, self.clock.now), {}, "the new baseline covers what history still lists")
        self.assertNotEqual(self.revision("issues"), before)
        self.assertEqual(self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"], "current")

    def test_a_gap_before_the_first_successful_check_is_rebaselined_too(self):
        """Checks that failed from the start leave no verified time, only the baseline."""
        before = self.revision("issues")
        self.clock.now += HISTORY_SECONDS
        self.index.check({}, self.clock.now)
        entry = self.index.revisions({KEY[0]})[KEY[0]]["issues"]
        self.assertEqual(entry["state"], "current")
        self.assertNotEqual(entry["revision"], before, "what expired from history is covered by a re-read")
        settled = entry["revision"]
        self.clock.now += 300
        self.index.check({}, self.clock.now)
        self.assertEqual(self.revision("issues"), settled, "an ordinary check afterwards changes nothing")

    def test_nothing_is_current_before_a_check_in_this_process(self):
        self.index.check({}, self.clock.now)
        self.index.close()
        self.index = self.opened()
        entry = self.index.revisions({KEY[0]})[KEY[0]]["issues"]
        self.assertEqual((entry["state"], entry["verified"] is not None), ("catching-up", True))
        self.index.check({}, self.clock.now)
        self.assertEqual(self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"], "current")

    def test_a_repository_that_left_the_configuration_is_no_longer_reported(self):
        self.index.check({}, self.clock.now)
        self.index.close()
        index = ListIndex(self.directory.name, clock=self.clock)
        self.addCleanup(index.close)
        index.start({KEY[0]: 10})
        index.check({"guid-9": [summary(9, "workflow_run", 11, at=self.clock.now)]}, self.clock.now)
        self.assertEqual(list(index.revisions({KEY[0], FORK})), [KEY[0]])
        self.assertFalse(index.db.execute("SELECT 1 FROM coverage WHERE repository = ?", (FORK,)).fetchone())

    def test_refresh_and_full_reconciliation_rebaseline(self):
        before = {kind: self.revision(kind) for kind in list_index.KINDS}
        self.index.rebaseline(KEY[0])
        self.assertTrue(all(self.revision(kind) != before[kind] for kind in list_index.KINDS))
        self.assertEqual(self.revision("issues", FORK), before["issues"])
        self.index.rebaseline()
        self.assertNotEqual(self.revision("issues", FORK), before["issues"])


def issue(number, *labels, updated="2026-11-14T10:00:00Z", state="open", **fields):
    """One issue as GitHub lists and delivers it."""
    return {
        "number": number,
        "title": f"Issue {number}",
        "state": state,
        "user": {"login": "author", "id": 5},
        "labels": [{"name": name, "color": "ededed"} for name in labels],
        "created_at": "2026-11-01T00:00:00Z",
        "updated_at": updated,
        "body": "not listed",
        **fields,
    }


class Hub:
    """An issue repository's API: the enrolled listing by page and issues by number."""

    def __init__(self):
        self.listed, self.items, self.paths, self.failing, self.during = [], {}, [], False, None

    def request(self, method, path):
        self.paths.append(path)
        if self.during:
            hook, self.during = self.during, None
            hook()
        if self.failing:
            raise Transient("unavailable")
        if "/issues?" in path:
            page = int(path.rsplit("page=", 1)[1])
            return self.listed[(page - 1) * 100 : page * 100]
        found = self.items.get(int(path.rsplit("/", 1)[1]), NotFound(path))
        if isinstance(found, Exception):
            raise found
        return found


class IssueRowsTests(ServiceFixture, unittest.TestCase):
    """Enrolled and sponsored issues held as rows, in the shape GitHub lists them."""

    def setUp(self):
        super().setUp()
        self.hub, self.sponsored = Hub(), []
        self.hub.listed = [issue(2, "concurro", "bug"), issue(1, "concurro", state="closed")]
        self.index = self.opened()

    def opened(self):
        index = ListIndex(self.directory.name, clock=self.clock)
        self.addCleanup(index.close)
        index.start(
            REPOSITORIES,
            rows={"issues": {KEY[0]}},
            reader=lambda repository: self.hub,
            work=False,
            sponsored=lambda repository: list(self.sponsored),
        )
        return index

    def built(self):
        self.clock.now += 1
        self.index.drain()
        self.index.check({}, self.clock.now)
        self.clock.now += 1

    def entry(self, kind="issues"):
        self.index.pending.join()
        return self.index.revisions({KEY[0]})[KEY[0]][kind]

    def listed(self):
        self.index.pending.join()
        return {
            item["number"]: [label["name"] for label in item["labels"]]
            for item in self.index.rows(KEY[0], "issues")["items"]
        }

    def deliver(self, guid, document, event="issues", action="edited"):
        self.clock.now += 1
        self.index.offer(guid, event, KEY[0], {"action": action, "issue": document})
        self.index.pending.join()

    def owed(self):
        return [row[0] for row in self.index.db.execute("SELECT item FROM pending WHERE kind = 'issues' ORDER BY item")]

    def test_a_rebuild_publishes_every_enrolled_issue_and_the_sponsored_ones(self):
        self.hub.listed = [issue(n, "concurro") for n in range(400, 50, -1)] + [issue(9, "concurro", pull_request={})]
        self.sponsored = [7, 8]
        self.hub.items[7] = issue(7, "question")
        self.assertEqual(self.entry()["state"], "rebuilding")
        self.built()
        listed = self.listed()
        self.assertEqual((len(listed), self.entry()["state"]), (351, "current"), "beyond three pages; no pull request")
        self.assertEqual(listed[7], ["question"], "sponsored, whatever its labels")
        self.assertNotIn(8, listed, "a sponsored issue GitHub no longer has")
        document = self.index.rows(KEY[0], "issues")["items"][0]
        self.assertEqual(set(document), {"number", "title", "state", "created_at", "updated_at", "user", "labels"})
        self.assertEqual((document["user"], document["labels"][0]), ({"login": "author"}, {"name": "concurro"}))

    def test_deliveries_keep_issues_current_under_the_ordering_rule(self):
        self.built()
        revision = self.entry()["revision"]
        self.deliver("enroll", issue(3, "concurro", updated="2026-11-14T10:01:00Z"), action="labeled")
        self.assertEqual(self.listed()[3], ["concurro"])
        self.assertNotEqual(self.entry()["revision"], revision)
        # A stage change arrives as two deliveries: the new label added, then the old one removed.
        added = issue(3, "concurro", "concurro:stage:intake", "concurro:stage:plan", updated="2026-11-14T10:02:00Z")
        removed = issue(3, "concurro", "concurro:stage:plan", updated="2026-11-14T10:02:02Z")
        self.deliver("added", added, action="labeled")
        self.assertEqual(self.listed()[3], ["concurro", "concurro:stage:intake", "concurro:stage:plan"])
        self.deliver("removed", removed, action="unlabeled")
        self.deliver("added-late", {**added, "title": "redelivered out of order"}, action="labeled")
        self.assertEqual(self.listed()[3], ["concurro", "concurro:stage:plan"], "the older snapshot is dropped")
        self.deliver(
            "comment",
            issue(3, "concurro", "concurro:stage:plan", updated="2026-11-14T10:05:00Z"),
            "issue_comment",
            "created",
        )
        self.assertEqual(self.index.rows(KEY[0], "issues")["items"][0]["updated_at"], "2026-11-14T10:05:00Z")
        revision = self.entry()["revision"]
        self.deliver("on-a-pull", issue(4, "concurro", pull_request={}), "issue_comment", "created")
        self.deliver("unrelated", issue(5, "bug"), action="opened")
        self.assertEqual(
            (self.entry()["revision"], sorted(self.listed())), (revision, [1, 2, 3]), "neither belongs to the list"
        )
        self.assertEqual(self.owed(), [])

    def test_unlabelling_keeps_the_row_out_of_the_list_until_relabelled(self):
        self.built()
        self.deliver("unlabel", issue(2, "bug", updated="2026-11-14T10:10:00Z"), action="unlabeled")
        self.assertEqual(sorted(self.listed()), [1])
        self.deliver("edit", issue(2, "bug", updated="2026-11-14T10:11:00Z", title="still followed"))
        self.deliver("relabel", issue(2, "bug", "concurro", updated="2026-11-14T10:12:00Z"), action="labeled")
        self.assertEqual(self.listed()[2], ["bug", "concurro"])

    def test_a_tie_with_different_content_is_settled_by_reading_the_issue(self):
        self.built()
        self.deliver("tie", issue(2, "concurro", "concurro:stage:plan"))
        self.assertEqual((self.listed()[2], self.owed()), (["concurro", "bug"], ["2"]))
        self.hub.items[2] = issue(2, "concurro", "concurro:stage:plan")
        self.clock.now += 1
        self.index.drain()
        self.assertEqual((self.listed()[2], self.owed()), (["concurro", "concurro:stage:plan"], []))

    def test_a_deleted_or_transferred_issue_leaves_and_is_not_brought_back_by_a_late_delivery(self):
        self.built()
        self.deliver("deleted", issue(2, "concurro", "bug", updated="2026-11-14T10:20:00Z"), action="deleted")
        self.deliver("moved", issue(1, "concurro", updated="2026-11-14T10:20:00Z"), action="transferred")
        self.assertEqual(self.listed(), {})
        self.deliver(
            "comment-deleted", issue(6, "concurro", updated="2026-11-14T10:21:00Z"), "issue_comment", "deleted"
        )
        self.assertEqual(sorted(self.listed()), [6], "a deleted comment is an ordinary update of its issue")
        self.deliver("late", issue(2, "concurro", updated="2026-11-14T10:19:00Z"), action="labeled")
        self.deliver("later", issue(2, "concurro", updated="2026-11-14T10:30:00Z"), action="labeled")
        self.assertEqual((sorted(self.listed()), self.owed()), ([6], ["2"]), "asked, not applied")
        self.hub.items[2] = RuntimeError("GitHub request failed: GET repos/o/r/issues/2 (HTTP 410)")
        self.clock.now += 1
        self.index.drain()
        self.assertEqual((sorted(self.listed()), self.owed()), ([6], []))
        self.deliver("never-seen", issue(70, "concurro", updated="2026-11-14T10:40:00Z"), action="deleted")
        self.deliver("never-seen-late", issue(70, "concurro", updated="2026-11-14T10:39:00Z"), action="labeled")
        self.assertNotIn(70, self.listed())

    def test_a_sponsorship_names_an_issue_the_list_has_never_seen(self):
        self.built()
        revision = self.entry()["revision"]
        self.sponsored = [9]
        self.hub.items[9] = issue(9, "question")
        self.index.signal(KEY[0], "sponsorships", 9)
        self.assertNotEqual(self.entry()["revision"], revision, "membership of the issue list may have changed")
        self.assertEqual(self.owed(), ["9"])
        self.clock.now += 1
        self.index.drain()
        self.assertEqual((self.listed()[9], self.owed()), (["question"], []))
        self.index.signal(KEY[0], "sponsorships", 9)
        self.index.signal(KEY[0], "sponsorships")
        self.assertEqual(self.entry() and self.owed(), [], "a held issue owes nothing")
        self.sponsored = []
        self.assertNotIn(9, self.listed(), "membership follows the registry")

    def test_a_lost_sponsorship_signal_is_repaired_at_start(self):
        self.built()
        self.sponsored = [9]
        self.hub.items[9] = issue(9)
        self.index.close()
        self.index = self.opened()
        self.assertEqual(self.owed(), ["9"])
        self.clock.now += 1
        self.index.drain()
        self.assertIn(9, self.listed())

    def test_a_lost_sponsorship_removal_still_moves_the_revision_at_start(self):
        self.sponsored = [9]
        self.hub.items[9] = issue(9)
        self.built()
        self.assertEqual(sorted(self.listed()), [1, 2, 9])
        self.index.check({}, self.clock.now)
        revision = self.entry()["revision"]
        self.index.close()
        self.sponsored = []
        self.index = self.opened()
        self.assertEqual(sorted(self.listed()), [1, 2])
        self.assertNotEqual(self.entry()["revision"], revision, "the list changed, so must its revision")

    def test_a_rebuild_reconciles_rows_that_are_held_but_not_listed(self):
        self.built()
        self.deliver("unlabel", issue(2, "bug", updated="2026-11-14T10:10:00Z"), action="unlabeled")
        self.hub.listed = [issue(1, "concurro", state="closed")]
        self.hub.items[2] = issue(2, "bug", updated="2026-11-14T11:00:00Z", title="Renamed while unseen")
        self.clock.now += 1
        self.index.rebaseline(KEY[0], kinds=("issues",))
        self.index.drain()
        self.sponsored = [2]
        self.index.signal(KEY[0], "sponsorships", 2)
        self.assertEqual(self.index.rows(KEY[0], "issues")["items"][0]["title"], "Renamed while unseen")
        self.assertEqual(self.entry() and self.owed(), [])

    def test_a_rebuild_rereads_what_its_listing_no_longer_returns(self):
        self.built()
        self.hub.listed = [issue(1, "concurro", state="closed")]
        self.hub.items[2] = issue(2, "bug", updated="2026-11-14T11:00:00Z")
        self.clock.now += 1
        self.index.rebaseline(KEY[0], kinds=("issues",))
        self.index.drain()
        self.assertEqual((sorted(self.listed()), self.entry()["state"]), ([1], "current"), "unlabelled while unseen")
        self.assertEqual(
            self.index.db.execute("SELECT COUNT(*) FROM issues WHERE document IS NOT NULL").fetchone()[0], 2
        )
        self.hub.listed = []
        self.hub.items[1] = RuntimeError("GitHub request failed: GET repos/o/r/issues/1 (HTTP 301)")
        self.clock.now += 1
        self.index.rebaseline(KEY[0], kinds=("issues",))
        self.index.drain()
        self.assertEqual((self.listed(), self.entry()["state"]), ({}, "current"), "moved away")
        self.hub.listed = [issue(3, "concurro")]
        self.sponsored = [4]
        self.hub.items[4] = RuntimeError("GitHub request failed: GET repos/o/r/issues/4 (HTTP 403)")
        self.clock.now += 1
        self.index.rebaseline(KEY[0], kinds=("issues",))
        self.index.drain()
        self.assertEqual(
            (self.listed(), self.entry()["state"]), ({}, "failed"), "a refusal fails the rebuild, not the row"
        )

    def test_a_delivery_during_a_rebuild_survives_it(self):
        self.built()

        def during():
            self.clock.now += 5
            self.index.offer(
                "during",
                "issues",
                KEY[0],
                {
                    "action": "labeled",
                    "issue": issue(2, "concurro", "bug", "concurro:stage:plan", updated="2026-11-14T12:00:00Z"),
                },
            )
            self.index.pending.join()

        self.hub.during = during
        self.clock.now += 1
        self.index.rebaseline(KEY[0], kinds=("issues",))
        self.index.drain()
        self.assertEqual(self.listed()[2], ["concurro", "bug", "concurro:stage:plan"])

    def test_the_history_check_replays_issue_payloads(self):
        self.built()
        details = {
            1: {"action": "labeled", "issue": issue(3, "concurro", updated="2026-11-14T10:05:00Z")},
            2: {"action": "created"},
        }
        groups = {"guid-1": [summary(1, "issues", at=self.clock.now)]}
        self.clock.now += 1
        self.index.check(groups, self.clock.now, lambda item: details[item["id"]])
        self.assertEqual((sorted(self.listed()), self.entry()["state"]), ([1, 2, 3], "current"))
        self.clock.now += 1
        self.index.check(
            {"guid-2": [summary(2, "issue_comment", at=self.clock.now - 1)]},
            self.clock.now,
            lambda item: details[item["id"]],
        )
        self.assertEqual(self.entry()["state"], "rebuilding", "a comment delivery without its issue cannot be applied")


def run(number, attempt=1, updated="2026-11-14T10:00:00Z", status="completed", created=None, **fields):
    """One executor run attempt as GitHub lists and delivers it."""
    return {
        "id": number,
        "run_attempt": attempt,
        "status": status,
        "conclusion": "success" if status == "completed" else None,
        "head_branch": "concurro/runs/session",
        "event": "workflow_dispatch",
        "path": f".github/workflows/{EXECUTOR_WORKFLOW}",
        "created_at": created or f"2026-11-14T09:{number % 60:02d}:00Z",
        "run_started_at": "2026-11-14T09:59:00Z",
        "updated_at": updated,
        "html_url": "https://github.invalid/not-listed",
        **fields,
    }


class Fork:
    """The fork's Actions API: a listing and exact attempts, which may fail or hook into a read."""

    def __init__(self):
        self.listed, self.attempts, self.paths, self.failing, self.during = [], {}, [], False, None

    def request(self, method, path):
        self.paths.append(path)
        if self.during:
            hook, self.during = self.during, None
            hook()
        if self.failing:
            raise Transient("unavailable")
        if "/attempts/" in path:
            number, _, attempt = path.rsplit("/runs/", 1)[1].partition("/attempts/")
            if (int(number), int(attempt)) not in self.attempts:
                raise NotFound(path)
            return self.attempts[int(number), int(attempt)]
        return {"workflow_runs": self.listed}


class RowsTests(ServiceFixture, unittest.TestCase):
    """The fork's executor runs held as rows: built from GitHub once, then kept from deliveries."""

    def setUp(self):
        super().setUp()
        self.fork = Fork()
        self.fork.listed = [run(3), run(2), run(1)]
        self.index = self.opened()

    def opened(self):
        index = ListIndex(self.directory.name, clock=self.clock)
        self.addCleanup(index.close)
        index.start(REPOSITORIES, rows={"runs": {FORK}}, reader=lambda repository: self.fork, work=False)
        return index

    def entry(self):
        self.index.pending.join()
        return self.index.revisions({FORK})[FORK]["runs"]

    def listed(self):
        self.index.pending.join()
        return [
            (item["run"]["id"], item["run"]["run_attempt"], item["first"] and item["first"]["run_attempt"])
            for item in self.index.rows(FORK, "runs")["items"]
        ]

    def built(self):
        """A first rebuild and a history check: the ordinary running state."""
        self.clock.now += 1
        self.index.drain()
        self.index.check({}, self.clock.now)
        self.clock.now += 1

    def deliver(self, guid, document, event="workflow_run"):
        self.clock.now += 1
        self.index.offer(guid, event, FORK, {"workflow_run": document})
        self.index.pending.join()

    def owed(self):
        return [row[0] for row in self.index.db.execute("SELECT item FROM pending ORDER BY item")]

    def test_the_first_rebuild_publishes_the_listing(self):
        before = self.entry()
        self.assertEqual((before["mode"], before["state"], before["built"]), ("rows", "rebuilding", None))
        self.assertEqual(self.index.revisions({KEY[0]})[KEY[0]]["runs"]["mode"], "signals", "only forks hold runs")
        self.assertEqual(self.listed(), [])
        self.built()
        after = self.entry()
        self.assertEqual((after["state"], after["built"] is not None), ("current", True))
        self.assertNotEqual(after["revision"], before["revision"])
        self.assertEqual(self.listed(), [(3, 1, 1), (2, 1, 1), (1, 1, 1)])
        self.assertNotIn("html_url", self.index.rows(FORK, "runs")["items"][0]["run"], "only listed fields are kept")
        self.assertIsNone(self.index.rows(KEY[0], "runs"))
        self.assertIsNone(self.index.rows(FORK, "issues"))

    def test_deliveries_keep_the_rows_current_under_the_ordering_rule(self):
        self.built()
        revision = self.entry()["revision"]
        self.deliver("new", run(4, status="in_progress", updated="2026-11-14T10:05:00Z"))
        self.assertEqual(self.listed()[0], (4, 1, 1))
        self.assertNotEqual(self.entry()["revision"], revision)
        self.deliver("done", run(4, updated="2026-11-14T10:09:00Z"))
        self.deliver("late", run(4, status="queued", updated="2026-11-14T10:01:00Z"))
        self.assertEqual(
            self.index.rows(FORK, "runs")["items"][0]["run"]["status"], "completed", "an older snapshot is dropped"
        )
        revision = self.entry()["revision"]
        self.deliver("again", run(4, updated="2026-11-14T10:09:00Z"))
        self.deliver("other", {**run(9), "path": ".github/workflows/ci.yml"})
        self.deliver("push", {**run(9), "event": "push"})
        self.assertEqual(self.entry()["revision"], revision, "an identical snapshot and other workflows change nothing")
        self.assertEqual(self.listed()[0], (4, 1, 1))
        self.assertEqual(self.owed(), [])

    def test_a_tie_with_different_content_is_settled_by_reading_that_attempt(self):
        self.built()
        self.deliver("tie", run(3, status="in_progress"))
        self.assertEqual(self.owed(), ["3/1"])
        self.assertEqual(self.index.rows(FORK, "runs")["items"][0]["run"]["status"], "completed", "not applied")
        self.fork.attempts[3, 1] = run(3, status="in_progress")
        self.clock.now += 1
        self.index.drain()
        self.assertEqual(self.index.rows(FORK, "runs")["items"][0]["run"]["status"], "in_progress")
        self.assertEqual(self.owed(), [])

    def test_a_rerun_first_seen_at_a_later_attempt_owes_attempt_one(self):
        self.built()
        self.deliver("rerun", run(7, attempt=3, updated="2026-11-14T10:20:00Z"))
        self.assertEqual((self.listed()[0], self.owed()), ((7, 3, None), ["7/1"]))
        self.fork.attempts[7, 1] = run(7)
        self.clock.now += 1
        self.index.drain()
        self.assertEqual((self.listed()[0], self.owed()), ((7, 3, 1), []))
        self.deliver("gone", run(8, attempt=2, updated="2026-11-14T10:30:00Z"))
        self.clock.now += 1
        self.index.drain()
        self.assertEqual((self.listed()[0], self.owed()), ((8, 2, None), []), "GitHub no longer has attempt 1")
        self.fork.paths.clear()
        self.deliver("gone-again", run(8, attempt=2, updated="2026-11-14T10:31:00Z"))
        self.index.drain()
        self.assertEqual(self.fork.paths, [], "an absent attempt is not asked for again")

    def test_an_owed_read_survives_a_restart_and_a_newer_request_survives_an_older_read(self):
        self.built()
        self.deliver("tie", run(3, status="in_progress"))
        self.index.close()
        self.index = self.opened()
        self.assertEqual(self.owed(), ["3/1"])
        requested = self.index.db.execute("SELECT requested FROM pending").fetchone()[0]
        self.fork.attempts[3, 1] = run(3, status="queued")

        def newer():
            self.clock.now += 5
            self.index.offer("tie-2", "workflow_run", FORK, {"workflow_run": run(3, status="waiting")})
            self.index.pending.join()

        self.fork.during = newer
        self.clock.now += 1
        self.index.read_run(FORK, "3/1")
        self.assertEqual(
            self.index.db.execute("SELECT requested FROM pending").fetchone()[0], requested, "its age is kept"
        )
        self.fork.attempts[3, 1] = run(3, status="waiting")
        self.clock.now += 1
        self.index.drain()
        self.assertEqual((self.owed(), self.index.rows(FORK, "runs")["items"][0]["run"]["status"]), ([], "waiting"))

    def test_a_rebuild_replaces_membership_but_keeps_runs_created_while_it_ran(self):
        self.built()
        self.deliver("extra", run(6, updated="2026-11-14T10:10:00Z"))
        self.assertEqual([item[0] for item in self.listed()], [6, 3, 2, 1])
        self.fork.listed = [run(5), run(3)]

        def created():
            self.clock.now += 5
            self.index.offer("during", "workflow_run", FORK, {"workflow_run": run(8, updated="2026-11-14T10:40:00Z")})
            self.index.pending.join()

        self.fork.during = created
        self.clock.now += 1
        self.index.rebaseline(FORK)
        self.assertEqual(self.entry()["state"], "rebuilding")
        self.index.drain()
        self.assertEqual(
            [item[0] for item in self.listed()], [8, 5, 3], "runs GitHub no longer lists leave; the new one stays"
        )
        self.assertEqual(self.entry()["state"], "current")

    def test_a_failed_rebuild_shows_and_waits_for_the_next_check(self):
        self.fork.failing = True
        self.index.drain()
        self.assertEqual(self.entry()["state"], "failed")
        calls = len(self.fork.paths)
        self.index.drain()
        self.assertEqual(len(self.fork.paths), calls, "not retried until the next history check")
        self.fork.failing = False
        self.index.check({}, self.clock.now)
        self.index.drain()
        self.assertEqual((self.entry()["state"], [item[0] for item in self.listed()]), ("current", [3, 2, 1]))

    def test_an_overdue_read_takes_the_kind_out_of_current(self):
        self.built()
        self.deliver("rerun", run(7, attempt=2))
        self.fork.failing = True
        self.index.drain()
        self.assertEqual(self.entry()["state"], "current", "a read owed for a moment is ordinary")
        self.clock.now += FRESH_SECONDS + 5
        self.index.check({}, self.clock.now)
        self.assertEqual(self.entry()["state"], "catching-up")

    def test_the_history_check_replays_payloads_or_rebuilds(self):
        self.built()
        details = {
            1: {"workflow_run": run(4, updated="2026-11-14T10:05:00Z")},
            2: {"workflow_run": {**run(9), "event": "push"}},
        }
        groups = {
            "guid-1": [summary(1, "workflow_run", 11, at=self.clock.now)],
            "guid-2": [summary(2, "workflow_run", 11, at=self.clock.now)],
        }
        self.clock.now += 1
        self.index.check(groups, self.clock.now, lambda item: details[item["id"]])
        self.assertEqual(([item[0] for item in self.listed()], self.entry()["state"]), ([4, 3, 2, 1], "current"))
        self.assertEqual(self.index.check(groups, self.clock.now, Mock(side_effect=AssertionError("has receipts"))), {})
        lost = {"guid-3": [summary(3, "workflow_run", 11, at=self.clock.now)]}
        self.clock.now += 1
        self.index.check(lost, self.clock.now, Mock(side_effect=RuntimeError("payload unreadable")))
        self.assertEqual(self.entry()["state"], "rebuilding", "what cannot be replayed is covered by a rebuild")
        self.fork.listed = [run(5), run(4)]
        self.index.drain()
        self.assertEqual([item[0] for item in self.listed()], [5, 4])
        many = {
            f"guid-m{n}": [summary(100 + n, "workflow_run", 11, at=self.clock.now + 1)] for n in range(REPLAY_LIMIT + 1)
        }
        self.clock.now += 2
        self.index.check(many, self.clock.now, Mock(side_effect=AssertionError("too many to replay")))
        self.assertEqual(self.entry()["state"], "rebuilding")

    def test_a_request_made_during_a_rebuild_is_still_owed_after_it(self):
        self.built()

        def again():
            self.clock.now += 5
            self.index.rebaseline(FORK)

        self.clock.now += 1
        self.index.rebaseline(FORK)
        self.fork.during = again
        self.clock.now += 1
        self.index.rebuild_runs(FORK)
        self.assertEqual(self.entry()["state"], "rebuilding", "the later request was not settled by the earlier read")
        listings = len(self.fork.paths)
        self.clock.now += 1
        self.index.drain()
        self.assertEqual((self.entry()["state"], len(self.fork.paths)), ("current", listings + 1))

    def test_a_replayed_delivery_a_newer_rebuild_covers_is_not_applied(self):
        self.built()
        self.deliver("old", run(6, updated="2026-11-14T10:10:00Z"))
        delivered = self.clock.now + 1
        self.clock.now += 2

        def detail(item):
            """While the payload is fetched, a rebuild publishes a listing without the run."""
            self.fork.listed = []
            self.index.rebaseline(FORK)
            self.clock.now += 5
            self.index.rebuild_runs(FORK)
            return {"workflow_run": run(6, status="in_progress", updated="2026-11-14T10:15:00Z")}

        self.index.check({"guid-7": [summary(7, "workflow_run", 11, at=delivered)]}, self.clock.now, detail)
        self.assertEqual(self.listed(), [], "the removed run did not come back")
        self.assertEqual(
            self.index.db.execute("SELECT outcome FROM receipts WHERE guid = 'guid-7'").fetchone()[0], "covered"
        )

    def test_an_attempt_github_no_longer_has_leaves_the_list(self):
        self.built()
        self.deliver("tie", run(3, status="in_progress"))
        revision = self.entry()["revision"]
        self.clock.now += 1
        self.index.drain()
        self.assertEqual(([item[0] for item in self.listed()], self.owed()), ([2, 1], []), "run 3 is gone, not stale")
        self.assertNotEqual(self.entry()["revision"], revision)
        self.deliver("rerun", run(2, attempt=2, updated="2026-11-14T10:20:00Z"))
        self.deliver("tie-2", run(2, attempt=2, status="queued", updated="2026-11-14T10:20:00Z"))
        self.fork.attempts[2, 1] = run(2)
        self.clock.now += 1
        self.index.drain()
        self.assertEqual([item[0] for item in self.listed()], [1], "its latest attempt is gone: no older one stands in")

    def test_a_delivery_cannot_undo_a_confirmed_absence(self):
        self.built()
        self.deliver("tie", run(3, status="in_progress"))
        self.clock.now += 1
        self.index.drain()
        self.assertEqual([item[0] for item in self.listed()], [2, 1])
        self.fork.paths.clear()
        self.deliver("late", run(3, status="queued", updated="2026-11-14T09:00:00Z"))
        self.deliver("later", run(3, status="completed", updated="2026-11-14T11:00:00Z"))
        self.assertEqual(([item[0] for item in self.listed()], self.owed()), ([2, 1], ["3/1"]), "asked, not applied")
        self.clock.now += 1
        self.index.drain()
        self.assertEqual(([item[0] for item in self.listed()], self.owed()), ([2, 1], []), "GitHub still says gone")
        self.deliver("back", run(3, updated="2026-11-14T11:30:00Z"))
        self.fork.attempts[3, 1] = run(3, updated="2026-11-14T11:30:00Z")
        self.clock.now += 1
        self.index.drain()
        self.assertEqual([item[0] for item in self.listed()], [3, 2, 1], "an authoritative read can restore it")

    def test_a_payload_without_its_run_is_not_an_unrelated_event(self):
        self.built()
        for payload in ({}, {"workflow_run": {}}, {"workflow_run": {"id": 9, "run_attempt": 1}}):
            with self.assertRaises(ValueError):
                self.index.offer("empty", "workflow_run", FORK, payload)
        groups = {"guid-5": [summary(5, "workflow_run", 11, at=self.clock.now)]}
        self.clock.now += 1
        self.index.check(groups, self.clock.now, lambda item: {"workflow_run": {}})
        self.assertEqual(self.entry()["state"], "rebuilding")
        self.assertFalse(self.index.db.execute("SELECT 1 FROM receipts WHERE guid = 'guid-5'").fetchone())

    def test_a_version_one_file_migrates_with_its_data(self):
        self.index.close()
        for suffix in ("", "-wal", "-shm"):
            Path(self.directory.name, list_index.DATABASE + suffix).unlink(missing_ok=True)
        db = sqlite3.connect(Path(self.directory.name, list_index.DATABASE))
        db.executescript(list_index.SCHEMA)
        db.execute("INSERT INTO meta VALUES ('version', '1'), ('incarnation', 'feedfacefeedface')")
        db.execute("INSERT INTO revisions VALUES (?, 'issues', 41)", (KEY[0],))
        db.execute(
            "INSERT INTO coverage VALUES (?, 'issues', 'signals', ?, ?)", (KEY[0], self.clock.now, self.clock.now)
        )
        db.execute("INSERT INTO coverage VALUES (?, 'runs', 'signals', ?, ?)", (FORK, self.clock.now, self.clock.now))
        db.commit()
        db.close()
        self.index = self.opened()
        self.assertEqual(self.index.revisions({KEY[0]})[KEY[0]]["issues"]["revision"], "feedfacefeedface:41")
        self.assertEqual((self.entry()["mode"], self.entry()["state"]), ("rows", "rebuilding"))
        self.built()
        self.assertEqual([item[0] for item in self.listed()], [3, 2, 1])

    def test_the_list_endpoint_serves_rows_only(self):
        self.built()
        lists = Lists(self.index, TOKEN)
        ask = lambda body: lists.handle("/lists", "Bearer " + TOKEN, json.dumps(body).encode())
        status, found = ask({"repository": FORK, "kind": "runs"})
        self.assertEqual(
            (status, found["state"], len(found["items"]), found["revision"]),
            (200, "current", 3, self.entry()["revision"]),
        )
        self.assertEqual(ask({"repository": KEY[0], "kind": "runs"})[0], 404)
        self.assertEqual(ask({"repository": FORK, "kind": "issues"})[0], 404)
        self.assertEqual(ask({"repository": FORK})[0], 400)
        self.assertEqual(lists.handle("/lists", "Bearer wrong", b"{}")[0], 401)


class WiringTests(ServiceFixture, unittest.TestCase):
    """Ingress, recovery, the registry and the listener, each with a list index that may fail."""

    def deliver(self, target, event, data, delivery):
        body = json.dumps(data).encode()
        headers = {
            "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest(),
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
        }
        return target.handle(headers, body)

    def test_ingress_offers_before_the_task_filter_and_ignores_failures(self):
        event = {
            "repository": {"full_name": KEY[0]},
            "installation": {"id": 42},
            "action": "labeled",
            "label": {"name": "concurro:stage:plan"},
            "issue": {"number": 7, "labels": [{"name": "concurro"}], "user": {"login": "author"}},
        }
        cases = {
            "bot": {**event, "sender": {"login": "concurro[bot]"}},
            "writer": {**event, "action": "edited", "label": None, "sender": {"login": "writer"}},
        }
        plain = Ingress(SECRET, self.registry, {KEY[0]: 42}, {"concurro[bot]"}, clock=self.clock)
        expected = {name: self.deliver(plain, "issues", data, "plain-" + name) for name, data in cases.items()}
        self.assertEqual(expected["bot"][0], 202)
        queued = [item.key for item in self.registry.due(self.clock())]
        for index in (Mock(), Mock(offer=Mock(side_effect=RuntimeError("index down")))):
            indexed = Ingress(
                SECRET, self.registry, {KEY[0]: 42}, {"concurro[bot]"}, clock=self.clock, list_index=index
            )
            for name, data in cases.items():
                guid = f"{id(index)}-{name}"
                self.assertEqual(self.deliver(indexed, "issues", data, guid), expected[name])
                index.offer.assert_called_with(guid, "issues", KEY[0], data)
            self.assertEqual([item.key for item in self.registry.due(self.clock())], queued)
        rejected = Mock()
        refused = Ingress(SECRET, self.registry, {KEY[0]: 42}, set(), clock=self.clock, list_index=rejected)
        self.assertEqual(self.deliver(refused, "issues", {**event, "installation": {"id": 7}}, "other")[0], 403)
        rejected.offer.assert_not_called()

    def test_recovery_checks_the_index_with_the_same_scan(self):
        api, index = DeliveryAPI(), Mock()
        api.items = [summary(1, at=self.clock.now)]
        reconciler = Mock(repositories=(KEY[0],), cleanup=None)
        recovery = Recovery(api, self.registry, reconciler, {KEY[0]: 42}, clock=self.clock, list_index=index)
        recovery.poll()
        index.rebaseline.assert_called_once_with()
        groups, started, detail = index.check.call_args.args
        self.assertEqual((list(groups), started), (["guid-1"], self.clock.now))
        api.details[1] = {"request": {"payload": {"action": "completed"}}}
        self.assertEqual(detail({"id": 1}), {"action": "completed"}, "the payload of one delivery")
        api.details[1] = {"request": {"payload": {}}}
        with self.assertRaises(IdentityError):
            detail({"id": 1})
        index.check.side_effect = RuntimeError("index down")
        self.clock.now += 300
        recovery.poll()
        self.assertEqual(
            self.registry.webhook_check(recovery.scope), self.clock.now, "delivery recovery still completed"
        )
        index.rebaseline.assert_called_once()

    def test_the_registry_reports_committed_sponsorship_changes_only(self):
        seen, issues = [], []
        self.registry.changed = lambda repository, number: (seen.append(repository), issues.append(number))
        with self.registry.transaction() as db:
            text = self.registry.noted(KEY, None, {"facts": [1]})
            db.execute("INSERT INTO issue_sponsorships VALUES (?,?,1,0,?)", (*KEY, text))
            self.assertEqual(seen, [], "nothing is reported before the commit")
        self.assertEqual(seen, [KEY[0]])
        with self.registry.transaction():
            self.registry.noted(KEY, text, {"facts": [1]})
        self.assertEqual(seen, [KEY[0]], "an unchanged document is not a change")
        with self.assertRaises(RuntimeError), self.registry.transaction():
            self.registry.noted(KEY, text, {"facts": [2]})
            raise RuntimeError("rolled back")
        self.registry.credential_events({"events": [], "next": 0, "more": False})
        self.assertEqual(seen, [KEY[0]], "neither a rollback nor an empty credential poll")
        revoked = {
            "sequence": 1,
            "user_id": 5,
            "route": "openai-api-key",
            "type": "revoked",
            "status": "revoked",
            "version": 1,
        }
        self.registry.credential_events({"events": [revoked], "next": 1, "more": False})
        self.assertEqual(seen, [KEY[0]] * 2, "a consumed credential event changes coverage without changing a document")
        self.registry.credential_events({"events": [revoked], "next": 1, "more": False})
        self.assertEqual(seen, [KEY[0]] * 2, "an event already consumed is not a change")
        self.registry.changed = Mock(side_effect=RuntimeError("listener down"))
        with self.registry.transaction():
            self.registry.noted(KEY, text, {"facts": [3]})
        self.registry.changed.assert_called_once_with(*KEY)
        self.assertEqual(issues, [KEY[1], None], "a document names its issue; a credential poll concerns them all")
        self.assertEqual(self.registry.sponsor_issues(KEY[0]), [KEY[1]], "the registry stays usable")

    def test_list_requests_need_the_token_and_never_reach_webhook_handling(self):
        index = ListIndex(self.directory.name, clock=self.clock)
        self.addCleanup(index.close)
        index.start(REPOSITORIES)
        plain = Ingress(SECRET, self.registry, {KEY[0]: 42}, set(), clock=self.clock)
        server = ingress.server(plain, "127.0.0.1", 0, lists=Lists(index, TOKEN))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def post(path, body, token=TOKEN):
            connection = http.client.HTTPConnection(*server.server_address, timeout=10)
            headers = {"Authorization": "Bearer " + token} if token else {}
            connection.request("POST", path, json.dumps(body).encode(), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"null") if path.startswith("/lists") else None

        status, found = post("/lists/revisions", {"repositories": [KEY[0], "owner/elsewhere"]})
        self.assertEqual((status, list(found["repositories"])), (200, [KEY[0]]))
        entry = found["repositories"][KEY[0]]["issues"]
        self.assertEqual((entry["mode"], entry["state"]), ("signals", "catching-up"))
        self.assertEqual(post("/lists/refresh", {"repository": KEY[0]})[0], 200)
        index.pending.join()
        self.assertNotEqual(index.revisions({KEY[0]})[KEY[0]]["issues"]["revision"], entry["revision"])
        self.assertEqual(post("/lists/refresh", {"repository": "owner/elsewhere"})[0], 404)
        self.assertEqual(post("/lists/revisions", {"repositories": "all"})[0], 400)
        self.assertEqual(post("/lists/unknown", {})[0], 404)
        for token in ("", "cd" * 32):
            self.assertEqual(post("/lists/revisions", {"repositories": []}, token)[0], 401)
        self.assertEqual(post("/elsewhere", {})[0], 401, "any other path is still a webhook delivery")
        server.lists = Lists(index, None)
        self.assertEqual(post("/lists/revisions", {"repositories": []})[0], 503)
        server.lists = None
        self.assertEqual(post("/lists/revisions", {"repositories": []})[0], 503)


if __name__ == "__main__":
    unittest.main()
