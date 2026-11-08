"""Task configuration and publication identities stay pinned across ticks and restarts."""

from copy import deepcopy
from types import SimpleNamespace
import unittest

from fixtures import ENGINE, KEY, ServiceFixture, RestBackend
from fakes import FakeGitHub
from policy import Blocked
from service.registry import TaskBinding
from service.settings import Target
from service.worker import Worker


class BindingTests(ServiceFixture, unittest.TestCase):
    """A target or its default-branch changes cannot silently redirect an active task."""

    def test_binding_is_durable_and_cannot_retarget(self):
        """The first configuration wins; a different target pair requires reconciliation."""
        expected = TaskBinding("source/repo", "fork/repo", "a" * 40)
        self.assertEqual(self.registry.bind(KEY, expected.upstream, expected.fork, expected.config_sha), expected)
        self.assertEqual(self.registry.bind(KEY, expected.upstream, expected.fork, "b" * 40), expected)
        with self.assertRaisesRegex(ValueError, "target changed"):
            self.registry.bind(KEY, "other/repo", expected.fork, "c" * 40)
        self.registry.close()
        self.registry = self.open()
        self.assertEqual(self.registry.binding(KEY), expected)

    def test_worker_loads_config_from_fork_once_per_task(self):
        """Later ticks keep the original fork config; issue identity and upstream code remain separate."""
        fork = "owner/fork"
        config = {**deepcopy(self.config), "repository": fork}
        fake = FakeGitHub(config)
        backend = RestBackend(fake)
        target = Target(KEY[0], fork, "both")
        settings = SimpleNamespace(targets=(target,), engine=ENGINE)
        worker = Worker(
            settings,
            None,
            self.registry,
            None,
            lambda repository: backend if repository == fork else None,
            config["bot_login"],
        )
        first = worker.load(*KEY, pin=True)
        pinned = first.config_sha
        calls = []
        original = backend.request

        def read(method, path, data=None):
            calls.append(path)
            if path == "graphql":
                raise AssertionError("an existing task must not consult the new default-branch head")
            return original(method, path, data)

        backend.request = read
        second = worker.load(*KEY)
        self.assertEqual(second.config_sha, pinned)
        self.assertEqual(second.config["repository"], KEY[0])
        self.assertEqual(second.config["fork"], fork)
        self.assertEqual(second.config["upstream"], KEY[0])
        self.assertTrue(calls)
        self.assertTrue(
            all(path.startswith(f"repos/{fork}/contents/") and path.endswith("?ref=" + pinned) for path in calls)
        )
        worker.targets[KEY[0]] = Target("different/upstream", fork, "fork")
        with self.assertRaisesRegex(Blocked, "target changed"):
            worker.load(*KEY)


if __name__ == "__main__":
    unittest.main()
