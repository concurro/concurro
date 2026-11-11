"""Reproducible offline history traffic workload using real checkpoint saves and fake GitHub."""

import argparse
import json
from contextlib import ExitStack
from copy import deepcopy
from unittest.mock import patch

import support  # noqa: F401 -- initializes the engine import path

import report_store
from checkpoints import LIMIT, Checkpoints
from fakes import CONFIG, FakeGitHub


def history_entry(index):
    """Include mutable annotations and canonical evidence shared across history collections."""
    return {
        "round": index,
        "provider": "claude",
        "note": "Prior evidence. " * 30,
        "report": {"verdict": "approve", "summary": f"Evidence {index}: " + "source evidence " * 200},
        "timing": {"created": index * 100, "started": index * 100 + 1, "finished": index * 100 + 90},
    }


def measure(count, whole_snapshots=False, *, report_characters=None, chunk_characters=None, snapshot_manifests=False):
    """Count immutable artifact bodies across append saves, with identical unlimited histories."""
    gh = FakeGitHub(CONFIG)
    cp = Checkpoints(gh, CONFIG, 7)
    fields = ("qc_history", "design_history", "review_history", "reports")
    state = {"version": 2, "issue": 7, "stage": "stopped", **dict.fromkeys(fields)}
    state.update({field: [] for field in fields})
    sizes = []
    reads = []
    original = gh.repo

    def request(path, method="GET", data=None):
        """Count report-data reads, including write confirmation and the final cold reload."""
        result = original(path, method, data)
        if method == "GET" and "/artifacts/" in path:
            if result["artifact"]["body"].startswith(report_store.MARKER):
                reads.append(path)
        return result

    def snapshot_reference(store, item, *, prepare=False):
        """Reproduce the previous checkpoint's whole immutable directory per list update."""
        key = store.put(item["value"]) if prepare else item["digest"]
        return {k: item[k] for k in ("path", "digest", "select", "annotations")} | {
            "artifacts": store.numbers.get(key, [])
        }

    with ExitStack() as context:
        if snapshot_manifests:
            context.enter_context(patch.object(report_store.Store, "checkpoint_reference", snapshot_reference))
        if whole_snapshots:
            context.enter_context(patch.object(report_store, "BLOCK_ENTRIES", count + 1))
        if chunk_characters is not None:
            context.enter_context(patch.object(report_store, "CHUNK", chunk_characters))
        context.enter_context(patch.object(gh, "repo", side_effect=request))
        for index in range(count):
            entry = history_entry(index)
            if report_characters is not None:
                entry["report"]["summary"] = f"Evidence {index}: " + "x" * report_characters
            for field in fields:
                state[field].append(deepcopy(entry))
            state["calls"] = index + 1
            cp.save(state)
            sizes.append(len(cp.audit["body"].encode()))
            assert sizes[-1] <= LIMIT
        assert Checkpoints(gh, CONFIG, 7).load([]) == state
        before = len(gh.artifacts[7])
        cp.save(state)
        assert len(gh.artifacts[7]) == before
    bodies = [item["artifact"]["body"] for item in gh.artifacts[7].values()]
    data = [body for body in bodies if body.startswith(report_store.MARKER)]
    return {
        "entries_per_collection": count,
        "data_artifacts": len(data),
        "data_body_bytes": sum(len(body.encode()) for body in data),
        "data_gets": len(reads),
        "peak_audit_bytes": max(sizes),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries", type=int, nargs="+", default=[48, 192])
    parser.add_argument("--whole-snapshots", action="store_true")
    parser.add_argument("--snapshot-manifests", action="store_true")
    parser.add_argument("--report-characters", type=int)
    parser.add_argument("--chunk-characters", type=int)
    options = parser.parse_args()
    print(
        json.dumps(
            [
                measure(
                    n,
                    options.whole_snapshots,
                    report_characters=options.report_characters,
                    chunk_characters=options.chunk_characters,
                    snapshot_manifests=options.snapshot_manifests,
                )
                for n in options.entries
            ],
            indent=2,
        )
    )
