"""Evidence contracts for independent design review and stable objection updates."""

import json
import re
from functools import lru_cache
from pathlib import Path

import classification
from policy import MODEL_RESPONSE_LIMIT, Blocked
from repository import source_lines

OBJECTION_INSTRUCTIONS = (
    'For each new concern, add an objection_updates entry with id "" and status "open"; '
    "the controller assigns its ID. Reuse an ID only from own_objections, exactly once per "
    "supplied objection. concerns must equal the detail strings of all open entries in order. "
    "A revise verdict needs at least one open entry, even when own_objections is empty."
)


class ReviewError(Blocked):
    """A correctable reviewer-output failure with fixed, non-sensitive feedback."""

    def __init__(self, code, field="", detail=""):
        """Carry a catalog code and schema-derived field path, never model or transport text."""
        self.code, self.field, self.detail = code, field, detail
        super().__init__(code)


REPAIR_DETAILS = {
    "review_shape": "Return a complete, internally consistent review using the response schema.",
    "review_bounds": "Shorten the indicated field to its schema limit; retain all criteria and objections.",
    "review_evidence": "Cover every supplied criterion and own objection exactly once with consistent dispositions.",
    "review_objection_id": 'For a new objection use id "" and status "open". Only reuse an ID from own_objections, once per ID.',
    "review_objection_coverage": "Include exactly one disposition for every supplied own_objections ID.",
    "review_concerns": 'Keep the concerns and add an open objection_updates entry for each, with identical detail text in the same order. New objections use id "".',
    "review_citation": "Use a literal fragment within the stated source line, or a contiguous multiline quote starting there. Re-read the pinned source.",
    "review_source": "Choose a bounded regular UTF-8 source visible through repository inspection, without symlinks or Git metadata.",
}


def requirement_parts(text, acknowledgements=False):
    """Drop structural labels and pure courtesy replies while retaining substantive short answers."""
    if acknowledgements and re.fullmatch(
        r"(?:thanks|thank you|thanks a lot|thank you very much|cheers)[.! ]*", text.strip(), re.I
    ):
        return []
    lines = text.splitlines()
    structural = {
        "acceptance",
        "acceptance criteria",
        "requirements",
        "context",
        "background",
        "summary",
        "scope",
        "validation",
        "tests",
        "expected behavior",
        "implementation",
    }
    retained = []
    for i, line in enumerate(lines):
        value = line.strip()
        heading = re.sub(r"^#{1,6}\s+|\s+#+$", "", value).strip().strip("*").rstrip(":").casefold()
        if heading in structural and (
            value.endswith(":")
            or value.startswith("#")
            or i + 1 < len(lines)
            and re.fullmatch(r"[-=]{3,}", lines[i + 1].strip())
        ):
            continue
        if i and re.fullmatch(r"[-=]{3,}", value) and lines[i - 1].strip().rstrip(":").casefold() in structural:
            continue
        retained.append(line)
    return [p.strip() for p in re.split(r"\n\s*\n|\n(?=\s*(?:[-*+] |[0-9]+[.)] ))", "\n".join(retained)) if p.strip()]


def criteria(requirements):
    """Cover substantive request units without treating headings or courtesy replies as requirements."""
    values = [requirements["title"]]
    values += requirement_parts(requirements["body"])
    for comment in requirements.get("discussion", []):
        values += requirement_parts(comment["body"], acknowledgements=True)
    # Repeated statements remain in the full issue context, but need one assessment.
    values = list(dict.fromkeys(values))
    if len(values) > 32:
        raise Blocked("request exceeds design criterion limit; narrow or clarify it")
    return [{"id": "R" + str(i + 1), "text": value} for i, value in enumerate(values)]


def report_limit():
    """Expose the fixed response transport capacity; stored history does not reduce it."""
    return MODEL_RESPONSE_LIMIT


@lru_cache(maxsize=1)
def review_schema():
    """Use the provider schema as the single source of field and collection length limits."""
    return json.loads((Path(__file__).resolve().parents[1] / "bundle/schemas/plan_qc.json").read_text())


def validate_bounds(value, schema=None, field=""):
    """Enforce schema lengths locally even when a provider does not enforce them."""
    schema = review_schema() if schema is None else schema
    if (
        isinstance(value, str)
        and len(value) > schema.get("maxLength", float("inf"))
        or isinstance(value, list)
        and len(value) > schema.get("maxItems", float("inf"))
    ):
        raise ReviewError("review_bounds", field)
    if isinstance(value, dict):
        for key, child in schema.get("properties", {}).items():
            if key in value:
                validate_bounds(value[key], child, field + "." + key if field else key)
    elif isinstance(value, list) and "items" in schema:
        for i, child in enumerate(value):
            validate_bounds(child, schema["items"], f"{field}[{i}]")


def citation_matches(lines, number, quote):
    """Match fragments only within the exact line; multiline quotes must start on that line."""
    if not 1 <= number <= len(lines):
        return False
    quoted = source_lines(quote)
    if not quoted or not quoted[0]:
        return False
    if len(quoted) == 1:
        return quoted[0] in lines[number - 1]
    window = lines[number - 1 : number - 1 + len(quoted)]
    return (
        len(window) == len(quoted)
        and window[0].endswith(quoted[0])
        and window[1:-1] == quoted[1:-1]
        and window[-1].startswith(quoted[-1])
    )


def citation_line(lines, number, quote):
    """Keep a valid anchor, or relocate an exact quote only when its match is unique."""
    if citation_matches(lines, number, quote):
        return number
    quoted = source_lines(quote)
    if not quoted or not quoted[0]:
        return None
    match = None
    for candidate, line in enumerate(lines, 1):
        if quoted[0] in line and citation_matches(lines, candidate, quote):
            if match is not None or len(quoted) == 1 and line.count(quoted[0]) > 1:
                return None
            match = candidate
    return match


def validate_evidence(report, requirements, own_objections=(), files=None):
    """Reject incomplete coverage, invented citations and contradictory approval dispositions."""
    report = classification.core(report)
    validate_bounds(report)
    expected = {r["id"] for r in criteria(requirements)}
    assessments, sources, updates = (report.get(k) for k in ("assessments", "sources", "objection_updates"))
    if not isinstance(assessments, list) or not isinstance(sources, list) or not isinstance(updates, list):
        raise Blocked("design review requires criterion evidence and objection dispositions")
    source_ids = set()
    for source_index, source in enumerate(sources):
        if (
            not isinstance(source, dict)
            or set(source) != {"id", "path", "line", "quote"}
            or not isinstance(source["id"], str)
            or not source["id"]
            or source["id"] in source_ids
            or not isinstance(source["path"], str)
            or source["path"].startswith("/")
            or any(p in ("", ".", "..", ".git") for p in source["path"].split("/"))
            or "\\" in source["path"]
            or type(source["line"]) is not int
            or source["line"] < 1
            or not isinstance(source["quote"], str)
            or not source["quote"]
        ):
            raise Blocked("invalid design evidence source")
        source_ids.add(source["id"])
        if files is not None:
            content = files.get(source["path"])
            line = (
                citation_line(source_lines(content), source["line"], source["quote"]) if content is not None else None
            )
            if line is None:
                raise ReviewError("review_citation", f"sources[{source_index}]")
            source["line"] = line
    if not 1 <= len(sources) <= 12:
        raise Blocked("design review needs bounded repository evidence")
    seen = set()
    for item in assessments:
        if (
            not isinstance(item, dict)
            or set(item) != {"criterion", "status", "sources", "reason", "counterexample"}
            or not isinstance(item["criterion"], str)
            or item["criterion"] in seen
            or item["status"] not in ("satisfied", "defect", "uncertain")
            or not isinstance(item["sources"], list)
            or not item["sources"]
            or not all(isinstance(v, str) for v in item["sources"])
            or not set(item["sources"]).issubset(source_ids)
            or not all(isinstance(item[k], str) and item[k].strip() for k in ("reason", "counterexample"))
        ):
            raise Blocked("invalid criterion assessment")
        seen.add(item["criterion"])
    if seen != expected:
        raise Blocked("design review must assess every supplied criterion exactly once")
    own = {v["id"]: v for v in own_objections}
    updated, open_details = set(), []
    for index, update in enumerate(updates):
        if (
            not isinstance(update, dict)
            or set(update) != {"id", "status", "detail", "counterexample", "closure", "evidence"}
            or not isinstance(update["id"], str)
            or update["status"] not in ("open", "resolved")
            or not all(
                isinstance(update[k], str) and update[k].strip()
                for k in ("detail", "counterexample", "closure", "evidence")
            )
        ):
            raise Blocked("invalid objection disposition")
        identifier = update["id"]
        if identifier:
            if identifier not in own or identifier in updated:
                raise ReviewError("review_objection_id", f"objection_updates[{index}].id")
            updated.add(identifier)
        elif update["status"] != "open":
            raise ReviewError("review_objection_id", f"objection_updates[{index}].status")
        if update["status"] == "open":
            open_details.append(update["detail"])
    if updated != set(own):
        raise ReviewError("review_objection_coverage", "objection_updates")
    if open_details != report["concerns"]:
        raise ReviewError("review_concerns", "objection_updates")
    if report["decision"] == "approve" and (open_details or any(a["status"] != "satisfied" for a in assessments)):
        raise Blocked("approval contradicts incomplete criterion evidence")
    if any(a["status"] == "defect" for a in assessments) and not open_details:
        raise Blocked("criterion defects need actionable objections")
    if any(a["status"] == "uncertain" for a in assessments) and not (open_details or report["questions"]):
        raise Blocked("criterion uncertainty needs an objection or clarification")


def update_objections(state, report, member, phase):
    """Resolve or refine individual owned findings without erasing their audit history."""
    items = state.setdefault("objections", [])
    resolved, identifiers = [], []
    for update in report["objection_updates"]:
        if update["id"]:
            item = next(v for v in items if v["id"] == update["id"])
        else:
            sequence = state.get("objection_sequence", 0) + 1
            state["objection_sequence"] = sequence
            item = {"id": f"O{sequence}-{member['id'][:8]}", "reviewer": member["id"]}
            items.append(item)
        item.update(
            text=update["detail"],
            counterexample=update["counterexample"],
            closure=update["closure"],
            evidence=update["evidence"],
            resolved=update["status"] == "resolved",
            plan=state.get("plan_id"),
            phase=phase,
        )
        identifiers.append(item["id"])
        if item["resolved"]:
            resolved.append(item["id"])
    return resolved, identifiers
