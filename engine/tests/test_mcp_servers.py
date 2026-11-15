"""Target MCP servers are validated once, sent with the turn and rendered for each CLI."""

import json
import unittest

import yaml

from support import CONFIG, ROOT, TARGET, setUpModule as setUpModule, tearDownModule as tearDownModule

import mcp_config
import runner_layout
from agent_entry import command_for
from agents import Agents
from controller import config_digest, validate_config
from fakes import named_config
from policy import Blocked
from runner_fixtures import GATEWAY, FakeExecutor

SERVERS = {
    "lodestone": {"transport": "stdio", "command": "lode", "args": ["mcp"]},
    "rust-analyzer": {
        "transport": "stdio",
        "command": "rust-analyzer-mcp",
        "args": ["--root={workspace}"],
        "env": {"RA_LOG": "error"},
    },
}
NORMALIZED = {
    "lodestone": {"command": "lode", "args": ["mcp"], "env": {}},
    "rust-analyzer": {"command": "rust-analyzer-mcp", "args": ["--root={workspace}"], "env": {"RA_LOG": "error"}},
}


def configured(servers=SERVERS):
    return {**named_config(), "mcp": {"servers": servers}}


class ConfigurationTests(unittest.TestCase):
    """The optional map accepts stdio launches and refuses anything it would not render."""

    def test_servers_are_normalized_and_bound_to_the_configuration(self):
        config = configured()
        validate_config(config)
        self.assertEqual(mcp_config.servers(config), NORMALIZED)
        self.assertNotEqual(config_digest(config), config_digest(named_config()))

    def test_configurations_without_servers_stay_valid(self):
        for config in (CONFIG, named_config(), configured({})):
            validate_config(config)
            self.assertEqual(mcp_config.servers(config), {})

    def test_malformed_servers_block_the_configuration(self):
        server = SERVERS["lodestone"]
        for mcp in (
            [],
            {},
            {"servers": []},
            {"servers": {}, "catalog": {}},
            {"servers": {"bad name": server}},
            {"servers": {7: server}},
            {"servers": {"lodestone": "lode mcp"}},
            {"servers": {"lodestone": {"command": "lode"}}},
            {"servers": {"lodestone": {**server, "transport": "http"}}},
            {"servers": {"lodestone": {**server, "url": "http://127.0.0.1:4600/mcp"}}},
            {"servers": {"lodestone": {**server, "command": " "}}},
            {"servers": {"lodestone": {**server, "args": "mcp"}}},
            {"servers": {"lodestone": {**server, "args": [1]}}},
            {"servers": {"lodestone": {**server, "env": {"A-B": "x"}}}},
            {"servers": {"lodestone": {**server, "env": {"A": 1}}}},
            {"servers": {"lodestone": {**server, "env": {7: "x"}}}},
        ):
            with self.subTest(mcp=mcp), self.assertRaises(Blocked):
                validate_config({**named_config(), "mcp": mcp})


class TurnTests(unittest.TestCase):
    """Only targets that declare servers change their turn requests."""

    def request(self, config):
        executor = FakeExecutor()
        agents = Agents(config, executor)
        context = {
            "bundle_id": agents.bundle.identity,
            "repository_commit": "a" * 40,
            "repository": {"refs": {"main": "a" * 40}, "head": "main"},
        }
        agents.ask("opus-high", "review", context)
        return executor.turns[-1][0]["request"]

    def test_declared_servers_travel_with_the_turn(self):
        self.assertEqual(self.request(configured())["mcp_servers"], NORMALIZED)

    def test_other_targets_keep_their_request_shape(self):
        for config in (named_config(), configured({})):
            self.assertNotIn("mcp_servers", self.request(config))


class CommandTests(unittest.TestCase):
    """Each CLI receives the same servers, with the turn's checkout substituted inside one argument."""

    WORKSPACE = "/runner/turn 1/work"
    BOUND = {**NORMALIZED, "rust-analyzer": {**NORMALIZED["rust-analyzer"], "args": ["--root=" + WORKSPACE]}}

    def command(self, provider, **extra):
        request = {"provider": provider, "model": "m", "schema": {}, "gateway": GATEWAY, **extra}
        return command_for({**request, "workspace": self.WORKSPACE, "mcp_servers": NORMALIZED}, "schema", "out")

    def test_claude_loads_only_the_declared_servers(self):
        for extra in ({}, {"resume_id": "thread", "session": {"turn": 1}}):
            command = self.command("claude", **extra)
            position = command.index("--mcp-config")
            self.assertEqual(json.loads(command[position + 1]), {"mcpServers": self.BOUND})
            # The option takes several values; the next flag ends them.
            self.assertEqual(command[position + 2], "--strict-mcp-config")

    def test_codex_receives_one_override_per_server(self):
        for extra in ({}, {"resume_id": "thread", "session": {"turn": 1}}):
            command = self.command("codex", **extra)
            overrides = [command[i + 1] for i, c in enumerate(command) if c == "-c"]
            self.assertIn('mcp_servers.lodestone={"command" = "lode", "args" = ["mcp"], "env" = {}}', overrides)
            self.assertIn(
                'mcp_servers.rust-analyzer={"command" = "rust-analyzer-mcp", '
                '"args" = ["--root=/runner/turn 1/work"], "env" = {"RA_LOG" = "error"}}',
                overrides,
            )
            self.assertIn("--ignore-user-config", command)
            self.assertEqual(command[-1], "-")


class ExampleIndexTests(unittest.TestCase):
    """The example's warmed lodestone index is one a role turn finds: same image, checkout path and caches."""

    WORKFLOWS = TARGET / ".github/workflows"
    WARMER = yaml.safe_load((WORKFLOWS / "lodestone-index.yml").read_text())["jobs"]["index"]

    def caches(self, steps):
        return {(s["with"]["path"], s["with"]["key"]) for s in steps if s.get("uses", "").startswith("actions/cache")}

    def test_the_warmer_indexes_the_path_turns_check_out_at(self):
        launcher = yaml.safe_load((ROOT / ".github/workflows/agent.yml").read_text())["jobs"]["role"]["steps"]
        serve = next(s["run"] for s in launcher if 'runner.py" serve' in s.get("run", ""))
        self.assertIn('--root "$RUNNER_TEMP/concurro-session"', serve)
        work = next(s["env"]["WORK"] for s in self.WARMER["steps"] if "WORK" in s.get("env", {}))
        turn = f"{runner_layout.TURN}/{runner_layout.WORKSPACE}"
        self.assertEqual(work, "${{ runner.temp }}/concurro-session/turns/" + turn)
        caller = yaml.safe_load((self.WORKFLOWS / "concurro-executor.yml").read_text())["jobs"]["role"]
        self.assertEqual(self.WARMER["runs-on"], caller["with"]["runs-on"])

    def test_setup_restores_exactly_what_the_warmer_saves(self):
        setup = yaml.safe_load((TARGET / ".concurro/setup/action.yml").read_text())["runs"]["steps"]
        self.assertEqual(len(self.caches(setup)), 2)
        self.assertEqual(self.caches(setup), self.caches(self.WARMER["steps"]))


if __name__ == "__main__":
    unittest.main()
