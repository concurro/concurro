"""Cold artifact operations on a pilot-sized issue; replay unchanged in baseline checkouts."""

import argparse
import json
from copy import deepcopy
from unittest.mock import patch

import support  # noqa: F401 -- initializes the engine import path

import conversation
import shared_report_traffic
from audit_traffic import Traffic
from checkpoints import MARKER, Checkpoints
from fakes import CONFIG, PLAN, FakeGitHub
from policy import digest
from report_store import Store


def prepared(gh=None):
    """Build a pilot-sized issue with a bound plan, block history and a small published report."""
    gh = gh or FakeGitHub(CONFIG)
    gh.comments = []
    for index in range(150):
        gh.repo(
            "issues/7/artifacts", "POST", {"type": "generic", "name": f"Existing {index}", "body": "Older evidence"}
        )
    state = {
        "version": 2,
        "issue": 7,
        "stage": "stopped",
        "calls": 3,
        "plan": deepcopy(PLAN),
        "plan_id": digest(PLAN),
        "base": "b" * 40,
        "qc": {"plan": {"decision": "approve", "rationale": "Verified", "concerns": [], "questions": []}},
        "review_history": [{"note": str(i) + "x" * 1000} for i in range(19)],
    }
    cp = Checkpoints(gh, CONFIG, 7)
    cp.save(state)
    queue(state)
    conversation.publish(state, gh, CONFIG, cp.reports)
    cp.save(state)
    cp.publish_dashboard(MARKER + "\nFixture\n" + cp.links()[1] + "\n")
    return gh, state


def queue(state):
    """Queue a distinct design-review event for the current report."""
    conversation.queue(
        state,
        None,
        source=("qc", "plan"),
        kind="qc",
        details={
            "provider": "claude",
            "reviewer": "reviewer",
            "model": "model",
            "round": 1,
            "phase": "cooperative",
            "resolved": [],
        },
    )


class OperationTraffic(Traffic):
    """Include comment collection GETs as well as artifact and direct requests."""

    def pages(self, path):
        """The parent instruments artifacts; other collections use 100-item REST pages."""
        result = super().pages(path)
        if not path.endswith("/artifacts"):
            self.counts["GET"] += len(result) // 100 + 1
            for start in range(0, len(result) + 1, 100):
                self.downloaded += self.response_bytes(result[start : start + 100])
        return result


def measure(operation):
    """Isolate the named operation after identical published fixtures and cold process recovery."""
    gh, state = prepared(OperationTraffic())
    cp = Checkpoints(gh, CONFIG, 7)
    state = cp.load(gh.comments)
    if operation == "publish_retained":
        state["qc"]["plan"]["rationale"] = "Retained evidence. " * 1000
        cp.save(state)
        cp.publish_dashboard(MARKER + "\nFixture\n" + cp.links()[1] + "\n")
    elif operation == "publish_unreferenced":
        state["qc"]["plan"]["rationale"] = "New inline report"
        cp.save(state)
        cp.publish_dashboard(MARKER + "\nFixture\n" + cp.links()[1] + "\n")
    elif operation == "lost_write_recovery":
        # Equivalent durable evidence to a POST whose reply/checkpoint did not survive.
        Store(gh, 7, CONFIG["bot_login"]).put({"summary": "Unacknowledged evidence"})
    if operation not in ("cold_load", "legacy_discovery", "lost_write_recovery"):
        cp = Checkpoints(gh, CONFIG, 7)
        state = cp.load(gh.comments)
    count = len(gh.artifacts[7])
    gh.counts.clear()
    gh.uploaded = gh.downloaded = 0
    before = deepcopy(state)
    if operation == "cold_load":
        assert Checkpoints(gh, CONFIG, 7).load(gh.comments) == state
    elif operation == "save":
        cp.save(state)
    elif operation == "save_tail":
        state["review_history"].append({"note": "New inline tail"})
        cp.save(state)
    elif operation.startswith("publish_"):
        queue(state)
        conversation.publish(state, gh, CONFIG, cp.reports)
    elif operation == "legacy_discovery":
        assert Checkpoints(gh, CONFIG, 7).load([]) == state
    elif operation == "lost_write_recovery":
        Store(gh, 7, CONFIG["bot_login"]).put({"summary": "Unacknowledged evidence"})
    else:
        raise ValueError(operation)
    assert state["calls"] == before["calls"]
    return {
        "operation": operation,
        "existing_artifacts": count,
        "GET": gh.counts["GET"],
        "collection_GET": gh.counts["collection_GET"],
        "POST": gh.counts["POST"],
        "PATCH": gh.counts["PATCH"],
        "new_artifacts": len(gh.artifacts[7]) - count,
        "uploaded_json_bytes": gh.uploaded,
        "modeled_response_bytes": gh.downloaded,
        "harness_calls": 0,
    }


def results():
    """Compare ordinary known-reference operations and explicitly ambiguous discovery paths."""
    operations = [
        "cold_load",
        "save",
        "save_tail",
        "publish_existing",
        "publish_retained",
        "publish_unreferenced",
        "legacy_discovery",
        "lost_write_recovery",
    ]
    with patch.object(shared_report_traffic, "Traffic", OperationTraffic):
        lifecycle = shared_report_traffic.measure(48)
    return {"operations": [measure(operation) for operation in operations], "lifecycle": lifecycle}


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    print(json.dumps(results(), indent=2))
