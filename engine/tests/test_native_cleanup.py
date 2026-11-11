"""Pin native execution results and cleanup order before splitting the turn lifecycle."""

import json
import signal
import subprocess
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support  # noqa: F401 -- initializes the engine test path

import processes
import turns


class NativeCleanupTests(unittest.TestCase):
    """Observe execute at the process boundary without invoking a provider or platform reaper."""

    def execute(
        self,
        stage="develop",
        code=0,
        stopped=None,
        timeout=60,
        communicate_error=None,
        finish_error=None,
        kill_error=None,
    ):
        """Run the real workspace lifecycle with a traced child and portable temporary files."""
        trace = self.trace = []
        process = Mock(pid=12345, returncode=code)
        process.poll.return_value = None if stopped is not None and stopped.is_set() else code
        process.wait.side_effect = lambda: trace.append("wait")

        def communicate(payload, *, timeout):
            trace.append(("communicate", timeout))
            self.payload = json.loads(payload)
            if communicate_error:
                raise communicate_error
            return '{"answer": true}', None

        def finish(baseline):
            trace.append(("finish", baseline))
            if finish_error:
                raise finish_error

        def kill(pid, sig):
            trace.append(("kill", pid, sig))
            if kill_error:
                raise kill_error

        class Timer:
            """Execute cancellation deterministically, retaining thread start/join ordering."""

            def __init__(self, *, target, daemon):
                trace.append(("thread", daemon))
                self.target = target

            def start(self):
                trace.append("start")
                self.target()

            def join(self, timeout):
                trace.append(("join", timeout))

        class Child:
            """Record the subprocess context lifetime around cleanup."""

            def __enter__(self):
                trace.append("enter")
                return process

            def __exit__(self, *_):
                trace.append("exit")

        def popen(*args, **kwargs):
            trace.append("popen")
            self.popen_args = args, kwargs
            return Child()

        def snapshot(spec, *, root, mirror):
            (root / ".git").mkdir()

        process.communicate.side_effect = communicate
        mirror = SimpleNamespace(path="unused", borrow=Mock(), release=lambda name: trace.append("release"))
        turn = {
            "request": {"stage": stage},
            "spec": {
                "base_branch": "main",
                "refs": {"main": "a" * 40},
                "head": "main",
                "candidate": None,
            },
            "timeout": timeout,
        }
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = turns.directory(directory)
            for target, name, replacement in (
                (turns, "snapshot", snapshot),
                (turns, "command", lambda: trace.append("command") or ["fake-agent"]),
                (turns, "agent_environment", lambda *args: trace.append("environment") or {"TEST": "env"}),
                (turns, "extract", lambda *args: trace.append("extract") or {"edits": []}),
                (processes, "begin", lambda: trace.append("begin") or {7}),
                (processes, "finish", finish),
                (subprocess, "Popen", popen),
                (threading, "Thread", Timer),
                (processes.os, "killpg", kill),
            ):
                stack.enter_context(patch.object(target, name, replacement))
            return turns.execute(turn, "test-key", mirror, root, stopped)

    def test_exit_result_table(self):
        for stage in ("develop", "review", "plan", "plan_qc"):
            for code, ordinary in (
                (0, "ok"),
                (76, "timed_out"),
                (-9, "uncertain"),
                (137, "uncertain"),
                (-2, "failed"),
                (-15, "failed"),
                (130, "failed"),
                (143, "failed"),
                (75, "uncertain"),
                (1, "failed"),
            ):
                with self.subTest(stage=stage, code=code):
                    bound_signal = stage in ("develop", "review") and code in (-9, 137, -2, -15, 130, 143)
                    expected = "terminated" if bound_signal else ordinary
                    result = self.execute(stage=stage, code=code)
                    reason = "process_signal" if bound_signal else "turn_timeout" if code == 76 else None
                    self.assertEqual((result["outcome"], result.get("termination_reason")), (expected, reason))
                    self.assertEqual("elapsed_seconds" in result, reason is not None)
                    self.assertEqual(result["response"], {"answer": True} if expected == "ok" else None)
                    self.assertEqual("extract" in self.trace, expected == "ok" and stage == "develop")

    def test_stopped_precedes_timeout_and_signal(self):
        for stage in ("develop", "review", "plan_qc"):
            for code in (0, 76, -9, -15):
                with self.subTest(stage=stage, code=code):
                    stopped = threading.Event()
                    stopped.set()
                    result = self.execute(stage=stage, code=code, stopped=stopped)
                    if stage != "plan_qc":
                        self.assertEqual(
                            (result["outcome"], result["termination_reason"]), ("terminated", "runner_stopped")
                        )
                        self.assertIn("elapsed_seconds", result)
                    elif code == 76:
                        self.assertEqual(
                            (result["outcome"], result["termination_reason"]), ("timed_out", "runner_stopped")
                        )
                    else:
                        self.assertNotIn("termination_reason", result)
                        self.assertNotIn("elapsed_seconds", result)

    def test_cleanup_order_and_timeout_arguments(self):
        for timeout, wait in ((60, 120), (7200, 7260), (None, None)):
            with self.subTest(timeout=timeout):
                self.execute(stopped=threading.Event(), timeout=timeout)
                self.assertEqual(
                    self.trace,
                    [
                        "begin",
                        "command",
                        "environment",
                        "popen",
                        "enter",
                        ("thread", True),
                        "start",
                        ("communicate", wait),
                        ("kill", 12345, signal.SIGKILL),
                        "wait",
                        ("join", 2),
                        ("finish", {7}),
                        "exit",
                        "extract",
                        "release",
                    ],
                )
                self.assertEqual(self.payload["timeout"], timeout)
                args, kwargs = self.popen_args
                self.assertEqual(args, (["fake-agent"],))
                self.assertEqual(
                    kwargs,
                    {
                        "stdin": subprocess.PIPE,
                        "stdout": subprocess.PIPE,
                        "stderr": subprocess.DEVNULL,
                        "text": True,
                        "env": {"TEST": "env"},
                        "start_new_session": True,
                    },
                )

    def test_timeout_and_communication_failure_still_clean_up(self):
        for error in (subprocess.TimeoutExpired("fake-agent", 120), OSError("read failed")):
            with self.subTest(error=type(error).__name__):
                if isinstance(error, subprocess.TimeoutExpired):
                    result = self.execute(communicate_error=error)
                    self.assertEqual(
                        (result["outcome"], result["response"], result["termination_reason"]),
                        ("timed_out", None, "turn_timeout"),
                    )
                else:
                    with self.assertRaises(OSError) as caught:
                        self.execute(communicate_error=error)
                    self.assertIs(caught.exception, error)
                self.assertEqual(
                    self.trace,
                    [
                        "begin",
                        "command",
                        "environment",
                        "popen",
                        "enter",
                        ("communicate", 120),
                        ("kill", 12345, signal.SIGKILL),
                        "wait",
                        ("finish", {7}),
                        "exit",
                        "release",
                    ],
                )

    def test_finish_failure_sets_stopped_and_propagates(self):
        for stopped in (None, threading.Event()):
            with self.subTest(stopped=stopped):
                error = RuntimeError("native turn descendants did not exit")
                with self.assertRaises(RuntimeError) as caught:
                    self.execute(stopped=stopped, finish_error=error)
                self.assertIs(caught.exception, error)
                if stopped is not None:
                    self.assertTrue(stopped.is_set())
                    self.assertLess(self.trace.index(("join", 2)), self.trace.index(("finish", {7})))
                self.assertEqual(self.trace[-3:], [("finish", {7}), "exit", "release"])
                self.assertNotIn("extract", self.trace)

    def test_gone_process_group_is_ignored_in_cancel_and_finally(self):
        stopped = threading.Event()
        stopped.set()
        result = self.execute(stopped=stopped, kill_error=ProcessLookupError())
        self.assertEqual(result["termination_reason"], "runner_stopped")
        self.assertEqual(
            self.trace,
            [
                "begin",
                "command",
                "environment",
                "popen",
                "enter",
                ("thread", True),
                "start",
                ("kill", 12345, signal.SIGKILL),
                ("communicate", 120),
                ("kill", 12345, signal.SIGKILL),
                "wait",
                ("join", 2),
                ("finish", {7}),
                "exit",
                "release",
            ],
        )

    def test_invalid_timeout_precedes_workspace_setup(self):
        for stage in ("develop", "review", "plan"):
            for timeout in (False, True, 0, -1, 60.0, "60"):
                with self.subTest(stage=stage, timeout=timeout), tempfile.TemporaryDirectory() as directory:
                    with self.assertRaisesRegex(turns.TurnError, "^invalid turn timeout$"):
                        turns.execute(
                            {"request": {"stage": stage}, "spec": {}, "timeout": timeout},
                            "key",
                            None,
                            Path(directory) / "invalid-name",
                        )
                    self.assertEqual(list(Path(directory).iterdir()), [])


class SubreaperTests(unittest.TestCase):
    """Pin the Linux ABI without requiring Linux for these unit tests."""

    def test_begin_platform_and_prctl_contract(self):
        with patch.object(processes.sys, "platform", "darwin"), patch.object(processes.ctypes, "CDLL") as library:
            self.assertIsNone(processes.begin())
            library.assert_not_called()
        with (
            patch.object(processes.sys, "platform", "linux"),
            patch.object(processes.ctypes, "CDLL") as library,
            patch.object(processes, "children", return_value={7}) as children,
        ):
            library.return_value.prctl.return_value = 0
            self.assertEqual(processes.begin(), {7})
            library.assert_called_once_with(None, use_errno=True)
            library.return_value.prctl.assert_called_once_with(36, 1, 0, 0, 0)
            children.assert_called_once_with()
            library.return_value.prctl.return_value = 1
            with patch.object(processes.ctypes, "get_errno", return_value=1):
                with self.assertRaises(OSError) as caught:
                    processes.begin()
            self.assertEqual(str(caught.exception), "[Errno 1] cannot contain native turn descendants")
            children.assert_called_once_with()
