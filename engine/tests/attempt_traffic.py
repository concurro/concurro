"""Cold per-call storage and interrupted-attempt traffic; copy unchanged into phase two to compare."""

import argparse
import json
from collections import Counter
from copy import deepcopy
from unittest.mock import patch

import support  # noqa: F401 -- initializes the engine import path

import conversation
import report_artifact
from artifact_fixtures import fixed_parts
from checkpoints import AUDIT_MARKER, MARKER, PLAN_NAME, Checkpoints
from controller import Controller
from discovery_traffic import OperationTraffic, prepared, queue
from fakes import CONFIG, Crash, FakeAgents
from policy import digest
from report_store import MARKER as DATA_MARKER, Store, record_json

COUNTERS = ("GET", "collection_GET", "POST", "PATCH")


def snapshot(gh):
    """Count all storage requests, keeping artifact discovery visible separately."""
    return {key: gh.counts[key] for key in COUNTERS}


def inventory(gh, first=0):
    """Count fixture artifacts and completion JSON without making transport requests."""
    purposes, groups = Counter(), {}
    peak = 0
    for number, envelope in gh.artifacts.get(7, {}).items():
        item = envelope["artifact"]
        body = item["body"]
        if body.startswith(AUDIT_MARKER):
            peak = max(peak, *(len(v["body"].encode()) for v in envelope["versions"]))
        if number <= first:
            continue
        if body.startswith(DATA_MARKER):
            if "\n<!-- concurro:report-part:v" not in body:
                purposes["legacy_data_parts"] += 1
                continue
            key = body[len(DATA_MARKER) :].split(":", 1)[0]
            part = report_artifact.parse(body, key)
            groups.setdefault((key, part["layout"]["record"], part["layout"].get("nonce")), []).append(part)
            purposes["immutable_data_parts"] += 1
        elif body.startswith(AUDIT_MARKER):
            purposes["audit"] += 1
        elif item["name"] == PLAN_NAME:
            purposes["plan"] += 1
        elif item["name"].startswith("Concurro report "):
            purposes["legacy_readable_archive"] += 1
        else:
            purposes["fixture_seed"] += 1
    completion_bytes = 0
    complete_sets = partial_sets = 0
    for parts in groups.values():
        if len(parts) != len(parts[0]["layout"]["ends"]):
            partial_sets += 1
            continue
        complete_sets += 1
        record = json.loads("".join(p["part"] for p in sorted(parts, key=lambda p: p["index"])))
        if "completion" in record:
            completion_bytes += len(record_json(record["completion"]).encode())
    return {
        "artifact_purposes": dict(purposes),
        "completion_record_json_bytes": completion_bytes,
        "complete_upload_sets": complete_sets,
        "partial_upload_sets": partial_sets,
        "peak_audit_bytes": peak,
    }


def fixture(unrelated):
    """Use the existing ten/155-artifact fixtures, without process caches."""
    with patch(
        "discovery_traffic.range",
        side_effect=lambda *args: range(unrelated) if args == (150,) else range(*args),
        create=True,
    ):
        return prepared(OperationTraffic())


def controller(gh):
    """Load the real controller's storage and completion path without model dispatch."""
    c = Controller(gh, FakeAgents(), CONFIG, target_root=support.TARGET)
    c.checkpoints = Checkpoints(gh, CONFIG, 7)
    c.state = c.checkpoints.load(gh.comments)
    return c


def attempt(c, index, large):
    """Represent the already-paid call with a scoped session and a deterministic response."""
    c.state["calls"] += 1
    request = {"task": index}
    operation = {
        "id": digest(request),
        "attempt": f"{index:032x}",
        "attempts": 1,
        "uncertain": 0,
        "provider": "claude",
        "stage": "plan",
        "request": digest(request),
        "contract": {},
        "status": "started",
    }
    c.state["operation"] = operation
    session = {"scope": "s" * 64, "binding": "s" * 64, "turn": index, "restarts": 0}
    c.state["sessions"] = {"plan:fixture": deepcopy(session)}
    value = {
        "decision": "approve",
        "rationale": f"Attempt {index}: " + ("Verified source evidence. " * 1000 if large else "Verified."),
        "concerns": [],
        "questions": [],
    }
    return operation, value, {**session, "role": "plan:fixture"}, request


def complete(c, operation, value, session, request):
    """Call each checkout's production completion method, including original session accounting."""
    c.record_session_turn(session, c.state["calls"], request, value)
    if hasattr(c.checkpoints.reports, "completion"):
        c.complete(operation, value, session)
    else:
        c.complete(operation, value)


def measure(unrelated, large, count=6):
    """Measure cold load through retention, application, publication, receipt and dashboard."""
    gh, _ = fixture(unrelated)
    setup = {"requests": snapshot(gh), **inventory(gh), "includes_unrelated_seed_POSTs": unrelated}
    rows = []
    for index in range(count):
        existing = len(gh.artifacts[7])
        gh.counts.clear()
        gh.uploaded = gh.downloaded = 0
        c = controller(gh)
        loaded = snapshot(gh)
        operation, value, session, request = attempt(c, index, large)
        before = snapshot(gh)
        complete(c, operation, value, session, request)
        retained = snapshot(gh)
        state, cp = c.state, c.checkpoints
        if state.get("qc", {}).get("plan"):
            state.setdefault("qc_history", []).append(deepcopy(state["qc"]["plan"]))
        state["qc"]["plan"] = deepcopy(value)
        queue(state)
        state.pop("operation")
        cp.save(state)
        conversation.publish(state, gh, CONFIG, cp.reports)
        cp.save(state)
        cp.publish_dashboard(MARKER + f"\nCompleted calls: {state['calls']}\n" + cp.links()[1] + "\n")
        rows.append(
            {
                "call": index + 1,
                "existing_artifacts": existing,
                "response_json_bytes": len(json.dumps(value).encode()),
                "cold_load": loaded,
                "retention_save": {k: retained[k] - before[k] for k in COUNTERS},
                "whole_call_storage": snapshot(gh),
                "new_artifacts": len(gh.artifacts[7]) - existing,
                **inventory(gh, existing),
                "unused_upload_sets": 0,
                "uploaded_json_bytes": gh.uploaded,
                "modeled_response_bytes": gh.downloaded,
            }
        )
    return {
        "response": "detached" if large else "inline",
        "setup": setup,
        "calls": rows,
        "total": {key: sum(row["whole_call_storage"][key] for row in rows) for key in COUNTERS},
        "harness_calls": 0,
    }


def recovery(unrelated, outcome="complete"):
    """Measure a lost completion checkpoint separately, including the one discovery scan."""
    gh, _ = fixture(unrelated)
    c = controller(gh)
    operation, value, session, request = attempt(c, 0, outcome != "inline")
    if outcome == "reused":
        c.checkpoints.reports.put(value)
    c.checkpoints.save(c.state)
    c.checkpoints.publish_dashboard(MARKER + "\nStarted\n" + c.checkpoints.links()[1] + "\n")
    original = gh.repo

    def request_part(path, method="GET", data=None):
        """Leave only the first continuation for the partial-upload fixture."""
        result = original(path, method, data)
        if outcome == "partial" and method == "POST" and (data or {}).get("body", "").startswith(DATA_MARKER):
            raise Crash()
        return result

    with (
        patch.object(c.checkpoints, "write", side_effect=Crash),
        patch.object(gh, "repo", side_effect=request_part),
        fixed_parts(12000 if outcome == "partial" else 1_000_000),
    ):
        try:
            complete(c, operation, value, session, request)
        except Crash:
            pass
        else:
            raise AssertionError("completion did not crash")
    existing = len(gh.artifacts[7])
    gh.counts.clear()
    gh.uploaded = gh.downloaded = 0
    c = controller(gh)
    before = deepcopy(c.state)
    restored = c.recover(c.state["operation"])
    assert restored == (value if outcome == "complete" else None)
    assert c.state["calls"] == before["calls"]
    return {
        "outcome": outcome,
        "existing_artifacts": existing,
        "requests": snapshot(gh),
        "direct_GET": gh.counts["GET"] - gh.counts["collection_GET"],
        **inventory(gh),
        "unused_complete_sets": int(outcome == "reused"),
        "unused_partial_sets": int(outcome == "partial"),
        "replacement_call_cost_if_readmitted": int(outcome != "complete"),
        "uploaded_json_bytes": gh.uploaded,
        "modeled_response_bytes": gh.downloaded,
        "new_artifacts": len(gh.artifacts[7]) - existing,
        "harness_calls": 0,
        "session_turn": c.state["sessions"][session["role"]]["turn"],
    }


def exceptional(unrelated, operation):
    """Measure bootstrap, queued legacy publication or derived-data replacement separately."""
    if operation == "bootstrap":
        gh = OperationTraffic()
        cp = Checkpoints(gh, CONFIG, 7)
        cp.load([])
        cp.save({"version": 2, "issue": 7, "stage": "stopped", "calls": 0})
        cp.publish_dashboard(MARKER + "\nNew task\n" + cp.links()[1] + "\n")
        return {"operation": operation, "requests": snapshot(gh), **inventory(gh), "harness_calls": 0}
    gh, _ = fixture(unrelated)
    c = controller(gh)
    cp, state = c.checkpoints, c.state
    unused = 0
    if operation == "legacy_queued":
        state["qc"]["plan"]["rationale"] = "Legacy queued event"
        with patch.object(conversation, "event_format", return_value=5):
            queue(state)
        cp.save(state)
        cp = Checkpoints(gh, CONFIG, 7)
        state = cp.load(gh.comments)
    elif operation == "derived_lost":
        original = gh.repo

        def lose(path, method="GET", data=None):
            """Create the derived object, but lose its receipt."""
            result = original(path, method, data)
            if method == "POST":
                raise Crash()
            return result

        with patch.object(gh, "repo", side_effect=lose):
            try:
                cp.reports.put({"summary": "Derived payload " * 2000})
            except Crash:
                pass
        unused = 1
    else:
        raise ValueError(operation)
    existing = len(gh.artifacts[7])
    gh.counts.clear()
    gh.uploaded = gh.downloaded = 0
    if operation == "legacy_queued":
        conversation.publish(state, gh, CONFIG, cp.reports)
    else:
        Store(gh, 7, CONFIG["bot_login"], CONFIG["repository"]).put({"summary": "Derived payload " * 2000})
    return {
        "operation": operation,
        "existing_artifacts": existing,
        "requests": snapshot(gh),
        "new_artifacts": len(gh.artifacts[7]) - existing,
        "unused_complete_sets": unused,
        "uploaded_json_bytes": gh.uploaded,
        "modeled_response_bytes": gh.downloaded,
        **inventory(gh, existing),
        "harness_calls": 0,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unrelated", type=int, default=150)
    parser.add_argument("--recovery", action="store_true")
    args = parser.parse_args()
    result = {
        "workloads": [measure(args.unrelated, False), measure(args.unrelated, True)],
        "exceptions": [exceptional(args.unrelated, kind) for kind in ("bootstrap", "legacy_queued", "derived_lost")],
    }
    if args.recovery:
        result["recovery"] = [recovery(args.unrelated, kind) for kind in ("complete", "partial", "inline", "reused")]
    print(json.dumps(result, indent=2))
