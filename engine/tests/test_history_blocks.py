"""Fixed-position history blocks, retained indexes, compatibility and artifact traffic."""

import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import report_artifact
import report_store
from artifact_fixtures import fixed_parts
from checkpoints import AUDIT_MARKER, STATE_PREFIX, CheckpointError, Checkpoints, legacy_state
from fakes import CONFIG, FakeGitHub
from policy import digest
from report_store import MARKER, Store
from storage_traffic import history_entry, measure


class HistoryBlockTests(unittest.TestCase):
    """Immutable blocks reuse reports and remain verifiable after process loss."""

    def setUp(self):
        """Use isolated fake artifacts with real production storage."""
        self.gh = FakeGitHub(CONFIG)
        self.cp = Checkpoints(self.gh, CONFIG, 7)

    def store(self):
        """Discard process caches to exercise verification and discovery."""
        return Store(self.gh, 7, CONFIG["bot_login"], CONFIG["repository"])

    def record(self, store, key):
        """Inspect emitted JSON independently of the production decoder."""
        parts = [self.gh.artifacts[7][n]["artifact"]["body"] for n in store.numbers[key]]
        return json.loads(
            "".join(
                report_artifact.parse(p, key)["part"]
                if report_artifact.DATA in p
                else p.split("\n```", 1)[1].split("\n", 1)[1].split("\n```", 1)[0]
                for p in parts
            )
        )

    def test_append_and_metadata_update_only_replace_affected_block(self):
        """Closed positions remain stable even when an old entry grows considerably."""
        store = self.store()
        values = [history_entry(i) for i in range(25)]
        first = self.record(store, store.put(values))
        self.assertEqual([r["count"] for r in first["blocks"]], [8, 8, 8, 1])
        before = len(self.gh.artifacts[7])
        self.store().put(values)
        self.assertEqual(len(self.gh.artifacts[7]), before)
        values.append(history_entry(25))
        second = self.record(store, store.put(values))
        self.assertEqual(first["blocks"][:-1], second["blocks"][:-1])
        values[1]["timing"]["finished"] += 10
        values[1]["note"] += "Late diagnostic. " * 1000
        values[1]["report"]["classification"] = {"complexity": "high", "rationale": "Updated annotation"}
        third = self.record(store, store.put(values))
        self.assertNotEqual(second["blocks"][0], third["blocks"][0])
        self.assertEqual(second["blocks"][1:], third["blocks"][1:])
        self.assertEqual(self.store().get(digest(values), store.numbers[digest(values)]), values)
        bodies = [item["artifact"]["body"] for item in self.gh.artifacts[7].values()]
        self.assertEqual(sum("Evidence 1: " in body for body in bodies), 1)

    def test_checkpoint_uses_format_seven_and_cold_round_trip(self):
        """Block-only histories require format 7 even without extended state fields."""
        values = [history_entry(i) for i in range(48)]
        state = {"version": 2, "issue": 7, "stage": "stopped", "calls": 48, "qc_history": values}
        self.cp.save(state)
        payload = legacy_state(self.cp.audit["body"])
        self.assertEqual(payload["state"]["version"], 7)
        self.assertEqual(Checkpoints(self.gh, CONFIG, 7).load([]), state)
        ref = next(r for r in payload["report_data"] if r["path"] == ["qc_history"])
        self.assertEqual(len(ref["history"]["blocks"]), 6)
        self.assertEqual(ref["history"]["tail"], {"value": [], "references": []})

    def test_missing_or_changed_block_is_report_data_error(self):
        """No subset of a committed history is returned when a referenced block is unavailable."""
        state = {"version": 2, "issue": 7, "qc_history": [history_entry(i) for i in range(48)]}
        self.cp.save(state)
        record = legacy_state(self.cp.audit["body"])["report_data"][0]["history"]
        number = record["blocks"][0]["artifacts"][0]
        del self.gh.artifacts[7][number]
        with self.assertRaises(CheckpointError) as caught:
            Checkpoints(self.gh, CONFIG, 7).load([])
        self.assertEqual(caught.exception.code, "report_data")

    def test_block_layout_validated_before_child_fetch(self):
        """Malformed lengths and positions are refused before looking up any block."""
        store = self.store()
        values = list(range(17))
        record = self.record(store, store.put(values))
        broken = []
        for field, value in (("length", 16), ("length", True), ("size", 0), ("size", True)):
            broken.append({**deepcopy(record), field: value})
        for count in (0, True, 9):
            altered = deepcopy(record)
            altered["blocks"][0]["count"] = count
            broken.append(altered)
        broken += [{**record, "blocks": []}, {**record, "references": []}]
        for invalid in broken:
            with self.subTest(record=invalid), patch.object(store, "get", side_effect=AssertionError("child fetched")):
                with self.assertRaises(report_store.ReportDataError):
                    store.restore_blocks(invalid)

    def test_reordered_blocks_fail_digest_verification(self):
        """Valid individual blocks cannot be reordered into another task history."""
        values = list(range(24))
        store = self.store()
        record = self.record(store, store.put(values))
        record["blocks"].reverse()
        other = FakeGitHub(CONFIG)
        target = Store(other, 7, CONFIG["bot_login"])
        with patch.object(target, "encode", return_value=record):
            target.put(values)
        manifest = target.numbers[digest(values)][0]
        reader = Store(other, 7, CONFIG["bot_login"])
        original = reader.get

        def get(key, numbers=None):
            """Resolve block evidence from the source store but verify the new manifest normally."""
            return original(key, numbers) if key == digest(values) else store.get(key, numbers)

        with patch.object(reader, "get", side_effect=get), self.assertRaises(report_store.ReportDataError):
            reader.get(digest(values), [manifest])

    def test_lost_block_and_manifest_replies_recover_without_duplicates(self):
        """Repeat a failed save after losing either immutable write response."""
        for manifest in (False, True):
            with self.subTest(manifest=manifest):
                self.setUp()
                values = [history_entry(i) for i in range(48)]
                state = {"version": 2, "issue": 7, "calls": 48, "qc_history": values}
                original = self.gh.repo
                lost = []

                def write(path, method="GET", data=None):
                    """Lose one matching creation response after the fake has committed it."""
                    result = original(path, method, data)
                    body = (data or {}).get("body", "")
                    if method == "POST" and body.startswith(MARKER) and not lost:
                        record = json.loads(body.split(report_artifact.DATA, 1)[1].split("\n-->\n", 1)[0])["part"]
                        is_manifest = '\n  "blocks": [' in record
                        is_block = '\n  "value": [' in record
                        if is_manifest if manifest else is_block:
                            lost.append(result)
                            raise RuntimeError("lost reply")
                    return result

                with patch.object(self.gh, "repo", side_effect=write), self.assertRaises(RuntimeError):
                    if manifest:
                        self.cp.reports.put(values)
                    else:
                        self.cp.save(state)
                recovered = Checkpoints(self.gh, CONFIG, 7)
                recovered.save(state)
                self.assertEqual(Checkpoints(self.gh, CONFIG, 7).load([]), state)
                before = len(self.gh.artifacts[7])
                Checkpoints(self.gh, CONFIG, 7).load([])
                recovered.save(state)
                self.assertEqual(len(self.gh.artifacts[7]), before)
                bodies = [item["artifact"]["body"] for item in self.gh.artifacts[7].values()]
                self.assertEqual(len(bodies), len(set(bodies)))

    def test_partial_multipart_manifest_recovers_in_place(self):
        """A manifest can span artifacts without changing its block layout during replay."""
        values = list(range(2400))
        key = digest(values)
        original = self.gh.repo

        def write(path, method="GET", data=None):
            """Lose the reply to the second manifest part, after all blocks were written."""
            result = original(path, method, data)
            if method == "POST" and (data or {}).get("body", "").startswith(f"{MARKER}{key}:2:"):
                raise RuntimeError("lost manifest part")
            return result

        with fixed_parts(16000):
            with patch.object(self.gh, "repo", side_effect=write), self.assertRaises(RuntimeError):
                self.store().put(values)
        before = deepcopy(self.gh.artifacts[7])
        recovered = self.store()
        recovered.put(values)
        self.assertGreater(len(recovered.numbers[key]), 2)
        self.assertEqual(self.store().get(key, recovered.numbers[key]), values)
        for number, item in before.items():
            self.assertEqual(self.gh.artifacts[7][number], item)
        self.assertEqual(len(self.gh.artifacts[7]), 300 + len(recovered.numbers[key]))

    def test_legacy_whole_list_and_partial_write_keep_their_encoding(self):
        """Old complete or interrupted snapshots remain reusable without conflicting markers."""
        for partial in (False, True):
            with self.subTest(partial=partial):
                self.setUp()
                values = [{"note": str(i) + "x" * 2000} for i in range(24)]
                legacy = self.store()
                original = self.gh.repo
                writes = []

                def write(path, method="GET", data=None):
                    """Simulate a process stopping after the first old-format part."""
                    result = original(path, method, data)
                    if method == "POST":
                        writes.append(result)
                        if partial:
                            raise RuntimeError("lost legacy response")
                    return result

                with (
                    patch.object(report_store, "BLOCK_ENTRIES", 1000, create=True),
                    patch.object(report_store, "CHUNK", 16000),
                ):
                    if partial:
                        with patch.object(self.gh, "repo", side_effect=write), self.assertRaises(RuntimeError):
                            legacy.put(values, legacy=True)
                    else:
                        legacy.put(values, legacy=True)
                first = deepcopy(self.gh.artifacts[7])
                current = self.store()
                key = current.put(values)
                self.assertNotIn("blocks", self.record(current, key))
                self.assertEqual(current.get(key), values)
                for number, old in first.items():
                    self.assertEqual(self.gh.artifacts[7][number], old)
                if not partial:
                    self.assertEqual(self.gh.artifacts[7], first)

    def test_old_whole_history_checkpoint_is_still_readable(self):
        """The new reader keeps format-3 whole-root references compatible."""
        values = [history_entry(i) for i in range(48)]
        store = self.store()
        with patch.object(report_store, "BLOCK_ENTRIES", 1000):
            key = store.put(values)
        payload = {
            "repository": CONFIG["repository"],
            "state": {"version": 3, "issue": 7, "qc_history": None},
            "report_data": [
                {
                    "path": ["qc_history"],
                    "digest": key,
                    "artifacts": store.numbers[key],
                    "select": [],
                    "annotations": {},
                }
            ],
        }
        body = AUDIT_MARKER + STATE_PREFIX + json.dumps(payload) + "\n```\n"
        self.assertEqual(self.cp.decode({"body": body}), {"version": 2, "issue": 7, "qc_history": values})

    def test_long_history_traffic_improves_without_losing_entries(self):
        """Compare identical real saves, counting report-data artifacts and their complete bodies."""
        whole = measure(64, whole_snapshots=True, chunk_characters=16000, snapshot_manifests=True)
        blocked = measure(64, chunk_characters=16000, snapshot_manifests=True)
        self.assertLess(blocked["data_body_bytes"], whole["data_body_bytes"])
        self.assertLess(blocked["data_artifacts"], whole["data_artifacts"])
