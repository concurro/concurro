"""List freshness from the service's list index: revisions instead of age, saved copies while it is away."""

import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from test_backoffice import USER, FakeOAuth, settings  # noqa: E402

from backoffice import app as app_module  # noqa: E402
from backoffice.auth import Sessions  # noqa: E402
from backoffice.commands import CommandError, IndexUnavailable, ServiceCommands  # noqa: E402
from backoffice.read_cache import LISTING, ReadCache  # noqa: E402
from backoffice.tasks import Tasks  # noqa: E402
from service.targets import Target  # noqa: E402

TOKEN = "ab" * 32
RUN = {
    "id": 77,
    "run_attempt": 1,
    "status": "completed",
    "conclusion": "success",
    "head_branch": "concurro/runs/00000000-0000-0000-0000-000000000000",
    "created_at": "2026-11-13T12:00:00Z",
    "run_started_at": "2026-11-13T12:00:00Z",
    "updated_at": "2026-11-13T12:02:00Z",
}


class Index:
    """The service's answers: one revision per kind, movable, or no answer at all."""

    def __init__(self, repositories=("up/project",)):
        self.repositories, self.down, self.asked, self.refreshed = repositories, False, 0, []
        self.counters = dict.fromkeys(("issues", "sponsorships", "pulls", "runs"), 1)
        self.modes = dict.fromkeys(self.counters, "signals")
        self.state, self.built, self.rows, self.listed = "current", "2026-11-14T10:00:00+00:00", [], 0

    def list_revisions(self, repositories):
        self.asked += 1
        if self.down:
            raise IndexUnavailable("down")
        entry = lambda kind: {
            "revision": f"x:{self.counters[kind]}",
            "mode": self.modes[kind],
            "state": self.state,
            "verified": "2026-11-14T10:00:00+00:00",
            "built": self.built if self.modes[kind] == "rows" else None,
        }
        return {r: {kind: entry(kind) for kind in self.counters} for r in self.repositories if r in repositories}

    def list_rows(self, repository, kind):
        self.listed += 1
        if self.down:
            raise IndexUnavailable("down")
        return self.rows

    def list_refresh(self, repository):
        if self.down:
            raise IndexUnavailable("down")
        self.refreshed.append(repository)


class Cached(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = 1000.0
        self.cache = ReadCache(self.root, clock=lambda: self.now)
        self.addCleanup(lambda: self.cache.close())

    def settle(self):
        for pending, _ in list(self.cache.pending.values()):
            pending.result(5)


class ReadCacheFreshnessTests(Cached):
    def read(self, load, **freshness):
        return self.cache.read("s", "o/r", LISTING + "issues", load, **freshness)

    def test_data_saved_against_a_revision_does_not_age(self):
        load = Mock(side_effect=[{"items": [n]} for n in (1, 2, 3, 4)])
        self.assertEqual(self.read(load, revision="a")[0], {"items": [1]})
        self.now += 86_400
        data, status = self.read(load, revision="a")
        self.assertEqual(
            (data, status["stale"], status["refreshing"], load.call_count), ({"items": [1]}, False, False, 1)
        )
        data, status = self.read(load, revision="b")
        self.assertEqual((data, status["stale"], status["refreshing"]), ({"items": [1]}, True, True))
        self.settle()
        self.assertEqual(self.read(load, revision="b")[0], {"items": [2]})
        self.assertEqual(load.call_count, 2)
        # Without the index the age rule applies again, starting with one read.
        self.assertTrue(self.read(load)[1]["refreshing"])
        self.settle()
        self.assertEqual((self.read(load)[0], load.call_count), ({"items": [3]}, 3))
        self.now += 61
        self.assertTrue(self.read(load)[1]["refreshing"])
        self.settle()

    def test_the_revision_saved_is_the_one_read_before_the_load(self):
        """A delivery that lands during the read moves the revision, so the list is due again."""
        self.read(lambda: {"items": ["during a"]}, revision="a")
        load = Mock(return_value={"items": ["during b"]})
        self.assertTrue(self.read(load, revision="b")[1]["refreshing"])
        self.settle()
        self.assertFalse(self.read(load, revision="b")[1]["stale"])

    def test_local_invalidation_outdates_at_an_unchanged_revision_and_rejects_older_reads(self):
        self.read(lambda: {"items": ["old"]}, revision="a")
        self.cache.read("s", "o/r", LISTING + "sponsorships:1", lambda: {"items": ["old"]}, revision="a")
        release = threading.Event()
        self.addCleanup(release.set)

        def before():
            self.assertTrue(release.wait(5))
            return {"items": ["read before the invalidation"]}

        key = LISTING + "sponsorships:1"
        self.assertTrue(self.cache.read("s", "o/r", key, before, revision="b")[1]["refreshing"])
        self.cache.outdate(LISTING + "sponsorships:")
        release.set()
        self.settle()
        after = Mock(return_value={"items": ["new"]})
        data, status = self.cache.read("s", "o/r", key, after, revision="b")
        self.assertEqual((data, status["refreshing"]), ({"items": ["old"]}, True), "the older read was discarded")
        self.settle()
        self.assertEqual(self.cache.read("s", "o/r", key, after, revision="b")[0], {"items": ["new"]})
        self.assertFalse(self.read(after, revision="a")[1]["stale"], "other lists were not outdated")
        self.cache.outdate(LISTING, ["O/R"])
        self.assertTrue(self.read(after, revision="a")[1]["refreshing"])

    def test_saved_only_never_reads(self):
        load = Mock(return_value={"items": [1]})
        with self.assertRaises(LookupError):
            self.read(load, saved="The index is unavailable.")
        self.read(load, revision="a")
        self.now += 86_400
        data, status = self.read(load, saved="The index is unavailable.")
        self.assertEqual((data, status["stale"], status["refreshing"]), ({"items": [1]}, True, False))
        self.assertEqual((status["error"], load.call_count), ("The index is unavailable.", 1))

    def test_an_incomplete_read_never_replaces_a_complete_copy_and_is_retried(self):
        load = Mock(side_effect=[{"items": ["good"]}, {"items": None, "degraded": True}, {"items": ["recovered"]}])
        self.read(load, revision="a")
        self.assertTrue(self.read(load, revision="b")[1]["refreshing"])
        self.settle()
        self.now += 86_400 - 61
        data, status = self.read(load, revision="b")
        self.assertEqual(
            (data, status["stale"], bool(status["error"]), load.call_count), ({"items": ["good"]}, True, True, 2)
        )
        self.now += 61
        self.assertTrue(self.read(load, revision="b")[1]["refreshing"], "retried at an unchanged revision")
        self.settle()
        data, status = self.read(load, revision="b")
        self.assertEqual((data, status["stale"], status["error"]), ({"items": ["recovered"]}, False, None))

    def test_an_incomplete_first_read_is_shown_flagged_and_retried(self):
        load = Mock(side_effect=[{"items": None, "degraded": True}, {"items": ["good"]}])
        data, status = self.read(load, revision="a")
        self.assertEqual((data["items"], status["stale"], bool(status["error"])), (None, True, True))
        self.now += 61
        self.read(load, revision="a")
        self.settle()
        self.assertEqual(self.read(load, revision="a")[0], {"items": ["good"]})

    def test_an_existing_database_gains_the_revision_column(self):
        self.read(lambda: {"items": [1]})
        self.cache.close()
        db = sqlite3.connect(self.root / "issue-reads.sqlite3")
        db.execute("ALTER TABLE snapshots DROP COLUMN revision")
        db.commit()
        db.close()
        self.cache = ReadCache(self.root, clock=lambda: self.now)
        load = Mock()
        self.assertEqual((self.read(load)[0], load.call_count), ({"items": [1]}, 0))


class ServiceClientTests(unittest.TestCase):
    def client(self, handler):
        return ServiceCommands("http://service.invalid", httpx.Client(transport=httpx.MockTransport(handler)), TOKEN)

    def test_list_requests_carry_the_shared_token_and_no_user_token(self):
        seen = []

        def handler(request):
            seen.append((request.url.path, request.headers["authorization"]))
            return httpx.Response(200, json={"repositories": {"up/project": {}}, "refreshed": True})

        service = self.client(handler)
        self.assertEqual(service.list_revisions(["up/project"]), {"up/project": {}})
        service.list_refresh("up/project")
        self.assertEqual(seen, [("/lists/revisions", "Bearer " + TOKEN), ("/lists/refresh", "Bearer " + TOKEN)])

    def test_the_client_and_the_service_agree(self):
        """The backoffice's requests against the service's own listener and index."""
        from service import ingress
        from service.list_index import ListIndex, Lists

        root = self.enterContext(tempfile.TemporaryDirectory())
        index = ListIndex(root)
        self.addCleanup(index.close)
        fork = Mock(request=Mock(return_value={"workflow_runs": [RUN]}))
        index.start(
            {"up/project": 10, "owner/project": 11}, rows={"runs": {"owner/project"}}, reader=lambda r: fork, work=False
        )
        index.drain()
        server = ingress.server(Mock(), "127.0.0.1", 0, lists=Lists(index, TOKEN))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = "http://{}:{}".format(*server.server_address)
        service = ServiceCommands(url, lists_token=TOKEN)
        held = service.list_revisions(["owner/project"])["owner/project"]["runs"]
        self.assertEqual((held["mode"], held["built"] is not None), ("rows", True))
        self.assertEqual([item["run"]["id"] for item in service.list_rows("owner/project", "runs")], [77])
        before = service.list_revisions(["up/project"])["up/project"]
        self.assertEqual(set(before), {"issues", "sponsorships", "pulls", "runs"})
        service.list_refresh("up/project")
        self.assertNotEqual(
            service.list_revisions(["up/project"])["up/project"]["issues"]["revision"], before["issues"]["revision"]
        )
        with self.assertRaises(IndexUnavailable):
            service.list_rows("up/project", "runs")
        with self.assertRaises(IndexUnavailable):
            ServiceCommands(url, lists_token="cd" * 32).list_revisions(["up/project"])
        with self.assertRaises(IndexUnavailable):
            ServiceCommands(url).list_revisions(["up/project"])

    def test_every_failure_is_index_unavailable(self):
        def refuse(request):
            raise httpx.ConnectError("refused")

        answers = [
            lambda request: httpx.Response(401, text="invalid signature"),
            lambda request: httpx.Response(401, json={"error": "invalid list index token"}),
            lambda request: httpx.Response(503, json={"error": "unavailable"}),
            lambda request: httpx.Response(200, text="not json"),
            lambda request: httpx.Response(200, json={"repositories": []}),
            refuse,
        ]
        for answer in answers:
            with self.assertRaises(IndexUnavailable):
                self.client(answer).list_revisions(["up/project"])


class ListingTests(Cached):
    def setUp(self):
        super().setUp()
        target = Target("up/project", "up/project", "upstream")
        self.access = Mock()
        self.access.repository.return_value = {"full_name": "up/project", "description": ""}
        self.access.task_issues.return_value = []
        self.access.pull_requests.return_value = []
        self.access.config.return_value = ({"repository": "up/project"}, "main")
        self.access.writable.return_value = True
        self.index, self.sponsorships = Index(), Mock(return_value={"issues": []})
        self.settings = settings(targets=(target,))

    def tasks(self, lists="index"):
        return Tasks(
            self.settings,
            self.access,
            USER,
            read_cache=self.cache,
            sponsorships=self.sponsorships,
            lists=self.index if lists == "index" else lists,
        )

    def reads(self):
        return [self.access.task_issues.call_count, self.access.pull_requests.call_count, self.sponsorships.call_count]

    def test_an_unchanged_revision_reads_nothing_however_old(self):
        first = self.tasks().overview()["cache"]
        self.assertEqual((first["state"], first["verified"]), ("current", "2026-11-14T10:00:00+00:00"))
        self.assertEqual(self.reads(), [1, 1, 1])
        for _ in range(3):
            self.now += 3600
            view = self.tasks().target_view("up/project")["cache"]
            self.assertEqual((view["stale"], view["refreshing"]), (False, False))
        self.assertEqual(self.reads(), [1, 1, 1])
        self.assertEqual(self.index.asked, 4, "one index request per page request")

    def test_a_moved_revision_rereads_only_its_kind(self):
        self.tasks().overview()
        self.index.counters["issues"] += 1
        self.assertTrue(self.tasks().overview()["cache"]["refreshing"])
        self.settle()
        self.assertEqual(self.reads(), [2, 1, 1])
        self.index.counters["sponsorships"] += 1
        self.index.state = "catching-up"
        self.assertEqual(self.tasks().overview()["cache"]["state"], "catching-up")
        self.settle()
        self.assertEqual(self.reads(), [2, 1, 2])

    def test_an_unavailable_index_serves_saved_lists_and_reads_nothing(self):
        self.tasks().overview()
        self.index.down = True
        self.now += 86_400
        view = self.tasks().overview()["cache"]
        self.assertEqual(
            (view["state"], view["refreshing"], view["error"]),
            ("unavailable", False, "Concurro's list index is unavailable."),
        )
        self.assertEqual(self.reads(), [1, 1, 1])
        self.cache.outdate(LISTING)
        self.tasks().overview()
        self.assertEqual(self.reads(), [1, 1, 1], "even an invalidated list is not read while the index is away")

    def test_nothing_saved_and_no_index_fails_the_list(self):
        self.index.down = True
        with self.assertRaises(IndexUnavailable):
            self.tasks().overview()
        self.assertEqual(self.reads(), [0, 0, 0])

    def test_a_kind_the_service_does_not_report_is_unavailable(self):
        self.tasks().overview()
        del self.index.counters["pulls"]
        self.now += 86_400
        self.assertEqual(self.tasks().overview()["cache"]["state"], "unavailable")
        self.assertEqual(self.reads(), [1, 1, 1])

    def test_a_failed_sponsorship_read_keeps_the_last_good_one_and_recovers(self):
        self.sponsorships.return_value = {"issues": [{"issue": 7, "status": "confirmed", "slots": {}}]}
        self.access.task_issues.return_value = [
            {
                "number": 7,
                "title": "T",
                "labels": [{"name": "concurro"}],
                "created_at": "2026-01-01Z",
                "updated_at": "2026-01-02Z",
            }
        ]
        self.tasks().overview()
        self.sponsorships.side_effect = CommandError(503, "sponsorship status unavailable")
        self.index.counters["sponsorships"] += 1
        self.tasks().overview()
        self.settle()
        self.now += 30
        view = self.tasks().overview()
        self.assertEqual(view["tasks"][0]["sponsorships"]["status"], "confirmed", "the last good projection")
        self.assertNotIn("sponsorshipStatus", view["targets"][0])
        self.assertTrue(view["cache"]["error"])
        self.sponsorships.side_effect = None
        self.now += 61
        self.tasks().overview()
        self.settle()
        self.assertIsNone(self.tasks().overview()["cache"]["error"], "recovered without any new revision")

    def runs_reader(self, listing, turns=()):
        self.access.transport.return_value = Mock(request=lambda method, path: listing(path))
        diagnostics = Mock(for_sessions=Mock(return_value=list(turns)))
        return lambda: Tasks(self.settings, self.access, USER, diagnostics, read_cache=self.cache, lists=self.index)

    def test_a_failed_runs_listing_says_so_and_is_retried(self):
        answers = iter([RuntimeError("down"), {"workflow_runs": [RUN]}, RuntimeError("down again")])

        def listing(path):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        reader = self.runs_reader(listing)
        first = reader().target_runs("up/project")
        self.assertEqual((first["runs"], first["message"]), ([], "Actions runs could not be loaded from GitHub."))
        self.assertEqual(reader().target_runs("up/project")["runs"], [], "not asked again within the retry delay")
        self.now += 61
        again = reader().target_runs("up/project")
        self.assertEqual(([run["id"] for run in again["runs"]], again["message"]), ([77], None))
        self.index.counters["runs"] += 1
        reader().target_runs("up/project")
        with self.assertRaises(RuntimeError):
            self.settle()
        kept = reader().target_runs("up/project")
        self.assertEqual(([run["id"] for run in kept["runs"]], bool(kept["cache"]["error"])), ([77], True))

    def test_runs_the_service_holds_are_taken_from_it_and_attributed_here(self):
        reader = self.runs_reader(Mock(side_effect=AssertionError("GitHub is not read for a held list")))
        rerun = {**RUN, "id": 78, "run_attempt": 2}
        self.index.modes["runs"] = "rows"
        self.index.rows = [{"run": RUN, "first": RUN}, {"run": rerun, "first": None}]
        first = reader().target_runs("up/project")
        self.assertEqual(
            [(run["id"], run["attempt"]) for run in first["runs"]], [(77, 1), (78, 2)], "unattributed: latest"
        )
        self.assertEqual((first["message"], first["cache"]["state"], self.index.listed), (None, "current", 1))
        self.now += 86_400
        reader().target_runs("up/project")
        self.assertEqual(self.index.listed, 1, "an unchanged revision asks the service for nothing more")
        self.index.counters["runs"] += 1
        self.index.rows = self.index.rows[:1]
        reader().target_runs("up/project")
        self.settle()
        self.assertEqual(
            ([run["id"] for run in reader().target_runs("up/project")["runs"]], self.index.listed), ([77], 2)
        )
        self.index.down = True
        away = reader().target_runs("up/project")
        self.assertEqual(([run["id"] for run in away["runs"]], away["cache"]["state"]), ([77], "unavailable"))

    def test_a_list_the_service_has_not_built_shows_the_saved_copy_or_fails(self):
        reader = self.runs_reader(Mock(side_effect=AssertionError("GitHub is not read for a held list")))
        self.index.modes["runs"], self.index.built, self.index.state = "rows", None, "rebuilding"
        with self.assertRaises(IndexUnavailable):
            reader().target_runs("up/project")
        self.index.built, self.index.state, self.index.rows = (
            "2026-11-14T10:00:00+00:00",
            "current",
            [{"run": RUN, "first": RUN}],
        )
        reader().target_runs("up/project")
        self.index.built, self.index.state = None, "failed"
        view = reader().target_runs("up/project")
        self.assertEqual(([run["id"] for run in view["runs"]], view["cache"]["state"]), ([77], "failed"))
        self.assertIn("could not build", view["cache"]["error"])
        self.assertEqual(self.index.listed, 1)

    def test_copies_saved_by_the_previous_release_still_serve(self):
        """Unchanged lists keep their saved copies; the reshaped runs list falls back to the old one."""
        scope = self.tasks().list_scope
        attributed = {"runs": [{"id": 70}], "message": None}
        self.cache.read(scope, "up/project", LISTING + "issues", lambda: {"items": []}, revision="x:1")
        self.cache.read(scope, "up/project", LISTING + "pulls", lambda: {"items": []}, revision="x:1")
        self.cache.read(scope, "up/project", LISTING + f"sponsorships:{USER.id}", lambda: {"items": []}, revision="x:1")
        self.cache.read(scope, "up/project", LISTING + "runs", lambda: {"items": attributed}, revision="x:1")
        self.index.down = True
        self.assertEqual(self.tasks().overview()["cache"]["state"], "unavailable")
        reader = self.runs_reader(Mock(side_effect=AssertionError("nothing is read while the index is away")))
        view = reader().target_runs("up/project")
        self.assertEqual((view["runs"], view["cache"]["state"]), ([{"id": 70}], "unavailable"))
        self.assertEqual(self.reads(), [0, 0, 0])

    def test_no_usable_cache_never_bypasses_an_unavailable_index(self):
        broken = Mock()
        broken.read.side_effect = sqlite3.OperationalError("disk I/O error")
        self.index.down = True
        for cache in (None, broken):
            with self.assertRaises(IndexUnavailable):
                Tasks(
                    self.settings, self.access, USER, read_cache=cache, sponsorships=self.sponsorships, lists=self.index
                ).overview()
        self.assertEqual(self.reads(), [0, 0, 0])
        self.index.down = False
        Tasks(
            self.settings, self.access, USER, read_cache=None, sponsorships=self.sponsorships, lists=self.index
        ).overview()
        self.assertEqual(self.reads(), [1, 1, 1], "with the index answering, GitHub is read directly")

    def test_entries_the_backoffice_cannot_use_are_unavailable(self):
        self.tasks().overview()
        good = self.index.list_revisions(["up/project"])["up/project"]["pulls"]
        for entry in (
            {},
            {**good, "mode": "rows"},
            {**good, "state": "rebuilding"},
            {**good, "built": 5},
            {**good, "revision": 7},
            {k: v for k, v in good.items() if k != "verified"},
            "current",
        ):
            self.index.list_revisions = lambda repositories, entry=entry: {
                "up/project": {
                    kind: entry if kind == "pulls" else good for kind in ("issues", "sponsorships", "pulls", "runs")
                }
            }
            self.now += 86_400
            self.assertEqual(self.tasks().overview()["cache"]["state"], "unavailable", entry)
        self.assertEqual(self.reads(), [1, 1, 1])

    def test_without_the_index_lists_age_as_before(self):
        self.assertNotIn("state", self.tasks(lists=None).overview()["cache"])
        self.now += 61
        self.assertTrue(self.tasks(lists=None).overview()["cache"]["refreshing"])
        self.assertEqual(self.index.asked, 0)


class EndpointTests(unittest.TestCase):
    def client(self, index, cache, writable=True, token=TOKEN):
        sessions = Sessions()
        sid = sessions.create("user-token", USER, 60)
        access = Mock()
        access.writable.return_value = writable
        access.repository.return_value = {"full_name": "up/project", "description": ""}
        access.task_issues.return_value = []
        access.pull_requests.return_value = []
        access.config.return_value = ({}, "main")
        index.lists_token = token
        index.sponsorships = Mock(return_value={"issues": []})
        self.enterContext(patch.object(app_module, "Access", return_value=access))
        self.enterContext(patch.object(app_module, "ReadCache", return_value=cache))
        self.vault = Mock()
        return TestClient(
            app_module.create_app(
                settings(read_cache_dir=Path("/unused"), lists_token=token),
                oauth=FakeOAuth(),  # type: ignore[arg-type]
                vault=self.vault,
                sessions=sessions,
                commands=index,  # type: ignore[arg-type]
            ),
            cookies={app_module.SESSION_COOKIE: sid},
        )

    def test_refresh_goes_through_the_service_for_writers_only(self):
        index, cache, headers = Index(("up/project", "owner/project")), Mock(), {app_module.CSRF_HEADER: "1"}
        client = self.client(index, cache)
        self.assertEqual(client.post("/api/targets/up/project/refresh").status_code, 403, "a write needs its header")
        self.assertEqual(client.post("/api/targets/up/project/refresh", headers=headers).json(), {"refreshed": True})
        self.assertEqual(index.refreshed, ["up/project", "owner/project"])
        index.down = True
        response = client.post("/api/targets/up/project/refresh", headers=headers)
        self.assertEqual(response.status_code, 503, "an unavailable index is never an authentication failure")
        reader = self.client(Index(), cache, writable=False)
        self.assertEqual(reader.post("/api/targets/up/project/refresh", headers=headers).status_code, 404)

    def test_without_the_index_refresh_outdates_the_saved_lists(self):
        index, cache = Index(), Mock()
        client = self.client(index, cache, token=None)
        response = client.post("/api/targets/up/project/refresh", headers={app_module.CSRF_HEADER: "1"})
        self.assertEqual(response.json(), {"refreshed": True})
        cache.outdate.assert_called_once_with(LISTING, ("up/project", "owner/project"))
        self.assertEqual(index.refreshed, [])

    def test_a_cold_list_with_the_index_away_is_a_503(self):
        index = Index()
        index.down = True
        cache = Mock()
        cache.read.side_effect = LookupError("nothing saved")
        response = self.client(index, cache).get("/api/overview")
        self.assertEqual(response.status_code, 503)
        self.assertIn("list index", response.json()["detail"])

    def test_credential_writes_outdate_saved_sponsorship_lists(self):
        cache, headers = Mock(), {app_module.CSRF_HEADER: "1"}
        client = self.client(Index(), cache)
        self.vault.put.return_value = self.vault.revoke.return_value = {"id": "openai-api-key"}
        client.put("/api/me/credentials/openai-api-key", json={"secret": "synthetic"}, headers=headers)
        client.delete("/api/me/credentials/openai-api-key", headers=headers)
        self.assertEqual([call.args for call in cache.outdate.call_args_list], [(LISTING + "sponsorships:",)] * 2)


if __name__ == "__main__":
    unittest.main()
