"""The list index applies bus deliveries, maintains projections and never affects tasks."""

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

from controller import Outcome
from github import NotFound, Transient
from service import ingress, list_index
from service.admission import EXECUTOR_WORKFLOW
from service.diagnostics import Store
from service.list_index import FRESH_SECONDS, HISTORY_SECONDS, ListIndex, Lists

TOKEN = "ab" * 32
FORK = "owner/fork"
REPOSITORIES = {KEY[0]: 10, FORK: 11}


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
        self.index.apply_delivery("one", "issues", KEY[0], {"issue": issue(7)})
        self.index.apply_delivery("one", "issues", KEY[0], {"issue": issue(7)})
        self.index.apply_delivery("two", "push", KEY[0], {})
        after = {kind: self.revision(kind) for kind in list_index.KINDS}
        self.assertEqual({k for k in after if after[k] != before[k]}, {"issues"})
        self.assertEqual(self.revision("issues", FORK), before["issues"], "other repositories are unaffected")
        self.assertEqual(self.changes()[-1], (KEY[0], "issues"))
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
        self.index.close()
        incarnation, self.index = self.index.incarnation, self.opened()
        self.assertEqual(self.index.incarnation, incarnation)
        self.assertEqual(self.revision("issues"), after["issues"], "a verified event kind keeps its revision")
        self.assertNotEqual(self.revision("sponsorships"), after["sponsorships"], "local signals may have been lost")
        self.assertEqual(self.changes()[-1], (FORK, None), "a restart tells the feed to resynchronize")
        self.assertIsNotNone(self.index.db.execute("SELECT 1 FROM receipts WHERE guid = 'one'").fetchone())
        self.clock.now += list_index.RETAINED_SECONDS + 1
        self.index.maintain()
        self.assertIsNone(self.index.db.execute("SELECT 1 FROM receipts WHERE guid = 'one'").fetchone())

    def test_event_kinds(self):
        expected = {"issues": "issues", "issue_comment": "issues", "pull_request": "pulls", "check_run": "pulls"}
        for number, (event, kind) in enumerate({**expected, "check_suite": "pulls", "workflow_run": "runs"}.items()):
            before = self.revision(kind)
            data = {"workflow_run": run(number), "issue": issue(number), "pull_request": delivered(pull(number))}
            self.index.apply_delivery(f"g{number}", event, KEY[0], {**data, event: {"head_sha": "a" * 40}, **data})
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

    def test_state_follows_verification_against_history(self):
        state = lambda: self.index.revisions({KEY[0]})[KEY[0]]["issues"]
        self.assertEqual((state()["state"], state()["verified"]), ("catching-up", None))
        started = self.clock.now
        self.clock.now += 3
        self.index.covered(datetime.fromtimestamp(started, UTC).isoformat())
        self.assertEqual(state()["state"], "current")
        self.assertEqual(state()["verified"], datetime.fromtimestamp(started, UTC).isoformat(), "the scan's start")
        self.clock.now = started + FRESH_SECONDS + 1
        self.assertEqual(state()["state"], "catching-up")
        self.index.close()
        self.clock.now = started + HISTORY_SECONDS
        again = self.opened()
        baseline = again.db.execute("SELECT baseline FROM coverage WHERE kind = 'issues'").fetchone()[0]
        self.assertEqual(baseline, self.clock.now, "history can no longer cover the gap")

    def test_bus_incompleteness_immediately_invalidates_current_state(self):
        """An observed loss or parked effect makes a verified list stale at once."""
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
        state = lambda: self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"]
        self.assertEqual(state(), "current")
        before = len(self.changes())
        self.index.consumer_completeness(False)
        self.assertEqual(state(), "catching-up")
        self.assertGreater(len(self.changes()), before)
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
        self.assertEqual(state(), "catching-up")
        self.index.consumer_completeness(True)
        self.assertEqual(state(), "current")

    def test_aged_coverage_rebaselines_on_the_next_applied_fact(self):
        """A coverage gap past the retention horizon raises the revision."""
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
        before = self.revision("issues")
        self.clock.now += HISTORY_SECONDS
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
        self.assertNotEqual(self.revision("issues"), before)
        self.assertEqual(self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"], "current")

    def test_aged_baseline_rebases_before_first_applied_coverage(self):
        """A scope never covered for three days starts with a new list baseline."""
        before = self.revision("issues")
        self.clock.now += HISTORY_SECONDS
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
        entry = self.index.revisions({KEY[0]})[KEY[0]]["issues"]
        self.assertEqual(entry["state"], "current")
        self.assertNotEqual(entry["revision"], before)
        settled = entry["revision"]
        self.clock.now += 300
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
        self.assertEqual(self.revision("issues"), settled)

    def test_nothing_is_current_before_a_check_in_this_process(self):
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
        self.index.close()
        self.index = self.opened()
        entry = self.index.revisions({KEY[0]})[KEY[0]]["issues"]
        self.assertEqual((entry["state"], entry["verified"] is not None), ("catching-up", True))
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
        self.assertEqual(self.index.revisions({KEY[0]})[KEY[0]]["issues"]["state"], "current")

    def test_a_repository_that_left_the_configuration_is_no_longer_reported(self):
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
        self.index.close()
        index = ListIndex(self.directory.name, clock=self.clock)
        self.addCleanup(index.close)
        index.start({KEY[0]: 10})
        index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
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


class FeedTests(ServiceFixture, unittest.TestCase):
    """The changes feed: replay from a cursor, a held request, and cursors it cannot continue from."""

    def setUp(self):
        super().setUp()
        self.index = ListIndex(self.directory.name, clock=self.clock)
        self.addCleanup(self.index.close)
        self.index.start(REPOSITORIES)
        self.start = self.index.changes(None, None, wait=0)

    def feed(self, since=None, wait=0, incarnation=None):
        since = self.start["sequence"] if since is None else since
        return self.index.changes(incarnation or self.index.incarnation, since, wait)

    def signalled(self, *items, kind="tasks"):
        for item in items:
            self.index.signal(KEY[0], kind, item)
        self.index.pending.join()

    def test_changes_replay_from_a_cursor(self):
        self.assertEqual((self.start["resynchronize"], self.start["changes"]), (True, []), "no cursor to continue from")
        self.assertEqual(self.feed()["changes"], [], "nothing since")
        self.signalled(7, 8)
        self.index.apply_delivery("guid", "issues", FORK, {"issue": issue(3)})
        self.index.pending.join()
        found = self.feed()
        self.assertEqual(
            [(c["repository"], c["kind"], c["item"]) for c in found["changes"]],
            [(KEY[0], "tasks", "7"), (KEY[0], "tasks", "8"), (FORK, "issues", None)],
        )
        self.assertNotIn("resynchronize", found)
        self.assertEqual(
            self.feed(found["changes"][0]["sequence"])["changes"], found["changes"][1:], "a repeated request"
        )
        self.assertEqual(self.feed(found["sequence"])["changes"], [])
        with patch.object(list_index, "FEED_CHANGES", 2):
            first = self.feed()
            self.assertEqual(first["changes"] + self.feed(first["sequence"])["changes"], found["changes"])

    def test_a_request_is_held_until_a_change_a_timeout_or_shutdown(self):
        answers = []
        waiter = threading.Thread(target=lambda: answers.append(self.feed(wait=30)))
        waiter.start()
        self.signalled(7)
        waiter.join(5)
        self.assertEqual([c["item"] for c in answers[0]["changes"]], ["7"])
        cursor = answers[0]["sequence"]
        self.assertEqual(self.feed(cursor, wait=0.05), {**answers[0], "changes": []}, "held, then answered empty")
        waiter = threading.Thread(target=lambda: answers.append(self.feed(cursor, wait=30)))
        waiter.start()
        self.index.release()
        waiter.join(5)
        self.assertEqual((waiter.is_alive(), answers[1]["changes"]), (False, []), "shutdown answers at once")

    def test_a_cursor_that_cannot_be_continued_resynchronizes(self):
        self.signalled(7)
        latest = self.feed()["sequence"]
        other = self.feed(latest, incarnation="another")
        self.assertEqual((other["resynchronize"], other["sequence"]), (True, latest), "another incarnation's")
        self.assertTrue(self.feed(latest + 5)["resynchronize"], "one this file never issued")
        self.clock.now += list_index.CHANGES_SECONDS + 1
        self.signalled(8)
        self.index.maintain()
        self.assertTrue(self.feed()["resynchronize"], "older than the changes kept")
        kept = [c["item"] for c in self.feed(latest)["changes"] if c["kind"] == "tasks"]
        self.assertEqual(kept, ["8"], "the newest pruned one still continues")
        self.clock.now += list_index.CHANGES_SECONDS + 1
        self.index.maintain()
        self.assertTrue(self.feed(latest)["resynchronize"], "everything since was pruned")
        current = self.index.changes(None, None, wait=0)["sequence"]
        self.assertNotIn("resynchronize", self.feed(current), "nothing missed since the newest change")

    def test_a_state_that_moves_without_a_list_changing_reaches_an_open_consumer(self):
        """After a restart a page reloads while recovery runs; a scan that finds nothing must still tell it."""
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
        self.index.close()
        self.index = ListIndex(self.directory.name, clock=self.clock)
        self.addCleanup(self.index.close)
        self.index.start(REPOSITORIES)
        cursor = self.index.changes(self.index.incarnation, self.start["sequence"], wait=0)["sequence"]
        revisions = self.index.revisions(set(REPOSITORIES))
        self.assertEqual(revisions[KEY[0]]["issues"]["state"], "catching-up")
        answers = []
        waiter = threading.Thread(target=lambda: answers.append(self.feed(cursor, wait=30)))
        waiter.start()
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
        waiter.join(5)
        found = self.feed(cursor)
        self.assertEqual(answers[0]["changes"], found["changes"][: len(answers[0]["changes"])])
        self.assertEqual(
            sorted((c["repository"], c["kind"], c["item"]) for c in found["changes"]),
            sorted((repository, "state", None) for repository in REPOSITORIES),
        )
        after = self.index.revisions(set(REPOSITORIES))
        self.assertEqual(after[KEY[0]]["issues"]["state"], "current", "observable before it is announced")
        self.assertEqual(
            {kind: entry["revision"] for kind, entry in after[KEY[0]].items()},
            {kind: entry["revision"] for kind, entry in revisions[KEY[0]].items()},
            "no list is read again for it",
        )
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
        self.assertEqual(self.feed(found["sequence"])["changes"], [], "an unchanged state announces nothing")

    def test_lost_local_signals_resynchronize_tasks_too(self):
        before = self.index.revisions({KEY[0]})[KEY[0]]["tasks"]["revision"]
        with self.index.transaction() as db:
            self.index.resync(db, [KEY[0]], self.clock.now)
        self.assertNotEqual(self.index.revisions({KEY[0]})[KEY[0]]["tasks"]["revision"], before)
        self.assertEqual([(c["repository"], c["kind"]) for c in self.feed()["changes"]], [(KEY[0], None)])


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
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
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
        self.index.apply_delivery(guid, event, KEY[0], {"action": action, "issue": document})
        self.index.pending.join()

    def test_bus_apply_commits_or_schedules_rebuild(self):
        """A bus ack follows a committed row or a durable rebuild request."""
        self.built()
        self.index.apply_delivery("sync-good", "issues", KEY[0], {"action": "edited", "issue": issue(7, "concurro")})
        self.assertIn(7, self.listed())
        self.assertEqual(
            self.index.db.execute("SELECT outcome FROM receipts WHERE guid = 'sync-good'").fetchone(),
            ("applied",),
        )
        self.index.apply_delivery("sync-bad", "issues", KEY[0], {"action": "edited", "issue": {"number": 8}})
        self.assertEqual(
            self.index.db.execute("SELECT outcome FROM receipts WHERE guid = 'sync-bad'").fetchone(),
            ("rebuild",),
        )
        self.assertIsNotNone(
            self.index.db.execute(
                "SELECT wanted FROM coverage WHERE repository = ? AND kind = 'issues'", (KEY[0],)
            ).fetchone()[0]
        )
        self.index.drain()
        self.assertIsNone(
            self.index.db.execute(
                "SELECT wanted FROM coverage WHERE repository = ? AND kind = 'issues'", (KEY[0],)
            ).fetchone()[0]
        )

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
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
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

    def test_an_overdue_read_that_changes_nothing_still_announces_the_state(self):
        self.built()
        held = issue(3, "concurro", updated="2026-11-14T10:01:00Z")
        self.deliver("enroll", held)
        self.deliver("tied", {**held, "title": "Same instant"})
        self.hub.items[3], self.hub.failing = held, True
        self.index.drain()
        self.clock.now += FRESH_SECONDS + 5
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
        self.assertEqual(
            (self.owed(), self.entry()["state"]), (["3"], "catching-up"), "verified, but a read is overdue"
        )
        revision = self.entry()["revision"]
        cursor = self.index.changes(None, None, wait=0)["sequence"]
        self.hub.failing = False
        self.index.drain()
        self.assertEqual((self.owed(), self.entry()["state"], self.entry()["revision"]), ([], "current", revision))
        changes = self.index.changes(self.index.incarnation, cursor, wait=0)["changes"]
        self.assertEqual([(c["repository"], c["kind"]) for c in changes], [(KEY[0], "state")])

    def test_a_delivery_during_a_rebuild_survives_it(self):
        self.built()

        def during():
            self.clock.now += 5
            self.index.apply_delivery(
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
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
        self.clock.now += 1

    def deliver(self, guid, document, event="workflow_run"):
        self.clock.now += 1
        self.index.apply_delivery(guid, event, FORK, {"workflow_run": document})
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
            self.index.apply_delivery("tie-2", "workflow_run", FORK, {"workflow_run": run(3, status="waiting")})
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
            self.index.apply_delivery(
                "during", "workflow_run", FORK, {"workflow_run": run(8, updated="2026-11-14T10:40:00Z")}
            )
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

    def test_a_failed_rebuild_retries_on_maintenance_without_coverage(self):
        self.fork.failing = True
        self.index.drain()
        self.assertEqual(self.entry()["state"], "failed")
        calls = len(self.fork.paths)
        self.index.drain()
        self.assertEqual(len(self.fork.paths), calls, "not retried until maintenance")
        self.fork.failing = False
        self.index.maintain()
        self.index.drain()
        self.assertEqual((self.entry()["state"], [item[0] for item in self.listed()]), ("catching-up", [3, 2, 1]))
        self.assertIsNone(self.entry()["verified"])
        self.assertFalse(self.index.checked)

    def test_an_overdue_read_takes_the_kind_out_of_current(self):
        self.built()
        self.deliver("rerun", run(7, attempt=2))
        self.fork.failing = True
        self.index.drain()
        self.assertEqual(self.entry()["state"], "current", "a read owed for a moment is ordinary")
        self.clock.now += FRESH_SECONDS + 5
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
        self.assertEqual(self.entry()["state"], "catching-up")

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

    def test_a_payload_without_its_run_rebuilds_the_list(self):
        """A malformed bus payload requests a rebuild instead of changing rows."""
        self.built()
        self.index.apply_delivery("empty", "workflow_run", FORK, {})
        self.assertEqual(self.entry()["state"], "rebuilding")
        self.assertEqual(
            self.index.db.execute("SELECT outcome FROM receipts WHERE guid = 'empty'").fetchone()[0], "rebuild"
        )

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


def pull(number, issue=None, sha="a" * 40, updated="2026-11-14T10:00:00Z", state="OPEN", checks=(), **fields):
    """One pull request as GraphQL lists it; `checks` are its rollup's `(status, conclusion)` check runs."""
    contexts = [{"__typename": "CheckRun", "status": status, "conclusion": conclusion} for status, conclusion in checks]
    return {
        "number": number,
        "title": f"PR {number}",
        "state": state,
        "isDraft": False,
        "headRefName": f"concurro/issue-{issue or number}-0123456789ab",
        "headRefOid": sha,
        "additions": 12,
        "deletions": 3,
        "updatedAt": updated,
        "commits": {
            "nodes": [
                {"commit": {"statusCheckRollup": {"contexts": {"nodes": [*contexts, {"__typename": "StatusContext"}]}}}}
            ]
        },
        **fields,
    }


def delivered(node):
    """The same pull request as a webhook delivers it."""
    return {
        "number": node["number"],
        "title": node["title"],
        "state": "open" if node["state"] == "OPEN" else "closed",
        "draft": node["isDraft"],
        "head": {"ref": node["headRefName"], "sha": node["headRefOid"], "repo": {"full_name": FORK}},
        "additions": node["additions"],
        "deletions": node["deletions"],
        "updated_at": node["updatedAt"],
        "body": "not listed",
    }


GREEN, RED, RUNNING = ("COMPLETED", "SUCCESS"), ("COMPLETED", "FAILURE"), ("IN_PROGRESS", None)


class Upstream:
    """An upstream's GraphQL API: the newest pull requests and pull requests by number."""

    def __init__(self):
        self.listed, self.items, self.numbers, self.failing, self.during = [], {}, [], False, None

    def request(self, method, path, data):
        """The answer as GitHub held it when asked; `during` runs after that, before it is returned."""
        number = data["variables"].get("number")
        self.numbers.append(number)
        listed = list(self.listed)
        found = ({node["number"]: node for node in listed} | self.items).get(number)
        if self.during:
            hook, self.during = self.during, None
            hook()
        if self.failing:
            raise Transient("unavailable")
        if number is None:
            return {"data": {"repository": {"pullRequests": {"nodes": listed}}}}
        errors = {} if found else {"errors": [{"type": "NOT_FOUND"}]}
        return {"data": {"repository": {"pullRequest": found}}, **errors}


class PullRowsTests(ServiceFixture, unittest.TestCase):
    """Task pull requests held as rows, with check runs read from the rollup."""

    def setUp(self):
        super().setUp()
        self.hub, self.linked = Upstream(), []
        self.index = ListIndex(self.directory.name, clock=self.clock)
        self.addCleanup(self.index.close)
        self.index.start(
            REPOSITORIES,
            rows={"pulls": {KEY[0]}},
            reader=lambda repository: self.hub,
            work=False,
            linked=lambda repository: list(self.linked),
        )

    def built(self):
        self.clock.now += 1
        self.index.drain()
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
        self.clock.now += 1

    def entry(self):
        self.index.pending.join()
        return self.index.revisions({KEY[0]})[KEY[0]]["pulls"]

    def listed(self):
        self.index.pending.join()
        return {item["number"]: item for item in self.index.rows(KEY[0], "pulls")["items"]}

    def deliver(self, guid, node=None, commit=None, event="check_run"):
        self.clock.now += 1
        if node:
            self.index.apply_delivery(
                guid, "pull_request", KEY[0], {"action": "edited", "pull_request": delivered(node)}
            )
        else:
            self.index.apply_delivery(guid, event, KEY[0], {"action": "completed", event: {"head_sha": commit}})
        self.index.pending.join()

    def owed(self):
        return [row[0] for row in self.index.db.execute("SELECT item FROM pending WHERE kind = 'pulls' ORDER BY item")]

    def read(self):
        self.clock.now += 1
        self.index.drain()

    def test_a_rebuild_publishes_task_pull_requests_with_their_checks(self):
        merged = pull(40, issue=36, state="MERGED", updated="2026-11-14T09:00:00Z")
        self.hub.listed = [pull(41, issue=36, checks=[GREEN]), merged, pull(37, headRefName="chore/other")]
        self.hub.items[5] = pull(5, updated="2026-10-01T00:00:00Z", checks=[RED])
        self.linked = [5, 6, 41]
        self.assertEqual(self.entry()["state"], "rebuilding")
        self.built()
        listed = self.listed()
        self.assertEqual((list(listed), self.entry()["state"]), ([41, 40, 5], "current"), "most recently updated first")
        self.assertEqual(self.hub.numbers[:3], [None, 5, 6], "linked ones outside the newest are read by number")
        self.assertEqual(sorted(self.hub.numbers[3:]), [5, 40, 41], "each discovered head's rollup is read once more")
        self.assertEqual(self.owed(), [])
        self.assertEqual(
            listed[41],
            {
                "number": 41,
                "title": "PR 41",
                "open": True,
                "draft": False,
                "ref": "concurro/issue-36-0123456789ab",
                "sha": "a" * 40,
                "additions": 12,
                "deletions": 3,
                "updated_at": "2026-11-14T10:00:00Z",
                "runs": [{"status": "completed", "conclusion": "success"}],
            },
        )
        self.assertEqual((listed[40]["open"], listed[5]["runs"][0]["conclusion"]), (False, "failure"))

    def test_a_rebuild_rereads_retained_rows_and_fails_without_the_rollup(self):
        self.hub.listed = [pull(41), pull(40)]
        self.built()
        self.hub.listed, self.hub.numbers = [pull(41, checks=[GREEN])], []
        self.hub.items[40] = pull(40, state="CLOSED", updated="2026-11-14T11:00:00Z")
        self.index.rebaseline(KEY[0], ("pulls",))
        self.built()
        self.assertEqual(self.hub.numbers, [None, 40], "a held row the listing no longer returns; no head is new")
        self.assertEqual([(n, item["open"]) for n, item in self.listed().items()], [(40, False), (41, True)])
        del self.hub.items[40]
        self.index.rebaseline(KEY[0], ("pulls",))
        self.built()
        self.assertEqual(list(self.listed()), [41], "one GitHub no longer has")
        for answer in ({"errors": [{"message": "refused"}]}, {"data": {"repository": None}}):
            self.hub.request = lambda method, path, data, answer=answer: answer
            self.index.rebaseline(KEY[0], ("pulls",))
            self.built()
            self.assertEqual((self.entry()["state"], list(self.listed())), ("failed", [41]))

    def test_deliveries_keep_pull_requests_current_and_owe_their_checks(self):
        self.built()
        opened = pull(41, checks=[RUNNING])
        self.deliver("other", pull(37, headRefName="chore/other"))
        self.assertEqual((self.listed(), self.owed()), ({}, []), "not a task branch")
        self.deliver("opened", opened)
        self.assertEqual((self.listed()[41]["runs"], self.owed()), ([], ["41"]), "listed before its checks are read")
        self.hub.items[41] = opened
        self.read()
        self.assertEqual(
            (self.listed()[41]["runs"], self.owed()), ([{"status": "in_progress", "conclusion": None}], [])
        )
        revision = self.entry()["revision"]
        self.deliver("titled", {**opened, "title": "Renamed", "updatedAt": "2026-11-14T10:01:00Z"})
        self.assertEqual((self.listed()[41]["title"], len(self.listed()[41]["runs"]), self.owed()), ("Renamed", 1, []))
        self.assertNotEqual(self.entry()["revision"], revision)
        pushed = pull(41, sha="b" * 40, updated="2026-11-14T10:02:00Z", additions=20, checks=[GREEN])
        self.deliver("pushed", {**pushed, "additions": 19})
        self.deliver("late", {**opened, "title": "Out of order", "updatedAt": "2026-11-14T10:01:30Z"})
        found = self.listed()[41]
        self.assertEqual((found["sha"], found["title"], found["runs"]), ("b" * 40, "PR 41", []), "a new head resets CI")
        self.hub.items[41] = pushed
        self.read()
        found = self.listed()[41]
        self.assertEqual(
            (found["additions"], found["runs"][0]["conclusion"]), (20, "success"), "the read corrects counts"
        )
        self.deliver("tied", {**pushed, "title": "Same instant"})
        self.assertEqual((self.listed()[41]["title"], self.owed()), ("PR 41", ["41"]), "a tie is answered by a read")

    def test_checks_owe_a_read_only_for_a_held_head(self):
        self.built()
        revision = self.entry()["revision"]
        self.deliver("early", commit="a" * 40)
        self.deliver("suite", commit="c" * 40, event="check_suite")
        self.assertEqual((self.owed(), self.entry()["revision"]), ([], revision), "no row has that head")
        self.hub.items[41] = pull(41, checks=[RED])
        self.deliver("opened", pull(41))
        self.read()
        self.assertEqual(self.listed()[41]["runs"], [{"status": "completed", "conclusion": "failure"}])
        # A rerun: the rollup then holds only the new attempt's check runs.
        self.hub.items[41] = pull(41, checks=[GREEN])
        self.deliver("rerun", commit="a" * 40)
        self.assertEqual(self.owed(), ["41"])
        revision = self.entry()["revision"]
        self.read()
        self.assertEqual(self.listed()[41]["runs"], [{"status": "completed", "conclusion": "success"}])
        self.assertNotEqual(self.entry()["revision"], revision)
        self.assertEqual(self.owed(), [])

    def test_signals_during_a_rollup_read_are_still_owed_after_it(self):
        self.built()
        self.hub.items[41] = pull(41, checks=[RUNNING])
        self.deliver("opened", pull(41))
        self.hub.during = lambda: self.deliver("completed", commit="a" * 40)
        self.read()
        self.assertEqual(self.owed(), [], "the read after the one the check interrupted")
        self.assertEqual(self.hub.numbers[-2:], [41, 41])
        pushed = pull(41, sha="b" * 40, updated="2026-11-14T10:05:00Z")

        def push():
            self.deliver("pushed", pushed)
            self.hub.items[41] = pull(41, sha="b" * 40, updated="2026-11-14T10:05:00Z", checks=[GREEN])
            self.hub.failing = True

        self.deliver("again", commit="a" * 40)
        self.hub.items[41] = pull(41, checks=[RED])
        self.hub.during = push
        self.read()
        self.hub.failing = False
        self.assertEqual((self.listed()[41]["sha"], self.listed()[41]["runs"]), ("b" * 40, []))
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
        self.read()
        self.assertEqual(self.listed()[41]["runs"], [{"status": "completed", "conclusion": "success"}])

    def test_a_check_during_a_read_that_discovers_its_head_is_not_lost(self):
        """The check matches no held head when delivered, so the read that brings the head owes another."""
        self.hub.listed = [pull(41, checks=[GREEN])]

        def failed():
            self.deliver("failed", commit="a" * 40)
            self.hub.items[41] = pull(41, checks=[RED])

        self.hub.during = failed
        self.built()
        self.assertEqual((self.listed()[41]["runs"][0]["conclusion"], self.owed()), ("failure", []), "a new row")
        for rebuild in (False, True):
            sha = ("c" if rebuild else "d") * 40
            stamp = "2026-11-14T10:1%d:00Z" % rebuild
            self.hub.listed = [pull(41, sha=sha, updated=stamp, checks=[GREEN])]
            self.hub.items[41] = self.hub.listed[0]

            def failed(sha=sha, stamp=stamp):
                self.deliver(f"failed-{sha[0]}", commit=sha)
                self.hub.items[41] = pull(41, sha=sha, updated=stamp, checks=[RED])

            self.hub.during = failed
            if rebuild:
                self.index.rebaseline(KEY[0], ("pulls",))
            else:
                self.deliver("checked", commit=self.listed()[41]["sha"])
            self.built()
            found = self.listed()[41]
            self.assertEqual((found["sha"], found["runs"][0]["conclusion"], self.owed()), (sha, "failure", []), rebuild)

    def test_an_answer_with_errors_is_never_published(self):
        self.hub.listed = [pull(41, checks=[RED])]
        self.built()
        partial = pull(41, commits={"nodes": [{"commit": {"statusCheckRollup": None}}]})
        errors = {"errors": [{"type": "INTERNAL", "path": ["repository", "pullRequest", "commits"]}]}
        answers = {
            None: {"data": {"repository": {"pullRequests": {"nodes": [partial]}}}, **errors},
            41: {"data": {"repository": {"pullRequest": partial}}, **errors},
        }
        self.hub.request = lambda method, path, data: answers[data["variables"].get("number")]
        self.deliver("checked", commit="a" * 40)
        self.read()
        self.assertEqual((self.listed()[41]["runs"][0]["conclusion"], self.owed()), ("failure", ["41"]))
        self.index.rebaseline(KEY[0], ("pulls",))
        self.read()
        self.assertEqual((self.listed()[41]["runs"][0]["conclusion"], self.entry()["state"]), ("failure", "failed"))
        answers[41] = {
            "data": {"repository": {"pullRequest": None}},
            "errors": [{"type": "NOT_FOUND"}, *errors["errors"]],
        }
        self.index.covered(datetime.fromtimestamp(self.clock.now, UTC).isoformat())
        with self.assertRaises(ValueError):
            self.index.read_pull(KEY[0], "41")
        self.assertIn(41, self.listed(), "absence only when not found is all GitHub reported")

    def test_a_result_for_a_head_that_moved_is_discarded(self):
        self.built()
        self.deliver("opened", pull(41))
        self.hub.items[41] = pull(41, checks=[RED])
        self.hub.during = lambda: self.deliver("pushed", pull(41, sha="b" * 40, updated="2026-11-14T10:05:00Z"))
        self.index.read_pull(KEY[0], "41")
        self.assertEqual((self.listed()[41]["sha"], self.listed()[41]["runs"]), ("b" * 40, []))
        self.assertEqual(self.owed(), ["41"], "the new head's read is still owed")


class WiringTests(ServiceFixture, unittest.TestCase):
    """Registry and listener changes reach the list index."""

    def test_the_registry_reports_committed_sponsorship_changes_only(self):
        seen, issues = [], []
        self.registry.changed = lambda kind, repository, number: (
            self.assertEqual(kind, "sponsorships"),
            seen.append(repository),
            issues.append(number),
        )
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
        self.registry.changed.assert_called_once_with("sponsorships", *KEY)
        self.assertEqual(issues, [KEY[1], None], "a document names its issue; a credential poll concerns them all")
        self.assertEqual(self.registry.sponsor_issues(KEY[0]), [KEY[1]], "the registry stays usable")

    def test_a_tick_outcome_is_a_task_change_for_a_live_generation_only(self):
        seen = []
        self.registry.changed = lambda *change: seen.append(change)
        generation = self.registry.grant(KEY, "worker", 60)
        self.assertFalse(self.registry.end(KEY, generation - 1, Outcome("terminal")))
        self.assertEqual([change for change in seen if change[0] == "tasks"], [], "a stale generation records nothing")
        self.assertTrue(self.registry.end(KEY, generation, Outcome("wait-until", retry_after=300)))
        self.assertEqual([change for change in seen if change[0] == "tasks"], [("tasks", *KEY)])

    def test_recorded_turn_changes_are_task_changes(self):
        store, seen = Store(Path(self.directory.name) / "diagnostics"), []
        self.addCleanup(store.close)
        store.changed = lambda repository, issue: seen.append((repository, issue))
        turn = {"request": {"stage": "plan"}}
        store.submitted("turn-1", turn, {"repository": KEY[0], "issue": KEY[1]})
        self.assertEqual(seen, [KEY])
        store.observed("turn-1", "queued", None, set())
        self.assertEqual(seen, [KEY], "an observation that shows nothing new")
        store.observed("turn-1", "running", {"started": 2.0}, {"s-1"})
        store.observed("turn-1", "running", None, {"s-1"}, {"session": "s-1", "run_id": 5})
        store.observed("turn-9", "running", None, set())
        self.assertEqual(seen, [KEY] * 3, "started, then a runner assigned; an unknown turn changes nothing")
        store.finished("turn-1", {"outcome": "ok"})
        self.assertEqual(seen, [KEY] * 4)
        store.changed = Mock(side_effect=RuntimeError("listener down"))
        store.finished("turn-1", {"outcome": "ok"})
        store.changed, written = lambda *task: seen.append(task), store.db
        store.db = Mock(execute=Mock(side_effect=sqlite3.OperationalError("disk full")))
        for record in (lambda: store.finished("turn-1", {}), lambda: store.observed("turn-1", "lost", None, set())):
            with self.assertRaises(sqlite3.OperationalError):
                record()
        store.db = written
        self.assertEqual(seen, [KEY] * 4, "a failed write is not a change")

    def test_list_requests_need_the_token_and_never_reach_webhook_handling(self):
        index = ListIndex(self.directory.name, clock=self.clock)
        self.addCleanup(index.close)
        index.start(REPOSITORIES)
        server = ingress.server("127.0.0.1", 0, lists=Lists(index, TOKEN))
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
        status, found = post("/lists/changes", {"incarnation": None, "since": None})
        self.assertEqual((status, found["resynchronize"], found["incarnation"]), (200, True, index.incarnation))
        index.signal(KEY[0], "tasks", 7)
        status, later = post("/lists/changes", {"incarnation": found["incarnation"], "since": found["sequence"]})
        self.assertEqual((status, [(c["kind"], c["item"]) for c in later["changes"]]), (200, [("tasks", "7")]))
        for body in ({"incarnation": 5, "since": 0}, {"incarnation": "x", "since": "0"}, {"since": 0}):
            self.assertEqual(post("/lists/changes", body)[0], 400)
        for token in ("", "cd" * 32):
            self.assertEqual(post("/lists/revisions", {"repositories": []}, token)[0], 401)
        self.assertEqual(post("/elsewhere", {})[0], 404, "unknown private paths are absent")
        server.lists = Lists(index, None)
        self.assertEqual(post("/lists/revisions", {"repositories": []})[0], 503)
        server.lists = None
        self.assertEqual(post("/lists/revisions", {"repositories": []})[0], 503)


if __name__ == "__main__":
    unittest.main()
