"""Design evaluation uses fresh native runner fixtures, never a host model process."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import CONFIG, TARGET, setUpModule as setUpModule, tearDownModule as tearDownModule

import turns
from bundle import Bundle
from design_eval import SOURCE, cases, evaluate, request, score


class EvaluationTests(unittest.TestCase):
    """Fixtures are unlabeled to the model, fresh, and executable without external calls in tests."""

    def test_requests_are_fresh_and_do_not_expose_expected_labels(self):
        bundle = Bundle(target=TARGET)
        first, second = [request(cases()[0], "codex", CONFIG, bundle, "a" * 40) for _ in range(2)]
        self.assertNotEqual(first["controller_evaluation"], second["controller_evaluation"])
        self.assertEqual(first["review_phase"], "fresh")
        self.assertNotIn("expected", first)
        self.assertNotIn("id", first)
        self.assertNotIn("controller_session", first)
        self.assertNotIn("validation", first["plan"])
        self.assertEqual(score([{"id": c["id"], "decision": c["expected"]} for c in cases()])["correct"], 4)

    def test_runner_builds_fixture_locally_without_fetching_target_history(self):
        code = (
            "import json,pathlib,sys; r=json.load(sys.stdin); "
            'print(json.dumps({"text":(pathlib.Path(r["workspace"])/"docs/workflow.md").read_text(),'
            '"head":r["context"]["repository"]["head"]}))'
        )
        turn = {
            "request": {
                "stage": "plan_qc",
                "provider": "codex",
                "evaluation": "a" * 32,
                "session": None,
                "context": {},
                "gateway": {"url": "https://invalid", "headers": {}},
            },
            "spec": {"refs": {"main": "b" * 40}, "head": "main", "base_branch": "main", "candidate": None},
            "timeout": 60,
            "retained": None,
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(turns, "command", return_value=[sys.executable, "-c", code]),
        ):
            root = Path(directory) / "turn-00000000000000000000000000000001"
            root.mkdir()
            result = turns.execute(turn, "session-key", None, root)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["response"], {"text": SOURCE["docs/workflow.md"], "head": "fixture"})
        self.assertIsNone(result["workspace"])
        self.assertIsNone(result["retained"])

    def test_results_are_saved_even_for_invalid_provider_output(self):
        class Agents:
            bundle = Bundle(target=TARGET)

            def ask(self, provider, stage, context):
                self.calls.append((provider, stage, context))
                return {"invalid": True}

        agents = Agents()
        agents.calls = []
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "results"
            result = evaluate(agents, CONFIG, "codex", "a" * 40, output)
            self.assertEqual(result["invalid"], 4)
            self.assertEqual(len(list(output.glob("*.json"))), 5)
            self.assertEqual(json.loads((output / "summary.json").read_text())["cases"], 4)
        self.assertEqual(len({c[2]["controller_evaluation"] for c in agents.calls}), 4)
