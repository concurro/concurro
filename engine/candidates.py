"""Pure controller checks for text candidates; no build, test or repository code execution."""

import difflib
from collections import Counter

DIFF_WORK_LIMIT = 200_000

import checks
from policy import Blocked, OutsideScope, path_allowed


def lines(text):
    """LF-delimited source lines, without a phantom trailing line."""
    parts = (text or "").split("\n")
    return parts[:-1] if parts[-1] == "" else parts


def diff_work(before, after):
    """Bound SequenceMatcher's equal-line pair search before running it."""
    left, right = Counter(before), Counter(after)
    return sum(count * right.get(line, 0) for line, count in left.items())


def changed_lines(before, after):
    """Count LF-line edits with bounded work; pathological inputs use a conservative upper bound."""
    left, right = lines(before), lines(after)
    first, last = 0, 0
    while first < min(len(left), len(right)) and left[first] == right[first]:
        first += 1
    while last < min(len(left), len(right)) - first and left[len(left)-last-1] == right[len(right)-last-1]:
        last += 1
    left, right = left[first:len(left)-last], right[first:len(right)-last]
    if diff_work(left, right) > DIFF_WORK_LIMIT:
        return max(len(left) + len(right), int(before != after))
    matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
    total = sum(i2-i1 + j2-j1 for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal")
    return max(total, int(before != after))


def validate(before, after, plan, config, execution):
    """Validate the complete approved-base-to-candidate change and return its sorted paths."""
    paths = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    if not paths:
        raise Blocked("candidate changes nothing")
    outside = [path for path in paths if not path_allowed(path, config, execution) or not checks.in_scope(path, plan)]
    if outside:
        raise OutsideScope(outside)
    scope = checks.scope_of(plan)
    if len(paths) > scope["max_files"]:
        raise Blocked(f"candidate changes {len(paths)} files, over the approved budget of {scope['max_files']}")
    total = sum(changed_lines(before.get(path), after.get(path)) for path in paths if not checks.generated(path, config["paths"]))
    if scope["max_changed_lines"] is not None and total > scope["max_changed_lines"]:
        raise Blocked(f"candidate changes {total} lines, over the approved budget of {scope['max_changed_lines']}")
    return paths


def development(report):
    """Require an explanation, agent-reported validation and either edits or a design-reopen request."""
    if (not isinstance(report, dict) or set(report) != {"summary", "validation", "edits", "design_issue"}
            or not isinstance(report["summary"], str) or not report["summary"].strip()
            or not isinstance(report["validation"], str) or len(report["validation"].encode()) > 4000):
        raise Blocked("invalid implementation response")
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
