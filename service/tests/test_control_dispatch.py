"""Characterize CLI request bytes and operator dispatch before table extraction."""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import fixtures  # noqa: F401 -- initializes the service and engine test paths

from service import __main__ as cli
from service.control import Control
from service.errors import OwnershipLost


class CliRequestTests(unittest.TestCase):
    """The parser and builder retain field order, defaults and fallback requests."""

    def test_initialize_creates_the_registry_without_starting_the_daemon(self):
        self.enterContext(patch.object(cli.logging, "basicConfig"))
        with tempfile.TemporaryDirectory() as directory:
            Path(directory).chmod(0o700)
            settings = SimpleNamespace(state_dir=Path(directory), registry_id="test")
            for expected in (0, 1):  # a second run refuses to initialize over the first
                with (
                    self.subTest(expected=expected),
                    patch.object(sys, "argv", ["service", "--settings", "settings.json", "initialize"]),
                    patch.object(cli.settings_module, "load", return_value=settings),
                    patch.object(cli, "Daemon") as daemon,
                    patch.object(cli.control, "request") as request,
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()) as stderr,
                ):
                    self.assertEqual(cli.main(), expected)
                    daemon.assert_not_called()
                    request.assert_not_called()
            self.assertIn("refusing to initialize", stderr.getvalue())
            cli.Registry.open(directory, "test").close()

    def test_request_table(self):
        """Every parsed subcommand produces literal pinned JSON, without parser-only fields."""
        cases = [
            (["serve"], b'{"command": "serve"}'),
            (["serve", "--initialize", "--listen", "localhost:1234", "--poll-seconds", "9"], b'{"command": "serve"}'),
            (["initialize"], b'{"command": "initialize"}'),
            (["status"], b'{"command": "status"}'),
            (["reconcile-once"], b'{"command": "reconcile-once"}'),
            (["adopt", "owner/repo"], b'{"command": "adopt", "repository": "owner/repo", "issues": []}'),
            (["adopt", "owner/repo", "7", "9"], b'{"command": "adopt", "repository": "owner/repo", "issues": [7, 9]}'),
            (["release", "owner/repo", "7"], b'{"command": "release", "repository": "owner/repo", "issue": 7}'),
            (
                ["evaluate", "owner/repo", "7", "profile-high"],
                b'{"command": "evaluate", "repository": "owner/repo", "issue": 7, "provider": "profile-high"}',
            ),
            (
                ["resolve", "intent", "applied", "seen"],
                b'{"command": "resolve", "intent": "intent", "disposition": "applied", "evidence": "seen", "operator": "local-user"}',
            ),
            (
                ["resolve", "intent", "not-applied", "not seen", "--operator", "writer"],
                b'{"command": "resolve", "intent": "intent", "disposition": "not-applied", "evidence": "not seen", "operator": "writer"}',
            ),
        ]
        with patch.object(cli.pwd, "getpwuid", return_value=SimpleNamespace(pw_name="local-user")):
            parser = cli.parser()
        for args, expected in cases:
            with self.subTest(args=args):
                parsed = parser.parse_args(["--settings", "settings.json", *args])
                value = cli.command(parsed)
                self.assertEqual(value, json.loads(expected))
                self.assertEqual(json.dumps(value).encode(), expected)
                # Existing direct callers need not obtain their Namespace from the parser.
                direct = SimpleNamespace(**json.loads(expected))
                self.assertEqual(json.dumps(cli.command(direct)).encode(), expected)
        self.assertEqual(cli.command(SimpleNamespace(command="future", ignored=True)), {"command": "future"})
        self.assertEqual(json.dumps(cli.command(SimpleNamespace(command="future"))).encode(), b'{"command": "future"}')
        for value in (None, [], {}, False):
            self.assertEqual(cli.command(SimpleNamespace(command=value)), {"command": value})

    def test_main_output_and_serve_bypass(self):
        """The CLI keeps exact stdout/exit status, and serve never calls the control socket."""
        self.enterContext(patch.object(cli.logging, "basicConfig"))
        settings = SimpleNamespace(state_dir=Path("state"))
        for reply, expected, code in (
            ({"ok": True, "result": None}, "null\n", 0),
            ({"ok": True, "result": {"tasks": [7]}}, '{\n  "tasks": [\n    7\n  ]\n}\n', 0),
            ({"ok": False, "error": "refused"}, '{\n  "error": "refused"\n}\n', 1),
        ):
            with (
                self.subTest(reply=reply),
                patch.object(sys, "argv", ["service", "--settings", "settings.json", "status"]),
                patch.object(cli.settings_module, "load", return_value=settings),
                patch.object(cli.control, "request", return_value=reply) as request,
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(cli.main(), code)
                self.assertEqual(output.getvalue(), expected)
                request.assert_called_once_with(Path("state"), {"command": "status"})
        with (
            patch.object(sys, "argv", ["service", "--settings", "settings.json", "serve"]),
            patch.object(cli.settings_module, "load", return_value=settings),
            patch.object(cli, "serve", return_value=23) as serve,
            patch.object(cli.control, "request") as request,
        ):
            self.assertEqual(cli.main(), 23)
            serve.assert_called_once()
            request.assert_not_called()
        with (
            patch.object(sys, "argv", ["service", "--settings", "settings.json", "status"]),
            patch.object(cli.settings_module, "load", return_value=settings),
            patch.object(cli.control, "request", side_effect=ConnectionError("offline")),
            redirect_stderr(io.StringIO()) as error,
        ):
            self.assertEqual(cli.main(), 1)
            self.assertEqual(error.getvalue(), "offline\n")


class DispatchTests(unittest.TestCase):
    """Each command preserves lazy argument access, conversion and error precedence."""

    def setUp(self):
        self.registry = Mock()
        self.registry.status.return_value = [{"issue": 7}]
        self.registry.mutation.return_value = SimpleNamespace(key=("owner/repo", 7))
        self.reconciler = Mock()
        self.reconciler.poll.return_value = [("owner/repo", 7), ("other/repo", 9)]
        self.cutover = Mock()
        self.cutover.adopt.return_value = {"adopted": [7], "refused": {}}
        self.cutover.release.return_value = "ignored"
        self.evaluate = Mock(return_value={"evaluation": "result"})
        self.control = Control(self.registry, self.reconciler, self.cutover, self.evaluate)

    def test_routing_table(self):
        """All commands route once with exact arguments and preserve their distinct returns."""
        cases = [
            ({"command": "status"}, self.registry.status, (), [{"issue": 7}]),
            ({"command": "reconcile-once"}, self.reconciler.poll, (), [["owner/repo", 7], ["other/repo", 9]]),
            (
                {"command": "adopt", "repository": "owner/repo"},
                self.cutover.adopt,
                ("owner/repo", ()),
                {"adopted": [7], "refused": {}},
            ),
            (
                {"command": "adopt", "repository": "owner/repo", "issues": [7, 9]},
                self.cutover.adopt,
                ("owner/repo", (7, 9)),
                {"adopted": [7], "refused": {}},
            ),
            (
                {"command": "release", "repository": "owner/repo", "issue": 7},
                self.cutover.release,
                ("owner/repo", 7),
                None,
            ),
            (
                {"command": "evaluate", "repository": "owner/repo", "issue": 7, "provider": "profile"},
                self.evaluate,
                ("owner/repo", 7, "profile"),
                {"evaluation": "result"},
            ),
            (
                {
                    "command": "resolve",
                    "intent": "id",
                    "disposition": "applied",
                    "evidence": "proof",
                    "operator": "writer",
                },
                self.registry.resolve,
                ("id", "applied", "proof", "writer"),
                {"repository": "owner/repo", "issue": 7},
            ),
        ]
        for request, handler, args, expected in cases:
            with self.subTest(request=request):
                for dependency in (self.registry, self.cutover, self.reconciler, self.evaluate):
                    dependency.reset_mock()
                self.assertEqual(self.control.handle(request), {"ok": True, "result": expected})
                handler.assert_called_once_with(*args)
                if request["command"] == "resolve":
                    self.assertEqual([c[0] for c in self.registry.mock_calls], ["mutation", "resolve"])

    def test_unknown_missing_and_unhashable_commands(self):
        """Malformed command values use the same unknown-command reply as unknown strings."""
        for value in ("unknown", "", [], {}, ["status"], {"status": True}, None, 0, False):
            with self.subTest(value=value):
                self.assertEqual(self.control.handle({"command": value}), {"ok": False, "error": "unknown command"})
        self.assertEqual(self.control.handle({}), {"ok": False, "error": "'command'"})
        for dependency in (self.registry, self.cutover, self.reconciler, self.evaluate):
            self.assertEqual(dependency.mock_calls, [])

    def test_missing_argument_precedence(self):
        """Missing fields are accessed only by the selected command, in the original order."""
        cases = {
            "evaluate": [("repository", "owner/repo"), ("issue", 7), ("provider", "profile")],
            "release": [("repository", "owner/repo"), ("issue", 7)],
            "adopt": [("repository", "owner/repo")],
            "resolve": [("intent", "id"), ("disposition", "applied"), ("evidence", "proof"), ("operator", "writer")],
        }
        for command, fields in cases.items():
            request = {"command": command}
            for field, value in fields:
                with self.subTest(command=command, missing=field):
                    self.assertEqual(self.control.handle(request), {"ok": False, "error": repr(field)})
                request[field] = value
        self.assertEqual(
            self.control.handle({"command": "evaluate", "repository": "owner/repo", "issue": False}),
            {"ok": False, "error": "issue numbers must be positive integers"},
        )

    def test_issue_number_and_issues_table(self):
        """Numbers are positive exact ints; malformed collections retain their current errors."""
        invalid = (None, False, True, 0, -1, 1.0, "7", [], {})
        for command in ("evaluate", "release", "adopt"):
            for value in invalid:
                request = {"command": command, "repository": "owner/repo", "provider": "profile"}
                request.update({"issues": [value]} if command == "adopt" else {"issue": value})
                with self.subTest(command=command, value=value):
                    self.assertEqual(
                        self.control.handle(request), {"ok": False, "error": "issue numbers must be positive integers"}
                    )
        for issues, error in (
            (None, "'NoneType' object is not iterable"),
            (7, "'int' object is not iterable"),
            ("7", "issue numbers must be positive integers"),
            ({"7": True}, "issue numbers must be positive integers"),
        ):
            self.assertEqual(
                self.control.handle({"command": "adopt", "repository": "owner/repo", "issues": issues}),
                {"ok": False, "error": error},
            )
        for issues in ([], {}, ""):
            self.assertTrue(
                self.control.handle({"command": "adopt", "repository": "owner/repo", "issues": issues})["ok"]
            )
            self.cutover.adopt.assert_called_with("owner/repo", ())
        self.evaluate.assert_not_called()
        self.cutover.release.assert_not_called()

    def test_disabled_and_falsey_evaluator(self):
        """Only None disables evaluation; disabled dispatch never reads its missing arguments."""
        self.control.evaluate = None
        self.assertEqual(self.control.handle({"command": "evaluate"}), {"ok": False, "error": "unknown command"})
        self.control.evaluate = False
        self.assertEqual(self.control.handle({"command": "evaluate"}), {"ok": False, "error": "'repository'"})
        self.assertEqual(
            self.control.handle({"command": "evaluate", "repository": "owner/repo", "issue": 7, "provider": "profile"}),
            {"ok": False, "error": "'bool' object is not callable"},
        )

    def test_handler_errors_and_resolve_missing_record(self):
        """Dispatch preserves handled refusals, unhandled failures and the resolve lookup guard."""
        self.registry.mutation.return_value = None
        self.assertEqual(
            self.control.handle(
                {
                    "command": "resolve",
                    "intent": "id",
                    "disposition": "applied",
                    "evidence": "proof",
                    "operator": "writer",
                }
            ),
            {"ok": False, "error": "no mutation with that intent ID"},
        )
        self.registry.resolve.assert_not_called()
        for error, expected in ((OwnershipLost("lost"), "lost"), (ValueError(), "ValueError")):
            self.registry.status.side_effect = error
            self.assertEqual(self.control.handle({"command": "status"}), {"ok": False, "error": expected})
        error = RuntimeError("not handled")
        self.registry.status.side_effect = error
        with self.assertRaises(RuntimeError) as raised:
            self.control.handle({"command": "status"})
        self.assertIs(raised.exception, error)


class TargetImportTests(unittest.TestCase):
    """The service establishes the engine import path without a test bootstrap."""

    def test_fresh_import_orders(self):
        """Target verification can load before or after its normal callers in a fresh process."""
        root = Path(__file__).resolve().parents[2]
        orders = [
            ("service.targets", "service.settings", "service.worker"),
            ("service.settings", "service.targets", "service.__main__"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for modules in orders:
                script = f"import sys, importlib; sys.path.insert(0, {str(root)!r}); " + "; ".join(
                    f"importlib.import_module({name!r})" for name in modules
                )
                result = subprocess.run(
                    [sys.executable, "-I", "-c", script], cwd=directory, capture_output=True, text=True, timeout=30
                )
                self.assertEqual(result.returncode, 0, result.stderr)
