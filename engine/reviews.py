"""PR review findings: a checkpoint-owned ledger that gives each finding one stable identity."""

from policy import digest

# A closed finding's text is excerpted once its review event is published.
EXCERPT = 300
ELIDED = "… [Full text in the published issue conversation.]"


def ledger(state):
    """The task's findings, oldest first."""
    return state.setdefault("findings", [])


def owned(state, provider):
    """The provider's findings it must answer: open ones, and ones withdrawn by a design reopen."""
    return [f for f in ledger(state) if f["provider"] == provider and f["status"] in ("open", "withdrawn")]


def context(state, provider):
    """What the reviewer sees of its own findings."""
    return [{k: f[k] for k in ("id", "status", "path", "start_line", "line", "detail")} for f in owned(state, provider)]


def apply(state, provider, report, head):
    """Record a validated report's dispositions and new findings; return the review sequence and new IDs.

    Deterministic for one report, so a retained model result applied again after a crash
    assigns the same identities.
    """
    sequence = state.get("review_sequence", 0) + 1
    state["review_sequence"] = sequence
    findings = {f["id"]: f for f in ledger(state)}
    for disposition in report["dispositions"]:
        finding = findings[disposition["id"]]
        finding.update(status=disposition["status"], answered=sequence)
    identifiers = []
    for item in report["findings"]:
        number = state.get("finding_sequence", 0) + 1
        state["finding_sequence"] = number
        identifier = f"F{number}"
        ledger(state).append({"id": identifier, "provider": provider, **item, "text_digest": digest(item["detail"]),
                              "head": head, "review": sequence, "status": "open"})
        identifiers.append(identifier)
    return sequence, identifiers


def withdraw(state):
    """A design reopen withdraws every open finding; its provider retires or re-adopts it under the new plan."""
    for finding in ledger(state):
        if finding["status"] == "open":
            finding["status"] = "withdrawn"


def open_findings(state):
    """Open findings for the implementer's repair context."""
    return [{k: f[k] for k in ("id", "provider", "path", "start_line", "line", "detail")}
            for f in ledger(state) if f["status"] == "open"]


def compact(state):
    """Excerpt the text of closed findings; called only once every queued event is published.

    The full text stays in the published review event that raised the finding, and its
    `text_digest` still identifies it.
    """
    for finding in state.get("findings", []):
        if (finding["status"] in ("fixed", "obsolete") and len(finding["detail"]) > EXCERPT
                and not finding["detail"].endswith(ELIDED)):
            finding["detail"] = finding["detail"][:EXCERPT] + ELIDED


def label(finding_id, item):
    """One finding as published: its ID, location and text; reports predating the ledger have neither IDs nor lines."""
    location = item.get("path") or "general"
    start, line = item.get("start_line"), item.get("line")
    if line is not None:
        location += f":{start}-{line}" if start not in (None, line) else f":{line}"
    prefix = f"**{finding_id}** " if finding_id else ""
    return f"{prefix}`{location}`: {item['detail']}"


def report_lines(report):
    """A report's new findings and dispositions as Markdown list items."""
    identifiers = report.get("finding_ids") or [None] * len(report["findings"])
    lines = ["- " + label(i, f) for i, f in zip(identifiers, report["findings"])]
    lines += [f"- **{d['id']}** {d['status']}: {d['evidence']}" for d in report.get("dispositions", [])]
    return lines
