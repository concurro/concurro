"""YAML and JSON target configs share validation, pinning and profile assignment semantics."""

import json
import unittest
import yaml

from fixtures import ENGINE, KEY, ServiceFixture
from support import TARGET, setUpModule as setUpModule, tearDownModule as tearDownModule
from fakes import named_config
from controller import config_digest, validate_config
from policy import Blocked
from service.target_config import decode, read_remote
from github import NotFound


class DecodeTests(unittest.TestCase):
    """YAML adds comments and unquoted keys without executable constructors or ambiguous values."""

    def test_example_yaml_is_valid(self):
        path = TARGET / ".concurro/config.yaml"
        config = decode(path.read_text(), path)
        validate_config(config)
        self.assertEqual(config["agents"]["triage"], "sol61-medium")

    def test_yaml_and_json_resolve_to_the_same_configuration(self):
        config = named_config()
        decoded = decode(yaml.safe_dump(config), "config.yaml")
        validate_config(decoded)
        self.assertEqual(decoded, decode(json.dumps(config), "config.json"))
        self.assertEqual(config_digest(config), config_digest(decoded))

    def test_yaml_rejects_unsafe_tags_duplicates_and_aliases(self):
        for text in ("!!python/object/apply:os.system ['false']", "models: {}\nmodels: {}", "a: &a [*a]", "[one, two]", "bad: ["):
            with self.subTest(text=text), self.assertRaises(Blocked):
                decode(text, "config.yaml")
        with self.assertRaises(Blocked):
            decode('{"models":{}, "models":{}}', "config.json")

    def test_config_source_is_unambiguous(self):
        for files in ({}, {".concurro/config.yaml": "{}", ".concurro/config.json": "{}"}):
            def read(path):
                if path not in files:
                    raise NotFound()
                return files[path]
            with self.assertRaises(Blocked):
                read_remote(read)


class TargetLoadingTests(ServiceFixture, unittest.TestCase):
    """The worker loads named YAML at the same pinned config commit as JSON."""

    def test_worker_runs_named_yaml_configuration(self):
        self.config = named_config()
        self.fake.config = self.config
        self.backend.files.pop(".concurro/config.json")
        self.backend.files[".concurro/config.yaml"] = yaml.safe_dump({**self.config, "engine": ENGINE})
        self.service()
        loaded = self.worker.load(*KEY)
        self.assertEqual(loaded.config["agents"], self.config["agents"])
        self.assertEqual(loaded.config["config_sha"], "a" * 40)
        self.reconciler.poll()
        self.drain()
        self.assertTrue(self.agents.calls)
        self.assertEqual(self.agents.calls[0][:2], ("sol-medium", "intake"))

    def test_both_remote_formats_are_rejected(self):
        self.backend.files[".concurro/config.yaml"] = "{}"
        self.service()
        with self.assertRaisesRegex(Blocked, "exactly one"):
            self.worker.load(*KEY)
