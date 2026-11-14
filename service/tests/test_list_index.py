"""The list index applies deliveries off the delivery path, repairs losses from GitHub's history and never affects tasks."""

import hashlib
import hmac
import http.client
import json
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

from service import ingress, list_index
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
        self.index.offer("one", "issues", KEY[0])
        self.index.offer("one", "issues", KEY[0])
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
            self.index.offer(f"g{number}", event, KEY[0])
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
        index.offer("dropped", "issues", KEY[0])
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
        self.index.offer("guid-1", "issues", KEY[0])
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
        self.index.offer("guid-2", "workflow_run", FORK)
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
                index.offer.assert_called_with(guid, "issues", KEY[0])
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
        groups, started = index.check.call_args.args
        self.assertEqual((list(groups), started), (["guid-1"], self.clock.now))
        index.check.side_effect = RuntimeError("index down")
        self.clock.now += 300
        recovery.poll()
        self.assertEqual(
            self.registry.webhook_check(recovery.scope), self.clock.now, "delivery recovery still completed"
        )
        index.rebaseline.assert_called_once()

    def test_the_registry_reports_committed_sponsorship_changes_only(self):
        seen = []
        self.registry.changed = seen.append
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
        self.registry.changed.assert_called_once_with(KEY[0])
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
