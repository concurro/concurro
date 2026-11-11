"""Structured implementer descriptions and conservative ownership of published PR bodies."""

import re

import markup
import protocol
from policy import Blocked, digest

TYPES = ("feat", "fix", "docs", "refactor", "perf", "test", "build", "ci", "chore", "revert")
FIELDS = {"problem": 1, "changes": 1, "compatibility": 0}


def sentence(value):
    """Recognize a nonempty single-line field."""
    return isinstance(value, str) and bool(value.strip()) and not any(c in value for c in "\r\n\x00")


def bullets(value, minimum):
    """Recognize a list of inline content fields with the required minimum."""
    return isinstance(value, list) and minimum <= len(value) and all(sentence(v) for v in value)


def validate(report):
    """Validate new fields while accepting already-retained legacy implementation results."""
    if "description" not in report and "commit" not in report:
        return
    description, commit = report.get("description"), report.get("commit")
    if report.get("design_issue") is not None and description is None and commit is None:
        return
    if (
        not isinstance(description, dict)
        or set(description) != {"lead", *FIELDS}
        or not sentence(description["lead"])
        or any(not bullets(description[k], minimum) for k, minimum in FIELDS.items())
        or not isinstance(commit, dict)
        or set(commit) != {"subject", "type", "changes"}
        or not sentence(commit["subject"])
        or commit["type"] not in TYPES
        or not bullets(commit["changes"], 1)
    ):
        raise Blocked("invalid implementation description or commit metadata")


REFERENCE = re.compile(
    r"(?P<url>https?://[^\s/<>]+/[^\s/<>]+/[^\s/<>]+/(?:issues|pull)/)(?=\d)"
    r"|(?P<issue>(?<![\w/])(?:[\w.-]+/[\w.-]+)?#)(?=\d)"
    r"|(?P<gh>(?<![\w-])GH-)(?=\d)"
    r"|(?P<mention>(?<![\w@])@)(?=[a-z0-9])",
    re.I,
)


def inert(value, *, preserve_code=True):
    """Break GitHub references, preserving code only in Markdown-rendered content."""
    value = str(value)
    if not preserve_code:
        return REFERENCE.sub(lambda m: m[0] + "\u200b", value)
    result, start, i = [], 0, 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            i += 2
            continue
        if value[i] == "`":
            run = re.match(r"`+", value[i:])[0]
            close = re.search(r"(?<!`)" + run + r"(?!`)", value[i + len(run) :])
            if close:
                end = i + len(run) + close.end()
                result += [REFERENCE.sub(lambda m: m[0] + "\u200b", value[start:i]), value[i:end]]
                start = i = end
                continue
            i += len(run)
            continue
        i += 1
    result.append(REFERENCE.sub(lambda m: m[0] + "\u200b", value[start:]))
    return "".join(result)


def inline(value):
    """Keep code spans and neutralize references without truncating bounded fields."""
    value = inert(value)
    return markup.inline(value, max(1, len(value)))


def validation_text(value):
    """Render validation as inert prose lines, never headings or checklist attestations."""
    return "\n".join(inline(line) for line in str(value).splitlines())


def sections(candidate, head, focus, repository=None):
    """Render fields in controller-owned sections with one source of testing claims."""
    description = candidate.get("description")
    if not description:
        return None
    if candidate.get("head") != head:
        raise Blocked("PR description does not match the candidate head")
    rendered = {"lead": inline(description["lead"])}
    rendered.update({key: "\n".join("- " + inline(v) for v in description[key]) for key in FIELDS})
    commit = (
        markup.link("candidate commit", f"https://github.com/{repository}/commit/{head}")
        if repository
        else markup.code(head[:12])
    )
    rendered["testing"] = (
        "Agent-reported validation for "
        + commit
        + ":\n\n"
        + validation_text(candidate.get("validation") or "No validation reported.")
    )
    rendered["review_focus"] = "\n".join("- " + inline(v) for v in focus)
    return rendered


def layout(parts, heading_level=3):
    """The default readable layout, omitting empty optional sections."""
    lines = [parts["lead"]]
    for key, title in (
        ("problem", "Problem"),
        ("changes", "Changes"),
        ("compatibility", "Compatibility"),
        ("testing", "Testing"),
        ("review_focus", "Review focus"),
    ):
        if parts[key]:
            lines.append(f"{'#' * heading_level} {title}\n\n{parts[key]}")
    return "\n\n".join(lines)


def commit_message(state, config, metadata):
    """Use the plan for the first subject and the implementer's round-specific repair metadata."""
    if metadata is None:
        # Already-retained results predate the new response contract.
        return f"fix(concurro): address issue #{state['issue']} (round {state['round']})"
    subject = metadata["subject"] if state.get("head") else state["plan"].get("headline") or state["plan"]["summary"]
    prefix = metadata["type"] + ": " if config.get("conventional_commits", False) else ""
    subject = prefix + inert(markup.one_line(subject).strip(), preserve_code=False)
    changes = "\n".join("- " + inert(v, preserve_code=False) for v in metadata["changes"])
    issue = (
        f"#{state['issue']}"
        if config.get("upstream", config.get("repository")) == config.get("repository")
        else protocol.issue_url(config["repository"], state["issue"])
    )
    return f"{subject}\n\n{changes}\n\nRefs {issue}"


def prepare_create(state, body, save):
    """Persist a body digest before creation so lost responses can recover ownership."""
    state["pr_description"] = {"intended": digest(body)}
    save()


def refresh(state, github, pr, body, save, legacy_body=None):
    """Refresh an unchanged owned body, retaining intended evidence across lost responses."""
    if not state.get("pr_description") and legacy_body is not None and pr.get("body") == legacy_body:
        state["pr_description"] = {"last": digest(legacy_body)}
        save()
    receipt = state.setdefault("pr_description", {})
    if receipt.get("human_edited"):
        return
    current = digest(pr.get("body") or "")
    if current not in (receipt.get("last"), receipt.get("intended")):
        receipt["human_edited"] = True
        save()
        return
    intended = digest(body)
    if current != intended:
        receipt["intended"] = intended
        save()
        github.repo(f"pulls/{pr['number']}", "PATCH", {"body": body})
        observed = github.repo(f"pulls/{pr['number']}")
        if digest(observed.get("body") or "") != intended:
            receipt["human_edited"] = True
            save()
            return
    receipt.update(last=intended, intended=intended)
    save()
