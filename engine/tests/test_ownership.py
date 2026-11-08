"""A controller acts only on tasks whose ownership marker names it, and otherwise writes nothing."""

import unittest
from unittest.mock import DEFAULT, patch

from support import (
    setUpModule as setUpModule,  # unittest module fixtures
    tearDownModule as tearDownModule,
)

import controller as controller_module
import conversation
from checkpoints import Checkpoints
from controller import ACTIONS, Controller
from pipeline_fixtures import ManualPipelineFixture
from policy import Blocked

SERVICE = "service:primary"
MUTATORS = ("sync_stage", "sync_attention", "create_linked_branch")


class OwnershipFixture:
    """Cutover and write recording shared by controller-level and entry-point tests."""

    def transfer(self, owner, **changes):
        """Rewrite the stored task as a cutover would, optionally adding pending work."""
        checkpoints = Checkpoints(self.gh, self.config, 7)
        state = checkpoints.load(self.gh.pages("issues/7/comments"))
        state["controller"] = owner
        state.update(changes)
        conversation.queue(state, "Pending event", "Would be published by the owner.")
        checkpoints.save(state)

    def mutations(self, controller_id, run=None):
        """Advance once (or run the entry point) and return every write attempted."""
        writes = []
        original = self.gh.repo

        def repo(path, method="GET", data=None):
            """Record non-GET requests while preserving fake behaviour for reads."""
            if method != "GET":
                writes.append((method, path))
            return original(path, method, data)

        controller = Controller(self.gh, self.agents, self.config, controller_id=controller_id)
        with (
            patch.object(self.gh, "repo", side_effect=repo),
            patch.multiple(self.gh, **{name: DEFAULT for name in MUTATORS}) as recorded,
        ):
            advanced = run() if run else controller.advance(7)
        writes += [(name, call.args) for name, mock in recorded.items() for call in mock.call_args_list]
        return controller, advanced, writes


class OwnershipTests(OwnershipFixture, ManualPipelineFixture, unittest.TestCase):
    """Cutover safety: a marker-aware controller leaves foreign-owned tasks untouched."""

    def test_new_tasks_record_their_controller(self):
        """Every task created after this change names its owner explicitly."""
        self.assertEqual(self.tick()["controller"], ACTIONS)

    def test_actions_controller_writes_nothing_for_a_service_task(self):
        """Pending events, label drift and bundle drift all stay untouched under another owner."""
        self.ready()
        self.transfer(SERVICE, bundle_id="drifted")
        calls = len(self.agents.calls)
        controller, advanced, writes = self.mutations(ACTIONS)
        self.assertFalse(advanced)
        self.assertEqual(advanced.kind, "terminal")
        self.assertEqual(writes, [])
        self.assertIsNone(controller.state)
        self.assertEqual(len(self.agents.calls), calls)
        self.assertEqual(self.gh.merges, 0)

    def test_owner_continues_after_cutover(self):
        """The named controller resumes the same task, including its pending publication."""
        self.ready()
        self.transfer(SERVICE)
        controller, advanced, writes = self.mutations(SERVICE)
        self.assertEqual(advanced.kind, "wait-until")
        self.assertTrue(controller.state["handoff_ready"])
        self.assertTrue(writes)

    def test_engine_pin_is_not_part_of_the_configuration_digest(self):
        """A service target's `engine` pin can change without invalidating its tasks; it must name a commit."""
        self.assertEqual(
            controller_module.config_digest({**self.config, "engine": "e" * 40}),
            controller_module.config_digest(self.config),
        )
        controller_module.validate_config({**self.config, "engine": "e" * 40})
        for engine in ("e" * 39, "E" * 40, 7):
            with self.assertRaises(Blocked):
                controller_module.validate_config({**self.config, "engine": engine})

    def test_unrecognized_markers_fail_closed(self):
        """Malformed or unknown markers are foreign to every controller."""
        self.ready()
        for marker in ({"id": ACTIONS}, "", None, "service:other"):
            with self.subTest(marker=marker):
                self.transfer(marker)
                for owner in (ACTIONS, SERVICE):
                    _, advanced, writes = self.mutations(owner)
                    self.assertFalse(advanced)
                    self.assertEqual(writes, [])


if __name__ == "__main__":
    unittest.main()
