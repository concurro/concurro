"""Schema evolution verdicts for additive and breaking edits."""

import sys
import unittest
import json
import os
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from check_compat import compatible, main  # noqa: E402


class CompatibilityTests(unittest.TestCase):
    """Only optional properties and open-enum additions stay in a version."""

    def test_optional_property(self):
        """A new optional field is compatible; a required one is not."""
        old = {"properties": {"id": {"type": "integer"}}, "required": ["id"]}
        new = {"properties": {"id": {"type": "integer"}, "note": {"type": "string"}}, "required": ["id"]}
        self.assertIsNone(compatible(old, new))
        new["required"].append("note")
        self.assertIsNotNone(compatible(old, new))

    def test_push_uses_previous_head_and_zero_has_no_baseline(self):
        """Main pushes compare their exact before SHA; initial pushes have no prior schema."""
        before = "a" * 40
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "push"}), patch.object(sys, "argv", ["compat", before]):
            with patch("check_compat.subprocess.run", return_value=Mock(stdout="")) as run:
                main()
                self.assertEqual(run.call_args.args[0][:5], ["git", "ls-tree", "-r", "--name-only", before])
        with patch.object(sys, "argv", ["compat", "0" * 40]):
            with patch("check_compat.subprocess.run") as run:
                main()
                run.assert_not_called()

    def test_enum_and_constraint(self):
        """Only marked open enums may grow; bounds never change in place."""
        open_old = {"type": "string", "enum": ["User"], "x-concurro-open": True}
        self.assertIsNone(compatible(open_old, {**open_old, "enum": ["User", "Bot"]}))
        closed_old = {"type": "string", "enum": ["open"]}
        self.assertIsNotNone(compatible(closed_old, {**closed_old, "enum": ["open", "closed"]}))
        self.assertIsNotNone(compatible({"minimum": 0}, {"minimum": 1}))

    def test_descriptions_only(self):
        """Editorial changes do not change the wire version."""
        self.assertIsNone(compatible({"type": "object", "description": "old"}, {"type": "object", "description": "new"}))

    def test_generated_optional_property(self):
        """Use the actual generated forms for optional additions."""
        schemas = Path(__file__).resolve().parents[2] / "schemas"
        for mode in ("de", "ser"):
            old = json.loads((schemas / f"github.issues.v1.{mode}.json").read_text())
            new = deepcopy(old)
            issue = new["$defs"]["Issue"]
            issue["properties"]["future_note"] = deepcopy(issue["properties"]["body"])
            if mode == "ser":
                issue["required"].append("future_note")
            self.assertIsNone(compatible(old, new, mode=mode))
            issue["properties"]["future_note"] = {"type": "string"}
            if mode == "de":
                issue["required"].append("future_note")
            self.assertIsNotNone(compatible(old, new, mode=mode))

    def test_generated_open_action(self):
        """A known action may be added to the generated open union."""
        schema = json.loads(
            (Path(__file__).resolve().parents[2] / "schemas/github.issues.v1.de.json").read_text()
        )
        updated = deepcopy(schema)
        issue = updated["$defs"]["GithubIssues"]
        branches = issue["anyOf"][0]["oneOf"]
        added = deepcopy(branches[0])
        added["properties"]["action"]["const"] = "converted"
        branches.append(added)
        action = issue["anyOf"][1]["properties"]["action"]
        action["pattern"] = action["pattern"].replace("|deleted)", "|deleted|converted)")
        self.assertIsNone(compatible(schema, updated))
        added["properties"]["new_required"] = {"type": "string"}
        added["required"].append("new_required")
        self.assertIsNotNone(compatible(schema, updated))
        added["required"].remove("new_required")
        self.assertIsNone(compatible(schema, updated))
        action["pattern"] = "^.*$"
        self.assertIsNotNone(compatible(schema, updated))


if __name__ == "__main__":
    unittest.main()
