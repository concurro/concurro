"""Behavior pinned before extracting the shared review-policy dependencies."""

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from itertools import permutations
from unittest.mock import patch

from support import ENGINE

import classification
import model_config
from policy import Blocked, design_roster, validate_pr_review, validate_review

ISSUE = {"kind": "plan", "explanation": "Needs a different design", "evidence": ["Observed incompatibility"]}
LOCAL = {"verdict": "approve", "summary": "Reviewed", "findings": [], "design_issue": None}
PR = {**LOCAL, "dispositions": []}
NAMED = {
    "models": {
        "opus": {"provider": "anthropic", "model": "claude-test", "effort": "medium"},
        "sol": {"provider": "openai", "model": "gpt-test", "effort": "high"},
    },
    "agents": {
        "triage": "sol",
        "planner": "opus",
        "plan_reviewers": ["opus", "sol"],
        "implementer": "opus",
        "pr_reviewers": ["sol", "opus"],
    },
}


class ValidatorContractTests(unittest.TestCase):
    """Each table combines failures to pin precedence as well as error text."""

    def test_local_review_first_errors(self):
        cases = [
            ({"extra": True, "headline": 1, "verdict": "invalid"}, "invalid presentation field"),
            ({"extra": True, "manifests": None, "verdict": "invalid"}, "invalid manifest assessments"),
            ({"extra": True, "verdict": "invalid"}, "invalid review fields"),
            ({"verdict": "invalid", "summary": None}, "invalid review verdict"),
            ({"verdict": [], "summary": None}, "invalid review verdict"),
            ({"summary": None, "design_issue": ISSUE}, "invalid review content"),
            (
                {"verdict": "reopen", "findings": [{}], "design_issue": {}},
                "design reopening requires structured evidence",
            ),
            (
                {"verdict": "reopen", "findings": [{}], "design_issue": ISSUE, "summary": "x" * 16001},
                "design reopening cannot also request implementation fixes",
            ),
            ({"design_issue": ISSUE, "findings": [{}]}, "review verdict contradicts design issue"),
            ({"findings": [{}], "summary": "x" * 16001}, "review verdict contradicts findings"),
            ({"verdict": "changes_requested", "findings": [{}], "summary": "x" * 16001}, "review too large"),
            ({"verdict": "changes_requested", "findings": [{}, {"path": 1, "detail": 2}]}, "invalid finding"),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes), self.assertRaises(Blocked) as caught:
                validate_review({**deepcopy(LOCAL), **changes})
            self.assertIs(type(caught.exception), Blocked)
            self.assertEqual(str(caught.exception), expected)
        with self.assertRaises(Blocked) as caught:
            validate_review({**LOCAL, "verdict": "invalid"}, manifests=["Cargo.toml"])
        self.assertEqual(str(caught.exception), "review must assess every changed manifest: Cargo.toml")

    def test_pr_review_first_errors(self):
        cases = [
            ({"extra": True, "headline": 1, "verdict": "invalid"}, "invalid presentation field"),
            ({"extra": True, "manifests": None, "verdict": "invalid"}, "invalid manifest assessments"),
            ({"extra": True, "verdict": "invalid"}, "invalid review fields"),
            ({"verdict": "invalid", "summary": None}, "invalid review verdict"),
            ({"verdict": {}, "summary": None}, "invalid review verdict"),
            ({"summary": None, "findings": [{}] * 21}, "invalid review content"),
            ({"dispositions": None, "design_issue": ISSUE}, "invalid review content"),
            ({"verdict": "reopen", "findings": [{}] * 21, "design_issue": {}}, "review too large"),
            (
                {"verdict": "reopen", "findings": [{}], "design_issue": {}},
                "design reopening requires structured evidence",
            ),
            (
                {"verdict": "reopen", "findings": [{}], "design_issue": ISSUE},
                "design reopening cannot also report or answer findings",
            ),
            (
                {"verdict": "reopen", "dispositions": [{}], "design_issue": ISSUE},
                "design reopening cannot also report or answer findings",
            ),
            ({"findings": [{}], "design_issue": ISSUE}, "review verdict contradicts design issue"),
            ({"findings": [{}], "dispositions": [{}]}, "invalid finding"),
            ({"dispositions": [{}]}, "invalid disposition"),
            (
                {"dispositions": [{"id": "missing", "status": "other", "evidence": "Observed"}]},
                "invalid disposition status",
            ),
            (
                {"dispositions": [{"id": "missing", "status": "open", "evidence": "Observed"}]},
                "dispositions must answer each of the reviewer's open and withdrawn findings once",
            ),
            ({"verdict": "changes_requested"}, "review verdict contradicts findings"),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes), self.assertRaises(Blocked) as caught:
                validate_pr_review({**deepcopy(PR), **changes}, [], [])
            self.assertIs(type(caught.exception), Blocked)
            self.assertEqual(str(caught.exception), expected)
        with self.assertRaises(Blocked) as caught:
            validate_pr_review({**PR, "verdict": "invalid"}, [], [], manifests=["Cargo.toml"])
        self.assertEqual(str(caught.exception), "review must assess every changed manifest: Cargo.toml")

    def test_distinct_acceptance_limits_and_advisory_fields(self):
        reopened = {"verdict": "reopen", "summary": "x" * 16001, "design_issue": ISSUE}
        self.assertIsNone(validate_review({**LOCAL, **reopened}))
        self.assertIsNone(validate_pr_review({**PR, **reopened}, [], []))
        self.assertIsNone(validate_pr_review({**PR, "summary": "x" * 16001}, [], []))
        self.assertIsNone(validate_pr_review({**PR, "classification": {"unknown": "ignored"}}, [], []))
        with self.assertRaises(Blocked) as caught:
            validate_review({**LOCAL, "classification": {"unknown": "ignored"}})
        self.assertEqual(str(caught.exception), "invalid review fields")
        # Local limits count JSON characters (including escaping), not UTF-8 source bytes.
        self.assertIsNone(validate_review({**LOCAL, "summary": "é" * 2600}))
        with self.assertRaises(Blocked) as caught:
            validate_review({**LOCAL, "summary": "é" * 2700})
        self.assertEqual(str(caught.exception), "review too large")


class ImportAndRosterContractTests(unittest.TestCase):
    """Fresh imports and serialized identities remain independent of import order."""

    def test_fresh_import_orders(self):
        modules = ("policy", "classification", "reviews", "design")
        for order in [*((module,) for module in modules), *permutations(modules)]:
            with self.subTest(order=order), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-c",
                        "import importlib, sys; sys.path.insert(0, sys.argv[1]); "
                        "[importlib.import_module(name) for name in sys.argv[2:]]; print('ok')",
                        str(ENGINE),
                        *order,
                    ],
                    cwd=directory,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "ok\n")

    def test_classification_core_projection(self):
        nested = {"classification": "nested data"}
        report = {"summary": "Reviewed", "classification": {"risk": "low"}, "evidence": nested}
        before = deepcopy(report)
        projected = classification.core(report)
        self.assertEqual(
            json.dumps(projected), '{"summary": "Reviewed", "evidence": {"classification": "nested data"}}'
        )
        self.assertIsNot(projected, report)
        self.assertIs(projected["evidence"], nested)
        self.assertEqual(report, before)
        for value in (None, "text", 1, [report]):
            self.assertIs(classification.core(value), value)

    def test_core_leaf_and_compatible_reexport(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]); import report_core; "
                    "assert not {'policy', 'classification', 'reviews', 'model_config'} & sys.modules.keys(); "
                    "import classification; assert classification.core is report_core.core; print('ok')",
                    str(ENGINE),
                ],
                cwd=directory,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ok\n")

    def test_named_roster_resolves_each_member_once(self):
        resolve = model_config.ModelConfiguration.resolve
        with patch.object(model_config.ModelConfiguration, "resolve", autospec=True, side_effect=resolve) as called:
            design_roster(NAMED)
        self.assertEqual([call.args[1] for call in called.call_args_list], ["opus", "sol"])

    def test_named_roster_bytes(self):
        expected = [
            {
                "id": "b10bf000da308e40a95fa6468adb47d64bcc44e80f785ad14d128de1657a0765",
                "provider": "opus",
                "model": "claude-test",
                "effort": "medium",
                "agent": {
                    "profile": "opus",
                    "provider": "anthropic",
                    "model": "claude-test",
                    "effort": "medium",
                    "harness": "claude",
                },
                "roles": ["plan_qc"],
            },
            {
                "id": "d55532b87dda2f27227daa4f3dd8ebc48c64fa47ea6223a86709ab8b01a5b7dd",
                "provider": "sol",
                "model": "gpt-test",
                "effort": "high",
                "agent": {
                    "profile": "sol",
                    "provider": "openai",
                    "model": "gpt-test",
                    "effort": "high",
                    "harness": "codex",
                },
                "roles": ["plan_qc"],
            },
        ]
        self.assertEqual(json.dumps(design_roster(NAMED)), json.dumps(expected))
        reversed_config = deepcopy(NAMED)
        reversed_config["agents"]["plan_reviewers"].reverse()
        self.assertEqual(json.dumps(design_roster(reversed_config)), json.dumps(expected[::-1]))

    def test_legacy_roster_bytes(self):
        config = {
            "planner": "claude",
            "implementer": "codex",
            "qc": "claude",
            "qc_model": "qc-test",
            "reviewers": ["codex", "claude"],
            "models": {"claude": "claude-test", "codex": "gpt-test"},
        }
        expected = [
            {
                "id": "9c9aa8eabf0fb252d9acf2df632266029fb39c1057a5a0f42c78f2e9f861a296",
                "provider": "claude",
                "model": "claude-test",
                "roles": ["plan", "review"],
            },
            {
                "id": "cf97c9c08c27cc3ad70c779cedea0d0de757c734912f9650689c91fe0a930ca1",
                "provider": "codex",
                "model": "gpt-test",
                "roles": ["develop", "review"],
            },
            {
                "id": "70f774399d37279d5bdaeddded43c1cb96160eda325dd38aa082b78dd9fbe1cf",
                "provider": "claude",
                "model": "qc-test",
                "roles": ["intake"],
            },
        ]
        self.assertEqual(json.dumps(design_roster(config)), json.dumps(expected))
        config["qc_model"] = ""
        expected[0]["roles"] = ["plan", "intake", "review"]
        self.assertEqual(json.dumps(design_roster(config)), json.dumps(expected[:2]))
