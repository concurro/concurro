"""Plain-text publication surfaces must not inherit Markdown code-span exemptions."""

import unittest
from copy import deepcopy

from support import CONFIG, setUpModule as setUpModule, tearDownModule as tearDownModule

import pr_description
from publication import Publication
from test_pr_descriptions import COMMIT


class PlainTextReferences(unittest.TestCase):
    """Neutralize only model references, including those enclosed in backticks."""

    def test_s1_titles_neutralize_references_before_truncation(self):
        for raw in (
            "Fix #12 overflow",
            "Closes `#12` overflow",
            "Resolve owner/repo#3",
            "Fix GH-4 for @team",
            "Fix https://github.com/o/r/issues/12",
            "Closes `https://github.com/o/r/pull/4`",
        ):
            for plan in ({"headline": raw, "summary": "Fallback"}, {"summary": raw + "."}):
                with self.subTest(plan=plan):
                    publication = Publication(None, None, CONFIG, {"issue": 7, "plan": plan}, None)
                    title = publication.pr_title()
                    self.assertIn("\u200b", title)
                    self.assertEqual(title.replace("\u200b", ""), raw)
                    self.assertLessEqual(len(title), 72)
        state = {"issue": 7, "plan": {"summary": "Fix #12 " + "overflow " * 20}}
        title = Publication(None, None, CONFIG, state, None).pr_title()
        self.assertIn("#\u200b12", title)
        self.assertTrue(title.endswith("…"))
        self.assertLessEqual(len(title), 72)
        for raw in ("Fix C# and #[derive]", "Fix overflow in `foo#bar`", "Contact a@example.com"):
            state["plan"] = {"summary": raw}
            self.assertEqual(Publication(None, None, CONFIG, state, None).pr_title(), raw)

    def test_s2_initial_and_repair_commits_neutralize_inside_code(self):
        references = (
            "#12",
            "owner/repo#3",
            "GH-4",
            "@team",
            "https://github.com/o/r/issues/12",
            "https://github.com/o/r/pull/4",
        )
        for reference in references:
            raw = "Closes `" + reference + "`"
            self.assertEqual(pr_description.inert(raw), raw, "Markdown still preserves code")
            for repair in (False, True):
                for conventional in (False, True):
                    state = {"issue": 7, "plan": {"summary": raw}, **({"head": "a"} if repair else {})}
                    metadata = {**deepcopy(COMMIT), "subject": raw, "changes": [raw, "Fix C# and #[derive]"]}
                    message = pr_description.commit_message(
                        state, {**CONFIG, "conventional_commits": conventional}, metadata
                    )
                    with self.subTest(reference=reference, repair=repair, conventional=conventional):
                        self.assertNotIn(reference, message)
                        self.assertEqual(message.count("\u200b"), 2)
                        self.assertIn("- " + raw, message.replace("\u200b", ""))
                        self.assertIn("- Fix C# and #[derive]", message)
                        self.assertTrue(message.endswith("Refs #7"))
                        self.assertLessEqual(len(message.splitlines()[0]), 72)
