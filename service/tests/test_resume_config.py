"""Current configuration adoption crosses checkpoint and registry boundaries safely."""

import base64
import json
import unittest
from copy import deepcopy
from unittest.mock import patch
from urllib.parse import unquote

from fixtures import ENGINE, KEY, ServiceFixture

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

from github import Transient
from policy import approval_valid
from service.errors import RegistryError
from service.github_reads import DEFAULT_BRANCH


class ResumeConfigTests(ServiceFixture, unittest.TestCase):
    """Exercise the service worker, real checkpoints and real SQLite binding together."""

    def prepare(self):
        """Reach manual approval and retain separate immutable configuration snapshots."""
        self.service()
        self.reconciler.poll()
        self.drain()
        self.old_sha = self.registry.binding(KEY).config_sha
        self.new_sha = "b" * 40
        self.new_config = deepcopy(self.config)
        self.new_config["paths"] = {"protected_extensions": [], "protected_files": [], "protected_prefixes": []}
        self.new_config["limits"] = {"changed_files": None, "workspace_files": None}
        self.new_config["max_model_calls"] += 10
        snapshots = {
            self.old_sha: deepcopy(self.backend.files),
            self.new_sha: {
                **self.backend.files,
                ".concurro/config.json": json.dumps({**self.new_config, "engine": ENGINE}),
            },
        }
        content = self.backend.content

        def read(relative):
            """Return files from the requested immutable commit, independently of current main."""
            path, _, sha = unquote(relative[len("contents/") :]).partition("?ref=")
            if sha in snapshots and path in snapshots[sha]:
                data = snapshots[sha][path].encode()
                return {
                    "type": "file",
                    "encoding": "base64",
                    "path": path,
                    "size": len(data),
                    "content": base64.b64encode(data).decode(),
                }
            return content(relative)

        self.patch = patch.object(self.backend, "content", side_effect=read)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        # The fork's default branch changes; the task's approved source remains pinned.
        request = self.backend.request

        def latest(method, path, data=None):
            """Advertise the new fork main only for default-branch lookup."""
            if method == "POST" and path == "graphql" and data.get("query") == DEFAULT_BRANCH:
                return {"data": {"repository": {"defaultBranchRef": {"name": "main", "target": {"oid": self.new_sha}}}}}
            return request(method, path, data)

        self.request_patch = patch.object(self.backend, "request", side_effect=latest)
        self.request_patch.start()
        self.addCleanup(self.request_patch.stop)

    def run_one(self):
        """Run one owned tick without draining into unrelated work."""
        generation = self.gateway.acquire(KEY, "resume-test")
        self.assertIsNotNone(generation)
        try:
            result = self.worker.run(KEY, generation)
        finally:
            self.gateway.release(KEY, generation, None)
        return result

    def test_latest_settings_require_resume_then_pin_next_runner(self):
        """New default-branch settings are adopted by resume, not accidentally by ordinary ticks."""
        self.prepare()
        before = self.state()
        self.assertEqual(self.worker.load(*KEY).config_sha, self.old_sha)
        self.fake.comment("/concurro resume")
        self.run_one()
        after = self.state()
        self.assertEqual(after["stage"], "planned")
        self.assertEqual(after["plan"], before["plan"])
        self.assertEqual(after["calls"], before["calls"])
        self.assertEqual(after["execution"], self.new_config["paths"])
        self.assertEqual(self.registry.binding(KEY).config_sha, self.new_sha)
        target = self.worker.load(*KEY)
        self.assertEqual(target.config["max_model_calls"], self.new_config["max_model_calls"])
        self.assertEqual(target.config_sha, self.new_sha)
        self.fake.comment("/concurro approve " + after["plan_id"])
        self.run_one()
        self.assertTrue(approval_valid(self.state(), target.config))
        self.assertTrue(self.agents.calls)

    def test_checkpoint_before_registry_update_recovers_without_reapplying(self):
        """A lost service update cannot lose writer intent or repeatedly adopt moving main."""
        self.prepare()
        self.fake.comment("/concurro resume")
        with patch.object(self.registry, "adopt_config", side_effect=Transient("lost update")):
            self.run_one()
        receipt = deepcopy(self.state()["bundle_resume"])
        calls = self.state()["calls"]
        self.assertEqual(self.registry.binding(KEY).config_sha, self.old_sha)
        self.run_one()
        self.assertEqual(self.registry.binding(KEY).config_sha, self.new_sha)
        self.assertEqual(self.state()["bundle_resume"], receipt)
        self.assertEqual(self.state()["calls"], calls)

    def test_failure_before_checkpoint_does_not_change_registry(self):
        """Runner configuration never advances ahead of the authoritative checkpoint."""
        self.prepare()
        self.fake.comment("/concurro resume")
        from checkpoints import Checkpoints

        with patch.object(Checkpoints, "save", side_effect=Transient("save unavailable")):
            self.run_one()
        self.assertEqual(self.registry.binding(KEY).config_sha, self.old_sha)
        self.assertNotIn("bundle_resume", self.state())

    def test_adoption_is_owned_and_compare_and_swap(self):
        """An old worker cannot overwrite another execution pin or revive an expired lease."""
        self.registry.bind(KEY, KEY[0], KEY[0], "a" * 40)
        generation = self.gateway.acquire(KEY, "resume-test")
        self.registry.adopt_config(KEY, generation, "a" * 40, "b" * 40)
        self.registry.adopt_config(KEY, generation, "a" * 40, "b" * 40)
        with self.assertRaises(RegistryError):
            self.registry.adopt_config(KEY, generation, "a" * 40, "c" * 40)
        self.gateway.release(KEY, generation, None)
        with self.assertRaises(RegistryError):
            self.registry.adopt_config(KEY, generation, "b" * 40, "c" * 40)

    def test_lost_registry_reply_keeps_adoption_idempotent(self):
        """A committed registry update does not cause another resume when its reply is lost."""
        self.prepare()
        self.fake.comment("/concurro resume")
        adopt = self.registry.adopt_config

        def lost(*args):
            """Commit the update, then lose its response."""
            adopt(*args)
            raise Transient("lost registry reply")

        with patch.object(self.registry, "adopt_config", side_effect=lost):
            self.run_one()
        self.assertEqual(self.registry.binding(KEY).config_sha, self.new_sha)
        before = deepcopy(self.state()["bundle_resume"])
        self.run_one()
        self.assertEqual(self.state()["bundle_resume"], before)

    def test_cleanup_uses_each_runners_recorded_configuration(self):
        """Adopting a new config must not strand old configuration-pinned runner refs."""
        from types import SimpleNamespace
        from unittest.mock import Mock

        from service.admission import RUN_BRANCH_PREFIX

        self.prepare()
        generation = self.gateway.acquire(KEY, "old-run")
        session = "a" * 32
        self.registry.register_runner(KEY, generation, session, self.old_sha)
        self.gateway.release(KEY, generation, None)
        generation = self.gateway.acquire(KEY, "adopt")
        self.registry.adopt_config(KEY, generation, self.old_sha, self.new_sha)
        self.gateway.release(KEY, generation, None)
        ref = "refs/heads/" + RUN_BRANCH_PREFIX + session
        reader = SimpleNamespace(request=Mock(return_value=[{"ref": ref, "object": {"sha": self.old_sha}}]))
        with (
            patch.object(self.worker, "_operator", return_value=Mock()),
            patch.object(self.worker, "reader", return_value=reader),
            patch.object(self.gateway, "mutate") as mutate,
        ):
            self.worker.cleanup_runners()
        self.assertEqual(mutate.call_args.args[2], "DELETE")
        self.assertTrue(self.registry.runner_session(session)["removed"])
