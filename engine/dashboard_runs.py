"""Presentation boundaries for explicit invalidations, independent of approval counters."""

import markup

INVALIDATED = "Writer explicitly invalidated the design and requested reassessment."
EMPTY = {"artifact": 0, "generation": 0, "review_round": 0, "plan_revisions": 0}


def round_number(value):
    """Read a legacy display round when recognizable; unfamiliar labels remain unnumbered."""
    try:
        number = int(str(value).split()[0])
        return number if number > 0 else None
    except (ValueError, IndexError):
        return None


def start(state):
    """Capture the next run's display offsets before invalidation changes task state."""
    return {
        "artifact": max((r["artifact"] for r in state.get("reports", [])), default=0),
        "generation": state.get("design_generation", 0) + 1,
        "review_round": state.get("round", 0),
        "plan_revisions": state.get("plan_revisions", 0),
    }


def boundaries(state):
    """Read persisted boundaries, deriving older ones from existing invalidation history."""
    result = [dict(EMPTY)]
    history = [h for h in state.get("design_history", []) if h.get("reason") == INVALIDATED]
    reports = state.get("reports", [])
    for record in state.get("invalidations", []):
        if "display_start" in record:
            result.append(record["display_start"])
            continue
        previous = result[-1]
        old = next(
            (h for h in history if h.get("plan") == record.get("plan") and h["generation"] >= previous["generation"]),
            None,
        )
        generation = old["generation"] + 1 if old else previous["generation"] + 1
        newer = [
            r["artifact"]
            for r in reports
            if r["step"] in ("Intake", "Design review")
            and (number := round_number(r.get("round"))) is not None
            and number > generation
        ]
        earlier = [r for r in reports if r["artifact"] < min(newer, default=float("inf"))]
        cutoff = min(newer) - 1 if newer else max((r["artifact"] for r in earlier), default=previous["artifact"])
        result.append(
            {
                "artifact": cutoff,
                "generation": generation,
                "review_round": max(
                    (
                        number - 1
                        for r in earlier
                        if r["step"] == "Code review" and (number := round_number(r.get("round"))) is not None
                    ),
                    default=0,
                ),
                "plan_revisions": 0,
            }
        )
    return result


def retain_boundaries(state):
    """Freeze inferred legacy boundaries before report-index retention removes their evidence."""
    for record, boundary in zip(state.get("invalidations", []), boundaries(state)[1:]):
        record.setdefault("display_start", boundary)


def current_reports(state):
    """Return only report links belonging to the current assessment run."""
    cutoff = boundaries(state)[-1]["artifact"]
    return [r for r in state.get("reports", []) if r["artifact"] > cutoff]


def round_label(step, value, boundary):
    """Translate durable round identities into run-relative display numbers."""
    number, separator, suffix = str(value).partition(" ")
    offset = (
        boundary["generation"]
        if step in ("Intake", "Design review")
        else boundary["review_round"]
        if step == "Code review"
        else 0
    )
    parsed = round_number(number)
    return str(max(1, parsed - offset)) + separator + suffix if parsed is not None else str(value)


def assessment_state(state):
    """Exclude a previous run's fallback estimate without changing stored evidence."""
    if not state.get("invalidations") or not state.get("classification_snapshot"):
        return state
    snapshot = state["classification_snapshot"]
    reports = current_reports(state)
    current = {r.get("assessment") for r in reports if r.get("assessment")}
    cutoff = boundaries(state)[-1]["artifact"]
    sources = snapshot.get("sources", [])
    if sources and all(s.get("report") in current or s.get("artifact", 0) > cutoff for s in sources):
        return state
    return {k: v for k, v in state.items() if k != "classification_snapshot"}


def display_round(step, value, boundary):
    """Translate a dashboard label while preserving audit grouping and stored rounds."""
    label = round_label(step, value, boundary)
    number, separator, phase = label.partition(" ")
    return number + separator + markup.review_phase(phase) if step == "Design review" and separator else label
