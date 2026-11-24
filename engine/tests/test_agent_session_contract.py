"""Pin effort defaults and session journal semantics at the native entry point."""

import fcntl
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import TARGET

import agent_entry
import model_config
import turns
from bundle import Bundle
from policy import digest
from session_entry import SessionTurn


class EffortTests(unittest.TestCase):
    """Explicit efforts and legacy defaults keep their CLI-specific effects."""

    def test_effort_table(self):
        for provider, default in (("claude", "medium"), ("codex", "low")):
            for extra, expected in (({}, default), ({"effort": "high"}, "high")):
                with self.subTest(provider=provider, extra=extra):
                    request = {
                        "provider": provider,
                        "model": "model",
                        "schema": {},
                        "gateway": {"url": "https://gateway.invalid"},
                        **extra,
                    }
                    command = agent_entry.command_for(request, "schema", "output")
                    with patch.dict(os.environ, {"CONCURRO_GATEWAY_KEY": "test"}, clear=True):
                        environment = agent_entry.provider_environment(request)
                    if provider == "claude":
                        self.assertEqual(command[command.index("--effort") + 1], expected)
                        self.assertEqual(
                            environment,
                            {
                                "ANTHROPIC_BASE_URL": "https://gateway.invalid",
                                "ANTHROPIC_AUTH_TOKEN": "test",
                                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                                "CLAUDE_CODE_EFFORT_LEVEL": expected,
                            },
                        )
                    else:
                        self.assertIn("model_reasoning_effort=" + json.dumps(expected), command)
                        self.assertEqual(environment, {})
                    # Legacy config/reviewer fields do not select an effort; profiles do.
                    resolved = model_config.resolve({"models": {provider: "model"}, **extra}, provider, "plan", extra)
                    self.assertEqual(resolved.profile.effort, default)

    def test_gateway_key_exports(self):
        self.assertEqual(agent_entry.KEY_VARIABLE, "CONCURRO_GATEWAY_KEY")
        self.assertEqual(turns.KEY_VARIABLE, "CONCURRO_GATEWAY_KEY")


class SessionEntryTests(unittest.TestCase):
    """Use real journals and flock, retaining objects so GC cannot hide leaked locks."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.addCleanup(os.chdir, Path.cwd())
        self.enterContext(patch.object(agent_entry.os, "umask"))
        self.bundle = Bundle(target=TARGET)
        self.opened = []
        self.pending = []

        def capture(*args):
            turn = SessionTurn(*args)
            self.opened.append(turn)
            self.addCleanup(turn.close)
            return turn

        self.enterContext(patch.object(agent_entry, "SessionTurn", capture))

    def request(self, provider):
        """An isolated retained role turn with the production bundle binding."""
        root = self.root / provider
        root.mkdir(exist_ok=True)
        return {
            "provider": provider,
            "model": "test-model",
            "stage": "plan",
            "timeout": 60,
            "session": {"scope": "a" * 64, "turn": 0},
            "workspace": str(root),
            "scratch": str(root),
            "retained_root": str(root),
            "gateway": {"url": "https://gateway.invalid"},
            "charter": self.bundle.charter_text,
            "instruction_texts": self.bundle.instruction_texts,
            "engine_resources": {p: text for p, text in self.bundle.resources.items() if p.startswith("bundle/")},
            "bundle_id": self.bundle.identity,
            "context": {"repository_commit": "b" * 40, "repository": {"refs": {}, "head": "main"}},
        }

    def assert_lock(self, root, held):
        """Probe with an independent descriptor, without reading or rewriting the journal."""
        with (root / "turn.lock").open("a") as lock:
            if held:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def invoke(self, provider="claude", error=None):
        """Run main with a local provider stand-in that checks the paid-call lock boundary."""
        request = self.request(provider)
        root = Path(request["workspace"])

        def run(*args, **kwargs):
            self.assert_lock(root, True)
            self.assertNotIn("response_schema", json.loads(kwargs["input"]))
            command = args[0]
            native_schema = (
                json.loads(Path(command[command.index("--output-schema") + 1]).read_text())
                if provider == "codex"
                else json.loads(command[command.index("--json-schema") + 1])
            )
            self.assertEqual(native_schema, self.bundle.schema(request["stage"]))
            self.assertEqual(self.opened[-1].prompt["response_schema"], native_schema)
            raw = (root / "turn.json").read_bytes()
            self.assertEqual(raw, b'{"scope": "' + b"a" * 64 + b'", "status": "pending"}')
            self.pending.append(raw)
            if error is not None:
                raise error
            identifier = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            if provider == "codex":
                (root / "result.json").write_text('{"answer": true}')
                stdout = json.dumps({"type": "thread.started", "thread_id": identifier})
            else:
                stdout = json.dumps(
                    {"subtype": "success", "session_id": identifier, "structured_output": {"answer": True}}
                )
            return SimpleNamespace(returncode=0, stdout=stdout)

        environment = {name: str(root / name) for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "COPILOT_HOME")}
        environment["CONCURRO_GATEWAY_KEY"] = "test-key"
        output = io.StringIO()
        with (
            patch.dict(os.environ, environment),
            patch.object(agent_entry.sys, "stdin", io.StringIO(json.dumps(request))),
            patch.object(agent_entry.sys, "stdout", output),
            patch.object(agent_entry.subprocess, "run", side_effect=run),
        ):
            agent_entry.main()
        return output.getvalue()

    def test_runtime_bundle_bytes_reach_native_runner_without_checkout_change(self):
        """The exact task bundle controls execution and its retained journal after a file update."""
        resources = {p: text for p, text in self.bundle.resources.items() if p.startswith("bundle/")}
        resources["bundle/instructions.md"] = "Frozen runtime instructions, different from the engine checkout."
        self.bundle = Bundle(
            root=self.bundle.root,
            charter_text=self.bundle.charter_text,
            instruction_texts=self.bundle.instruction_texts,
            engine_resources=resources,
        )
        self.assertEqual(self.invoke("codex"), '{"answer": true}\n')
        turn = self.opened[-1]
        self.assertEqual(turn.prompt["instructions"]["shared"], resources["bundle/instructions.md"])
        self.assertEqual(
            json.loads(turn.path.read_text())["prompt"]["instructions"]["shared"], resources["bundle/instructions.md"]
        )

    def test_legacy_controller_wording_is_independent_of_operator_bundle_directory(self):
        """An old frozen bundle needs no extra compatibility file from its operator."""
        resources = {p: text for p, text in self.bundle.resources.items() if p.startswith("bundle/")}
        resources.pop("bundle/controller.json")
        manifest = json.loads(resources["bundle/manifest.json"])
        manifest["resources"].remove("bundle/controller.json")
        resources["bundle/manifest.json"] = json.dumps(manifest, indent=2) + "\n"
        with tempfile.TemporaryDirectory() as operator_directory:
            legacy = Bundle(
                root=operator_directory,
                charter_text=self.bundle.charter_text,
                instruction_texts=self.bundle.instruction_texts,
                engine_resources=resources,
            )
            self.assertFalse((Path(operator_directory) / "legacy-controller.json").exists())
            self.assertIn("advisory concerns", legacy.controller_instruction("intake_correction"))
            self.assertEqual(
                legacy.controller_instruction("feedback_files"), self.bundle.controller_instruction("feedback_files")
            )

    def test_success_journal_and_release(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                self.assertEqual(self.invoke(provider), '{"answer": true}\n')
                turn = self.opened[-1]
                self.assertTrue(turn.lock.closed)
                self.assert_lock(turn.root, False)
                expected = {
                    "scope": "a" * 64,
                    "status": "complete",
                    "turn": 0,
                    "request": digest(turn.prompt),
                    "identifier": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "prompt": turn.prompt,
                    "response": {"answer": True},
                }
                self.assertEqual(turn.path.read_bytes(), json.dumps(expected).encode())

    def test_cached_response_preserves_journal_and_releases(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                self.invoke(provider)
                raw = self.opened[-1].path.read_bytes()
                count = len(self.pending)
                self.assertEqual(self.invoke(provider), '{"answer": true}\n')
                turn = self.opened[-1]
                self.assertEqual(len(self.pending), count, "cached response makes no provider call")
                self.assertTrue(turn.lock.closed)
                self.assert_lock(turn.root, False)
                self.assertEqual(turn.path.read_bytes(), raw)

    def test_provider_failure_keeps_pending_bytes_and_close_only_releases(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                error = RuntimeError("provider failed")
                with self.assertRaises(RuntimeError) as caught:
                    self.invoke(provider, error)
                self.assertIs(caught.exception, error)
                turn = self.opened[-1]
                self.assertEqual(turn.path.read_bytes(), self.pending[-1])
                with patch.object(turn, "write", side_effect=AssertionError("close must not write")):
                    turn.close()
                self.assert_lock(turn.root, False)
                self.assertEqual(turn.path.read_bytes(), self.pending[-1])

    def test_lock_contention_preserves_journal(self):
        root = self.root / "locked"
        root.mkdir()
        session, prompt = {"scope": "a" * 64, "turn": 0}, '{"context": "test"}'
        turn = SessionTurn(session, prompt, root)
        try:
            turn.start()
            raw = turn.path.read_bytes()
            with self.assertRaises(BlockingIOError):
                SessionTurn(session, prompt, root)
            self.assert_lock(root, True)
            self.assertEqual(turn.path.read_bytes(), raw)
        finally:
            turn.close()
        self.assert_lock(root, False)
        self.assertEqual(turn.path.read_bytes(), raw)

    def test_failure_releases_without_waiting_for_process_exit(self):
        error = subprocess.TimeoutExpired("provider", 60)
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            self.invoke(error=error)
        self.assertIs(caught.exception, error)
        turn = self.opened[-1]
        self.assertTrue(turn.lock.closed)
        self.assert_lock(turn.root, False)
        self.assertEqual(turn.path.read_bytes(), self.pending[-1])
