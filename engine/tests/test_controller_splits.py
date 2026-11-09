"""Event precedence and publication request boundaries."""

import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule

from controller import BUNDLE_REASON, Controller, validate_config
from pipeline_fixtures import ConsensusFixture
from policy import Blocked


class StepPrecedenceTests(ConsensusFixture, unittest.TestCase):
    """Completion observations win over commands, stale authority and legacy migration."""

    def setUp(self):
        super().setUp()
        self.reach("review")

    def external(self, pr, issue="closed", stage="blocked", foreign=False):
        """Run the public step with real PR evidence and observable save boundaries."""
        self.gh.pr.update(pr)
        self.gh.issue["state"] = issue
        c = self.loaded()
        c.state.update(stage=stage, reason=BUNDLE_REASON, retry_stage="gates", config_digest="stale")
        if foreign:
            c.state["controller"] = "another-controller"
        with (
            patch.object(c, "load"),
            patch.object(c, "save") as save,
            patch.object(c, "commands", side_effect=AssertionError("commands ran before external event")),
            patch.object(self.gh, "repo", wraps=self.gh.repo) as repo,
        ):
            result = c.step(7)
        return c, result, save.call_args_list, [call.args[0] for call in repo.call_args_list]

    def test_merge_wins_over_pr_close_issue_close_block_and_stale_authority(self):
        c, result, saves, reads = self.external({"merged": True, "state": "closed", "merge_commit_sha": "f" * 40})
        self.assertTrue(result)
        self.assertEqual(c.state["stage"], "observed_merged")
        self.assertEqual(c.state["completion"]["observed_stage"], "blocked")
        self.assertEqual([call.kwargs for call in saves], [{"outboxes": False}])
        self.assertEqual(reads, ["issues/7", "pulls/9"])

    def test_pr_close_wins_over_issue_close_and_legacy_block(self):
        c, result, saves, reads = self.external({"merged": False, "state": "closed"})
        self.assertFalse(result)
        self.assertEqual(c.state["stage"], "closed")
        self.assertEqual(c.state["reason"], "PR closed without merging.")
        self.assertEqual([call.kwargs for call in saves], [{"outboxes": False}])
        self.assertEqual(reads, ["issues/7", "pulls/9"])

    def test_already_closed_pr_does_not_save_or_stop_on_closed_issue(self):
        c, result, saves, _ = self.external({"merged": False, "state": "closed"}, stage="closed")
        self.assertFalse(result)
        self.assertEqual(c.state["stage"], "closed")
        self.assertEqual(saves, [])

    def test_issue_close_stops_open_pr_before_commands(self):
        c, result, saves, _ = self.external({"merged": False, "state": "open"})
        self.assertTrue(result)
        self.assertEqual(c.state["stage"], "stopped")
        self.assertEqual(c.state["reason"], "Issue closed without a confirmed exact-head merge.")
        self.assertEqual([call.kwargs for call in saves], [{"outboxes": False}])

    def test_foreign_owner_wins_before_any_pr_observation(self):
        c, result, saves, reads = self.external({"merged": True, "state": "closed"}, foreign=True)
        self.assertFalse(result)
        self.assertIsNone(c.state)
        self.assertTrue(c.foreign)
        self.assertEqual(saves, [])
        self.assertEqual(reads, ["issues/7"])

    def test_wrong_merged_head_still_blocks_when_issue_closed(self):
        self.gh.pr["head"]["sha"] = "e" * 40
        with self.assertRaisesRegex(Blocked, "merged PR head differs from the exact published controller head"):
            self.external({"merged": True, "state": "closed", "merge_commit_sha": "f" * 40})

    def test_legacy_observer_migration_precedes_commands(self):
        c = self.loaded()
        c.state.update(stage="blocked", reason=BUNDLE_REASON, retry_stage="gates")
        seen = []
        with (
            patch.object(c, "load"),
            patch.object(c, "save"),
            patch.object(c, "commands", side_effect=lambda _: seen.append(deepcopy(c.state)) or True),
        ):
            self.assertTrue(c.step(7))
        self.assertEqual(seen[0]["stage"], "gates")
        self.assertNotIn("reason", seen[0])
        self.assertNotIn("retry_stage", seen[0])

    def test_authority_pr_check_does_not_add_a_merged_rejection(self):
        c = self.loaded()
        original = self.gh.repo
        reads = 0

        def repo(path, method="GET", data=None):
            nonlocal reads
            result = original(path, method, data)
            if path == "pulls/9":
                reads += 1
                result["merged"] = reads > 1
            return result

        with patch.object(self.gh, "repo", side_effect=repo), patch.object(c, "review", return_value=False) as review:
            self.assertFalse(c.step(7))
        self.assertEqual(reads, 2)
        review.assert_called_once_with()


class PublicationSequenceTests(ConsensusFixture, unittest.TestCase):
    """Freeze publication requests, payloads and save boundaries through the first review."""

    def trace(self, crash=None, blobs=False):
        """Exercise real checkpoint replay; only non-publication storage traffic is omitted."""
        self.reach("implement")
        events = []
        original_repo, original_pages, original_save = self.gh.repo, self.gh.pages, Controller.save
        crashed = False

        def repo(path, method="GET", data=None):
            nonlocal crashed
            if path.startswith(("git/", "pulls")):
                events.append(["repo", path, method, deepcopy(data)])
            result = original_repo(path, method, data)
            if path == crash and method == "POST" and not crashed:
                crashed = True
                events.append(["lost-response", path])
                raise OSError("lost publication response")
            return result

        def pages(path, key=None):
            if path.startswith("pulls"):
                events.append(["pages", path, key])
            return original_pages(path, key)

        def save(c, *args, **kwargs):
            events.append(["save", c.state["stage"], deepcopy(c.state.get("pending")), kwargs])
            result = original_save(c, *args, **kwargs)
            events.append(["saved", c.state["stage"]])
            return result

        with (
            patch.object(self.gh, "repo", side_effect=repo),
            patch.object(self.gh, "pages", side_effect=pages),
            patch.object(Controller, "save", save),
            patch("controller.INLINE_BLOB_LIMIT", 8 if blobs else 64000),
        ):
            for _ in range(6):
                c = self.loaded()
                try:
                    if c.state["stage"] == "implement":
                        c.implement()
                    elif c.state["stage"] == "publish":
                        c.publish()
                    else:
                        c.review()
                        break
                except OSError as error:
                    self.assertEqual(str(error), "lost publication response")
            else:
                self.fail("publication did not reach review")
        self.assertEqual(c.state["stage"], "review")
        self.assertEqual(len(c.state["reviews"]), 1)
        self.assertEqual(sum(stage == "develop" for _, stage, _ in self.agents.calls), 1)
        self.assertEqual(crashed, crash is not None)
        return events

    def check_trace(self, key, **kwargs):
        actual = self.trace(**kwargs)
        expected = json.loads(Path(__file__).with_name("goldens").joinpath("publication.json").read_text())[key]
        self.assertEqual(actual, expected)
        posts = [e[1] for e in actual if e[0] == "repo" and e[2] == "POST"]
        self.assertEqual(posts[0], "git/refs")
        self.assertLess(posts.index("git/trees"), posts.index("git/commits"))
        self.assertLess(posts.index("git/commits"), posts.index("pulls"))
        self.assertEqual(posts.count("pulls"), 1)
        self.assertEqual(posts[-1], "pulls/9/reviews")

    def test_full_publication_sequence(self):
        self.check_trace("normal")

    def test_lost_tree_response_replays_without_repeating_implementation(self):
        self.check_trace("tree-crash", crash="git/trees")

    def test_lost_pr_response_finds_existing_pr(self):
        self.check_trace("pr-crash", crash="pulls")

    def test_uploaded_blob_sequence(self):
        self.check_trace("blob", blobs=True)


class ConfigBoundaryTests(unittest.TestCase):
    """Keep exact integer policy, including legacy fields that still accept bools."""

    def test_integer_boundaries(self):
        from support import CONFIG

        cases = (
            ("max_plan_revisions", 0, 3, "invalid plan revision budget", False),
            ("max_review_repairs", 0, 8, "invalid review repair budget", False),
            ("max_design_reopens", 0, 3, "invalid design reopen budget", False),
            ("max_fix_rounds", 0, 3, "invalid budget", True),
            ("max_model_calls", 1, 64, "invalid budget", True),
        )
        for key, low, high, message, accepts_bool in cases:
            for value in (low, high, low - 1, high + 1, True, False):
                with self.subTest(key=key, value=value):
                    config = {**deepcopy(CONFIG), key: value}
                    valid = low <= value <= high and (type(value) is int or accepts_bool)
                    if valid:
                        validate_config(config)
                    else:
                        with self.assertRaisesRegex(Blocked, "^" + message + "$"):
                            validate_config(config)
