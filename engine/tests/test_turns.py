"""Turns: workspaces from snapshots, extraction, the agent container contract, executors and the runner loop."""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from support import CONFIG, setUpModule, tearDownModule  # unittest module fixtures
import agent_entry
from agents import Agents, GatewayUnavailable, HostExecutor, Retained, RunnerExecutor
from credentials import Credential
from gitfixture import Upstream, mirror_of
from policy import Blocked, SessionFailure, UncertainOutcome
from relay import ExchangeError
import repository
import runner
import turns

GATEWAY = {"url": "https://gateway.invalid/concurro", "headers": {"codex": {"x-concurro-plan": "chatgpt"}}}
IMAGE = "sha256:" + "a" * 64
MODELS = {**CONFIG["models"], "codex": "gpt-test", "claude": "claude-test"}
# Stands in for the agent container: reads the request, edits the workspace, answers as the entry point does.
AGENT = """
import json, os, pathlib, sys
root = pathlib.Path(sys.argv[1])
request = json.load(sys.stdin)
assert os.environ["CONCURRO_GATEWAY_KEY"] == "sk-agw-test" and "GH_TOKEN" not in os.environ
assert request["workspace"] == "/work" and request["timeout"] == 1800
work = root / "work"
(work / "docs/guide.md").write_text("Guide\\nEdited\\n")
(work / "docs/new.md").write_text("New\\n")
(work / "target").mkdir()
(work / "target/build.out").write_text("ignored")
if request.get("session"):
    (root / "retained/turn.json").write_text(json.dumps({"turn": request["session"]["turn"]}))
print(json.dumps({"summary": "done", "checks": [], "design_issue": None}))
"""


class Workspace(unittest.TestCase):
    """A real upstream and mirror, and a turn directory prepared the way `turns.execute` prepares one."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name).resolve()
        self.upstream = Upstream(self.tmp / "upstream.git")
        self.base = self.upstream.commit({"docs/guide.md": "Guide\n", "src/lib.rs": "fn main() {}\n",
                                          ".gitignore": "target/\n"}, branch="main", message="base")
        self.mirror = mirror_of(self.upstream, self.tmp / "mirrors")
        self.spec = {"refs": {"main": self.base}, "head": "main", "base_branch": "main", "candidate": None}

    def prepare(self, candidate=None):
        root = turns.directory(self.tmp / "turns")
        (root / "work").mkdir()
        self.mirror.borrow(root.name, "main", [self.base])
        repository.snapshot({**self.spec, "candidate": candidate}, root=root / "work", mirror=self.mirror.path)
        shutil.copytree(root / "work/.git", root / "pristine.git", symlinks=True)
        return root


class ExtractionTests(Workspace):

    def test_changes_are_extracted_against_the_snapshot_head(self):
        root = self.prepare(candidate={"files": {"docs/guide.md": "Candidate\n"}})
        work = root / "work"
        self.assertEqual((work / "docs/guide.md").read_text(), "Candidate\n", "the head is the candidate")
        (work / "src/lib.rs").write_text("fn main() { run(); }\n")
        (work / ".gitignore").chmod(0o755)
        (work / "src/run.rs").write_text("pub fn run() {}\n")
        (work / "docs/guide.md").unlink()
        (work / "target").mkdir()
        (work / "target/debug.bin").write_bytes(b"\xff\x00")
        changes = turns.extract(work, root / "pristine.git", root)
        self.assertEqual(changes["edits"], [{"path": "src/lib.rs", "content": "fn main() { run(); }\n"},
                                            {"path": "src/run.rs", "content": "pub fn run() {}\n"}])
        self.assertEqual(changes["removed"], ["docs/guide.md"])

    def test_nothing_the_agent_wrote_under_git_takes_part(self):
        root = self.prepare()
        work, marker = root / "work", self.tmp / "ran"
        hook = self.tmp / "hook.sh"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        with (work / ".git/config").open("a") as config:
            config.write(f"[core]\n\tfsmonitor = {hook}\n\thooksPath = {self.tmp}\n"
                         f"[filter \"evil\"]\n\tclean = {hook}\n\trequired = true\n")
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
        git = {**os.environ, "GIT_DIR": str(pristine), "GIT_INDEX_FILE": str(root / "link.index")}

        def run(*args, env=git):
            return subprocess.run(args, check=True, capture_output=True, env=env, text=True).stdout.strip()

        run("git", "read-tree", "HEAD")
        run("git", "update-index", "--add", "--cacheinfo", f"160000,{self.base},vendor/sub")
        commit = run("git", "commit-tree", run("git", "write-tree"), "-p", "HEAD", "-m", "submodule")
        run("git", "update-ref", "HEAD", commit)
        sub = work / "vendor/sub"
        sub.mkdir(parents=True)
        inner = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "GIT_CONFIG_GLOBAL": os.devnull,
                 "GIT_CONFIG_NOSYSTEM": "1"}
        run("git", "init", "-q", str(sub), env=inner)
        (sub / "file").write_text("x\n")
        run("git", "-C", str(sub), "add", "file", env=inner)
        run("git", "-C", str(sub), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x", env=inner)
        hook = self.tmp / "hook.sh"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
        hook.chmod(0o755)
        with (sub / ".git/config").open("a") as config:
            config.write(f"[filter \"evil\"]\n\tclean = {hook}\n")
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
        for name, make in (("link", lambda p: p.symlink_to("/etc/passwd")),
                           ("binary", lambda p: p.write_bytes(b"\xff\xfe"))):
            path = work / ("src/" + name)
            make(path)
            with self.subTest(name=name), self.assertRaises(turns.TurnError):
                turns.extract(work, root / "pristine.git", root)
            path.unlink()


class ExecuteTests(Workspace):

    def execute(self, request, retained=None):
        root = turns.directory(self.tmp / "turns")
        fake = lambda name, image, directory, mirror, kept, limits: [sys.executable, "-c", AGENT, str(directory)]
        turn = {"request": request, "spec": self.spec, "retained": retained, "timeout": 1800}
        with patch.object(turns, "command", side_effect=fake), patch.object(turns.cleanup, "remove") as removed, \
                patch.dict(os.environ, {"GH_TOKEN": "owner-secret"}):
            result = turns.execute(turn, IMAGE, "sk-agw-test", self.mirror, root)
        removed.assert_called_once()
        return result, root

    def test_a_develop_turn_returns_its_workspace_and_transcript(self):
        session = {"scope": "c" * 64, "turn": 1, "role": "develop:codex:m"}
        result, root = self.execute({"stage": "develop", "session": session}, retained=None)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["response"], {"summary": "done", "checks": [], "design_issue": None})
        self.assertEqual(result["workspace"]["edits"], [{"path": "docs/guide.md", "content": "Guide\nEdited\n"},
                                                        {"path": "docs/new.md", "content": "New\n"}])
        restored = self.tmp / "restored"
        turns.unpack(result["retained"], restored)
        self.assertEqual(json.loads((restored / "turn.json").read_text()), {"turn": 1})
        self.assertEqual(self.mirror.git("for-each-ref", "refs/concurro/snapshots/").stdout, b"", "pins released")

    def test_a_review_turn_carries_no_workspace(self):
        result, _ = self.execute({"stage": "review", "session": None})
        self.assertEqual((result["outcome"], result["workspace"], result["retained"]), ("ok", None, None))

    def test_exit_codes_and_output_map_to_outcomes(self):
        self.assertEqual(turns.outcome(0, '{"a": 1}'), ("ok", {"a": 1}))
        for code, stdout, expected in ((75, "", "uncertain"), (137, "", "uncertain"), (-9, "", "uncertain"),
                                       (1, '{"a": 1}', "failed"), (0, "not json", "failed"),
                                       (0, "1" * (turns.MAX_RESPONSE + 1), "failed")):
            with self.subTest(code=code, stdout=stdout[:10]):
                self.assertEqual(turns.outcome(code, stdout)[0], expected)

    def test_the_container_holds_the_key_by_name_and_mounts_nothing_else(self):
        root = Path("/runner/turn-" + "0" * 32)
        command = turns.command("c", IMAGE, root, "/srv/mirror.git", True, turns.Limits())
        self.assertEqual([command[i + 1] for i, a in enumerate(command) if a == "--mount"],
                         [f"type=bind,source={root}/work,target=/work", f"type=bind,source={root}/scratch,target=/scratch",
                          "type=bind,source=/srv/mirror.git,target=/srv/mirror.git,readonly",
                          f"type=bind,source={root}/retained,target=/retained"])
        variables = [command[i + 1] for i, a in enumerate(command) if a == "--env"]
        self.assertIn(turns.KEY_VARIABLE, variables)
        self.assertFalse([v for v in variables if "=" not in v and v != turns.KEY_VARIABLE])
        for option in ("--cap-drop=ALL", "--security-opt=no-new-privileges", f"--user={os.getuid()}:{os.getgid()}"):
            self.assertIn(option, command)
        self.assertNotIn("--privileged", command)

    def test_retained_archives_are_bounded_and_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "a").write_text("transcript")
            archive = turns.pack(directory)
        with tempfile.TemporaryDirectory() as directory:
            turns.unpack(archive, directory)
            self.assertEqual(Path(directory, "a").read_text(), "transcript")
        with patch.object(turns, "MAX_RETAINED", 10), self.assertRaises(turns.TurnError):
            turns.unpack(archive, tempfile.mkdtemp())


class FakeExecutor:
    gateway = GATEWAY

    def __init__(self, result=None):
        self.turns, self.result = [], result or {"outcome": "ok", "response": {"ok": True}}

    def run(self, turn, label):
        self.turns.append((turn, label))
        return self.result


class AgentsTests(unittest.TestCase):

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.retained = Path(directory.name)
        self.config = {**CONFIG, "models": MODELS}

    def ask(self, result=None, stage="review", session=None, candidate=None, **extra):
        executor = FakeExecutor(result)
        agents = Agents(self.config, executor, retained=self.retained)
        context = {"bundle_id": agents.bundle.identity, "repository_commit": "a" * 40,
                   "repository": {"refs": {"main": "a" * 40}, "head": "main"}, **extra}
        if session:
            context["controller_session"] = session
        if candidate is not None:
            context["controller_candidate"] = candidate
        return agents.ask("codex", stage, context), executor, agents

    def test_a_turn_carries_the_request_spec_and_gateway(self):
        _, executor, agents = self.ask(stage="develop", candidate={"docs/guide.md": "x"},
                                       result={"outcome": "ok", "response": {"summary": "s", "checks": [], "design_issue": None},
                                               "workspace": {"edits": [], "removed": [], "skipped": []}})
        [(turn, label)] = executor.turns
        request = turn["request"]
        self.assertEqual((request["provider"], request["model"], request["gateway"]), ("codex", "gpt-test", GATEWAY))
        self.assertEqual(request["charter"], agents.bundle.charter_text)
        self.assertNotIn("controller_candidate", request["context"])
        self.assertEqual(turn["spec"]["candidate"], {"files": {"docs/guide.md": "x"}})
        self.assertEqual((turn["timeout"], label), (1800, CONFIG["repository"] + ":develop"))

    def test_develop_returns_the_workspace_edits(self):
        response = {"summary": "s", "checks": ["lint"], "design_issue": None}
        workspace = {"edits": [{"path": "a.md", "content": "x"}], "removed": [], "skipped": []}
        result, _, _ = self.ask({"outcome": "ok", "response": response, "workspace": workspace}, stage="develop",
                                candidate={})
        self.assertEqual(result, {**response, "edits": workspace["edits"]})
        issue = {**response, "design_issue": {"kind": "plan", "explanation": "e", "evidence": []}}
        result, _, _ = self.ask({"outcome": "ok", "response": issue, "workspace": workspace}, stage="develop", candidate={})
        self.assertEqual(result["edits"], [], "a design issue keeps no edits")
        with self.assertRaisesRegex(Blocked, "deleted tracked files"):
            self.ask({"outcome": "ok", "response": response, "workspace": {**workspace, "removed": ["b.md"]}},
                     stage="develop", candidate={})

    def test_failures_map_to_the_engines_recovery(self):
        for outcome, expected in (("failed", Blocked), ("uncertain", UncertainOutcome), ("lost", UncertainOutcome)):
            with self.subTest(outcome=outcome), self.assertRaises(expected) as raised:
                self.ask({"outcome": outcome, "response": None})
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
        self.assertEqual(agents.credential("claude", "plan", None), Credential("gateway:claude", "claude", "org", "gateway-key"))
        with self.assertRaisesRegex(Blocked, "no inference gateway route"):
            agents.credential("copilot", "review", None)
        foreign = agents.credential("claude", "review", None).record()
        with self.assertRaisesRegex(Blocked, "another provider"):
            self.ask(controller_credential=foreign)
        with self.assertRaisesRegex(Blocked, "explicit model"):
            Agents({**CONFIG, "models": {**MODELS, "codex": ""}}, FakeExecutor()).ask("codex", "review", {})
        with self.assertRaisesRegex(Blocked, "bundle differs"):
            agents.ask("codex", "review", {"bundle_id": "old"})


class FakeOperator:

    def __init__(self, replies=()):
        self.replies, self.calls = list(replies), []

    def mint(self, *args):
        self.calls.append(("mint", args))
        return {"session": "s-1", "key": "sk-agw-host"}

    def end(self, session):
        self.calls.append(("end", session))

    def submit(self, *args):
        self.calls.append(("submit", args))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def poll(self, turn, wait=None):
        self.calls.append(("poll", turn))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def cancel(self, turn):
        self.calls.append(("cancel", turn))


TURN = {"request": {"model": "claude-test", "stage": "plan"}, "spec": {}, "retained": None, "timeout": 600}


class ExecutorTests(unittest.TestCase):

    def test_a_host_turn_mints_a_key_and_always_deletes_it(self):
        operator = FakeOperator()
        executor = HostExecutor(operator, GATEWAY, IMAGE, object(), tempfile.mkdtemp(), 1000)
        with patch.object(turns, "execute", return_value={"outcome": "ok"}) as execute:
            self.assertEqual(executor.run(TURN, "label"), {"outcome": "ok"})
        self.assertEqual(execute.call_args.args[2], "sk-agw-host")
        self.assertEqual(operator.calls, [("mint", (["claude-test"], 1000, 1200, "label")), ("end", "s-1")])
        with patch.object(turns, "execute", side_effect=turns.TurnError("bad workspace")), \
                self.assertRaisesRegex(Blocked, "bad workspace"):
            executor.run(TURN, "label")
        self.assertEqual(operator.calls[-1], ("end", "s-1"))
        operator.mint = lambda *args: (_ for _ in ()).throw(ExchangeError(0, "unreachable"))
        with self.assertRaises(GatewayUnavailable):
            executor.run(TURN, "label")

    def runner(self, replies, dispatch=None, clock=None):
        operator = FakeOperator(replies)
        started = []
        identity = patch("agents.uuid.uuid4", return_value="t")
        identity.start()
        self.addCleanup(identity.stop)
        executor = RunnerExecutor(operator, GATEWAY, {"repository_id": "1"}, ["claude-*"], 5000,
                                  dispatch or started.append, clock=clock or (lambda: 0), sleep=lambda _: None)
        return executor, operator, started

    def test_a_runner_turn_dispatches_a_session_when_needed_and_returns_the_result(self):
        result = {"outcome": "ok", "response": {"a": 1}}
        executor, operator, started = self.runner([{"turn": "t", "dispatch": "s-2"}, {"status": "queued"},
                                                   {"status": "running"}, {"status": "done", "result": result}])
        self.assertEqual(executor.run(TURN, "label"), result)
        self.assertEqual(started, ["s-2"])
        self.assertEqual(operator.calls[0], ("submit", ("t", {"repository_id": "1"}, ["claude-*"], 5000, 21600, TURN, None)))

    def test_a_lost_submission_reply_is_recovered_under_the_same_turn(self):
        result = {"outcome": "ok", "response": {}}
        executor, operator, started = self.runner([ExchangeError(0, "unreachable"), ExchangeError(502, ""),
                                                   {"turn": "t", "dispatch": "s-2"}, {"status": "queued", "dispatch": "s-2"},
                                                   {"status": "done", "result": result}])
        self.assertEqual(executor.run(TURN, "label"), result)
        self.assertEqual([call[1][0] for call in operator.calls if call[0] == "submit"], ["t", "t", "t"])
        self.assertEqual(started, ["s-2"], "a session is dispatched once")

    def test_a_turn_completed_before_its_admission_is_recovered_returns_its_result(self):
        result = {"outcome": "ok", "response": {}}
        executor, operator, _ = self.runner([ExchangeError(0, "unreachable"),
                                             {"turn": "t", "dispatch": None, "status": "done", "result": result}])
        self.assertEqual(executor.run(TURN, "label"), result)
        executor, operator, _ = self.runner([{"turn": "t", "dispatch": None, "status": "done"},
                                             {"status": "done", "result": result}])
        self.assertEqual(executor.run(TURN, "label"), result, "a reply without the result is polled for it")
        self.assertEqual(operator.calls[-1], ("poll", "t"))

    def test_an_unknown_admission_is_uncertain_and_a_refused_one_is_not(self):
        executor, operator, _ = self.runner([ExchangeError(0, "unreachable")] * 4)
        self.assertEqual(executor.run(TURN, "label")["outcome"], "uncertain")
        self.assertEqual(operator.calls[-1], ("cancel", "t"), "a late admission finds the turn withdrawn")
        executor, operator, _ = self.runner([ExchangeError(400, "invalid")])
        with self.assertRaises(GatewayUnavailable):
            executor.run(TURN, "label")
        self.assertEqual([call[0] for call in operator.calls], ["submit"])

    def test_a_lost_turn_is_uncertain_and_a_refusal_blocks(self):
        executor, _, _ = self.runner([{"turn": "t", "dispatch": None}, {"status": "lost"}])
        self.assertEqual(executor.run(TURN, "label")["outcome"], "lost")
        executor, operator, _ = self.runner([{"turn": "t", "dispatch": None},
                                             {"status": "queued", "refusal": "token does not match the session: actor_id"}])
        with self.assertRaisesRegex(Blocked, "actor_id"):
            executor.run(TURN, "label")
        self.assertEqual(operator.calls[-1], ("cancel", "t"))

    def test_a_turn_past_its_deadline_is_withdrawn(self):
        times = iter([0, 10 ** 6])
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


class FakeRunner:

    def __init__(self, batches):
        self.batches, self.completed = list(batches), []

    def next(self, capacity, wait):
        if not self.batches:
            raise ExchangeError(410, "session has ended")
        batch = self.batches.pop(0)
        return batch[:capacity]

    def complete(self, turn, result):
        self.completed.append((turn, result))


class RunnerLoopTests(unittest.TestCase):

    def test_turns_run_and_report_until_the_session_ends(self):
        turn = {"id": "t1", "payload": {"request": {"stage": "plan", "provider": "claude"}}}
        client = FakeRunner([[turn], []])
        loop = runner.Loop(client, "sk-agw-run", IMAGE, object(), tempfile.mkdtemp(), 2, 3600, turns.Limits())
        with patch.object(turns, "execute", return_value={"outcome": "ok", "response": {}}) as execute, \
                patch.object(runner, "HEARTBEAT", 0):
            loop.serve()
        self.assertEqual(execute.call_args.args[:3], (turn["payload"], IMAGE, "sk-agw-run"))
        self.assertEqual(client.completed, [("t1", {"outcome": "ok", "response": {}})])

    def test_a_turn_that_cannot_run_reports_a_failure(self):
        turn = {"id": "t2", "payload": {"request": {"stage": "plan", "provider": "codex"}}}
        client = FakeRunner([[turn]])
        loop = runner.Loop(client, "k", IMAGE, object(), tempfile.mkdtemp(), 1, 3600, turns.Limits())
        with patch.object(turns, "execute", side_effect=OSError("disk full")), patch.object(runner, "HEARTBEAT", 0):
            loop.serve()
        self.assertEqual(client.completed[0][1]["outcome"], "failed")

    def test_an_idle_session_ends(self):
        client = FakeRunner([[], [], []])
        loop = runner.Loop(client, "k", IMAGE, object(), tempfile.mkdtemp(), 1, -1, turns.Limits())
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


class RelayEndToEndTests(Workspace):
    """The controller's agents, the real exchange over HTTP, the runner loop and a real workspace."""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gateway/tests"))
        import test_exchange  # puts the exchange on the module path
        import exchange as exchange_module
        self.signer = test_exchange.Signer(str(self.tmp))
        self.exchange = exchange_module.Exchange(str(self.tmp / "exchange.db"), test_exchange.FakeGateway(),
                                                 exchange_module.Keys(fetch=lambda: {"keys": [self.signer.jwk]}),
                                                 test_exchange.AUDIENCE)
        self.server = exchange_module.server(self.exchange, "127.0.0.1:0", "operator-secret")
        import threading
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.expected = dict(test_exchange.EXPECTED)

    def test_a_develop_turn_round_trips_through_a_runner_session(self):
        import threading
        from relay import Operator, Runner
        loops = []

        def dispatch(session):
            def serve():
                client = Runner(self.url, session)
                grant = client.key(self.signer.token())
                loop = runner.Loop(client, grant["key"], IMAGE, self.mirror, self.tmp / "runner", 1, 0, turns.Limits())
                loop.serve()
                client.end()
            thread = threading.Thread(target=serve)
            thread.start()
            loops.append(thread)

        # The runner's agent: the stand-in container script, with the key the exchange minted.
        def fake(name, image, directory, mirror, kept, limits):
            return [sys.executable, "-c", AGENT.replace('"sk-agw-test"', 'os.environ["CONCURRO_GATEWAY_KEY"]'),
                    str(directory)]

        executor = RunnerExecutor(Operator(self.url, "operator-secret"), GATEWAY, self.expected, ["gpt-*"], 1000, dispatch)
        agents = Agents({**CONFIG, "models": MODELS, "base": "main"}, executor, retained=self.tmp / "sessions")
        context = {"bundle_id": agents.bundle.identity, "repository_commit": self.base,
                   "repository": {"refs": {"main": self.base}, "head": "main"},
                   "controller_candidate": {"src/lib.rs": "fn main() {}\n"}}
        with patch.object(turns, "command", side_effect=fake), patch.object(turns.cleanup, "remove"), \
                patch.object(runner, "HEARTBEAT", 1):
            result = agents.ask("codex", "develop", context)
            for thread in loops:
                thread.join(30)
        self.assertEqual(result["edits"], [{"path": "docs/guide.md", "content": "Guide\nEdited\n"},
                                           {"path": "docs/new.md", "content": "New\n"}])
        self.assertEqual(len(loops), 1, "one session served the target")
        self.assertEqual(self.exchange.gateway.list(), [], "the session's key is gone once its runner ended it")
