"""Private App delivery API, pagination and daemon maintenance."""

import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from fixtures import KEY, ServiceFixture

from service.identity import Identity, IdentityError
from service.transport import Response
from service.webhook_recovery import DeliveryBridge, next_page


class DeliveryAPI:
    """A deterministic App API with delivery listing and redelivery."""

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
        """Expose two-item pages so tests exercise cursor traversal."""
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


class DeliveryBridgeTests(ServiceFixture, unittest.TestCase):
    """Private App delivery listing and redelivery work with the shared bearer."""

    def setUp(self):
        super().setUp()
        self.api = DeliveryAPI()

    def test_private_delivery_bridge_pages_and_redelivers(self):
        """Ingress receives every page and can request an App redelivery with its bearer."""
        bridge = DeliveryBridge(self.api, "bridge-secret")
        self.api.items = [delivery(1), delivery(2), delivery(3)]
        self.assertEqual(bridge.handle("GET", "/v1/deliveries", "Bearer wrong")[0], 401)
        status, first = bridge.handle("GET", "/v1/deliveries", "Bearer bridge-secret")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in first["deliveries"]], [1, 2])
        self.assertEqual(first["next_cursor"], "2")
        status, second = bridge.handle("GET", "/v1/deliveries?cursor=2", "Bearer bridge-secret")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in second["deliveries"]], [3])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(
            bridge.handle("POST", "/v1/deliveries/3/redeliver", "Bearer bridge-secret"),
            (202, {}),
        )
        self.assertIn(("POST", "app/hook/deliveries/3/attempts"), self.api.requests)


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


class DaemonMaintenanceTests(ServiceFixture, unittest.TestCase):
    """The daemon timer maintains lists and bootstraps changed scope."""

    def test_timer_calls_maintenance(self):
        from service.__main__ import Daemon

        daemon = Daemon.__new__(Daemon)
        daemon.stopping = Mock()
        daemon.stopping.wait.side_effect = [False, True]
        daemon.scheduler, daemon.reconciler, daemon.list_index, daemon.stop = Mock(), Mock(), Mock(), Mock()
        daemon.registry = Mock()
        daemon.scope = "A"
        daemon.registry.bootstrap_scope.return_value = "A"
        daemon.registry.observations.return_value = []
        daemon.poll_seconds = 300

        def thread(**kwargs):
            """Run the background job synchronously without opening servers."""
            return Mock(start=kwargs["target"], is_alive=lambda: False)

        with patch("service.__main__.threading.Thread", side_effect=thread):
            daemon.run()
        daemon.list_index.maintain.assert_called_once_with()
        daemon.reconciler.policy_observer.assert_called_once_with()
        daemon.reconciler.cleanup.assert_called_once_with()
        daemon.reconciler.poll.assert_not_called()
        daemon.stop.assert_called_once_with()
        daemon.list_index.maintain.side_effect = RuntimeError("unavailable")
        daemon.maintenance()
        self.assertEqual(daemon.reconciler.policy_observer.call_count, 2)
        self.assertEqual(daemon.reconciler.cleanup.call_count, 2)

    def test_scope_changes_bootstrap_only_after_completion(self):
        """A fresh scope, including A after B, reconciles and records only completed work."""
        from service.__main__ import Daemon

        daemon = Daemon.__new__(Daemon)
        daemon.registry = self.registry
        daemon.list_index = Mock()
        daemon.reconciler = Mock(policy_observer=None, cleanup=None)
        daemon.scope = "A"
        with self.registry.transaction() as db:
            db.execute("INSERT INTO webhook_recovery VALUES (?, ?)", ("A", self.clock()))
        daemon.maintenance()
        self.assertEqual(self.registry.bootstrap_scope(), "A")
        daemon.reconciler.poll.assert_called_once_with(strict=True)
        daemon.list_index.rebaseline.assert_called_once_with()
        self.registry.close()
        self.registry = self.open()
        daemon.registry = self.registry
        daemon.maintenance()
        self.assertEqual(daemon.reconciler.poll.call_count, 1, "unchanged scope does no idle task scan")
        daemon.scope = "B"
        daemon.list_index.rebaseline.side_effect = RuntimeError("unavailable")
        daemon.maintenance()
        self.assertEqual(self.registry.bootstrap_scope(), "A", "failed rebaseline cannot complete bootstrap")
        daemon.list_index.rebaseline.side_effect = None
        daemon.maintenance()
        self.assertEqual(self.registry.bootstrap_scope(), "B")
        daemon.scope = "A"
        daemon.maintenance()
        self.assertEqual(self.registry.bootstrap_scope(), "A")
        self.assertEqual(daemon.reconciler.poll.call_count, 4)
