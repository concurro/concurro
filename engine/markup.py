"""Concurro's comment vocabulary and Markdown serializers.

Model text renders inert: a block of it cannot open HTML or leave a code fence open, and
titles and headlines render as plain text. Engine values are serialized by kind, so no
value is trusted to be Markdown-safe.
"""

import re

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
SEVERITY = {"critical": ("‼️", "Critical"), "high": ("❗", "High"), "medium": ("⚠️", "Medium"), "low": ("ℹ️", "Low")}
STATUS = {
    "open": ("📌", "Open"),
    "fixed": ("✅", "Fixed"),
    "obsolete": ("🗃️", "Obsolete"),
    "withdrawn": ("↩️", "Withdrawn"),
    "resolved": ("✅", "Resolved"),
}
PASS, FAIL = ("🟩", "Pass"), ("🟥", "Fail")
GATE, BLOCKED, ESCALATED = "🚧", "🛑", "📣"
BLOCKING_NOTE = "Every open finding blocks the merge, whatever its severity."
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


def fence(content, char="`"):
    """A block fence of at least three characters, longer than any matching run in its contents."""
    longest = max((len(run) for run in re.findall(char + r"+", content)), default=0)
    return char * max(3, longest + 1)


def code(value, cell=False):
    """An inline code span for an engine value; in a table cell, `|` is escaped as GitHub requires."""
    value = one_line(value)
    if cell:
        value = value.replace("|", "\\|")
    longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if value.startswith("`") or value.endswith("`") else ""
    return f"{fence}{pad}{value}{pad}{fence}"


def link(caption, url):
    """A link whose caption is plain text and whose URL the engine built."""
    url = "".join(c if c.isalnum() or c in "/:.-_#?=&%~" else "%{:02X}".format(ord(c)) for c in str(url))
    return f"[{text(caption)}]({url})"


def cell(value):
    """A plain-text table cell."""
    return text(value)


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


def headline(report, field):
    """The report's headline, or its main text's first sentence, as plain text; None without either."""
    explicit = one_line(report.get("headline") or "")
    if explicit:
        return text(explicit, HEADLINE_LIMIT)
    sentence = first_sentence(report.get(field) or "")
    return text(sentence) if sentence else None


def redundant(report, field):
    """Whether the main text says no more than its fallback headline: one short sentence."""
    return not one_line(report.get("headline") or "") and first_sentence(report.get(field) or "") == one_line(
        report.get(field) or ""
    )


def finding_title(finding):
    """A finding's title as plain text: its own, or its detail's first words."""
    raw = one_line(finding.get("title") or "") or first_sentence(finding["detail"]) or one_line(finding["detail"])
    return text(raw, TITLE_LIMIT)


def severity(finding):
    """`emoji Label` for a finding's severity; empty for findings recorded without one."""
    entry = SEVERITY.get(finding.get("severity"))
    return label(entry) if entry else ""


def excerpt(value, max_bytes, note):
    """Bound a Markdown display excerpt without changing its stored source text."""
    if len(note.encode()) + 2 > max_bytes:
        raise ValueError("excerpt note exceeds display capacity")
    rendered = block(value)
    if len(rendered.encode()) <= max_bytes:
        return rendered
    while len((rendered + "\n\n" + note).encode()) > max_bytes:
        value = value[: len(value) // 2]
        rendered = block(value)
    return rendered + "\n\n" + note


def review_phase(value):
    """Name retained-context reviews without changing persisted protocol identifiers."""
    return "iterative" if value == "cooperative" else str(value)


def inline(value, limit=TITLE_LIMIT):
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
                        code(raw[start : start + close.start()], cell=True),
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


def finding_title_v2(finding):
    """A bounded finding title with inert prose and well-formed inline code."""
    raw = one_line(finding.get("title") or "") or first_sentence(finding["detail"]) or one_line(finding["detail"])
    return inline(raw)
