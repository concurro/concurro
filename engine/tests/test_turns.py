"""Turns: workspaces from snapshots, extraction, the agent container contract, executors and the runner loop."""

from copy import deepcopy
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from support import CONFIG, setUpModule as setUpModule, tearDownModule as tearDownModule  # unittest module fixtures
from runner_fixtures import (
    ExecutorFixture,
    GATEWAY,
    MODELS,
    AGENT,
    Workspace,
    FakeExecutor,
    FakeOperator,
    TASK,
    TURN,
    FakeRunner,
)
import agent_entry
from agents import Agents, GatewayUnavailable, Retained, RunnerExecutor
from credentials import Credential
from gitfixture import IDENTITY
from mirror import git_environment
from policy import Blocked, SessionFailure, UncertainOutcome
from relay import ExchangeError
import runner
import turns


class ExtractionTests(Workspace, unittest.TestCase):
    def test_changes_are_extracted_against_the_snapshot_head(self):
        root = self.prepare(candidate={"files": {"docs/guide.md": "Candidate\n"}})
        work = root / "work"
        self.assertEqual((work / "docs/guide.md").read_text(), "Candidate\n", "the head is the candidate")
        (work / "src/lib.rs").write_text("fn main() { run(); }\n")
        (work / "src/run.rs").write_text("pub fn run() {}\n")
        (work / "docs/guide.md").unlink()
        (work / "target").mkdir()
        (work / "target/debug.bin").write_bytes(b"\xff\x00")
        changes = turns.extract(work, root / "pristine.git", root)
        self.assertEqual(
            changes["edits"],
            [
                {"path": "src/lib.rs", "content": "fn main() { run(); }\n"},
                {"path": "src/run.rs", "content": "pub fn run() {}\n"},
            ],
        )
        self.assertEqual(changes["removed"], ["docs/guide.md"])

    def test_nothing_the_agent_wrote_under_git_takes_part(self):
        root = self.prepare()
        work, marker = root / "work", self.tmp / "ran"
        hook = self.tmp / "hook.sh"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        with (work / ".git/config").open("a") as config:
            config.write(
                f"[core]\n\tfsmonitor = {hook}\n\thooksPath = {self.tmp}\n"
                f'[filter "evil"]\n\tclean = {hook}\n\trequired = true\n'
            )
        (work / ".gitattributes").write_text("* filter=evil\n")
        (work / ".git/info").mkdir(exist_ok=True)
        (work / ".git/info/exclude").write_text("src/hidden.rs\n")
        (work / "src/hidden.rs").write_text("hidden\n")
        changes = turns.extract(work, root / "pristine.git", root)
        self.assertFalse(marker.exists(), "no program of the agent's ran")
        self.assertEqual([e["path"] for e in changes["edits"]], [".gitattributes", "src/hidden.rs"])

    def test_a_submodule_is_never_entered(self):
        root = self.prepare()
        work, pristine, marker = root / "work", root / "pristine.git", self.tmp / "ran"
        git = {**git_environment(), **IDENTITY, "GIT_DIR": str(pristine), "GIT_INDEX_FILE": str(root / "link.index")}

        def run(*args, env=git):
            return subprocess.run(args, check=True, capture_output=True, env=env, text=True).stdout.strip()

        run("git", "read-tree", "HEAD")
        run("git", "update-index", "--add", "--cacheinfo", f"160000,{self.base},vendor/sub")
        commit = run(
            "git",
            "-c",
            "user.useConfigOnly=true",
            "commit-tree",
            run("git", "write-tree"),
            "-p",
            "HEAD",
            "-m",
            "submodule",
        )
        run("git", "update-ref", "HEAD", commit)
        sub = work / "vendor/sub"
        sub.mkdir(parents=True)
        inner = {
            **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        run("git", "init", "-q", str(sub), env=inner)
        (sub / "file").write_text("x\n")
        run("git", "-C", str(sub), "add", "file", env=inner)
        run("git", "-C", str(sub), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x", env=inner)
        hook = self.tmp / "hook.sh"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
        hook.chmod(0o755)
        with (sub / ".git/config").open("a") as config:
            config.write(f'[filter "evil"]\n\tclean = {hook}\n')
        (sub / ".gitattributes").write_text("* filter=evil\n")
        # Same size, new mtime: Git must hash the file, through the filter.
        (sub / "file").write_text("y\n")
        os.utime(sub / "file", (1, 1))
        changes = turns.extract(work, pristine, root)
        self.assertFalse(marker.exists(), "nothing configured inside the submodule ran")
        self.assertEqual(changes["edits"], [])
        self.assertEqual(changes["removed"], [])

    def test_paths_the_engine_cannot_carry_are_refused_or_skipped(self):
        root = self.prepare()
        work = root / "work"
        nested = work / "vendor/clone"
        nested.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(nested)], check=True)
        (nested / "x").write_text("x")
        self.assertEqual(turns.extract(work, root / "pristine.git", root)["skipped"], ["vendor/clone/"])
        for name, make in (
            ("link", lambda p: p.symlink_to("/etc/passwd")),
            ("binary", lambda p: p.write_bytes(b"\xff\xfe")),
        ):
            path = work / ("src/" + name)
            make(path)
            with self.subTest(name=name), self.assertRaises(turns.TurnError):
                turns.extract(work, root / "pristine.git", root)
            path.unlink()


class ExecuteTests(Workspace, unittest.TestCase):
    def execute(self, request, retained=None, feedback_files=None):
        root = turns.directory(self.tmp / "turns")
        fake = lambda: [sys.executable, "-c", AGENT, str(root)]
        turn = {
            "request": request,
            "spec": self.spec,
            "retained": retained,
            "timeout": None if request["stage"] == "develop" else 1800,
            "feedback": feedback_files or {},
        }
        with patch.object(turns, "command", side_effect=fake), patch.dict(os.environ, {"GH_TOKEN": "owner-secret"}):
            result = turns.execute(turn, "sk-agw-test", self.mirror, root)
        return result, root

    def test_a_develop_turn_returns_its_workspace_and_transcript(self):
        session = {"scope": "c" * 64, "turn": 1, "role": "develop:codex:m"}
        result, root = self.execute({"stage": "develop", "session": session}, retained=None)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["response"], {"summary": "done", "checks": [], "design_issue": None})
        self.assertEqual(
            result["workspace"]["edits"],
            [{"path": "docs/guide.md", "content": "Guide\nEdited\n"}, {"path": "docs/new.md", "content": "New\n"}],
        )
        restored = self.tmp / "restored"
        turns.unpack(result["retained"], restored)
        self.assertEqual(json.loads((restored / "turn.json").read_text()), {"turn": 1})
        self.assertEqual(self.mirror.git("for-each-ref", "refs/concurro/snapshots/").stdout, b"", "pins released")

    def test_native_turn_reads_full_feedback_outside_checkout_and_retained_home(self):
        """The actual agent subprocess receives files before it starts; extraction excludes them."""
        from policy import digest

        evidence = {"id": "F1", "detail": "complete feedback " * 20000}
        result, root = self.execute({"stage": "develop", "session": None}, feedback_files={digest(evidence): evidence})
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(json.loads((root / "scratch/feedback-read.json").read_text()), [evidence])
        self.assertFalse(any("feedback" in edit["path"] for edit in result["workspace"]["edits"]))
        self.assertFalse(list((root / "retained").rglob("*.json")))

    def test_a_review_turn_carries_no_workspace(self):
        result, _ = self.execute({"stage": "review", "session": None})
        self.assertEqual((result["outcome"], result["workspace"], result["retained"]), ("ok", None, None))

    def test_exit_codes_and_output_map_to_outcomes(self):
        self.assertEqual(turns.outcome(0, '{"a": 1}'), ("ok", {"a": 1}))
        for code, stdout, expected in (
            (75, "", "uncertain"),
            (137, "", "uncertain"),
            (-9, "", "uncertain"),
            (1, '{"a": 1}', "failed"),
            (0, "not json", "failed"),
            (0, "1" * (turns.MAX_RESPONSE + 1), "failed"),
        ):
            with self.subTest(code=code, stdout=stdout[:10]):
                self.assertEqual(turns.outcome(code, stdout)[0], expected)

    def test_native_tools_keep_target_home_and_tools_but_not_job_credentials(self):
        root = Path("/runner/turn-" + "0" * 32)
        with patch.dict(
            os.environ,
            {
                "HOME": "/home/runner",
                "CARGO_HOME": "/toolchain/cargo",
                "PATH": "/target/bin",
                "GITHUB_TOKEN": "github-secret",
                "GH_TOKEN": "gh-secret",
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc-secret",
                "OPENAI_API_KEY": "provider-secret",
            },
        ):
            env = turns.agent_environment(root, "session-key", True)
        self.assertEqual(
            (env["HOME"], env["CARGO_HOME"], env["PATH"]), ("/home/runner", "/toolchain/cargo", "/target/bin")
        )
        self.assertEqual(env["CODEX_HOME"], str(root / "retained/codex"))
        self.assertEqual(env[turns.KEY_VARIABLE], "session-key")
        self.assertFalse(set(env) & {"GITHUB_TOKEN", "GH_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "OPENAI_API_KEY"})
        self.assertNotIn("docker", turns.command())

    def test_mode_only_and_nul_binary_changes_are_rejected(self):
        root = self.prepare()
        path = root / "work/src/lib.rs"
        path.chmod(0o755)
        with self.assertRaisesRegex(turns.TurnError, "mode changes"):
            turns.extract(root / "work", root / "pristine.git", root)
        path.chmod(0o644)
        path.write_bytes(b"text\0binary")
        with self.assertRaisesRegex(turns.TurnError, "binary"):
            turns.extract(root / "work", root / "pristine.git", root)

    def test_content_and_mode_change_is_not_silently_dropped(self):
        root = self.prepare()
        path = root / "work/src/lib.rs"
        path.write_text("changed\n")
        path.chmod(0o755)
        with self.assertRaisesRegex(turns.TurnError, "mode changes"):
            turns.extract(root / "work", root / "pristine.git", root)

    def test_extraction_refusal_keeps_the_retained_transcript(self):
        session = {"scope": "c" * 64, "turn": 1, "role": "develop:codex:m"}
        with patch.object(turns, "extract", side_effect=turns.TurnError("mode changes are not supported")):
            result, _ = self.execute({"stage": "develop", "session": session})
        self.assertEqual(result["outcome"], "ok")
        self.assertIn("mode changes", result["workspace"]["error"])
        self.assertTrue(result["retained"])

    def test_retained_archives_are_bounded_and_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "a").write_text("transcript")
            archive = turns.pack(directory)
        with tempfile.TemporaryDirectory() as directory:
            turns.unpack(archive, directory)
            self.assertEqual(Path(directory, "a").read_text(), "transcript")
        with patch.object(turns, "MAX_RETAINED", 10), self.assertRaises(turns.TurnError):
            turns.unpack(archive, tempfile.mkdtemp())

    def test_an_oversized_retained_archive_names_its_largest_parts(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "codex/sessions").mkdir(parents=True)
            Path(directory, "codex/sessions/r.jsonl").write_bytes(os.urandom(4096))
            Path(directory, 'codex/a"\nb').write_text("{}")
            with (
                patch.object(turns, "MAX_RETAINED", 10),
                self.assertRaisesRegex(
                    turns.TurnError, r'largest \(uncompressed\): "codex/sessions" 4 KiB, "codex/a\\"\\nb" 0 KiB$'
                ),
            ):
                turns.pack(directory)

    def test_a_retained_codex_home_is_archived_without_its_caches(self):
        kept = [
            "codex/sessions/2026/11/04/rollout.jsonl",
            "codex/state_5.sqlite",
            "codex/state_5.sqlite-wal",
            "codex/thread_history_1.sqlite",
            "home/.config/x",
            "turn.json",
        ]
        cached = [
            "codex/.tmp/bundled-marketplaces/m.json",
            "codex/.tmp/plugins/p.json",
            "codex/tmp/arg0/x",
            "codex/plugins/cache/c",
            "codex/cache/remote_plugin_catalog/c",
            "codex/log/codex.log",
            "codex/logs_2.sqlite",
            "codex/logs_2.sqlite-wal",
            "codex/models_cache.json",
        ]
        with tempfile.TemporaryDirectory() as directory:
            for name in kept + cached:
                Path(directory, name).parent.mkdir(parents=True, exist_ok=True)
                Path(directory, name).write_text("x")
            archive = turns.pack(directory)
        with tempfile.TemporaryDirectory() as directory:
            turns.unpack(archive, directory)
            self.assertEqual(
                sorted(str(p.relative_to(directory)) for p in Path(directory).rglob("*") if p.is_file()), sorted(kept)
            )


class AgentsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.retained = Path(directory.name)
        self.config = {**CONFIG, "models": MODELS}

    def ask(self, result=None, stage="review", session=None, candidate=None, **extra):
        executor = FakeExecutor(result)
        agents = Agents(self.config, executor, retained=self.retained)
        context = {
            "bundle_id": agents.bundle.identity,
            "repository_commit": "a" * 40,
            "repository": {"refs": {"main": "a" * 40}, "head": "main"},
            **extra,
        }
        if session:
            context["controller_session"] = session
        if candidate is not None:
            context["controller_candidate"] = candidate
        return agents.ask("codex", stage, context), executor, agents

    def test_a_turn_carries_the_request_spec_and_gateway(self):
        _, executor, agents = self.ask(
            stage="develop",
            candidate={"docs/guide.md": "x"},
            result={
                "outcome": "ok",
                "response": {"summary": "s", "checks": [], "design_issue": None},
                "workspace": {"edits": [], "removed": [], "skipped": []},
            },
        )
        [(turn, label)] = executor.turns
        request = turn["request"]
        self.assertEqual((request["provider"], request["model"], request["gateway"]), ("codex", "gpt-test", GATEWAY))
        self.assertEqual(request["charter"], agents.bundle.charter_text)
        self.assertNotIn("controller_candidate", request["context"])
        self.assertEqual(turn["spec"]["candidate"], {"files": {"docs/guide.md": "x"}})
        self.assertEqual((turn["timeout"], label), (None, CONFIG["repository"] + ":develop"))

    def test_pr_review_uses_the_job_limit_but_design_review_stays_bounded(self):
        for stage, expected in (("review", None), ("plan_qc", 1200), ("plan", 1200)):
            _, executor, _ = self.ask(stage=stage)
            self.assertEqual(executor.turns[0][0]["timeout"], expected)

    def test_develop_returns_the_workspace_edits(self):
        response = {"summary": "s", "checks": ["lint"], "design_issue": None}
        workspace = {"edits": [{"path": "a.md", "content": "x"}], "removed": [], "skipped": []}
        result, _, _ = self.ask(
            {"outcome": "ok", "response": response, "workspace": workspace}, stage="develop", candidate={}
        )
        self.assertEqual(result, {**response, "edits": workspace["edits"]})
        issue = {**response, "design_issue": {"kind": "plan", "explanation": "e", "evidence": []}}
        result, _, _ = self.ask(
            {"outcome": "ok", "response": issue, "workspace": workspace}, stage="develop", candidate={}
        )
        self.assertEqual(result["edits"], [], "a design issue keeps no edits")
        result, _, _ = self.ask(
            {"outcome": "ok", "response": response, "workspace": {**workspace, "removed": ["b.md"]}},
            stage="develop",
            candidate={},
        )
        self.assertEqual(result["edits"], [*workspace["edits"], {"path": "b.md", "content": None}])

    def test_failures_map_to_the_engines_recovery(self):
        for outcome, expected in (("failed", Blocked), ("uncertain", UncertainOutcome), ("lost", UncertainOutcome)):
            with self.subTest(outcome=outcome), self.assertRaises(expected) as raised:
                self.ask({"outcome": outcome, "response": None}, stage="intake")
            self.assertNotIsInstance(raised.exception, SessionFailure)
        session = {"scope": "d" * 64, "turn": 0}
        archive = base64.b64encode(b"kept").decode()
        with self.assertRaises(SessionFailure) as raised:
            self.ask({"outcome": "uncertain", "response": None, "retained": archive}, stage="plan", session=session)
        self.assertTrue(raised.exception.uncertain)
        self.assertEqual(Retained(self.retained).load(session), archive, "the transcript survives a failed turn")

    def test_a_session_turn_carries_its_transcript(self):
        session = {"scope": "e" * 64, "turn": 1}
        Retained(self.retained).save(session, base64.b64encode(b"before").decode())
        after = base64.b64encode(b"after").decode()
        _, executor, _ = self.ask({"outcome": "ok", "response": {}, "retained": after}, stage="plan", session=session)
        self.assertEqual(base64.b64decode(executor.turns[0][0]["retained"]), b"before")
        self.assertEqual(Retained(self.retained).load(session), after)
        with self.assertRaises(Blocked):
            Retained(self.retained).load({"scope": "../x", "turn": 0})

    def test_routes_models_and_bundles_are_checked(self):
        agents = Agents(self.config, FakeExecutor())
        self.assertEqual(
            agents.credential("claude", "plan", None), Credential("gateway:claude", "claude", "org", "gateway-key")
        )
        with self.assertRaisesRegex(Blocked, "no inference gateway route"):
            agents.credential("copilot", "review", None)
        foreign = agents.credential("claude", "review", None).record()
        with self.assertRaisesRegex(Blocked, "another provider"):
            self.ask(controller_credential=foreign)
        with self.assertRaisesRegex(Blocked, "explicit model"):
            Agents({**CONFIG, "models": {**MODELS, "codex": ""}}, FakeExecutor()).ask("codex", "review", {})
        with self.assertRaisesRegex(Blocked, "bundle differs"):
            agents.ask("codex", "review", {"bundle_id": "old"})


class ExecutorTests(ExecutorFixture, unittest.TestCase):
    def test_a_runner_turn_dispatches_a_session_when_needed_and_returns_the_result(self):
        result = {"outcome": "ok", "response": {"a": 1}}
        executor, operator, started = self.runner(
            [
                {"turn": "t", "dispatch": "s-2"},
                {"status": "queued"},
                {"status": "running"},
                {"status": "done", "result": result},
            ]
        )
        self.assertEqual(executor.run(TURN, "label"), result)
        self.assertEqual(started, ["s-2"])
        self.assertEqual(
            operator.calls[0],
            (
                "submit",
                (
                    "t",
                    {"repository_id": "1"},
                    ["claude-test"],
                    5000,
                    21600,
                    TURN,
                    None,
                    {**TASK, "base": "a" * 40, "role": "plan:claude", "phase": "single"},
                ),
            ),
        )

    def test_a_lost_submission_reply_is_recovered_under_the_same_turn(self):
        result = {"outcome": "ok", "response": {}}
        executor, operator, started = self.runner(
            [
                ExchangeError(0, "unreachable"),
                ExchangeError(502, ""),
                {"turn": "t", "dispatch": "s-2"},
                {"status": "queued", "dispatch": "s-2"},
                {"status": "done", "result": result},
            ]
        )
        self.assertEqual(executor.run(TURN, "label"), result)
        self.assertEqual([call[1][0] for call in operator.calls if call[0] == "submit"], ["t", "t", "t"])
        self.assertEqual(started, ["s-2"], "a session is dispatched once")

    def test_a_turn_completed_before_its_admission_is_recovered_returns_its_result(self):
        result = {"outcome": "ok", "response": {}}
        executor, operator, _ = self.runner(
            [ExchangeError(0, "unreachable"), {"turn": "t", "dispatch": None, "status": "done", "result": result}]
        )
        self.assertEqual(executor.run(TURN, "label"), result)
        executor, operator, _ = self.runner(
            [{"turn": "t", "dispatch": None, "status": "done"}, {"status": "done", "result": result}]
        )
        self.assertEqual(executor.run(TURN, "label"), result, "a reply without the result is polled for it")
        self.assertEqual(operator.calls[-1], ("poll", "t"))

    def test_an_unknown_admission_is_uncertain_and_a_refused_one_is_not(self):
        executor, operator, _ = self.runner([ExchangeError(0, "unreachable")] * 4)
        self.assertEqual(executor.run(TURN, "label")["outcome"], "uncertain")
        self.assertEqual(operator.calls[-1], ("cancel", "t"), "a late admission finds the turn withdrawn")
        executor, operator, _ = self.runner([ExchangeError(400, "invalid")])
        with self.assertRaises(GatewayUnavailable):
            executor.run(TURN, "label")
        self.assertEqual([call[0] for call in operator.calls], ["submit", "cancel"])

    def test_a_lost_turn_is_uncertain_and_a_refusal_blocks(self):
        executor, _, _ = self.runner([{"turn": "t", "dispatch": None}, {"status": "lost"}])
        self.assertEqual(executor.run(TURN, "label")["outcome"], "lost")
        executor, operator, _ = self.runner(
            [
                {"turn": "t", "dispatch": None},
                {"status": "queued", "refusal": "token does not match the session: actor_id"},
            ]
        )
        with self.assertRaisesRegex(Blocked, "actor_id"):
            executor.run(TURN, "label")
        self.assertEqual(operator.calls[-1], ("cancel", "t"))

    def test_a_turn_past_its_deadline_is_withdrawn(self):
        times = iter([0, 10**6])
        executor, operator, _ = self.runner([{"turn": "t", "dispatch": None}], clock=lambda: next(times))
        self.assertEqual(executor.run(TURN, "label")["outcome"], "uncertain")
        self.assertEqual(operator.calls[-1], ("cancel", "t"))

    def test_a_failed_dispatch_ends_its_session_and_withdraws_the_turn(self):
        def refuse(session):
            raise Blocked("GitHub rejected the write")

        executor, operator, _ = self.runner([{"turn": "t", "dispatch": "s-3"}], dispatch=refuse)
        with self.assertRaisesRegex(Blocked, "rejected"):
            executor.run(TURN, "label")
        self.assertEqual(operator.calls[1:], [("cancel", "t"), ("end", "s-3")])


class RunnerLoopTests(unittest.TestCase):
    def test_turns_run_and_report_until_the_session_ends(self):
        turn = {"id": "t1", "payload": {"request": {"stage": "plan", "provider": "claude"}}}
        client = FakeRunner([[turn], []])
        loop = runner.Loop(client, "sk-agw-run", object(), tempfile.mkdtemp(), 3600)
        with (
            patch.object(turns, "execute", return_value={"outcome": "ok", "response": {}}) as execute,
            patch.object(runner, "HEARTBEAT", 0),
        ):
            loop.serve()
        self.assertEqual(execute.call_args.args[:2], (turn["payload"], "sk-agw-run"))
        self.assertEqual(client.completed, [("t1", {"outcome": "ok", "response": {}})])

    def test_a_turn_that_cannot_run_reports_a_failure(self):
        turn = {"id": "t2", "payload": {"request": {"stage": "plan", "provider": "codex"}}}
        client = FakeRunner([[turn]])
        loop = runner.Loop(client, "k", object(), tempfile.mkdtemp(), 3600)
        with patch.object(turns, "execute", side_effect=OSError("disk full")), patch.object(runner, "HEARTBEAT", 0):
            loop.serve()
        self.assertEqual(client.completed[0][1]["outcome"], "failed")

    def test_an_idle_session_ends(self):
        client = FakeRunner([[], [], []])
        loop = runner.Loop(client, "k", object(), tempfile.mkdtemp(), -1)
        with patch.object(runner, "HEARTBEAT", 0):
            loop.serve()
        self.assertEqual(len(client.batches), 2, "it stopped at the first idle poll")


class EntryTests(unittest.TestCase):
    """The CLIs run with native tools in the workspace, the gateway their only endpoint."""

    def request(self, provider, **extra):
        return {"provider": provider, "model": "m", "schema": {}, "gateway": GATEWAY, **extra}

    def test_codex_uses_the_gateway_route_and_the_container_as_its_sandbox(self):
        command = agent_entry.command_for(self.request("codex"), "schema", "out")
        self.assertIn('sandbox_mode="danger-full-access"', command)
        self.assertIn('model_provider="concurro"', command)
        provider = next(c for c in command if c.startswith("model_providers.concurro="))
        self.assertIn('"base_url" = "https://gateway.invalid/concurro/v1"', provider)
        self.assertIn('"env_key" = "CONCURRO_GATEWAY_KEY"', provider)
        self.assertIn('"http_headers" = {"x-concurro-plan" = "chatgpt"}', provider)
        self.assertFalse([c for c in command if "mcp_servers" in c or "shell_tool" in c])

    def test_claude_gets_native_tools_and_the_key_as_its_token(self):
        command = agent_entry.command_for(self.request("claude"), "schema", "out")
        self.assertEqual(command[command.index("--permission-mode") + 1], "bypassPermissions")
        self.assertNotIn("--tools", command)
        self.assertNotIn("--mcp-config", command)
        with patch.dict(os.environ, {"CONCURRO_GATEWAY_KEY": "sk-agw-x"}):
            environment = agent_entry.provider_environment(self.request("claude"))
        self.assertEqual(environment["ANTHROPIC_BASE_URL"], GATEWAY["url"])
        self.assertEqual(environment["ANTHROPIC_AUTH_TOKEN"], "sk-agw-x")
        with self.assertRaises(ValueError):
            agent_entry.command_for(self.request("copilot"), "schema", "out")


if __name__ == "__main__":
    unittest.main()


class RelayEndToEndTests(Workspace, unittest.TestCase):
    """The controller's agents, the real exchange over HTTP, the runner loop and a real workspace."""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gateway/tests"))
        import exchange_fixtures  # puts the exchange on the module path
        import exchange as exchange_module

        self.signer = exchange_fixtures.Signer(str(self.tmp))
        self.exchange = exchange_module.Exchange(
            str(self.tmp / "exchange.db"),
            exchange_fixtures.FakeGateway(),
            exchange_module.Keys(fetch=lambda: {"keys": [self.signer.jwk]}),
            exchange_fixtures.AUDIENCE,
        )
        self.server = exchange_module.server(self.exchange, "127.0.0.1:0", "operator-secret")
        import threading

        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.expected = {**exchange_fixtures.EXPECTED, "actor_id": "3003"}

    def test_same_cli_review_profiles_run_in_distinct_groups_and_cancel_independently(self):
        """Real HTTP exchange staffing separates profiles, while each runner stays serial."""
        from relay import Operator, Runner

        dispatched = []
        executor = RunnerExecutor(
            Operator(self.url, "operator-secret"), GATEWAY, self.expected, ["gpt-*"], 1000, dispatched.append, task=TASK
        )

        def turn(profile):
            """Different model profiles deliberately share the codex harness and phase."""
            return {
                **deepcopy(TURN),
                "request": {
                    "provider": "codex",
                    "stage": "plan_qc",
                    "model": "gpt-" + profile,
                    "context": {"base": "a" * 40, "review_phase": "cooperative"},
                    "agent": {
                        "profile": profile,
                        "provider": "openai",
                        "harness": "codex",
                        "model": "gpt-" + profile,
                        "effort": "high",
                    },
                },
            }

        first, second = executor.submit(turn("one")), executor.submit(turn("two"))
        queued = executor.submit(turn("one"))
        self.assertEqual(len(dispatched), 2)
        self.assertNotEqual(first.group, second.group)
        runners = []
        for session in dispatched:
            client = Runner(self.url, session)
            client.key(self.signer.token(actor_id="3003", ref="refs/heads/concurro/runs/" + session))
            runners.append(client)
        a, b = runners
        self.assertEqual([t["id"] for t in a.next(8, 0)], [first.identifier])
        self.assertEqual([t["id"] for t in b.next(8, 0)], [second.identifier])
        self.assertEqual(a.next(8, 0), [])
        self.assertEqual(b.next(8, 0), [])
        executor.cancel(second)
        a.complete(first.identifier, {"outcome": "ok", "response": {}})
        self.assertEqual(executor.observe(first)["outcome"], "ok")
        self.assertEqual([t["id"] for t in a.next(8, 0)], [queued.identifier])
        executor.cancel(queued)

    def test_a_develop_turn_round_trips_through_a_runner_session(self):
        self.round_trip()

    def test_large_workspace_contents_cross_the_exchange_outside_model_output(self):
        """The structured answer stays small while extracted source exceeds the old 2 MB cap."""
        self.round_trip(large=True)

    def test_full_feedback_crosses_the_exchange_and_is_read_on_the_runner(self):
        """The real exchange and runner carry full files separately from the prompt index."""
        self.round_trip(with_feedback=True)

    def round_trip(self, large=False, with_feedback=False):
        """Run the real relay and extraction, optionally returning a multi-megabyte edit."""
        import threading
        from relay import Operator, Runner

        loops = []

        def dispatch(session):
            def serve():
                client = Runner(self.url, session)
                grant = client.key(self.signer.token(actor_id="3003", ref="refs/heads/concurro/runs/" + session))
                loop = runner.Loop(client, grant["key"], self.mirror, self.tmp / "runner", 0)
                loop.serve()
                client.end()

            thread = threading.Thread(target=serve)
            thread.start()
            loops.append(thread)

        # The runner's agent: the stand-in container script, with the key the exchange minted.
        def fake():
            """Locate the turn from its CLI home now that command takes no directory."""
            script = AGENT.replace('"sk-agw-test"', 'os.environ["CONCURRO_GATEWAY_KEY"]')
            if with_feedback:
                script = script.replace(
                    "request = json.load(sys.stdin)",
                    'request = json.load(sys.stdin)\nassert request.get("feedback_directory")\n'
                    'assert json.loads(next(pathlib.Path(request["feedback_directory"]).glob("*.json")).read_text())'
                    ' == [{"id": "F1", "detail": "full evidence " * 20000}]',
                )
            if large:
                script = script.replace('"Guide\\nEdited\\n"', '"Source\\n" * 500000')
            script = script.replace(
                "root = pathlib.Path(sys.argv[1])", 'root = pathlib.Path(os.environ["CODEX_HOME"]).parents[1]'
            )
            return [sys.executable, "-c", script]

        executor = RunnerExecutor(
            Operator(self.url, "operator-secret"), GATEWAY, self.expected, ["gpt-*"], 1000, dispatch, task=TASK
        )
        agents = Agents({**CONFIG, "models": MODELS, "base": "main"}, executor, retained=self.tmp / "sessions")
        context = {
            "bundle_id": agents.bundle.identity,
            "repository_commit": self.base,
            "repository": {"refs": {"main": self.base}, "head": "main"},
            "controller_candidate": {"src/lib.rs": "fn main() {}\n"},
        }
        if with_feedback:
            import feedback

            context = feedback.prepare(
                {**context, "review_findings": [{"id": "F1", "detail": "full evidence " * 20000}]}
            )
        with patch.object(turns, "command", side_effect=fake), patch.object(runner, "HEARTBEAT", 1):
            result = agents.ask("codex", "develop", context)
            for thread in loops:
                thread.join(30)
        self.assertEqual(
            result["edits"],
            [
                {"path": "docs/guide.md", "content": "Source\n" * 500000 if large else "Guide\nEdited\n"},
                {"path": "docs/new.md", "content": "New\n"},
            ],
        )
        self.assertEqual(len(loops), 1, "one session served the target")
        self.assertEqual(self.exchange.gateway.list(), [], "the session's key is gone once its runner ended it")


class SetupLifecycleTests(unittest.TestCase):
    """Cold setup stays live without taking a turn; setup consumes only the signed grant's binding."""

    def test_setup_heartbeat_never_takes_a_turn_and_stops_on_revocation(self):
        import threading

        stopped = threading.Event()
        calls = []

        class Client:
            def next(self, capacity, wait):
                calls.append((capacity, wait))
                if len(calls) == 4:
                    raise ExchangeError(410, "ended")
                return []

        runner.heartbeat(Client(), stopped, interval=0)
        self.assertEqual(calls, [(0, 0)] * 4)
        self.assertTrue(stopped.is_set())

    def test_setup_uses_recorded_config_and_upstream_base(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, base = root / "config", root / "base"
            output = root / "output"
            with patch.object(runner, "checkout") as checkout, patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
                runner.prepare({"binding": {**TASK, "base": "a" * 40}}, config, base)
            self.assertEqual(checkout.call_args_list[0].args, (TASK["fork"], TASK["config_sha"], config))
            self.assertEqual(checkout.call_args_list[1].args, (TASK["upstream"], "a" * 40, base))
            self.assertIn("setup=false", output.read_text())
            (config / ".concurro/setup").mkdir(parents=True)
            (config / ".concurro/setup/action.yml").write_text("runs: {using: composite, steps: []}")
            with patch.object(runner, "checkout"), patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
                runner.prepare({"binding": {**TASK, "base": "a" * 40}}, config, base)
            self.assertIn("setup=true", output.read_text())

    def test_claim_persists_secrets_privately_and_starts_a_detached_helper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "session"
            args = ["runner.py", "claim", "--session", "s", "--gateway", "https://gateway.invalid", "--root", str(root)]
            grant = {"key": "session-key", "token": "runner-token", "binding": TASK}
            with (
                patch.object(sys, "argv", args),
                patch.object(runner, "oidc_token", return_value="jwt"),
                patch.object(runner.Runner, "key", return_value=grant),
                patch.object(runner.subprocess, "Popen") as start,
            ):
                start.return_value.pid = 42
                runner.main()
            self.assertEqual(json.loads((root / "grant.json").read_text()), grant)
            self.assertEqual((root / "grant.json").stat().st_mode & 0o777, 0o600)
            self.assertTrue(start.call_args.kwargs["start_new_session"])
            self.assertIn("heartbeat", start.call_args.args[0])
            self.assertNotIn("session-key", repr(start.call_args))

    def test_lost_ownership_cancels_dispatched_session_and_discards_result(self):
        from service.errors import OwnershipLost

        operator = FakeOperator([{"turn": "t", "dispatch": "s"}, {"status": "done", "result": {"outcome": "ok"}}])
        checks = []

        def owner():
            checks.append(1)
            if len(checks) == 3:
                raise OwnershipLost("new owner")

        executor = RunnerExecutor(
            operator, GATEWAY, {}, ["claude-*"], 1000, lambda _: None, task=TASK, check_owner=owner
        )
        with self.assertRaises(OwnershipLost):
            executor.run(TURN, "test")
        self.assertIn(("end", "s"), operator.calls)
        self.assertTrue(any(call[0] == "cancel" for call in operator.calls))
