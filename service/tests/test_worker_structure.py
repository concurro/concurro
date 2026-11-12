"""Pin worker failure effects and helper call boundaries before moving them."""

import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import fixtures  # noqa: F401 -- initializes the service and engine test paths

from checkpoints import CheckpointError
from controller import Outcome
from conversation import PublicationError
from github import Transient
from policy import Blocked, UncertainOutcome
from service import failures
from service.errors import OwnershipLost, RegistryFailure
from service.settings import CHECKOUT
from service.worker import Worker

KEY = ("owner/repo", 7)


class Guard:
    """Record the two failure-counter critical sections."""

    def __init__(self, trace):
        self.trace = trace

    def __enter__(self):
        self.trace.append("lock")

    def __exit__(self, *args):
        self.trace.append("unlock")


class Counters(dict):
    """Record reads and writes without changing ordinary dictionary semantics."""

    def __init__(self, trace):
        super().__init__({KEY: 2})
        self.trace = trace

    def get(self, key, default=None):
        self.trace.append("attempt")
        return super().get(key, default)

    def __setitem__(self, key, value):
        self.trace.append("increment")
        super().__setitem__(key, value)

    def pop(self, key, default=None):
        self.trace.append("clear")
        return super().pop(key, default)


class FailureSequenceTests(unittest.TestCase):
    """Effects, catches, diagnostics, acknowledgements and counters retain their exact order."""

    def fixture(self, *, verified=True, display=None, stop=None, next_stop=None, write_error=None):
        """Build a worker with observable effects and independently injectable failures."""
        self.trace = []
        worker = Worker.__new__(Worker)
        worker.guard, worker.failures = Guard(self.trace), Counters(self.trace)
        self.stop_count = 0

        def stopped(*args):
            self.trace.append("stop")
            self.stop_count += 1
            return stop if self.stop_count == 1 else next_stop

        def write(kind, *args):
            self.trace.append(kind)
            if write_error:
                raise write_error

        def suspend(*args, **kwargs):
            self.trace.append("suspend")
            if display:
                raise display
            controller.state["stage"] = "blocked"

        worker.gateway = SimpleNamespace(stop=stopped)
        worker.registry = SimpleNamespace(
            acknowledge=Mock(side_effect=lambda *args: write("ack", *args)),
            defer=Mock(side_effect=lambda *args: write("defer", *args)),
        )
        controller = SimpleNamespace(
            state={"stage": "plan"} if verified else {}, gh="gh", config="config", suspend=Mock(side_effect=suspend)
        )
        return worker, controller

    def invoke(self, worker, controller, error, snapshot):
        """Run the real classifier and record diagnostics without publishing them."""
        original = failures.classify

        def classify(*args, **kwargs):
            self.trace.append("classify")
            return original(*args, **kwargs)

        def health(*args, **kwargs):
            self.trace.append("health")

        with (
            patch("service.worker.failures.classify", side_effect=classify),
            patch("service.worker.log.warning", side_effect=lambda *args: self.trace.append("log")) as log,
            patch("service.worker.sync_checkpoint_health", side_effect=health) as notice,
        ):
            result = worker.failed(KEY, 3, error, controller, snapshot)
        self.assertEqual(log.call_args.args[:4], ("tick of %s#%s (generation %s) ended: %s", *KEY, 3))
        return result, notice

    def test_failure_sequence_table(self):
        """Every effect and fallback runs before ack/defer and the guarded counter update."""
        cases = [
            ("blocked", Blocked("reason"), True, None, ["suspend", "ack"], "blocked"),
            ("ordinary", ValueError("private"), True, None, ["suspend", "ack"], "blocked"),
            ("transient", Transient("retry", retry_after=17), True, None, ["defer"], "wait-until"),
            ("uncertain", UncertainOutcome("lost"), True, None, ["defer"], "wait-until"),
            ("unverified", ValueError("private"), False, None, ["defer"], "wait-until"),
            ("health", CheckpointError("report_data"), True, None, ["health", "defer"], "wait-until"),
            ("publication", PublicationError("report_data"), True, None, ["suspend", "health", "ack"], "blocked"),
            (
                "display checkpoint",
                Blocked("reason"),
                True,
                CheckpointError("report_data"),
                ["suspend", "health", "classify", "defer"],
                "wait-until",
            ),
            (
                "display ordinary",
                Blocked("reason"),
                True,
                ValueError("private"),
                ["suspend", "health", "classify", "defer"],
                "wait-until",
            ),
        ]
        for name, error, verified, display, middle, kind in cases:
            with self.subTest(name=name):
                worker, controller = self.fixture(verified=verified, display=display)
                result, notice = self.invoke(worker, controller, error, {"input": 1})
                self.assertEqual(result.kind, kind)
                self.assertEqual(
                    self.trace,
                    ["lock", "attempt", "unlock", "stop", "classify", "log"]
                    + middle
                    + ["lock", "increment" if kind == "wait-until" else "clear", "unlock"],
                )
                self.assertEqual(dict(worker.failures), {KEY: 3} if kind == "wait-until" else {})
                if kind == "wait-until":
                    self.assertEqual(result.retry_after, 17 if name == "transient" else 240)
                    worker.registry.defer.assert_called_once_with(KEY, {"input": 1})
                else:
                    worker.registry.acknowledge.assert_called_once_with(KEY, {"input": 1, "_stage": "blocked"})
                if "suspend" in middle:
                    reason = (
                        str(error)
                        if isinstance(error, Blocked)
                        else "Transport or schema failure; inspect service health."
                    )
                    controller.suspend.assert_called_once_with(
                        "blocked", reason, outboxes=False, publish_events=name != "publication"
                    )
                if "health" in middle:
                    self.assertEqual(notice.call_args.args, ("gh", "config", 7))
                    diagnostic = notice.call_args.kwargs["error"]
                    if name == "display ordinary":
                        self.assertEqual(diagnostic.code, "failure_display")
                    else:
                        self.assertIs(diagnostic, display or error)
                else:
                    notice.assert_not_called()

    def test_gateway_stop_precedence_and_effect_boundary(self):
        """Recorded stops override thrown stops; effect stops skip acknowledgements and counters."""
        initial, caught, recorded = OwnershipLost("initial"), OwnershipLost("effect"), OwnershipLost("recorded")
        for error, stop, display, next_stop, expected, tail in (
            (caught, None, None, None, caught, []),
            (caught, initial, None, None, initial, []),
            (Blocked("reason"), None, caught, None, caught, ["suspend", "stop"]),
            (Blocked("reason"), None, caught, recorded, recorded, ["suspend", "stop"]),
        ):
            with self.subTest(stop=stop, display=display, next_stop=next_stop):
                worker, controller = self.fixture(stop=stop, display=display, next_stop=next_stop)
                result, notice = self.invoke(worker, controller, error, {"input": 1})
                self.assertIs(result, expected)
                self.assertEqual(self.trace, ["lock", "attempt", "unlock", "stop", "classify", "log"] + tail)
                self.assertEqual(dict(worker.failures), {KEY: 2})
                notice.assert_not_called()

    def test_acknowledgement_and_deferral_failures_propagate(self):
        """Storage failures after the effect are not converted into a returned stop."""
        for error, tail in ((Blocked("reason"), ["suspend", "ack"]), (Transient("retry"), ["defer"])):
            with self.subTest(error=type(error).__name__):
                failed = RegistryFailure("storage")
                worker, controller = self.fixture(write_error=failed)
                with self.assertRaises(RegistryFailure) as raised:
                    self.invoke(worker, controller, error, {"input": 1})
                self.assertIs(raised.exception, failed)
                self.assertEqual(self.trace, ["lock", "attempt", "unlock", "stop", "classify", "log"] + tail)
                self.assertEqual(dict(worker.failures), {KEY: 2})

    def test_missing_snapshot_skips_deferral(self):
        """Failures before snapshot/state loading still increment backoff without diagnostics."""
        worker, _ = self.fixture()
        result, notice = self.invoke(worker, None, ValueError("input"), None)
        self.assertEqual(result, Outcome("wait-until", retry_after=240))
        self.assertEqual(
            self.trace, ["lock", "attempt", "unlock", "stop", "classify", "log", "lock", "increment", "unlock"]
        )
        notice.assert_not_called()


class WorkerHelperTests(unittest.TestCase):
    """Observe helper behaviour through the original public worker entry points."""

    def test_operator_read_points(self):
        """Executor reads before binding validation; cleanup reads before enumerating sessions."""
        worker = Worker.__new__(Worker)
        worker.settings = SimpleNamespace(
            gateway=SimpleNamespace(url="https://example.invalid", headers={}, operator_token_path=Path("operator"))
        )
        events = []
        worker.registry = SimpleNamespace(
            binding=lambda key: events.append("binding"),
            runner_sessions=lambda: events.append("sessions") or [],
        )
        with (
            patch(
                "service.worker.private.read", side_effect=lambda *args: events.append(("read", args)) or b" token \n"
            ),
            patch("service.worker.Operator", side_effect=lambda *args: events.append(("operator", args))),
        ):
            with self.assertRaisesRegex(Blocked, "runner task has no recorded target binding"):
                worker.executor(KEY[0], SimpleNamespace(key=KEY))
            worker.cleanup_runners()
        start = [("read", (Path("operator"), (CHECKOUT,))), ("operator", ("https://example.invalid", "token"))]
        self.assertEqual(events, start + ["binding"] + start + ["sessions"])

    def test_run_facades_and_acknowledged_stage(self):
        """All three facade calls retain reader order and only fork/upstream receive task_key."""
        worker = Worker.__new__(Worker)
        trace = []
        worker.guard, worker.failures = Guard(trace), Counters(trace)
        worker.renewing = lambda *args: nullcontext()
        worker.reconciler = SimpleNamespace(snapshot=lambda key, cached=True: {"input": 1})
        config = {"fork": "fork/repo", "upstream": "up/repo"}
        worker.load = lambda *args: SimpleNamespace(config=config, bundle="bundle")
        worker.reader = lambda repo: trace.append(("read", repo)) or "reader:" + repo
        worker.gateway = SimpleNamespace(stop=lambda *args: None)
        worker.registry = SimpleNamespace(acknowledge=Mock())
        worker.delegation_key = None
        worker.sponsorship = None
        worker.make_executor = lambda *args: None
        worker.agents = lambda *args, **kwargs: None
        worker.settings = SimpleNamespace(state_dir=Path("state"), controller_id="controller")
        worker.report_cache = None
        controller = SimpleNamespace(state={"stage": "planned"}, advance=lambda number: Outcome("await-human"))

        def facade(*args, **kwargs):
            trace.append(("facade", args, kwargs))
            return "facade"

        with (
            patch("service.worker.GatewayGitHub", side_effect=facade),
            patch("service.worker.Controller", return_value=controller),
            patch("service.worker.sync_checkpoint_health"),
        ):
            self.assertEqual(worker.run(KEY, 3), Outcome("await-human"))
        expected = []
        for repo, kwargs in ((KEY[0], {}), ("fork/repo", {"task_key": KEY}), ("up/repo", {"task_key": KEY})):
            expected.extend([("read", repo), ("facade", (repo, 7, 3, worker.gateway, "reader:" + repo), kwargs)])
        self.assertEqual(trace, expected + ["lock", "clear", "unlock"])
        worker.registry.acknowledge.assert_called_once_with(KEY, {"input": 1, "_stage": "planned"})

    def test_evaluation_facade_has_no_task_key(self):
        """Evaluation retains its unkeyed facade and always releases before runner cleanup."""
        worker = Worker.__new__(Worker)
        trace = []
        worker.targets = {KEY[0]: object()}
        worker.gateway = SimpleNamespace(
            acquire=lambda *args, **kwargs: 3, release=lambda *args: trace.append("release")
        )
        worker.renewing = lambda *args: nullcontext()
        worker.load = lambda *args, **kwargs: SimpleNamespace(config={"upstream": "up/repo"}, bundle="bundle")
        worker.reader = lambda repo: "reader:" + repo
        worker.make_executor = lambda *args: None
        worker.agents = lambda *args, **kwargs: None
        worker.settings = SimpleNamespace(state_dir=Path("state"))
        worker.cleanup_runners = lambda: trace.append("cleanup")
        with (
            patch("service.worker.GatewayGitHub") as facade,
            patch("service.worker.resolve"),
            patch("service.worker.head", return_value="a" * 40),
            patch("design_eval.evaluate", return_value="result"),
        ):
            self.assertEqual(worker.evaluate(*KEY, "reviewer"), "result")
        facade.assert_called_once_with(*KEY, 3, worker.gateway, "reader:" + KEY[0])
        self.assertEqual(trace, ["release", "cleanup"])
