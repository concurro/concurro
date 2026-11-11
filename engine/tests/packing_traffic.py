"""Replay frozen large-record workloads against phase two and response-budget packing."""

import argparse
import json
from copy import deepcopy
from unittest.mock import patch

import support  # noqa: F401 -- initializes the engine import path

from audit_traffic import Traffic
from fakes import CONFIG
from report_store import Store


def measure(name, value, lost_reply=False):
    """Publish, cold-recover and reload exact evidence, counting all fake API traffic."""
    gh = Traffic()
    original = gh.repo
    lost = []

    def store():
        """Start a cold process with no cached artifact references."""
        return Store(gh, 7, CONFIG["bot_login"], CONFIG["repository"])

    def write(path, method="GET", data=None):
        """Lose only the first applied reply, retaining the same fixture on both revisions."""
        response = original(path, method, data)
        if lost_reply and method == "POST" and not lost:
            lost.append(response["number"])
            raise RuntimeError("lost reply")
        return response

    with patch.object(gh, "repo", side_effect=write):
        try:
            store().put(value)
        except RuntimeError:
            if not lost:
                raise
    before = deepcopy(gh.artifacts[7])
    recovered = store()
    key = recovered.put(value)
    numbers = recovered.numbers[key]
    assert store().get(key, numbers) == value
    for number, item in before.items():
        assert gh.artifacts[7][number] == item
    bodies = [item["artifact"]["body"] for item in gh.artifacts[7].values()]
    return {
        "case": name,
        "logical_json_bytes": len(json.dumps(value).encode()),
        "artifacts": len(bodies),
        "unused_artifacts": len(set(gh.artifacts[7]) - set(numbers)),
        "largest_body_bytes": max(len(b.encode()) for b in bodies),
        "data_body_bytes": sum(len(b.encode()) for b in bodies),
        "requests": dict(gh.counts),
        "uploaded_json_bytes": gh.uploaded,
        "modeled_response_bytes": gh.downloaded,
        "interruption_injected": lost_reply,
        "harness_calls": 0,
    }


def cases():
    """Keep these inputs identical when replayed in a baseline checkout."""
    return [
        measure("3 MB plain record", {"evidence": "x" * 3_000_000}),
        measure("escaped Unicode record", {"evidence": '>"\\雪😀\n' * 100_000}),
        measure("9 MB interrupted record", {"evidence": "x" * 9_000_000}, lost_reply=True),
    ]


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    print(json.dumps(cases(), indent=2))
