"""Backoffice API: sign-in, request protection, task projection, diagnostics, commands and credentials."""

import base64
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backoffice import app as app_module, github as github_module, tasks as tasks_module  # noqa: E402
from backoffice.auth import AuthError, Sessions, User, challenge  # noqa: E402
from backoffice.commands import CommandError, ServiceCommands  # noqa: E402
from backoffice.credentials import Vault  # noqa: E402
from backoffice.settings import Settings  # noqa: E402
from backoffice.turns import Diagnostics, normalize  # noqa: E402
from github import NotFound, Transient  # noqa: E402
from service import diagnostics  # noqa: E402
from service.targets import Target  # noqa: E402

USER = User(42, "octo", "Octo Cat", "https://avatars.invalid/42")
TURN_ID = "12345678-1234-1234-1234-123456789abc"


def settings(public_url="https://host.invalid/concurro", **extra) -> Settings:
    return Settings(
        **{
            "public_url": public_url,
            "client_id": "client",
            "client_secret": "secret",
            "targets": (Target("up/project", "owner/project", "fork"),),
            **extra,
        }
    )


class FakeOAuth:
    def __init__(self):
        self.exchanged = []

    def exchange(self, code, callback, verifier):
        if code != "good":
            raise AuthError("GitHub refused the sign-in; please sign in again")
        self.exchanged.append((code, callback, verifier))
        return "user-token", 3600.0

    def user(self, token):
        return USER


def archive(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class SignInTests(unittest.TestCase):
    def setUp(self):
        self.oauth = FakeOAuth()
        self.client = TestClient(
            # The edge strips `/concurro`, so cookies scoped to it can't be exercised here; use the root.
            app_module.create_app(settings("https://host.invalid"), oauth=self.oauth, vault=Vault(None)),  # type: ignore[arg-type]
            base_url="https://host.invalid",
        )

    def start(self, next_path="/tasks"):
        response = self.client.get("/auth/login", params={"next": next_path}, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlsplit(response.headers["location"]).query)
        self.assertEqual(query["redirect_uri"], ["https://host.invalid/auth/callback"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        return query["state"][0], query["code_challenge"][0]

    def test_sign_in_binds_state_and_pkce_to_the_browser(self):
        state, code_challenge = self.start()
        response = self.client.get("/auth/callback", params={"code": "good", "state": state}, follow_redirects=False)
        self.assertEqual(response.headers["location"], "/tasks")
        verifier = self.oauth.exchanged[0][2]
        self.assertEqual(challenge(verifier), code_challenge)
        self.assertEqual(self.client.get("/api/me").json()["login"], "octo")
        # The state was consumed.
        again = self.client.get("/auth/callback", params={"code": "good", "state": state}, follow_redirects=False)
        self.assertIn("/signin?error=", again.headers["location"])

    def test_a_callback_from_another_browser_is_refused(self):
        state, _ = self.start()
        self.client.cookies.clear()
        response = self.client.get("/auth/callback", params={"code": "good", "state": state}, follow_redirects=False)
        self.assertIn("/signin?error=", response.headers["location"])
        self.assertEqual(self.oauth.exchanged, [])
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_open_redirects_are_dropped(self):
        state, _ = self.start("//evil.invalid")
        response = self.client.get("/auth/callback", params={"code": "good", "state": state}, follow_redirects=False)
        self.assertEqual(response.headers["location"], "/")

    def test_writes_need_the_request_header(self):
        state, _ = self.start()
        self.client.get("/auth/callback", params={"code": "good", "state": state}, follow_redirects=False)
        refused = self.client.post("/api/tasks/up/project/7/commands", json={"command": "stop"})
        self.assertEqual(refused.status_code, 403)
        unknown = self.client.post(
            "/api/tasks/up/project/7/commands", json={"command": "merge"}, headers={app_module.CSRF_HEADER: "1"}
        )
        self.assertEqual(unknown.status_code, 422)


class SettingsTests(unittest.TestCase):
    def test_paths_derive_from_the_public_url(self):
        configured = settings()
        self.assertEqual(configured.base_path, "/concurro")
        self.assertEqual(configured.callback_url, "https://host.invalid/concurro/auth/callback")
        self.assertTrue(configured.secure)


class SessionTests(unittest.TestCase):
    def test_sessions_expire(self):
        now = [0.0]
        sessions = Sessions(clock=lambda: now[0])
        sid = sessions.create("token", USER, 10)
        self.assertIsNotNone(sessions.get(sid))
        now[0] = 11
        self.assertIsNone(sessions.get(sid))


class CommandTests(unittest.TestCase):
    def test_arguments_resolve_to_commands_the_engine_accepts(self):
        from delegation import command_text

        approve = tasks_module.command_argument("approve", None, lambda: {"plan_id": "a" * 64})
        self.assertEqual(approve, "a" * 64)
        self.assertEqual(command_text("approve", approve), "/concurro approve " + "a" * 64)
        self.assertIsNone(tasks_module.command_argument("stop", None, lambda: None))
        self.assertEqual(
            command_text("retry", tasks_module.command_argument("retry", "2", lambda: None)),
            "/concurro retry --plan-revisions 2",
        )
        with self.assertRaises(tasks_module.TaskNotFound):
            tasks_module.command_argument("approve", None, lambda: {})

    def test_commands_go_to_the_service_with_the_sign_in_token(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.path, request.headers["authorization"], json.loads(request.content)))
            if json.loads(request.content)["command"] == "stop":
                return httpx.Response(
                    201, json={"posted": "/concurro stop", "actor": "octo", "url": "https://github.invalid/c/1"}
                )
            return httpx.Response(403, json={"error": "@octo does not have write access to up/project"})

        service = ServiceCommands("http://service.invalid", httpx.Client(transport=httpx.MockTransport(handler)))
        sessions = Sessions()
        sid = sessions.create("user-token", USER, 60)
        client = TestClient(
            app_module.create_app(
                settings(), oauth=FakeOAuth(), vault=Vault(None), sessions=sessions, commands=service
            ),  # type: ignore[arg-type]
            cookies={app_module.SESSION_COOKIE: sid},
        )
        headers = {app_module.CSRF_HEADER: "1"}
        posted = client.post("/api/tasks/up/project/7/commands", json={"command": "stop"}, headers=headers)
        self.assertEqual((posted.status_code, posted.json()["posted"]), (200, "/concurro stop"))
        self.assertEqual(seen[0][:2], ("/actions", "Bearer user-token"))
        self.assertEqual(seen[0][2], {"repository": "up/project", "issue": 7, "command": "stop", "argument": None})
        invalid = client.post(
            "/api/tasks/up/project/7/commands", json={"command": "retry", "argument": "x"}, headers=headers
        )
        self.assertEqual(invalid.status_code, 422)
        refused = client.post("/api/tasks/up/project/7/commands", json={"command": "resume"}, headers=headers)
        self.assertEqual(
            (refused.status_code, refused.json()["detail"]), (403, "@octo does not have write access to up/project")
        )

    def test_only_failures_before_delivery_say_nothing_was_posted(self):
        for error, status, text in (
            (httpx.ConnectError("refused"), 503, "nothing was posted"),
            (httpx.ReadTimeout("slow"), 502, "may or may not have been posted"),
            (httpx.RemoteProtocolError("closed"), 502, "may or may not have been posted"),
        ):

            def handler(request: httpx.Request, error=error) -> httpx.Response:
                raise error

            service = ServiceCommands("http://service.invalid", httpx.Client(transport=httpx.MockTransport(handler)))
            with self.subTest(type(error).__name__), self.assertRaises(CommandError) as failed:
                service.send("user-token", "up/project", 7, "stop", None)
            self.assertEqual(failed.exception.status, status)
            self.assertIn(text, failed.exception.reason)


class CredentialTests(unittest.TestCase):
    def test_routes_map_between_the_ui_and_the_vault(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path, request.headers["authorization"]))
            if request.method == "GET":
                entry = {
                    "route": "anthropic-api-key",
                    "status": "valid",
                    "fingerprint": "abcd",
                    "version": 2,
                    "updated": "t",
                }
                return httpx.Response(200, json=[entry])
            return httpx.Response(422, json={"error": "provider rejected the secret"})

        vault = Vault("http://vault.invalid", httpx.Client(transport=httpx.MockTransport(handler)))
        routes = {r["id"]: r for r in vault.routes("user-token")}
        self.assertEqual(routes["anthropic_api"]["status"], "valid")
        self.assertEqual(routes["openai_api"]["status"], "missing")
        self.assertEqual(routes["claude_subscription"]["status"], "missing")
        with self.assertRaises(Exception) as raised:
            vault.put("user-token", "openai_api", "sk-test")
        self.assertEqual(getattr(raised.exception, "status", None), 422)
        self.assertEqual(seen[-1][:2], ("PUT", "/credentials/openai-api-key"))
        self.assertTrue(all(auth == "Bearer user-token" for _, _, auth in seen))


class DiagnosticsTests(unittest.TestCase):
    def test_turns_and_transcripts_read_back(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = diagnostics.Store(root, clock=lambda: 1000.0)
        self.addCleanup(store.close)
        turn = {
            "request": {"stage": "develop", "provider": "claude", "model": "claude-opus", "effort": "high"},
            "spec": {},
            "feedback": {},
        }
        store.submitted(TURN_ID, turn, {"repository": "up/project", "issue": 7})
        store.observed(TURN_ID, "done", {"created": 1000.0, "started": 1002.0, "finished": 1010.0}, {"s-1"})
        lines = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Reading"}]}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"p": 1}}]},
            },
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        ]
        data = archive({"claude/projects/x/s.jsonl": "\n".join(json.dumps(line) for line in lines)})
        workspace = {"edits": [{"path": "a.py", "content": "print(1)\n"}], "removed": ["b.py"], "skipped": []}
        store.finished(
            TURN_ID,
            {
                "outcome": "ok",
                "response": {"summary": "done"},
                "retained": base64.b64encode(data).decode(),
                "workspace": workspace,
            },
        )
        [result] = Diagnostics(root).turns("up/project", 7)
        self.assertEqual(
            (result["n"], result["stage"], result["role"], result["outcome"]), (1, "implement", "implementer", "ok")
        )
        self.assertEqual((result["queuedMs"], result["durationMs"]), (2000, 8000))
        self.assertEqual([item["kind"] for item in result["transcript"]], ["assistant", "tool_call", "tool_result"])
        self.assertIn("=== a.py", result["diff"])
        self.assertIn("b.py (removed)", result["diff"])
        self.assertEqual(Diagnostics(root).turns("up/project", 8), [])
        self.assertEqual(Diagnostics(None).turns("up/project", 7), [])

    def test_codex_transcripts_normalize(self):
        events = [
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]},
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call", "name": "shell", "arguments": "{}", "call_id": "c1"},
            },
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1", "output": "done"}},
            {"type": "event_msg", "payload": {}},
        ]
        items = normalize(archive({"codex/sessions/r.jsonl": "\n".join(json.dumps(e) for e in events)}))
        self.assertEqual([i["kind"] for i in items], ["assistant", "tool_call", "tool_result"])
        self.assertEqual(normalize(b"not an archive")[0]["kind"], "assistant")


class RosterTests(unittest.TestCase):
    def test_named_and_legacy_configurations(self):
        named = {
            "models": {"opus": {"provider": "anthropic", "model": "claude-opus", "effort": "high"}},
            "agents": {
                "triage": "opus",
                "planner": "opus",
                "plan_reviewers": ["opus"],
                "implementer": "opus",
                "pr_reviewers": ["opus"],
            },
        }
        roster, profiles = tasks_module.roster_and_profiles(named)
        self.assertEqual(len(roster), 5)
        self.assertEqual(
            profiles, [{"name": "opus", "provider": "anthropic", "model": "claude-opus", "effort": "high"}]
        )
        legacy = {
            "planner": "claude",
            "implementer": "codex",
            "qc": "codex",
            "reviewers": ["claude", "codex"],
            "models": {"claude": "claude-opus", "codex": "gpt-sol"},
        }
        roster, profiles = tasks_module.roster_and_profiles(legacy)
        self.assertEqual(
            [r["role"] for r in roster], ["planner", "plan_reviewer", "implementer", "pr_reviewer", "pr_reviewer"]
        )
        self.assertEqual({p["name"]: p["provider"] for p in profiles}, {"claude": "anthropic", "codex": "openai"})


class PullListingTests(unittest.TestCase):
    def test_task_branches_map_to_issues_and_open_pulls_win(self):
        def pull(number, ref, open=True, draft=False, runs=None, lines=(None, None)):
            return {
                "number": number,
                "title": f"PR {number}",
                "open": open,
                "draft": draft,
                "ref": ref,
                "sha": "a" * 40,
                "additions": lines[0],
                "deletions": lines[1],
                "runs": runs,
            }

        green = [{"status": "completed", "conclusion": "success"}]
        listing = [
            pull(42, "concurro/issue-35-b716ab5519d2", draft=True, runs=[], lines=(3, 1)),
            pull(41, "concurro/issue-36-22ba309e9891", runs=green, lines=(120, 7)),
            pull(37, "chore/concurro-rpc-pilot-ci", open=False),
            pull(20, "concurro/issue-36-0123456789ab", open=False),
            pull(19, "concurro/issue-12-0123456789ab", open=False),
        ]

        class Access:
            def pull_requests(self, repository):
                assert repository == "up/project"
                return listing

        reader = tasks_module.Tasks(settings(), Access(), USER)  # type: ignore[arg-type]
        pulls = reader.pulls_by_issue("up/project")
        self.assertEqual(sorted(pulls), [12, 35, 36])
        self.assertEqual((pulls[35]["number"], pulls[35]["ready"], pulls[35]["ci"]), (42, False, "none"))
        self.assertEqual(
            (pulls[36]["number"], pulls[36]["ready"], pulls[36]["ci"], pulls[36]["additions"], pulls[36]["deletions"]),
            (41, True, "passing", 120, 7),
        )
        self.assertEqual((pulls[12]["number"], pulls[12]["ready"], pulls[12]["additions"]), (19, False, None))
        self.assertNotIn("open", pulls[36])

    def test_one_query_lists_line_counts_and_check_runs_with_a_rest_fallback(self):
        """List metadata survives GraphQL failures; hidden repositories stay hidden."""
        node = {
            "number": 41,
            "title": "PR 41",
            "state": "OPEN",
            "isDraft": False,
            "headRefName": "concurro/issue-36-22ba309e9891",
            "headRefOid": "b" * 40,
            "additions": 120,
            "deletions": 7,
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "statusCheckRollup": {
                                "contexts": {
                                    "nodes": [
                                        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
                                        {"__typename": "StatusContext"},
                                    ]
                                }
                            }
                        }
                    }
                ]
            },
        }
        queries = []

        class Engine:
            def __init__(self, result):
                self.result = result

            def api(self, path, method="GET", data=None):
                queries.append((path, method, data["variables"]))
                if isinstance(self.result, Exception):
                    raise self.result
                return self.result

        access = github_module.UserAccess("token")
        access.engine = lambda repository: Engine({"data": {"repository": {"pullRequests": {"nodes": [node]}}}})  # type: ignore[method-assign]
        [pull] = access.pull_requests("up/project")
        self.assertEqual(queries, [("graphql", "POST", {"owner": "up", "name": "project"})])
        self.assertEqual(
            (pull["open"], pull["ref"], pull["additions"], pull["deletions"]),
            (True, "concurro/issue-36-22ba309e9891", 120, 7),
        )
        self.assertEqual(pull["runs"], [{"status": "completed", "conclusion": "failure"}])
        self.assertEqual(tasks_module.ci(pull["runs"]), "failing")

        rest = [{"number": 41, "title": "PR 41", "state": "open", "draft": True, "head": {"ref": "x", "sha": "c" * 40}}]
        access.get = lambda repository, path: rest  # type: ignore[method-assign]
        for failure in (
            RuntimeError("HTTP 502"),
            Transient("throttled", 60),
            {"errors": [{"message": "refused"}]},
            {"data": {"repository": None}},
        ):
            with self.subTest(failure=failure):
                access.engine = lambda repository: Engine(failure)  # type: ignore[method-assign]
                [pull] = access.pull_requests("up/project")
                self.assertEqual((pull["draft"], pull["additions"], pull["runs"]), (True, None, None))

        access.engine = lambda repository: Engine(NotFound("hidden"))  # type: ignore[method-assign]
        with self.assertRaises(NotFound):
            access.pull_requests("up/project")


class TaskConfigTests(unittest.TestCase):
    def test_tasks_run_with_the_workers_merged_configuration(self):
        target = Target("up/project", "owner/project", "upstream")
        reader = tasks_module.Tasks(settings(targets=(target,)), None, USER)  # type: ignore[arg-type]
        reader.configs["owner/project"] = ({"repository": "owner/project", "bot_login": "bot"}, "main")
        config = reader.task_config(target, "up/project")
        self.assertEqual(
            (config["repository"], config["upstream"], config["fork"]), ("up/project", "up/project", "owner/project")
        )
        self.assertEqual(reader.publication_repository(target), "up/project")

    def test_engine_refusals_are_readable(self):
        from checkpoints import CheckpointError

        class Failing(tasks_module.Tasks):
            def overview(self):
                raise CheckpointError("checkpoint_format")

        sessions = Sessions()
        sid = sessions.create("token", USER, 60)
        original = app_module.Tasks
        app_module.Tasks = Failing  # type: ignore[misc]
        self.addCleanup(setattr, app_module, "Tasks", original)
        client = TestClient(app_module.create_app(settings(), oauth=FakeOAuth(), vault=Vault(None), sessions=sessions))  # type: ignore[arg-type]
        response = client.get("/api/overview", cookies={app_module.SESSION_COOKIE: sid})
        self.assertEqual(response.status_code, 502)
        self.assertIn("checkpoint_format", response.json()["detail"])


class ProjectionTests(unittest.TestCase):
    ISSUE = {
        "number": 7,
        "title": "Fix it",
        "user": {"login": "author"},
        "labels": [{"name": "concurro"}, {"name": "concurro:stage:review"}, {"name": "bug"}],
        "created_at": "2026-11-01T00:00:00Z",
        "updated_at": "2026-11-02T00:00:00Z",
        "closed_at": None,
    }

    def test_summary_uses_the_stage_label(self):
        task = tasks_module.summary("up/project", self.ISSUE, "manual")
        self.assertEqual((task["stage"], task["labels"]), ("review", ["bug"]))

    def test_plan_versions_are_unique_and_keep_the_current_plan_in_full(self):
        """Repeated review generations cannot duplicate a plan's UI identity."""
        for history_ids, expected in (
            (["a", "b", "c"], ["a", "b", "c"]),
            (["a", "a", "b"], ["a", "b", "c"]),
            (["c", "b", "c"], ["b", "c"]),
            ([], ["c"]),
        ):
            with self.subTest(history=history_ids):
                state = {
                    "stage": "escalated",
                    "plan_id": "c",
                    "plan": {"headline": "Current plan", "summary": "Full summary", "steps": ["Do the work"]},
                    "plan_revisions": 2,
                    "design_history": [{"plan": plan_id, "summary": "Historical summary"} for plan_id in history_ids],
                }
                task = tasks_module.project(state, {}, "up/project", self.ISSUE)
                plans = task["plans"]
                self.assertEqual([plan["id"] for plan in plans], expected)
                self.assertEqual([plan["version"] for plan in plans], list(range(1, len(expected) + 1)))
                self.assertEqual(plans[-1]["summary"], "Current plan")
                self.assertEqual(plans[-1]["body"], "Full summary\n\n### Steps\n- Do the work")
                self.assertTrue(all(plan["body"] == "Historical summary" for plan in plans[:-1]))

    def test_checkpoint_state_projects_into_the_task(self):
        member = {"id": "m1", "model": "claude-opus", "effort": "high", "agent": {"profile": "opus-high"}}
        state = {
            "stage": "observed_merged",
            "mode": "autonomous",
            "plan_id": "p" * 64,
            "plan": {"summary": "Do it", "steps": ["one"], "headline": "Fix the bug"},
            "approval": {"kind": "consensus", "plan": "p" * 64},
            "design_generation": 1,
            "design_votes": {"m1": {"identity": member, "report": {"decision": "approve"}}},
            "objections": [{"id": "O1-m1", "reviewer": "m1", "text": "edge case", "resolved": True}],
            "review_history": [{"claude": {"verdict": "changes_requested", "head": "a" * 40, "sequence": 1}}],
            "reviews": {"claude": {"verdict": "approve", "head": "b" * 40, "sequence": 2}},
            "findings": [
                {"id": "F1", "provider": "claude", "review": 1, "status": "fixed", "detail": "Off by one\nmore"}
            ],
            "calls": 9,
            "round": 1,
            "completion": {
                "kind": "observed_external",
                "merge_sha": "c" * 40,
                "merged_by": "human",
                "observed_stage": "review",
            },
            "reports": [{"step": "review", "provider": "claude", "outcome": "approve", "timing": {"finished": 1.8e9}}],
        }
        task = tasks_module.project(state, {"max_model_calls": 40}, "up/project", self.ISSUE)
        self.assertEqual((task["stage"], task["atStage"], task["mode"]), ("observed_merged", "review", "autonomous"))
        self.assertEqual(task["plans"][-1]["summary"], "Fix the bug")
        self.assertEqual(task["approvedPlanId"], "p" * 64)
        self.assertEqual(task["designReview"][0]["reviews"][0]["agent"], "opus-high")
        self.assertTrue(task["designReview"][0]["reviews"][0]["objections"][0]["resolved"])
        self.assertEqual([r["votes"][0]["verdict"] for r in task["rounds"]], ["changes_requested", "approve"])
        self.assertEqual(task["findings"][0]["title"], "Off by one")
        self.assertEqual(task["budgets"]["modelCalls"], {"used": 9, "max": 40})
        self.assertEqual(task["completion"]["handedOff"], False)
        self.assertEqual(len(task["timeline"]), 2)


if __name__ == "__main__":
    unittest.main()
