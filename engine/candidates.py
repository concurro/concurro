"""Pure controller checks for text candidates; no build, test or repository code execution."""

from collections import Counter

DIFF_WORK_LIMIT = 200_000

import checks
import pr_description
from policy import Blocked, OutsideScope, path_allowed


def diff_work(before, after):
    """Bound SequenceMatcher's equal-line pair search before running it."""
    left, right = Counter(before), Counter(after)
    return sum(count * right.get(line, 0) for line, count in left.items())


def validate(before, after, plan, config):
    """Validate the complete approved-base-to-candidate change and return its sorted paths."""
    paths = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    if not paths:
        raise Blocked("candidate changes nothing")
    outside = [path for path in paths if not path_allowed(path, config) or not checks.in_scope(path, plan)]
    if outside:
        raise OutsideScope(outside)
    problem = checks.file_count_error(len(paths), plan, config)
    if problem:
        raise Blocked(problem)
    return paths


def development(report):
    """Require an explanation, agent-reported validation and either edits or a design-reopen request."""
    if (
        not isinstance(report, dict)
        or set(report)
        not in (
            {"summary", "validation", "edits", "design_issue"},
            {"summary", "validation", "edits", "design_issue", "description", "commit"},
        )
        or not isinstance(report["summary"], str)
        or not report["summary"].strip()
        or not isinstance(report["validation"], str)
    ):
        raise Blocked("invalid implementation response")
    pr_description.validate(report)
    if report["design_issue"] is not None and report["edits"]:
        raise Blocked("design reopening cannot include candidate edits")


def changed_paths(files):
    """Expand GitHub rename entries to both policy-governed paths, matching controller text edits."""
    paths = set()
    for item in files:
        paths.add(item["filename"])
        if item.get("status") == "renamed":
            previous = item.get("previous_filename")
            if not isinstance(previous, str) or not previous:
                raise Blocked("renamed PR file lacks its previous path")
            paths.add(previous)
    return sorted(paths)
