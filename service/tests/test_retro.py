"""Retrospective collection reads once, replays offline and keeps private content out of its snapshot."""

import base64
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import fixtures  # noqa: F401 -- initializes the service and engine test paths

from support import CONFIG, setUpModule as setUpModule, tearDownModule as tearDownModule

import test_bundle_lifecycle as lifecycle
from github import NotFound
from pipeline_fixtures import ConsensusFixture
from service import diagnostics, retro

REPOSITORY = CONFIG["repository"]
FORK = "owner/fork"
CONFIG_SHA = "c" * 40
HEAD = "0" * 39 + "f"
SECRET = "PRIVATE-PROSE-MARKER"
TURN_ID = "12345678-1234-1234-1234-123456789abc"
NAMED_TURN = "22345678-1234-1234-1234-123456789abc"
WINDOW = ("2020-01-01", "2100-01-01")
USER = {"login": "maintainer", "id": 50, "type": "User"}
BOT = {"login": "concurro[bot]", "id": 51, "type": "Bot"}


def page(path):
    return path + ("&" if "?" in path else "?") + "per_page=100&page=1"


class Reader:
    """GitHub GETs answered from fixed responses; any other read fails the test."""

    def __init__(self, responses, missing=()):
        self.responses, self.missing = responses, set(missing)

    def request(self, method, path, data=None):
        assert method == "GET" and data is None
        if path in self.missing:
            raise NotFound("GitHub request failed: GET " + path)
        if path not in self.responses:
            raise AssertionError("unexpected read: " + path)
        return deepcopy(self.responses[path])


class Operator:
    """Exchange observations: one request seen by two sources, and private text in every free field."""

    def __init__(self, pending=False, native_status="available"):
        self.pending, self.native_status = pending, native_status

    def observations(self, repository, issue):
        usage = {"input_tokens": 100, "output_tokens": 20}
        return {
            "events": [
                {
                    "sequence": 1,
                    "request": "r1",
                    "turn": TURN_ID,
                    "source": "provider",
                    "usage": usage,
                    "model": SECRET,
                },
                {"sequence": 2, "request": "r1", "turn": TURN_ID, "source": "native", "usage": usage},
                {"sequence": 3, "request": "r2", "turn": TURN_ID, "source": "provider", "category": SECRET},
            ],
            "pending": self.pending,
            "native_status": self.native_status,
        }


def configuration_responses(configs):
    """The fork's configuration file at each commit: JSON present, YAML absent."""
    responses, missing = {}, set()
    for sha, config in configs.items():
        data = json.dumps(config).encode()
        responses[f"repos/{FORK}/contents/.concurro/config.json?ref={sha}"] = {
            "type": "file",
            "encoding": "base64",
            "path": ".concurro/config.json",
            "size": len(data),
            "content": base64.b64encode(data).decode(),
        }
        missing.add(f"repos/{FORK}/contents/.concurro/config.yaml?ref={sha}")
    return responses, missing


def task_responses(gh, configs, merged_at=None, commits=(), inline=(), reviews=(), comments=None):
    """Public reads for issue 7 and PR 9, plus the fork's configuration at each task commit."""
    root = f"repos/{REPOSITORY}"
    issue = {**gh.issue, "html_url": f"https://github.com/{REPOSITORY}/issues/7", "closed_at": None}
    pull = {"html_url": "pr", "state": "open", "merged": False, "merged_at": None, "closed_at": None, "head": {}}
    if merged_at:
        pull.update(state="closed", merged=True, merged_at=merged_at, closed_at=merged_at)
    responses = {
        page(f"{root}/issues?state=all&labels=concurro&sort=updated"): [issue],
        page(f"{root}/issues/7/comments"): comments if comments is not None else [],
        f"{root}/pulls/9": pull,
        page(f"{root}/pulls/9/commits"): list(commits),
        page(f"{root}/pulls/9/comments"): list(inline),
        page(f"{root}/pulls/9/reviews"): list(reviews),
        page(f"{root}/issues/9/comments"): [],
    }
    for number, value in gh.artifacts[7].items():
        responses[f"{root}/issues/7/artifacts/{number}"] = value
    for number in range(1, 100):
        versions = gh.repo(f"issues/7/artifacts/1?per_page=30&page={number}")
        responses[f"{root}/issues/7/artifacts/1?per_page=30&page={number}"] = versions
        if not versions["has_next_page"]:
            break
    found, missing = configuration_responses(configs)
    return {**responses, **found}, missing


def capture(root, responses, missing, operator, window=WINDOW):
    """Capture a snapshot and build the run, as `retro-collect` does with live sources."""
    reader = Reader(responses, missing)
    target = SimpleNamespace(
        upstream=REPOSITORY, fork=FORK, issue_repositories=(REPOSITORY,), repositories=(REPOSITORY, FORK)
    )
    settings = SimpleNamespace(targets=[target], reviewer=None, engine="e" * 40)
    store = root / "diagnostics"
    live = retro.Live({REPOSITORY: reader, FORK: reader}, store if store.exists() else None, operator)
    return retro.capture(settings, FORK, *window, root / "snapshot", root / "output", lambda _: (live, "concurro[bot]"))


def dossier(root):
    return json.loads(next((root / "output/dossiers").iterdir()).read_text())


class RetroTests(ConsensusFixture, unittest.TestCase):
    """A task with findings, human follow-up and recorded turns."""

    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()
        super().tearDown()

    def task(self, stage="merged", findings=True):
        """Reach review gates, then record findings, a stage and the configuration commit."""
        state = self.reach("gates")
        state["config_sha"] = CONFIG_SHA
        if findings:
            base = {"detail": "Detail.", "head": HEAD, "review": 1, "status": "fixed", "format": 1}
            state["findings"] = [
                {**base, "id": "F1", "provider": "claude", "severity": "high", "title": "Lock order", "path": "a.rs"}
                | {"start_line": 3, "line": 9},
                {**base, "id": "F2", "provider": "codex", "severity": "medium", "title": "Locks", "path": "a.rs"}
                | {"start_line": 8, "line": 12},
                {**base, "id": "F3", "provider": "codex", "severity": "low", "title": "Name", "path": "b.rs"}
                | {"start_line": 1, "line": 1},
            ]
        state["stage"] = stage
        self.persist(state)
        return state

    def record_turns(self, state):
        """A legacy turn as production records it (no agent), a named one, and an evaluation turn."""
        store = diagnostics.Store(self.root / "diagnostics")
        task = {"repository": REPOSITORY, "issue": 7}
        request = {
            "model": "claude-opus-5-5",
            "provider": "claude",
            "effort": "medium",
            "stage": "plan",
            "bundle_id": state["bundle_id"],
            "context": SECRET,
        }
        store.submitted(TURN_ID, {"request": request, "feedback": {"text": SECRET}, "spec": {"x": SECRET}}, task)
        store.observed(TURN_ID, "done", {"created": 1.0, "started": 2.0, "finished": 9.0}, ["s1"])
        store.finished(TURN_ID, {"outcome": "ok", "response": {"text": SECRET}, "elapsed_seconds": 7.0})
        named = {**request, "model": SECRET, "agent": {"profile": SECRET, "model": SECRET}}
        store.submitted(NAMED_TURN, {"request": named}, task)
        store.submitted("evaluation-turn", {"request": {**request, "evaluation": "e1"}}, task)
        store.close()

    def capture(self, state, window=WINDOW, merged_at="2099-01-02T00:00:00Z", operator=None):
        self.record_turns(state)
        human_commit = {
            "sha": "d" * 40,
            "author": USER,
            "committer": USER,
            "commit": {"message": "Fix lock order\n\nBody", "committer": {"date": "2099-01-01T00:00:00Z"}},
            "html_url": "https://github.com/c/1",
        }
        bot_commit = {
            "sha": HEAD,
            "author": BOT,
            "committer": BOT,
            "commit": {"message": "Bot", "committer": {"date": "2020-06-01T00:00:00Z"}},
        }
        responses, missing = task_responses(
            self.gh,
            {CONFIG_SHA: CONFIG},
            merged_at=merged_at,
            commits=[bot_commit, human_commit],
            inline=[
                {"id": 70, "user": USER, "path": "a.rs", "line": 4}
                | {"created_at": "2099-01-01T00:00:00Z", "html_url": "c70", "body": "Order?"}
            ],
            reviews=[{"id": 80, "user": USER, "state": "COMMENTED", "body": "", "submitted_at": "2099-01-01"}],
            comments=[
                {"id": 1, "user": USER, "body": "/concurro start", "created_at": "2020-06-01T00:00:00Z"},
                {"id": 2, "user": BOT, "body": "Dashboard", "created_at": "2020-06-01T00:00:00Z"},
            ],
        )
        responses[f"repos/{REPOSITORY}/commits/{'d' * 40}"] = {"files": [{"filename": "a.rs"}]}
        return capture(self.root, responses, missing, operator or Operator(), window)

    def test_offline_rebuild_matches_capture_without_live_reads(self):
        """The snapshot alone reproduces the run and every dossier."""
        run = self.capture(self.task())
        offline, dossiers = retro.collect(retro.Snapshot(self.root / "snapshot"))
        output = json.loads((self.root / "output/run.json").read_text())
        self.assertEqual(json.loads(json.dumps(offline, sort_keys=True)), output)
        self.assertEqual(json.loads(json.dumps(dossiers[0], sort_keys=True)), dossier(self.root))
        self.assertEqual(run["metrics"]["population"]["included"], 1)
        with self.assertRaisesRegex(ValueError, "absent from snapshot"):
            retro.Snapshot(self.root / "snapshot").request("GET", "repos/x/y/issues/1")
        self.assertEqual(retro.main(["--snapshot", str(self.root / "snapshot"), "--output", str(self.root / "b")]), 0)
        self.assertEqual((self.root / "b/run.json").read_text(), (self.root / "output/run.json").read_text())

    def test_private_text_and_undeclared_names_never_enter_snapshot_or_output(self):
        """Prose is never read; undeclared profile and model names become `other` before saving."""
        self.capture(self.task())
        for path in list((self.root / "snapshot").rglob("*.json")) + list((self.root / "output").rglob("*.json")):
            self.assertNotIn(SECRET, path.read_text(), path)
        observations = dossier(self.root)["observations"]
        turns = {t["id"]: t for t in observations["turns"]}
        self.assertEqual(set(turns), {TURN_ID, NAMED_TURN})
        self.assertEqual(observations["evaluation_turns_excluded"], 1)
        self.assertEqual((turns[NAMED_TURN]["profile"], turns[NAMED_TURN]["model"]), ("other", "other"))
        self.assertEqual(turns[TURN_ID]["outcome"], "ok")
        declared = {"profiles": [], "models": [], "legacy": False}
        row = {**dict.fromkeys(retro.TURN_COLUMNS), "status": SECRET, "id": "x y", "model": "m"}
        self.assertEqual(retro.turn_record(row, declared)["status"], "other")
        self.assertEqual(retro.turn_record(row, declared)["model"], "other")

    def test_legacy_turns_take_their_harness_as_profile(self):
        """Production legacy requests carry no agent record; their costs belong to the harness profile."""
        self.capture(self.task())
        turns = {t["id"]: t for t in dossier(self.root)["observations"]["turns"]}
        self.assertEqual(turns[TURN_ID]["profile"], "claude")
        self.assertEqual(turns[TURN_ID]["model"], "claude-opus-5-5")
        profiles = json.loads((self.root / "output/run.json").read_text())["metrics"]["profiles"]
        self.assertEqual(profiles["claude"]["turns"], 1)

    def test_usage_counts_each_observed_request_once(self):
        """Two sources for one request count once; a request without usage leaves the turn incomplete."""
        self.capture(self.task())
        turn = next(t for t in dossier(self.root)["observations"]["turns"] if t["id"] == TURN_ID)
        self.assertEqual(
            turn["tokens"], {"input_tokens": 100, "output_tokens": 20, "observed_requests": 2, "observed_with_usage": 1}
        )
        self.assertFalse(turn["usage_complete"])
        profile = json.loads((self.root / "output/run.json").read_text())["metrics"]["profiles"]["claude"]
        self.assertEqual(profile["input_tokens"], {"sum": None, "known": 0, "total": 1})
        self.assertEqual(profile["observed_input_tokens"], {"sum": 100, "known": 1, "total": 1})

    def assert_unsettled(self, operator):
        """A task with requests awaiting observation has no complete-turn usage, only observed totals."""
        run = self.capture(self.task(), operator=operator)
        self.assertTrue(all(t["usage_complete"] is False for t in dossier(self.root)["observations"]["turns"]))
        self.assertEqual(run["metrics"]["coverage"]["tasks_with_unsettled_inference"], 1)
        self.assertTrue(any("awaiting observations" in line for line in run["limitations"]))

    def test_pending_requests_never_make_usage_complete(self):
        self.assert_unsettled(Operator(pending=True))

    def test_unavailable_native_observations_never_make_usage_complete(self):
        self.assert_unsettled(Operator(native_status="unavailable"))

    def test_candidates_separate_human_follow_up_and_overlap(self):
        """Human commits and comments after approval and overlapping findings are reported, not classified."""
        self.capture(self.task())
        candidates = {(c["kind"], json.dumps(c["ref"])): c for c in dossier(self.root)["candidates"]}
        commit = candidates[("human_commit", json.dumps("d" * 40))]
        self.assertTrue(commit["after_all_approved"])
        self.assertTrue(commit["after_controller_head"])
        self.assertEqual(commit["paths"], ["a.rs"])
        self.assertIn(("human_inline_comment", json.dumps("c70")), candidates)
        self.assertIn(("overlapping_findings", json.dumps(["F1", "F2"])), candidates)
        self.assertNotIn(("overlapping_findings", json.dumps(["F1", "F3"])), candidates)
        self.assertNotIn("human_review", {kind for kind, _ in candidates})
        inputs = dossier(self.root)["observations"]["writer_inputs"]
        self.assertEqual([(i["id"], i["command"]) for i in inputs], [(1, "start")])

    def test_segments_read_the_fork_configuration(self):
        """A task without resumes has one segment, with roles and profiles from its configuration commit."""
        self.capture(self.task())
        [segment] = dossier(self.root)["segments"]
        self.assertEqual(segment["config_sha"], CONFIG_SHA)
        self.assertTrue(segment["configuration"]["legacy"])
        self.assertEqual(segment["configuration"]["roles"]["pr_reviewers"], ["claude", "codex"])
        self.assertEqual(segment["configuration"]["profiles"]["claude"]["models"], ["claude-opus-5-5"])
        attributions = [r["attribution"]["method"] for r in dossier(self.root)["observations"]["reports"]]
        self.assertEqual(set(attributions), {"only"})

    def test_terminal_tasks_outside_the_window_are_excluded(self):
        """Only the terminal transition's time decides a finished task's membership."""
        run = self.capture(self.task(), window=("2020-01-01", "2099-01-01"))
        self.assertEqual(run["population"][0]["reason"], "terminal outside window")
        self.assertFalse(list((self.root / "output/dossiers").iterdir()))

    def test_paused_tasks_are_included_whatever_the_window(self):
        """A suspended task is read by every run until it finishes."""
        run = self.capture(self.task(stage="blocked"), window=("2099-06-01", "2100-01-01"))
        self.assertEqual(run["population"][0]["reason"], "suspended or judged")
        self.assertTrue(run["population"][0]["included"])

    def test_active_tasks_are_excluded(self):
        run = self.capture(self.task(stage="review", findings=False))
        self.assertEqual(run["population"][0]["reason"], "active")

    def test_existing_snapshot_and_output_are_never_overwritten(self):
        self.capture(self.task())
        with self.assertRaisesRegex(ValueError, "already holds a snapshot"):
            retro.Snapshot(self.root / "snapshot", {"target": {}}, object())
        with self.assertRaisesRegex(ValueError, "already holds a run"):
            retro.write(self.root / "output", {}, [])


class ResumeAttributionTests(unittest.TestCase):
    """Real controller transitions: reviews after `/concurro resume` ran under the adopted configuration."""

    setUp = lifecycle.BundleLifecycleTests.setUp
    controller = lifecycle.BundleLifecycleTests.controller
    loaded = lifecycle.BundleLifecycleTests.loaded
    tick = lifecycle.BundleLifecycleTests.tick
    reach = lifecycle.BundleLifecycleTests.reach

    def resumed(self):
        """Review under configuration A, resume onto B with a changed claude model, and review again under B."""
        old = {**self.config, "config_sha": "1" * 40}
        new = deepcopy(old)
        new["config_sha"] = "2" * 40
        new["models"]["claude"] += "-updated"
        self.config = old
        controller = self.reach("gates")
        controller.resume_target = lambda: (new, controller.bundle)
        self.gh.comment("/concurro resume")
        controller.commands(self.gh.comments)
        self.config, self.agents.fix = new, True
        self.reach("gates")
        return old, new

    def collect(self, old, new, tamper=None):
        """Pause the task and collect it, optionally altering the GitHub responses first."""
        controller = self.loaded()
        controller.suspend("blocked", "collect")
        responses, missing = task_responses(self.gh, {old["config_sha"]: old, new["config_sha"]: new})
        if tamper:
            tamper(responses)
        root = self.root / "retro"
        capture(root, responses, missing, None)
        return dossier(root)

    def assert_attributed(self, collected):
        """Reviews under A stay in S1; claude's reviews and finding under B are in S2."""
        self.assertTrue(collected["configuration_history"]["complete"])
        self.assertEqual([s["config_sha"] for s in collected["segments"]], ["1" * 40, "2" * 40])
        observations = collected["observations"]
        reviews = [r for r in observations["reports"] if r["step"] == "Code review"]
        segments = {(r["profile"], r["model"]): r["attribution"]["segment"] for r in reviews}
        self.assertEqual(segments[("claude", "claude-opus-5-5")], "S1")
        self.assertEqual(segments[("claude", "claude-opus-5-5-updated")], "S2")
        self.assertNotIn("only", {r["attribution"]["method"] for r in reviews})
        later_codex = [r for r in reviews if r["profile"] == "codex"][-1]
        self.assertNotEqual(later_codex["attribution"]["segment"], "S1")
        [finding] = observations["findings"]
        raising = next(r for r in reviews if r["id"] == finding["raised_in"])
        self.assertEqual(raising["model"], "claude-opus-5-5-updated")
        self.assertEqual(finding["attribution"]["segment"], "S2")

    def test_reviews_and_findings_after_a_model_change_resume(self):
        """A changed reviewer model decides its post-resume reviews; an unchanged one stays unknown."""
        self.assert_attributed(self.collect(*self.resumed()))

    def test_invalidation_after_resume_keeps_both_configurations(self):
        """Invalidation drops the current resume receipt; the audit's version history still has it."""
        old, new = self.resumed()
        self.gh.comment("/concurro invalidate")
        controller = self.loaded()
        controller.commands(self.gh.comments)
        self.assertNotIn("bundle_resume", self.loaded().state)
        self.assert_attributed(self.collect(old, new))

    def assert_unattributed(self, collected):
        """Without a complete history, no report or finding names a segment."""
        self.assertFalse(collected["configuration_history"]["complete"])
        records = collected["observations"]["reports"] + collected["observations"]["findings"]
        self.assertTrue(records)
        self.assertTrue(all(r["attribution"]["segment"] is None for r in records))

    def history_page(self, versions):
        """Replace the audit history with one final page holding only `versions`."""

        def tamper(responses):
            root = f"repos/{REPOSITORY}/issues/7/artifacts/1?per_page=30&page="
            for key in [k for k in responses if k.startswith(root)]:
                del responses[key]
            first = deepcopy(self.gh.repo("issues/7/artifacts/1?per_page=30&page=1"))
            first.update(versions=versions(self.gh.artifacts[7][1]["versions"]), has_next_page=False)
            responses[root + "1"] = first

        return tamper

    def test_history_missing_its_newest_versions_is_incomplete(self):
        """A history that doesn't start at the verified current version can't prove the task's configurations."""
        old, new = self.resumed()
        self.assert_unattributed(self.collect(old, new, self.history_page(lambda versions: versions[-1:])))

    def test_empty_history_is_incomplete(self):
        old, new = self.resumed()
        self.assert_unattributed(self.collect(old, new, self.history_page(lambda versions: [])))

    def test_incomplete_version_history_attributes_nothing(self):
        """A missing audit version means a configuration may be missing; no record is attributed."""
        old, new = self.resumed()
        versions = self.gh.artifacts[7][1]["versions"]
        del versions[len(versions) // 2]
        self.assert_unattributed(self.collect(old, new))


class AttributionTests(unittest.TestCase):
    """Execution evidence decides a segment; anything else stays unknown with its candidates."""

    def segment(self, identifier, starts, model, bundle="b1", effort="medium"):
        profiles = {"claude": {"provider": "anthropic", "models": [model], "effort": effort}}
        return {
            "id": identifier,
            "bundle_id": bundle,
            "starts": starts,
            "configuration": {"legacy": True, "roles": {}, "profiles": profiles},
        }

    def history(self, *segments, complete=True):
        return {"segments": list(segments), "complete": complete}

    def test_evidence_and_resume_times(self):
        history = self.history(
            self.segment("S1", None, "m1"), self.segment("S2", 100.0, "m2"), self.segment("S3", 200.0, "m2")
        )
        attribute = retro.attribute
        self.assertEqual(attribute(history, profile="claude", model="m1"), {"segment": "S1", "method": "execution"})
        self.assertEqual(
            attribute(history, profile="claude", model="m2"),
            {"segment": None, "method": "unknown", "candidates": ["S2", "S3"]},
        )
        self.assertEqual(
            attribute(history, profile="claude", model="m2", before=150.0), {"segment": "S2", "method": "execution"}
        )
        self.assertEqual(attribute(history, profile="claude", before=50.0), {"segment": "S1", "method": "execution"})
        self.assertEqual(attribute(history)["method"], "unknown")
        only = self.history(self.segment("S1", None, "m"))
        self.assertEqual(attribute(only, model="anything"), {"segment": "S1", "method": "only"})

    def test_incomplete_history_attributes_nothing(self):
        only = self.history(self.segment("S1", None, "m"), complete=False)
        self.assertEqual(
            retro.attribute(only, profile="claude", model="m"),
            {"segment": None, "method": "unknown", "candidates": ["S1"]},
        )

    def test_bundle_and_effort_rule_out_segments(self):
        history = self.history(
            self.segment("S1", None, "m", bundle="b1"), self.segment("S2", None, "m", bundle="b2", effort="high")
        )
        self.assertEqual(retro.attribute(history, bundle="b2")["segment"], "S2")
        self.assertEqual(retro.attribute(history, profile="claude", effort="medium")["segment"], "S1")

    def test_versions_split_segments_where_the_configuration_changed(self):
        """Each change of the effective configuration starts a segment at the comment that caused it."""
        base = {"bundle_id": "b0", "config_digest": "c0", "config_sha": None}
        first = {**base, "bundle_resume": {"new_bundle": "b1", "config_digest": "c1", "comment": 11}}
        invalidated = {**base, "bundle_id": "b2", "config_digest": "c1", "invalidations": [{"comment": 12}]}
        states = [{**base, "invalidations": []}, {**first, "invalidations": []}, {**first, "invalidations": []}]
        states.append(invalidated)
        result = retro.segments(None, states, invalidated, FORK, {11: 100.0, 12: 200.0})
        self.assertEqual(
            [(s["bundle_id"], s["config_digest"], s["starts"]) for s in result],
            [("b0", "c0", None), ("b1", "c1", 100.0), ("b2", "c1", 200.0)],
        )
        self.assertTrue(all(s["configuration"] is None for s in result))


if __name__ == "__main__":
    unittest.main()
