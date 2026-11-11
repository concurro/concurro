"""Cold ordinary artifact operations use durable references; ambiguous writes still discover."""

import unittest
from copy import deepcopy
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import conversation
from checkpoints import MARKER, Checkpoints, legacy_state
from discovery_traffic import prepared, queue
from fakes import CONFIG, FakeGitHub
from policy import digest
from report_store import Cache, ReportDataError, Store


class DiscoveryTests(unittest.TestCase):
    """Keep read and publication behavior independent of a warm process cache."""

    def setUp(self):
        """Use an already published issue, with no cached Checkpoints or Store."""
        self.gh, self.state = prepared()

    def cold(self):
        """Load using the persisted dashboard, plan and data identities."""
        cp = Checkpoints(self.gh, CONFIG, 7)
        state = cp.load(self.gh.comments)
        self.assertEqual(state, self.state)
        return cp, state

    def no_scan(self, path):
        """Allow comment discovery but reject an artifact collection request."""
        if path.endswith("/artifacts"):
            raise AssertionError("ordinary artifact collection scan")
        return FakeGitHub.pages(self.gh, path)

    def test_cold_load_and_unchanged_save_do_not_scan(self):
        """Verified block directories need no search for hypothetical whole-list artifacts."""
        with patch.object(self.gh, "pages", side_effect=self.no_scan):
            cp, state = self.cold()
            cp.save(state)

    def test_history_tail_append_does_not_scan_for_a_whole_list(self):
        """Appending inline metadata preserves the known block-directory representation."""
        with patch.object(self.gh, "pages", side_effect=self.no_scan):
            cp, state = self.cold()
            state["review_history"].append({"note": "new tail entry"})
            cp.save(state)

    def test_cold_republication_uses_small_report_receipt(self):
        """A report below the detachment threshold still has a durable publication reference."""
        with patch.object(self.gh, "pages", side_effect=self.no_scan):
            cp, state = self.cold()
            queue(state)
            before = len(self.gh.artifacts[7])
            conversation.publish(state, self.gh, CONFIG, cp.reports)
            self.assertEqual(len(self.gh.artifacts[7]), before)
            self.assertEqual(len(state["reports"]), 2)

    def test_receipt_reference_is_verified_not_trusted_as_content(self):
        """Missing, edited, foreign and mismatched data never fall back to a collection scan."""
        for fault in ("missing", "edited", "foreign", "digest", "empty", "none"):
            with self.subTest(fault=fault):
                self.setUp()
                cp, state = self.cold()
                ref = state["reports"][0]["data"]
                number = ref["artifacts"][0]
                item = self.gh.artifacts[7][number]
                if fault == "missing":
                    del self.gh.artifacts[7][number]
                elif fault == "edited":
                    item["versions"][0]["version"] = 2
                elif fault == "foreign":
                    item["artifact"]["creator"]["login"] = "someone-else"
                elif fault == "digest":
                    item["artifact"]["body"] = item["versions"][0]["body"] = item["artifact"]["body"].replace(
                        "Verified", "Modified"
                    )
                else:
                    ref["artifacts"] = [] if fault == "empty" else None
                store = Store(self.gh, 7, CONFIG["bot_login"], CONFIG["repository"])
                store.bind_receipts(state)
                before = deepcopy(self.gh.artifacts)
                with patch.object(self.gh, "pages", side_effect=self.no_scan):
                    with self.assertRaises(ReportDataError):
                        store.put(state["qc"]["plan"])
                self.assertEqual(self.gh.artifacts, before)

    def test_receipt_only_journal_and_cold_cache_use_direct_reads(self):
        """The report index is not required when the publication receipt still carries the identity."""
        cp, state = self.cold()
        state.pop("reports")
        store = Store(self.gh, 7, CONFIG["bot_login"], CONFIG["repository"], cache=Cache())
        store.bind_receipts(state)
        with (
            patch.object(self.gh, "pages", side_effect=self.no_scan),
            patch.object(self.gh, "repo", wraps=self.gh.repo) as request,
        ):
            key = store.put(state["qc"]["plan"])
        self.assertEqual(key, digest(state["qc"]["plan"]))
        self.assertTrue(request.called)
        self.assertTrue(all("/artifacts/" in c.args[0] for c in request.call_args_list))

    def test_history_block_completion_needs_no_discovery(self):
        """A newly closed block gets a fresh nonce without a recovery scan."""
        cp, state = self.cold()
        state["review_history"].extend({"note": "tail " + str(i)} for i in range(5))
        with patch.object(self.gh, "pages", wraps=self.gh.pages) as pages:
            cp.save(state)
        self.assertEqual(pages.call_args_list, [])
        # Once the directory is saved, its next cold save needs no scan.
        cp.publish_dashboard(MARKER + "\nFixture\n" + cp.links()[1] + "\n")
        with patch.object(self.gh, "pages", side_effect=self.no_scan):
            cold = Checkpoints(self.gh, CONFIG, 7)
            loaded = cold.load(self.gh.comments)
            self.assertEqual(loaded, state)
            cold.save(loaded)

    def test_legacy_whole_history_remains_whole_without_discovery(self):
        """Already-bound old whole-list artifacts retain their identities and bytes on cold saves."""
        cp, state = self.cold()
        cp.reports.put(state["review_history"], legacy=True)
        cp.save(state)
        cp.publish_dashboard(MARKER + "\nFixture\n" + cp.links()[1] + "\n")
        payload = legacy_state(cp.audit["body"])
        ref = next(r for r in payload["report_data"] if r["path"] == ["review_history"])
        self.assertIn("artifacts", ref)
        self.assertNotIn("history", ref)
        before = deepcopy(self.gh.artifacts[7])
        with patch.object(self.gh, "pages", side_effect=self.no_scan):
            cold = Checkpoints(self.gh, CONFIG, 7)
            restored = cold.load(self.gh.comments)
            cold.save(restored)
        for number in ref["artifacts"]:
            self.assertEqual(self.gh.artifacts[7][number], before[number])

    def test_missing_receipt_recovers_lost_creation_reply(self):
        """An unreferenced legacy artifact is discovered before re-creating it, even after a restart."""
        value = {"summary": "Unacknowledged " * 1000}
        original = self.gh.repo
        created = []

        def write(path, method="GET", data=None):
            """Apply creation but lose the first reply before a reference can be saved."""
            item = original(path, method, data)
            if method == "POST" and not created:
                created.append(item["number"])
                raise RuntimeError("lost reply")
            return item

        with patch.object(self.gh, "repo", side_effect=write), self.assertRaises(RuntimeError):
            Store(self.gh, 7, CONFIG["bot_login"], write_version=2).put(value)
        before = deepcopy(self.gh.artifacts[7])
        store = Store(self.gh, 7, CONFIG["bot_login"], write_version=2)
        with patch.object(self.gh, "pages", wraps=self.gh.pages) as pages:
            key = store.put(value)
        self.assertEqual([c.args[0] for c in pages.call_args_list], ["issues/7/artifacts"])
        self.assertEqual(store.numbers[key], created)
        self.assertEqual(self.gh.artifacts[7], before)

    def test_list_receipt_precedes_legacy_whole_list_discovery(self):
        """A known immutable list reference is verified before considering directory conversion."""
        values = [{"note": str(i) + "x" * 1000} for i in range(17)]
        writer = Store(self.gh, 7, CONFIG["bot_login"])
        key = writer.put(values)
        known = {"digest": key, "artifacts": writer.numbers[key]}
        cold = Store(self.gh, 7, CONFIG["bot_login"])
        cold.bind_receipts({"reports": [{"data": known}]})
        item = {"path": ["review_history"], "digest": key, "value": values, "select": [], "annotations": {}}
        with patch.object(self.gh, "pages", side_effect=self.no_scan):
            result = cold.checkpoint_reference(item, prepare=True)
        self.assertEqual(result["artifacts"], known["artifacts"])
        self.assertNotIn("history", result)

    def test_pinned_identical_duplicate_is_not_retargeted(self):
        """Known receipt identities win over a lower-numbered identical artifact."""
        cp, state = self.cold()
        known = deepcopy(state["reports"][0]["data"])
        original = self.gh.artifacts[7][known["artifacts"][0]]["artifact"]
        duplicate = self.gh.repo("issues/7/artifacts", "POST", {k: original[k] for k in ("type", "name", "body")})
        known["artifacts"] = [duplicate["number"]]
        store = Store(self.gh, 7, CONFIG["bot_login"])
        store.bind_receipts({"reports": [{"data": known}]})
        with patch.object(self.gh, "pages", side_effect=self.no_scan):
            key = store.put(state["qc"]["plan"])
        self.assertEqual(store.numbers[key], known["artifacts"])
