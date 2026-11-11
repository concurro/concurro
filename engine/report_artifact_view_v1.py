"""Immutable shared-view renderer 1, including its formatting dependencies.

This compatibility snapshot deliberately does not import live UI helpers. Its output is
part of interrupted-write recovery; change presentation by adding a renderer version.
"""

import re
from types import SimpleNamespace

EVENT_MARKER = "<!-- concurro:event:v1:"
VIEW_MARKER = "<!-- concurro:report-view:v5 -->"

VERDICT = {
    "approve": ("✅", "Approve"),
    "changes_requested": ("🛠️", "Changes requested"),
    "reopen": ("🧭", "Design reopened"),
}

DECISION = {
    "proceed": ("✅", "Proceed"),
    "approve": ("✅", "Approve"),
    "revise": ("🛠️", "Revise"),
    "clarify": ("❓", "Clarify"),
    "reject": ("🛑", "Reject"),
    "escalate": ("📣", "Escalate"),
}

TITLE_LIMIT, HEADLINE_LIMIT, FALLBACK_LIMIT = 100, 160, 240

FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")

INLINE = re.compile(r"[\\`*_\[\]()!~|]")

BLOCK_START = re.compile(r"[#+=-]")

NUMBERED = re.compile(r"(\d{1,9})([.)])")

PUNCTUATION = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")

SENTENCE = re.compile(r"[.!?](?=\s|$)")


def label(entry):
    """`emoji Label` for one vocabulary entry."""
    return f"{entry[0]} {entry[1]}"


def one_line(value):
    """Collapse all whitespace, including line breaks, to single spaces."""
    return " ".join(str(value).split())


def text(value, limit=None):
    """Plain inline text: one line, cut to `limit`, with every Markdown and HTML metacharacter escaped."""
    value = one_line(value)
    if limit and len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    value = INLINE.sub(lambda m: "\\" + m[0], value).replace("<", "&lt;").replace(">", "&gt;")
    if BLOCK_START.match(value):
        return "\\" + value
    return NUMBERED.sub(r"\1\\\2", value, count=1) if NUMBERED.match(value) else value


def code(value, cell=False):
    """An inline code span for an engine value; in a table cell, `|` is escaped as GitHub requires."""
    value = one_line(value)
    if cell:
        value = value.replace("|", "\\|")
    longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if value.startswith("`") or value.endswith("`") else ""
    return f"{fence}{pad}{value}{pad}{fence}"


def table(headers, rows):
    """A Markdown table of already-serialized cells."""
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("----" for _ in headers) + " |"]
    return "\n".join(lines + ["| " + " | ".join(row) + " |" for row in rows])


def inline_html(line):
    """Escape `<` outside the line's code spans.

    A backslash-escaped character, including a backtick, is literal and opens no span; an
    unclosed backtick run is literal too.
    """
    out, i = [], 0
    while i < len(line):
        if line[i] == "\\" and i + 1 < len(line) and line[i + 1] in PUNCTUATION:
            out.append(line[i : i + 2])
            i += 2
            continue
        if line[i] == "`":
            run = re.match(r"`+", line[i:])[0]
            close = re.search(r"(?<!`)" + run + r"(?!`)", line[i + len(run) :])
            if close:
                end = i + len(run) + close.end()
                out.append(line[i:end])
                i = end
                continue
            out.append(run)
            i += len(run)
            continue
        out.append("&lt;" if line[i] == "<" else line[i])
        i += 1
    return "".join(out)


def block(value):
    """Model text as a self-contained Markdown block.

    Outside code spans and fenced code every `<` is written as `&lt;`, so no tag, comment,
    processing instruction or raw HTML block can start; an unclosed fence is closed.
    """
    lines, fence = [], None
    for line in str(value).strip("\n").split("\n"):
        match = FENCE.match(line)
        if fence:
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not line[match.end() :].strip():
                fence = None
            lines.append(line)
        elif match and not (match[1][0] == "`" and "`" in line[match.end() :]):
            fence = match[1]
            lines.append(line)
        else:
            lines.append(inline_html(line))
    if fence:
        lines.append(fence)
    return "\n".join(lines)


def details(caption, body):
    """A collapsed section; GitHub renders Markdown inside only with the blank lines."""
    return f"<details><summary>{text(caption)}</summary>\n\n{body}\n\n</details>"


def sub(*parts):
    """The metadata line of already-serialized parts."""
    return "<sub>" + " · ".join(p for p in parts if p) + "</sub>"


def first_sentence(value):
    """The first sentence of model text, or None when it has no short, clean one."""
    value = one_line(value)
    for match in SENTENCE.finditer(value):
        if match.end() >= 20:
            sentence = value[: match.end()]
            return sentence if len(sentence) <= FALLBACK_LIMIT else None
    return None


def excerpt(value, max_bytes, note, *, render=block):
    """Shorten raw source before rendering; preserve complete renderer-owned markup."""
    if len(note.encode()) + 2 > max_bytes:
        raise ValueError("excerpt note exceeds display capacity")
    rendered = render(value)
    if len(rendered.encode()) <= max_bytes:
        return rendered
    while len((rendered + "\n\n" + note).encode()) > max_bytes:
        value = value[: len(value) // 2]
        rendered = render(value)
    return rendered + "\n\n" + note


def inline(value, limit=TITLE_LIMIT, *, cell=False):
    """Bound inline prose while retaining complete code spans, including in table cells."""
    raw = one_line(value)
    if len(raw) > limit:
        raw = raw[: limit - 1].rstrip() + "…"
    parts, plain, i = [], [], 0
    while i < len(raw):
        if raw[i] == "\\" and i + 1 < len(raw):
            plain.extend(raw[i : i + 2])
            i += 2
            continue
        if raw[i] == "`":
            run = re.match(r"`+", raw[i:])[0]
            close = re.search(r"(?<!`)" + run + r"(?!`)", raw[i + len(run) :])
            if close:
                start = i + len(run)
                segment = "".join(plain)
                parts.extend(
                    (
                        segment
                        if not segment.strip()
                        else (
                            (" " if segment.startswith(" ") else "")
                            + text(segment)
                            + (" " if segment.endswith(" ") else "")
                        ),
                        (
                            code(raw[start : start + close.start()], cell=cell)
                            if raw[start : start + close.start()].strip()
                            else text(raw[i : start + close.end()])
                        ),
                    )
                )
                plain = []
                i = start + close.end()
                continue
            plain.append(run)
            i += len(run)
            continue
        plain.append(raw[i])
        i += 1
    segment = "".join(plain)
    parts.append((" " if segment.startswith(" ") else "") + text(segment))
    return "".join(parts)


def finding_title_v2(finding, *, cell=False):
    """A bounded finding title with inert prose and well-formed inline code."""
    raw = one_line(finding.get("title") or "") or first_sentence(finding["detail"]) or one_line(finding["detail"])
    return inline(raw, cell=cell)


_markup = SimpleNamespace(
    DECISION=DECISION,
    VERDICT=VERDICT,
    code=code,
    details=details,
    excerpt=excerpt,
    finding_title_v2=finding_title_v2,
    first_sentence=first_sentence,
    inline=inline,
    label=label,
    one_line=one_line,
    sub=sub,
    table=table,
    text=text,
)


def explanation(report, field, lead):
    """Remove an actually repeated headline while preserving the remaining Markdown."""
    raw = report[field]
    if len(_markup.one_line(lead)) > 240:
        return raw
    prefix = r"\s*" + r"\s+".join(re.escape(word) for word in lead.split()) + r"(?=\s|$)"
    match = re.match(prefix, raw)
    return raw[match.end() :].lstrip() if match else raw


def bullets(values, limit):
    """Render bounded model list entries without allowing them to introduce headings."""
    return "\n".join("- " + _markup.inline(value, limit) for value in values)


def render(event, value, links, limit=60000):
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
            _markup.label((_markup.VERDICT | _markup.DECISION)[verdict]) if verdict else "Implementation report"
        )
    heading = f"{EVENT_MARKER}{event['id']} -->\n{VIEW_MARKER}\n### {title}"
    footer = _markup.details("Raw data", links)
    agent = details.get("agent") or (report.get("agent") if not rejected else None) or {}
    identity = [agent.get(k) for k in ("profile", "provider", "model")]
    if agent.get("effort"):
        identity.append("effort " + agent["effort"])
    identity = list(dict.fromkeys(v for v in identity if v)) or [
        details.get("provider", details.get("reviewer", "Concurro")),
        details.get("model", ""),
    ]
    footer = _markup.sub(*(_markup.text(v, 200) for v in identity if v)) + "\n\n" + footer
    # Shorten excerpts as a unit; full canonical evidence is unchanged.
    for allowance in (2000, 500, 120, 40):
        sections = []
        if rejected:
            reason = details.get("diagnostic", {}).get("detail", "Output did not satisfy the response contract.")
            sections += [
                _markup.text(reason, allowance),
                "Rejected output; not an accepted review. Concurro will report any required action on the dashboard.",
            ]
            if value.get("truncated"):
                sections.append("Output excerpted to fit storage limits.")
        else:
            field = "rationale" if "rationale" in report else "summary"
            if report.get(field):
                lead = report.get("headline") or _markup.first_sentence(report[field]) or report[field]
                sections.append(_markup.inline(lead, 240))
                remainder = explanation(report, field, lead)
                if remainder:
                    sections.append(
                        _markup.details(
                            "Explanation",
                            _markup.excerpt(remainder, allowance + 200, "Full explanation in Raw data."),
                        )
                    )
            identifiers = report.get("finding_ids", [])
            if report.get("findings"):
                entries = []
                for i, finding in enumerate(report["findings"]):
                    identifier = identifiers[i] if i < len(identifiers) else finding.get("id", "Finding")
                    title = _markup.finding_title_v2(finding)
                    location = _markup.code(finding["path"]) if finding.get("path") else "General"
                    entries.append(
                        f"- **{_markup.text(identifier)}** {title} · {location}\n  "
                        + _markup.inline(finding["detail"], allowance)
                    )
                sections += ["#### Findings\n\n" + "\n".join(entries)]
            updates = report.get("objection_updates", [])
            if updates:
                identifiers = details.get("disposition_ids", [])
                entries = []
                for i, entry in enumerate(updates):
                    identifier = identifiers[i] if i < len(identifiers) else entry.get("id") or "New objection"
                    entries.append(
                        f"- **{_markup.text(identifier)}** · {_markup.text(entry['status'])}: "
                        + _markup.inline(entry.get("detail") or entry.get("evidence", ""), allowance)
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
                        f"- **{_markup.text(d['id'])}** · {_markup.text(d['status'])}: {_markup.inline(d['evidence'], allowance)}"
                        for d in report["dispositions"]
                    )
                ]
            if report.get("assessments"):
                labels = details.get("criterion_labels", {})
                rows = [
                    [
                        _markup.inline(labels.get(a["criterion"], "Review criterion"), 200, cell=True),
                        _markup.text(a["status"]),
                        _markup.inline(a["reason"], allowance, cell=True),
                    ]
                    for a in report["assessments"]
                ]
                sections += ["#### Assessments\n\n" + _markup.table(["Criterion", "Result", "Reason"], rows)]
            if report.get("design_issue"):
                sections += ["#### Design issue\n\n" + _markup.inline(report["design_issue"]["explanation"], allowance)]
            if report.get("validation"):
                sections += [
                    "#### Testing (agent-reported)\n\n"
                    + _markup.excerpt(report["validation"], allowance + 200, "Full validation in Raw data.")
                ]
            classification = report.get("classification")
            if classification:
                sections += [
                    " · ".join(
                        _markup.text(label + ": " + str(classification.get(key, "unknown")), 100)
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


def submitted(value):
    """Render version-one neutral presentation using only immutable submitted content."""
    note = "Submitted model output; acceptance, attribution and diagnostics are recorded on the issue dashboard."
    if not isinstance(value, dict) or not any((k in value for k in ("summary", "rationale", "decision", "verdict"))):
        return "### Stored task data\n\nCanonical data is available in this artifact's raw API body."
    event = {"id": "shared", "kind": "qc", "title": "Submitted model output", "details": {}}
    try:
        rendered = render(event, value, "Canonical data is in this artifact's raw API body.")
        rendered = rendered.split("\n", 2)[2]
    except (KeyError, TypeError, ValueError, AttributeError):
        rendered = "### Submitted model output\n\nCanonical data is available in this artifact's raw API body."
    return rendered + "\n\n" + note
