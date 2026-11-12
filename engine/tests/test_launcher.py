"""The launcher workflow against the real exchange: it runs the engine its session names, whatever its own commit."""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "engine"), str(ROOT / "gateway/tests")]
import exchange_fixtures  # puts the exchange on the module path

import exchange as exchange_module
import runner
from mirror import git_environment

WORKFLOW = yaml.load((ROOT / ".github/workflows/agent.yml").read_text(), Loader=yaml.BaseLoader)
STEPS = {step["name"]: step for step in WORKFLOW["jobs"]["role"]["steps"]}


class Issuer(BaseHTTPRequestHandler):
    """GitHub's OIDC token endpoint for one job; `server.mint` signs each requested token."""

    def do_GET(self):
        body = json.dumps({"value": self.server.mint()}).encode()
        self.server.requests.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class LauncherTests(unittest.TestCase):
    """Run the workflow's own step scripts with GitHub, the token endpoint and the exchange local."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.tmp = Path(directory.name).resolve()
        self.signer = exchange_fixtures.Signer(str(self.tmp))
        self.exchange = exchange_module.Exchange(
            str(self.tmp / "exchange.db"),
            exchange_fixtures.FakeGateway(),
            exchange_module.Keys(fetch=lambda: {"keys": [self.signer.jwk]}),
            exchange_fixtures.AUDIENCE,
        )
        self.gateway = self.serve(exchange_module.server(self.exchange, "127.0.0.1:0", "operator-secret"))
        issuer = ThreadingHTTPServer(("127.0.0.1", 0), Issuer)
        issuer.requests, issuer.mint = [], lambda: self.signer.token(**self.claims)
        self.issuer = issuer
        self.oidc = self.serve(issuer)
        # The engine repository as GitHub serves it: the deployed engine, then a later launcher commit.
        self.remote = self.tmp / "github/concurro/concurro.git"
        work = self.tmp / "work"
        shutil.copytree(ROOT / "engine", work / "engine", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        self.git("init", "--quiet", "--initial-branch=main", cwd=work)
        self.engine = self.commit(work, "engine")
        (work / "launcher").write_text("changed after the engine was deployed\n")
        self.launcher = self.commit(work, "launcher")
        self.git("clone", "--quiet", "--bare", str(work), str(self.remote), cwd=self.tmp)
        self.git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=self.remote)
        binding = {**exchange_fixtures.BINDING, "engine": self.engine}
        self.session = self.exchange.issue(dict(exchange_fixtures.EXPECTED), ["claude-*"], 1000, 3600, binding=binding)
        self.claims = {"job_workflow_sha": self.launcher, "ref": "refs/heads/concurro/runs/" + self.session}
        self.temp = self.tmp / "runner"
        self.temp.mkdir()
        self.addCleanup(self.stop_helper)

    def serve(self, server):
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def git(self, *args, cwd):
        return subprocess.run(
            ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            env=git_environment(),
        ).stdout.strip()

    def commit(self, work, message):
        self.git("add", "--all", cwd=work)
        self.git("commit", "--quiet", "--message", message, cwd=work)
        return self.git("rev-parse", "HEAD", cwd=work)

    def step(self, name):
        """Run one workflow step's script as the runner would."""
        env = {
            "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
            "HOME": str(self.tmp),
            "RUNNER_TEMP": str(self.temp),
            "GATEWAY": self.gateway,
            "SESSION": self.session,
            "ACTIONS_ID_TOKEN_REQUEST_URL": self.oidc + "/token?api-version=2",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.file://{self.tmp}/github/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://github.com/",
        }
        return subprocess.run(
            ["bash", "-c", STEPS[name]["run"]], env=env, cwd=self.tmp, capture_output=True, text=True, timeout=60
        )

    def stop_helper(self):
        pid = self.temp / "concurro-session/heartbeat.pid"
        if pid.exists():
            try:
                os.kill(int(pid.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass

    def checked_out(self):
        return self.git("rev-parse", "HEAD", cwd=self.temp / "concurro")

    def refusal(self):
        return self.exchange.db.execute("SELECT refusal FROM sessions WHERE id = ?", (self.session,)).fetchone()[0]

    def test_a_launcher_at_another_commit_runs_the_sessions_engine(self):
        loaded = self.step("Load Concurro runtime")
        self.assertEqual(loaded.returncode, 0, loaded.stderr)
        self.assertNotEqual(self.launcher, self.engine)
        self.assertEqual(self.checked_out(), self.engine)
        self.assertTrue(all("audience=concurro-gateway" in path for path in self.issuer.requests))
        claimed = self.step("Claim agent session")
        self.assertEqual(claimed.returncode, 0, claimed.stderr)
        grant = json.loads((self.temp / "concurro-session/grant.json").read_text())
        self.assertEqual(grant["binding"]["engine"], self.engine)
        self.assertNotIn(grant["token"], loaded.stdout + claimed.stdout + claimed.stderr)
        closed = self.step("Close agent session")
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertTrue(self.exchange.row(self.session)["ended"])

    def test_another_workflow_learns_no_engine_and_loads_nothing(self):
        for name, claims in (
            ("workflow", {"job_workflow_ref": "x/y/.github/workflows/agent.yml@refs/heads/main"}),
            ("repository", {"repository_id": "9"}),
            ("session ref", {"ref": "refs/heads/main"}),
        ):
            with self.subTest(name=name):
                self.claims = {**self.claims, **claims}
                loaded = self.step("Load Concurro runtime")
                self.assertNotEqual(loaded.returncode, 0)
                self.assertFalse((self.temp / "concurro/engine").exists())
                self.assertNotIn(self.engine, loaded.stdout + loaded.stderr)

    def test_a_runner_on_another_engine_is_refused_the_session(self):
        """A launcher that loaded its own commit, as before sessions named the engine, cannot claim."""
        self.git("clone", "--quiet", str(self.remote), str(self.temp / "concurro"), cwd=self.tmp)
        self.assertEqual(self.checked_out(), self.launcher)
        claimed = self.step("Claim agent session")
        self.assertNotEqual(claimed.returncode, 0)
        self.assertFalse((self.temp / "concurro-session/grant.json").exists())
        self.assertIn("runner engine differs", self.refusal())

    def test_every_launcher_command_is_one_the_engine_accepts(self):
        """The launcher outlives engine releases: its calls stay within the runner's command line."""
        calls = [step["run"] for step in STEPS.values() if "engine/runner.py" in step.get("run", "")]
        commands = set()
        for script in calls:
            for call in script.split('python3 "$RUNNER_TEMP/concurro/engine/runner.py"')[1:]:
                words = call.replace("\\\n", " ").split("\n")[0].split()
                options = [word for word in words if word.startswith("--")]
                values = [value for option in options for value in (option, "/x")]
                parsed = runner.arguments().parse_args([words[0], *values])
                commands.add(parsed.command)
        self.assertEqual(commands, {"claim", "prepare", "serve", "end", "summary"})
        self.assertIn('< "$RUNNER_TEMP/concurro/engine/agent-clients.txt"', STEPS["Install agent clients"]["run"])
        clients = (ROOT / "engine/agent-clients.txt").read_text().split()
        self.assertEqual(
            [client.rsplit("@", 1)[0] for client in clients], ["@openai/codex", "@anthropic-ai/claude-code"]
        )


if __name__ == "__main__":
    unittest.main()
