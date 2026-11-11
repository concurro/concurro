"""Offline retention/publication lifecycle comparison; replay unchanged in the phase-one checkout."""

import argparse
import json
from collections import Counter
from copy import deepcopy

import support  # noqa: F401 -- initializes the engine import path

import conversation
from audit_traffic import Traffic
from checkpoints import AUDIT_MARKER, MARKER, PLAN_NAME, Checkpoints
from fakes import CONFIG, PLAN
from policy import digest
from report_store import MARKER as DATA_MARKER


def measure(count=48):
    """Count storage and modeled traffic for retained, annotated and published reports, then cold idle loads."""
    gh = Traffic()
    cp = Checkpoints(gh, CONFIG, 7)
    state = {
        "version": 2,
        "issue": 7,
        "stage": "stopped",
        "calls": 0,
        "plan": deepcopy(PLAN),
        "plan_id": digest(PLAN),
        "base": "b" * 40,
        "qc": {},
        "qc_history": [],
    }
    peak = 0
    for index in range(count):
        if state["qc"]:
            state["qc_history"].append(deepcopy(state["qc"]["plan"]))
        report = {
            "decision": "approve",
            "rationale": f"Report {index}: " + "Verified source evidence. " * 200,
            "concerns": [],
            "questions": [],
            "classification": {"complexity": "medium", "risk": "high", "source": {"report": f"attempt-{index}"}},
        }
        state["calls"] = index + 1
        state["operation"] = {"status": "completed", "response": deepcopy(report)}
        cp.save(state)  # Retain before validation/annotations/publication.
        report.update(agent={"profile": "reviewer"}, sequence=index)
        state["qc"]["plan"] = report
        conversation.queue(
            state,
            None,
            source=("qc", "plan"),
            kind="qc",
            details={
                "provider": "claude",
                "reviewer": "reviewer",
                "model": "test-model",
                "round": index + 1,
                "phase": "cooperative",
                "resolved": [],
            },
        )
        cp.save(state)
        conversation.publish(state, gh, CONFIG, cp.reports)
        state.pop("operation")
        cp.save(state)
        peak = max(peak, len(cp.audit["body"].encode()))
        cp.publish_dashboard(MARKER + f"\nCompleted calls: {state['calls']}\n" + cp.links()[1] + "\n")
    for _ in range(10):
        cp = Checkpoints(gh, CONFIG, 7)
        assert cp.load(gh.comments) == state
        cp.save(state)
        cp.publish_dashboard(MARKER + f"\nCompleted calls: {state['calls']}\n" + cp.links()[1] + "\n")
    purposes = Counter()
    for item in gh.artifacts[7].values():
        artifact = item["artifact"]
        body = artifact["body"]
        if body.startswith(AUDIT_MARKER):
            purposes["audit"] += 1
        elif artifact["name"] == PLAN_NAME:
            purposes["plan"] += 1
        elif artifact["name"].startswith("Concurro report "):
            purposes["separate_readable_view"] += 1
        elif body.startswith(DATA_MARKER):
            if "\n<!-- concurro:report-part:v2\n" in body:
                encoded = json.loads(body.split("\n<!-- concurro:report-part:v2\n", 1)[1].split("\n-->\n", 1)[0])[
                    "part"
                ]
            else:
                encoded = body.split("\n```", 1)[1].split("\n", 1)[1].split("\n```", 1)[0]
            record = json.loads(encoded)  # This workload deliberately stays single-part.
            if "report" in record:
                purpose = "annotation_wrapper"
            elif isinstance(record.get("value"), dict) and "decision" in record["value"]:
                purpose = "canonical_report"
            elif "blocks" in record or isinstance(record.get("value"), list):
                purpose = "history_or_index_block"
            else:
                purpose = "overflow_or_other_metadata"
            purposes[purpose] += 1
    return {
        "reports": count,
        "artifacts": dict(purposes),
        "artifact_total": sum(purposes.values()),
        "requests": dict(gh.counts),
        "uploaded_json_bytes": gh.uploaded,
        "modeled_response_bytes": gh.downloaded,
        "peak_audit_body_bytes": peak,
        "interruption_injected": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", type=int, default=48)
    print(json.dumps(measure(parser.parse_args().reports), indent=2))
