"""Version-five human report views over immutable canonical report data."""

import re

import markup
import protocol

VIEW_MARKER = "<!-- concurro:report-view:v5 -->"
RAW_OPEN = "<details><summary>Raw data</summary>\n\n"
RAW_CLOSE = "\n\n</details>"


def data_links(body):
    """Read only the controller's terminal data section in version-five archives."""
    prefix, separator, tail = body.rpartition(RAW_OPEN)
    if not separator or not prefix or not tail.endswith(RAW_CLOSE):
        raise ValueError("Missing report Raw data section")
    return tail.removesuffix(RAW_CLOSE)


def explanation(report, field, lead):
    """Remove an actually repeated headline while preserving the remaining Markdown."""
    raw = report[field]
    if len(markup.one_line(lead)) > 240:
        return raw
    prefix = r"\s*" + r"\s+".join(re.escape(word) for word in lead.split()) + r"(?=\s|$)"
    match = re.match(prefix, raw)
    return raw[match.end() :].lstrip() if match else raw


def bullets(values, limit):
    """Render bounded model list entries without allowing them to introduce headings."""
    return "\n".join("- " + markup.inline(value, limit) for value in values)


def render(event, value, links, limit=protocol.PUBLICATION_BODY_BYTES):
    """Render a deterministic readable archive, retaining canonical links on every path."""
    details, kind = event["details"], event["kind"]
    value = value if isinstance(value, dict) else {}
    report = (value.get("review") or value) if kind == "local" else value
    rejected = kind in ("intake_rejection", "review_rejection")
    if rejected:
        title = "Intake output rejected" if kind == "intake_rejection" else "Design review output rejected"
    else:
        verdict = report.get("verdict") or report.get("decision")
        title = event.get("title") or (
            markup.label((markup.VERDICT | markup.DECISION)[verdict]) if verdict else "Implementation report"
        )
    heading = f"{protocol.EVENT_MARKER}{event['id']} -->\n{VIEW_MARKER}\n### {title}"
    footer = markup.details("Raw data", links)
    agent = details.get("agent") or (report.get("agent") if not rejected else None) or {}
    identity = [agent.get(k) for k in ("profile", "provider", "model")]
    if agent.get("effort"):
        identity.append("effort " + agent["effort"])
    identity = list(dict.fromkeys(v for v in identity if v)) or [
        details.get("provider", details.get("reviewer", "Concurro")),
        details.get("model", ""),
    ]
    footer = markup.sub(*(markup.text(v, 200) for v in identity if v)) + "\n\n" + footer
    # Shorten excerpts as a unit; full canonical evidence is unchanged.
    for allowance in (2000, 500, 120, 40):
        sections = []
        if rejected:
            reason = details.get("diagnostic", {}).get("detail", "Output did not satisfy the response contract.")
            sections += [
                markup.text(reason, allowance),
                "Rejected output; not an accepted review. Concurro will report any required action on the dashboard.",
            ]
            if value.get("truncated"):
                sections.append("Output excerpted to fit storage limits.")
        else:
            field = "rationale" if "rationale" in report else "summary"
            if report.get(field):
                lead = report.get("headline") or markup.first_sentence(report[field]) or report[field]
                sections.append(markup.inline(lead, 240))
                remainder = explanation(report, field, lead)
                if remainder:
                    sections.append(
                        markup.details(
                            "Explanation",
                            markup.excerpt(remainder, allowance + 200, "Full explanation in Raw data."),
                        )
                    )
            identifiers = report.get("finding_ids", [])
            if report.get("findings"):
                entries = []
                for i, finding in enumerate(report["findings"]):
                    identifier = identifiers[i] if i < len(identifiers) else finding.get("id", "Finding")
                    title = markup.finding_title_v2(finding)
                    location = markup.code(finding["path"]) if finding.get("path") else "General"
                    entries.append(
                        f"- **{markup.text(identifier)}** {title} · {location}\n  "
                        + markup.inline(finding["detail"], allowance)
                    )
                sections += ["#### Findings\n\n" + "\n".join(entries)]
            updates = report.get("objection_updates", [])
            if updates:
                identifiers = details.get("disposition_ids", [])
                entries = []
                for i, entry in enumerate(updates):
                    identifier = identifiers[i] if i < len(identifiers) else entry.get("id") or "New objection"
                    entries.append(
                        f"- **{markup.text(identifier)}** · {markup.text(entry['status'])}: "
                        + markup.inline(entry.get("detail") or entry.get("evidence", ""), allowance)
                    )
                sections += ["#### Objections\n\n" + "\n".join(entries)]
            elif report.get("concerns"):
                sections += ["#### Concerns\n\n" + bullets(report["concerns"], allowance)]
            for key, caption in (("questions", "Questions"), ("tests", "Testing")):
                if report.get(key):
                    sections += [f"#### {caption}\n\n" + bullets(report[key], allowance)]
            if report.get("dispositions"):
                sections += [
                    "#### Earlier findings\n\n"
                    + "\n".join(
                        f"- **{markup.text(d['id'])}** · {markup.text(d['status'])}: {markup.inline(d['evidence'], allowance)}"
                        for d in report["dispositions"]
                    )
                ]
            if report.get("assessments"):
                labels = details.get("criterion_labels", {})
                rows = [
                    [
                        markup.inline(labels.get(a["criterion"], "Review criterion"), 200, cell=True),
                        markup.text(a["status"]),
                        markup.inline(a["reason"], allowance, cell=True),
                    ]
                    for a in report["assessments"]
                ]
                sections += ["#### Assessments\n\n" + markup.table(["Criterion", "Result", "Reason"], rows)]
            if report.get("design_issue"):
                sections += ["#### Design issue\n\n" + markup.inline(report["design_issue"]["explanation"], allowance)]
            if report.get("validation"):
                sections += [
                    "#### Testing (agent-reported)\n\n"
                    + markup.excerpt(report["validation"], allowance + 200, "Full validation in Raw data.")
                ]
            classification = report.get("classification")
            if classification:
                sections += [
                    " · ".join(
                        markup.text(label + ": " + str(classification.get(key, "unknown")), 100)
                        for key, label in (
                            ("complexity", "Complexity"),
                            ("change_risk", "Change risk"),
                            ("confidence", "Confidence"),
                        )
                    )
                ]
        body = "\n\n".join([heading, *sections, footer])
        if len(body.encode()) <= limit:
            return body
    # Extremely many entries still have a bounded human summary and complete data links.
    note = "This report exceeds the display limit. The complete report is available in Raw data."
    return "\n\n".join([heading, note, footer])
