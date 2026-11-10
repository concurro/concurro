"""Language-independent path envelopes and generated-file policy."""

from fnmatch import fnmatchcase
from functools import lru_cache

SCOPE_KEYS = {"paths", "max_files"}
MAX_SCOPE_PATHS, MAX_SCOPE_FILES = 8, 32
MAX_SCOPE_PATH_CHARACTERS = 200
MAX_PLAN_FILES = 8
MAX_OVERLAY_FILES = MAX_SCOPE_FILES + MAX_PLAN_FILES


def glob_matches(path, pattern):
    """Match path segments with ** covering zero or more directories."""
    parts, patterns = path.split("/"), pattern.split("/")

    @lru_cache(maxsize=None)
    def match(index, pattern_index):
        """Memoize wildcard branches within this bounded path comparison."""
        if pattern_index == len(patterns):
            return index == len(parts)
        if patterns[pattern_index] == "**":
            return match(index, pattern_index + 1) or index < len(parts) and match(index + 1, pattern_index)
        return (
            index < len(parts)
            and fnmatchcase(parts[index], patterns[pattern_index])
            and match(index + 1, pattern_index + 1)
        )

    return match(0, 0)


def _strings(value, minimum, maximum, length=MAX_SCOPE_PATH_CHARACTERS):
    """Return whether a value is a bounded list of distinct non-empty strings."""
    return (
        isinstance(value, list)
        and minimum <= len(value) <= maximum
        and all(isinstance(v, str) and v.strip() and len(v) <= length for v in value)
        and len(set(value)) == len(value)
    )


def _relative(value):
    """Accept relative repository paths and globs without traversal or absolute roots."""
    return (
        isinstance(value, str)
        and bool(value)
        and not value.startswith("/")
        and "\\" not in value
        and all(p not in ("", ".", "..") for p in value.split("/"))
    )


def relative_path(value):
    """Expose the repository-relative path predicate without changing its validation rules."""
    return _relative(value)


def scope_pattern(value):
    """Treat a trailing slash as a recursive directory scope, without relaxing path validation."""
    return value + "**" if isinstance(value, str) and value.endswith("/") else value


def scope_of(plan):
    """Return path and file limits, ignoring legacy line budgets without mutating the plan."""
    scope = plan.get("scope")
    if scope is not None:
        return {"paths": [scope_pattern(path) for path in scope["paths"]], "max_files": scope["max_files"]}
    return {"paths": list(plan["files"]), "max_files": len(plan["files"])}


def in_scope(path, plan):
    """Whether a path lies inside the plan's envelope."""
    return any(glob_matches(path, pattern) for pattern in scope_of(plan)["paths"])


def scope_errors(plan):
    """Why a plan's `scope` is invalid, or None."""
    scope = plan.get("scope")
    if scope is None:
        return None
    # Ignore the obsolete field even if a new response includes it; it grants no authority.
    if not isinstance(scope, dict) or set(scope) - {"max_changed_lines"} != SCOPE_KEYS:
        return "plan scope needs paths and max_files"
    if not _strings(scope["paths"], 1, MAX_SCOPE_PATHS):
        return (
            "plan scope paths must contain 1–8 distinct non-empty strings "
            f"(at most {MAX_SCOPE_PATH_CHARACTERS} characters each)"
        )
    if not all(_relative(scope_pattern(v)) for v in scope["paths"]):
        return "plan scope paths must be relative paths, directories or globs without empty, . or .. components"
    if type(scope["max_files"]) is not int or not len(plan["files"]) <= scope["max_files"] <= MAX_SCOPE_FILES:
        return "plan scope max_files must cover its primary files and be at most 32"
    if not all(in_scope(path, plan) for path in plan["files"]):
        return "plan files must lie inside the plan scope"
    return None


def generated(path, policy):
    """Whether target configuration marks a path as generated (aggregate bounds still apply)."""
    return any(glob_matches(path, pattern) for pattern in policy.get("generated", []))
