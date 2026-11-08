"""Report storage traffic, immutable cache boundaries and checkpoint format migration."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from support import setUpModule as setUpModule, tearDownModule as tearDownModule
from checkpoints import (
    Checkpoints,
    CheckpointError,
    FORMAT,
    BATCH_FORMAT,
    AUDIT_MARKER,
    STATE_PREFIX,
    legacy_state,
    LIMIT,
)
from controller import Controller
from policy import digest, design_roster
from report_store import Cache, Store, MARKER, ReportDataError
from fakes import CONFIG, FakeAgents, FakeGitHub, APPROVE, design_report, assessment, intake
import classification
import conversation
import design


class StorageLifecycleTests(unittest.TestCase):
    """Long histories must not add reads or rewritten history blobs to an idle tick."""

    def setUp(self):
        """Create the same service-level cache used across controller invocations."""
        self.gh, self.agents = FakeGitHub(CONFIG), FakeAgents()
        self.gh.comments = []
        self.cache = Cache()

    def controller(self):
        """Load through the production checkpoint and shared-cache path."""
        controller = Controller(self.gh, self.agents, CONFIG, report_cache=self.cache)
        controller.issue = self.gh.issue
        controller.load(self.gh.comments)
        return controller

    def seed(self, count=15):
        """Install a long history of real report shapes and an associated generated plan."""
        controller = self.controller()
        controller.advance(7)
        controller.advance(7)
        controller.state["review_history"] = [
            {
                "claude": {
                    "verdict": "approve",
                    "summary": f"Review {i}: " + "Evidence. " * 300,
                    "findings": [],
                    "sequence": i,
                    "head": "a" * 40,
                }
            }
            for i in range(count)
        ]
        controller.state["stage"] = "stopped"
        controller.save()
        return controller

    def test_full_call_budget_keeps_checkpoints_bounded_and_active_ratings_intact(self):
        """Persist 48 maximally classified and timed reports through publication and reload."""
        self.run_capacity(parallel=False)

    def test_full_call_budget_with_large_pending_parallel_results(self):
        """Protect both sibling results and ratings even when their optional timing is dropped."""
        self.run_capacity(parallel=True)

    def run_capacity(self, parallel):
        """Exercise full history with optional concurrently retained model-sized reports."""
        controller = self.seed(0)
        state = controller.state
        bundle_receipt = {
            "approved_bundle": state["bundle_id"],
            "old_bundle": "b" * 64,
            "new_bundle": "c" * 64,
            "old_skill": "d" * 64,
            "new_skill": "e" * 64,
            "actor": "writer",
            "comment": 9999999999,
            "plan": state["plan_id"],
        }
        state["implementation_bundle"] = deepcopy(bundle_receipt)
        budget = controller.config["max_model_calls"]
        self.assertEqual(budget, 48)
        roster = design_roster(controller.config)
        originals, archives, sizes = {}, {}, []
        if parallel:
            state["review_batch_version"] = 1
            state["review_batch"] = {
                "binding": "f" * 64,
                "stage": "plan_qc",
                "members": {
                    m["id"]: {
                        "identity": m,
                        "status": "complete",
                        "operation": {
                            "id": digest(m),
                            "attempt": "e" * 32,
                            "status": "completed",
                            "stage": "plan_qc",
                            "provider": m["provider"],
                            "response": {
                                "decision": "approve",
                                "rationale": m["id"] + "R" * 290000,
                                "classification": assessment(rationale="R" * 600, unknowns=["U" * 200] * 5),
                            },
                            "timing": {
                                "turn": "aaaaaaaa-1234-1234-1234-123456789abc",
                                "status": "done",
                                "created": 100,
                                "started": 101,
                                "finished": 110,
                            },
                        },
                    }
                    for m in roster
                },
            }
            pending = deepcopy(state["review_batch"]["members"])

        def save_and_check(protected):
            """Exercise real pressure handling and storage, including actual artifact references."""
            controller.save(publish_events=False, outboxes=False)
            body = controller.checkpoints.audit["body"]
            sizes.append(len(body.encode()))
            self.assertLessEqual(sizes[-1], LIMIT)
            self.assertEqual(state["implementation_bundle"], bundle_receipt)
            for report in protected:
                source = report["classification"]["source"]["report"]
                self.assertEqual(report["classification"], originals[source])
            self.assertEqual(controller.checkpoints.decode({"body": body}), state)
            for key, member in state.get("review_batch", {}).get("members", {}).items():
                self.assertEqual(member["operation"]["response"], pending[key]["operation"]["response"])

        for i in range(budget):
            stage = "plan_qc" if i < budget // 2 else "review"
            member = roster[i % len(roster)]
            provider = member["provider"]
            if stage == "plan_qc":
                slot = state["design_votes"]
                if member["id"] in slot:
                    state["design_history"].append({"generation": i // 2, "votes": [deepcopy(slot[member["id"]])]})
                report = design_report(
                    intake(decision="approve", rationale=f"Review {i}: " + "Evidence. " * 300),
                    {"review_criteria": design.criteria(state["requirements"])},
                )
                path = ["design_votes", member["id"], "report"]
                details = {
                    "plan": state["plan_id"],
                    "round": i // 2 + 1,
                    "phase": "cooperative",
                    "reviewer": member["id"],
                    "resolved": [],
                    "disposition_ids": [],
                }
            else:
                slot = state["reviews"]
                if provider in slot:
                    state.setdefault("review_history", []).append({provider: deepcopy(slot[provider])})
                report = {
                    **deepcopy(APPROVE),
                    "summary": f"Review {i}: " + "Evidence. " * 300,
                    "head": "f" * 40,
                    "sequence": i + 1,
                    "finding_ids": [],
                    "dispositions": [],
                    "design_issue": None,
                }
                path = ["reviews", provider]
                details = {
                    "head": "f" * 40,
                    "round": i // 2 + 1,
                    "pr": 9,
                    "pr_url": f"https://github.com/{CONFIG['repository']}/pull/9",
                }
            state.update(
                calls=i + 1,
                round=i // 2,
                operation={
                    "id": digest(i),
                    "attempt": f"{i:032x}",
                    "status": "consumed",
                    "stage": stage,
                    "provider": provider,
                    "timing": {
                        "turn": f"{i:08x}-1234-1234-1234-123456789abc",
                        "status": "done",
                        "created": 1794040000.123456,
                        "started": 1794040010.123456,
                        "finished": 1794040610.123456,
                    },
                },
            )
            if stage == "review":
                state["head"] = "f" * 40
            agent = {
                "profile": provider + "-high",
                "provider": "anthropic" if provider == "claude" else "openai",
                "harness": provider,
                "model": "claude-opus-5-5" if provider == "claude" else "gpt-6.1-sol",
                "effort": "high",
            }
            report = classification.attach(
                {
                    **report,
                    "classification": assessment(rationale="R" * 600, unknowns=[str(n) + "U" * 199 for n in range(5)]),
                },
                state,
                agent,
                stage,
                classification.MAX_BYTES,
            )
            rating = report["classification"]
            self.assertEqual((len(rating["rationale"]), [len(v) for v in rating["unknowns"]]), (600, [200] * 5))
            source = rating["source"]["report"]
            originals[source] = deepcopy(rating)
            if stage == "plan_qc":
                slot[member["id"]] = {"identity": member, "binding": digest(i), "report": report}
            else:
                slot[provider] = report
            active = [v["report"] for v in state["design_votes"].values()] + list(state["reviews"].values())
            # Unarchived reports must survive even before an event is queued.
            save_and_check(active)
            conversation.queue(
                state,
                None,
                source=path,
                kind="qc" if stage == "plan_qc" else "review",
                details={**details, "provider": provider, "model": agent["model"]},
            )
            save_and_check(active)
            conversation.publish(state, self.gh, controller.config, controller.checkpoints.reports)
            observed = state["reports"][-1].get("timing")
            if not parallel:
                self.assertIsNotNone(observed)
            if observed:
                self.assertEqual(observed["attempt"], f"{i:032x}")
                self.assertEqual(observed["turn"], f"{i:08x}-1234-1234-1234-123456789abc")
            receipt = state["conversation"]["receipts"]["/".join(path)]
            archives[source] = deepcopy(receipt["data"])
            if stage == "review":
                # Keep the PR publication pending after the issue archive has been published.
                state["pr_outbox"] = [{"kind": "review", "provider": provider, "sequence": i + 1}]
                save_and_check(active)
                state["pr_outbox"] = []
            save_and_check(active)
            state = controller.state = self.controller().state

        self.assertEqual(state["calls"], budget)
        self.assertEqual(len(archives), budget)
        self.assertEqual(len(state["reports"]), conversation.REPORT_INDEX)
        if not parallel:
            self.assertTrue(any("timing" in report for report in state["reports"]))
        self.assertTrue(
            any("timing" not in report for report in state["reports"]),
            "pressure must drop optional old receipts before it blocks a checkpoint",
        )
        if not parallel:
            self.assertEqual(state["reports"][-1]["timing"]["attempt"], f"{budget - 1:032x}")
        self.assertNotIn("timings", state, "no independently growing timing history")
        historical = [v["report"] for entry in state["design_history"] for v in entry["votes"]]
        historical += [report for group in state["review_history"] for report in group.values()]
        self.assertTrue(
            any("classification" not in report or not report["classification"]["rationale"] for report in historical),
            "the fixture must actually exercise pressure relief",
        )
        # Trimming the checkpoint must not alter the complete immutable archived assessments.
        cold = Store(self.gh, 7, CONFIG["bot_login"], CONFIG["repository"])
        for source, ref in archives.items():
            self.assertEqual(cold.get(ref["digest"], ref["artifacts"])["classification"], originals[source])

    def test_idle_reloads_use_constant_reads_and_no_collection_or_data_writes(self):
        """Growing history does not increase steady-state artifact calls."""
        previous = self.seed().state
        original = self.gh.repo
        calls = []

        def record(path, method="GET", data=None):
            """Count network operations, including unexpected immutable writes."""
            calls.append((path, method))
            return original(path, method, data)

        with (
            patch.object(self.gh, "repo", side_effect=record),
            patch.object(self.gh, "pages", wraps=self.gh.pages) as pages,
        ):
            for _ in range(3):
                current = self.controller()
                self.assertEqual(current.state, previous)
                current.save()
            self.assertFalse(any("/artifacts" in call.args[0] for call in pages.call_args_list))
        # Audit + plan on load, audit recheck on unchanged save, independent of report count.
        self.assertEqual(len([path for path, _ in calls if "/artifacts/" in path]), 9)
        self.assertTrue(all(method == "GET" for path, method in calls if "/artifacts" in path))

    def test_changing_history_metadata_does_not_rewrite_evidence(self):
        """Sequence/status changes remain inline; an appended report stores only its own evidence."""
        controller = self.seed()
        before = {n for n, item in self.gh.artifacts[7].items() if item["artifact"]["body"].startswith(MARKER)}
        controller.state["review_history"][0]["claude"]["sequence"] += 100
        controller.state["review_history"].reverse()
        controller.save()
        after = {n for n, item in self.gh.artifacts[7].items() if item["artifact"]["body"].startswith(MARKER)}
        self.assertEqual(after, before)
        controller.state["review_history"].append({"codex": {"verdict": "approve", "summary": "New evidence. " * 300}})
        controller.save()
        after = {n for n, item in self.gh.artifacts[7].items() if item["artifact"]["body"].startswith(MARKER)}
        self.assertEqual(len(after - before), 1)
        self.assertEqual(self.controller().state, controller.state)

    def test_ledger_details_reference_the_report_instead_of_copying_it(self):
        """A finding's full text is recoverable from its report and is stored once."""
        controller = self.seed(0)
        detail = "UNIQUE FINDING " + "actual source evidence. " * 1500
        report = {"verdict": "changes_requested", "summary": "Fix this", "findings": [{"detail": detail}]}
        controller.state.update(reviews={"claude": report}, findings=[{"id": "F1", "status": "open", "detail": detail}])
        controller.save()
        raw = legacy_state(controller.checkpoints.audit["body"])
        ref = next(r for r in raw["report_data"] if r["path"] == ["findings", 0, "detail"])
        self.assertEqual(ref["select"], ["findings", 0, "detail"])
        self.assertEqual(ref["digest"], digest(report))
        bodies = [
            i["artifact"]["body"] for i in self.gh.artifacts[7].values() if i["artifact"]["body"].startswith(MARKER)
        ]
        self.assertEqual(sum(b.count("UNIQUE FINDING") for b in bodies), 1)
        self.assertEqual(self.controller().state["findings"][0]["detail"], detail)

    def test_cache_is_bounded_scoped_and_cannot_be_mutated_by_consumers(self):
        """Only exact accepted provenance can reuse cached evidence; eviction verifies again."""
        cache = Cache(max_bytes=10000)
        store = Store(self.gh, 7, CONFIG["bot_login"], CONFIG["repository"], cache)
        value = {"summary": "evidence " * 500}
        key = store.put(value)
        numbers = store.numbers[key]
        scope = store.scope
        hit = cache.get(scope, key, numbers)
        hit["summary"] = "mutated"
        self.assertEqual(cache.get(scope, key, numbers), value)
        for other in [("other/repo", 7, scope[2]), (scope[0], 8, scope[2]), (scope[0], 7, "human")]:
            self.assertIsNone(cache.get(other, key, numbers))
        self.assertIsNone(cache.get(scope, key, [numbers[0] + 100]))
        for i in range(5):
            cache.put(scope, digest(i), [100 + i], {"body": str(i) * 3000})
        self.assertLessEqual(cache.size, cache.limit)
        self.assertIsNone(cache.get(scope, key, numbers))
        fresh = Store(self.gh, 7, CONFIG["bot_login"], CONFIG["repository"], cache)
        with patch.object(self.gh, "pages", side_effect=AssertionError("known references need no listing")):
            self.assertEqual(fresh.get(key, numbers), value)
        self.gh.artifacts[7][numbers[0]]["versions"][0]["version"] = 2
        with self.assertRaises(ReportDataError):
            Store(self.gh, 7, CONFIG["bot_login"]).get(key, numbers)

    def test_v2_loads_but_new_wire_format_is_explicit_and_unknown_versions_stop(self):
        """Legacy state migrates without changing approval inputs; old readers reject v3."""
        controller = self.seed(5)
        checkpoints = controller.checkpoints
        state = deepcopy(controller.state)
        legacy = checkpoints.audit_body(state, external=False)
        self.assertEqual(legacy_state(legacy)["state"]["version"], 2)
        self.assertEqual(checkpoints.decode({"body": legacy}), state)
        raw = legacy_state(checkpoints.audit_body(state))
        self.assertEqual(raw["state"]["version"], FORMAT)
        # This is the deployed v2 reader's admission predicate.
        self.assertNotEqual(raw["state"]["version"], 2)
        self.assertEqual(self.controller().state, state)
        raw["state"]["version"] = BATCH_FORMAT + 1
        body = AUDIT_MARKER + STATE_PREFIX + json.dumps(raw) + "\n```\n"
        with self.assertRaises(CheckpointError):
            checkpoints.decode({"body": body})
        raw["state"]["version"] = 2
        body = AUDIT_MARKER + STATE_PREFIX + json.dumps(raw) + "\n```\n"
        with self.assertRaises(CheckpointError):
            checkpoints.decode({"body": body})

    def test_overlapping_and_foreign_state_references_fail_closed(self):
        """Nested placeholders cannot overwrite authority or overlap hydrated evidence."""
        controller = self.seed(5)
        checkpoints = controller.checkpoints
        raw = legacy_state(checkpoints.audit_body(controller.state))
        reference = raw["report_data"][0]
        for path in (
            ["approval"],
            reference["path"],
            reference["path"] + ["rationale"],
            reference["path"][:-1],
            ["review_history", -1],
        ):
            with self.subTest(path=path):
                broken = deepcopy(raw)
                broken["report_data"].append({**reference, "path": path})
                body = AUDIT_MARKER + STATE_PREFIX + json.dumps(broken) + "\n```\n"
                with self.assertRaises(CheckpointError):
                    checkpoints.decode({"body": body})

    def test_bound_checkpoint_ignores_unreferenced_copies_but_discovery_rejects_them(self):
        """Only the App's saved pointer selects authority once a checkpoint exists."""
        controller = self.seed(0)
        original = self.gh.artifacts[7][controller.checkpoints.audit["number"]]
        self.gh.artifacts[7][999] = deepcopy(original)
        self.gh.artifacts[7][999]["artifact"]["number"] = 999
        self.assertEqual(self.controller().state, controller.state)
        with self.assertRaises(CheckpointError):
            Checkpoints(self.gh, CONFIG, 7).load([])
