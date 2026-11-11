"""Removed work-shape ceilings retain durable evidence and explicit resource budgets."""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from support import CONFIG, ROOT, TARGET, setUpModule as setUpModule, tearDownModule as tearDownModule

import conversation
import design
import pr_description
import reviews
from artifact_fixtures import fixed_parts
from bundle import Bundle
from controller import validate_config
from fakes import PLAN, FakeGitHub, named_config
from pipeline_fixtures import PipelineFixture
from policy import Blocked, validate_plan, validate_pr_review
from report_store import Store
from session_entry import SessionTurn
from test_pr_descriptions import COMMIT, DESCRIPTION


class ConfigTests(unittest.TestCase):
    """Operator policy scales without adding one setting per list or file."""

    def test_models_reviewers_documents_and_globs_have_no_count_or_length_ceiling(self):
        config = named_config()
        for i in range(40):
            name = "profile" + "x" * 80 + str(i)
            config["models"][name] = {"provider": "openai", "model": "model" + "x" * 220 + str(i), "effort": "high"}
            config["agents"]["plan_reviewers"].append(name)
            config["agents"]["pr_reviewers"].append(name)
        long_path = "/".join(["segment" * 10] * 5)
        config["policy_documents"] = [f"{long_path}/{i}.md" for i in range(40)]
        config["paths"] = {
            "protected_extensions": [],
            "protected": [f"p{i}/**" for i in range(40)],
            "generated": [f"{long_path}/{i}/**" for i in range(40)],
        }
        validate_config(config)
        config["agents"]["plan_reviewers"] = ["sol-high"]
        with self.assertRaises(Blocked):
            validate_config(config)

    def test_resource_settings_validate_types_and_keep_defaults(self):
        for name in ("turn_timeout_seconds", "max_retained_bytes"):
            for value in (None, 1, 1000000000):
                validate_config({**CONFIG, name: value})
            for value in (0, -1, True, 1.5, "1"):
                with self.subTest(name=name, value=value), self.assertRaises(Blocked):
                    validate_config({**CONFIG, name: value})

    def test_large_instruction_and_charter_resources_are_preserved(self):
        charter = json.loads((TARGET / ".concurro/charter.json").read_text())
        charter["purpose"] = "complete authority " * 2000
        instructions = {".concurro/instructions/shared.md": "Long instructions. " * 7000}
        bundle = Bundle(ROOT, charter_text=json.dumps(charter), instruction_texts=instructions)
        self.assertEqual(bundle.instruction_texts, instructions)
        self.assertEqual(json.loads(bundle.charter_text), charter)

    def test_uncompressed_retained_storage_budget_is_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "transcript").write_text("x" * 100)
            session = {"scope": "a" * 64, "turn": 0}
            with self.assertRaisesRegex(ValueError, "storage budget"):
                SessionTurn(session, "{}", root, max_bytes=99)
            for bound in (100, None):
                turn = SessionTurn(session, "{}", root, max_bytes=bound)
                turn.lock.close()


class OutputTests(PipelineFixture, unittest.TestCase):
    """Large accepted results remain available after process restart and publication."""

    def test_large_plan_is_accepted_archived_and_reloaded(self):
        self.reach("plan")
        plan = deepcopy(PLAN)
        plan["summary"] = "Detailed reasoning 🌍. " * 4000
        plan["publication"]["changes"] = ["Complete change " * 30] * 12
        validate_plan(plan, self.config)
        with patch.object(self.agents, "ask", return_value=plan):
            controller = self.tick()
        self.assertEqual(controller.state["stage"], "plan_qc")
        self.assertEqual(self.loaded().state["plan"], plan)
        view = controller.checkpoints.plan_body(controller.state)
        self.assertLessEqual(len(view.encode()), 60000)
        self.assertIn("Complete plan data, part", view)
        self.assertIn('"version": 7', controller.checkpoints.audit_body(controller.state))

    def test_pending_events_persist_and_publish_beyond_eight(self):
        controller = self.reach("plan")
        for i in range(12):
            conversation.queue(controller.state, "Queued notice", f"event number {i}")
        controller.save(publish_events=False, outboxes=False)
        restored = self.loaded()
        self.assertEqual(len(restored.state["conversation"]["pending"]), 12)
        restored.save()
        self.assertFalse(self.loaded().state["conversation"]["pending"])
        self.assertEqual(sum("### Queued notice" in c["body"] for c in self.gh.comments), 12)

    def test_report_parts_beyond_128_round_trip(self):
        gh = FakeGitHub(deepcopy(CONFIG))
        store = Store(gh, 7, CONFIG["bot_login"], CONFIG["repository"])
        # Small chunks exercise the same part index without a multi-megabyte fixture.
        with fixed_parts(100):
            report = {"evidence": "é" * 20000}
            key = store.put(report)
        self.assertGreater(len(store.numbers[key]), 128)
        self.assertEqual(Store(gh, 7, CONFIG["bot_login"], CONFIG["repository"]).get(key, store.numbers[key]), report)

    def test_large_pr_view_links_to_preserved_candidate(self):
        """Long implementation prose fits the PR endpoint without losing canonical content."""
        self.config["autonomous"] = True
        controller = self.reach("publish")
        controller.state["candidate"]["description"] = deepcopy(DESCRIPTION)
        controller.state["candidate"]["description"]["lead"] = "Detailed evidence 🌍. " * 5000
        controller.save(publish_events=False, outboxes=False)
        self.assertEqual(self.loaded().state["candidate"], controller.state["candidate"])
        body = controller.pr_body()
        self.assertLessEqual(len(body.encode()), 60000)
        self.assertIn("Full implementation evidence", body)
        self.assertIn("Closes", body)

    def test_candidate_preflight_measures_the_published_excerpt(self):
        """A long description does not fail review preflight after its PR view is bounded."""
        self.config["autonomous"] = True
        controller = self.reach("publish")
        description = deepcopy(DESCRIPTION)
        description["lead"] = "Detailed explanation. " * 10000
        controller.config["max_context_bytes"] = 100000
        before, after = {"docs/guide.md": "old"}, {"docs/guide.md": "new"}
        self.assertIsNone(controller.oversized(None, before, after, description={"description": description}))

    def test_many_findings_render_without_exceeding_publication_capacity(self):
        """A large finding table is a view; the complete report remains canonical."""
        report = {
            "verdict": "changes_requested",
            "summary": "Complete evidence",
            "dispositions": [],
            "design_issue": None,
            "model": "test",
            "findings": [
                {"path": "long/" * 50 + "file.md", "line": 1, "start_line": None, "detail": "Evidence 🌍 " * 8}
                for _ in range(200)
            ],
        }
        state = {"issue": 7, "plan_id": "a" * 64}
        reviews.apply(state, "claude", report, "b" * 40, "https://github.com/o/r/issues/7")
        item = {"format": reviews.FORMAT, "head": "b" * 40, "sequence": 1, "provider": "claude"}
        findings = state["findings"]
        body = reviews.review_body(state, CONFIG, item, report, findings, {f["id"]: "body" for f in findings})
        self.assertLessEqual(len(body.encode()), reviews.REVIEW_BODY_LIMIT)
        self.assertTrue(body.startswith(reviews.review_marker(state, CONFIG, 1)))
        self.assertEqual(len(findings), 200)

    def test_older_review_rendering_remains_replayable(self):
        """Old Unicode bodies retain character accounting; new reviews consistently count bytes."""
        report = {
            "verdict": "approve",
            "summary": "é" * 35000,
            "model": "test",
            "dispositions": [],
            "design_issue": None,
        }
        state = {"issue": 7, "plan_id": "a" * 64}
        item = {"head": "b" * 40, "sequence": 1, "provider": "claude"}
        legacy = reviews.review_body(state, CONFIG, {**item, "format": 4}, report, [], {})
        current = reviews.review_body(state, CONFIG, {**item, "format": reviews.FORMAT}, report, [], {})
        self.assertIn(report["summary"], legacy)
        self.assertGreater(len(legacy.encode()), reviews.REVIEW_BODY_LIMIT)
        self.assertLess(len(legacy), reviews.REVIEW_BODY_LIMIT)
        self.assertLessEqual(len(current.encode()), reviews.REVIEW_BODY_LIMIT)

    def test_description_commit_and_validation_keep_full_text(self):
        report = {"description": deepcopy(DESCRIPTION), "commit": deepcopy(COMMIT)}
        report["description"]["lead"] = "Long lead. " * 60
        report["description"]["changes"] *= 12
        report["commit"]["subject"] = "Long subject " * 20
        report["commit"]["changes"] *= 12
        pr_description.validate(report)
        state = {"head": "a" * 40, "issue": 7, "round": 1}
        text = pr_description.commit_message(state, CONFIG, report["commit"])
        self.assertIn(report["commit"]["subject"].strip(), text)
        self.assertEqual(text.count("\n- "), len(report["commit"]["changes"]))

    def test_many_criteria_and_findings_preserve_long_paths(self):
        self.assertGreater(
            len(design.criteria({"title": "Task", "body": "\n\n".join(f"Requirement {i}" for i in range(50))})), 32
        )
        path = "/".join(["long-segment" * 8] * 4) + "/file.rs"
        finding = {"path": path, "line": 1, "start_line": None, "detail": "Evidence", "severity": "high"}
        report = {
            "verdict": "changes_requested",
            "summary": "All findings",
            "findings": [finding] * 30,
            "dispositions": [],
            "design_issue": None,
        }
        validate_pr_review(report, [], [])
