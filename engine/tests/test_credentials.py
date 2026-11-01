"""Credential identities are chosen before secrets are read and recorded without material."""

import json
import unittest
from unittest.mock import patch

from support import CONFIG, setUpModule, tearDownModule  # unittest module fixtures
from agents import Agents
from credentials import Credential, EnvironmentCredentials
from policy import Blocked
import test_pipeline


class EnvironmentCredentialTests(unittest.TestCase):
    """The v1 resolver selects the provider's only configured credential."""

    def test_resolution_is_unique_and_secret_free(self):
        """Exactly one configured variable per provider; identities never carry material."""
        credentials = EnvironmentCredentials({"CLAUDE_CODE_OAUTH_TOKEN": "secret-oauth", "CODEX_API_KEY": "secret-key"})
        claude = credentials.resolve("claude", "plan", None)
        self.assertEqual(claude, Credential("env:CLAUDE_CODE_OAUTH_TOKEN", "claude", "org", "long-lived-token"))
        self.assertEqual(credentials.resolve("codex", "review", None).adapter, "direct-api-key")
        self.assertNotIn("secret", json.dumps(claude.record()))
        self.assertEqual(credentials.material(claude), {"CLAUDE_CODE_OAUTH_TOKEN": "secret-oauth"})
        for environ in ({}, {"CLAUDE_CODE_OAUTH_TOKEN": "a", "ANTHROPIC_API_KEY": "b"}):
            with self.subTest(environ=sorted(environ)), self.assertRaisesRegex(Blocked, "exactly one"):
                EnvironmentCredentials(environ).resolve("claude", "plan", None)
        with self.assertRaises(Blocked):
            credentials.resolve("unknown", "plan", None)

    def test_material_only_for_the_current_selection(self):
        """A forged, foreign or replaced selection never yields another secret."""
        environ = {"CODEX_API_KEY": "secret-key"}
        credentials = EnvironmentCredentials(environ)
        selected = credentials.resolve("codex", "develop", None)
        for forged in (Credential("env:CLAUDE_CODE_OAUTH_TOKEN", "codex", "org", "direct-api-key"),
                       Credential("env:CODEX_API_KEY", "codex", "contributed", "direct-api-key"),
                       Credential("env:CODEX_API_KEY", "claude", "org", "direct-api-key")):
            with self.subTest(forged=forged), self.assertRaises(Blocked):
                credentials.material(forged)
        environ.clear()
        with self.assertRaises(Blocked):
            credentials.material(selected)

    def test_transport_identity_is_strict(self):
        """Only the exact identity fields and known kinds are accepted from transport."""
        record = Credential("env:CODEX_API_KEY", "codex", "org", "direct-api-key").record()
        self.assertEqual(Credential.parse(record).record(), record)
        for bad in ({**record, "secret": "x"}, {**record, "kind": "shared"}, {**record, "id": ""},
                    {k: v for k, v in record.items() if k != "account"}, "env:CODEX_API_KEY"):
            with self.subTest(bad=bad), self.assertRaises(Blocked):
                Credential.parse(bad)


class AgentCredentialTests(unittest.TestCase):
    """The transport passes only the selected secret, by name, and never in the payload."""

    def ask(self, credentials, selected):
        """Run one patched provider call and return the Docker invocation."""
        agents = Agents(CONFIG, archive_loader=lambda commit: b"", credentials=credentials)
        context = {"bundle_id": agents.bundle.identity, "repository_commit": "a" * 40}
        if selected is not None:
            context["controller_credential"] = selected.record()
        with patch.dict("os.environ", {"CONCURRO_AGENT_IMAGE": "sha256:" + "a" * 64}), \
                patch("agents.inspection", return_value=("a" * 40, {})), patch("agents.Snapshot") as snapshot, \
                patch("agents.subprocess.run") as run:
            snapshot.return_value.__enter__.return_value.mount.return_value = "type=volume,source=x,target=/repo"
            run.return_value.returncode = 0
            run.return_value.stdout = "{}"
            agents.ask("codex", "review", context)
        return run.call_args_list[0]

    def test_selected_secret_reaches_only_the_container_environment(self):
        """The secret is named on the command line and valued only in Docker's environment."""
        credentials = EnvironmentCredentials({"CODEX_API_KEY": "secret-key", "CLAUDE_CODE_OAUTH_TOKEN": "other"})
        call = self.ask(credentials, credentials.resolve("codex", "review", None))
        command, payload = call.args[0], call.kwargs["input"]
        self.assertEqual(command[command.index("--env") + 1], "CODEX_API_KEY")
        self.assertEqual(command.count("--env"), 1)
        self.assertEqual(call.kwargs["env"]["CODEX_API_KEY"], "secret-key")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", call.kwargs["env"])
        self.assertNotIn("secret-key", payload + " ".join(command))
        self.assertNotIn("controller_credential", json.loads(payload)["context"])

    def test_foreign_provider_selection_is_rejected(self):
        """A selection made for one provider cannot run another provider's call."""
        credentials = EnvironmentCredentials({"CODEX_API_KEY": "k", "CLAUDE_CODE_OAUTH_TOKEN": "t"})
        with self.assertRaisesRegex(Blocked, "another provider"):
            self.ask(credentials, credentials.resolve("claude", "review", None))


class ControllerCredentialTests(unittest.TestCase):
    """The controller selects per call and records identities, not secrets, in audit state."""

    # Reuse the pipeline fixture without re-running its tests.
    pipeline = test_pipeline.PipelineTests
    setUp, tick, ready = pipeline.setUp, pipeline.tick, pipeline.ready

    def test_lifecycle_records_credential_usage(self):
        """Every debited call is attributed to the selected credential identity."""
        self.ready()
        state = self.tick()
        self.assertEqual(set(state["credentials"]), {"fixture:claude", "fixture:codex"})
        self.assertEqual(sum(v["calls"] for v in state["credentials"].values()), state["calls"])
        self.assertEqual(state["credentials"]["fixture:codex"],
                         {"provider": "codex", "kind": "org", "adapter": "direct-api-key", "account": None,
                          "calls": state["credentials"]["fixture:codex"]["calls"]})
        self.assertTrue(self.agents.calls)
        self.assertTrue(all(c["controller_credential"]["provider"] == p for p, _, c in self.agents.calls))


if __name__ == "__main__":
    unittest.main()
