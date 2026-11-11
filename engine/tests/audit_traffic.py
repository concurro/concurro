"""Offline audit traffic comparison; copy this script into the baseline checkout to replay it."""

import argparse
import json
import re
from collections import Counter
from copy import deepcopy
from urllib.parse import parse_qs

from markdown_it import MarkdownIt

import support  # noqa: F401 -- initializes the engine import path

from checkpoints import AUDIT_MARKER, MARKER, PLAN_NAME, Checkpoints
from fakes import CONFIG, PLAN, FakeGitHub
from github import GitHub
from policy import digest
from report_store import MARKER as DATA_MARKER
from storage_traffic import history_entry


class Traffic(FakeGitHub):
    """Model latest/history pagination and count serialized fixture payloads, including rendered HTML."""

    def __init__(self):
        """Start isolated issue storage and deterministic wire accounting."""
        super().__init__(CONFIG)
        self.comments = []
        self.counts = Counter()
        self.uploaded = self.downloaded = 0
        self.renderer = MarkdownIt("commonmark", {"html": True})

    def response_bytes(self, result):
        """Approximate transport bytes using CommonMark, without claiming GitHub HTML equivalence."""

        def rendered(value):
            """Add rendered bodies to API objects, removing invisible HTML comments as GitHub does."""
            if isinstance(value, list):
                return [rendered(v) for v in value]
            if not isinstance(value, dict):
                return value
            result = {k: rendered(v) for k, v in value.items()}
            if isinstance(value.get("body"), str):
                result["body_html"] = re.sub(r"<!--.*?-->", "", self.renderer.render(value["body"]), flags=re.S)
            return result

        return len(json.dumps(rendered(result), ensure_ascii=False).encode())

    def repo(self, path, method="GET", data=None):
        """Apply real fixture writes and simulate the measured 30-default/100-retained version window."""
        route, _, query = path.partition("?")
        # Use the route so this instrument also works with the baseline's unpaginated fake.
        result = super().repo(route, method, data)
        self.counts[method] += 1
        if data is not None:
            self.uploaded += len(json.dumps(data, ensure_ascii=False).encode())
        if method == "GET" and "/artifacts/" in route:
            versions = result["versions"][:100]
            parameters = parse_qs(query)
            size = int(parameters.get("per_page", [30])[0])
            page = int(parameters.get("page", [1])[0])
            result["versions"] = versions[(page - 1) * size : page * size]
            result["total_count"] = len(versions)
            result["has_next_page"] = page * size < len(versions)
        self.downloaded += self.response_bytes(result)
        return result

    def pages(self, path):
        """Count the checkout's real artifact page width, including its final empty page."""
        result = super().pages(path)
        if path.endswith("/artifacts"):
            client = GitHub(CONFIG["repository"])
            paths = []
            client.repo = lambda path: paths.append(path) or []
            client.pages(path)
            size = int(parse_qs(paths[0].partition("?")[2])["per_page"][0])
            requests = len(result) // size + 1
            self.counts["GET"] += requests
            self.counts["collection_GET"] += requests
            for offset in range(0, len(result) + 1, size):
                self.downloaded += self.response_bytes({"artifacts": result[offset : offset + size]})
        return result


def measure(count=48):
    """Save identical report histories and a dashboard, then perform ten cold idle save cycles."""
    gh = Traffic()
    cp = Checkpoints(gh, CONFIG, 7)
    fields = ("qc_history", "design_history", "review_history", "reports")
    state = {
        "version": 2,
        "issue": 7,
        "stage": "stopped",
        "plan": deepcopy(PLAN),
        "plan_id": digest(PLAN),
        "base": "b" * 40,
        **{field: [] for field in fields},
    }
    peak = 0

    def publish():
        """Use each revision's actual dashboard publication contract with identical visible text."""
        body = MARKER + f"\nCompleted calls: {state['calls']}\n" + cp.links()[1] + "\n"
        if hasattr(cp, "publish_dashboard"):
            cp.publish_dashboard(body)
        elif gh.comments:
            gh.repo(f"issues/comments/{gh.summary()['id']}", "PATCH", {"body": body})
        else:
            gh.repo("issues/7/comments", "POST", {"body": body})

    for index in range(count):
        for field in fields:
            state[field].append(history_entry(index))
        state["calls"] = index + 1
        cp.save(state)
        peak = max(peak, len(cp.audit["body"].encode()))
        publish()
    for _ in range(10):
        cp = Checkpoints(gh, CONFIG, 7)
        assert cp.load(gh.comments) == state
        cp.save(state)
        publish()
    purposes = Counter()
    for item in gh.artifacts[7].values():
        body = item["artifact"]["body"]
        if body.startswith(AUDIT_MARKER):
            purposes["audit"] += 1
        elif item["artifact"]["name"] == PLAN_NAME:
            purposes["plan"] += 1
        elif body.startswith(DATA_MARKER):
            # This workload uses short reports; block records alone contain a block layout.
            purposes["history" if '"blocks"' in body or '"round"' in body else "report"] += 1
    return {
        "entries_per_collection": count,
        "artifacts": dict(purposes),
        "requests": dict(gh.counts),
        "uploaded_json_bytes": gh.uploaded,
        "modeled_response_bytes": gh.downloaded,
        "peak_audit_body_bytes": peak,
        "interruption_injected": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries", type=int, default=48)
    print(json.dumps(measure(parser.parse_args().entries), indent=2))
