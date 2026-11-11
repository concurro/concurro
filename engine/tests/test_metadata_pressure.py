"""Checkpoint pressure must not discard metadata already stored outside the audit."""

import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import classification
import report_store
import review_timing
from checkpoints import Checkpoints
from controller import Controller
from fakes import CONFIG, FakeAgents, FakeGitHub
from test_review_timing import OBSERVED


def rated_report(source):
    """Create archived evidence with annotations that can independently remain inline."""
    return {
        "decision": "approve",
        "rationale": "Canonical evidence. " * 200,
        "classification": {"source": {"report": source}, "rationale": "R" * 600, "unknowns": ["U" * 200]},
    }


class MetadataPressureTests(unittest.TestCase):
    """Use real packing, artifact saves and reloads near the audit's publication limit."""

    def setUp(self):
        """Give each case independent fake GitHub artifacts."""
        self.gh = FakeGitHub(CONFIG)
        self.cp = Checkpoints(self.gh, CONFIG, 7)

    def size(self, state):
        """Measure the actual public checkpoint, including artifact references."""
        return len(self.cp.audit_body(state).encode())

    def crowded(self):
        """Persist detached histories, then add unrelated inline pressure below the hard limit."""
        state = {
            "version": 2,
            "issue": 7,
            "reports": [
                {"artifact": i + 1, "assessment": str(i), "timing": deepcopy(OBSERVED), "note": "index " * 100}
                for i in range(24)
            ],
            "review_history": [
                {"report": rated_report(str(i)), "classification_snapshot": {"note": "snapshot " * 80}}
                for i in range(24)
            ],
            "conversation": {
                "pending": [{"index": {"timing": deepcopy(OBSERVED)}, "body": "pending " * 100} for _ in range(24)]
            },
            "operation": {"status": "completed", "timing": deepcopy(OBSERVED), "response": {"ok": True}},
            "classification_snapshot": {"note": "inline fallback"},
            "padding": "",
        }
        self.cp.save(state)
        state["padding"] = "x" * (59500 - self.size(state))
        self.cp.save(state)
        self.assertEqual(self.size(state), 59500)
        return state

    def test_pressure_preserves_detached_metadata_and_does_not_rewrite_blocks(self):
        """Timing, ratings, snapshots and assessment links outside the audit remain identical."""
        state = self.crowded()
        expected = deepcopy(state)
        expected["operation"].pop("timing")
        expected.pop("classification_snapshot")
        before = set(self.gh.artifacts[7])
        controller = Controller(self.gh, FakeAgents(), CONFIG)
        controller.state, controller.checkpoints = state, self.cp
        controller.checkpoint()
        self.assertEqual(state, expected)
        self.assertEqual(set(self.gh.artifacts[7]), before, "metadata relief must not rewrite detached blocks")
        self.assertEqual(Checkpoints(self.gh, CONFIG, 7).load([]), expected)

    def test_detached_core_still_allows_inline_classification_relief(self):
        """An individual report reference carries inline annotations, unlike a detached history."""
        state = self.crowded()
        state["qc_history"] = [rated_report("0")]
        state["qc_history"][0]["classification"] = state["review_history"][0]["report"]["classification"]
        # Stay below the spill threshold so this case still exercises inline relief.
        size = len(self.cp.render_audit(self.cp.audit_payload(state)).encode())
        state["padding"] = state["padding"][: -(size - 59900)]
        self.assertEqual(self.size(state), 59900)
        before = deepcopy(state["review_history"])
        _, refs = report_store.packed(state)
        ref = next(r for r in refs if r["path"] == ["qc_history", 0])
        self.assertIn("classification", ref["annotations"])
        classification.make_room(state, 59000, self.size)
        self.assertNotIn("classification", state["qc_history"][0])
        self.assertEqual(state["review_history"], before)

    def test_pressure_still_removes_inline_timing_and_assessment_links(self):
        """Small inline records retain their existing pressure-relief behavior."""
        state = {
            "version": 2,
            "issue": 7,
            "reports": [{"artifact": 1, "assessment": "old", "timing": deepcopy(OBSERVED)}],
            "conversation": {"pending": [{"index": {"timing": deepcopy(OBSERVED)}}]},
            "review_batch": {"members": {"a": {"operation": {"timing": deepcopy(OBSERVED)}}}},
        }
        classification.make_room(state, 0, self.size)
        review_timing.make_room(state, 0, self.size)
        self.assertEqual(state["reports"], [{"artifact": 1}])
        self.assertEqual(state["conversation"]["pending"], [{"index": {}}])
        self.assertEqual(state["review_batch"]["members"]["a"]["operation"], {})

    def test_manifest_detection_uses_the_record_encoder(self):
        """Changing indentation for both writes and replay keeps block detection aligned."""
        values = list(range(24))
        with patch("report_store.record_json", side_effect=lambda value: json.dumps(value, sort_keys=True, indent=4)):
            key = self.cp.reports.put(values)
            before = deepcopy(self.gh.artifacts[7])
            fresh = report_store.Store(self.gh, 7, CONFIG["bot_login"], CONFIG["repository"])
            self.assertEqual(fresh.put(values), key)
            self.assertEqual(fresh.get(key), values)
            self.assertEqual(self.gh.artifacts[7], before)
