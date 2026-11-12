"""App-wide delivery recovery, pagination, retention gaps and durable progress."""

import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from fixtures import KEY, ServiceFixture

from service.identity import Identity, IdentityError
from service.transport import Response
from service.webhook_recovery import HISTORY_SECONDS, Recovery, next_page


class DeliveryAPI:
    """A deterministic App API; only failed enabled-target deliveries may be retried."""

    app_id = 123

    def __init__(self):
        self.items, self.requests, self.details = [], [], {}
        self.fail = None
        self.lost = False
        self.page_size = 2

    def app_jwt(self):
        """A sentinel credential, never an installation token."""
        return "app-jwt"

    def api(self, method, path, token, data=None, *, with_headers=False):
        """Expose two-item pages so tests exercise cursor traversal and cross-page grouping."""
        self.requests.append((method, path))
        if token != "app-jwt":
            raise AssertionError(token)
        if path == self.fail:
            raise IdentityError("injected")
        if method == "POST":
            if self.lost:
                raise IdentityError("lost reply")
            return None
        if path.startswith("app/hook/deliveries?"):
            start = int(path.rsplit("cursor=", 1)[1]) if "cursor=" in path else 0
            items = deepcopy(self.items[start : start + self.page_size])
            headers = {}
            if start + self.page_size < len(self.items):
                headers["link"] = (
                    f'<https://api.github.com/app/hook/deliveries?cursor={start + self.page_size}>; rel="next"'
                )
            return items, headers
        number = int(path.rsplit("/", 1)[1])
        return deepcopy(
            self.details.get(number)
            or {
                "status": "failed to connect to host",
                "status_code": 0,
                "request": {"payload": {"repository": {"full_name": KEY[0]}, "installation": {"id": 42}}},
            }
        )


def delivery(number, guid=None, status="OK", code=202, installation=42):
    """A summary independent of the production decoder."""
    return {
        "id": number,
        "guid": guid or str(number),
        "status": status,
        "status_code": code,
        "installation_id": installation,
    }


class RecoveryTests(ServiceFixture, unittest.TestCase):
    """No issue polling with intact delivery coverage, even after service restarts."""

    def setUp(self):
        super().setUp()
        self.api = DeliveryAPI()
        self.reconciler = Mock(repositories=(KEY[0],), cleanup=None)
        self.recovery = Recovery(self.api, self.registry, self.reconciler, {KEY[0]: 42}, clock=self.clock)

    def test_bootstrap_then_twelve_checks_per_idle_hour(self):
        """An empty delivery list costs one request per check, independent of task count."""
        self.recovery.poll()
        self.reconciler.poll.assert_called_once_with(strict=True)
        self.api.requests.clear()
        for _ in range(12):
            self.clock.now += 300
            self.recovery.poll()
        self.assertEqual(len(self.api.requests), 12)
        self.reconciler.poll.assert_called_once()

    def test_restart_reuses_coverage_and_long_gap_reconciles(self):
        """Restarts preserve coverage; a gap beyond GitHub retention requires state reconciliation."""
        self.recovery.poll()
        self.registry.close()
        self.registry = self.open()
        self.recovery.registry = self.registry
        self.recovery.poll()
        self.reconciler.poll.assert_called_once()
        self.clock.now += HISTORY_SECONDS
        self.recovery.poll()
        self.assertEqual(self.reconciler.poll.call_count, 2)

    def test_changed_app_or_targets_bootstraps(self):
        """New enrollment destinations cannot inherit another target set's coverage."""
        self.recovery.poll()
        other = Recovery(self.api, self.registry, self.reconciler, {KEY[0]: 43}, clock=self.clock)
        other.poll()
        self.assertEqual(self.reconciler.poll.call_count, 2)

    def test_all_pages_group_successes_before_retrying(self):
        """A failed original on page one is not retried when a later page confirms redelivery."""
        self.api.items = [delivery(1, "same", "failed", 500), delivery(2), delivery(3, "same")]
        self.recovery.poll()
        self.assertEqual(
            self.api.requests,
            [
                ("GET", "app/hook/deliveries?per_page=100"),
                ("GET", "app/hook/deliveries?per_page=100&cursor=2"),
            ],
        )

    def test_transport_failure_retried_but_pending_and_foreign_left_alone(self):
        """No HTTP response still warrants retry; unfinished deliveries must not be duplicated."""
        self.api.items = [
            delivery(1, status="failed to connect to host", code=0),
            delivery(2, status="Pending", code=None),
            delivery(3, status="failed", code=500, installation=99),
            delivery(4, status="", code=None),
        ]
        self.recovery.poll()
        self.assertEqual([p for m, p in self.api.requests if m == "POST"], ["app/hook/deliveries/1/attempts"])

    def test_pending_redelivery_prevents_another_retry(self):
        """An accepted redelivery is revisited next poll, not multiplied while it is pending."""
        self.api.items = [delivery(1, "same", "failed", 500), delivery(2, "same", "Pending", None)]
        self.recovery.poll()
        self.assertFalse(any(m == "POST" for m, _ in self.api.requests))

    def test_wrong_repository_and_newly_successful_detail_not_retried(self):
        """Installation membership alone does not authorize recovery of another target."""
        self.api.items = [delivery(1, status="failed", code=500), delivery(2, status="failed", code=500)]
        self.api.details = {
            1: {"request": {"payload": {"repository": {"full_name": "elsewhere/repo"}}}},
            2: {"status": "OK", "status_code": 202},
        }
        self.recovery.poll()
        self.assertFalse(any(m == "POST" for m, _ in self.api.requests))

    def test_page_failure_never_advances_coverage_or_redelivers_partial_listing(self):
        """An incomplete listing might hide a successful retry and cannot become coverage evidence."""
        self.recovery.poll()
        before = self.registry.webhook_check(self.recovery.scope)
        self.clock.now += 300
        self.api.items = [delivery(1, status="failed", code=500), delivery(2), delivery(3)]
        self.api.fail = "app/hook/deliveries?per_page=100&cursor=2"
        self.recovery.poll()
        self.assertEqual(self.registry.webhook_check(self.recovery.scope), before)
        self.assertFalse(any(m == "POST" for m, _ in self.api.requests))

    def test_failed_bootstrap_retried(self):
        """Reconciliation failures are not recorded as successful bootstrap."""
        self.reconciler.poll.side_effect = RuntimeError("unavailable")
        self.recovery.poll()
        self.assertIsNone(self.registry.webhook_check(self.recovery.scope))
        self.assertEqual(self.api.requests, [])

    def test_lost_redelivery_reply_recovers_from_successful_attempt(self):
        """A failed POST reply leaves progress unchanged; the next scan recognizes success."""
        self.recovery.poll()
        before = self.registry.webhook_check(self.recovery.scope)
        self.clock.now += 300
        self.api.items = [delivery(1, "same", "failed", 500)]
        self.api.lost = True
        self.recovery.poll()
        self.assertEqual(self.registry.webhook_check(self.recovery.scope), before)
        self.api.items.append(delivery(2, "same"))
        self.recovery.poll()
        self.assertEqual(sum(m == "POST" for m, _ in self.api.requests), 1)

    def test_retained_older_failure_is_not_skipped_by_a_time_cursor(self):
        """Delayed failures remain discoverable on later pages, regardless of previous poll time."""
        self.recovery.poll()
        self.api.items = [delivery(3), delivery(2), delivery(1, status="timed out", code=None)]
        self.recovery.poll()
        self.assertIn(("POST", "app/hook/deliveries/1/attempts"), self.api.requests)

    def test_unresolved_deliveries_cannot_age_out_silently(self):
        """Persistent delivery failure triggers reconciliation before retained history is lost."""
        self.recovery.poll()
        before = self.registry.webhook_check(self.recovery.scope)
        self.api.items = [delivery(1, status="failed", code=500)]
        for _ in range(3):
            self.clock.now += 24 * 60 * 60
            self.recovery.poll()
        self.assertEqual(self.reconciler.poll.call_count, 2)
        self.assertGreater(self.registry.webhook_check(self.recovery.scope), before)

    def test_registry_v4_upgrade_preserves_queue_and_receipts(self):
        """The new coverage table upgrades without replacing authoritative task state."""
        import sqlite3
        from pathlib import Path

        self.registry.receive("old", [KEY], "issue_comment")
        self.registry.close()
        with sqlite3.connect(Path(self.directory.name) / "registry.db") as db:
            db.execute("DROP TABLE webhook_recovery")
            db.execute("DROP TABLE observations")
            db.execute("UPDATE meta SET value='4' WHERE name='schema'")
        self.registry = self.open()
        self.assertFalse(self.registry.receive("old", [KEY], "issue_comment"))
        self.assertEqual([item.key for item in self.registry.due(self.clock())], [KEY])
        self.assertIsNone(self.registry.webhook_check("scope"))


class PaginationTests(unittest.TestCase):
    """Authenticate App requests and preserve cursor links through the existing transport."""

    def test_link_parsing_and_rejection(self):
        self.assertEqual(
            next_page({"link": '<https://api.github.com/app/hook/deliveries?cursor=a%2Bb>; rel="next"'}),
            "app/hook/deliveries?per_page=100&cursor=a%2Bb",
        )
        self.assertIsNone(next_page({}))
        with self.assertRaises(IdentityError):
            next_page({"link": '<https://elsewhere.invalid/app/hook/deliveries?cursor=x>; rel="next"'})

    def test_identity_returns_pagination_headers_without_changing_normal_api(self):
        ident = Identity(Mock(app_id=123, private_key_path="unused", spec=["app_id", "private_key_path"]))
        response = Response(True, 200, {"link": "next"}, b"[]")
        with patch("service.identity.transport.exchange", return_value=response) as exchange:
            self.assertEqual(ident.api("GET", "app/hook/deliveries", "jwt", with_headers=True), ([], {"link": "next"}))
            self.assertEqual(ident.api("GET", "app/hook/deliveries", "jwt"), [])
        self.assertEqual(exchange.call_args.args[4]["Authorization"], "Bearer jwt")


class DaemonRecoveryTests(unittest.TestCase):
    """The actual daemon timer targets App recovery rather than repository snapshots."""

    def test_timer_calls_recovery(self):
        from service.__main__ import Daemon

        daemon = Daemon.__new__(Daemon)
        daemon.stopping = Mock()
        daemon.stopping.wait.side_effect = [False, True]
        daemon.scheduler, daemon.recovery, daemon.reconciler, daemon.stop = Mock(), Mock(), Mock(), Mock()
        daemon.registry = Mock()
        daemon.registry.observations.return_value = []
        daemon.poll_seconds = 300

        def thread(**kwargs):
            """Run the background job synchronously without opening servers."""
            return Mock(start=kwargs["target"], is_alive=lambda: False)

        with patch("service.__main__.threading.Thread", side_effect=thread):
            daemon.run()
        daemon.recovery.poll.assert_called_once_with()
        daemon.reconciler.poll.assert_not_called()
        daemon.stop.assert_called_once_with()
