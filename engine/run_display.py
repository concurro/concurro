"""GitHub Actions labels and concise runner receipts; never execution authority."""

import json
import os
import re

import markup
import protocol
from model_config import default_effort
from vocab import TurnOutcome

INPUTS = {"task-label", "operation-label", "agent-label"}
OPERATIONS = {
    "intake": "Task assessment",
    "plan": "Design",
    "plan_qc": "Design review",
    "develop": "Implementation",
    "review": "PR review",
}
DECISIONS = {"approve", "changes_requested", "reopen", "revise", "proceed", "clarify", "reject", "escalate"}


def labels(request, task):
    """Describe the task and resolved invocation without exposing its prompt or credentials."""
    context = request.get("context", {})
    pr = context.get("pull_request")
    repository, number = (task.get("upstream"), pr) if pr else (task.get("repository"), task.get("issue"))
    target = f"{repository}#{number}" if repository and number else "Agent task"
    model = request.get("model", "Agent")
    effort = request.get("effort") or default_effort(request.get("provider"))
    agent = f"{model} · {effort} effort"
    profile = request.get("agent", {}).get("profile")
    if profile:
        agent = f"{profile} ({agent})"
    operation = OPERATIONS.get(request.get("stage"), "Agent task")
    if request.get("evaluation"):
        operation += " evaluation"
    elif context.get("review_phase") == "fresh":
        operation += " (fresh)"
    return {"task-label": markup.one_line(target), "operation-label": operation, "agent-label": markup.one_line(agent)}


def start(root, identifier, request, task):
    """Persist a receipt before execution so interrupted turns do not appear successful."""
    context = request.get("context", {})
    record = {**labels(request, task), "outcome": "No result recorded", "acknowledged": False}
    repository, issue = task.get("repository"), task.get("issue")
    if repository and issue:
        record["task"] = protocol.issue_url(repository, issue)
    if context.get("pull_request") and task.get("upstream"):
        record["pr"] = f"https://github.com/{task['upstream']}/pull/{context['pull_request']}"
    commit = context.get("head") or context.get("repository_commit") or context.get("base")
    if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit):
        record["commit"] = commit
    save(root, identifier, record)
    return record


def finish(root, identifier, record, result, elapsed, acknowledged):
    """Record execution and delivery separately; report only fixed decisions and counts."""
    record.update(elapsed=f"{elapsed:.1f}s", acknowledged=acknowledged)
    if result is not None:
        outcome = result.get("outcome")
        record["outcome"] = outcome if isinstance(outcome, str) and outcome in TurnOutcome else "Unknown"
        response = result.get("response")
        if outcome == TurnOutcome.OK and isinstance(response, dict):
            decision = response.get("verdict", response.get("decision"))
            if isinstance(decision, str) and decision in DECISIONS:
                record["decision"] = decision
            if isinstance(response.get("findings"), list):
                record["findings"] = len(response["findings"])
    save(root, identifier, record)


def save(root, identifier, record):
    """Save receipts outside agent workspaces without failing execution or delivery."""
    try:
        root.mkdir(parents=True, exist_ok=True)
        path = root / "run-summary.json"
        records = load(root)
        records[identifier] = record
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(records))
        temporary.replace(path)
    except (OSError, ValueError, RecursionError):
        print("Concurro result summary could not be saved.", flush=True)


def load(root):
    """Read receipts with the field shapes required by saving and rendering."""
    try:
        records = json.loads((root / "run-summary.json").read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(records, dict):
        raise ValueError("invalid summary receipts")
    for record in records.values():
        if (
            not isinstance(record, dict)
            or not all(isinstance(record.get(name), str) for name in INPUTS | {"outcome"})
            or type(record.get("acknowledged")) is not bool
            or any(
                not isinstance(record[name], str)
                for name in ("task", "pr", "commit", "decision", "elapsed")
                if name in record
            )
            or "findings" in record
            and (type(record["findings"]) is not int or record["findings"] < 0)
        ):
            raise ValueError("invalid summary receipt")
    return records


def summary(root):
    """Write all observed turns, or a setup/interruption notice, to the Actions summary."""
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if not destination:
        return
    notice = "No agent turn was recorded; setup may have failed or execution was interrupted."
    try:
        records = load(root)
    except (OSError, ValueError, RecursionError):
        records = {}
        notice = "Turn summary unavailable: stored receipts could not be read."
    lines = ["## Concurro result", "", f"Job status: **{markup.text(os.environ.get('JOB_STATUS', 'unknown'))}**", ""]
    if not records:
        for variable in ("RUN_OPERATION", "RUN_TASK", "RUN_AGENT"):
            if os.environ.get(variable):
                lines.extend([markup.text(os.environ[variable]), ""])
        lines.extend([notice, ""])
    for record in records.values():
        lines.extend([f"### {markup.text(record['operation-label'])} · {markup.text(record['task-label'])}", ""])
        rows = [("Agent", markup.text(record["agent-label"]))]
        for key, caption, resource in (("task", "Task", "issues"), ("pr", "PR", "pull")):
            url = record.get(key, "")
            if re.fullmatch(rf"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+/{resource}/[1-9][0-9]*", url):
                rows.append((caption, markup.link("Open " + caption, url)))
        if "commit" in record:
            rows.append(("Commit", markup.text(record["commit"])))
        rows += [
            ("Execution", markup.text(record["outcome"])),
            ("Result delivery", "Acknowledged" if record["acknowledged"] else "Not confirmed"),
        ]
        rows += [
            (name.title(), markup.text(record[name])) for name in ("decision", "findings", "elapsed") if name in record
        ]
        lines.extend([markup.table(("Detail", "Value"), rows), ""])
    lines.extend(
        ["Agent decisions are reported before controller validation; an OK execution is not task approval.", ""]
    )
    try:
        with open(destination, "a") as stream:
            stream.write("\n".join(lines))
    except (OSError, UnicodeError):
        print("Concurro result summary could not be written.", flush=True)
