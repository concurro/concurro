"""Explicit, pinned mapping of structured PR fields into repository template headings."""

import re
from copy import deepcopy

import markup
import pr_description
from policy import Blocked

PATH = ".github/PULL_REQUEST_TEMPLATE.md"
FIELDS = {"lead", "problem", "changes", "compatibility", "testing", "review_focus"}
HEADING = re.compile(r"^ {0,3}(#{1,6} [^\r\n]+?)\s*$")
CHECKED = re.compile(r"^(\s*(?:[-+*]|\d+[.)])\s+)\[[xX]\]")


def validate(mapping):
    """Reject ambiguous or implicit mappings before any publication work."""
    if mapping is None:
        return
    if not isinstance(mapping, dict) or not 1 <= len(mapping) <= len(FIELDS):
        raise Blocked("pr_template_sections must map exact headings to field lists")
    seen = set()
    for heading, fields in mapping.items():
        if (
            not isinstance(heading, str)
            or not HEADING.fullmatch(heading)
            or not isinstance(fields, list)
            or not fields
            or not all(isinstance(field, str) and field in FIELDS for field in fields)
            or len(set(fields)) != len(fields)
            or seen.intersection(fields)
        ):
            raise Blocked("invalid or duplicate PR template field mapping")
        seen.update(fields)


def pin(state, config, read, save):
    """Freeze a regular UTF-8 template from the approved base once per base and mapping."""
    mapping = config.get("pr_template_sections")
    if mapping is None:
        return
    validate(mapping)
    previous = state.get("pr_template")
    if previous and previous["base"] == state["base"] and previous["sections"] == mapping:
        render({field: "" for field in FIELDS}, previous, state["base"])
        return
    template = read(state["base"], [PATH], inspection=True)[PATH]
    if template is None:
        raise Blocked(f"Configured PR template is missing at {PATH}")
    snapshot = {"base": state["base"], "sections": deepcopy(mapping), "text": template}
    # Validate headings now, before creating candidate Git objects.
    render({field: "" for field in FIELDS}, snapshot, state["base"])
    state["pr_template"] = snapshot
    save()


def render(parts, snapshot, base):
    """Insert fields only under exact headings and retain unchecked template checklists."""
    if not snapshot:
        return pr_description.layout(parts)
    if snapshot["text"] is None:
        raise Blocked(f"Configured PR template is missing at {PATH}")
    if snapshot["base"] != base:
        raise Blocked("PR template does not match the approved base")
    mapping = snapshot["sections"]
    validate(mapping)
    source = re.sub(r"<!--.*?-->", "", snapshot["text"], flags=re.S)
    lines, fence, found = [], None, set()
    heading_level = 6
    used = {field for fields in mapping.values() for field in fields}
    if "lead" not in used and parts["lead"]:
        lines += [parts["lead"], ""]
    for line in source.splitlines():
        match = markup.FENCE.match(line)
        if fence:
            lines.append(line)
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not line[match.end() :].strip():
                fence = None
            continue
        if match and not (match[1][0] == "`" and "`" in line[match.end() :]):
            fence = match[1]
            lines.append(line)
            continue
        heading = HEADING.fullmatch(line)
        name = heading[1] if heading else None
        if name:
            heading_level = min(heading_level, len(name.split(" ", 1)[0]))
        lines.append(markup.inline_html(CHECKED.sub(r"\1[ ]", line)))
        if name in mapping:
            if name in found:
                raise Blocked("PR template has a duplicated mapped heading")
            found.add(name)
            content = "\n\n".join(parts[field] for field in mapping[name] if parts[field])
            if content:
                lines += ["", content, ""]
    if fence or found != set(mapping):
        raise Blocked("PR template has an unclosed fence or a missing mapped heading")
    remaining = {key: value if key not in used and key != "lead" else "" for key, value in parts.items()}
    tail = pr_description.layout(remaining, heading_level=heading_level).strip()
    if tail:
        lines += ["", tail]
    return "\n".join(lines).strip()
