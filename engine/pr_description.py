"""Bounded implementer descriptions and conservative ownership of published PR bodies."""

import markup
from policy import Blocked, digest

TYPES = ("feat", "fix", "docs", "refactor", "perf", "test", "build", "ci", "chore", "revert")
FIELDS = {"problem": (1, 4), "changes": (1, 6), "compatibility": (0, 4)}


def sentence(value, maximum=200):
    """Recognize a nonempty single-line field bounded in characters."""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= maximum
        and not any(c in value for c in "\r\n\x00")
    )


def bullets(value, minimum, maximum):
    """Recognize a bounded list of inline content fields."""
    return isinstance(value, list) and minimum <= len(value) <= maximum and all(sentence(v) for v in value)


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
        or any(not bullets(description[k], *bounds) for k, bounds in FIELDS.items())
        or not isinstance(commit, dict)
        or set(commit) != {"subject", "type", "changes"}
        or not sentence(commit["subject"], 72)
        or commit["type"] not in TYPES
        or not bullets(commit["changes"], 1, 6)
    ):
        raise Blocked("invalid implementation description or commit metadata")


def sections(candidate, head, focus, repository=None):
    """Render fields in controller-owned sections with one source of testing claims."""
    description = candidate.get("description")
    if not description:
        return None
    if candidate.get("head") != head:
        raise Blocked("PR description does not match the candidate head")
    rendered = {"lead": markup.text(description["lead"])}
    rendered.update({key: "\n".join("- " + markup.text(v) for v in description[key]) for key in FIELDS})
    commit = (
        markup.link("candidate commit", f"https://github.com/{repository}/commit/{head}")
        if repository
        else markup.code(head[:12])
    )
    rendered["testing"] = (
        "Agent-reported validation for "
        + commit
        + ":\n\n"
        + markup.block(candidate.get("validation") or "No validation reported.")
    )
    rendered["review_focus"] = "\n".join("- " + markup.text(v) for v in focus)
    return rendered


def layout(parts):
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
            lines.append(f"### {title}\n\n{parts[key]}")
    return "\n\n".join(lines)


def commit_message(state, config, metadata):
    """Use the plan for the first subject and the implementer's round-specific repair metadata."""
    if metadata is None:
        # Already-retained results predate the new response contract.
        return f"fix(concurro): address issue #{state['issue']} (round {state['round']})"
    subject = metadata["subject"] if state.get("head") else state["plan"].get("headline") or state["plan"]["summary"]
    subject = markup.one_line(subject).strip()[:72]
    if config.get("conventional_commits", False):
        subject = metadata["type"] + ": " + subject
    changes = "\n".join("- " + v for v in metadata["changes"])
    return f"{subject}\n\n{changes}\n\nRefs #{state['issue']}"


def prepare_create(state, body, save):
    """Persist a body digest before creation so lost responses can recover ownership."""
    state["pr_description"] = {"intended": digest(body)}
    save()


def refresh(state, github, pr, body, save):
    """Refresh an unchanged owned body, retaining intended evidence across lost responses."""
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
