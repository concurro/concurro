"""Budget-driven artifact packing preserves complete evidence and frozen recovery layouts."""

import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from markdown_it import MarkdownIt

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

import artifact_capacity
import protocol
import report_artifact
from fakes import CONFIG, FakeGitHub
from report_store import ReportDataError, Store


class PackingTests(unittest.TestCase):
    """Exercise actual immutable publication rather than only the sizing formula."""

    def setUp(self):
        """Start independent fake storage for each case."""
        self.gh = FakeGitHub(CONFIG)

    def store(self):
        """Create a cold writer without shared in-memory receipts."""
        return Store(self.gh, 7, CONFIG["bot_login"], CONFIG["repository"])

    def test_multimegabyte_record_fits_one_artifact(self):
        """A large hidden record fits the transport without an independent part-size cap."""
        value = {"evidence": "x" * 3_000_000}
        store = self.store()
        key = store.put(value)
        self.assertEqual(len(store.numbers[key]), 1)
        self.assertEqual(self.store().get(key, store.numbers[key]), value)

    def test_one_budget_changes_new_layouts(self):
        """Changing the shared response allowance changes packing with no second setting."""
        counts = []
        for budget in (40_000, 160_000):
            self.setUp()
            with patch.object(protocol, "ARTIFACT_RESPONSE_BYTES", budget):
                store = self.store()
                key = store.put({"evidence": "x" * 90_000})
                counts.append(len(store.numbers[key]))
        self.assertGreater(counts[0], counts[1])

    def test_short_view_before_splitting_canonical_data(self):
        """A compact frozen presentation avoids a split caused only by a duplicate view."""
        with (
            patch.object(protocol, "ARTIFACT_RESPONSE_BYTES", 40_000),
            patch.object(report_artifact, "submitted", return_value="view " * 5000),
        ):
            store = self.store()
            key = store.put({"evidence": "x" * 1000})
        self.assertEqual(len(store.numbers[key]), 1)
        body = self.gh.artifacts[7][store.numbers[key][0]]["artifact"]["body"]
        self.assertLess(len(body.encode()), 5000)
        self.assertEqual(self.store().get(key, store.numbers[key]), {"evidence": "x" * 1000})

    def test_lost_continuation_keeps_layout_after_budget_increase(self):
        """Any surviving continuation pins its encoding, view and boundaries across a restart."""
        value = {"evidence": "x" * 90_000}
        original = self.gh.repo
        lost = []

        def write(path, method="GET", data=None):
            """Lose the first successful artifact write reply."""
            response = original(path, method, data)
            if method == "POST" and not lost:
                lost.append(response["number"])
                raise RuntimeError("lost reply")
            return response

        with patch.object(protocol, "ARTIFACT_RESPONSE_BYTES", 40_000):
            with patch.object(self.gh, "repo", side_effect=write), self.assertRaises(RuntimeError):
                self.store().put(value)
        before = deepcopy(self.gh.artifacts[7])
        # The entry is written last; a lost first reply must leave a continuation.
        text = before[lost[0]]["artifact"]["body"]
        self.assertIn(":2:", text.splitlines()[0])
        store = self.store()
        store.put(value)
        for number, item in before.items():
            self.assertEqual(self.gh.artifacts[7][number], item)

    def test_lower_budget_refuses_frozen_write_without_repacking_or_uploads(self):
        """An interrupted layout cannot be silently replaced when the response allowance shrinks."""
        value = {"evidence": "x" * 90_000}
        original = self.gh.repo
        lost = []

        def write(path, method="GET", data=None):
            """Keep a continuation while losing its write receipt."""
            result = original(path, method, data)
            if method == "POST" and not lost:
                lost.append(result["number"])
                raise RuntimeError("lost reply")
            return result

        with patch.object(protocol, "ARTIFACT_RESPONSE_BYTES", 80_000):
            with patch.object(self.gh, "repo", side_effect=write), self.assertRaises(RuntimeError):
                self.store().put(value)
        before = deepcopy(self.gh.artifacts)
        with patch.object(protocol, "ARTIFACT_RESPONSE_BYTES", 40_000):
            with self.assertRaisesRegex(ReportDataError, "Frozen artifact layout.*response budget"):
                self.store().put(value)
        self.assertEqual(self.gh.artifacts, before)
        store = self.store()
        key = store.put(value)
        self.assertEqual(self.store().get(key, store.numbers[key]), value)

    def test_each_lost_reply_keeps_layout_and_compact_renderer(self):
        """Entry and continuation losses retain exactly one artifact per part after a budget change."""
        value = {"evidence": '<--!>雪\\u003e "' * 800}
        with patch.object(protocol, "ARTIFACT_RESPONSE_BYTES", 40_000):
            store = self.store()
            key = store.put(value)
        expected = deepcopy(self.gh.artifacts[7])
        self.assertGreater(len(expected), 1)
        for fail_at in range(1, len(expected) + 1):
            with self.subTest(write=fail_at):
                self.setUp()
                original = self.gh.repo
                writes = []

                def write(path, method="GET", data=None):
                    """Lose one successful POST reply, including the final entry part."""
                    result = original(path, method, data)
                    if method == "POST":
                        writes.append(result["number"])
                        if len(writes) == fail_at:
                            raise RuntimeError("lost reply")
                    return result

                with patch.object(protocol, "ARTIFACT_RESPONSE_BYTES", 40_000):
                    with patch.object(self.gh, "repo", side_effect=write), self.assertRaises(RuntimeError):
                        self.store().put(value)
                with patch.object(report_artifact, "submitted", side_effect=AssertionError("renderer changed")):
                    cold = self.store()
                    self.assertEqual(cold.put(value), key)
                    self.assertEqual(self.store().get(key, cold.numbers[key]), value)
                self.assertEqual(self.gh.artifacts[7], expected)

    def test_complete_bodies_include_descriptor_links_and_utf8(self):
        """The real serialized parts fit after Unicode, quoting and every continuation link is added."""
        value = {"evidence": 'é雪😀 --> \\u003e "\n' * 1000}
        with patch.object(protocol, "ARTIFACT_RESPONSE_BYTES", 60_000):
            store = self.store()
            key = store.put(value)
            for number in store.numbers[key]:
                text = self.gh.artifacts[7][number]["artifact"]["body"]
                visible = text.split("\n-->\n", 1)[1]
                self.assertTrue(artifact_capacity.fits(text, visible))
                parsed = report_artifact.parse(text, key)
                self.assertEqual(parsed["layout"]["renderer"], 2)
            self.assertEqual(self.store().get(key, store.numbers[key]), value)

    def test_separate_hard_ceiling_even_when_transport_budget_is_larger(self):
        """A generous transport setting cannot admit bodies above GitHub's independent ceiling."""
        self.assertEqual(protocol.GITHUB_ARTIFACT_BODY_BYTES, 16_777_215)
        with (
            patch.object(protocol, "ARTIFACT_RESPONSE_BYTES", 16_000_000),
            patch.object(protocol, "GITHUB_ARTIFACT_BODY_BYTES", 20_000),
        ):
            store = self.store()
            key = store.put({"evidence": "x" * 90_000})
            self.assertGreater(len(store.numbers[key]), 1)
            for number in store.numbers[key]:
                self.assertLessEqual(len(self.gh.artifacts[7][number]["artifact"]["body"].encode()), 20_000)

    def test_too_small_for_descriptor_leaves_storage_untouched(self):
        """A response allowance below the metadata envelope cannot cause partial writes."""
        with patch.object(protocol, "ARTIFACT_RESPONSE_BYTES", 1):
            with self.assertRaises(ReportDataError):
                self.store().put({"evidence": "data"})
        self.assertFalse(self.gh.artifacts.get(7))

    def test_compact_renderer_is_frozen(self):
        """The fallback is reproducible from a continuation descriptor without a live renderer."""
        self.assertEqual(
            report_artifact.view_for({}, {"renderer": 2}),
            "### Submitted model output\n\nCanonical data is available in the raw API body and any linked parts.",
        )


class ResponseAccountingTests(unittest.TestCase):
    """Pin wire escaping, response shapes and measured HTML headroom independently of packing."""

    def test_json_escaped_bytes_are_not_a_fixed_ratio(self):
        """Quotes, slashes, controls, literal escapes and Unicode use literal JSON expectations."""
        cases = [
            ("plain", b'"plain"'),
            ('"\\\n\t\x00', b'"\\"\\\\\\n\\t\\u0000"'),
            ("</x> & /", b'"</x> & /"'),
            ("\\u003e", b'"\\\\u003e"'),
            ("é雪😀", '"é雪😀"'.encode()),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(json.dumps(value, ensure_ascii=False).encode(), expected)
                self.assertEqual(artifact_capacity.json_bytes(value), len(expected))

    def test_shapes_and_historical_body_are_counted_separately(self):
        """Latest detail has two body/views; old history can have a larger rendered fenced body."""
        with (
            patch.object(artifact_capacity, "METADATA_BYTES", 7),
            patch.object(artifact_capacity, "HTML_MULTIPLIER", 2),
            patch.object(artifact_capacity, "HTML_OVERHEAD", 3),
        ):
            # JSON string lengths: body=6, visible=3, old body=5, old visible=6.
            self.assertEqual(artifact_capacity.response_sizes("body", "v"), {"post": 22, "listing": 22, "detail": 37})
            self.assertEqual(artifact_capacity.response_sizes("body", "v", historical=("old", "view"))["detail"], 42)

    def test_exact_budget_and_body_boundaries(self):
        """Equality fits; one byte over either independent constraint does not."""
        text, view = "é" * 100, "# view"
        required = max(artifact_capacity.response_sizes(text, view).values())
        with patch.object(protocol, "ARTIFACT_RESPONSE_BYTES", required):
            with patch.object(protocol, "GITHUB_ARTIFACT_BODY_BYTES", 200):
                self.assertTrue(artifact_capacity.fits(text, view))
            with patch.object(protocol, "GITHUB_ARTIFACT_BODY_BYTES", 199):
                self.assertFalse(artifact_capacity.fits(text, view))
        with patch.object(protocol, "ARTIFACT_RESPONSE_BYTES", required - 1):
            self.assertFalse(artifact_capacity.fits(text, view))

    def test_rendered_fixture_shapes_fit_allowance(self):
        """Controlled Markdown and HTML escaping fit the estimator; this is not a universal HTML proof."""
        renderer = MarkdownIt("commonmark", {"html": True})
        for visible in (
            "# Submitted output\n\n" + '[a](https://example.com/"<&>) `x` & < >\n\n' * 100,
            "```json\n" + '{"quote": "<&é雪😀>"}\n' * 100 + "```\n",
            "\n".join(f"- [Data part {i}](https://github.com/owner/repo/issues/7/artifacts/{i})" for i in range(100)),
        ):
            with self.subTest(prefix=visible[:20]):
                body = "<!-- hidden -->\n" + visible
                html = renderer.render(visible)
                artifact = {"number": 1, "name": "Concurro data", "body": body, "body_html": html}
                wire = {
                    "post": artifact,
                    "listing": [artifact],
                    "detail": {"artifact": artifact, "versions": [artifact | {"version": 1}]},
                }
                for shape, payload in wire.items():
                    self.assertLessEqual(
                        artifact_capacity.json_bytes(payload), artifact_capacity.response_sizes(body, visible)[shape]
                    )
