"""Target migration preserves explicit policy and refuses silently discarded custom profiles."""

from copy import deepcopy
import unittest
from fixtures import CONFIG
from service.migrate_target import migrate


class MigrationTests(unittest.TestCase):
    def test_rust_config_preserves_policy_and_removes_retired_fields(self):
        config = {**deepcopy(CONFIG), "execution_profiles": ["rust"], "merge_enabled": False,
                  "required_checks": ["Rust"], "max_local_seconds": 900}
        result = migrate(config)
        self.assertEqual(result["paths"]["protected_files"], config["paths"]["protected_files"])
        self.assertIn("**/Cargo.lock", result["paths"]["generated"])
        self.assertNotIn("execution_profiles", result)
        self.assertNotIn("merge_enabled", result)
        self.assertIn("execution_profiles", config)

    def test_custom_profile_requires_explicit_translation(self):
        with self.assertRaises(ValueError):
            migrate({**CONFIG, "execution_profiles": ["target:custom"]})

    def test_native_target_is_unchanged(self):
        self.assertEqual(migrate(CONFIG), CONFIG)
