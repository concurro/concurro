"""A task view reads the engine's own checkpoint through the Reader App's transport, end to end."""

import sqlite3
import sys
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "service/tests")]

import fixtures  # noqa: E402 -- initializes the service and engine test paths

from support import setUpModule as setUpModule, tearDownModule as tearDownModule  # noqa: E402

import report_store  # noqa: E402
from app_fakes import App  # noqa: E402
from backoffice import github as github_module, tasks as tasks_module  # noqa: E402
from backoffice.auth import User  # noqa: E402
from backoffice.github import Access  # noqa: E402
from backoffice.read_cache import ReadCache  # noqa: E402
from backoffice.settings import Reader, Settings  # noqa: E402
from backoffice.tasks import TaskNotFound, Tasks  # noqa: E402
from checkpoints import MARKER, CheckpointError, Checkpoints  # noqa: E402
from service.identity import Identity  # noqa: E402
from service.targets import Target  # noqa: E402
from service.transport import StaticCredential  # noqa: E402

KEY = fixtures.KEY


class Wire(fixtures.Wire):
    """Answers some paths every time, for reads the engine's fake backend doesn't serve."""

    def __init__(self, backend):
        super().__init__(backend)
        self.fixed = {}
        self.paths = []
        self.reads: list[tuple[str, str]] = []
        self.hidden_from_user: set[str] = set()
        self.token = ""

    def connection(self, host, timeout):
        return Connection(self)

    def answer(self, method, path, data):
        self.paths.append(path)
        self.reads.append((self.token, path))
        if self.token == "Bearer user-token" and path in self.hidden_from_user:
            return fixtures.Reply(404, {"message": "Not Found"})
        if (method, path) in self.fixed:
            return fixtures.Reply(200, self.fixed[method, path])
        return super().answer(method, path, data)


class Connection(fixtures.WireConnection):
    """Records each request's token, so the wire can answer as GitHub would for it."""

    def request(self, method, target, body=None, headers=None):
        self.wire.token = (headers or {}).get("Authorization", "")
        super().request(method, target, body, headers)


class ReaderApp:
    """The Reader App's identity, with one fixed token for every repository."""

    def credential(self, repository, kind):
        assert kind == "read"
        return StaticCredential("reader-token")


class TaskViewTests(fixtures.ServiceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        for cache in (tasks_module.STATES, tasks_module.CONFIGS, github_module.REPOSITORIES, github_module.PERMISSIONS):
            cache.clear()
        self.enterContext(patch.object(tasks_module, "REPORTS", report_store.Cache()))
        self.wire = Wire(self.backend)
        repository = KEY[0]
        self.wire.fixed["GET", f"repos/{repository}"] = {"full_name": repository, "description": "fixture"}
        for login, permission in (("writer", "write"), ("reader", "read")):
            self.wire.fixed["GET", f"repos/{repository}/collaborators/{login}/permission"] = {"permission": permission}
        targets = (Target(repository, repository, "fork"),)
        self.settings = Settings(
            public_url="https://host.invalid/concurro",
            client_id="client",
            client_secret="secret",
            targets=targets,
            reader=Reader(1, Path("/unused"), targets),
        )

    def tasks(self, login="writer", read_cache=None):
        access = Access(ReaderApp(), "user-token", self.wire.connection)
        return Tasks(self.settings, access, User(1, login, login.title(), ""), read_cache=read_cache)

    def issue_listing(self):
        issue = deepcopy(self.fake.issue)
        issue.setdefault("created_at", "2026-11-01T00:00:00Z")
        issue.setdefault("updated_at", "2026-11-02T00:00:00Z")
        issue.setdefault("title", "Fixture task")
        return issue

    def test_task_view_projects_the_checkpoint(self):
        state, _ = self.tick()
        for _ in range(3):
            if state and state["stage"] not in ("intake", "plan", "plan_qc", "planned"):
                break
            state, _ = self.tick()
        self.assertIsNotNone(state)
        issue = self.issue_listing()
        self.wire.fixed["GET", f"repos/{KEY[0]}/issues/7"] = issue
        reader = self.tasks()
        view = reader.task_view(KEY[0], 7)
        self.assertEqual(reader.configs[KEY[0]][0]["repository"], KEY[0], "the target configuration was read")
        task = view["task"]
        self.assertEqual(task["stage"], state["stage"])
        self.assertEqual(task["issue"], 7)
        self.assertTrue(task["plans"], "the approved plan is projected")
        self.assertEqual(task["plans"][-1]["id"], state["plan_id"])
        self.assertEqual(view["target"]["mode"], "manual")
        self.assertTrue(view["target"]["writer"])
        self.assertFalse(self.tasks("reader").task_view(KEY[0], 7)["target"]["writer"], "a reader sees no commands")
        # An unchanged checkpoint comment serves the decoded state without reading artifacts again.
        self.wire.paths.clear()
        again = self.tasks().task_view(KEY[0], 7)
        self.assertEqual(again["task"]["plans"], task["plans"])
        self.assertFalse([path for path in self.wire.paths if "/artifacts" in path])

    def large_checkpoint(self):
        """Publish independent plan/review artifacts through the real storage writer."""
        state = {
            "version": 2,
            "issue": 7,
            "base": "a" * 40,
            "stage": "planned",
            "plan_id": "plan-test",
            "plan": {"summary": "A plan " * 1500, "steps": ["one"], "files": [], "tests": [], "questions": []},
            "reviews": {"reviewer": {"verdict": "approve", "summary": "Review " * 1500}},
            "reports": [],
            "calls": 5,
            "pr": 9,
        }
        cp = Checkpoints(self.fake, self.config, 7)
        cp.save(state)
        self.fake.comments = []
        self.fake.comment(MARKER + "\n" + "\n".join(cp.links()), self.config["bot_login"])
        self.wire.fixed["GET", f"repos/{KEY[0]}/issues/7"] = self.issue_listing()
        return cp, state

    def test_persistent_summary_and_sections_share_issue_and_comment_reads(self):
        """Restarted summaries and lazy sections reuse one persisted issue/checkpoint snapshot."""
        self.large_checkpoint()
        with tempfile.TemporaryDirectory() as root:
            cache = ReadCache(Path(root))
            try:
                expected = self.tasks(read_cache=cache).task_view(KEY[0], 7, "summary")
            finally:
                cache.close()
            tasks_module.STATES.clear()
            cache = ReadCache(Path(root))
            try:
                self.wire.paths.clear()
                again = self.tasks(read_cache=cache).task_view(KEY[0], 7, "summary")
                self.assertEqual(again["task"], expected["task"])
                self.assertFalse(again["cache"]["stale"])
                self.assertEqual(self.wire.paths, [], "a fresh summary needs no repeated GitHub reads")
                for section in ("plan", "design", "timeline"):
                    phase = self.tasks(read_cache=cache).task_view(KEY[0], 7, section)
                    self.assertEqual(phase["task"]["revision"], again["task"]["revision"])
                self.assertFalse([p for p in self.wire.paths if "/comments" in p or p.endswith("issues/7")])
                github_module.REPOSITORIES.clear()
                self.wire.hidden_from_user.add(f"repos/{KEY[0]}")
                with self.assertRaises(TaskNotFound):
                    self.tasks(read_cache=cache).task_view(KEY[0], 7, "summary")
            finally:
                cache.close()

    def test_phase_refreshes_summary_when_audit_advances_behind_old_comment(self):
        """Historical checkpoint pointers cannot label newer phase evidence with an older summary revision."""
        cp, state = self.large_checkpoint()
        with tempfile.TemporaryDirectory() as root:
            cache = ReadCache(Path(root))
            try:
                before = self.tasks(read_cache=cache).task_view(KEY[0], 7, "summary")
                state["plan_id"] = "new-plan"
                cp.save(state)
                # Leave the dashboard comment on the verified historical audit version.
                plan = self.tasks(read_cache=cache).task_view(KEY[0], 7, "plan")
                after = self.tasks(read_cache=cache).task_view(KEY[0], 7, "summary")
                self.assertNotEqual(before["task"]["revision"], plan["task"]["revision"])
                self.assertEqual(plan["task"]["revision"], after["task"]["revision"])
                self.assertEqual(after["task"]["currentPlan"]["id"], "new-plan")
            finally:
                cache.close()

    def test_old_phase_does_not_refresh_current_issue_snapshot(self):
        """A stale phase cache refreshes its evidence without rescanning issue metadata or comments."""
        cp, state = self.large_checkpoint()
        with tempfile.TemporaryDirectory() as root:
            cache = ReadCache(Path(root))
            try:
                self.tasks(read_cache=cache).task_view(KEY[0], 7, "plan")
                state["plan_id"] = "new-plan"
                cp.save(state)
                scope = cache.db.execute("SELECT scope FROM snapshots").fetchone()[0]
                reader = self.tasks(read_cache=cache)
                config = reader.task_config(reader.find_target(KEY[0])[0], KEY[0])
                cache.read(scope, KEY[0], 7, lambda: reader.snapshot(KEY[0], 7, config), refresh=True)
                self.wire.paths.clear()
                plan = reader.task_view(KEY[0], 7, "plan")
                self.assertEqual(plan["task"]["currentPlan"]["id"], "new-plan")
                self.assertFalse([p for p in self.wire.paths if "/comments" in p or p.endswith("issues/7")])
                self.assertEqual(plan["task"]["revision"], reader.task_view(KEY[0], 7, "summary")["task"]["revision"])
            finally:
                cache.close()

    def test_phase_mismatch_does_not_retry_a_failed_snapshot(self):
        """A cached phase mismatch does not bypass the shared snapshot's failure cooldown."""
        self.large_checkpoint()
        reader = self.tasks()
        config = reader.task_config(reader.find_target(KEY[0])[0], KEY[0])
        snapshot = reader.snapshot(KEY[0], 7, config)
        reader.state(KEY[0], 7, "plan", comments=snapshot["comments"], config=config)
        snapshot["state"]["_audit"] = {"digest": "newer"}
        reader.read_cache = Mock()
        reader.read_cache.read.return_value = (snapshot, {"error": "rate limited"})
        self.wire.paths.clear()
        with self.assertRaises(CheckpointError):
            reader.task_view(KEY[0], 7, "plan")
        self.assertEqual(self.wire.paths, [])
        self.assertEqual(reader.read_cache.read.call_count, 1)

    def test_unavailable_database_falls_back_to_direct_reads(self):
        """The optional display store cannot prevent an otherwise valid issue read."""
        self.large_checkpoint()
        cache = Mock()
        cache.read.side_effect = sqlite3.DatabaseError("broken read cache")
        task = self.tasks(read_cache=cache).task_view(KEY[0], 7, "summary")
        self.assertEqual(task["task"]["stage"], "planned")
        self.assertNotIn("cache", task)

    def test_permission_hint_is_reused_without_crossing_users(self):
        """Polling the summary does not repeat UI permission reads or share writer status with a reader."""
        self.assertTrue(self.tasks().access.writable(KEY[0], "writer"))
        self.wire.paths.clear()
        self.assertTrue(self.tasks().access.writable(KEY[0], "writer"))
        self.assertEqual(self.wire.paths, [])
        self.assertFalse(self.tasks("reader").access.writable(KEY[0], "reader"))

    def test_summary_skips_hidden_evidence_and_pull_reads(self):
        """Initial display verifies only the audit, even when large phase bodies exist."""
        cp, state = self.large_checkpoint()
        with patch.object(Tasks, "pull", side_effect=AssertionError("eager PR read")):
            view = self.tasks().task_view(KEY[0], 7, "summary")
        task = view["task"]
        self.assertEqual(task["stage"], "planned")
        self.assertEqual(task["budgets"]["modelCalls"]["used"], 5)
        self.assertEqual(task["currentPlan"]["id"], state["plan_id"])
        self.assertTrue(task["phaseRecords"][1])
        self.assertTrue(task["phaseRecords"][4])
        self.assertEqual(task["pr"]["ci"], "unknown")
        self.assertIsNone(task["pr"]["additions"])
        self.assertNotIn("body", task)
        for field in ("plans", "rounds", "findings", "designReview", "timeline"):
            self.assertEqual(task[field], [])
        artifacts = [path for path in self.wire.paths if "/artifacts" in path]
        self.assertEqual(artifacts, [f"repos/{KEY[0]}/issues/7/artifacts/{cp.audit['number']}?per_page=1&page=1"])

    def test_phase_reads_only_its_reports_and_does_not_poison_full_state(self):
        """Section caches never substitute a partial state for controller/legacy reads."""
        _, state = self.large_checkpoint()
        self.tasks().task_view(KEY[0], 7, "summary")
        self.wire.paths.clear()
        plan = self.tasks().task_view(KEY[0], 7, "plan")["task"]
        self.assertIn(state["plan"]["summary"], plan["plans"][0]["body"])
        self.assertEqual(plan["rounds"], [])
        plan_reads = [p for p in self.wire.paths if "/artifacts/" in p and "?" not in p]
        self.assertEqual(len(plan_reads), 1)
        self.wire.paths.clear()
        review = self.tasks().task_view(KEY[0], 7, "review")["task"]
        self.assertEqual(review["rounds"][0]["votes"][0]["verdict"], "approve")
        review_reads = [p for p in self.wire.paths if "/artifacts/" in p and "?" not in p]
        self.assertEqual(len(review_reads), 1)
        self.assertNotEqual(plan_reads, review_reads)
        self.assertEqual(plan["revision"], review["revision"])
        self.wire.paths.clear()
        design = self.tasks().task_view(KEY[0], 7, "design")["task"]
        self.assertEqual(design["plans"][0]["id"], state["plan_id"])
        self.assertEqual(design["plans"][0]["body"], "")
        self.assertFalse([p for p in self.wire.paths if "/artifacts/" in p and "?" not in p])
        self.assertEqual(self.tasks().state(KEY[0], 7), state)

    def test_unselected_corruption_is_isolated_but_selected_evidence_is_verified(self):
        """A damaged hidden report does not block the summary or get trusted by its phase."""
        cp, _ = self.large_checkpoint()
        numbers = [
            n
            for n, item in self.fake.artifacts[7].items()
            if item["artifact"]["name"].startswith("Concurro review data")
        ]
        for number in numbers:
            self.fake.artifacts[7][number]["artifact"]["creator"] = {"login": "someone-else"}
        with patch.object(tasks_module, "REPORTS", report_store.Cache()):
            self.tasks().task_view(KEY[0], 7, "summary")
            with self.assertRaises(CheckpointError):
                self.tasks().task_view(KEY[0], 7, "plan")
        tasks_module.STATES.clear()
        self.fake.artifacts[7][cp.audit["number"]]["artifact"]["creator"] = {"login": "someone-else"}
        with self.assertRaises(CheckpointError):
            self.tasks().task_view(KEY[0], 7, "summary")

    def test_pull_is_independent_and_inventory_reads_are_not_needed_for_display(self):
        """PR enrichment is explicit and display never expands changed-file inventories."""
        self.large_checkpoint()
        with patch("checkpoints.file_inventory.restore", side_effect=AssertionError("inventory read")):
            for section in tasks_module.SECTION_FIELDS:
                with patch.object(Tasks, "pull", return_value={"number": 9, "ci": "passing"}) as pull:
                    view = self.tasks().task_view(KEY[0], 7, section)
                    self.assertEqual(pull.call_count, int(section == "pull"))
                    if section == "pull":
                        self.assertEqual(view["task"]["pr"]["ci"], "passing")

    def test_sections_with_identical_evidence_reuse_decoded_state(self):
        """Equivalent display projections share an audit read without affecting full reads."""
        self.large_checkpoint()
        for first, second in (("summary", "pull"), ("summary", "implement"), ("review", "handoff")):
            with self.subTest(first=first, second=second):
                expected = self.tasks().state(KEY[0], 7, first)
                self.wire.paths.clear()
                self.assertEqual(self.tasks().state(KEY[0], 7, second), expected)
                self.assertFalse([p for p in self.wire.paths if "/artifacts" in p])

    def test_selected_restore_still_validates_unselected_slots(self):
        """Reference shape and overlapping paths remain validated before skipped reads."""
        store = Mock()
        ref = {"path": ["plan"], "digest": "a" * 64, "artifacts": [1], "select": [], "annotations": {}}
        with self.assertRaises(ValueError):
            report_store.restore({"version": 7, "plan": {}}, [ref], store, fields=set())
        with self.assertRaises(ValueError):
            report_store.restore({"version": 7, "plan": None}, [ref, ref], store, fields=set())
        store.get.assert_not_called()

    def test_target_lookup_does_not_read_unrelated_repositories(self):
        """Issue navigation checks only the selected target's repository visibility."""
        unrelated = Target("unrelated/up", "unrelated/fork", "fork")
        self.settings = replace(self.settings, targets=(unrelated, *self.settings.targets))
        self.tasks().find_target(KEY[0])
        self.assertFalse(any("unrelated" in path for path in self.wire.paths))

    def test_the_users_token_only_decides_visibility(self):
        """Reads use the Reader App, but a repository hidden from the user's own token hides its target."""
        reader = self.tasks()
        self.assertTrue(reader.target_record(*reader.find_target(KEY[0]))["writer"])
        users = {path for token, path in self.wire.reads if token == "Bearer user-token"}
        self.assertEqual(users, {f"repos/{KEY[0]}"})
        self.assertIn(("Bearer reader-token", f"repos/{KEY[0]}/collaborators/writer/permission"), self.wire.reads)
        github_module.REPOSITORIES.clear()
        self.wire.hidden_from_user.add(f"repos/{KEY[0]}")
        self.assertEqual(self.tasks().visible_targets(), [])
        with self.assertRaises(TaskNotFound):
            self.tasks().task_view(KEY[0], 7)

    def test_unknown_repositories_are_not_found(self):
        with self.assertRaises(TaskNotFound):
            self.tasks().task_view("someone/else", 7)

    def test_separate_installations_and_visibility_for_both_repositories(self):
        """Each organization supplies its own read token; either hidden repository hides the whole target."""
        target = Target("upstream/project", "fork/project", "both")
        settings = replace(self.settings, targets=(target,), reader=replace(self.settings.reader, targets=(target,)))
        app = App(fixtures.Clock())
        original = app.handle
        installations = {target.upstream: 11, target.fork: 22}
        tokens, reads, minted = {}, [], []
        hidden = set()
        wanted = {
            target.upstream: {
                "contents": "read",
                "pull_requests": "read",
                "checks": "read",
                "statuses": "read",
                "actions": "read",
                "issues": "read",
            },
            target.fork: {"contents": "read", "actions": "read", "issues": "read"},
        }

        def answer(method, path, headers, body):
            """Serve separate installations and reject data reads with the wrong repository's token."""
            token = headers["Authorization"].removeprefix("Bearer ")
            if path == "/installation/repositories?per_page=100":
                return 200, {"total_count": 1, "repositories": [{"full_name": tokens[token]}]}
            for repository, installation in installations.items():
                if path == f"/repos/{repository}/installation":
                    self.assertEqual(token, "test-jwt")
                    return 200, {
                        "id": installation,
                        "repository_selection": "selected",
                        "account": {"login": repository.split("/")[0]},
                    }
                if method == "POST" and path == f"/app/installations/{installation}/access_tokens":
                    self.assertEqual(token, "test-jwt")
                    status, result = original(method, "/app/installations/5/access_tokens", headers, body)
                    self.assertEqual(app.minted[-1][1:], (["project"], wanted[repository]))
                    tokens[result["token"]] = repository
                    minted.append(installation)
                    return status, result
                if path == f"/repos/{repository}" and token == "user-token":
                    reads.append((token, path))
                    return (404, {}) if repository in hidden else (200, {"full_name": repository})
                if path.startswith(f"/repos/{repository}/"):
                    self.assertEqual(tokens.get(token), repository)
                    reads.append((token, path))
                    if path.endswith("/permission"):
                        return 200, {"permission": "read"}
                    return 200, []
            return original(method, path, headers, body)

        identity = Identity(settings.reader, app.connection, app.clock)
        with (
            patch.object(app, "handle", side_effect=answer),
            patch.object(identity, "app_jwt", return_value="test-jwt"),
        ):
            access = Access(identity, "user-token", app.connection)
            reader = Tasks(settings, access, User(1, "reader", "Reader", ""))
            self.assertEqual(reader.find_target(target.upstream)[0], target)
            self.assertEqual({path for _, path in reads}, {f"/repos/{r}" for r in target.repositories})
            self.assertEqual(minted, [], "visibility alone needs no installation token")
            for repository in target.repositories:
                self.assertEqual(access.task_issues(repository), [])
            self.assertFalse(access.writable(target.fork, "reader"))
            self.assertEqual(minted, [11, 22], "the fork's writer check reuses its read token")
            for repository in target.repositories:
                with self.subTest(hidden=repository):
                    hidden.clear()
                    hidden.add(repository)
                    github_module.REPOSITORIES.clear()
                    view = Tasks(settings, Access(identity, "user-token", app.connection), reader.user)
                    self.assertEqual(view.visible_targets(), [])
                    for destination in target.repositories:
                        with self.assertRaises(TaskNotFound):
                            view.task_view(destination, 7)
            self.assertEqual(minted, [11, 22])


if __name__ == "__main__":
    unittest.main()
