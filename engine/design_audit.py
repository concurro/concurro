"""Read-only, replayable design-review measurements from issue artifacts; never calls models."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess

import protocol
import dashboard_runs
from conversation import MARKER, REPORT_LINK
import review_timing
from policy import digest
from report_store import Store


class Snapshot:
    """Capture GET responses once locally, or reproduce a report entirely offline."""

    def __init__(self, directory, repository, issue, fetch=False):
        """Bind cached responses to one issue and an explicit collection mode."""
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository) or issue < 1:
            raise ValueError("Invalid repository or issue")
        self.directory, self.repository, self.issue = Path(directory), repository, issue
        self.fetch, self.reads = fetch, {}
        self.directory.mkdir(parents=True, exist_ok=True)
        meta = self.directory / "snapshot.json"
        if not meta.exists():
            if not fetch:
                raise ValueError("Snapshot missing; use --fetch to collect GET responses")
            meta.write_text(
                json.dumps(
                    {
                        "repository": repository,
                        "issue": issue,
                        "collection_started_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
            )
        self.meta = json.loads(meta.read_text())
        if (self.meta["repository"], self.meta["issue"]) != (repository, issue):
            raise ValueError("Snapshot belongs to another issue")

    def repo(self, path, method="GET", data=None):
        """Accept only this issue's artifact GETs, even if a caller tries to mutate."""
        if method != "GET" or data is not None or not re.fullmatch(rf"issues/{self.issue}/artifacts/[1-9][0-9]*", path):
            raise ValueError("Audit transport only permits artifact GETs")
        number = int(path.rsplit("/", 1)[1])
        file = self.directory / f"{number}.json"
        if not file.exists():
            if not self.fetch:
                raise ValueError("Artifact absent from offline snapshot")
            result = subprocess.run(
                ["gh", "api", "--method", "GET", f"repos/{self.repository}/{path}"],
                capture_output=True,
                text=True,
                timeout=90,
            )
            if result.returncode:
                raise ValueError("Artifact GET failed")
            value = json.loads(result.stdout)
            file.write_text(json.dumps(value))
        value = json.loads(file.read_text())
        artifact = value["artifact"]
        if artifact["number"] != number:
            raise ValueError("Artifact number mismatch")
        self.reads[number] = {
            "number": number,
            "id": artifact["id"],
            "url": protocol.artifact_url(self.repository, self.issue, number),
            "body_sha256": hashlib.sha256(artifact["body"].encode()).hexdigest(),
            "response_digest": digest(value),
        }
        return value


def json_block(body):
    """Read the JSON body from an audit or older embedded report archive."""
    match = re.search(r"\n(`{3,})json\n(.*?)\n\1(?:\n|$)", body, re.S)
    if not match:
        raise ValueError("JSON block unavailable")
    return json.loads(match[2])


def archived_report(snapshot, store, number):
    """Verify immutable authorship and full-report commitments, including nested parts."""
    item = store.verify(snapshot.repo(f"issues/{snapshot.issue}/artifacts/{number}"))
    if not item["body"].startswith(MARKER):
        raise ValueError("Not a report archive")
    root = protocol.artifact_url(snapshot.repository, snapshot.issue)
    parts = [match["number"] for match in REPORT_LINK.finditer(item["body"]) if match["root"] == root]
    if not parts:
        return json_block(item["body"])
    numbers = list(map(int, parts))
    first = store.verify(snapshot.repo(f"issues/{snapshot.issue}/artifacts/{numbers[0]}"))
    marker = re.match(r"<!-- concurro:report-data:v1:(" + protocol.DIGEST_PATTERN + r"):", first["body"])
    if not marker:
        raise ValueError("Missing report commitment")
    return store.get(marker[1], numbers)


def measure(snapshot, author="concurro[bot]"):
    """Measure indexed review outcomes, explicitly separating unavailable or ambiguous data."""
    response = snapshot.repo(f"issues/{snapshot.issue}/artifacts/1")
    audit, latest = response["artifact"], response["versions"][0]
    if (
        audit["creator"]["login"] != author
        or audit["updated_by_actor"]["login"] != author
        or latest["actor"]["login"] != author
        or latest["body"] != audit["body"]
        or not audit["body"].startswith("<!-- concurro:audit:v1 -->")
    ):
        raise ValueError("Audit authorship or latest version invalid")
    envelope = json_block(audit["body"])
    state = envelope["state"]
    if envelope["repository"] != snapshot.repository or state["issue"] != snapshot.issue:
        raise ValueError("Audit identity mismatch")
    store = Store(snapshot, snapshot.issue, author, snapshot.repository)
    seen, records, counts = set(), [], {}
    boundaries = dashboard_runs.boundaries(state)
    for entry in state.get("reports", []):
        if entry.get("step") != "Design review" or entry["artifact"] in seen:
            continue
        number = entry["artifact"]
        seen.add(number)
        run = max(i for i, b in enumerate(boundaries, 1) if number > b["artifact"])
        phase = dashboard_runs.round_label("Design review", entry["round"], boundaries[run - 1])
        count = counts.setdefault(f"run {run} / {phase}", Counter())
        count["archived_outputs"] += 1
        count["rejected" if entry["outcome"] == "invalid" else "accepted"] += 1
        record = {k: entry[k] for k in ("artifact", "provider", "round", "outcome")}
        record.update(run=run, run_round=phase)
        observed = review_timing.normalize(entry.get("timing"))
        record["timing"] = observed
        record["durations"] = review_timing.durations(observed)
        count["timing_observed" if observed else "timing_unavailable"] += 1
        if observed:
            record["attempt"] = entry["timing"].get("attempt")
            record["operation"] = entry["timing"].get("operation")
        record["url"] = protocol.artifact_url(snapshot.repository, snapshot.issue, number)
        if entry.get("diagnostic"):
            record["validation_code"] = entry["diagnostic"]["code"]
            count["validation:" + record["validation_code"]] += 1
        try:
            report = archived_report(snapshot, store, number)
            record["report_digest"] = digest(report)
            updates = report.get("objection_updates", [])
            record["objection_dispositions"] = [{"id": u["id"], "status": u["status"]} for u in updates]
            # Invalid reports are evidence of failed output, never authoritative dispositions.
            if entry["outcome"] != "invalid":
                count["open_objection_dispositions"] += sum(u["status"] == "open" for u in updates)
                count["resolved_objection_dispositions"] += sum(u["status"] == "resolved" for u in updates)
            record["verification"] = "verified"
        except (ValueError, KeyError, TypeError, RuntimeError) as error:
            record["verification"] = "unavailable: " + type(error).__name__
        records.append(record)
    return {
        "version": 2,
        "source": snapshot.meta,
        "audit_version": latest["version"],
        "bundle": state.get("bundle_id"),
        "config_digest": state.get("config_digest"),
        "config_sha": state.get("config_sha"),
        "engine_commit": None,
        "stage": state["stage"],
        "total_task_model_calls": state.get("calls"),
        "plan_revisions": state.get("plan_revisions"),
        "design_generation": state.get("design_generation"),
        "invalidations": state.get("invalidations", []),
        "run_boundaries": boundaries,
        "by_round": counts,
        "reports": records,
        "sources": [snapshot.reads[n] for n in sorted(snapshot.reads)],
        "limitations": [
            "Coverage is the frozen audit report index, not all historical artifacts; the index is capped at 40 entries.",
            "Archived outputs are not exact model-call counts: failed provider attempts may have no report.",
            "Rejected outputs are not automatically correction calls; retry cause is not fully recorded here.",
            "Objection dispositions are observations, not counts of unique defects. New-versus-repeated fresh findings require semantic inspection.",
            "Engine SHA, tokens, pure model durations, pure queue time and reviewer/planner discussion attribution are unavailable.",
            "Timing covers observed indexed reports only; failed attempts without reports and pruned receipts may be missing. Missing observations are unknown, not zero.",
            "Admission-to-claim includes staffing/setup; execution includes tools and transcript handling. No complete-round span or savings is inferred from partial receipts.",
            "No predicted call or wall-clock savings are treated as measured results.",
        ],
    }


def main():
    """Write derived metrics and commitments; keep raw API responses only in the local snapshot."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--author", default="concurro[bot]")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--fetch", action="store_true", help="Allow missing artifact GETs through gh")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    snapshot = Snapshot(args.snapshot, args.repository, args.issue, args.fetch)
    report = measure(snapshot, args.author)
    Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"Measured {len(report['reports'])} archived design outputs; read {len(report['sources'])} artifacts.")


if __name__ == "__main__":
    main()
