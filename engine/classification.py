"""Advisory complexity and change-risk estimates derived from bound model reports."""

import json
from dataclasses import asdict, dataclass

import review_state
from model_config import assigned
from policy import approval_binding, design_roster, digest
from report_core import core as core
from reviews import report_for

LEVELS = ("unknown", "low", "medium", "high")
CONFIDENCE = ("low", "medium", "high")
STAGES = ("intake", "plan_qc", "review")
MAX_BYTES = 4096
FIELDS = {"complexity", "change_risk", "confidence", "rationale", "unknowns"}


@dataclass(frozen=True)
class Assessment:
    """Bounded descriptive labels and supporting evidence, never an approval input."""

    complexity: str = "unknown"
    change_risk: str = "unknown"
    confidence: str = "low"
    rationale: str = ""
    unknowns: tuple[str, ...] = ()
    diagnostic: str = ""
    version: int = 1

    def record(self):
        """Return JSON-compatible metadata with optional controller diagnostics."""
        value = asdict(self)
        value["unknowns"] = list(self.unknowns)
        if not value["diagnostic"]:
            del value["diagnostic"]
        return value


@dataclass(frozen=True)
class Snapshot:
    """The last complete estimate and the immutable report references that support it."""

    complexity: str
    change_risk: str
    confidence: str
    binding: dict
    sources: list
    version: int = 1

    def record(self):
        """Return a bounded checkpoint snapshot."""
        return asdict(self)


@dataclass(frozen=True)
class View:
    """A current or provisional estimate, including coverage and disagreement."""

    estimate: Snapshot
    assessed: int
    expected: int
    complete: bool
    provisional: bool
    disagreement: bool
    previous_binding: dict | None = None


def size(value):
    """Count serialized bytes using the checkpoint's conservative ASCII encoding."""
    return len(json.dumps(value, ensure_ascii=True).encode())


def normalize(value):
    """Treat missing or malformed metadata as unavailable, clipping excessive evidence."""
    if not isinstance(value, dict) or not FIELDS <= value.keys():
        return Assessment(diagnostic="Classification missing or malformed.").record()
    if (
        not all(isinstance(value[k], str) and value[k] in LEVELS for k in ("complexity", "change_risk"))
        or not isinstance(value["confidence"], str)
        or value["confidence"] not in CONFIDENCE
        or not isinstance(value["rationale"], str)
        or not isinstance(value["unknowns"], list)
        or not all(isinstance(v, str) for v in value["unknowns"])
    ):
        return Assessment(diagnostic="Classification missing or malformed.").record()
    try:
        value["rationale"][:600].encode("utf-8")
        for unknown in value["unknowns"][:5]:
            unknown[:200].encode("utf-8")
    except UnicodeError:
        return Assessment(diagnostic="Classification missing or malformed.").record()
    shortened = (
        len(value["rationale"]) > 600 or len(value["unknowns"]) > 5 or any(len(v) > 200 for v in value["unknowns"])
    )
    result = Assessment(
        value["complexity"],
        value["change_risk"],
        value["confidence"],
        value["rationale"][:600],
        tuple(v[:200] for v in value["unknowns"][:5]),
        "Classification evidence shortened." if shortened else "",
    ).record()
    # Retained responses may already carry a controller diagnostic; do not lose it on replay.
    if value.get("diagnostic") in ("Classification missing or malformed.", "Classification evidence shortened."):
        result["diagnostic"] = value["diagnostic"]
    return fit(result, MAX_BYTES)


def fit(value, limit):
    """Shorten optional evidence, then omit metadata if even its labels cannot fit."""
    result = dict(value)
    if size(result) <= limit:
        return result
    result.update(rationale="", unknowns=[], diagnostic="Classification evidence shortened.")
    return result if size(result) <= limit else None


def binding(state, stage):
    """Use the task's existing authoritative revision identities for one assessment stage."""
    value = {
        "stage": stage,
        "requirements": digest(state.get("requirements")),
        "base": state.get("base"),
        "bundle": state.get("bundle_id"),
        "config": state.get("config_digest"),
    }
    if stage != "intake":
        value.update(
            plan=state.get("plan_id"),
            generation=state.get("design_generation"),
            input_revision=state.get("input_revision", 0),
        )
    if stage == "plan_qc":
        value["phase"] = state.get("design_phase", "cooperative")
    if stage == "review":
        value.update(head=state.get("head"), round=state.get("round", 0))
    return value


def attach(report, state, agent, stage, allowance):
    """Attach normalized evidence with controller-owned provenance within available space."""
    result = core(report)
    if not isinstance(result, dict):
        return result
    value = normalize(report.get("classification"))
    operation = review_state.operation(state)
    value["source"] = {
        "binding": binding(state, stage),
        "agent": agent,
        "report": str(operation.get("id", "")) + ":" + str(operation.get("attempt", "")),
    }
    value = fit(value, min(MAX_BYTES, max(0, allowance)))
    if value is not None:
        result["classification"] = value
    return result


def without_hints(value):
    """Remove classification metadata from retrieved outcomes sent to independent assessors."""
    if isinstance(value, dict):
        return {k: without_hints(v) for k, v in value.items() if k not in ("classification", "classification_snapshot")}
    if isinstance(value, list):
        return [without_hints(v) for v in value]
    return value


def context_core(context):
    """Project advisory report hints without touching file maps or other source content."""
    return {
        key: without_hints(value) if key in ("prior_reviews", "design_feedback", "prior_outcomes_untrusted") else value
        for key, value in context.items()
    }


def stage_of(state):
    """Select the assessment scope currently being worked on, including repairs and reopens."""
    stage = state.get("stage")
    if stage in ("clarify", "rejected", "escalated"):
        history = state.get("qc_history", [])
        last = history[-1] if history else {}
        if (
            last.get("stage") in STAGES
            and last.get("base") == state.get("base")
            and last.get("requirements_digest") == binding(state, "intake")["requirements"]
            and (last["stage"] == "intake" or state.get("plan_id") and last.get("plan") == state["plan_id"])
            and (last.get("decision") in ("clarify", "reject", "escalate") or last.get("risk") == "high")
        ):
            return last["stage"]
    if stage in ("blocked", "stopped", "rejected", "escalated"):
        stage = state.get("retry_stage", stage)
    if stage == "intake" or not state.get("plan_id"):
        return "intake"
    if stage in ("plan", "plan_qc", "planned") or not state.get("head"):
        return "plan_qc"
    return "review"


def candidates(state, config, stage):
    """Read current report slots without adding a second observation store."""
    if stage == "intake":
        return [state.get("qc", {}).get("intake")]
    if stage == "plan_qc":
        votes = state.get("design_votes", {})
        return [
            v.get("report")
            if (v := votes.get(member["id"], {})).get("binding") == approval_binding(state, config)
            and v.get("identity") == member
            else None
            for member in design_roster(config)
        ]
    reports = dict(state.get("reviews", {}))
    reopened = state.get("review_event", {})
    identity = reopened.get("classification", {}).get("source", {}).get("agent", {}).get("profile")
    if identity:
        reports[identity] = reopened
    return [reports.get(provider) for provider in assigned(config, "pr_reviewers")]


def resolve_sources(sources, state):
    """Resolve stable operation references to the existing report archive index."""
    archives = {
        r["report"]: r["artifact"]
        for r in state.get("classification_snapshot", {}).get("sources", [])
        if r.get("artifact")
    }
    archives.update({r["assessment"]: r["artifact"] for r in state.get("reports", []) if r.get("assessment")})
    return [
        {**source, **({"artifact": archives[source["report"]]} if source["report"] in archives else {})}
        for source in sources
    ]


def reduce(assessments, expected, current_binding, previous=None):
    """Combine independent labels conservatively, without turning uncertainty into a gate."""
    known = [v for v in assessments if isinstance(v, dict)]
    assessed = sum(v.get("complexity") in LEVELS[1:] and v.get("change_risk") in LEVELS[1:] for v in known)
    complete = assessed == expected and expected > 0
    prior = previous if isinstance(previous, dict) else {}
    labels = {}
    for dimension in ("complexity", "change_risk"):
        values = [v.get(dimension, "unknown") for v in known]
        if not complete:
            values.append(prior.get(dimension, "unknown"))
        labels[dimension] = max((v for v in values if v in LEVELS), key=LEVELS.index, default="unknown")
    confidences = [v.get("confidence", "low") for v in known]
    if not complete and prior:
        confidences += [prior.get("confidence", "low")]
    confidence = min((v for v in confidences if v in CONFIDENCE), key=CONFIDENCE.index, default="low")
    disagreement = any(
        len({v.get(d) for v in known if v.get(d) in LEVELS[1:]}) > 1 for d in ("complexity", "change_risk")
    )
    sources = [{"report": v["source"]["report"]} for v in known if v.get("source", {}).get("report")]
    if not complete:
        sources += [s for s in prior.get("sources", []) if s["report"] not in {v["report"] for v in sources}]
    sources = sorted(sources, key=lambda v: v["report"])
    estimate = Snapshot(labels["complexity"], labels["change_risk"], confidence, current_binding, sources)
    return View(
        estimate,
        assessed,
        expected,
        complete,
        not complete or current_binding["stage"] == "intake",
        disagreement,
        prior.get("binding") if not complete and prior else None,
    )


def view(state, config):
    """Derive the current view, filtering reports whose authoritative binding is stale."""
    stage = stage_of(state)
    current = binding(state, stage)
    observations = []
    reports = candidates(state, config, stage)
    for report in reports:
        value = report.get("classification") if isinstance(report, dict) else None
        if isinstance(value, dict) and value.get("source", {}).get("binding") == current:
            observations.append(value)
    result = reduce(observations, len(reports), current, state.get("classification_snapshot"))
    return result


def snapshot(state, config):
    """Return a complete new snapshot or retain the last complete estimate with resolved links."""
    result = view(state, config)
    previous = state.get("classification_snapshot")
    value = result.estimate.record() if result.complete else dict(previous) if previous else None
    if value is not None:
        value["sources"] = resolve_sources(value["sources"], state)
    return value


def make_room(state, limit, measure):
    """Release historical advisory metadata without changing current or pending assessments."""
    if measure(state) <= limit:
        return
    archived = {r["assessment"] for r in state.get("reports", []) if r.get("assessment") and r.get("artifact")}
    current = list(state.get("qc", {}).values()) + list(state.get("reviews", {}).values())
    current += [vote["report"] for vote in state.get("design_votes", {}).values()]
    if state.get("review_event"):
        current.append(state["review_event"])
    frozen = {report.get("classification", {}).get("source", {}).get("report") for report in current}
    for event in state.get("conversation", {}).get("pending", []):
        value = state
        for key in event.get("source", []):
            value = value[key]
        if event.get("source") and isinstance(value, dict):
            frozen.add(value.get("classification", {}).get("source", {}).get("report"))
    for item in state.get("pr_outbox", []):
        if item.get("kind") == "review":
            report = report_for(state, item) or {}
            frozen.add(report.get("classification", {}).get("source", {}).get("report"))
    reports, snapshots, seen = [], [], set()

    def visit(value):
        """Collect existing optional metadata once, preserving shared report objects."""
        if not isinstance(value, (dict, list)) or id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, dict):
            if isinstance(value.get("classification"), dict):
                reference = value["classification"].get("source", {}).get("report")
                if reference in archived - frozen:
                    reports.append(value)
            if "classification_snapshot" in value:
                snapshots.append(value)
            for key, child in value.items():
                if key not in ("classification", "classification_snapshot", "operation", "pr_outbox"):
                    visit(child)
        else:
            for child in value:
                visit(child)

    visit(state)
    for report in reports:
        value = report["classification"]
        value.update(rationale="", unknowns=[])
        if measure(state) <= limit:
            return
    for report in reports:
        report.pop("classification", None)
        if measure(state) <= limit:
            return
    # Keep the task's last complete estimate longest; archives remain authoritative.
    for parent in reversed(snapshots):
        parent.pop("classification_snapshot", None)
        if measure(state) <= limit:
            return
    for entry in state.get("reports", []):
        if entry.get("assessment") not in frozen:
            entry.pop("assessment", None)
        if measure(state) <= limit:
            return
